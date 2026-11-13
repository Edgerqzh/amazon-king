import base64
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
import urllib.error
import urllib.parse

from app import create_server
from template_manager import library, listings, spapi
from test_filling import template_book, MARKET, PRICE

SECRETS = {'SPAPI_CLIENT_ID': 'amzn1.application-oa2-client.testclient', 'SPAPI_CLIENT_SECRET': 'secret-value-never-shown', 'SPAPI_REFRESH_TOKEN': 'Atzr|refresh-token-never-shown', 'SPAPI_SELLER_ID': 'ATEST001'}
SCHEMA_URL = 'https://selling-partner-definitions-prod.s3.amazonaws.com/schema/SUITCASE.json'
SCHEMA = {'properties': {
    'item_name': {'type': 'array', 'items': {'properties': {'value': {'type': 'string'}, 'marketplace_id': {}, 'language_tag': {}}}},
    'brand': {'type': 'array', 'items': {'properties': {'value': {'type': 'string'}, 'marketplace_id': {}}}},
    'color': {'type': 'array', 'items': {'properties': {'value': {'anyOf': [{'type': 'string'}, {'type': 'string', 'enum': ['black', 'navy_blue'], 'enumNames': ['Black', 'Navy Blue']}]}, 'marketplace_id': {}}}},
    'batteries_required': {'type': 'array', 'items': {'properties': {'value': {'type': 'boolean'}, 'marketplace_id': {}}}},
    'fulfillment_availability': {'type': 'array', 'items': {'properties': {'quantity': {'type': 'integer'}, 'fulfillment_channel_code': {'type': 'string'}}}},
    'purchasable_offer': {'type': 'array', 'items': {'properties': {'marketplace_id': {}, 'audience': {}, 'our_price': {'type': 'array', 'items': {'properties': {'schedule': {'type': 'array', 'items': {'properties': {'value_with_tax': {'type': 'number'}}}}}}}}}},
}}


class Reply:
    def __init__(self, status, body):
        self.status, self.headers, self.body = status, {}, body if isinstance(body, bytes) else json.dumps(body).encode()
    def __enter__(self):return self
    def __exit__(self, *a):return False
    def read(self, size=-1):return self.body


class FakeAmazon:
    """Stands in for urlopen: records every request, never touches the network."""
    def __init__(self):
        self.requests, self.replies, self.tokens = [], [], 0

    def __call__(self, request, timeout=None):
        url, method = request.full_url, request.get_method()
        body = request.data
        self.requests.append(SimpleNamespace(url=url, method=method, headers={k.lower(): v for k, v in request.header_items()}, body=json.loads(body) if body and method != 'POST' else body))
        if url == spapi.TOKEN_URL:
            self.tokens += 1
            return Reply(200, {'access_token': 'Atza|access-%d' % self.tokens, 'expires_in': 3600})
        if url == SCHEMA_URL:
            return Reply(200, SCHEMA)
        status, payload = self.replies.pop(0) if self.replies else (200, {})
        if status >= 400:
            raise urllib.error.HTTPError(url, status, 'error', {}, io.BytesIO(json.dumps(payload).encode()))
        return Reply(status, payload)

    def api(self):return [r for r in self.requests if r.url.startswith(spapi.ENDPOINT)]


class MemoryVault:
    name = '测试凭据库'
    def __init__(self):self.values = {}
    def read(self):return dict(self.values)
    def write(self, values):self.values = dict(values)
    def delete(self):self.values = {}


def client(fake, environ=SECRETS, path=None, vault=None):
    return spapi.Client(config_path=path or Path(tempfile.gettempdir()) / 'no-such-spapi-config.json', environ=dict(environ), opener=fake, sleep=lambda s: None, vault=vault)


