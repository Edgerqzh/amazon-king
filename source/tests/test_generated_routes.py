"""HTTP/auth integration for official-rule template generation. No real API calls."""
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest

from app import create_server


class GeneratorStub:
    def __init__(self):
        self.calls = []

    def search(self, user, body):
        self.calls.append(('search', user['username'], body))
        return {'marketplace': 'AE', 'items': [{'name': 'LUGGAGE', 'display_name': 'Luggage'}], 'truncated': False}

    def generate(self, user, body):
        self.calls.append(('generate', user['username'], body))
        return {'token': 'test-token', 'marketplace': 'AE', 'product_type': 'LUGGAGE', 'fields': [], 'expires_in': 600}

    def artifact(self, user, body):
        self.calls.append(('artifact', user['username'], body))
        return b'workbook-fixture', 'generated-AE.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

    def save(self, user, body):
        self.calls.append(('save', user['username'], body))
        return {'id': 'saved-template', 'shop': body['shop'], 'marketplace': 'AE', 'table_type': 'upload'}


class GeneratedRoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = create_server(Path(cls.tmp.name) / 'library', 0)
        cls.server.generated_templates = GeneratorStub()
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

    def test_search_generate_and_save_use_json_not_upload_fields(self):
        writer = self.login('writer')
        for action, payload in [('search', {'marketplace': 'AE', 'query': 'suitcase', 'mode': 'item_name', 'search_locale': 'en_US'}),
                                ('generate', {'marketplace': 'AE', 'product_type': 'LUGGAGE'}),
                                ('save', {'token': 'test-token', 'shop': 'Demo shop'})]:
            status, raw, _ = self.req('POST', '/api/generated-templates/' + action, payload, writer)
            self.assertEqual(status, 200, raw)
            self.assertEqual(self.server.generated_templates.calls[-1], (action, 'writer', payload))
        self.assertEqual(self.req('POST', '/api/generated-templates/no-such-action', {}, writer)[0], 404)

    def test_artifact_download_uses_protected_post_and_security_headers(self):
        writer = self.login('writer')
        status, content, headers = self.req('POST', '/api/generated-templates/artifact', {'token': 'test-token', 'kind': 'template'}, writer)
        self.assertEqual(status, 200)
        self.assertEqual(content, b'workbook-fixture')
        self.assertIn('attachment;', headers['Content-Disposition'])
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn("default-src 'self'", headers['Content-Security-Policy'])
        self.assertEqual(self.req('GET', '/api/generated-templates/artifact?token=test-token', cookie=writer)[0], 404)

    def test_anonymous_viewer_and_cross_site_requests_are_rejected(self):
        reader, writer = self.login('reader'), self.login('writer')
        for action in ('search', 'generate', 'artifact', 'save'):
            path = '/api/generated-templates/' + action
            self.assertEqual(self.req('POST', path, {})[0], 401)
            self.assertEqual(self.req('POST', path, {}, reader)[0], 403)
            self.assertEqual(self.req('POST', path, {}, writer, {'Origin': 'https://evil.example'})[0], 403)
            self.assertEqual(self.req('POST', path, {}, writer, {'X-Template-Request': '0'})[0], 403)

    def test_generated_routes_enforce_small_json_requests(self):
        writer = self.login('writer')
        path = '/api/generated-templates/search'
        self.assertEqual(self.req('POST', path, {'query': 'x' * (64 * 1024)}, writer)[0], 413)
        self.assertEqual(self.req('POST', path, [], writer)[0], 400)
        self.assertEqual(self.req('POST', path, {}, writer, {'Content-Type': 'application/x-template-upload', 'X-Template-Meta-Length': '0'})[0], 415)

    def test_real_http_generate_save_fill_json_precheck_and_confirmed_submit(self):
        import base64
        from template_manager import generated_templates
        from test_generated_templates import FakeClient
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'library'
            server = create_server(root, 0)
            client = FakeClient()
            client.require = lambda: None
            server.spapi = client
            server.generated_templates = generated_templates.Service(client, server.spapi_definitions, root, server.library_lock)
            server.auth.create('writer', 'a-secure-test-password', 'editor')
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.server = server
            try:
                writer = self.login('writer')
                status, raw, _ = self.req('POST', '/api/generated-templates/generate', {'marketplace': 'AE', 'product_type': 'SUITCASE'}, writer)
                self.assertEqual(status, 200, raw)
                preview = json.loads(raw)
                status, raw, _ = self.req('POST', '/api/generated-templates/save', {'token': preview['token'], 'shop': 'Demo shop'}, writer)
                self.assertEqual(status, 200, raw)
                saved = json.loads(raw)
                keys = [field['key'] for field in preview['fields']]
                values = {'contribution_sku': 'HTTP-DEMO-01', 'compliance::json': '{"approved":true,"count":2}'}
                values.update({key: 'Demo case' if key.startswith('item_name[') else 'BrandA' if key.startswith('brand[') else '3' if key.startswith('number_of_items#') else 'false' if key.startswith('batteries_required#') else '' for key in keys if key not in values})
                source = generated_templates._xlsx([('Products', [keys, [values.get(key, '') for key in keys]])])
                body = {'template_id': saved['id'], 'marketplace': 'AE', 'shop': 'Demo shop', 'table_type': 'upload',
                        'filename': 'source.xlsx', 'data': base64.b64encode(source).decode(), 'header_row': 1}
                status, raw, _ = self.req('POST', '/api/fill/preview', body, writer)
                self.assertEqual(status, 200, raw)
                report = json.loads(raw)
                self.assertEqual(report['errors'], 0, report['issues'])
                self.assertEqual(report['template']['generated_format'], 'spapi-v1')
                body['precheck_consent'] = True
                status, raw, _ = self.req('POST', '/api/fill/precheck', body, writer)
                self.assertEqual(status, 200, raw)
                result = json.loads(raw)
                self.assertEqual(result['summary'], {'processed': 1, 'successful': 1}, result)
                held = result['submittable'][0]
                preview_call = next(call for call in reversed(client.calls) if call[0] == 'PUT')
                self.assertEqual(preview_call[3]['attributes']['compliance'], {'approved': True, 'count': 2})
                submit = {'token': held['token'], 'sku': held['sku'], 'confirmed': False}
                before = len(client.calls)
                self.assertEqual(self.req('POST', '/api/amazon/listing/submit', submit, writer)[0], 400)
                self.assertEqual(len(client.calls), before)
                submit['confirmed'] = True
                status, raw, _ = self.req('POST', '/api/amazon/listing/submit', submit, writer)
                self.assertEqual(status, 200, raw)
                self.assertTrue(json.loads(raw)['accepted'])
                self.assertTrue(client.calls[-1][4])
                self.assertEqual(client.calls[-1][3], preview_call[3])
                client.seller = 'AOTHER001'
                before = len(client.calls)
                status, raw, _ = self.req('POST', '/api/fill/precheck', body, writer)
                self.assertEqual(status, 400, raw)
                self.assertIn('卖家', json.loads(raw)['error'])
                self.assertEqual(len(client.calls), before)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(5)
                del self.server

    def test_frontend_asset_and_entry_are_served_without_inline_code(self):
        status, data, headers = self.req('GET', '/generated-templates.js?v=test')
        self.assertEqual(status, 200)
        self.assertIn(b'/api/generated-templates/search', data)
        self.assertIn('javascript', headers['Content-Type'])
        self.assertNotIn('unsafe-inline', headers['Content-Security-Policy'])
        status, html, _ = self.req('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(b'id="generated-panel"', html)
        self.assertIn(b'/generated-templates.js', html)


if __name__ == '__main__':
    unittest.main()
