"""Offers watch (hijackers, Buy Box) and restock suggestions."""
import datetime
import json
from pathlib import Path
import tempfile
import unittest

from template_manager import spapi, watch

UTC = datetime.timezone.utc
OWN = 'AOWNSELLER1'


def offer(seller, price, buy_box=False, fba=True):
    return {'SellerId': seller, 'ListingPrice': {'Amount': price, 'CurrencyCode': 'AED'}, 'Shipping': {'Amount': 0, 'CurrencyCode': 'AED'},
            'IsBuyBoxWinner': buy_box, 'IsFulfilledByAmazon': fba}


def reply(*offers):
    winner = next((o for o in offers if o['IsBuyBoxWinner']), None)
    summary = {'TotalOfferCount': len(offers)}
    if winner:
        summary['BuyBoxPrices'] = [{'LandedPrice': winner['ListingPrice']}]
    return {'payload': {'Offers': list(offers), 'Summary': summary}}


class FakeClient:
    store_id, label, region = 'default', '主店铺', 'EU'

    def __init__(self, replies):
        self.replies, self.calls = dict(replies), []

    def require(self):
        return {'seller_id': OWN}

    def call(self, method, path, query=None, body=None, live=False):
        self.calls.append((method, path, query))
        asin = path.split('/')[-2]
        value = self.replies[asin]
        if isinstance(value, Exception):
            raise value
        return value


class OffersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'offers-watch.json'
        self.sleeps = []
        self.watch = watch.Offers(self.path, sleep=self.sleeps.append, now=lambda: datetime.datetime(2026, 11, 19, 8, tzinfo=UTC))

    def test_states_are_read_from_offers(self):
        client = FakeClient({
            'B000000001': reply(offer(OWN, 50, True)),
            'B000000002': reply(offer(OWN, 50, True), offer('AOTHER0001', 52)),
            'B000000003': reply(offer(OWN, 50), offer('AOTHER0001', 45, True)),
            'B000000004': reply(offer('AOTHER0001', 45, True)),
            'B000000005': reply(offer(OWN, 50), offer('AOTHER0002', 49)),
            'B000000006': spapi.SpApiError('not found', 404),
        })
        result = self.watch.check(client, None, 'AE', 'B000000001, B000000002\nB000000003 B000000004 b000000005 B000000006')
        states = {row['asin']: row.get('state', 'error') for row in result['items']}
        self.assertEqual(states, {'B000000001': 'ours', 'B000000002': 'shared', 'B000000003': 'lost', 'B000000004': 'not_selling', 'B000000005': 'no_buy_box', 'B000000006': 'error'})
        self.assertEqual(self.sleeps, [watch.OFFERS_INTERVAL] * 5)
        lost = next(r for r in result['items'] if r['asin'] == 'B000000003')
        self.assertEqual(lost['others'][0]['link'], 'https://www.amazon.ae/sp?seller=AOTHER0001')
        self.assertEqual(lost['mine']['price'], 50.0);self.assertFalse(lost['buy_box_own'])
        self.assertTrue(all(r.get('first_check', True) for r in result['items']))
        self.assertEqual(client.calls[0][2], {'MarketplaceId': 'A2VIGQ35RCS4UG', 'ItemCondition': 'New'})

    def test_second_check_marks_new_sellers_and_lost_buy_box(self):
        self.watch.check(FakeClient({'B000000002': reply(offer(OWN, 50, True), offer('AOTHER0001', 52))}), None, 'AE', ['B000000002'])
        again = watch.Offers(self.path, sleep=self.sleeps.append)  # survives a restart
        result = again.check(FakeClient({'B000000002': reply(offer(OWN, 50), offer('AOTHER0001', 52), offer('ANEWSELL01', 44, True))}), None, 'AE', ['B000000002'])
        row = result['items'][0]
        self.assertEqual(row['state'], 'lost');self.assertEqual(row['new_sellers'], ['ANEWSELL01'])
        self.assertIn('上次检查时购物车还在我们这', row['note']);self.assertTrue(result['previous_at'])
        # Another store or site keeps its own history.
        other = watch.Offers(self.path, sleep=self.sleeps.append).check(FakeClient({'B000000002': reply(offer(OWN, 50), offer('ANEWSELL01', 44, True))}), None, 'SA', ['B000000002'])
        self.assertTrue(other['items'][0]['first_check'])
        self.assertNotIn('Atzr', self.path.read_text(encoding='utf-8'))

    def test_bad_input_is_refused_before_any_call(self):
        client = FakeClient({})
        for bad in ([], 'not-an-asin', ['B000000001'] * 0, ['B00000000%d' % (i % 10) + 'X' for i in range(3)], ['B%09d' % i for i in range(21)]):
            with self.assertRaises(ValueError):self.watch.check(client, None, 'AE', bad)
        with self.assertRaises(spapi.SpApiError):self.watch.check(client, None, 'US', ['B000000001'])
        self.assertEqual(client.calls, [])

    def test_auth_errors_stop_the_run(self):
        client = FakeClient({'B000000001': spapi.SpApiError('denied', 403), 'B000000002': reply()})
        with self.assertRaises(spapi.SpApiError):self.watch.check(client, None, 'AE', ['B000000001', 'B000000002'])
        self.assertEqual(len(client.calls), 1)


