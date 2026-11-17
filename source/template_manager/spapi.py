"""Amazon Selling Partner API client. Standard library only.

Credentials stay on this machine: the system credential vault (see vault.py),
environment variables, or a file outside the repository. They are never returned
to the browser, logged, or put in errors.

What this client will send, decided in `permitted` before any request is made:
- GET: always (read-only).
- PUT/PATCH on Listings Items with mode=VALIDATION_PREVIEW: validates and discards.
- POST to create a report request: asks Amazon to prepare a read-only export.
- A live PATCH on Listings Items, only when the caller passes live=True, and only
  replacing the price offer or the stock quantity.
- A live PUT of one complete listing, only when the caller passes live=True.
  Callers reach both through operations.Changes.submit, after a preview and a
  person's confirmation of that one SKU.
Anything else is refused.
"""
import json
import os
import secrets
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ENDPOINT = 'https://sellingpartnerapi-eu.amazon.com'
TOKEN_URL = 'https://api.amazon.com/auth/o2/token'
MARKETPLACE_IDS = {'AE': 'A2VIGQ35RCS4UG', 'SA': 'A17E79C6D8DWNP'}
KEYS = ('client_id', 'client_secret', 'refresh_token', 'seller_id')
LABELS = {'client_id': 'Client ID', 'client_secret': 'Client Secret', 'refresh_token': 'Refresh Token', 'seller_id': '卖家编号（Merchant Token）'}
ENV = {key: 'SPAPI_' + key.upper() for key in KEYS}
LISTING_PATH = '/listings/2021-08-01/items/'
PREVIEW = 'VALIDATION_PREVIEW'
REPORTS_PATH = '/reports/2021-06-30/reports'
LIVE_PATCH_PATHS = ('/attributes/purchasable_offer', '/attributes/fulfillment_availability')
MIN_INTERVAL = 0.25  # under the 5 requests/second Listings Items limit
TIMEOUT = 30
MAX_RESPONSE = 32 * 1024 * 1024
DEFAULT_STORE = 'default'
DEFAULT_STORE_NAME = '主店铺'
STORE_LIMIT = 10  # a private application can be self-authorized by at most 10 seller accounts
SHARED_KEYS = ('client_id', 'client_secret')


class SpApiError(ValueError):
    def __init__(self, message, status=None, code=''):
        super().__init__(message)
        self.status, self.code = status, code


def default_config_path(environ=None):
    environ = os.environ if environ is None else environ
    custom = environ.get('SPAPI_CONFIG', '').strip()
    return Path(custom) if custom else Path.home() / '.amazon-template-manager' / 'spapi.json'


