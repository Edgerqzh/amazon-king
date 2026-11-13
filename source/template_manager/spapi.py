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


class SpApiError(ValueError):
    def __init__(self, message, status=None, code=''):
        super().__init__(message)
        self.status, self.code = status, code


def default_config_path(environ=None):
    environ = os.environ if environ is None else environ
    custom = environ.get('SPAPI_CONFIG', '').strip()
    return Path(custom) if custom else Path.home() / '.amazon-template-manager' / 'spapi.json'


class Client:
    def __init__(self, config_path=None, environ=None, opener=None, sleep=time.sleep, clock=time.monotonic, vault=None):
        self.environ = os.environ if environ is None else environ
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
            if self.config_path.is_file():
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
        self.vault.write(current)
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
            'source': ' + '.join(label for name, label in (('vault', getattr(self.vault, 'name', '系统凭据管理器')), ('env', '环境变量'), ('file', '本机凭证文件')) if name in sources),
            'vault': self.vault is not None, 'vault_name': getattr(self.vault, 'name', ''), 'in_vault': 'vault' in sources, 'plain_file': 'file' in sources or self.config_path.is_file(),
            'config_file': str(self.config_path), 'seller_hint': '…' + seller[-4:] if len(seller) >= 8 else '',
            'endpoint': ENDPOINT, 'marketplace_ids': dict(MARKETPLACE_IDS),
        }

    def require(self):
        values, _, problem = self.credentials()
        missing = [LABELS[key] for key in KEYS if not values.get(key)]
        if missing:
            raise SpApiError((problem + '；' if problem else '') + '亚马逊接口凭证尚未配置完整，缺少：' + '、'.join(missing) + '。请由管理员在服务器本机配置')
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