COLUMNS = ['amazon-order-id', 'purchase-date', 'order-status', 'fulfillment-channel', 'sku', 'quantity', 'buyer-email']
NOW = datetime.datetime(2026, 11, 19, tzinfo=UTC)


def order(sku, qty, days_ago, status='Shipped', channel='Amazon'):
    return ['111-0000000-0000000', (NOW - datetime.timedelta(days=days_ago)).isoformat(), status, channel, sku, str(qty), '']


class RestockTests(unittest.TestCase):
    def test_sales_count_only_fba_orders_that_were_not_cancelled(self):
        rows = [order('A', 2, 1), order('A', 1, 20), order('A', 5, 2, status='Cancelled'), order('A', 4, 3, channel='Merchant'), order('B', 3, 10), ['x']]
        total, recent = watch.sold_per_sku(COLUMNS, rows, 30, NOW)
        self.assertEqual(total, {'A': 3, 'B': 3});self.assertEqual(recent, {'A': 2})
        with self.assertRaises(ValueError):watch.sold_per_sku(['order-id'], rows, 30, NOW)

    def test_suggestions_and_states(self):
        inventory = [
            {'sku': 'OUT', 'asin': 'B1', 'fulfillable': 0, 'inbound': 0},
            {'sku': 'URGENT', 'fulfillable': 10, 'inbound': 0},
            {'sku': 'SOON', 'fulfillable': 100, 'inbound': 20},
            {'sku': 'OK', 'fulfillable': 900, 'inbound': 0},
            {'sku': 'SLOW', 'fulfillable': 40, 'inbound': 0},
            {'sku': 'IDLE', 'fulfillable': 0, 'inbound': 0},
        ]
        total = {'OUT': 60, 'URGENT': 60, 'SOON': 60, 'OK': 60, 'GONE': 30}
        recent = {'OUT': 14, 'URGENT': 14, 'SOON': 14, 'OK': 14}
        result = watch.restock(inventory, total, recent, 30, 30, 30)
        rows = {r['sku']: r for r in result['items']}
        self.assertEqual(rows['SOON']['daily'], 2.0)  # (60/30 + 14/7) / 2
        self.assertEqual(rows['SOON']['suggest'], 2 * 60 - 100 - 20 or 0)
        self.assertEqual(rows['URGENT']['days_left'], 5.0);self.assertEqual(rows['URGENT']['suggest'], 110)
        self.assertEqual({k: r['state'] for k, r in rows.items()},
                         {'OUT': 'out', 'URGENT': 'urgent', 'SOON': 'ok', 'OK': 'ok', 'SLOW': 'slow', 'IDLE': 'idle', 'GONE': 'out'})
        self.assertEqual(rows['OK']['suggest'], 0);self.assertEqual(rows['GONE']['daily'], 0.5)
        self.assertEqual([r['state'] for r in result['items']][:3], ['out', 'out', 'urgent'])
        lower = {r['sku']: r for r in watch.restock(inventory, total, recent, 30, 30, 60)['items']}
        self.assertEqual(lower['SOON']['state'], 'soon');self.assertEqual(lower['SOON']['suggest'], 60)
        for bad in ((6, 30, 30), (30, 0, 30), (30, 30, 181), (30, '30', 30)):
            with self.assertRaises(ValueError):watch.restock(inventory, total, recent, *bad)


if __name__ == '__main__':
    unittest.main()
