#!/usr/bin/env python3
"""Local Amazon template manager. Python 3.9+, no external dependencies."""
import argparse
import base64
import binascii
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import getpass
import re
from http.cookies import SimpleCookie
from pathlib import Path
import shutil
import tempfile
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlparse
import zipfile

from template_manager import library, aliases
from template_manager.auth import Auth
from template_manager import inventory, filling, copywriting
from template_manager import ai
from template_manager import roster
from template_manager import batch
from template_manager import processing
from template_manager import spapi, listings, operations, vault, generated_templates, lookalike, lookalike_history, watch, insights
from template_manager.locks import LibraryLock

WEB = Path(__file__).parent / 'web'
MAX_BODY = 46 * 1024 * 1024
INVENTORY_MAX_FILE = 100 * 1024 * 1024
# Binary upload: body = UTF-8 JSON metadata (X-Template-Meta-Length bytes) followed by the raw file.
UPLOAD_TYPE = 'application/x-template-upload'
META_MAX = 16 * 1024 * 1024
LOGIN_MAX = 64 * 1024


def file_limit(path):
    return INVENTORY_MAX_FILE if path in ('/api/inventory/inspect','/api/inventory/analyze') else 32*1024*1024


class InventoryProgress:
    """Small member-scoped progress snapshots; never retain inventory contents."""
    def __init__(self):
        self.lock = threading.Lock()
        self.jobs = {}

    def start(self, user, job):
        if not isinstance(job, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,80}', job):
            raise ValueError('识别任务编号无效')
        with self.lock:
            now = time.monotonic()
            self.jobs = {key: value for key, value in self.jobs.items() if now-value[0] < 3600}
            key = (user, job)
            if key in self.jobs:
                raise ValueError('识别任务已提交，请等待结果')
            if len(self.jobs) >= 256:
                del self.jobs[next(iter(self.jobs))]
            self.jobs[key] = (now, {'stage': 'parse', 'completed': 0, 'total': 0})

    def update(self, user, job, state):
        with self.lock:
            if (user, job) in self.jobs:
                self.jobs[(user, job)] = (time.monotonic(), dict(state))

    def get(self, user, job):
        with self.lock:
            found = self.jobs.get((user, job))
            return dict(found[1]) if found and time.monotonic()-found[0] < 3600 else None


def error_text(e):
    """Turn library errors into messages a team member can act on."""
    if isinstance(e, zipfile.BadZipFile):
        return '文件已损坏或不是有效的 Excel 工作簿（旧版 .xls 改扩展名也会这样，请用 Excel 另存为 .xlsx）'
    return str(e)


