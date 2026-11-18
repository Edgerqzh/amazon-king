"""Store analysis: listing issues, inbound shipments, profit, returns, review requests."""
import datetime
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from template_manager import insights, spapi

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 11, 19, 8, tzinfo=UTC)


class Fake:
    store_id, label, region = 'default', '主店铺', 'EU'

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def seller_id(self):
        return 'AOWNSELLER1'

    def call(self, method, path, query=None, body=None, live=False):
        self.calls.append((method, path, dict(query or {}), body, live))
        if not spapi.Client.permitted(method, path, query or {}, body, live):
            raise AssertionError('not permitted: %s %s' % (method, path))
        value = self.answer(method, path, query or {})
        if isinstance(value, Exception):
            raise value
        return value


def money(amount):
    return {'CurrencyAmount': amount, 'CurrencyCode': 'AED'}


class ListingIssueTests(unittest.TestCase):
    def test_pages_are_read_and_unsellable_items_come_first(self):
        pages = [
            {'items': [{'sku': 'OK-1', 'summaries': [{'marketplaceId': 'A2VIGQ35RCS4UG', 'status': ['BUYABLE']}], 'issues': []},
                       {'sku': 'WARN-1', 'summaries': [{'marketplaceId': 'A2VIGQ35RCS4UG', 'status': ['BUYABLE'], 'asin': 'B1'}],
                        'issues': [{'code': '18027', 'message': 'image', 'severity': 'WARNING'}]}], 'pagination': {'nextToken': 'p2'}},
            {'items': [{'sku': 'BAD-1', 'summaries': [{'marketplaceId': 'A2VIGQ35RCS4UG', 'status': ['DISCOVERABLE']}],
                        'issues': [{'code': '90220', 'message': 'missing', 'severity': 'ERROR', 'attributeNames': ['item_name']}]}]},
        ]
        client = Fake(lambda m, p, q: pages.pop(0))
        result = insights.listing_issues(client, 'AE', sleep=lambda s: None)
        self.assertEqual([r['sku'] for r in result['items']], ['BAD-1', 'WARN-1'])
        self.assertEqual(result['counts'], {'not_buyable': 1, 'errors': 1, 'warnings_only': 1})
        self.assertEqual(client.calls[0][1], spapi.LISTING_PATH + 'AOWNSELLER1')
        self.assertEqual(client.calls[1][2]['pageToken'], 'p2');self.assertEqual(client.calls[0][2]['withIssueSeverity'], 'ERROR,WARNING')


class InboundTests(unittest.TestCase):
    def test_closed_shipment_with_missing_units_is_flagged(self):
        def answer(method, path, query):
            if path == '/fba/inbound/v0/shipments':
                return {'payload': {'ShipmentData': [
                    {'ShipmentId': 'FBA15ABC', 'ShipmentName': 'Pens', 'ShipmentStatus': 'CLOSED', 'DestinationFulfillmentCenterId': 'DXB3'},
                    {'ShipmentId': 'FBA15DEF', 'ShipmentStatus': 'RECEIVING'},
                    {'ShipmentId': 'FBA15XYZ', 'ShipmentStatus': 'CANCELLED'}, {'ShipmentId': '../x', 'ShipmentStatus': 'CLOSED'}]}}
            items = {'FBA15ABC': [{'SellerSKU': 'A', 'QuantityShipped': 100, 'QuantityReceived': 96}],
                     'FBA15DEF': [{'SellerSKU': 'B', 'QuantityShipped': 50, 'QuantityReceived': 10}]}[path.split('/')[-2]]
            return {'payload': {'ItemData': items}}
        client = Fake(answer)
        result = insights.inbound(client, 'AE', 90, NOW, sleep=lambda s: None)
        self.assertEqual([s['id'] for s in result['items']], ['FBA15ABC', 'FBA15DEF', 'FBA15XYZ'])
        first = result['items'][0]
        self.assertTrue(first['short']);self.assertEqual((first['shipped'], first['received']), (100, 96))
        self.assertFalse(result['items'][1]['short']);self.assertIsNone(result['items'][2]['shipped'])
        self.assertEqual(result['counts'], {'open': 1, 'short': 1})
        self.assertEqual(len(client.calls), 3)  # the cancelled shipment's items are not read
        with self.assertRaises(ValueError):insights.inbound(client, 'AE', 400, NOW)


