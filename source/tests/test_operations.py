import datetime
import gzip
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

from app import create_server
from template_manager import library, listings, operations, spapi
from test_filling import template_book, MARKET
from test_spapi import FakeAmazon, Reply, client, SCHEMA, SCHEMA_URL

NOW = datetime.datetime(2026, 11, 13, 10, 0, tzinfo=datetime.timezone.utc)
DOC_URL = 'https://tortuga-prod-eu.s3-eu-west-1.amazonaws.com/report-doc'
ITEM = {'summaries': [{'marketplaceId': MARKET, 'asin': 'B000TEST01', 'productType': 'SUITCASE', 'itemName': 'Case'}],
        'attributes': {'purchasable_offer': [{'marketplace_id': MARKET, 'audience': 'ALL', 'currency': 'AED', 'our_price': [{'schedule': [{'value_with_tax': 20.0}]}], 'discounted_price': [{'schedule': [{'value_with_tax': 18.0}]}]},
                                              {'marketplace_id': MARKET, 'audience': 'B2B', 'currency': 'AED', 'our_price': [{'schedule': [{'value_with_tax': 19.0}]}]}],
                       'fulfillment_availability': [{'fulfillment_channel_code': 'DEFAULT', 'quantity': 4, 'lead_time_to_ship_max_days': 2}]}}


class Amazon(FakeAmazon):
    def __init__(self):
        super().__init__();self.documents = {}
    def __call__(self, request, timeout=None):
        if request.full_url in self.documents:
            self.requests.append(SimpleNamespace(url=request.full_url, method='GET', headers={k.lower(): v for k, v in request.header_items()}, body=None))
            return Reply(200, self.documents[request.full_url])
        return super().__call__(request, timeout)


