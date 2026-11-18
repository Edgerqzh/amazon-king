from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest

from app import create_server


class HistoryRoutesTests(unittest.TestCase):
    def test_history_routes_authentication_roles_export_and_owner_deletion(self):
        with tempfile.TemporaryDirectory() as temp:
            server = create_server(Path(temp)/'library', 0)
            for name, role in [('owner', 'editor'), ('other', 'editor'), ('reader', 'viewer')]:
                server.auth.create(name, 'a-secure-test-password', role)
            thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
            def request(method, path, body=None, cookie=None):
                connection = HTTPConnection('127.0.0.1', server.server_port, timeout=10)
                headers = {'Content-Type': 'application/json', 'X-Template-Request': '1'}
                if cookie:headers['Cookie'] = cookie
                connection.request(method, path, json.dumps(body) if body is not None else None, headers)
                response = connection.getresponse();result = response.status, response.read(), dict(response.getheaders());connection.close()
                return result
            try:
                self.assertEqual(request('GET', '/api/lookalike/history')[0], 401)
                cookies = {}
                for name in ('owner', 'other', 'reader'):
                    cookies[name] = request('POST', '/api/login', {'username': name, 'password': 'a-secure-test-password'})[2]['Set-Cookie'].split(';')[0]
                server.lookalike.jobs['test-job'] = {'owner': 'owner', 'marketplace': 'AE', 'description': '笔', 'brand': '',
                    'keywords': ['black pen'], 'created_at': '2026-11-17T10:00:00Z', 'store': 'default', 'candidates': {}, 'expires': float('inf')}
                status, raw, _ = request('POST', '/api/lookalike/history/save', {'entries': [{'token': 'test-job', 'name': 'pen.png'}]}, cookies['owner'])
                self.assertEqual(status, 200);ident = json.loads(raw)['id']
                self.assertEqual(request('GET', '/api/lookalike/history/open?id='+ident, cookie=cookies['reader'])[0], 200)
                for kind in ('csv', 'xlsx', 'json'):
                    status, data, headers = request('GET', '/api/lookalike/history/export?id='+ident+'&format='+kind, cookie=cookies['reader'])
                    self.assertEqual(status, 200);self.assertTrue(data);self.assertIn('attachment;', headers['Content-Disposition'])
                payload = {'id': ident, 'confirmed': True}
                self.assertEqual(request('POST', '/api/lookalike/history/delete', payload, cookies['reader'])[0], 403)
                self.assertNotEqual(request('POST', '/api/lookalike/history/delete', payload, cookies['other'])[0], 200)
                self.assertEqual(request('POST', '/api/lookalike/history/delete', payload, cookies['owner'])[0], 200)
                self.assertNotEqual(request('GET', '/api/lookalike/history/open?id='+ident, cookie=cookies['reader'])[0], 200)
            finally:
                server.shutdown();server.server_close();thread.join(5)
