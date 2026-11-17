"""Several of the seller's own stores, each with its own authorization."""
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.parse

from app import create_server
from template_manager import operations, spapi
from test_spapi import FakeAmazon, MemoryVault, MARKET

APP = {'client_id': 'amzn1.application-oa2-client.main', 'client_secret': 'amzn1.oa2-cs.v1.' + 'm' * 40}
MAIN = {**APP, 'refresh_token': 'Atzr|main-' + 'a' * 50, 'seller_id': 'AMAIN0001'}
SECOND = {'refresh_token': 'Atzr|second-' + 'b' * 50, 'seller_id': 'ASECOND01'}


class Vaults:
    """One in-memory vault per target name, like separate Windows credential entries."""
    def __init__(self):self.items = {}
    def __call__(self, target):return self.items.setdefault(target, MemoryVault())
    def values(self, store):return self.items.get(spapi.TARGET_PREFIX + store, MemoryVault()).values


def make(tmp, fake=None):
    fake = fake or FakeAmazon()
    vaults, main_vault = Vaults(), MemoryVault()
    options = {'opener': fake, 'sleep': lambda s: None}
    main = spapi.Client(config_path=Path(tmp) / 'none.json', environ={}, vault=main_vault, **options)
    main.save(MAIN)
    stores = spapi.Stores(main, Path(tmp) / 'amazon-stores.json', vault_for=vaults, **options)
    return stores, vaults, main_vault, fake


class StoresTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.stores, self.vaults, self.main_vault, self.fake = make(self.tmp.name)

    def add(self, name='第二家店', values=SECOND):
        return self.stores.save('new', name, dict(values))

    def test_first_store_is_listed_and_a_new_store_reuses_its_application(self):
        listed = self.stores.public()
        self.assertEqual([s['name'] for s in listed['stores']], ['主店铺']);self.assertEqual(listed['limit'], 10)
        shown = self.add()
        self.assertTrue(shown['configured']);self.assertTrue(shown['shared_app']);self.assertEqual(shown['name'], '第二家店')
        # Only this store's own token and seller are kept for it; the application stays with the first store.
        self.assertEqual(self.vaults.values(shown['store']), SECOND)
        values = self.stores.client(shown['store']).require()
        self.assertEqual(values['client_id'], APP['client_id']);self.assertEqual(values['refresh_token'], SECOND['refresh_token'])
        text = json.dumps(self.stores.public(), ensure_ascii=False)
        for secret in (MAIN['client_secret'], MAIN['refresh_token'], SECOND['refresh_token']):self.assertNotIn(secret, text)

    def test_a_rotated_secret_on_the_first_store_reaches_every_store(self):
        store = self.add()['store']
        self.stores.client().save({'client_secret': 'amzn1.oa2-cs.v1.' + 'z' * 40})
        self.assertTrue(self.stores.client(store).require()['client_secret'].endswith('z'))

    def test_a_store_can_have_its_own_application(self):
        own = {'client_id': 'amzn1.application-oa2-client.other', 'client_secret': 'amzn1.oa2-cs.v1.' + 'o' * 40, **SECOND}
        shown = self.add('另一个主体', own)
        self.assertFalse(shown['shared_app']);self.assertEqual(self.stores.client(shown['store']).require()['client_id'], own['client_id'])

    def test_each_store_calls_amazon_with_its_own_token_and_seller(self):
        store = self.add()['store']
        for name in (None, store):
            self.fake.replies = [(200, {'payload': []})]
            self.stores.client(name).call('GET', '/sellers/v1/marketplaceParticipations')
        tokens = [urllib.parse.parse_qs(r.body.decode())['refresh_token'][0] for r in self.fake.requests if r.url == spapi.TOKEN_URL]
        self.assertEqual(tokens, [MAIN['refresh_token'], SECOND['refresh_token']])
        self.assertEqual(self.stores.client(store).seller_id(), 'ASECOND01')

    def test_bad_input_changes_nothing(self):
        for name, values, message in (('', SECOND, '名称'), ('主店铺', SECOND, '同名'), ('乙', {**SECOND, 'seller_id': MAIN['seller_id']}, '主店铺'),
                                      ('乙', {'seller_id': 'ASECOND01'}, 'Refresh Token'), ('乙', {**SECOND, 'refresh_token': 'not-a-token'}, 'Atzr')):
            with self.assertRaises(spapi.SpApiError) as caught:self.stores.save('new', name, dict(values))
            self.assertIn(message, str(caught.exception))
        self.assertEqual(len(self.stores.public()['stores']), 1)
        self.assertFalse(any(v.values for v in self.vaults.items.values()))
        with self.assertRaises(spapi.SpApiError):self.stores.client('s00000000')
        with self.assertRaises(spapi.SpApiError):self.stores.client('../x')

    def test_at_most_ten_stores(self):
        for number in range(9):
            self.add('店%d' % number, {'refresh_token': 'Atzr|' + str(number) * 40, 'seller_id': 'ASHOP%04d' % number})
        with self.assertRaises(spapi.SpApiError) as caught:self.add('店10', {'refresh_token': 'Atzr|' + 'x' * 40, 'seller_id': 'ASHOPXXXX'})
        self.assertIn('10', str(caught.exception))

    def test_rename_update_and_remove(self):
        store = self.add()['store']
        self.stores.save(store, '改名后', {})
        self.assertEqual(self.stores.client(store).label, '改名后');self.assertEqual(self.vaults.values(store), SECOND)
        self.stores.save(store, '', {'refresh_token': 'Atzr|' + 'n' * 50})
        self.assertEqual(self.vaults.values(store)['refresh_token'], 'Atzr|' + 'n' * 50);self.assertEqual(self.vaults.values(store)['seller_id'], 'ASECOND01')
        self.stores.save(None, '总店', {})
        self.assertEqual(self.stores.client().label, '总店');self.assertTrue(self.stores.client().public()['configured'])
        self.stores.remove(store)
        self.assertEqual(self.vaults.values(store), {});self.assertEqual([s['name'] for s in self.stores.public()['stores']], ['总店'])
        self.stores.remove(None)
        self.assertEqual(self.main_vault.values, {});self.assertEqual(len(self.stores.public()['stores']), 1)

    def test_find_store_by_seller_fingerprint(self):
        from template_manager import roster
        store = self.add()['store']
        self.assertEqual(self.stores.find(roster.fingerprint('ASECOND01'), roster.fingerprint)['id'], store)
        self.assertIsNone(self.stores.find(roster.fingerprint('ANOBODY01'), roster.fingerprint))

    def test_a_preview_or_report_stays_with_its_store(self):
        store = self.add()['store']
        first, second = self.stores.client(), self.stores.client(store)
        changes = operations.Changes()
        token = changes.hold('owner', 'SKU1', 'AE', 'PATCH', spapi.LISTING_PATH + 'AMAIN0001/SKU1', {'marketplaceIds': MARKET}, {'productType': 'X', 'patches': []}, [], first.store_id, first.label)
        with self.assertRaises(ValueError) as caught:changes.submit(second, 'owner', token, 'SKU1', True)
        self.assertIn('主店铺', str(caught.exception))
        self.assertEqual([r for r in self.fake.requests if r.method == 'PATCH'], [])
        reports = operations.Reports()
        self.fake.replies = [(202, {'reportId': 'R1'})]
        reports.create(first, 'listings', 'AE')
        with self.assertRaises(ValueError):reports.get(second, 'R1')
        with self.assertRaises(ValueError):reports.download('R1', store)


class StoreRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server = create_server(Path(cls.tmp.name) / 'library', 0)
        cls.fake = FakeAmazon()
        cls.vaults = Vaults()
        main = spapi.Client(config_path=Path(cls.tmp.name) / 'none.json', environ={}, vault=MemoryVault(), opener=cls.fake, sleep=lambda s: None)
        main.save(MAIN)
        cls.server.spapi = main
        cls.server.spapi_stores.vault_for = cls.vaults
        cls.server.spapi_stores.options = {'opener': cls.fake, 'sleep': lambda s: None}
        for name, role in (('owner', 'admin'), ('reader', 'viewer')):cls.server.auth.create(name, 'amazon-test-password', role)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.tmp.cleanup()

    def request(self, path, body=None, cookie=None):
        c = HTTPConnection('127.0.0.1', self.server.server_port, timeout=20);headers = {'Content-Type': 'application/json', 'X-Template-Request': '1'}
        if cookie:headers['Cookie'] = cookie
        c.request('GET' if body is None else 'POST', path, None if body is None else json.dumps(body), headers);r = c.getresponse();result = r.status, r.read();c.close();return result

    def login(self, user):
        c = HTTPConnection('127.0.0.1', self.server.server_port, timeout=20)
        c.request('POST', '/api/login', json.dumps({'username': user, 'password': 'amazon-test-password'}), {'Content-Type': 'application/json', 'X-Template-Request': '1'})
        r = c.getresponse();r.read();cookie = r.getheader('Set-Cookie').split(';')[0];c.close();return cookie

    def test_add_list_use_and_remove_a_store(self):
        owner, reader = self.login('owner'), self.login('reader')
        self.assertEqual(self.request('/api/amazon/credentials', {'store': 'new', 'name': '乙店', **SECOND}, reader)[0], 403)
        status, raw = self.request('/api/amazon/credentials', {'store': 'new', 'name': '乙店', **SECOND}, owner);self.assertEqual(status, 200, raw)
        for secret in (SECOND['refresh_token'], MAIN['client_secret']):self.assertNotIn(secret.encode(), raw)
        added = json.loads(raw);store = added['store']
        self.assertEqual([s['name'] for s in added['stores']], ['主店铺', '乙店'])
        status, raw = self.request('/api/amazon/status?store=' + store, None, reader);self.assertEqual(status, 200)
        shown = json.loads(raw);self.assertEqual(shown['name'], '乙店');self.assertEqual(shown['seller_hint'], '…ND01')
        self.fake.replies = [(404, {'errors': [{'code': 'NOT_FOUND', 'message': 'x'}]})]
        before = len(self.fake.requests)
        status, raw = self.request('/api/amazon/listings', {'store': store, 'marketplace': 'AE', 'skus': ['S1']}, reader);self.assertEqual(status, 200, raw)
        self.assertTrue(any('/ASECOND01/S1' in r.url for r in self.fake.requests[before:]))
        self.assertEqual(self.request('/api/amazon/listings', {'store': 'nope', 'marketplace': 'AE', 'skus': ['S1']}, reader)[0], 400)
        status, raw = self.request('/api/amazon/credentials/delete', {'store': store}, owner);self.assertEqual(status, 200, raw)
        self.assertEqual([s['name'] for s in json.loads(raw)['stores']], ['主店铺'])
        self.assertTrue(self.server.spapi.public()['configured'])


if __name__ == '__main__':
    unittest.main()