class ReadOnlyTests(unittest.TestCase):
    def test_orders_paginate_summarise_and_never_carry_buyer_details(self):
        fake = Amazon();c = client(fake)
        order = lambda i, status, total: {'AmazonOrderId': '111-0000000-%07d' % i, 'PurchaseDate': '2026-11-1%dT08:00:00Z' % i, 'OrderStatus': status, 'FulfillmentChannel': 'MFN', 'OrderTotal': {'Amount': total, 'CurrencyCode': 'AED'}, 'NumberOfItemsUnshipped': 1, 'BuyerInfo': {'BuyerEmail': 'buyer@example.com'}, 'ShippingAddress': {'City': 'Dubai'}}
        fake.replies = [(200, {'payload': {'Orders': [order(1, 'Unshipped', '10.50')], 'NextToken': 'next-1'}}), (200, {'payload': {'Orders': [order(2, 'Shipped', '5.00'), order(3, 'Canceled', '99')]}})]
        result = operations.orders(c, 'AE', 7, 'open', NOW)
        self.assertEqual([o['id'][-1] for o in result['orders']], ['3', '2', '1']);self.assertEqual(result['counts'], {'Unshipped': 1, 'Shipped': 1, 'Canceled': 1});self.assertEqual(result['totals'], {'AED': '15.50'})
        text = json.dumps(result);self.assertNotIn('buyer@example.com', text);self.assertNotIn('Dubai', text)
        first, second = fake.api()
        self.assertIn('CreatedAfter=2026-11-06T10%3A00%3A00Z', first.url);self.assertIn('OrderStatuses=Unshipped%2CPartiallyShipped', first.url);self.assertIn('NextToken=next-1', second.url)
        for bad in ((0, ''), (31, ''), (7, 'everything')):
            with self.assertRaises(ValueError):operations.orders(c, 'AE', *bad)
        fake.replies = [(200, {'payload': {'OrderItems': [{'SellerSKU': 'S1', 'ASIN': 'B0', 'Title': 'Case', 'QuantityOrdered': 2, 'ItemPrice': {'Amount': '21', 'CurrencyCode': 'AED'}}]}})]
        self.assertEqual(operations.order_items(c, '111-0000000-0000001')['items'][0]['ordered'], 2)
        with self.assertRaises(ValueError):operations.order_items(c, '../feeds')

    def test_sales_by_local_day(self):
        fake = Amazon();c = client(fake)
        fake.replies = [(200, {'payload': [{'interval': '2026-11-12T00:00+04:00--2026-11-13T00:00+04:00', 'unitCount': 3, 'orderCount': 2, 'totalSales': {'amount': '60.5', 'currencyCode': 'AED'}, 'averageUnitPrice': {'amount': '20.17', 'currencyCode': 'AED'}},
                                             {'interval': '2026-11-13T00:00+04:00--2026-11-13T14:00+04:00', 'unitCount': 1, 'orderCount': 1, 'totalSales': {'amount': '20', 'currencyCode': 'AED'}}]})]
        result = operations.sales(c, 'AE', 2, NOW)
        self.assertEqual((result['units'], result['orders'], result['sales'], result['currency']), (4, 3, '80.5', 'AED'));self.assertEqual(result['rows'][0]['date'], '2026-11-12')
        url = fake.api()[0].url;self.assertIn('2026-11-12T00%3A00%3A00%2B04%3A00--2026-11-13T14%3A00%3A00%2B04%3A00', url);self.assertIn('granularityTimeZone=Asia%2FDubai', url)

    def test_overview_keeps_the_part_that_worked(self):
        fake = Amazon();c = client(fake)
        fake.replies = [(200, {'payload': [{'interval': '2026-11-13T00:00+04:00--x', 'unitCount': 2, 'orderCount': 1, 'totalSales': {'amount': '40', 'currencyCode': 'AED'}}]}), (403, {'errors': [{'code': 'Unauthorized', 'message': 'denied'}]})]
        result = operations.overview(c, 'AE', NOW)
        self.assertEqual(result['sales']['sales'], '40');self.assertIn('error', result['open_orders']);self.assertTrue(all(r.method == 'GET' for r in fake.api()))
        with self.assertRaises(spapi.SpApiError):operations.overview(client(Amazon(), {}), 'AE', NOW)
        with self.assertRaises(spapi.SpApiError):operations.overview(c, 'US', NOW)

    def test_fba_inventory_pages(self):
        fake = Amazon();c = client(fake)
        fake.replies = [(200, {'payload': {'inventorySummaries': [{'sellerSku': 'S1', 'totalQuantity': 9, 'inventoryDetails': {'fulfillableQuantity': 5, 'inboundShippedQuantity': 3, 'reservedQuantity': {'totalReservedQuantity': 1}}}]}, 'pagination': {'nextToken': 't2'}}),
                        (200, {'payload': {'inventorySummaries': [{'sellerSku': 'S2', 'totalQuantity': 0}]}})]
        result = operations.fba_inventory(c, 'SA', ['S1', 'S2'])
        self.assertEqual([(i['sku'], i['fulfillable'], i['inbound'], i['reserved']) for i in result['items']], [('S1', 5, 3, 1), ('S2', 0, 0, 0)])
        self.assertIn('sellerSkus=S1%2CS2', fake.api()[0].url);self.assertIn('nextToken=t2', fake.api()[1].url);self.assertTrue(all(r.method == 'GET' for r in fake.api()))

    def test_report_is_created_polled_downloaded_and_stripped_of_private_columns(self):
        fake = Amazon();c = client(fake);reports = operations.Reports()
        fake.replies = [(202, {'reportId': '50001'})]
        made = reports.create(c, 'orders', 'AE', 3, NOW)
        post = fake.api()[0];self.assertEqual(post.method, 'POST');self.assertTrue(post.url.endswith('/reports/2021-06-30/reports'))
        fake.replies = [(200, {'processingStatus': 'IN_PROGRESS'})]
        self.assertEqual(reports.get(c, made['id'])['status'], 'IN_PROGRESS')
        with self.assertRaises(ValueError):reports.download(made['id'])
        fake.documents[DOC_URL] = gzip.compress('amazon-order-id\tsku\tquantity\tbuyer-email\tship-city\trecipient-name\tnote\r\n111-1\tS1\t2\tb@example.com\tDubai\tSomeone\t=1+1\r\n'.encode())
        fake.replies = [(200, {'processingStatus': 'DONE', 'reportDocumentId': 'doc-1'}), (200, {'url': DOC_URL, 'compressionAlgorithm': 'GZIP'})]
        done = reports.get(c, made['id'])
        self.assertEqual(done['columns'], ['amazon-order-id', 'sku', 'quantity', 'note']);self.assertEqual(done['rows'], [['111-1', 'S1', '2', '=1+1']]);self.assertEqual(done['total'], 1)
        self.assertNotIn('x-amz-access-token', [r for r in fake.requests if r.url == DOC_URL][0].headers)
        data, name = reports.download(made['id']);text = data.decode('utf-8-sig')
        self.assertNotIn('Dubai', text);self.assertNotIn('b@example.com', text);self.assertIn("'=1+1", text);self.assertTrue(name.endswith('.csv'))
        before = len(fake.api());reports.get(c, made['id']);self.assertEqual(len(fake.api()), before)
        for bad in (('nope', 'AE'), ('orders', 'US')):
            with self.assertRaises(ValueError):reports.create(c, *bad)
        with self.assertRaises(ValueError):reports.get(c, '99999')

    def test_template_check_reports_missing_required_and_unknown_columns(self):
        fake = Amazon();c = client(fake)
        fields = [{'key': 'sku', 'canonical': 'sku'}, {'key': 'product_type#1.value', 'canonical': 'feed_product_type'}, {'key': '::record_action', 'canonical': 'update_delete'},
                  {'key': 'item_name#1.value', 'canonical': 'item_name', 'label': 'Item Name', 'column': 'D'}, {'key': 'old_field#1.value', 'canonical': '', 'label': 'Old Field', 'column': 'H'},
                  {'key': 'amzn1.volt.ca.product_id_value', 'canonical': 'product_id'}]
        schema = {'properties': {'item_name': {'title': 'Title'}, 'brand': {'title': 'Brand Name'}, 'externally_assigned_product_identifier': {}}, 'required': ['item_name', 'brand', 'externally_assigned_product_identifier']}
        fake.documents[SCHEMA_URL] = schema
        fake.replies = [(200, {'schema': {'link': {'resource': SCHEMA_URL}}, 'productTypeVersion': {'number': 'UAAAAAAAAAAAAAAAA=', 'latest': True}}), (404, {'errors': [{'code': 'NotFound', 'message': 'no such type'}]})]
        result = operations.template_check(c, listings.Definitions(), fields, ['SUITCASE', 'GONE'], 'AE')
        first, second = result['types']
        self.assertEqual(first['missing_required'], [{'attribute': 'brand', 'title': 'Brand Name'}]);self.assertEqual(first['unknown_columns'], [{'attribute': 'old_field', 'label': 'Old Field', 'column': 'H'}])
        self.assertTrue(first['version']['latest']);self.assertIn('error', second);self.assertEqual(result['status'], 'review')
        self.assertTrue(all(r.method == 'GET' for r in fake.requests if r.url.startswith(spapi.ENDPOINT)))