class ProfitTests(unittest.TestCase):
    def events(self):
        shipment = {'MarketplaceName': 'Amazon.ae', 'ShipmentItemList': [{'SellerSKU': 'PEN', 'QuantityShipped': 2,
                    'ItemChargeList': [{'ChargeType': 'Principal', 'ChargeAmount': money(105.0)}, {'ChargeType': 'ShippingCharge', 'ChargeAmount': money(5.0)}],
                    'ItemFeeList': [{'FeeType': 'Commission', 'FeeAmount': money(-15.75)}, {'FeeType': 'FBAPerUnitFulfillmentFee', 'FeeAmount': money(-20.0)}],
                    'PromotionList': [{'PromotionAmount': money(-5.0)}]}]}
        other_site = {'MarketplaceName': 'Amazon.sa', 'ShipmentItemList': [{'SellerSKU': 'PEN', 'QuantityShipped': 9, 'ItemChargeList': [{'ChargeType': 'Principal', 'ChargeAmount': money(999.0)}]}]}
        no_cost = {'MarketplaceName': 'Amazon.ae', 'ShipmentItemList': [{'SellerSKU': 'CUP', 'QuantityShipped': 1, 'ItemChargeList': [{'ChargeType': 'Principal', 'ChargeAmount': money(21.0)}]}]}
        refund = {'MarketplaceName': 'Amazon.ae', 'ShipmentItemAdjustmentList': [{'SellerSKU': 'PEN', 'QuantityShipped': 1,
                  'ItemChargeAdjustmentList': [{'ChargeType': 'Principal', 'ChargeAmount': money(-52.5)}],
                  'ItemFeeAdjustmentList': [{'FeeType': 'Commission', 'FeeAmount': money(6.3)}]}]}
        service = {'FeeList': [{'FeeType': 'Subscription', 'FeeAmount': money(-99.0)}]}
        return [shipment, other_site, no_cost], [refund], [service]

    def test_profit_per_sku(self):
        shipments, refunds, services = self.events()
        result = insights.profit(shipments, refunds, services, 'AE', {'PEN': '20.00'})
        pen = next(r for r in result['items'] if r['sku'] == 'PEN')
        self.assertEqual((pen['units'], pen['refund_units'], pen['sales'], pen['refunds'], pen['fees']), (2, 1, '105.00', '-52.50', '-29.45'))
        # VAT inside the 52.50 kept: 52.5 * 5 / 105 = 2.50
        self.assertEqual(pen['vat'], '2.50');self.assertEqual(pen['cost'], '40.00')
        self.assertEqual(pen['profit'], '-19.45')  # 105 + 5 - 5 - 29.45 - 52.5 - 2.5 - 40
        cup = next(r for r in result['items'] if r['sku'] == 'CUP')
        self.assertTrue(cup['cost_missing']);self.assertEqual(result['cost_missing'], ['CUP'])
        self.assertEqual(result['totals']['service_fees'], '-99.00');self.assertEqual(result['currency'], 'AED')
        self.assertEqual(result['totals']['sales'], '126.00')  # the SA event is left out
        no_vat = insights.profit(shipments, refunds, services, 'AE', {'PEN': '20'}, vat_included=False)
        self.assertEqual(next(r for r in no_vat['items'] if r['sku'] == 'PEN')['vat'], '0.00')
        with self.assertRaises(ValueError):insights.profit(shipments, refunds, services, 'AE', {}, vat_rate=50)

    def test_events_are_paged_slowly(self):
        pages = [{'payload': {'FinancialEvents': {'ShipmentEventList': [{'x': 1}]}, 'NextToken': 'n'}}, {'payload': {'FinancialEvents': {'RefundEventList': [{'y': 1}]}}}]
        sleeps = []
        client = Fake(lambda m, p, q: pages.pop(0))
        shipments, refunds, services, truncated = insights.financial_events(client, 30, NOW, sleep=sleeps.append)
        self.assertEqual((len(shipments), len(refunds), truncated), (1, 1, False));self.assertEqual(sleeps, [2.1])
        self.assertEqual(client.calls[1][2], {'NextToken': 'n'})
        self.assertEqual(client.calls[0][2]['PostedBefore'], '2026-11-19T07:55:00Z')

    def test_costs_are_kept_per_store_and_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'costs.json'
            costs = insights.Costs(path)
            costs.set('default', 'AE', {'PEN': '8.5', 'CUP': 0})
            costs.set('default', 'AE', {'CUP': ''})
            self.assertEqual(insights.Costs(path).get('default', 'AE'), {'PEN': '8.50'})
            self.assertEqual(costs.get('default', 'SA'), {})
            for bad in ({'PEN': 'abc'}, {'PEN': -1}, {'': 1}, ['PEN']):
                with self.assertRaises(ValueError):costs.set('default', 'AE', bad)