class ClientTests(unittest.TestCase):
    def test_public_status_never_contains_credentials(self):
        text = json.dumps(client(FakeAmazon()).public(), ensure_ascii=False)
        for secret in list(SECRETS.values())[:3]:self.assertNotIn(secret, text)
        self.assertTrue(client(FakeAmazon()).public()['configured'])
        partial = client(FakeAmazon(), {'SPAPI_CLIENT_ID': 'x'}).public()
        self.assertFalse(partial['configured']);self.assertEqual(len(partial['missing']), 3)

    def test_credentials_file_and_environment_override(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'spapi.json'
            path.write_text(json.dumps({'client_id': 'file-id', 'client_secret': 'file-secret', 'refresh_token': 'file-refresh', 'seller_id': 'AFILE001'}), encoding='utf-8')
            c = client(FakeAmazon(), {'SPAPI_SELLER_ID': 'AENV0001'}, path)
            values = c.require()
            self.assertEqual(values['client_id'], 'file-id');self.assertEqual(values['seller_id'], 'AENV0001')
            path.write_text('{broken', encoding='utf-8')
            self.assertFalse(c.public()['configured']);self.assertTrue(c.public()['problem'])

    def test_vault_keeps_credentials_validates_them_and_never_reveals_them(self):
        good = {'client_id': 'amzn1.application-oa2-client.abc', 'client_secret': 'amzn1.oa2-cs.v1.' + 'k' * 40, 'refresh_token': 'Atzr|' + 'r' * 60, 'seller_id': 'AVAULT001'}
        store = MemoryVault();c = client(FakeAmazon(), {}, vault=store)
        self.assertFalse(c.public()['configured']);self.assertTrue(c.public()['vault'])
        for bad in ({**good, 'client_id': good['refresh_token'], 'refresh_token': good['client_id']}, {**good, 'seller_id': 'not-a-seller'}, {**good, 'client_secret': 'short'}, {**good, 'client_secret': ''}, {**good, 'refresh_token': 'Atzr|a\nb'}, 'text'):
            with self.assertRaises(spapi.SpApiError):c.save(bad)
        self.assertEqual(store.values, {})
        c.save({key: '  ' + value + ' ' for key, value in good.items()});self.assertEqual(store.values, good)
        shown = c.public();text = json.dumps(shown, ensure_ascii=False)
        self.assertTrue(shown['configured']);self.assertTrue(shown['in_vault']);self.assertEqual(shown['source'], '测试凭据库');self.assertEqual(shown['seller_hint'], '…T001')
        for secret in list(good.values())[:3]:self.assertNotIn(secret, text)
        c.save({'client_secret': 'amzn1.oa2-cs.v1.' + 'n' * 40});self.assertEqual(store.values['refresh_token'], good['refresh_token']);self.assertTrue(store.values['client_secret'].endswith('n'))
        self.assertEqual(client(FakeAmazon(), {'SPAPI_SELLER_ID': 'AENV0001'}, vault=store).require()['seller_id'], 'AENV0001')
        c.forget();self.assertEqual(store.values, {});self.assertFalse(c.public()['configured'])
        with self.assertRaises(spapi.SpApiError):client(FakeAmazon(), {}).save(good)

    def test_write_requests_are_refused_before_any_network_call(self):
        fake = FakeAmazon();c = client(fake)
        item = spapi.LISTING_PATH + 'ATEST001/SKU1'
        for method, path, query in [('PUT', item, {}), ('PATCH', item, {'mode': 'LIVE'}), ('DELETE', item, {'mode': spapi.PREVIEW}), ('POST', '/feeds/2021-06-30/feeds', {'mode': spapi.PREVIEW}), ('PUT', '/orders/v0/orders/1', {'mode': spapi.PREVIEW})]:
            with self.assertRaises(spapi.SpApiError):c.call(method, path, query, {})
        self.assertEqual(fake.requests, [])
        c.call('PUT', item, {'mode': spapi.PREVIEW, 'marketplaceIds': MARKET}, {'productType': 'SUITCASE'})
        self.assertIn('mode=VALIDATION_PREVIEW', fake.api()[0].url)

    def test_token_is_cached_sent_in_header_and_refreshed_after_401(self):
        fake = FakeAmazon();c = client(fake)
        c.call('GET', '/sellers/v1/marketplaceParticipations');c.call('GET', '/sellers/v1/marketplaceParticipations')
        self.assertEqual(fake.tokens, 1);self.assertEqual(fake.api()[0].headers['x-amz-access-token'], 'Atza|access-1')
        form = urllib.parse.parse_qs(fake.requests[0].body.decode());self.assertEqual(form['grant_type'], ['refresh_token'])
        fake.replies = [(401, {'errors': [{'code': 'Unauthorized', 'message': 'expired'}]}), (200, {'payload': []})]
        self.assertEqual(c.call('GET', '/sellers/v1/marketplaceParticipations'), {'payload': []});self.assertEqual(fake.tokens, 2)

    def test_throttling_is_retried_and_errors_hide_secrets(self):
        fake = FakeAmazon();c = client(fake)
        fake.replies = [(429, {}), (200, {'payload': [{'marketplace': {'id': MARKET, 'name': 'Amazon.ae', 'countryCode': 'AE'}, 'participation': {'isParticipating': True}}]})]
        self.assertEqual(spapi.participations(c)[0]['name'], 'Amazon.ae')
        fake.replies = [(400, {'errors': [{'code': 'InvalidInput', 'message': 'bad token Atza|access-1 and secret-value-never-shown'}]})]
        with self.assertRaises(spapi.SpApiError) as caught:c.call('GET', '/sellers/v1/marketplaceParticipations')
        self.assertNotIn('Atza|access-1', str(caught.exception));self.assertNotIn('secret-value-never-shown', str(caught.exception));self.assertIn('InvalidInput', str(caught.exception))

    def test_schema_documents_only_from_amazon_hosts(self):
        c = client(FakeAmazon())
        for url in ('http://selling-partner-definitions-prod.s3.amazonaws.com/x', 'https://example.com/x.json', 'https://amazonaws.com.evil.test/x', None):
            with self.assertRaises(spapi.SpApiError):c.fetch_document(url)


class ConversionTests(unittest.TestCase):
    FIELDS = [{'key': 'sku', 'canonical': 'sku'}, {'key': 'product_type#1.value', 'canonical': 'feed_product_type'}, {'key': '::record_action', 'canonical': 'update_delete'},
              {'key': 'amzn1.volt.ca.product_id_type', 'canonical': 'product_id_type'}, {'key': 'amzn1.volt.ca.product_id_value', 'canonical': 'product_id'}, {'key': 'Notes', 'canonical': '', 'label': '备注'}]

    def test_paths_selectors_lists_and_types(self):
        values = {'sku': '00007', 'product_type#1.value': 'SUITCASE', '::record_action': 'Create', 'item_name[language_tag=en_AE]#1.value': 'Travel case',
                  PRICE: '19.50', 'fulfillment_availability#1.quantity': '0', 'fulfillment_availability#1.fulfillment_channel_code': 'DEFAULT',
                  'bullet_point#1.value': 'First', 'bullet_point#3.value': 'Third', 'color#1.value': 'Navy Blue', 'batteries_required#1.value': 'No', 'brand#1.value': '',
                  'amzn1.volt.ca.product_id_type': 'EAN', 'amzn1.volt.ca.product_id_value': '0123456789012', 'Notes': 'internal'}
        product_type, attributes, skipped = listings.convert(values, self.FIELDS, MARKET, SCHEMA)
        self.assertEqual(product_type, 'SUITCASE');self.assertEqual(skipped, ['备注'])
        self.assertEqual(attributes['item_name'], [{'language_tag': 'en_AE', 'value': 'Travel case', 'marketplace_id': MARKET}])
        self.assertEqual(attributes['purchasable_offer'], [{'marketplace_id': MARKET, 'audience': 'ALL', 'our_price': [{'schedule': [{'value_with_tax': 19.5}]}]}])
        self.assertEqual(attributes['fulfillment_availability'], [{'quantity': 0, 'fulfillment_channel_code': 'DEFAULT'}])
        self.assertEqual([b['value'] for b in attributes['bullet_point']], ['First', 'Third'])
        self.assertEqual(attributes['color'][0]['value'], 'navy_blue');self.assertIs(attributes['batteries_required'][0]['value'], False)
        self.assertEqual(attributes['externally_assigned_product_identifier'], [{'type': 'ean', 'value': '0123456789012', 'marketplace_id': MARKET}])
        for absent in ('brand', 'sku', 'product_type', 'Notes'):self.assertNotIn(absent, attributes)

    def test_without_schema_values_stay_text_and_identifier_needs_a_type(self):
        _, attributes, skipped = listings.convert({PRICE: '19.50', 'amzn1.volt.ca.product_id_value': '123'}, self.FIELDS, MARKET, None)
        self.assertEqual(attributes['purchasable_offer'][0]['our_price'][0]['schedule'][0]['value_with_tax'], '19.50')
        self.assertNotIn('externally_assigned_product_identifier', attributes);self.assertEqual(len(skipped), 1)

    def test_sku_list_limits(self):
        self.assertEqual(listings.clean_skus('A1\n a2 ,A1;B3', 50), ['A1', 'a2', 'B3'])
        for bad in ('', [1], ['x' * 41], [str(i) for i in range(51)]):
            with self.assertRaises(ValueError):listings.clean_skus(bad, 50)


class ListingTests(unittest.TestCase):
    def test_status_reads_summary_offers_stock_and_issues(self):
        fake = FakeAmazon();c = client(fake)
        fake.replies = [(200, {'sku': 'A 1/x', 'summaries': [{'marketplaceId': MARKET, 'asin': 'B000TEST01', 'productType': 'SUITCASE', 'status': ['BUYABLE', 'DISCOVERABLE'], 'itemName': 'Case'}],
                               'offers': [{'marketplaceId': MARKET, 'offerType': 'B2C', 'price': {'currencyCode': 'AED', 'amount': '19.50'}}], 'fulfillmentAvailability': [{'fulfillmentChannelCode': 'DEFAULT', 'quantity': 4}],
                               'issues': [{'code': '90220', 'message': "'brand' is required but not supplied.", 'severity': 'ERROR', 'attributeNames': ['brand']}]}),
                        (404, {'errors': [{'code': 'NOT_FOUND', 'message': 'not found'}]})]
        result = listings.status(c, ['A 1/x', 'MISSING'], 'AE')
        first, second = result['items']
        self.assertEqual((first['asin'], first['status'], first['offers'][0]['amount'], first['stock'][0]['quantity']), ('B000TEST01', ['BUYABLE', 'DISCOVERABLE'], '19.50', 4))
        self.assertEqual(first['issues'][0]['severity'], 'error');self.assertEqual(first['issues'][0]['attributes'], ['brand'])
        self.assertFalse(second['found'])
        self.assertTrue(all(r.method == 'GET' for r in fake.api()));self.assertIn('/ATEST001/A%201%2Fx?', fake.api()[0].url)
        with self.assertRaises(spapi.SpApiError):listings.status(c, ['A'], 'US')

    def rows(self):
        fields = [{'key': 'sku', 'label': 'SKU', 'column': 'A', 'canonical': 'sku'}, {'key': 'product_type#1.value', 'label': 'Product Type', 'column': 'B', 'canonical': 'feed_product_type'},
                  {'key': 'item_name#1.value', 'label': 'Item Name', 'column': 'D', 'canonical': 'item_name'}, {'key': 'brand#1.value', 'label': 'Brand Name', 'column': 'E', 'canonical': 'brand_name'},
                  {'key': PRICE, 'label': 'Selling Price', 'column': 'F', 'canonical': 'standard_price'}]
        rows = [{'sku': 'S1', 'source_rows': [2], 'values': {'sku': 'S1', 'product_type#1.value': 'SUITCASE', 'item_name#1.value': 'Case', 'brand#1.value': '', PRICE: '20'}},
                {'sku': 'S2', 'source_rows': [3], 'values': {'sku': 'S2', 'product_type#1.value': 'SUITCASE', 'item_name#1.value': 'Bag', 'brand#1.value': 'BrandA', PRICE: '30'}},
                {'sku': '', 'source_rows': [4], 'values': {'product_type#1.value': 'SUITCASE'}}]
        return fields, rows

    def test_precheck_only_validates_and_maps_issues_to_template_columns(self):
        fake = FakeAmazon();c = client(fake);fields, rows = self.rows()
        fake.replies = [(200, {'schema': {'link': {'resource': SCHEMA_URL}}}),
                        (200, {'sku': 'S1', 'status': 'INVALID', 'issues': [{'code': '90220', 'message': "'brand' is required but not supplied.", 'severity': 'ERROR', 'attributeNames': ['brand']}, {'code': '99300', 'message': 'Price looks low', 'severity': 'WARNING', 'attributeNames': ['purchasable_offer']}]}),
                        (200, {'sku': 'S2', 'status': 'VALID', 'issues': []})]
        seen = []
        result = listings.precheck(c, listings.Definitions(), rows, fields, 'AE', 'upload', 1, 20, seen.append)
        writes = [r for r in fake.api() if r.method != 'GET']
        self.assertEqual([r.method for r in writes], ['PUT', 'PUT'])
        for r in writes:self.assertIn('mode=VALIDATION_PREVIEW', r.url);self.assertEqual(r.body['requirements'], 'LISTING');self.assertEqual(r.body['productType'], 'SUITCASE')
        self.assertEqual(writes[0].body['attributes']['purchasable_offer'][0]['our_price'][0]['schedule'][0]['value_with_tax'], 20)
        self.assertNotIn('brand', writes[0].body['attributes'])
        self.assertEqual(result['format'], 'precheck');self.assertEqual(result['summary'], {'processed': 2, 'successful': 1})
        by = {(i['sku'], i['field_key']): i for i in result['issues']}
        self.assertEqual(by[('S1', 'brand#1.value')]['column'], 'E');self.assertEqual(by[('S1', 'brand#1.value')]['severity'], 'error')
        self.assertEqual(by[('S1', PRICE)]['severity'], 'warning')
        self.assertTrue(any(i['sku'] == '' and i['severity'] == 'error' for i in result['issues']))
        self.assertEqual(seen[-1]['stage'], 'done')
        self.assertEqual(sum('/definitions/' in r.url for r in fake.api()), 1)

    def test_price_precheck_uses_partial_update_preview_and_keeps_going_after_a_bad_sku(self):
        fake = FakeAmazon();c = client(fake);fields, rows = self.rows()
        rows = [{'sku': r['sku'], 'source_rows': r['source_rows'], 'values': {'sku': r['sku'], PRICE: r['values'].get(PRICE, '')}} for r in rows[:2]]
        fake.replies = [(400, {'errors': [{'code': 'InvalidInput', 'message': 'SKU not found'}]}), (200, {'status': 'VALID', 'issues': []})]
        result = listings.precheck(c, listings.Definitions(), rows, fields, 'AE', 'price_update', 1, 20)
        writes = fake.api();self.assertEqual([r.method for r in writes], ['PATCH', 'PATCH'])
        self.assertEqual(writes[1].body['productType'], 'PRODUCT');self.assertEqual(writes[1].body['patches'][0]['path'], '/attributes/purchasable_offer')
        self.assertEqual(result['summary'], {'processed': 2, 'successful': 1});self.assertEqual(result['issues'][0]['severity'], 'unknown')
        for bad in ((0, 20), (1, 0), (1, 101), (9, 5)):
            with self.assertRaises(ValueError):listings.precheck(c, listings.Definitions(), rows, fields, 'AE', 'price_update', *bad)

    def test_authorisation_failure_stops_the_batch(self):
        fake = FakeAmazon();c = client(fake);fields, rows = self.rows()
        fake.replies = [(403, {'errors': [{'code': 'Unauthorized', 'message': 'Access to requested resource is denied.'}]})]
        with self.assertRaises(spapi.SpApiError):listings.precheck(c, listings.Definitions(), rows, fields, 'AE', 'upload', 1, 20)
        self.assertEqual(len(fake.api()), 1)


class AmazonAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory();cls.root = Path(cls.temp.name) / 'library';cls.server = create_server(cls.root, 0)
        cls.fake = FakeAmazon();cls.server.spapi = client(cls.fake)
        for user, role in [('owner', 'admin'), ('writer', 'editor'), ('reader', 'viewer')]:cls.server.auth.create(user, 'amazon-test-password', role)
        source = Path(cls.temp.name) / 'original.xlsm';source.write_bytes(template_book())
        cls.template = library.run(SimpleNamespace(command='add', library=str(cls.root), source=str(source), shop='测试甲', marketplace='AE', category='行李箱', version='v1', table_type='upload'))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.temp.cleanup()

    def request(self, path, body=None, cookie=None):
        c = HTTPConnection('127.0.0.1', self.server.server_port, timeout=20);headers = {'Content-Type': 'application/json', 'X-Template-Request': '1'}
        if cookie:headers['Cookie'] = cookie
        c.request('GET' if body is None else 'POST', path, None if body is None else json.dumps(body), headers);r = c.getresponse();result = r.status, r.read(), dict(r.getheaders());c.close();return result

    def login(self, user):return self.request('/api/login', {'username': user, 'password': 'amazon-test-password'})[2]['Set-Cookie'].split(';')[0]

    def test_status_test_and_lookup_permissions(self):
        owner, writer, reader = self.login('owner'), self.login('writer'), self.login('reader')
        self.assertEqual(self.request('/api/amazon/status')[0], 401)
        status, raw, _ = self.request('/api/amazon/status', None, reader);self.assertEqual(status, 200)
        for secret in list(SECRETS.values())[:3]:self.assertNotIn(secret.encode(), raw)
        self.assertTrue(json.loads(raw)['configured'])
        self.assertEqual(self.request('/api/amazon/test', {}, writer)[0], 403)
        self.fake.replies = [(200, {'payload': [{'marketplace': {'id': MARKET, 'name': 'Amazon.ae', 'countryCode': 'AE'}, 'participation': {'isParticipating': True}}]})]
        status, raw, _ = self.request('/api/amazon/test', {}, owner);self.assertEqual(status, 200, raw);self.assertEqual(json.loads(raw)['marketplaces'][0]['country'], 'AE')
        self.fake.replies = [(404, {'errors': [{'code': 'NOT_FOUND', 'message': 'x'}]})]
        status, raw, _ = self.request('/api/amazon/listings', {'marketplace': 'AE', 'skus': ['NOPE']}, reader);self.assertEqual(status, 200, raw);self.assertFalse(json.loads(raw)['items'][0]['found'])
        self.assertEqual(self.request('/api/amazon/listings', {'marketplace': 'AE', 'skus': []}, reader)[0], 400)

    def test_credentials_are_saved_only_by_an_admin_and_never_echoed(self):
        owner, writer = self.login('owner'), self.login('writer')
        good = {'client_id': 'amzn1.application-oa2-client.abc', 'client_secret': 'amzn1.oa2-cs.v1.' + 'k' * 40, 'refresh_token': 'Atzr|' + 'r' * 60, 'seller_id': 'AVAULT001'}
        original = self.server.spapi;store = MemoryVault();self.server.spapi = client(self.fake, {}, vault=store)
        try:
            for path in ('/api/amazon/credentials', '/api/amazon/credentials/delete'):self.assertEqual(self.request(path, good, writer)[0], 403)
            self.assertEqual(self.request('/api/amazon/credentials', {**good, 'seller_id': 'bad'}, owner)[0], 400);self.assertEqual(store.values, {})
            status, raw, _ = self.request('/api/amazon/credentials', good, owner);self.assertEqual(status, 200, raw);self.assertEqual(store.values, good)
            for secret in list(good.values())[:3]:self.assertNotIn(secret.encode(), raw)
            self.assertTrue(json.loads(raw)['in_vault'])
            status, raw, _ = self.request('/api/amazon/credentials/delete', {}, owner);self.assertEqual(status, 200);self.assertFalse(json.loads(raw)['configured']);self.assertEqual(store.values, {})
        finally:self.server.spapi = original

    def test_fill_precheck_needs_editor_consent_and_sends_only_previews(self):
        owner, reader = self.login('owner'), self.login('reader')
        body = {'filename': 'products.csv', 'data': base64.b64encode(b'SKU,title,brand,price,quantity\n00001,Case,BrandA,20,1').decode(), 'template_id': self.template['id'], 'shop': '测试甲', 'marketplace': 'AE', 'table_type': 'upload'}
        self.assertEqual(self.request('/api/fill/precheck', {**body, 'precheck_consent': True}, reader)[0], 403)
        before = len(self.fake.api())
        status, raw, _ = self.request('/api/fill/precheck', body, owner);self.assertEqual(status, 400);self.assertEqual(len(self.fake.api()), before)
        self.fake.replies = [(200, {'schema': {'link': {'resource': SCHEMA_URL}}}), (200, {'status': 'INVALID', 'issues': [{'code': '90220', 'message': "'brand' is required but not supplied.", 'severity': 'ERROR', 'attributeNames': ['brand']}]})]
        status, raw, _ = self.request('/api/fill/precheck', {**body, 'precheck_consent': True, 'precheck_start': 1, 'precheck_limit': 20, 'job_id': 'precheck-1'}, owner)
        self.assertEqual(status, 200, raw);result = json.loads(raw)
        self.assertEqual(result['template']['id'], self.template['id']);self.assertEqual(result['issues'][0]['field_key'], 'brand#1.value');self.assertEqual(result['range'], {'start': 1, 'count': 1, 'total': 1})
        self.assertFalse(any('卖家记号' in note for note in result['notes']))
        sent = [r for r in self.fake.api()[before:] if r.method != 'GET']
        self.assertEqual(len(sent), 1);self.assertEqual(sent[0].method, 'PUT');self.assertIn('mode=VALIDATION_PREVIEW', sent[0].url);self.assertIn('/ATEST001/00001?', sent[0].url)
        self.assertEqual(sent[0].body['attributes']['item_name'][0]['value'], 'Case')
        self.assertEqual(self.request('/api/inventory/progress?job_id=precheck-1', None, owner)[0], 200)


if __name__ == '__main__':
    unittest.main()