class ChangeTests(unittest.TestCase):
    def setUp(self):
        self.fake = Amazon();self.client = client(self.fake);self.time = [1000.0]
        self.temp = tempfile.TemporaryDirectory();self.log = Path(self.temp.name) / 'changes.jsonl'
        self.changes = operations.Changes(self.log, clock=lambda: self.time[0])
    def tearDown(self):self.temp.cleanup()

    def preview(self, items, replies=None):
        self.fake.replies = replies if replies is not None else [(200, ITEM), (200, {'status': 'VALID', 'issues': []})]
        return self.changes.preview(self.client, 'owner', 'AE', items)

    def test_preview_keeps_other_offer_fields_and_sends_nothing_live(self):
        row = self.preview([{'sku': 'S1', 'price': '21.50', 'quantity': 7}])['items'][0]
        self.assertTrue(row['token']);self.assertEqual([(c['old'], c['new']) for c in row['changes']], [('20.0', '21.50'), ('4', '7')]);self.assertTrue(any('促销价' in n for n in row['notes']))
        get, patch = self.fake.api()
        self.assertEqual(get.method, 'GET');self.assertEqual(patch.method, 'PATCH');self.assertIn('mode=VALIDATION_PREVIEW', patch.url)
        offer, stock = patch.body['patches']
        self.assertEqual(offer['path'], '/attributes/purchasable_offer');self.assertEqual(offer['value'][0]['our_price'], [{'schedule': [{'value_with_tax': 21.5}]}])
        self.assertEqual(offer['value'][0]['discounted_price'], ITEM['attributes']['purchasable_offer'][0]['discounted_price']);self.assertEqual(offer['value'][1], ITEM['attributes']['purchasable_offer'][1])
        self.assertEqual(stock['value'], [{'fulfillment_channel_code': 'DEFAULT', 'quantity': 7, 'lead_time_to_ship_max_days': 2}]);self.assertEqual(patch.body['productType'], 'SUITCASE')
        self.assertFalse(self.log.exists())

    def test_submit_sends_exactly_the_previewed_change_once_and_logs_it(self):
        row = self.preview([{'sku': 'S1', 'price': '21.50'}])['items'][0];previewed = self.fake.api()[-1].body
        for args in (('other', row['token'], 'S1', True), ('owner', row['token'], 'S2', True), ('owner', 'made-up', 'S1', True)):
            with self.assertRaises(ValueError):self.changes.submit(self.client, *args)
        with self.assertRaises(ValueError):self.changes.submit(self.client, 'owner', row['token'], 'S1', 'yes')
        self.assertEqual(len(self.fake.api()), 2)
        self.fake.replies = [(200, {'sku': 'S1', 'status': 'ACCEPTED', 'submissionId': 'sub-1', 'issues': []})]
        result = self.changes.submit(self.client, 'owner', row['token'], 'S1', True)
        live = self.fake.api()[-1]
        self.assertTrue(result['accepted']);self.assertEqual(live.method, 'PATCH');self.assertNotIn('mode=', live.url);self.assertEqual(live.body, previewed)
        with self.assertRaises(ValueError):self.changes.submit(self.client, 'owner', row['token'], 'S1', True)
        self.assertEqual(len(self.fake.api()), 3)
        logged = json.loads(self.log.read_text(encoding='utf-8').strip());self.assertEqual((logged['user'], logged['sku'], logged['status'], logged['submission']), ('owner', 'S1', 'ACCEPTED', 'sub-1'))
        self.assertEqual(self.changes.recent()[0]['changes'][0]['new'], '21.50')

    def test_preview_expires_and_invalid_or_unsafe_changes_get_no_token(self):
        row = self.preview([{'sku': 'S1', 'quantity': 0}])['items'][0];self.time[0] += operations.TOKEN_LIFE + 1
        with self.assertRaises(ValueError):self.changes.submit(self.client, 'owner', row['token'], 'S1', True)
        invalid = self.preview([{'sku': 'S1', 'price': '1'}], [(200, ITEM), (200, {'status': 'INVALID', 'issues': [{'code': '90117', 'message': 'Price too low', 'severity': 'ERROR', 'attributeNames': ['purchasable_offer']}]})])['items'][0]
        self.assertFalse(invalid['token']);self.assertEqual(invalid['issues'][0]['severity'], 'error');self.assertTrue(any('相差' in n for n in invalid['notes']))
        same = self.preview([{'sku': 'S1', 'price': '20', 'quantity': 4}], [(200, ITEM)])['items'][0];self.assertFalse(same['token']);self.assertIn('相同', same['error'])
        fba = {**ITEM, 'attributes': {'fulfillment_availability': [{'fulfillment_channel_code': 'AMAZON_EU'}]}}
        self.assertIn('亚马逊物流', self.preview([{'sku': 'S1', 'quantity': 3}], [(200, fba)])['items'][0]['error'])
        missing = self.preview([{'sku': 'S9', 'price': '5'}], [(404, {'errors': [{'code': 'NOT_FOUND', 'message': 'x'}]})])['items'][0];self.assertFalse(missing['token'])
        for bad in ([], [{'sku': 'S1'}], [{'sku': 'S1', 'price': '-1'}], [{'sku': 'S1', 'price': 12.5}], [{'sku': 'S1', 'price': '1.999'}], [{'sku': 'S1', 'quantity': 1.5}], [{'sku': 'S1', 'quantity': -1}], [{'sku': 'S1', 'price': '5'}, {'sku': 'S1', 'quantity': 1}], [{'sku': 'S%d' % i, 'quantity': 1} for i in range(21)]):
            before = len(self.fake.api())
            with self.assertRaises(ValueError):self.changes.preview(self.client, 'owner', 'AE', bad)
            self.assertEqual(len(self.fake.api()), before)

    def test_client_allows_live_writes_only_for_price_and_stock_patches(self):
        fake = Amazon();c = client(fake);item = spapi.LISTING_PATH + 'ATEST001/S1'
        ok = {'productType': 'PRODUCT', 'patches': [{'op': 'replace', 'path': '/attributes/purchasable_offer', 'value': []}]}
        refused = [('PATCH', item, {}, ok, False), ('PUT', item, {}, ok, True), ('DELETE', item, {}, None, True), ('PATCH', '/orders/v0/orders/1', {}, ok, True),
                   ('PATCH', item, {}, {'patches': [{'op': 'replace', 'path': '/attributes/item_name', 'value': []}]}, True), ('PATCH', item, {}, {'patches': [{'op': 'delete', 'path': '/attributes/purchasable_offer'}]}, True),
                   ('PATCH', item, {}, {'patches': []}, True), ('POST', '/feeds/2021-06-30/feeds', {}, {}, True), ('POST', '/reports/2021-06-30/schedules', {}, {}, True)]
        for method, path, query, body, live in refused:
            with self.assertRaises(spapi.SpApiError):c.call(method, path, query, body, live=live)
        self.assertEqual(fake.requests, [])
        c.call('PATCH', item, {'marketplaceIds': MARKET}, ok, live=True);self.assertEqual(fake.api()[0].method, 'PATCH')


class OperationsAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory();cls.root = Path(cls.temp.name) / 'library';cls.server = create_server(cls.root, 0)
        cls.fake = Amazon();cls.server.spapi = client(cls.fake)
        for user, role in [('owner', 'admin'), ('writer', 'editor'), ('reader', 'viewer')]:cls.server.auth.create(user, 'amazon-test-password', role)
        source = Path(cls.temp.name) / 'original.xlsm';source.write_bytes(template_book())
        cls.template = library.run(SimpleNamespace(command='add', library=str(cls.root), source=str(source), shop='测试甲', marketplace='AE', category='行李箱', version='v1', table_type='upload'))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.temp.cleanup()

    def request(self, path, body, cookie=None):
        c = HTTPConnection('127.0.0.1', self.server.server_port, timeout=20);headers = {'Content-Type': 'application/json', 'X-Template-Request': '1'}
        if cookie:headers['Cookie'] = cookie
        c.request('POST', path, json.dumps(body), headers);r = c.getresponse();result = r.status, r.read(), dict(r.getheaders());c.close();return result

    def login(self, user):return self.request('/api/login', {'username': user, 'password': 'amazon-test-password'})[2]['Set-Cookie'].split(';')[0]

    def test_viewers_are_refused_and_unknown_routes_are_404(self):
        owner, reader = self.login('owner'), self.login('reader');before = len(self.fake.requests)
        for route in ('overview', 'orders', 'order-items', 'sales', 'fba-inventory', 'report/create', 'report', 'report/download', 'template-check', 'change/preview', 'change/submit', 'change/log'):
            self.assertEqual(self.request('/api/amazon/' + route, {'marketplace': 'AE'}, reader)[0], 403, route)
            self.assertEqual(self.request('/api/amazon/' + route, {'marketplace': 'AE'})[0], 401, route)
        self.assertEqual(len(self.fake.requests), before)
        self.assertEqual(self.request('/api/amazon/feeds', {}, owner)[0], 404)

    def test_change_flow_and_template_check_over_http(self):
        owner, writer = self.login('owner'), self.login('writer')
        self.fake.replies = [(200, ITEM), (200, {'status': 'VALID', 'issues': []})]
        status, raw, _ = self.request('/api/amazon/change/preview', {'marketplace': 'AE', 'items': [{'sku': 'S1', 'quantity': 9}]}, writer);self.assertEqual(status, 200, raw)
        row = json.loads(raw)['items'][0];self.assertTrue(row['token'])
        self.assertEqual(self.request('/api/amazon/change/submit', {'token': row['token'], 'sku': 'S1', 'confirmed': True}, owner)[0], 400)
        self.assertEqual(self.request('/api/amazon/change/submit', {'token': row['token'], 'sku': 'S1'}, writer)[0], 400)
        self.fake.replies = [(200, {'status': 'ACCEPTED', 'submissionId': 'sub-9', 'issues': []})]
        status, raw, _ = self.request('/api/amazon/change/submit', {'token': row['token'], 'sku': 'S1', 'confirmed': True}, writer);self.assertEqual(status, 200, raw);self.assertTrue(json.loads(raw)['accepted'])
        status, raw, _ = self.request('/api/amazon/change/log', {}, owner);self.assertEqual(json.loads(raw)[0]['submission'], 'sub-9')
        self.assertTrue((self.root.parent / 'amazon-changes.jsonl').exists())
        self.fake.replies = [(200, {'schema': {'link': {'resource': SCHEMA_URL}}, 'productTypeVersion': {'number': 'v1', 'latest': True}})]
        status, raw, _ = self.request('/api/amazon/template-check', {'template_id': self.template['id']}, owner);self.assertEqual(status, 200, raw)
        result = json.loads(raw);self.assertEqual(result['types'][0]['product_type'], 'SUITCASE');self.assertEqual(result['template']['id'], self.template['id'])
        self.assertIn('size', [u['attribute'] for u in result['types'][0]['unknown_columns']])