class Handler(BaseHTTPRequestHandler):
    # Per socket read/write, not per request: a stalled client cannot hold a thread forever.
    timeout = 120

    def __init__(self, *args, data_dir, **kwargs):
        self.root = Path(data_dir)
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):
        # Do not log uploaded filenames or metadata.
        pass

    def send(self, data, status=200, mime='application/json; charset=utf-8', filename=None, cookie=None, extra_headers=None):
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        if cookie:
            self.send_header('Set-Cookie', cookie)
        for key, value in (extra_headers or {}).items():
            self.send_header(key, str(value))
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' blob: data: https://m.media-amazon.com https://images-na.ssl-images-amazon.com https://images-eu.ssl-images-amazon.com; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'")
        if filename:
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(filename, safe=''))
        self.end_headers()
        self.wfile.write(data)

    def trusted(self, pending=0):
        host = self.headers.get('Host', '')
        allowed = self.server.allowed_hosts
        if host not in allowed:
            self.discard(pending)
            self.send({'error': '访问域名未配置'}, 403)
            return False
        origin = self.headers.get('Origin')
        if origin and origin != self.server.scheme + '://' + host:
            self.discard(pending)
            self.send({'error': '拒绝跨站请求'}, 403)
            return False
        return True

    def body_size(self):
        """Declared body length, for draining small rejected requests (never above MAX_BODY)."""
        try:
            size = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            return 0
        return size if 0 < size <= MAX_BODY else 0

    def token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
            return cookie['session'].value if 'session' in cookie else ''
        except Exception:
            return ''

    def discard(self, remaining):
        """Drain an unread request body in small chunks so the client receives the reply."""
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 64 * 1024))
            if not chunk:
                break
            remaining -= len(chunk)

    def identity(self, roles=None, pending=0):
        user = self.server.auth.user(self.token())
        if not user or (roles and user['role'] not in roles):
            self.discard(pending)
            if not user:
                self.send({'error': '请先登录'}, 401)
            else:
                self.send({'error': '当前账号没有此操作权限'}, 403)
            return None
        return user

    def read_exact(self, size):
        data = self.rfile.read(size)
        if len(data) != size:
            raise ValueError('上传中断，请重试')
        return data

    def call(self, command, **kwargs):
        return library.run(SimpleNamespace(library=str(self.root), command=command, **kwargs))

    def do_GET(self):
        if not self.trusted():
            return
        path = urlparse(self.path)
        try:
            if not path.path.startswith('/api/'):
                assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                          '/generated-templates.js': ('generated-templates.js', 'text/javascript; charset=utf-8'),
                          '/lookalike.js': ('lookalike.js', 'text/javascript; charset=utf-8'),
                          '/oauth.js': ('oauth.js', 'text/javascript; charset=utf-8'),
                          '/watch.js': ('watch.js', 'text/javascript; charset=utf-8'),
                          '/insights.js': ('insights.js', 'text/javascript; charset=utf-8'),
                          '/style.css': ('style.css', 'text/css; charset=utf-8')}
                if path.path in assets:
                    name, mime = assets[path.path]
                    self.send((WEB / name).read_bytes(), mime=mime)
                else:
                    self.send({'error': '页面不存在'}, 404)
                return
            # Accounts live in their own database: no library lock is needed to check them.
            user = self.identity()
            if not user:
                return
            if path.path == '/api/inventory/progress':
                state = self.server.inventory_progress.get(user['username'], parse_qs(path.query).get('job_id', [''])[0])
                self.send(state if state is not None else {'stage': 'upload'})
                return
            if path.path == '/api/me':
                self.send(user)
                return
            if path.path == '/api/ai/settings':
                self.send(self.server.ai.public())
                return
            if path.path == '/api/amazon/status':
                store = parse_qs(path.query).get('store', [''])[0]
                self.send({**self.spapi_client(store).public(), **self.server.spapi_stores.public()})
                return
            if path.path == '/api/lookalike/history':
                self.send(self.server.lookalike_history.list(user))
                return
            if path.path in ('/api/lookalike/history/open', '/api/lookalike/history/export'):
                query = parse_qs(path.query)
                ident = query.get('id', [''])[0]
                if path.path.endswith('/export'):
                    data, filename, mime = self.server.lookalike_history.export(user, ident, query.get('format', ['csv'])[0])
                    self.send(data, mime=mime, filename=filename)
                else:
                    self.send(self.server.lookalike_history.get(user, ident))
                return
            if path.path == '/api/shops/roster' and user['role'] != 'admin':
                self.send({'error': '当前账号没有此操作权限'}, 403)
                return
            # Read-only: other readers proceed in parallel. The reply is sent after the lock is
            # released, so a slow client downloading a large file never holds up other members.
            with self.server.library_lock.shared(), tempfile.TemporaryDirectory() as d:
                reply = self.read_reply(path, Path(d))
            self.send(*reply[0], **reply[1])
        except Exception as e:
            self.send({'error': error_text(e)}, 400)

    def read_reply(self, path, scratch):
        """Build a GET reply (args, kwargs for send) while holding the shared library lock."""
        if path.path == '/api/templates':
            q = parse_qs(path.query)
            return (self.call('list', marketplace=q.get('marketplace', [None])[0], category=q.get('category', [None])[0], shop=q.get('shop', [None])[0], table_type=q.get('table_type', [None])[0]),), {}
        if path.path == '/api/shops':
            return (roster.directory(self.root),), {}
        if path.path == '/api/shops/roster':
            return (roster.editable_directory(self.root),), {}
        if path.path == '/api/shops/targets':
            return (roster.targets(self.root, parse_qs(path.query).get('marketplace', [''])[0]),), {}
        if path.path == '/api/aliases':
            return (aliases.directory(self.root),), {}
        if path.path == '/api/aliases/export':
            return (aliases.export_csv(self.root),), {'mime': 'text/csv; charset=utf-8', 'filename': '商品叫法归类.csv'}
        if path.path == '/api/backup':
            dest = scratch / 'backup.zip'
            self.call('backup', output=str(dest))
            return (dest.read_bytes(),), {'mime': 'application/zip', 'filename': 'amazon-template-backup.zip'}
        if path.path.startswith('/api/download/'):
            ident = path.path.rsplit('/', 1)[-1]
            rows = self.call('list', marketplace=None, category=None)
            row = next((r for r in rows if r['id'] == ident), None)
            if row is None:
                return ({'error': '模板不存在'}, 404), {}
            return (library.verified(self.root, row),), {'mime': 'application/octet-stream', 'filename': library.download_filename(row)}
        return ({'error': '页面不存在'}, 404), {}

    def spapi_client(self, store):
        """The SP-API client of one of the seller's stores; empty means the first store."""
        return self.server.spapi_stores.client(store if isinstance(store, str) else None)

    def amazon(self, body, user):
        """Store operations for admins and editors. Only change/submit alters the live store."""
        client, name = self.spapi_client(body.get('store')), user['username']
        market = body.get('marketplace', '')
        route = self.path[len('/api/amazon/'):]
        if route == 'overview':
            self.send(operations.overview(client, market))
        elif route == 'orders':
            self.send(operations.orders(client, market, body.get('days', 7), body.get('status', '')))
        elif route == 'order-items':
            self.send(operations.order_items(client, body.get('id')))
        elif route == 'sales':
            self.send(operations.sales(client, market, body.get('days', 14)))
        elif route == 'fba-inventory':
            self.send(operations.fba_inventory(client, market, body.get('skus') or None))
        elif route == 'offers-watch':
            # Read-only: who sells on our own ASINs and who holds the Buy Box.
            self.send(self.server.offers_watch.check(client, self.server.spapi_stores, market, body.get('asins')))
        elif route == 'restock/start':
            # Read-only: asks Amazon for the FBA order report the suggestion is built from.
            days = body.get('days', 30)
            if type(days) is not int or not 7 <= days <= 30:
                raise ValueError('统计天数应为 7 到 30 之间的整数')
            self.send(self.server.spapi_reports.create(client, 'orders', market, days))
        elif route == 'restock':
            reports = self.server.spapi_reports
            state = reports.get(client, body.get('id'))
            if state.get('status') != 'DONE':
                self.send({k: state[k] for k in ('id', 'status', 'note') if k in state})
            else:
                entry = reports.entry(body.get('id'), getattr(client, 'store_id', spapi.DEFAULT_STORE))
                if entry['kind'] != 'orders':
                    raise ValueError('这不是订单报告，请重新生成补货建议')
                days = entry.get('days', 30)
                columns, rows = entry['table']
                total, recent = watch.sold_per_sku(columns, rows, days)
                stock = operations.fba_inventory(client, entry['marketplace'])
                result = watch.restock(stock['items'], total, recent, days, body.get('lead_days', 30), body.get('cover_days', 30))
                self.send({**result, 'status': 'DONE', 'marketplace': entry['marketplace'], 'stock_truncated': stock['truncated']})
        elif route == 'insights/issues':
            self.send(insights.listing_issues(client, market))
        elif route == 'insights/inbound':
            self.send(insights.inbound(client, market, body.get('days', 90)))
        elif route == 'insights/costs':
            store = getattr(client, 'store_id', spapi.DEFAULT_STORE)
            if 'values' in body:
                self.send({'costs': self.server.sku_costs.set(store, market, body.get('values'))})
            else:
                spapi.marketplace_id(market, getattr(client, 'region', None))
                self.send({'costs': self.server.sku_costs.get(store, market)})
        elif route == 'insights/profit':
            spapi.marketplace_id(market, getattr(client, 'region', None))
            shipments, refunds, services, truncated = insights.financial_events(client, body.get('days', 30))
            vat = body.get('vat_rate')
            if vat is not None and (type(vat) not in (int, float) or isinstance(vat, bool)):
                raise ValueError('增值税率应为数字')
            costs = self.server.sku_costs.get(getattr(client, 'store_id', spapi.DEFAULT_STORE), market)
            result = insights.profit(shipments, refunds, services, market, costs, vat, body.get('vat_included', True) is not False)
            self.send({**result, 'days': body.get('days', 30), 'truncated': truncated})
        elif route == 'insights/returns/start':
            days = body.get('days', 30)
            reports = self.server.spapi_reports
            self.send({'returns': reports.create(client, 'returns', market, days)['id'], 'orders': reports.create(client, 'orders', market, days)['id']})
        elif route == 'insights/returns':
            reports, store = self.server.spapi_reports, getattr(client, 'store_id', spapi.DEFAULT_STORE)
            states = {key: reports.get(client, body.get(key)) for key in ('returns', 'orders')}
            failed = next((s for s in states.values() if s.get('status') == 'FATAL'), None)
            if states['returns'].get('status') == 'CANCELLED':
                self.send({'status': 'DONE', 'empty': True, 'items': [], 'total_returned': 0, 'total_sold': 0, 'flagged': 0, 'reasons': [], 'marketplace': market})
            elif failed is not None:
                self.send({'status': failed['status'], 'note': failed.get('note', '')})
            elif any(s.get('status') not in ('DONE', 'CANCELLED') for s in states.values()):
                self.send({'status': 'IN_PROGRESS'})
            else:
                returned, ordered = reports.entry(body.get('returns'), store), reports.entry(body.get('orders'), store)
                if returned['kind'] != 'returns' or ordered['kind'] != 'orders':
                    raise ValueError('报告类型不对，请重新生成退货分析')
                # A cancelled orders report means no orders in the period.
                total = watch.sold_per_sku(*ordered['table'], ordered.get('days') or 30)[0] if ordered['table'] is not None else {}
                result = insights.returns(*returned['table'], total)
                self.send({**result, 'status': 'DONE', 'marketplace': returned['marketplace'], 'days': returned.get('days')})
        elif route == 'reviews/orders':
            self.send(self.server.reviews.candidates(client, market))
        elif route == 'reviews/send':
            # Sends Amazon's own review request for the orders a person ticked and confirmed.
            self.send(self.server.reviews.send(client, name, market, body.get('orders'), body.get('confirmed')))
        elif route == 'report/create':
            self.send(self.server.spapi_reports.create(client, body.get('kind'), market, body.get('days', 7)))
        elif route == 'report':
            self.send(self.server.spapi_reports.get(client, body.get('id')))
        elif route == 'report/download':
            data, filename = self.server.spapi_reports.download(body.get('id'), getattr(client, 'store_id', spapi.DEFAULT_STORE))
            self.send(data, mime='text/csv; charset=utf-8', filename=filename)
        elif route == 'template-check':
            with self.server.library_lock.shared():
                target = next((row for row in self.call('list', marketplace=None, category=None) if row['id'] == body.get('template_id')), None)
                if target is None:raise ValueError('所选模板不存在，请刷新模板列表')
                template_data = library.verified(self.root, target)
            book = filling.Workbook(template_data)
            try:info = filling.template_info(book, target['marketplace'], target['table_type'])
            finally:book.close()
            result = operations.template_check(client, self.server.spapi_definitions, info['fields'], info['product_types'], target['marketplace'])
            result['template'] = {key:target[key] for key in ('id','shop','marketplace','table_type','category','version')}
            result['generated_at'] = (target.get('metadata') or {}).get('generated_at', '')
            self.send(result)
        elif route == 'change/preview':
            self.send(self.server.spapi_changes.preview(client, name, market, body.get('items')))
        elif route in ('change/submit', 'listing/submit'):
            self.send(self.server.spapi_changes.submit(client, name, body.get('token'), body.get('sku'), body.get('confirmed'), overwrite=body.get('overwrite') is True))
        elif route == 'change/log':
            self.send(self.server.spapi_changes.recent())
        else:
            return False
        return True

    def do_POST(self):
        if not self.trusted(pending=self.body_size()):
            return
        kind = self.headers.get('Content-Type', '').split(';')[0].strip()
        if kind not in ('application/json', UPLOAD_TYPE):
            self.discard(self.body_size())
            self.send({'error': '请求必须为 JSON 或文件上传'}, 415)
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            limit=file_limit(self.path)
            # Check sizes from headers only; nothing is read before the caller is authenticated.
            if kind == UPLOAD_TYPE:
                meta_size = int(self.headers.get('X-Template-Meta-Length', '-1'))
                valid = 0 <= meta_size <= META_MAX and 0 < size - meta_size <= limit
            else:
                meta_size = size
                valid = 0 < size <= MAX_BODY
            if not valid:
                hint = '' if kind == UPLOAD_TYPE or size <= MAX_BODY else '；如页面是旧版本，请按 Ctrl+F5 刷新后重试'
                self.send({'error':f'文件过大或请求为空（此操作文件上限 {limit//(1024*1024)} MB）{hint}'}, 413)
                return
            if self.path.startswith('/api/generated-templates/') and (kind != 'application/json' or size > 64 * 1024):
                self.discard(self.body_size())
                self.send({'error': '规则生成请求须为 JSON，且不超过 64 KB'}, 413 if size > 64 * 1024 else 415)
                return
            if self.path.startswith('/api/lookalike/') and (kind != 'application/json' or size > 7 * 1024 * 1024):
                self.discard(self.body_size())
                self.send({'error': '以图找同款请求须为 JSON，图片不超过 4 MB'}, 413 if size > 7 * 1024 * 1024 else 415)
                return
            if self.headers.get('X-Template-Request') != '1':
                self.discard(size)
                self.send({'error': '缺少请求校验标记'}, 403)
                return
            if self.path == '/api/login':
                if kind != 'application/json' or size > LOGIN_MAX:
                    self.discard(size)
                    self.send({'error': '登录信息格式错误'}, 400)
                    return
                body = json.loads(self.read_exact(size))
                username, password = body.get('username', ''), body.get('password', '')
                if not isinstance(username, str) or not isinstance(password, str) or len(username) > 80 or len(password) > 256:
                    raise ValueError('登录信息格式错误')
                # Password hashing takes ~0.5 s and touches only the accounts database: no library lock.
                token = self.server.auth.login(username, password, self.client_address[0])
                suffix = '; Secure' if self.server.scheme == 'https' else ''
                self.send({'ok': True}, cookie=f'session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200' + suffix)
                return
            roles = ('admin',) if self.path in ('/api/users', '/api/restore', '/api/ai/settings', '/api/ai/test', '/api/amazon/test', '/api/amazon/credentials', '/api/amazon/credentials/delete', '/api/amazon/public-app', '/api/amazon/oauth/start', '/api/amazon/oauth/finish', '/api/shops/import', '/api/shops/preview') else ('admin', 'editor')
            if self.path in ('/api/logout', '/api/inventory/inspect', '/api/inventory/analyze', '/api/templates/download', '/api/processing-report', '/api/amazon/listings'):
                roles = None
            current_user = self.identity(roles, pending=size)
            if not current_user:
                return
            meta = self.read_exact(meta_size) if meta_size else b'{}'
            data = self.read_exact(size - meta_size) if kind == UPLOAD_TYPE else None
            body = json.loads(meta)
            if not isinstance(body, dict):
                raise ValueError('请求格式错误')
            if self.path == '/api/logout':
                self.server.auth.logout(self.token())
                self.send({'ok': True}, cookie='session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
                return
            if self.path == '/api/users':
                self.server.auth.create(body['username'], body['password'], body['role'])
                self.send({'ok': True})
                return
            if self.path == '/api/ai/settings':
                if self.server.scheme != 'https' and self.client_address[0] not in ('127.0.0.1', '::1'):
                    self.send({'error': 'HTTP 模式下仅允许在服务器本机配置 API 密钥，请用 127.0.0.1 打开软件'}, 403)
                    return
                self.server.ai.configure(body.get('provider'), body.get('model'), body.get('api_key', ''),body.get('region'))
                self.send(self.server.ai.public())
                return
            if self.path == '/api/ai/test':
                self.send(ai.probe(self.server.ai))
                return
            if self.path in ('/api/amazon/credentials', '/api/amazon/credentials/delete', '/api/amazon/public-app', '/api/amazon/oauth/finish'):
                # Secrets are typed only on the server computer itself, never over plain HTTP from another machine.
                if self.server.scheme != 'https' and self.client_address[0] not in ('127.0.0.1', '::1'):
                    self.send({'error': '只能在运行本软件的电脑上保存或清除凭证，请在那台电脑上用 127.0.0.1 打开软件'}, 403)
                    return
                stores = self.server.spapi_stores
                if self.path == '/api/amazon/public-app':
                    stores.save_public_app(body)
                    self.send(stores.public())
                    return
                if self.path == '/api/amazon/oauth/finish':
                    shown = stores.oauth_finish(body.get('text'))
                    self.send({**shown, **stores.public()})
                    return
                if self.path.endswith('/delete'):
                    stores.remove(body.get('store'))
                    shown = stores.client().public()
                else:
                    shown = stores.save(body.get('store'), body.get('name'), body)
                self.send({**shown, **stores.public()})
                return
            if self.path == '/api/amazon/oauth/start':
                # A consent link: whoever opens it signs in to that store on Amazon and agrees. No secret leaves this computer.
                self.send(self.server.spapi_stores.oauth_start(body.get('name'), body.get('marketplace', 'AE'), body.get('store') or None))
                return
            if self.path == '/api/amazon/test':
                # Read-only: lists the marketplaces this seller account sells in.
                client = self.spapi_client(body.get('store'))
                self.send({'marketplaces': spapi.participations(client), **client.public()})
                return
            if self.path == '/api/amazon/listings':
                # Read-only lookup of existing SKUs.
                self.send(listings.status(self.spapi_client(body.get('store')), body.get('skus'), body.get('marketplace', '')))
                return
            if self.path.startswith('/api/lookalike/'):
                # Photo -> same product: official catalog search, AI image comparison, seller offers. Read-only.
                action, service = self.path[len('/api/lookalike/'):], self.server.lookalike
                if action == 'keywords':
                    self.send(service.keywords(current_user, body))
                elif action == 'search':
                    self.send(service.search(current_user, self.spapi_client(body.get('store')), body))
                elif action == 'compare':
                    self.send(service.compare(current_user, body))
                elif action == 'prices':
                    self.send(service.prices(current_user, self.spapi_client(body.get('store')), self.server.spapi_stores, body))
                elif action in ('history/save', 'history/delete'):
                    self.send(getattr(self.server.lookalike_history, action.split('/')[1])(current_user, body))
                else:
                    self.send({'error': '接口不存在'}, 404)
                return
            if self.path.startswith('/api/generated-templates/'):
                action = self.path[len('/api/generated-templates/'):]
                if action == 'artifact':
                    content, filename, mime = self.server.generated_templates.artifact(current_user, body)
                    self.send(content, mime=mime, filename=filename)
                elif action in ('search', 'generate', 'save'):
                    self.send(getattr(self.server.generated_templates, action)(current_user, body))
                else:
                    self.send({'error': '接口不存在'}, 404)
                return
            if self.path.startswith('/api/amazon/'):
                if not self.amazon(body, current_user):
                    self.send({'error': '接口不存在'}, 404)
                return
            lock = self.server.library_lock
            if self.path == '/api/template-shop':
                with lock.exclusive():
                    result = library.move_shop(self.root, body.get('id'), body.get('shop'))
                self.send(result)
                return
            if self.path == '/api/template/type':
                with lock.exclusive():
                    result = library.change_table_type(self.root, body.get('id'), body.get('table_type'))
                self.send(result)
                return
            if self.path == '/api/template/delete':
                with lock.exclusive():
                    result = library.delete_template(self.root,body.get('id'))
                self.send(result)
                return
            if self.path == '/api/templates/delete':
                with lock.exclusive():
                    result = library.delete_templates(self.root,body.get('ids'))
                self.send(result)
                return
            if self.path == '/api/templates/download':
                with lock.shared():
                    data,filename=library.download_templates(self.root,body.get('ids'))
                self.send(data,mime='application/zip' if filename.endswith('.zip') else 'application/octet-stream',filename=filename)
                return
            if self.path == '/api/shops/import':
                with lock.exclusive():
                    result = roster.import_rows(self.root,body['rows']) if 'rows' in body else roster.import_text(self.root, body.get('text'))
                self.send(result)
                return
            if self.path == '/api/shops/preview':
                groups={}
                for record in roster.parse(body.get('text')):
                    row=groups.setdefault(record['shop'],{'shop':record['shop'],'ae':'','sa':''})
                    row[record['marketplace'].lower()]=record['seller_id']
                self.send(list(groups.values()))
                return
            if self.path == '/api/aliases/preview':
                rows = aliases.parse_text(body.get('text', ''), body.get('category', ''))
                with lock.shared():result = aliases.preview(self.root, rows)
                self.send(result)
                return
            if self.path == '/api/aliases/save':
                with lock.exclusive():result = aliases.apply(self.root, body.get('rows'), current_user['username'], body.get('revision'))
                self.send(result)
                return
            if self.path == '/api/aliases/update':
                with lock.exclusive():result = aliases.update(self.root, body.get('key'), body.get('alias'), body.get('category'), current_user['username'], body.get('revision'))
                self.send(result)
                return
            if self.path == '/api/aliases/delete':
                with lock.exclusive():result = aliases.delete(self.root, body.get('keys'), body.get('revision'))
                self.send(result)
                return
            if data is None:
                data = base64.b64decode(body['data'], validate=True)
            if len(data) > limit:
                raise ValueError(f'文件不能超过 {limit//(1024*1024)} MB')
            name = body.get('filename', '')
            if not isinstance(name, str) or not name or '/' in name or '\\' in name or len(name) > 200:
                raise ValueError('文件名无效')
            if self.path == '/api/aliases/inspect':
                rows, info = aliases.parse_file(data, name, body.get('sheet'))
                with lock.shared():result = {**aliases.preview(self.root, rows), **info}
                self.send(result)
                return
            if self.path in ('/api/fill/preview', '/api/fill/export', '/api/fill/generate', '/api/fill/precheck'):
                with lock.shared():
                    templates = self.call('list', marketplace=None, category=None)
                    target = next((row for row in templates if row['id'] == body.get('template_id')), None)
                    if target is None:raise ValueError('所选填写模板不存在，请刷新模板列表')
                    if target.get('account_scope_unknown'):raise ValueError('模板来源尚未绑定店铺，请先完成归属设置')
                    if any(body.get(key) != target.get(key) for key in ('shop','marketplace','table_type')):raise ValueError('填写模板的店铺、地区或表格类型已变化，请重新选择')
                    template_data = library.verified(self.root, target)
                    alias_directory = aliases.directory(self.root)
                    rules = alias_directory['items']
                    if self.path == '/api/fill/export' and body.get('alias_revision') is not None and body['alias_revision'] != alias_directory['revision']:raise ValueError('商品叫法归类已变化，请重新检查填写结果后导出')
                options = {key:body.get(key) for key in ('sheet','header_row','mapping','defaults','by_sku')}
                options['copy_confirmed'] = body.get('copy_confirmed', False)
                options['copy_language'] = body.get('copy_language', 'en')
                options['alias_rules'] = rules
                options['replace_existing'] = body.get('replace_existing', False)
                if self.path == '/api/fill/generate':
                    if target['table_type'] != 'upload':raise ValueError('改价模式不生成标题、卖点或类目，请使用新商品上传表格')
                    if body.get('copy_consent') is not True:raise ValueError('请先同意将选定 SKU 的商品名称和参数发送到已配置的 AI 服务商')
                    if not self.server.ai_job_lock.acquire(blocking=False):
                        self.send({'error':'AI 正在处理其他任务，请稍后重试，避免重复产生费用'},409)
                        return
                    user, job = current_user['username'], body.get('job_id')
                    progress = self.server.inventory_progress
                    try:
                        if job is not None:progress.start(user,job)
                        if not self.server.fill_job_lock.acquire(blocking=False):raise ValueError('另一个填写任务正在读取资料，请稍后重试')
                        try:
                            ctx = copywriting.context(template_data,data,name,target['marketplace'],target['table_type'],language=body.get('copy_language','en'),limit=body.get('copy_limit',20),start=body.get('copy_start',1),**options)
                        finally:self.server.fill_job_lock.release()
                        def update_copy(state):
                            if job is not None:progress.update(user,job,state)
                        result = copywriting.generate(self.server.ai,ctx,cache=self.server.copy_cache if body.get('copy_cache') is not False else None,cache_scope=user,progress=update_copy)
                        result['target'] = {key:target[key] for key in ('id','shop','marketplace','table_type','category','version')}
                        result['alias_revision'] = alias_directory['revision']
                        self.send(result)
                        update_copy({'stage':'done','total':result['requested'],'completed':result['generated'],'calls':result['calls'],'cached':result['cached'],'failed':len(result['errors'])})
                    except Exception:
                        if job is not None:progress.update(user,job,{'stage':'error'})
                        raise
                    finally:self.server.ai_job_lock.release()
                    return
                if self.path == '/api/fill/precheck':
                    # Validation only (mode=VALIDATION_PREVIEW): Amazon checks the rows and saves nothing.
                    if body.get('precheck_consent') is not True:raise ValueError('请先确认将所选 SKU 的填写内容发送到亚马逊做预检')
                    client = self.spapi_client(body.get('store'))
                    client.require()
                    owner = target.get('metadata', {}).get('account_fingerprint')
                    owner_store = self.server.spapi_stores.find(owner, roster.fingerprint) if owner else None
                    switch = '这份模板属于已接入的店铺“%s”，请先切换到该店铺' % owner_store['name'] if owner_store else '请切换到这份模板所属的店铺'
                    if target.get('metadata', {}).get('generated_format') == 'spapi-v1' and owner != roster.fingerprint(client.seller_id()):
                        raise ValueError('生成表所属卖家与当前店铺“%s”的卖家不一致。%s，或重新生成；未发送预检' % (client.label, switch))
                    if not self.server.spapi_job_lock.acquire(blocking=False):
                        self.send({'error':'另一个亚马逊预检正在进行，请稍后重试'},409)
                        return
                    user, job = current_user['username'], body.get('job_id')
                    progress = self.server.inventory_progress
                    try:
                        if job is not None:progress.start(user,job)
                        if not self.server.fill_job_lock.acquire(blocking=False):raise ValueError('另一个填写任务正在读取资料，请稍后重试')
                        try:
                            book, tpl, report, rows = filling.prepare(template_data, data, name, target['marketplace'], target['table_type'], **options)
                            book.close()
                        finally:self.server.fill_job_lock.release()
                        fields = [{key:f[key] for key in ('key','label','column','canonical','generated_format','json_attribute') if key in f} for f in tpl['fields'] if f['active']]
                        def update_precheck(state):
                            if job is not None:progress.update(user,job,state)
                        result = listings.precheck(client, self.server.spapi_definitions, rows, fields, target['marketplace'], target['table_type'], start=body.get('precheck_start',1), limit=body.get('precheck_limit',20), progress=update_precheck, hold=lambda sku, path, query, payload: self.server.spapi_changes.hold_listing(user, target['marketplace'], sku, path, query, payload, getattr(client, 'store_id', spapi.DEFAULT_STORE), getattr(client, 'label', spapi.DEFAULT_STORE_NAME)))
                        result['template'] = {key:target[key] for key in ('id','shop','marketplace','table_type','category','version')}
                        result['store'] = client.label
                        if owner and owner != roster.fingerprint(client.seller_id()):
                            result['notes'].insert(0,'注意：这份模板里的卖家记号与当前店铺“%s”的卖家编号不一致，本次预检结果不适用于模板所属店铺。%s' % (client.label, switch))
                        result['local_errors'] = report['errors']
                        self.send(result)
                    except Exception:
                        if job is not None:progress.update(user,job,{'stage':'error'})
                        raise
                    finally:self.server.spapi_job_lock.release()
                    return
                if not self.server.fill_job_lock.acquire(blocking=False):
                    self.send({'error':'另一个填写任务正在处理，请稍后重试'},409)
                    return
                try:
                    if self.path == '/api/fill/preview':
                        book, _, report, _ = filling.prepare(template_data, data, name, target['marketplace'], target['table_type'], **options)
                        book.close()
                        report['target'] = {key:target[key] for key in ('id','shop','marketplace','table_type','category','version','download_name')}
                        report['alias_revision'] = alias_directory['revision']
                        self.send(report)
                    else:
                        archive, filename, report = filling.export(template_data, data, name, target['marketplace'], target['table_type'], target['download_name'], strict=body.get('strict',False), **options)
                        self.send(archive,mime='application/zip',filename=filename,extra_headers={'X-Fill-Rows':report['output_rows'],'X-Fill-Errors':report['errors'],'X-Fill-Warnings':report['warnings']})
                finally:self.server.fill_job_lock.release()
                return
            if self.path == '/api/fill/source':
                self.send(filling.inspect_source(data,name))
                return
            if self.path == '/api/processing-report':
                # Read-only: explains Amazon's report; a library template only supplies field names.
                fields, target, notes = None, None, []
                if body.get('template_id'):
                    with lock.shared():
                        target = next((row for row in self.call('list', marketplace=None, category=None) if row['id'] == body.get('template_id')), None)
                        if target is None:raise ValueError('所选模板不存在，请刷新模板列表')
                        template_data = library.verified(self.root, target)
                    try:fields = processing.template_fields(template_data, target)
                    except ValueError as e:notes.append('未能读取所选模板的字段（'+str(e)+'），已按报告本身识别字段')
                result = processing.parse(data, name, fields)
                result['template'] = {key:target[key] for key in ('id','shop','marketplace','table_type','category','version')} if target and fields else None
                result['notes'] = notes
                self.send(result)
                return
            if self.path == '/api/inventory/inspect':
                self.send(inventory.inspect(data, name, body.get('sheet'), body.get('header_row')))
                return
            if self.path == '/api/template/inspect':
                with lock.shared():
                    result = library.inspect_template(self.root, data, Path(name).suffix)
                self.send(result)
                return
            if self.path == '/api/template/batch':
                save_to_library = body.get('save_to_library', True)
                download_zip = body.get('download_zip', True)
                # Copies are generated without any lock; only the final save takes the write lock.
                archive, report = batch.generate(self.root, data, name, body.get('shops'), body.get('category'), body.get('version'), save_to_library, create_archive=download_zip, table_type=body.get('table_type', 'upload'), auto_version=body.get('auto_version', False), read_lock=lock.shared, write_lock=lock.exclusive)
                if download_zip:
                    self.send(archive, mime='application/zip', filename=report['archive_name'], extra_headers={'X-Generated-Count': report['generated'], 'X-Saved-Count': report['saved'], 'X-Reused-Count': report['reused'], 'X-Failed-Count':report['failed'], 'X-Batch-Version':quote(report['version'], safe='')})
                else:
                    self.send(report)
                return
            if self.path == '/api/inventory/analyze':
                user, job = current_user['username'], body.get('job_id')
                progress = self.server.inventory_progress
                if job is not None:
                    progress.start(user, job)
                def update(state):
                    if job is not None:
                        progress.update(user, job, state)
                try:
                    with lock.shared():
                        templates = self.call('list', marketplace=None, category=None)
                        alias_rules = aliases.directory(self.root)['items']
                    tables = inventory.read_tables(data, name)
                    report = inventory.analyze(data, name, body.get('marketplace', ''), templates, body.get('sheet'), body.get('header_row'), body.get('mapping'), body.get('category_overrides'), body.get('shop', library.GLOBAL_SHOP), tables=tables, table_type=body.get('table_type', 'upload'), template_overrides=body.get('template_overrides'), alias_rules=alias_rules)
                    if body.get('use_ai') is True:
                        if body.get('ai_consent') is not True:
                            raise ValueError('请先同意将本次分析涉及的商品资料发送到所选 AI 服务商')
                        if not self.server.ai_job_lock.acquire(blocking=False):
                            update({'stage': 'error'})
                            self.send({'error': '其他成员正在进行 AI 分析，请稍后重试，避免重复产生费用'}, 409)
                            return
                        try:
                            report = ai.enrich(report, tables, templates, data, name, self.server.ai, body.get('include_images') is True, body.get('ai_limit', 30), cache=self.server.ai_cache if body.get('use_ai_cache') is not False else None, cache_scope=user, progress=update, alias_rules=alias_rules)
                        finally:
                            self.server.ai_job_lock.release()
                    update({'stage': 'done', 'completed': len(report['items']), 'total': len(report['items'])})
                except Exception:
                    update({'stage': 'error'})
                    raise
                self.send(report)
                return
            if self.path not in ('/api/upload', '/api/restore'):
                self.send({'error': '接口不存在'}, 404)
                return
            with tempfile.TemporaryDirectory() as d, lock.exclusive():
                source = Path(d) / name
                source.write_bytes(data)
                if self.path == '/api/upload':
                    meta = {k: str(body.get(k, '')).strip() for k in ('marketplace', 'category', 'version')}
                    meta['shop'] = str(body.get('shop', library.GLOBAL_SHOP)).strip()
                    meta['table_type'] = library.validate_table_type(body.get('table_type', 'upload'))
                    if any(len(v) > 120 for v in meta.values()):
                        raise ValueError('站点、类目或版本过长')
                    self.send(self.call('add', source=str(source), **meta))
                elif self.path == '/api/restore':
                    rows = self.call('list', marketplace=None, category=None)
                    if rows:
                        raise ValueError('仅支持恢复到空模板库，请先使用完整备份妥善保留现有库')
                    stage = Path(d) / 'restored'
                    library.run(SimpleNamespace(command='restore', library=str(stage), source=str(source)))
                    # Preserve an empty existing directory until replacement has succeeded.
                    self.root.parent.mkdir(parents=True, exist_ok=True)
                    replacement = self.root.parent / ('.restore-' + stage.name + '-' + library.uuid.uuid4().hex)
                    shutil.copytree(stage, replacement)
                    old = self.root.with_name(self.root.name + '-empty-' + library.uuid.uuid4().hex)
                    self.root.rename(old)
                    try:
                        replacement.rename(self.root)
                    except Exception:
                        old.rename(self.root)
                        raise
                    shutil.rmtree(old)
                    restored = self.call('list', marketplace=None, category=None)
                    self.send({'count': len(restored)})
                else:
                    self.send({'error': '接口不存在'}, 404)
        except Exception as e:
            self.send({'error': error_text(e)}, 400)


def create_server(data_dir, port=8765, host='127.0.0.1', allowed_hosts=None, scheme='http'):
    data_dir = Path(data_dir)
    server = ThreadingHTTPServer((host, port), partial(Handler, data_dir=data_dir))
    server.auth = Auth(data_dir.parent / 'accounts.sqlite3')
    server.ai = ai.Settings(data_dir.parent / 'ai-config.json')
    server.spapi = spapi.Client(vault=vault.default())
    # The first store is whatever server.spapi is at the moment of use (tests replace it).
    server.spapi_stores = spapi.Stores(lambda: server.spapi, data_dir.parent / 'amazon-stores.json', vault_for=vault.default)
    server.spapi_definitions = listings.Definitions()
    server.spapi_job_lock = threading.Lock()
    server.spapi_reports = operations.Reports()
    server.offers_watch = watch.Offers(data_dir.parent / 'offers-watch.json')
    server.sku_costs = insights.Costs(data_dir.parent / 'sku-costs.json')
    server.reviews = insights.Reviews(data_dir.parent / 'review-requests.jsonl')
    server.spapi_changes = operations.Changes(data_dir.parent / 'amazon-changes.jsonl')
    server.library_lock = LibraryLock()
    server.generated_templates = generated_templates.Service(server.spapi, server.spapi_definitions, data_dir, server.library_lock, resolve=lambda store: server.spapi_stores.client(store if isinstance(store, str) else None))
    server.ai_job_lock = threading.Lock()
    server.fill_job_lock = threading.Lock()
    server.ai_cache = ai.ResultCache()
    server.copy_cache = ai.ResultCache()
    server.lookalike = lookalike.Service(server.ai)
    server.lookalike_history = lookalike_history.History(data_dir.parent / 'lookalike.sqlite3', server.lookalike)
    server.inventory_progress = InventoryProgress()
    server.allowed_hosts = set(allowed_hosts or [f'127.0.0.1:{server.server_port}', f'localhost:{server.server_port}'])
    server.scheme = scheme
    return server


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--port', type=int, default=8765)
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--allowed-host', action='append', help='允许的 Host，含非默认端口；可重复指定')
    p.add_argument('--https', action='store_true', help='仅在 HTTPS 反向代理后使用')
    p.add_argument('--create-user', help='创建用户并退出；交互输入密码')
    p.add_argument('--role', choices=['admin', 'editor', 'viewer'], default='admin')
    p.add_argument('--data-dir', default=str(Path(__file__).parent / '.data' / 'library'))
    a = p.parse_args()
    if a.create_user:
        password = getpass.getpass('密码（至少 12 字符）：')
        if password != getpass.getpass('再次输入密码：'):
            p.error('两次密码不同')
        Auth(Path(a.data_dir).resolve().parent / 'accounts.sqlite3').create(a.create_user, password, a.role)
        print('用户已创建：' + a.create_user)
        raise SystemExit(0)
    if a.host != '127.0.0.1' and not a.allowed_host:
        p.error('对外监听必须通过 --allowed-host 显式指定访问地址')
    server = create_server(Path(a.data_dir).resolve(), a.port, a.host, a.allowed_host, 'https' if a.https else 'http')
    print(f'无敌亚马逊大王已启动：http://127.0.0.1:{server.server_port}', flush=True)
    print(f'模板保存位置：{Path(a.data_dir).resolve()}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