class Client:
    def __init__(self, config_path=None, environ=None, opener=None, sleep=time.sleep, clock=time.monotonic, vault=None,
                 store_id=DEFAULT_STORE, legacy=True, shared=None):
        # store_id: which of the seller's stores this client talks to.
        # legacy: also read environment variables and the plain file (the first store only).
        # shared: returns the first store's Client ID/Secret, used when this store has none of its own.
        self.store_id, self.label, self.legacy, self.shared = store_id, DEFAULT_STORE_NAME, legacy, shared
        self.environ = (os.environ if environ is None else environ) if legacy else {}
        self.config_path = Path(config_path) if config_path else default_config_path(self.environ)
        self.opener = opener or urllib.request.urlopen
        self.sleep, self.clock = sleep, clock
        self.lock = threading.Lock()
        self.pace = threading.Lock()
        self.last_call = 0.0
        self.access = None  # (token, expires_at, credential signature)
        self.vault = vault  # None: only environment variables and the file are used

    # --- credentials -----------------------------------------------------
    def credentials(self):
        """Read on every use, so a newly saved value works without a restart.

        For each item: an environment variable wins, then the system vault, then the file.
        """
        values, problem = {}, ''
        try:
            if self.legacy and self.config_path.is_file():
                if self.config_path.stat().st_size > 64 * 1024:
                    raise ValueError('too large')
                loaded = json.loads(self.config_path.read_text(encoding='utf-8-sig'))
                if not isinstance(loaded, dict):
                    raise ValueError('not an object')
                values = {key: loaded[key].strip() for key in KEYS if isinstance(loaded.get(key), str)}
        except (OSError, ValueError):
            problem = '凭证文件无法读取或不是有效的 JSON，请核对文件格式'
        stored = {}
        if self.vault is not None:
            try:
                stored = self.vault.read()
            except (OSError, ValueError):
                problem = problem or '无法读取系统凭据管理器'
        sources = set()
        for key in KEYS:
            env = self.environ.get(ENV[key], '').strip()
            kept = stored.get(key, '').strip() if isinstance(stored.get(key), str) else ''
            if env:
                values[key] = env
                sources.add('env')
            elif kept:
                values[key] = kept
                sources.add('vault')
            elif values.get(key):
                sources.add('file')
        if self.shared is not None and not any(values.get(key) for key in SHARED_KEYS):
            try:
                common = self.shared()
            except (OSError, ValueError):
                common = {}
            if all(common.get(key) for key in SHARED_KEYS):
                values.update({key: common[key] for key in SHARED_KEYS})
                sources.add('shared')
        return values, sources, problem

    def save(self, entered):
        """Store credentials in the system vault. A blank item keeps what the vault already has."""
        if self.vault is None:
            raise SpApiError('这台电脑不支持系统凭据管理器，请改用环境变量或凭证文件')
        if not isinstance(entered, dict):
            raise SpApiError('凭证格式无效')
        current = {key: value for key, value in self.vault.read().items() if key in KEYS}
        for key in KEYS:
            value = entered.get(key, '')
            if not isinstance(value, str) or len(value) > 2000 or re.search(r'[\x00-\x1f\x7f]', value.strip()):
                raise SpApiError(LABELS[key] + ' 含有无效字符或过长')
            if value.strip():
                current[key] = value.strip()
        own = dict(current)
        if self.shared is not None and not any(current.get(key) for key in SHARED_KEYS):
            # This store uses the first store's application: only its own token and seller are kept.
            common = self.shared()
            current.update({key: common[key] for key in SHARED_KEYS if common.get(key)})
        missing = [LABELS[key] for key in KEYS if not current.get(key)]
        if missing:
            raise SpApiError('还缺少：' + '、'.join(missing))
        if not current['client_id'].startswith('amzn1.application-oa2-client.'):
            raise SpApiError('Client ID 应以 amzn1.application-oa2-client. 开头，请核对是否填反了')
        if not current['refresh_token'].startswith('Atzr|'):
            raise SpApiError('Refresh Token 应以 Atzr| 开头，请核对是否填反了')
        if len(current['client_secret']) < 20 or current['client_secret'].startswith(('Atzr|', 'Atza|')):
            raise SpApiError('Client Secret 看起来不对，请核对是否填反了')
        if not re.fullmatch(r'A[A-Z0-9]{5,30}', current['seller_id']):
            raise SpApiError('卖家编号应为以 A 开头的大写字母和数字（卖家后台“账户信息 → 卖家记号”）')
        self.vault.write(own)
        with self.lock:
            self.access = None

    def forget(self):
        """Remove the credentials kept in the system vault."""
        if self.vault is not None:
            self.vault.delete()
        with self.lock:
            self.access = None

    def public(self):
        """What the browser may know: never a credential value."""
        values, sources, problem = self.credentials()
        missing = [LABELS[key] for key in KEYS if not values.get(key)]
        seller = values.get('seller_id', '')
        return {
            'configured': not missing, 'missing': missing, 'problem': problem,
            'store': self.store_id, 'name': self.label, 'shared_app': 'shared' in sources,
            'source': ' + '.join(label for name, label in (('vault', getattr(self.vault, 'name', '系统凭据管理器')), ('env', '环境变量'), ('file', '本机凭证文件'), ('shared', '沿用主店铺的应用')) if name in sources),
            'vault': self.vault is not None, 'vault_name': getattr(self.vault, 'name', ''), 'in_vault': 'vault' in sources, 'plain_file': 'file' in sources or (self.legacy and self.config_path.is_file()),
            'config_file': str(self.config_path), 'seller_hint': '…' + seller[-4:] if len(seller) >= 8 else '',
            'endpoint': ENDPOINT, 'marketplace_ids': dict(MARKETPLACE_IDS),
        }

    def require(self):
        values, _, problem = self.credentials()
        missing = [LABELS[key] for key in KEYS if not values.get(key)]
        if missing:
            raise SpApiError((problem + '；' if problem else '') + '店铺“%s”的亚马逊接口凭证尚未配置完整，缺少：' % self.label + '、'.join(missing) + '。请由管理员在服务器本机配置')
        if not re.fullmatch(r'A[A-Z0-9]{5,30}', values['seller_id']):
            raise SpApiError('卖家编号格式不对：应为以 A 开头的大写字母和数字（卖家后台“账户信息 → 卖家记号”）')
        return values

    def seller_id(self):
        return self.require()['seller_id']

    def scrub(self, text, values=None):
        """Remove any credential or token that an upstream message might echo."""
        text = str(text)
        secrets = [v for k, v in (values or self.credentials()[0]).items() if k != 'seller_id' and v]
        if self.access:
            secrets.append(self.access[0])
        for secret in secrets:
            text = text.replace(secret, '***')
        return re.sub(r'Atz[ar]\|[A-Za-z0-9_\-+/=.]+', '***', text)[:600]

    # --- transport -------------------------------------------------------
    def open(self, request):
        try:
            with self.opener(request, timeout=TIMEOUT) as response:
                return response.status, dict(response.headers), response.read(MAX_RESPONSE + 1)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, dict(e.headers or {}), e.read(MAX_RESPONSE + 1)
        except (urllib.error.URLError, OSError) as e:
            raise SpApiError('无法连接亚马逊接口，请检查服务器网络：' + self.scrub(getattr(e, 'reason', e))) from None

    def token(self, values):
        signature = (values['client_id'], values['client_secret'], values['refresh_token'])
        with self.lock:
            if self.access and self.access[2] == signature and self.clock() < self.access[1]:
                return self.access[0]
            form = urllib.parse.urlencode({'grant_type': 'refresh_token', 'refresh_token': values['refresh_token'], 'client_id': values['client_id'], 'client_secret': values['client_secret']}).encode()
            request = urllib.request.Request(TOKEN_URL, data=form, method='POST', headers={'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8'})
            status, _, raw = self.open(request)
            try:
                data = json.loads(raw)
            except ValueError:
                data = {}
            token = data.get('access_token') if isinstance(data, dict) else None
            if status != 200 or not isinstance(token, str) or not token:
                reason = data.get('error_description') or data.get('error') or '' if isinstance(data, dict) else ''
                hint = '；Client Secret 可能已过期或已轮换，Refresh Token 可能已失效' if status in (400, 401) else ''
                raise SpApiError('换取亚马逊访问令牌失败（HTTP %s）%s%s' % (status, '：' + self.scrub(reason, values) if reason else '', hint), status)
            try:
                life = float(data.get('expires_in', 3600))
            except (TypeError, ValueError):
                life = 3600
            self.access = (token, self.clock() + max(60, life - 120), signature)
            return token

    @staticmethod
    def permitted(method, path, query, body, live):
        if method == 'GET':
            return True
        if method in ('PUT', 'PATCH') and path.startswith(LISTING_PATH) and query.get('mode') == PREVIEW:
            return True
        if method == 'POST' and path == REPORTS_PATH:
            return True
        if live is True and method == 'PATCH' and path.startswith(LISTING_PATH) and 'mode' not in query:
            patches = body.get('patches') if isinstance(body, dict) else None
            return isinstance(patches, list) and bool(patches) and all(isinstance(p, dict) and p.get('op') == 'replace' and p.get('path') in LIVE_PATCH_PATHS for p in patches)
        if live is True and method == 'PUT' and path.startswith(LISTING_PATH) and 'mode' not in query:
            return isinstance(body, dict) and body.get('requirements') == 'LISTING' and isinstance(body.get('productType'), str) and bool(body['productType']) and isinstance(body.get('attributes'), dict) and bool(body['attributes'])
        return False

    def call(self, method, path, query=None, body=None, live=False):
        """One SP-API request. Refuses anything outside the short list in the module docstring."""
        query = dict(query or {})
        if not self.permitted(method, path, query, body, live):
            raise SpApiError('已拦截：这个请求不在允许范围内（只读查询、预检、生成报告，以及经确认的上架、改价／改库存）')
        if not path.startswith('/') or '..' in path or '?' in path or '#' in path:
            raise SpApiError('接口路径无效')
        values = self.require()
        url = ENDPOINT + path + ('?' + urllib.parse.urlencode(query, doseq=True) if query else '')
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        for attempt in range(4):
            headers = {'x-amz-access-token': self.token(values), 'Accept': 'application/json', 'User-Agent': 'listing-tool/1.0 (Language=Python)'}
            if data is not None:
                headers['Content-Type'] = 'application/json'
            with self.pace:
                wait = self.last_call + MIN_INTERVAL - self.clock()
                if wait > 0:
                    self.sleep(wait)
                self.last_call = self.clock()
            status, _, raw = self.open(urllib.request.Request(url, data=data, method=method, headers=headers))
            if status == 429 and attempt < 3:
                self.sleep(1.5 * (attempt + 1))
                continue
            if status in (401, 403) and attempt == 0:
                # An expired or revoked access token: fetch a fresh one once.
                with self.lock:
                    self.access = None
                if status == 401:
                    continue
            break
        if len(raw) > MAX_RESPONSE:
            raise SpApiError('亚马逊返回内容过大，已中止')
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {}
        if 200 <= status < 300:
            return payload
        errors = payload.get('errors') if isinstance(payload, dict) else None
        first = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}
        code, message = str(first.get('code') or ''), self.scrub(first.get('message') or '', values)
        hints = {403: '；可能是应用没有对应角色，或授权已失效', 404: '', 429: '；请求过于频繁，请稍后再试'}
        raise SpApiError('亚马逊接口返回错误（HTTP %s%s）%s%s' % (status, ' ' + code if code else '', '：' + message if message else '', hints.get(status, '')), status, code)

    def fetch_document(self, url):
        """Download a JSON document Amazon linked to. No token is sent to that host."""
        try:
            return json.loads(self.fetch_raw(url))
        except ValueError:
            raise SpApiError('亚马逊字段定义不是有效的 JSON') from None

    def fetch_raw(self, url):
        """Download a schema or report document Amazon linked to. No token is sent to that host."""
        parsed = urllib.parse.urlparse(url if isinstance(url, str) else '')
        if parsed.scheme != 'https' or not parsed.hostname or not re.search(r'(^|\.)(amazon\.com|amazonaws\.com|media-amazon\.com)$', parsed.hostname):
            raise SpApiError('亚马逊返回的文档地址不在预期范围，已中止')
        status, _, raw = self.open(urllib.request.Request(url, method='GET', headers={'User-Agent': 'listing-tool/1.0 (Language=Python)'}))
        if status != 200 or len(raw) > MAX_RESPONSE:
            raise SpApiError('下载亚马逊文档失败（HTTP %s），或文档过大' % status, status)
        return raw