class ReturnsTests(unittest.TestCase):
    def test_rates_reasons_and_flags(self):
        columns = ['return-date', 'order-id', 'sku', 'asin', 'product-name', 'quantity', 'detailed-disposition', 'reason']
        rows = ([['', '', 'PEN', 'B1', 'Pen', '1', 'SELLABLE', 'UNWANTED_ITEM']] * 2 + [['', '', 'CUP', 'B2', 'Cup', '1', 'DEFECTIVE', 'DEFECTIVE']] * 3
                + [['', '', 'CUP', 'B2', 'Cup', '2', 'CUSTOMER_DAMAGED', 'NOT_AS_DESCRIBED']])
        result = insights.returns(columns, rows, {'PEN': 100, 'CUP': 20})
        cup, pen = result['items']
        self.assertEqual((cup['sku'], cup['returned'], cup['rate'], cup['flag']), ('CUP', 5, 25.0, True))
        self.assertEqual(cup['top_reasons'][0], {'code': 'DEFECTIVE', 'label': '商品有缺陷', 'count': 3})
        self.assertEqual((pen['rate'], pen['flag']), (2.0, False))
        self.assertEqual((result['total_returned'], result['total_sold'], result['flagged']), (7, 120, 1))


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / 'reviews.jsonl'
        self.reviews = insights.Reviews(self.log, sleep=lambda s: None, now=lambda: NOW)

    def answer(self, method, path, query):
        if path == '/orders/v0/orders':
            return {'payload': {'Orders': [{'AmazonOrderId': '111-0000001-0000001', 'PurchaseDate': '2026-11-01T00:00:00Z', 'FulfillmentChannel': 'AFN', 'OrderTotal': {'Amount': '20', 'CurrencyCode': 'AED'}, 'BuyerInfo': {'BuyerEmail': 'x@y'}},
                                           {'AmazonOrderId': '111-0000002-0000002', 'PurchaseDate': '2026-10-25T00:00:00Z'}]}}
        if method == 'GET':
            eligible = path.endswith('0000001')
            return {'_links': {'actions': [{'href': path + '/solicitations/productReviewAndSellerFeedback', 'name': 'productReviewAndSellerFeedback'}] if eligible else []}}
        return {}

    def test_candidates_window_and_no_buyer_details(self):
        client = Fake(self.answer)
        result = self.reviews.candidates(client, 'AE')
        query = client.calls[0][2]
        self.assertEqual((query['CreatedAfter'], query['CreatedBefore'], query['OrderStatuses']), ('2026-10-15T08:00:00Z', '2026-11-14T08:00:00Z', 'Shipped'))
        self.assertEqual([o['id'] for o in result['orders']], ['111-0000002-0000002', '111-0000001-0000001'])
        self.assertNotIn('x@y', json.dumps(result))

    def test_send_checks_eligibility_and_remembers(self):
        client = Fake(self.answer)
        with self.assertRaises(ValueError):self.reviews.send(client, 'amy', 'AE', ['111-0000001-0000001'], False)
        for bad in ([], ['bad'], ['111-0000001-0000001'] * 2, ['111-0000001-%07d' % i for i in range(21)]):
            with self.assertRaises(ValueError):self.reviews.send(client, 'amy', 'AE', bad, True)
        self.assertEqual(client.calls, [])
        result = self.reviews.send(client, 'amy', 'AE', ['111-0000001-0000001', '111-0000002-0000002'], True)
        self.assertEqual([r['result'] for r in result['items']], ['sent', 'not_eligible'])
        posts = [c for c in client.calls if c[0] == 'POST']
        self.assertEqual(posts, [('POST', '/solicitations/v1/orders/111-0000001-0000001/solicitations/productReviewAndSellerFeedback', {'marketplaceIds': 'A2VIGQ35RCS4UG'}, None, True)])
        again = insights.Reviews(self.log, sleep=lambda s: None, now=lambda: NOW)  # remembered after a restart
        self.assertEqual(again.send(Fake(self.answer), 'amy', 'AE', ['111-0000001-0000001'], True)['items'][0]['result'], 'skipped')
        self.assertEqual({o['id']: o['done'] for o in again.candidates(Fake(self.answer), 'AE')['orders']}, {'111-0000001-0000001': 'sent', '111-0000002-0000002': 'not_eligible'})

    def test_missing_permission_stops_the_batch(self):
        client = Fake(lambda m, p, q: spapi.SpApiError('Unauthorized', 403))
        with self.assertRaises(spapi.SpApiError):self.reviews.send(client, 'amy', 'AE', ['111-0000001-0000001', '111-0000002-0000002'], True)
        self.assertEqual(len(client.calls), 1)

    def test_only_this_exact_request_is_allowed_to_send(self):
        allowed = spapi.Client.permitted
        path = '/solicitations/v1/orders/111-0000001-0000001/solicitations/productReviewAndSellerFeedback'
        self.assertTrue(allowed('POST', path, {'marketplaceIds': 'X'}, None, True))
        self.assertFalse(allowed('POST', path, {'marketplaceIds': 'X'}, None, False))
        self.assertFalse(allowed('POST', path, {'marketplaceIds': 'X'}, {'text': 'hi'}, True))
        self.assertFalse(allowed('POST', '/messaging/v1/orders/111-0000001-0000001/messages/legalDisclosure', {'marketplaceIds': 'X'}, None, True))
        self.assertFalse(allowed('POST', path.replace('111-0000001-0000001', '../x'), {'marketplaceIds': 'X'}, None, True))


if __name__ == '__main__':
    unittest.main()
