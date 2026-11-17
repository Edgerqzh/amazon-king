"""Stores of other legal entities join by authorizing the public application through a link."""
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.parse

from template_manager import spapi
from test_spapi import FakeAmazon, Reply
from test_stores import make

PUBLIC = {'application_id': 'amzn1.sp.solution.' + '1234abcd-' * 3 + 'ffff', 'client_id': 'amzn1.application-oa2-client.public',
          'client_secret': 'amzn1.oa2-cs.v1.' + 'p' * 40, 'redirect_uri': 'https://example.github.io/king/authorize.html'}
CODE = 'ANcodeXYZ' + 'q' * 20
REFRESH = 'Atzr|public-' + 'r' * 50


class Exchange(FakeAmazon):
    """Answers the authorization-code grant like Login with Amazon."""
    def __init__(self):
        super().__init__();self.grants = [];self.status = 200;self.reply = None

    def __call__(self, request, timeout=None):
        if request.full_url == spapi.TOKEN_URL:
            form = dict(urllib.parse.parse_qsl(request.data.decode()))
            if form['grant_type'] == 'authorization_code':
                self.grants.append(form)
                payload = self.reply if self.reply is not None else {'refresh_token': REFRESH, 'access_token': 'Atza|x', 'expires_in': 3600}
                if self.status != 200:
                    raise urllib.error.HTTPError(request.full_url, self.status, 'bad', {}, io.BytesIO(json.dumps(payload).encode()))
                return Reply(200, payload)
        return super().__call__(request, timeout)


class Clock:
    def __init__(self):self.now = 1000.0
    def __call__(self):return self.now


def state_of(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.fake = Exchange()
        self.stores, self.vaults, self.main_vault, _ = make(self.tmp.name, self.fake)
        self.clock = self.stores.oauth_clock = Clock()

    def link(self, name='别家主体店', **kw):
        self.stores.save_public_app(PUBLIC)
        started = self.stores.oauth_start(name, 'AE', **kw)
        return started, state_of(started['url'])

    def test_public_app_is_checked_and_never_shown(self):
        with self.assertRaises(spapi.SpApiError):self.stores.save_public_app({**PUBLIC, 'application_id': PUBLIC['client_id']})
        with self.assertRaises(spapi.SpApiError):self.stores.save_public_app({**PUBLIC, 'redirect_uri': 'http://x.example/a'})
        with self.assertRaises(spapi.SpApiError):self.stores.save_public_app({**PUBLIC, 'client_id': PUBLIC['client_secret'], 'client_secret': PUBLIC['client_id']})
        self.assertFalse(self.stores.public()['public_app']['configured'])
        self.stores.save_public_app(PUBLIC)
        self.stores.save_public_app({**PUBLIC, 'client_secret': ''})  # blank keeps the saved secret
        shown = json.dumps(self.stores.public(), ensure_ascii=False)
        self.assertTrue(self.stores.public()['public_app']['configured'])
        self.assertNotIn(PUBLIC['client_secret'], shown);self.assertNotIn(PUBLIC['client_id'], shown)

    def test_link_needs_the_public_app(self):
        with self.assertRaises(spapi.SpApiError):self.stores.oauth_start('店', 'AE')

    def test_link_and_finish_add_a_store_on_the_public_app(self):
        started, query = self.link()
        self.assertTrue(started['url'].startswith('https://sellercentral.amazon.ae/apps/authorize/consent?'))
        self.assertEqual(query['application_id'], PUBLIC['application_id']);self.assertEqual(query['redirect_uri'], PUBLIC['redirect_uri'])
        self.assertEqual(query['version'], 'beta')
        shown = self.stores.oauth_finish('KING1:%s:%s:AOTHER0001\n' % (query['state'], CODE))
        grant = self.fake.grants[0]
        self.assertEqual((grant['code'], grant['redirect_uri'], grant['client_id']), (CODE, PUBLIC['redirect_uri'], PUBLIC['client_id']))
        self.assertEqual(shown['name'], '别家主体店');self.assertTrue(shown['configured'])
        row = next(s for s in self.stores.public()['stores'] if s['name'] == '别家主体店')
        self.assertEqual(row['app'], 'public');self.assertIn('公共应用', row['source'])
        self.assertEqual(self.vaults.values(row['store']), {'refresh_token': REFRESH, 'seller_id': 'AOTHER0001'})
        self.assertEqual(self.stores.client(row['store']).credentials()[0]['client_id'], PUBLIC['client_id'])
        with self.assertRaises(spapi.SpApiError):  # the state is used up
            self.stores.oauth_finish('KING1:%s:%s:AOTHER0001' % (query['state'], CODE))

    def test_unknown_or_old_state_is_refused_without_calling_amazon(self):
        _, query = self.link()
        with self.assertRaises(spapi.SpApiError):self.stores.oauth_finish('KING1:%s:%s:AOTHER0001' % ('x' * 24, CODE))
        self.clock.now += spapi.OAUTH_LIFE + 1
        with self.assertRaises(spapi.SpApiError):self.stores.oauth_finish('KING1:%s:%s:AOTHER0001' % (query['state'], CODE))
        for bad in ('', 'hello', 'KING1:a:b:c', 'KING1:%s:%s:lower' % (query['state'], CODE)):
            with self.assertRaises(spapi.SpApiError):self.stores.oauth_finish(bad)
        self.assertEqual(self.fake.grants, [])

    def test_failed_exchange_hides_secrets_and_adds_nothing(self):
        self.fake.status = 400;self.fake.reply = {'error': 'invalid_grant', 'error_description': 'bad code ' + CODE}
        _, query = self.link()
        with self.assertRaises(spapi.SpApiError) as caught:self.stores.oauth_finish('KING1:%s:%s:AOTHER0001' % (query['state'], CODE))
        self.assertNotIn(CODE, str(caught.exception));self.assertNotIn(PUBLIC['client_secret'], str(caught.exception))
        self.assertEqual(len(self.stores.public()['stores']), 1)

    def test_same_seller_is_not_added_twice(self):
        _, query = self.link('甲')
        self.stores.oauth_finish('KING1:%s:%s:AOTHER0001' % (query['state'], CODE))
        query = state_of(self.stores.oauth_start('乙', 'SA')['url'])
        with self.assertRaises(spapi.SpApiError):self.stores.oauth_finish('KING1:%s:%s:AOTHER0001' % (query['state'], CODE))

    def test_reauthorize_an_existing_store(self):
        store = self.stores.save('new', '老店', {'refresh_token': 'Atzr|old-' + 'o' * 40, 'seller_id': 'AOLD00001'})['store']
        _, query = self.link(store=store)
        self.stores.oauth_finish('KING1:%s:%s:AOLD00001' % (query['state'], CODE))
        self.assertEqual(self.vaults.values(store)['refresh_token'], REFRESH)
        self.assertEqual(next(s for s in self.stores.public()['stores'] if s['store'] == store)['app'], 'public')
        with self.assertRaises(spapi.SpApiError):self.stores.oauth_start('', 'AE', store=spapi.DEFAULT_STORE)


if __name__ == '__main__':
    unittest.main()
