import io
import threading
import unittest
import urllib.error
from unittest.mock import patch

from template_manager import bsr

URL = 'https://www.amazon.ae/gp/bestsellers/home/123'


def card(value='B000TEST01', rank=1, title='Travel bag', price=True, modern=True):
    cls = '_cDEzb_grid-cell_1uMOS' if modern else 'zg-item-immersion'
    return ('<div class="%s"><span class="zg-bdg-text">#%s</span>'
            '<a href="/dp/%s/ref=abc"><img alt="%s" src="https://evil.test/no-fetch.jpg"></a>'
            '<a href="/gp/product/%s"><div class="_cDEzb_p13n-sc-css-line-clamp-3_abc">%s</div></a>%s</div>') % (
                cls, rank, value, title, value, title,
                '<span class="a-price"><span class="a-offscreen">AED 1,249.05</span><span class="a-price-symbol">AED</span><span class="a-price-whole">1,249.</span><span class="a-price-fraction">05</span></span>' if price else '')


def page(cards=None, next_page=False):
    return '<html><h1>Travel &amp; Bags</h1><div id="zg-grid-ctr">' + (cards if cards is not None else card()) + '</div>' + ('<ul class="a-pagination"><li class="a-last"><a href="' + URL + '?pg=2">Next</a></li></ul>' if next_page else '') + '</html>'


class Reply:
    def __init__(self, body='', status=200, headers=None, url=None):
        self.status, self.headers = status, headers or {}
        self.stream = io.BytesIO(body.encode() if isinstance(body, str) else body)
        self.url, self.closed = url, False
    def read(self, size=-1):
        return self.stream.read(size)
    def close(self):
        self.closed = True
    def geturl(self):
        return self.url


class FakeNetwork:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []
        self.time, self.sleeps = 0., []
    def clock(self):
        return self.time
    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.time += seconds
    def __call__(self, request, timeout=None):
        self.requests.append((request.full_url, dict(request.header_items()), timeout, self.time))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if reply.url is None:
            reply.url = request.full_url
        return reply
    def collector(self):
        return bsr.Collector(opener=self, clock=self.clock, sleep=self.sleep)