class ListingSubmitTests(unittest.TestCase):
    """A new product goes live only from a row Amazon validated, one confirmed SKU at a time."""
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory();cls.root = Path(cls.temp.name) / 'library';cls.server = create_server(cls.root, 0)
        cls.fake = Amazon();cls.server.spapi = client(cls.fake)
        for user, role in [('owner', 'admin'), ('writer', 'editor')]:cls.server.auth.create(user, 'amazon-test-password', role)
        source = Path(cls.temp.name) / 'original.xlsm';source.write_bytes(template_book())
        cls.template = library.run(SimpleNamespace(command='add', library=str(cls.root), source=str(source), shop='测试甲', marketplace='AE', category='行李箱', version='v1', table_type='upload'))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.temp.cleanup()

    request = OperationsAPITests.request
    login = OperationsAPITests.login

    def precheck(self, cookie, csv, replies):
        import base64
        body = {'filename': 'products.csv', 'data': base64.b64encode(csv).decode(), 'template_id': self.template['id'], 'shop': '测试甲', 'marketplace': 'AE', 'table_type': 'upload', 'precheck_consent': True}
        self.fake.replies = ([] if self.server.spapi_definitions.cache else [(200, {'schema': {'link': {'resource': SCHEMA_URL}}})]) + replies
        status, raw, _ = self.request('/api/fill/precheck', body, cookie);self.assertEqual(status, 200, raw);return json.loads(raw)

    def test_only_valid_rows_get_a_token_and_submit_sends_the_validated_body(self):
        owner, writer = self.login('owner'), self.login('writer')
        self.fake.documents[SCHEMA_URL] = SCHEMA
        result = self.precheck(owner, b'SKU,title,brand,price,quantity\nNEW1,Case,BrandA,20,1\nBAD2,Bag,BrandA,30,2', [(200, {'status': 'VALID', 'issues': [{'code': '1', 'message': 'tip', 'severity': 'WARNING'}]}), (200, {'status': 'INVALID', 'issues': [{'code': '90220', 'message': 'brand missing', 'severity': 'ERROR', 'attributeNames': ['brand']}]})])
        self.assertEqual([i['sku'] for i in result['submittable']], ['NEW1']);item = result['submittable'][0];self.assertEqual(item['title'], 'Case')
        previewed = [r for r in self.fake.api() if r.method == 'PUT' and '/NEW1?' in r.url][-1]
        submit = lambda cookie, **extra: self.request('/api/amazon/listing/submit', {'token': item['token'], 'sku': 'NEW1', **extra}, cookie)
        sent = len(self.fake.api())
        self.assertEqual(submit(owner)[0], 400);self.assertEqual(submit(writer, confirmed=True)[0], 400);self.assertEqual(len(self.fake.api()), sent)
        # The SKU already exists: nothing is written until the member confirms the overwrite.
        self.fake.replies = [(200, {'summaries': [{'itemName': 'Old case', 'asin': 'B000OLD001'}]})]
        status, raw, _ = submit(owner, confirmed=True);self.assertEqual(status, 200, raw);self.assertEqual(json.loads(raw), {'needs_overwrite': True, 'sku': 'NEW1', 'title': 'Old case', 'asin': 'B000OLD001'})
        self.assertEqual(self.fake.api()[-1].method, 'GET')
        self.fake.replies = [(404, {'errors': [{'code': 'NOT_FOUND', 'message': 'x'}]}), (200, {'status': 'ACCEPTED', 'submissionId': 'sub-new', 'issues': []})]
        status, raw, _ = submit(owner, confirmed=True);self.assertEqual(status, 200, raw);self.assertTrue(json.loads(raw)['accepted'])
        live = self.fake.api()[-1]
        self.assertEqual(live.method, 'PUT');self.assertNotIn('mode=', live.url);self.assertEqual(live.body, previewed.body);self.assertEqual(live.body['requirements'], 'LISTING')
        self.assertEqual(submit(owner, confirmed=True, overwrite=True)[0], 400)
        logged = [json.loads(line) for line in (self.root.parent / 'amazon-changes.jsonl').read_text(encoding='utf-8').splitlines()][-1]
        self.assertEqual((logged['sku'], logged['changes'][0]['kind'], logged['submission']), ('NEW1', 'listing', 'sub-new'))

    def test_confirmed_overwrite_skips_the_lookup_and_price_tables_get_no_token(self):
        owner = self.login('owner');self.fake.documents[SCHEMA_URL] = SCHEMA
        result = self.precheck(owner, b'SKU,title,brand,price,quantity\nNEW3,Case,BrandA,20,1', [(200, {'status': 'VALID', 'issues': []})])
        item = result['submittable'][0];before = len(self.fake.api())
        self.fake.replies = [(200, {'status': 'ACCEPTED', 'submissionId': 'sub-over', 'issues': []})]
        status, raw, _ = self.request('/api/amazon/listing/submit', {'token': item['token'], 'sku': 'NEW3', 'confirmed': True, 'overwrite': True}, owner);self.assertEqual(status, 200, raw)
        self.assertEqual([r.method for r in self.fake.api()[before:]], ['PUT'])
        fake = Amazon();c = client(fake);fields, rows = [{'key': 'sku', 'label': 'SKU', 'column': 'A', 'canonical': 'sku'}, {'key': 'fulfillment_availability#1.quantity', 'label': 'Quantity', 'column': 'B', 'canonical': 'quantity'}], [{'sku': 'S1', 'source_rows': [2], 'values': {'sku': 'S1', 'fulfillment_availability#1.quantity': '3'}}]
        fake.replies = [(200, {'status': 'VALID', 'issues': []})];held = []
        self.assertEqual(listings.precheck(c, listings.Definitions(), rows, fields, 'AE', 'price_update', 1, 20, hold=lambda *a: held.append(a))['submittable'], []);self.assertEqual(held, [])

    def test_client_allows_a_live_put_only_for_a_complete_listing(self):
        fake = Amazon();c = client(fake);item = spapi.LISTING_PATH + 'ATEST001/S1';ok = {'productType': 'SUITCASE', 'requirements': 'LISTING', 'attributes': {'item_name': [{'value': 'Case'}]}}
        for body, live, path, query in [(ok, False, item, {}), ({**ok, 'requirements': 'LISTING_OFFER_ONLY'}, True, item, {}), ({**ok, 'attributes': {}}, True, item, {}), ({**ok, 'productType': ''}, True, item, {}), (ok, True, '/orders/v0/orders/1', {}), (ok, 'yes', item, {})]:
            with self.assertRaises(spapi.SpApiError):c.call('PUT', path, query, body, live=live)
        self.assertEqual(fake.requests, [])
        c.call('PUT', item, {'marketplaceIds': MARKET}, ok, live=True);self.assertEqual(fake.api()[0].method, 'PUT')


if __name__ == '__main__':
    unittest.main()
