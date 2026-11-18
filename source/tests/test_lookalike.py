"""Photo -> same product: keywords, catalog search, image comparison, seller offers."""
import base64
import json
import unittest
from unittest import mock

from template_manager import ai, lookalike, spapi

PNG = base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'0' * 100).decode()
EDITOR = {'username': 'writer', 'role': 'editor'}
OTHER = {'username': 'other', 'role': 'editor'}


class Settings:
    def __init__(self, provider='qwen'):
        self.values = provider, 'qwen3-vl-plus', 'test-key', ai.QWEN_REGIONS['cn']['url']
    def connection(self):return self.values


class Clock:
    def __init__(self):self.now = 0.0
    def __call__(self):return self.now


def item(asin, title, brand='Pilot', image=True):
    return {'asin': asin, 'summaries': [{'marketplaceId': 'A2VIGQ35RCS4UG', 'itemName': title, 'brand': brand, 'browseClassification': {'displayName': 'Gel Pens'}}],
            'images': [{'marketplaceId': 'A2VIGQ35RCS4UG', 'images': [{'variant': 'MAIN', 'link': 'https://m.media-amazon.com/images/I/%s.jpg' % asin}] if image else []}]}


class FakeClient:
    store_id, label = 'default', '主店铺'
    def __init__(self, replies):self.replies, self.calls = list(replies), []
    def call(self, method, path, query=None, body=None, live=False):
        self.calls.append((method, path, dict(query or {})))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):raise reply
        return reply


class Stores:
    def __init__(self, sellers):self.sellers = sellers
    def entries(self):return [{'id': k, 'name': v[1]} for k, v in self.sellers.items()]
    def client(self, store):
        seller = self.sellers[store][0]
        return type('C', (), {'require': lambda self: {'seller_id': seller}})()


class LookalikeTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.sleeps = Clock(), []
        self.service = lookalike.Service(Settings(), clock=self.clock, sleep=self.sleeps.append)

    def start(self):
        answer = {'description': '黑色中性笔', 'brand': 'Pilot', 'keywords': ['black gel pen', 'Black Gel Pen', 'retractable pen', 'bad,word', 'x']}
        with mock.patch.object(ai, 'request_json', return_value=answer) as sent:
            result = self.service.keywords(EDITOR, {'marketplace': 'AE', 'image': PNG, 'ai_consent': True})
        return result, sent

    def test_step1_reads_photo_and_cleans_keywords(self):
        result, sent = self.start()
        self.assertEqual(result['keywords'], ['black gel pen', 'retractable pen'])
        self.assertEqual(result['brand'], 'Pilot')
        images = sent.call_args.kwargs['images']
        self.assertTrue(images[0][1].startswith('data:image/png;base64,'))
        for body, message in (({'marketplace': 'AE', 'image': PNG}, '同意'), ({'marketplace': 'CA', 'image': PNG, 'ai_consent': True}, 'AE'),
                              ({'marketplace': 'AE', 'image': base64.b64encode(b'GIF89a...').decode(), 'ai_consent': True}, 'JPG'),
                              ({'marketplace': 'AE', 'image': 'not base64!', 'ai_consent': True}, '图片')):
            with self.assertRaises((ValueError, spapi.SpApiError)) as caught:
                self.service.keywords(EDITOR, body)
            self.assertIn(message, str(caught.exception))
        with self.assertRaises(PermissionError):
            self.service.keywords({'username': 'v', 'role': 'viewer'}, {})

    def test_step1_needs_a_vision_provider(self):
        service = lookalike.Service(Settings('deepseek'))
        with self.assertRaises(ValueError) as caught:
            service.keywords(EDITOR, {'marketplace': 'AE', 'image': PNG, 'ai_consent': True})
        self.assertIn('看图', str(caught.exception))

    def test_steps_2_to_4(self):
        result, _ = self.start()
        token = result['token']
        client = FakeClient([
            {'items': [item('B000000001', 'Pilot black gel pen 0.5'), item('B000000002', 'Blue ballpoint', 'Other')], 'pagination': {'nextToken': 'n1'}},
            {'items': [item('B000000003', 'Gel pen refill', image=False)]},
            {'items': [item('B000000001', 'Pilot black gel pen 0.5')]},
        ])
        found = self.service.search(EDITOR, client, {'token': token, 'keywords': ['black gel pen', 'retractable pen']})
        self.assertEqual(found['count'], 3)
        self.assertEqual(found['items'][0]['asin'], 'B000000001')
        self.assertEqual(found['items'][0]['found_by'], ['black gel pen', 'retractable pen'])
        self.assertEqual(client.calls[0][2]['keywords'], 'black,gel,pen')
        self.assertEqual(client.calls[1][2]['pageToken'], 'n1')
        notes = ' '.join(found['items'][0]['evidence'])
        self.assertIn('被 2 个核心词搜到', notes);self.assertIn('品牌与图片相同：Pilot', notes)
        self.assertIn('品牌不同', ' '.join(next(i for i in found['items'] if i['asin'] == 'B000000002')['evidence']))
        with self.assertRaises(ValueError):
            self.service.search(OTHER, client, {'token': token, 'keywords': ['pen']})

        reply = {'items': [{'asin': 'B000000001', 'verdict': 'same', 'similarity': 95, 'reason': '外形颜色一致'},
                           {'asin': 'B000000002', 'verdict': 'maybe', 'similarity': 50},
                           {'asin': 'B999999999', 'verdict': 'same', 'similarity': 99}]}
        with mock.patch.object(ai, 'request_json', return_value=reply) as sent:
            compared = self.service.compare(EDITOR, {'token': token, 'asins': ['B000000001', 'B000000002', 'B000000003'], 'ai_consent': True})
        rows = {r['asin']: r for r in compared['items']}
        self.assertEqual((rows['B000000001']['verdict'], rows['B000000001']['similarity']), ('same', 95))
        self.assertEqual(rows['B000000002']['verdict'], '');self.assertIn('没有给出有效结果', rows['B000000002']['ai_reason'])
        self.assertIn('没有主图', rows['B000000003']['ai_reason'])
        self.assertEqual(len(sent.call_args.kwargs['images']), 3)  # reference + 2 candidates with pictures

        offers = {'payload': {'Summary': {'TotalOfferCount': 3, 'BuyBoxPrices': [{'LandedPrice': {'Amount': 12.5, 'CurrencyCode': 'AED'}}]},
                              'Offers': [{'SellerId': 'AOTHER0001', 'ListingPrice': {'Amount': 12.0, 'CurrencyCode': 'AED'}, 'Shipping': {'Amount': 0.5, 'CurrencyCode': 'AED'}, 'IsBuyBoxWinner': True},
                                         {'SellerId': 'ASECOND01', 'ListingPrice': {'Amount': 11.0, 'CurrencyCode': 'AED'}, 'IsFulfilledByAmazon': True},
                                         {'SellerId': 'ABAD', 'ListingPrice': {'Amount': 'x'}}]}}
        client = FakeClient([offers, spapi.SpApiError('not found', 404)])
        priced = self.service.prices(EDITOR, client, Stores({'s1': ('ASECOND01', '二店')}), {'token': token, 'asins': ['B000000001', 'B000000002']})
        first = next(r for r in priced['items'] if r['asin'] == 'B000000001')['offers']
        self.assertEqual(first['count'], 3);self.assertEqual(first['lowest']['total'], 11.0);self.assertEqual(first['lowest']['own_store'], '二店')
        self.assertEqual(first['own_stores'], ['二店']);self.assertEqual(first['buy_box'], {'amount': 12.5, 'currency': 'AED'})
        self.assertEqual(len(first['list']), 2)
        self.assertIn('error', next(r for r in priced['items'] if r['asin'] == 'B000000002')['offers'])
        self.assertEqual(client.calls[0][1], '/products/pricing/v0/items/B000000001/offers')
        self.assertEqual(self.sleeps, [lookalike.OFFERS_INTERVAL])
        self.assertTrue(all(method == 'GET' for method, _, _ in client.calls))
        with self.assertRaises(spapi.SpApiError):
            self.service.prices(EDITOR, FakeClient([spapi.SpApiError('denied', 403)]), None, {'token': token, 'asins': ['B000000001']})

    def test_job_expires_and_is_bounded(self):
        result, _ = self.start()
        self.clock.now += lookalike.TOKEN_LIFE + 1
        with self.assertRaises(ValueError) as caught:
            self.service.search(EDITOR, FakeClient([]), {'token': result['token'], 'keywords': ['pen']})
        self.assertIn('过期', str(caught.exception))
        for _ in range(lookalike.MAX_JOBS + 3):
            self.start()
        self.assertEqual(len(self.service.jobs), lookalike.MAX_JOBS)

    def test_images_reach_the_model_after_the_text(self):
        captured = {}
        class Response:
            def __enter__(self):return self
            def __exit__(self, *a):return False
            def read(self, n):return json.dumps({'choices': [{'message': {'content': '{"keywords":["gel pen"]}'}}]}).encode()
        def opener(request, timeout=None):
            captured['body'] = json.loads(request.data);captured['timeout'] = timeout
            return Response()
        with mock.patch('urllib.request.urlopen', opener):
            ai.request_json(Settings(), 'system', {'a': 1}, images=[('ref', 'data:image/png;base64,AAAA')])
        content = captured['body']['messages'][1]['content']
        self.assertEqual(content[0]['type'], 'text');self.assertEqual(content[2]['image_url']['url'], 'data:image/png;base64,AAAA')
        self.assertEqual(captured['timeout'], 120)
        with self.assertRaises(ValueError):
            ai.request_json(Settings('deepseek'), 'system', {}, images=[('ref', 'data:x')])


if __name__ == '__main__':
    unittest.main()