class Stores:
    """The seller's own stores, each authorized separately; one Client per store.

    The first store ("default") keeps today's setup: vault entry, environment variables or file.
    Each further store keeps its own Refresh Token and seller ID in its own vault entry, and may
    reuse the first store's Client ID/Secret. Names live in a small file; it holds no secrets.
    """

    def __init__(self, default, index_path, vault_for=None, **client_options):
        self.get_default = default if callable(default) else (lambda: default)
        self.index_path = Path(index_path)
        self.vault_for, self.options = vault_for, client_options
        self.lock = threading.RLock()
        self.clients = {}

    # --- index -----------------------------------------------------------
    def entries(self):
        rows = []
        try:
            if self.index_path.is_file() and self.index_path.stat().st_size <= 64 * 1024:
                loaded = json.loads(self.index_path.read_text(encoding='utf-8-sig'))
                rows = [row for row in (loaded.get('stores') if isinstance(loaded, dict) else None) or []
                        if isinstance(row, dict) and isinstance(row.get('id'), str) and isinstance(row.get('name'), str)
                        and re.fullmatch(r'default|s[0-9a-f]{8}', row['id'])]
        except (OSError, ValueError):
            rows = []
        seen, unique = set(), []
        for row in rows:
            if row['id'] not in seen:
                seen.add(row['id'])
                unique.append({'id': row['id'], 'name': row['name']})
        if DEFAULT_STORE not in seen:
            unique.insert(0, {'id': DEFAULT_STORE, 'name': DEFAULT_STORE_NAME})
        return unique[:STORE_LIMIT]

    def write(self, rows):
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.index_path.with_name(self.index_path.name + '.tmp')
        temporary.write_text(json.dumps({'stores': rows}, ensure_ascii=False, indent=1), encoding='utf-8')
        os.replace(temporary, self.index_path)

    @staticmethod
    def clean_name(name, rows, own=None):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40 or re.search(r'[\x00-\x1f\x7f]', name):
            raise SpApiError('店铺名称须为 1–40 个字')
        name = name.strip()
        if any(row['name'] == name and row['id'] != own for row in rows):
            raise SpApiError('已有同名店铺：' + name)
        return name

    # --- clients ---------------------------------------------------------
    def shared(self):
        values = self.get_default().credentials()[0]
        return {key: values[key] for key in SHARED_KEYS if values.get(key)}

    def client(self, store=None):
        """The Client for one store; no value means the first store."""
        with self.lock:
            rows = self.entries()
            if store in (None, '', DEFAULT_STORE):
                found = self.get_default()
                found.label = next(row['name'] for row in rows if row['id'] == DEFAULT_STORE)
                return found
            row = next((row for row in rows if row['id'] == store), None) if isinstance(store, str) else None
            if row is None:
                raise SpApiError('所选店铺不存在，请刷新页面后重新选择店铺')
            found = self.clients.get(store)
            if found is None:
                vault = self.vault_for(TARGET_PREFIX + store) if self.vault_for else None
                found = self.clients[store] = Client(vault=vault, store_id=store, legacy=False, shared=self.shared, **self.options)
            found.label = row['name']
            return found

    def public(self):
        """What the browser may know about every store: names and status, never a credential."""
        with self.lock:
            stores = []
            for row in self.entries():
                shown = self.client(row['id']).public()
                stores.append({key: shown[key] for key in ('store', 'name', 'configured', 'missing', 'problem', 'seller_hint', 'shared_app', 'source', 'in_vault')})
            return {'stores': stores, 'limit': STORE_LIMIT}

    def seller_owner(self, seller, own=None):
        for row in self.entries():
            if row['id'] != own and self.client(row['id']).credentials()[0].get('seller_id') == seller:
                return row['name']
        return None

    def save(self, store, name, entered):
        """Add a store (store is empty or 'new') or update one: its name and/or credentials."""
        if not isinstance(entered, dict):
            raise SpApiError('凭证格式无效')
        typed = {key: entered.get(key).strip() for key in KEYS if isinstance(entered.get(key), str) and entered.get(key).strip()}
        with self.lock:
            rows = self.entries()
            adding = store == 'new'
            store = DEFAULT_STORE if store in (None, '') else store
            if adding:
                if len(rows) >= STORE_LIMIT:
                    raise SpApiError('最多接入 %d 个店铺（亚马逊私有应用的自授权上限）' % STORE_LIMIT)
                if self.vault_for is None or self.vault_for(TARGET_PREFIX + 'check') is None:
                    raise SpApiError('这台电脑不支持系统凭据管理器，无法添加更多店铺')
                store = 's' + secrets.token_hex(4)
                rows.append({'id': store, 'name': self.clean_name(name, rows, store)})
            else:
                row = next((row for row in rows if row['id'] == store), None) if isinstance(store, str) else None
                if row is None:
                    raise SpApiError('所选店铺不存在，请刷新页面后重新选择店铺')
                if name is not None and name != '':
                    row['name'] = self.clean_name(name, rows, store)
            if typed.get('seller_id'):
                owner = self.seller_owner(typed['seller_id'], store)
                if owner:
                    raise SpApiError('这个卖家编号已经接在店铺“%s”下，同一个店铺不用重复添加' % owner)
            target = self.client(store) if not adding else None
            if adding:
                self.clients.pop(store, None)
                vault = self.vault_for(TARGET_PREFIX + store)
                target = self.clients[store] = Client(vault=vault, store_id=store, legacy=False, shared=self.shared, **self.options)
            if adding or typed:
                try:
                    target.save(typed)
                except Exception:
                    if adding:
                        self.clients.pop(store, None)
                    raise
            self.write(rows)
            return self.client(store).public()

    def remove(self, store):
        """The first store: forget its saved credentials. Any other store: remove it completely."""
        with self.lock:
            if store in (None, '', DEFAULT_STORE):
                self.get_default().forget()
                return
            rows = self.entries()
            if not any(row['id'] == store for row in rows):
                raise SpApiError('所选店铺不存在，请刷新页面后重新选择店铺')
            self.client(store).forget()
            self.clients.pop(store, None)
            self.write([row for row in rows if row['id'] != store])

    def find(self, fingerprint, fingerprint_of):
        """The name of the store whose seller has this fingerprint, if any."""
        for row in self.entries():
            try:
                seller = self.client(row['id']).require()['seller_id']
            except (SpApiError, AttributeError, KeyError, TypeError):
                found = self.client(row['id'])
                try:
                    seller = found.seller_id()
                except (SpApiError, AttributeError):
                    continue
            try:
                if seller and fingerprint_of(seller) == fingerprint:
                    return row
            except ValueError:
                continue
        return None


TARGET_PREFIX = 'AmazonKing/SP-API/'


def marketplace_id(marketplace):
    if marketplace not in MARKETPLACE_IDS:
        raise SpApiError('亚马逊接口目前只支持 AE 和 SA 站点')
    return MARKETPLACE_IDS[marketplace]


def participations(client):
    """Read-only connection test: the marketplaces this seller account sells in."""
    payload = client.call('GET', '/sellers/v1/marketplaceParticipations')
    rows = []
    for item in payload.get('payload') or []:
        market, part = item.get('marketplace') or {}, item.get('participation') or {}
        rows.append({'id': market.get('id', ''), 'name': market.get('name', ''), 'country': market.get('countryCode', ''), 'currency': market.get('defaultCurrencyCode', ''), 'participating': bool(part.get('isParticipating')), 'suspended': bool(part.get('hasSuspendedListings'))})
    return rows
