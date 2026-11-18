import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.parse
import zipfile
from unittest.mock import patch
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from template_manager import bsr, filling, operations, spapi
from test_bsr import card, page
from test_spapi import FakeAmazon, client
from test_stores import make, SECOND
from test_filling import template_book


class RegionTests(unittest.TestCase):
    def test_endpoints_and_marketplace_region_validation_before_network(self):
        for region, market, host in [('EU', 'AE', 'sellingpartnerapi-eu.amazon.com'), ('NA', 'US', 'sellingpartnerapi-na.amazon.com')]:
            fake = FakeAmazon();c = client(fake);c.region = region
            c.call('GET', '/catalog/2022-04-01/items', {'marketplaceIds': spapi.marketplace_id(market, region)})
            self.assertEqual(urllib.parse.urlsplit([r for r in fake.requests if r.url != spapi.TOKEN_URL][0].url).hostname, host)
            before = len(fake.requests)
            bad = 'US' if region == 'EU' else 'AE'
            with self.assertRaises(spapi.SpApiError) as caught:
                operations.orders(c, bad)
            self.assertIn('不一致', str(caught.exception))
            with self.assertRaises(spapi.SpApiError):
                c.call('GET', '/catalog/2022-04-01/items', {'marketplaceIds': spapi.MARKETPLACE_IDS[bad]})
            with self.assertRaises(spapi.SpApiError):
                c.call('POST', spapi.REPORTS_PATH, body={'marketplaceIds': [spapi.MARKETPLACE_IDS[bad]]})
            self.assertEqual(len(fake.requests), before)
        self.assertEqual(spapi.marketplace_id('US', 'NA'), 'ATVPDKIKX0DER')

    def test_old_store_index_defaults_europe_and_region_survives_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            stores, vaults, _, _ = make(temp)
            stores.index_path.write_text(json.dumps({'stores': [{'id': 'default', 'name': '旧店铺'}]}), encoding='utf-8')
            self.assertEqual(stores.client().region, 'EU')
            shown = stores.save('new', '美国店', {**SECOND, 'region': 'NA'})
            self.assertEqual(shown['region'], 'NA')
            self.assertEqual(set(shown['marketplace_ids']), {'US'})
            fresh = spapi.Stores(stores.get_default(), stores.index_path, vault_for=vaults)
            self.assertEqual(fresh.client(shown['store']).endpoint, spapi.ENDPOINTS['NA'])
            stores.save('default', None, {'region': 'NA'})
            self.assertEqual(stores.client().region, 'NA')
            before = stores.index_path.read_bytes()
            with self.assertRaises(spapi.SpApiError):
                stores.save('default', None, {'region': 'FE'})
            self.assertEqual(before, stores.index_path.read_bytes())

    def test_us_sales_handles_both_daylight_offsets(self):
        fake = FakeAmazon();c = client(fake);c.region = 'NA'
        for month, offset in [(1, '-08:00'), (7, '-07:00')]:
            now = datetime.datetime(2026, month, 13, 18, tzinfo=datetime.timezone.utc)
            operations.sales(c, 'US', 2, now)
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.requests[-1].url).query)
            self.assertEqual(query['granularityTimeZone'], ['America/Los_Angeles'])
            self.assertIn(offset, query['interval'][0])
            self.assertEqual(query['marketplaceIds'], ['ATVPDKIKX0DER'])

    def test_us_public_bsr_and_actual_price_currency(self):
        url = 'https://www.amazon.com/gp/bestsellers/home/123'
        html = page(card()).replace('AED', '$')
        parsed = bsr.parse_page(html, 'US', url)
        self.assertEqual(parsed['items'][0]['currency'], 'USD')
        self.assertEqual(parsed['items'][0]['price'], '1249.05')
        self.assertTrue(parsed['items'][0]['url'].startswith('https://www.amazon.com/dp/'))
        foreign = bsr.parse_page(html.replace('$', 'S$'), 'US', url)
        self.assertEqual(foreign['items'][0]['currency'], '')

    def test_us_timezone_fallback_and_daylight_transition(self):
        fake = FakeAmazon();c = client(fake);c.region = 'NA'
        with patch.object(operations, 'ZoneInfo') as zone:
            zone.side_effect = ZoneInfoNotFoundError
            zone.from_file.side_effect = ZoneInfo.from_file
            operations.sales(c, 'US', 2, datetime.datetime(2026, 3, 9, 18, tzinfo=datetime.timezone.utc))
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.requests[-1].url).query)
        start, end = query['interval'][0].split('--')
        self.assertTrue(start.endswith('-08:00'))
        self.assertTrue(end.endswith('-07:00'))

    def test_us_template_filling_preserves_sku_and_validates_usd(self):
        target = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(template_book())) as source, zipfile.ZipFile(target, 'w') as dest:
            for entry in source.infolist():
                dest.writestr(entry, source.read(entry.filename).replace(b'A2VIGQ35RCS4UG', b'ATVPDKIKX0DER'))
        data = 'SKU,标题,品牌,售价,库存,币种\n00007,Travel case,BrandA,19.50,2,USD\n'.encode()
        book, template, report, rows = filling.prepare(target.getvalue(), data, 'products.csv', 'US', 'upload')
        try:
            self.assertEqual(report['status'], 'ready', report['issues'])
            self.assertEqual(rows[0]['sku'], '00007')
            self.assertIn('USD', rows[0]['values'].values())
        finally:
            book.close()
