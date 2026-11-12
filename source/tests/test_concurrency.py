"""Members must not block each other on reads, while changes stay one at a time."""
import base64
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import create_server
from template_manager import batch, library, roster
from template_manager.locks import LibraryLock
from test_metadata import account_book
from test_roster import TABLE

SLOW = 2.0      # seconds a patched operation holds its lock
FAST = 1.5      # an unblocked request must finish well before SLOW elapses
BASE = '2026-11-12-批量'


def held_inside(lock_side, seconds=0.3):
    """Start a thread that holds lock_side() for `seconds`; return (thread, entered_event)."""
    entered = threading.Event()
    def run():
        with lock_side():
            entered.set()
            time.sleep(seconds)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    entered.wait(5)
    return thread, entered


class LibraryLockTests(unittest.TestCase):
    def test_readers_share(self):
        lock = LibraryLock()
        thread, _ = held_inside(lock.shared, 1.0)
        start = time.monotonic()
        with lock.shared():
            self.assertLess(time.monotonic() - start, 0.5)
        thread.join()

    def test_writer_excludes_readers_and_other_writers(self):
        lock = LibraryLock()
        for side in (lock.shared, lock.exclusive):
            thread, _ = held_inside(lock.exclusive, 0.5)
            start = time.monotonic()
            with side():
                self.assertGreaterEqual(time.monotonic() - start, 0.4)
            thread.join()
        thread, _ = held_inside(lock.shared, 0.5)
        start = time.monotonic()
        with lock.exclusive():
            self.assertGreaterEqual(time.monotonic() - start, 0.4)
        thread.join()

    def test_waiting_writer_is_not_starved_by_new_readers(self):
        lock = LibraryLock()
        order = []
        reader, _ = held_inside(lock.shared, 0.5)
        def writer():
            with lock.exclusive():
                order.append('writer')
        w = threading.Thread(target=writer); w.start()
        time.sleep(0.1)  # writer is now waiting behind the first reader
        def late_reader():
            with lock.shared():
                order.append('late reader')
        r = threading.Thread(target=late_reader); r.start()
        for t in (reader, w, r):
            t.join(5)
        self.assertEqual(order, ['writer', 'late reader'])


class BatchSaveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name) / 'library'
        roster.import_text(self.root, TABLE)
        self.clock = patch.object(batch, 'version_base', return_value=BASE); self.clock.start()
        self.lock = LibraryLock()

    def tearDown(self):
        self.clock.stop(); self.temp.cleanup()

    def rows(self):
        return library.run(SimpleNamespace(command='list', library=str(self.root), marketplace=None, category=None))

    def generate(self, source, **kwargs):
        return batch.generate(self.root, source, 'source.xlsm', ['测试甲', '测试乙'], '行李箱', None, auto_version=True,
                              create_archive=False, read_lock=self.lock.shared, write_lock=self.lock.exclusive, **kwargs)

    def test_parallel_batches_get_distinct_versions(self):
        real = batch.replace_identity
        def slow(*args):
            time.sleep(0.3)
            return real(*args)
        sources = [account_book(account='ATEST001', timestamp=f'run-{i}') for i in range(2)]
        with patch.object(batch, 'replace_identity', side_effect=slow), ThreadPoolExecutor(2) as pool:
            results = [f.result()[1] for f in [pool.submit(self.generate, s) for s in sources]]
        self.assertEqual(sorted(r['version'] for r in results), [BASE, BASE + '-02'])
        self.assertEqual([r['saved'] for r in results], [2, 2])
        self.assertEqual(len(self.rows()), 4)

    def test_roster_change_during_generation_saves_nothing(self):
        real = batch.replace_identity
        def change_roster(*args):
            result = real(*args)
            # What library.move_shop does to the roster when an admin reassigns the account.
            with self.lock.exclusive():
                db = library.connect(self.root)
                try:
                    with db:
                        db.execute("UPDATE shop_registry SET shop='测试丙' WHERE shop='测试甲'")
                finally:
                    db.close()
            return result
        with patch.object(batch, 'replace_identity', side_effect=change_roster):
            with self.assertRaisesRegex(ValueError, '生成期间被修改'):
                self.generate(account_book(account='ATEST001'))
        self.assertEqual(self.rows(), [])


class ServerConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / 'library'
        cls.server = create_server(cls.root, 0)
        for name, role in [('owner', 'admin'), ('writer', 'editor')]:
            cls.server.auth.create(name, 'a-secure-test-password', role)
        roster.import_text(cls.root, TABLE)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.tmp.cleanup()

    def req(self, method, path, data=None, cookie=None):
        c = HTTPConnection('127.0.0.1', self.server.server_port, timeout=30)
        headers = {'Content-Type': 'application/json', 'X-Template-Request': '1'}
        if cookie: headers['Cookie'] = cookie
        c.request(method, path, json.dumps(data) if data is not None else None, headers)
        r = c.getresponse(); result = (r.status, r.read(), r.getheader('Set-Cookie')); c.close()
        return result

    def login(self, name='writer'):
        status, _, cookie = self.req('POST', '/api/login', {'username': name, 'password': 'a-secure-test-password'})
        self.assertEqual(status, 200)
        return cookie.split(';')[0]

    def timed(self, *args, **kwargs):
        start = time.monotonic()
        result = self.req(*args, **kwargs)
        return result, time.monotonic() - start

    def while_slow(self, target, name, slow_request, other_request):
        """Run slow_request with `target.name` delayed by SLOW, then time other_request meanwhile."""
        real = getattr(target, name)
        started = threading.Event()
        def slow(*args, **kwargs):
            started.set(); time.sleep(SLOW)
            return real(*args, **kwargs)
        with patch.object(target, name, side_effect=slow), ThreadPoolExecutor(1) as pool:
            background = pool.submit(slow_request)
            self.assertTrue(started.wait(10))
            (status, raw, _), elapsed = other_request()
            self.assertIn(background.result()[0], (200, 400))
        return status, raw, elapsed

    def test_listing_is_not_blocked_by_a_large_download(self):
        cookie = self.login()
        status, _, elapsed = self.while_slow(
            library, 'download_templates',
            lambda: self.req('POST', '/api/templates/download', {'ids': ['missing']}, cookie),
            lambda: self.timed('GET', '/api/templates', cookie=cookie))
        self.assertEqual(status, 200)
        self.assertLess(elapsed, FAST)

    def test_login_is_not_blocked_by_a_change_but_reads_wait_for_it(self):
        cookie = self.login('owner')
        slow_delete = lambda: self.req('POST', '/api/templates/delete', {'ids': ['missing']}, cookie)
        status, _, elapsed = self.while_slow(library, 'delete_templates', slow_delete,
            lambda: self.timed('POST', '/api/login', {'username': 'writer', 'password': 'a-secure-test-password'}))
        self.assertEqual(status, 200)
        self.assertLess(elapsed, FAST)
        # Reads still never see a half-finished change.
        status, _, elapsed = self.while_slow(library, 'delete_templates', slow_delete,
            lambda: self.timed('GET', '/api/templates', cookie=cookie))
        self.assertEqual(status, 200)
        self.assertGreater(elapsed, SLOW - 0.5)

    def test_listing_is_not_blocked_while_batch_copies_are_generated(self):
        cookie = self.login()
        payload = {'filename': 'source.xlsm', 'data': base64.b64encode(account_book(account='ATEST001')).decode(),
                   'shops': ['测试甲'], 'category': '并发测试', 'version': 'v-concurrency', 'table_type': 'upload',
                   'save_to_library': True, 'download_zip': False}
        status, _, elapsed = self.while_slow(
            batch, 'replace_identity',
            lambda: self.req('POST', '/api/template/batch', payload, cookie),
            lambda: self.timed('GET', '/api/templates', cookie=cookie))
        self.assertEqual(status, 200)
        self.assertLess(elapsed, FAST)
        listed = json.loads(self.req('GET', '/api/templates', cookie=cookie)[1])
        self.assertIn('v-concurrency', {row['version'] for row in listed})


if __name__ == '__main__':
    unittest.main()
