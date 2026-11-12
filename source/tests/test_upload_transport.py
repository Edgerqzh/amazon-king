import base64
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import zipfile

from app import create_server, UPLOAD_TYPE
from template_manager.auth import Auth


def workbook_bytes():
    fixture = io.BytesIO()
    with zipfile.ZipFile(fixture, 'w') as z:
        z.writestr('[Content_Types].xml', '<Types/>')
        z.writestr('xl/workbook.xml', '<workbook/>')
        z.writestr('xl/vbaProject.bin', b'macro-fixture')
    return fixture.getvalue()


class UploadTransportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = create_server(Path(cls.tmp.name) / 'library', 0)
        for name, role in [('owner', 'admin'), ('reader', 'viewer')]:
            cls.server.auth.create(name, 'a-secure-test-password', role)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.tmp.cleanup()

    def raw(self, path, body, headers):
        c = HTTPConnection('127.0.0.1', self.server.server_port, timeout=30)
        c.request('POST', path, body, headers)
        r = c.getresponse(); result = (r.status, r.read()); c.close()
        return result

    def upload(self, path, data, meta, cookie=None, extra=None):
        head = json.dumps(meta).encode()
        headers = {'Content-Type': UPLOAD_TYPE, 'X-Template-Request': '1', 'X-Template-Meta-Length': str(len(head))}
        if cookie: headers['Cookie'] = cookie
        headers.update(extra or {})
        return self.raw(path, head + data, headers)

    def login(self, name):
        c = HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        c.request('POST', '/api/login', json.dumps({'username': name, 'password': 'a-secure-test-password'}),
                  {'Content-Type': 'application/json', 'X-Template-Request': '1'})
        r = c.getresponse(); r.read(); cookie = r.getheader('Set-Cookie'); c.close()
        self.assertEqual(r.status, 200)
        return cookie.split(';')[0]

    def test_binary_upload_matches_base64_upload(self):
        owner = self.login('owner')
        data = workbook_bytes()
        meta = {'filename': 'binary.xlsm', 'shop': '二进制店', 'marketplace': 'AE', 'category': '家居', 'version': 'v1'}
        status, result = self.upload('/api/upload', data, meta, owner)
        self.assertEqual(status, 200, result)
        ident = json.loads(result)['id']
        c = HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        c.request('GET', '/api/download/' + ident, headers={'Cookie': owner})
        self.assertEqual(c.getresponse().read(), data); c.close()
        # The legacy base64 JSON form still stores the same bytes (reused, not duplicated).
        legacy = json.dumps({**meta, 'data': base64.b64encode(data).decode()})
        status, result = self.raw('/api/upload', legacy, {'Content-Type': 'application/json', 'X-Template-Request': '1', 'Cookie': owner})
        self.assertEqual(status, 200, result)
        self.assertTrue(json.loads(result)['reused'])

    def test_binary_inventory_inspect_for_reader(self):
        reader = self.login('reader')
        status, raw = self.upload('/api/inventory/inspect', 'SKU,品名,类目\n001,杯子,厨具\n'.encode(), {'filename': '库存.csv'}, reader)
        self.assertEqual(status, 200, raw)

    def test_rejected_requests_get_a_reply_instead_of_a_reset(self):
        big = b'x' * (4 * 1024 * 1024)
        # Not logged in: body is drained in chunks, client still receives the JSON 401.
        status, raw = self.upload('/api/upload', big, {'filename': 'a.xlsx'})
        self.assertEqual(status, 401)
        self.assertIn('请先登录', json.loads(raw)['error'])
        reader = self.login('reader')
        status, raw = self.upload('/api/upload', big, {'filename': 'a.xlsx'}, reader)
        self.assertEqual(status, 403)
        status, raw = self.upload('/api/upload', big, {'filename': 'a.xlsx'}, reader, {'X-Template-Request': '0'})
        self.assertEqual(status, 403)
        self.assertIn('校验标记', json.loads(raw)['error'])
        status, raw = self.upload('/api/upload', big, {'filename': 'a.xlsx'}, reader, {'Origin': 'https://evil.example'})
        self.assertEqual(status, 403)

    def test_invalid_upload_framing_is_rejected_before_reading(self):
        owner = self.login('owner')
        headers = {'Content-Type': UPLOAD_TYPE, 'X-Template-Request': '1', 'Cookie': owner}
        for meta_length in ('-1', '99', 'abc'):
            status, _ = self.raw('/api/upload', b'{}' + b'0' * 10, {**headers, 'X-Template-Meta-Length': meta_length})
            self.assertIn(status, (400, 413), meta_length)
        # Metadata only, no file part.
        status, _ = self.raw('/api/upload', b'{}', {**headers, 'X-Template-Meta-Length': '2'})
        self.assertEqual(status, 413)

    def test_login_body_is_size_limited(self):
        body = json.dumps({'username': 'owner', 'password': 'p' * (70 * 1024)})
        status, _ = self.raw('/api/login', body, {'Content-Type': 'application/json', 'X-Template-Request': '1'})
        self.assertEqual(status, 400)


class AuthHandleTest(unittest.TestCase):
    def test_account_database_is_released_after_each_call(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'accounts.sqlite3'
            auth = Auth(path)
            auth.create('owner', 'a-secure-test-password', 'admin')
            token = auth.login('owner', 'a-secure-test-password', '127.0.0.1')
            with self.assertRaises(ValueError):
                auth.login('owner', 'wrong-password', '127.0.0.1')
            self.assertEqual(auth.user(token)['username'], 'owner')
            auth.logout(token)
            # Windows refuses to rename a file that still has an open handle.
            moved = path.rename(Path(d) / 'moved.sqlite3')
            moved.rename(path)


if __name__ == '__main__':
    unittest.main()