class UrlTests(unittest.TestCase):
    def test_restricts_host_scheme_port_path_and_query(self):
        self.assertEqual(bsr.validate_url('https://amazon.ae/gp/bestsellers/home/123/ref=abc?ref_=x&pg=2', 'AE'), URL + '?pg=2')
        self.assertEqual(bsr.validate_url('/Bag/dp/B000TEST01/ref=xx', 'AE', 'product', URL), 'https://www.amazon.ae/dp/B000TEST01')
        for url in ('http://www.amazon.ae/gp/bestsellers', 'https://www.amazon.sa/gp/bestsellers', 'https://amazon.ae.evil.test/gp/bestsellers', 'https://127.0.0.1/gp/bestsellers', 'https://[::1]/gp/bestsellers', 'https://user@amazon.ae/gp/bestsellers', 'https://amazon.ae:444/gp/bestsellers', 'https://amazon.ae/s?k=bag', 'https://amazon.ae/gp/bestsellers/../admin', 'https://amazon.ae/gp/bestsellers/%2e%2e/admin', 'https://amazon.ae/gp/bestsellers?url=https://evil.test', 'https://amazon.ae/gp/bestsellers?pg=3', 'https://amazon.ae/gp/bestsellers?pg=2&pg=1', 'https://amazon.ae/gp/bestsellers#x', 'https://amazon.ae\\@evil.test/gp/bestsellers', '//amazon.ae/gp/bestsellers', 'https://amazon.ae/gp/bestsellers\n'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                bsr.validate_url(url, 'AE')
        for market in (None, [], 'CA', True):
            with self.assertRaises(ValueError):
                bsr.validate_url(URL, market)

    def test_no_ambient_proxy_or_auto_redirect(self):
        collector = bsr.Collector()
        handlers = collector.opener.__self__.handlers
        self.assertTrue(any(isinstance(h, bsr._NoRedirect) for h in handlers))
        self.assertFalse(any(isinstance(h, __import__('urllib.request', fromlist=['ProxyHandler']).ProxyHandler) and h.proxies for h in handlers))


class ParserTests(unittest.TestCase):
    def test_modern_legacy_rank_title_and_exact_price(self):
        for modern in (True, False):
            parsed = bsr.parse_page(page(card(modern=modern)), 'AE', URL)
            item = parsed['items'][0]
            self.assertEqual((item['asin'], item['title'], item['rank'], item['price'], item['currency'], item['raw_price']), ('B000TEST01', 'Travel bag', 1, '1249.05', 'AED', 'AED 1,249.05'))
            self.assertNotIn('image', item)
            self.assertEqual(parsed['category'], 'Travel & Bags')

    def test_missing_price_unknown_currency_and_arabic(self):
        html = page(card(price=False).replace('Travel bag', 'حقيبة سفر'))
        item = bsr.parse_page(html, 'AE', URL)['items'][0]
        self.assertEqual((item['price'], item['currency'], item['raw_price']), ('', '', ''))
        html = page(card()).replace('AED 1,249.05', 'SAR 1,249.05').replace('>AED<', '>SAR<')
        item = bsr.parse_page(html, 'SA', URL.replace('amazon.ae', 'amazon.sa'))['items'][0]
        self.assertEqual(item['currency'], 'SAR')
        html = page(card()).replace('AED', '$')
        self.assertEqual(bsr.parse_page(html, 'AE', URL)['items'][0]['currency'], '')
        html = page(card()).replace('1,249.', '١٬٢٤٩٫').replace('>05<', '>٠٥<')
        self.assertEqual(bsr.parse_page(html, 'AE', URL)['items'][0]['price'], '1249.05')

    def test_no_inferred_rank_or_unvalidated_asin(self):
        for html in ('<html>login</html>', page(''), page(card().replace('zg-bdg-text', 'unrelated')), page(card().replace('/dp/B000TEST01', 'https://evil.test/dp/B000TEST01').replace('/gp/product/B000TEST01', '/s?k=x')), page(card().replace('B000TEST01', 'invalid'))):
            with self.assertRaises(bsr.BsrError):
                bsr.parse_page(html, 'AE', URL)
        wrapped = '<div id="zg-grid-ctr"><div id="gridItemRoot">' + card() + '</div></div>'
        self.assertEqual(len(bsr.parse_page(wrapped, 'AE', URL)['items']), 1)

    def test_real_pagination_only_same_category_and_at_most_two(self):
        parsed = bsr.parse_page(page(next_page=True), 'AE', URL)
        self.assertEqual(parsed['next_url'], URL + '?pg=2')
        self.assertEqual(bsr.parse_page(page(next_page=True), 'AE', URL+'?pg=2')['next_url'], '')
        for html in (page() + '<a href="' + URL + '?pg=2">Next</a>', page(next_page=True).replace('/123?pg=2', '/456?pg=2'), page(next_page=True).replace('a-last', 'a-last a-disabled'), page(next_page=True).replace('www.amazon.ae/gp', 'evil.test/gp')):
            self.assertEqual(bsr.parse_page(html, 'AE', URL)['next_url'], '')

    def test_discovery_only_sales_rank_not_unrelated_links(self):
        html = '<a href="/gp/bestsellers/unrelated/999">Other</a><table id="productDetails_detailBullets_sections1"><tr><th>Manufacturer</th><td><a href="/gp/bestsellers/no/888">No</a></td></tr><tr><th>Best Sellers Rank</th><td>#5 in <a href="/gp/bestsellers/home/123">Bags</a><a href="/gp/bestsellers/home/456">Cases</a></td></tr></table>'
        found = bsr.discover_categories(html, 'AE', bsr.product_url('AE', 'B000TEST01'))
        self.assertEqual([v['label'] for v in found], ['Bags', 'Cases'])
        self.assertEqual(bsr.discover_categories('<li id="SalesRank">#1 <a href="/gp/bestsellers/home/123">Bags</a></li>', 'AE', bsr.product_url('AE', 'B000TEST01'))[0]['url'], URL)
        with self.assertRaises(bsr.BsrError):
            bsr.discover_categories('<table id="productDetails"><a href="/gp/bestsellers/home/123">Recommendation</a></table>', 'AE', bsr.product_url('AE', 'B000TEST01'))

    def test_challenges_and_structural_bounds(self):
        for text in ('<title>Robot Check</title>', '<form action="/errors/validateCaptcha">', 'Type the characters you see', '<script src="/challenge-platform/x"></script>', '<meta http-equiv="refresh" content="5"><div id="bm-verify"></div>'):
            with self.assertRaises(bsr.BsrError) as caught:
                bsr.parse_page(text, 'AE', URL)
            self.assertEqual(caught.exception.status, 'blocked')
        for text in ('x' * (bsr.MAX_HTML+1), '<div>'*130, '<br>'*60001):
            with self.assertRaises(bsr.BsrError):
                bsr.parse_page(text, 'AE', URL)


class NetworkTests(unittest.TestCase):
    def test_polite_requests_progress_two_pages_and_headers(self):
        fake = FakeNetwork(Reply('User-agent: *\nAllow: /'), Reply(page(next_page=True)), Reply(page(card(rank=51), next_page=True)))
        progress = []
        result = fake.collector().collect('AE', URL, checkpoint=progress.append)
        self.assertEqual(result['pages_done'], 2)
        self.assertEqual([p['page'] for p in progress], [1, 2])
        self.assertEqual(len(fake.requests), 3)
        self.assertTrue(all(b[3]-a[3] >= 5 for a, b in zip(fake.requests, fake.requests[1:])))
        for _, headers, timeout, _ in fake.requests:
            self.assertNotIn('Cookie', headers)
            self.assertNotIn('Authorization', headers)
            self.assertEqual(timeout, 12)

    def test_robots_fail_closed_no_product_request(self):
        for reply in (Reply('User-agent: *\nDisallow: /gp/'), Reply('', 404), Reply('<html>blank</html>'), Reply('User-agent: *\nCrawl-delay: 10'), urllib.error.URLError('offline')):
            fake = FakeNetwork(reply)
            with self.assertRaises(bsr.BsrError) as caught:
                fake.collector().collect('AE', URL)
            self.assertEqual(caught.exception.status, 'blocked')
            self.assertEqual(len(fake.requests), 1)

    def test_redirects_validate_each_hop_and_robots(self):
        for location in ('https://evil.test/x', 'https://www.amazon.sa/gp/bestsellers/home/123', 'http://www.amazon.ae/gp/bestsellers/home/123', '/ap/signin', '/gp/bestsellers/../admin', URL):
            fake = FakeNetwork(Reply('User-agent: *\nAllow: /'), Reply('', 302, {'Location': location}))
            with self.assertRaises(bsr.BsrError) as caught:
                fake.collector().collect('AE', URL)
            self.assertEqual(caught.exception.status, 'blocked')
            self.assertEqual(len(fake.requests), 2)
        # Canonical host redirects remain eligible; no automatic redirect following.
        fake = FakeNetwork(Reply('User-agent: *\nDisallow: /gp/bestsellers/private'), Reply('', 302, {'Location': '/gp/bestsellers/private'}))
        with self.assertRaises(bsr.BsrError):
            fake.collector().collect('AE', URL)
        self.assertEqual(len(fake.requests), 2)
        fake = FakeNetwork(Reply('User-agent: *\nAllow: /'), Reply('', 302, {'Location': URL+'?pg=2'}), Reply(page(card(rank=51))))
        seen = []
        fake.collector().collect('AE', URL, checkpoint=seen.append)
        self.assertEqual(seen[0]['page'], 2)

    def test_stop_on_403_429_challenge_and_preserve_checkpoint(self):
        for response in (Reply('', 403), Reply('', 429), Reply('<html>Robot Check</html>')):
            fake = FakeNetwork(Reply('User-agent: *\nAllow: /'), Reply(page(next_page=True)), response)
            seen = []
            with self.assertRaises(bsr.BsrError) as caught:
                fake.collector().collect('AE', URL, checkpoint=seen.append)
            self.assertEqual(caught.exception.status, 'blocked')
            self.assertEqual(len(seen), 1)
            self.assertEqual(len(fake.requests), 3)

    def test_request_and_total_time_budgets_are_bounded(self):
        fake = FakeNetwork(Reply('User-agent: *\nAllow: /'), *[Reply('', 302, {'Location': URL+'?pg='+('2' if n % 2 == 0 else '1')}) for n in range(4)])
        with self.assertRaises(bsr.BsrError):
            fake.collector().collect('AE', URL)
        self.assertLessEqual(len(fake.requests), 5)
        fake = FakeNetwork()
        collector = fake.collector()
        session = bsr._Session(collector, 'AE', None, None)
        session.requests = 12
        with self.assertRaises(bsr.BsrError):
            session.fetch(URL)
        self.assertEqual(fake.requests, [])
        session.requests = 0
        fake.time = 151
        with self.assertRaises(bsr.BsrError):
            session.fetch(URL)
        self.assertEqual(fake.requests, [])

    def test_discovery_does_not_accept_changed_target_asin(self):
        product = bsr.product_url('AE', 'B000TEST01')
        other = bsr.product_url('AE', 'B000TEST02')
        html = '<li id="SalesRank">#1 <a href="/gp/bestsellers/home/123">Bags</a></li>'
        fake = FakeNetwork(Reply('User-agent: *\nAllow: /'), Reply('', 302, {'Location': other}), Reply(html))
        with self.assertRaises(bsr.BsrError) as caught:
            fake.collector().discover('AE', 'B000TEST01')
        self.assertEqual(caught.exception.status, 'blocked')

    def test_maximum_candidates_and_no_unrecognized_remote_page_success(self):
        fake = FakeNetwork(Reply('User-agent: *\nAllow: /'), Reply(page(''.join(card('B%09d' % i, i) for i in range(1, 101)), next_page=True)))
        seen = []
        result = fake.collector().collect('AE', URL, checkpoint=seen.append)
        self.assertEqual(result['count'], 100)
        self.assertEqual(len(fake.requests), 2)
        fake = FakeNetwork(Reply('User-agent: *\nAllow: /'), Reply('<html>empty shell</html>'))
        with self.assertRaises(bsr.BsrError):
            fake.collector().collect('AE', URL)

    def test_response_caps_cancel_and_no_silent_redirect(self):
        for response in (Reply(b'x'*(bsr.MAX_HTML+1)), Reply('', headers={'Content-Length': str(bsr.MAX_HTML+1)}), Reply(page(), url='https://evil.test/'), Reply(page(), headers={'Content-Encoding': 'gzip'})):
            fake = FakeNetwork(Reply('User-agent: *\nAllow: /'), response)
            with self.assertRaises(bsr.BsrError):
                fake.collector().collect('AE', URL)
            self.assertTrue(response.closed)
        cancel = threading.Event()
        cancel.set()
        fake = FakeNetwork()
        with self.assertRaises(bsr.BsrError) as caught:
            fake.collector().collect('AE', URL, cancel=cancel)
        self.assertEqual(caught.exception.status, 'cancelled')
        self.assertEqual(fake.requests, [])


if __name__ == '__main__':
    unittest.main()
