"""HTTP boundaries for research routes; no Amazon or model network calls."""
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest

from app import create_server


class ResearchStub:
    def __init__(self):
        self.calls = []

    def list(self, user, marketplace=''):
        self.calls.append(('list', user['username'], marketplace))
        return {'batches': [], 'classes': {'pending': '待核对'}}

    def get(self, user, ident):
        self.calls.append(('get', user['username'], ident))
        if ident != 'saved':
            raise ValueError('批次不存在或没有访问权限')
        return {'id': ident, 'saved': True, 'items': []}

    def export(self, user, ident, kind):
        self.get(user, ident)
        if kind != 'csv':
            raise ValueError('格式无效')
        return b'ASIN,title\r\n', 'competitors.csv', 'text/csv; charset=utf-8'

    def start(self, user, body):
        self.calls.append(('start', user['username'], body))
        return {'id': 'private', 'status': 'running'}

    import_html = start
    choose = start
    cancel = start
    save = start
    review = start
    delete = start
    score = start


class CompetitorRoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = create_server(Path(cls.tmp.name) / 'library', 0)
        cls.server.competitors = ResearchStub()
        for name, role in [('owner', 'admin'), ('writer', 'editor'), ('reader', 'viewer')]:
            cls.server.auth.create(name, 'a-secure-test-password', role)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(5)
        cls.tmp.cleanup()

    def req(self, method, path, body=None, cookie=None, headers=None):
        conn = HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        h = {'Content-Type': 'application/json', 'X-Template-Request': '1'}
        if cookie:
            h['Cookie'] = cookie
        h.update(headers or {})
        conn.request(method, path, json.dumps(body) if body is not None else None, h)
        response = conn.getresponse()
        result = response.status, response.read(), dict(response.getheaders())
        conn.close()
        return result

    def login(self, name):
        status, _, headers = self.req('POST', '/api/login', {'username': name, 'password': 'a-secure-test-password'})
        self.assertEqual(status, 200)
        return headers['Set-Cookie'].split(';')[0]

    def test_read_requires_login_and_saved_exports_keep_security_headers(self):
        self.assertEqual(self.req('GET', '/api/competitors')[0], 401)
        cookie = self.login('reader')
        status, raw, headers = self.req('GET', '/api/competitors?marketplace=AE', cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['batches'], [])
        self.assertEqual(self.server.competitors.calls[-1], ('list', 'reader', 'AE'))
        status, raw, headers = self.req('GET', '/api/competitors/export?id=saved&format=csv', cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn(b'ASIN', raw)
        self.assertIn('attachment;', headers['Content-Disposition'])
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn("default-src 'self'", headers['Content-Security-Policy'])
        self.assertEqual(self.req('GET', '/api/competitors/batch?id=private', cookie=cookie)[0], 400)

    def test_mutations_are_role_checked_and_do_not_enter_upload_parser(self):
        reader, writer = self.login('reader'), self.login('writer')
        for action in ('start', 'import', 'choose', 'cancel', 'save', 'review', 'delete', 'score'):
            self.assertEqual(self.req('POST', '/api/competitors/' + action, {}, reader)[0], 403)
            # No filename/data fields: JSON dispatch must occur before template upload code.
            status, raw, _ = self.req('POST', '/api/competitors/' + action, {'source': 'B000000001'}, writer)
            self.assertEqual(status, 200, raw)
            self.assertEqual(self.server.competitors.calls[-1][1], 'writer')
        self.assertEqual(self.req('POST', '/api/competitors/unknown', {}, writer)[0], 404)

    def test_research_routes_keep_origin_and_request_marker_checks(self):
        writer = self.login('writer')
        self.assertEqual(self.req('POST', '/api/competitors/start', {}, writer, {'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.req('POST', '/api/competitors/start', {}, writer, {'X-Template-Request': '0'})[0], 403)
        self.assertEqual(self.req('GET', '/api/competitors', cookie=writer, headers={'Host': 'evil.example'})[0], 403)

    def test_full_browser_import_payload_with_real_service(self):
        from template_manager import competitors
        from test_bsr import page, URL
        old = self.server.competitors
        self.server.competitors = competitors.Service(Path(self.tmp.name) / 'import-route.sqlite3')
        try:
            writer = self.login('writer')
            payload = {'marketplace': 'AE', 'source_url': URL, 'html': page(),
                       'target_asin': '', 'target_title': 'Travel bag', 'target_brand': 'Example',
                       'keywords': ['bag'], 'exclude_words': ['replacement']}
            status, raw, _ = self.req('POST', '/api/competitors/import', payload, writer)
            self.assertEqual(status, 200, raw)
            data = json.loads(raw)
            self.assertTrue(data['imported'])
            self.assertEqual(data['target_brand'], 'Example')
            self.assertGreater(len(data['items']), 0)
        finally:
            self.server.competitors = old

    def test_frontend_module_is_served_without_relaxing_csp(self):
        status, data, headers = self.req('GET', '/competitors.js?v=test')
        self.assertEqual(status, 200)
        self.assertIn(b'COMP_CLASSES', data)
        self.assertIn('javascript', headers['Content-Type'])
        self.assertNotIn('unsafe-inline', headers['Content-Security-Policy'])


if __name__ == '__main__':
    unittest.main()
