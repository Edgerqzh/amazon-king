import tempfile
from pathlib import Path
import unittest

from template_manager import bsr, competitors
from test_bsr import URL, FakeNetwork, Reply, page

EDITOR = {'username': 'writer', 'role': 'editor'}


def search_card(asin='B000TEST01', title='Travel bag', sponsored=False):
    return ('<div data-component-type="s-search-result" data-asin="%s"><h2>%s</h2>'
            '<span class="a-price"><span class="a-offscreen">AED 39.99</span>'
            '<span class="a-price-whole">39.</span><span class="a-price-fraction">99</span></span>%s</div>') % (
                asin, title, '<span class="puis-sponsored-label-text">Sponsored</span>' if sponsored else '')


class SearchTests(unittest.TestCase):
    def test_parse_cards_prices_positions_and_ads(self):
        parsed = bsr.parse_search_page(search_card(sponsored=True) + search_card('B000TEST02'), 'AE', bsr.search_url('AE', 'bag', 2))
        self.assertEqual((parsed['keyword'], parsed['page']), ('bag', 2))
        self.assertEqual([(i['position'], i['price'], i['currency'], i['sponsored']) for i in parsed['items']],
                         [(1, '39.99', 'AED', True), (2, '39.99', 'AED', False)])

    def test_only_generated_search_urls(self):
        url = bsr.search_url('AE', 'black gel pen', 5)
        self.assertEqual(bsr.validate_url(url, 'AE', 'search'), url)
        for bad in (url+'&ref=x', url.replace('page=5', 'page=6'), url+'&k=other', url.replace('/s?', '/s/?'),
                    url.replace('www.amazon.ae', 'evil.test'), url.replace('https:', 'http:'), url+'#x', url.replace('page=5', 'page=05')):
            with self.assertRaises(ValueError):
                bsr.validate_url(bad, 'AE', 'search')

    def test_explicit_empty_result_and_shell_rejection(self):
        html = '<div data-component-type="s-search-results"><h2>No results for this query</h2></div>'
        fake = FakeNetwork(Reply('User-agent: *\nAllow: /'), Reply(html))
        seen = []
        result = fake.collector().search('AE', ['bag'], checkpoint=seen.append)
        self.assertEqual((result['count'], result['pages_done']), (0, 1))
        self.assertEqual(seen[0]['items'], [])
        with self.assertRaises(bsr.BsrError):
            bsr.parse_search_page('<html>empty shell</html>', 'AE', bsr.search_url('AE', 'bag'))

    def test_five_pages_per_keyword_and_pacing(self):
        fake = FakeNetwork(Reply('User-agent: *\nAllow: /'), *[Reply(search_card()) for _ in range(25)])
        seen = []
        result = fake.collector().search('AE', ['bag', 'case', 'travel', 'carry', 'luggage'], checkpoint=seen.append)
        self.assertEqual(result['pages_done'], 25)
        self.assertEqual(len(fake.requests), 26)
        self.assertTrue(all(b[3]-a[3] >= 5 for a, b in zip(fake.requests, fake.requests[1:])))

    def test_challenge_rate_limit_and_all_redirects_stop_preserving_pages(self):
        for reply in (Reply('Robot Check'), Reply('', 403), Reply('', 429),
                      Reply('', 302, {'Location': bsr.search_url('AE', 'bag', 3)}),
                      Reply('', 301, {'Location': '/s?k=other&page=1'}),
                      Reply(search_card(), url='https://evil.test')):
            fake = FakeNetwork(Reply('User-agent: *\nAllow: /'), Reply(search_card()), reply)
            seen = []
            with self.assertRaises(bsr.BsrError) as caught:
                fake.collector().search('AE', ['bag'], checkpoint=seen.append)
            self.assertEqual(caught.exception.status, 'blocked')
            self.assertEqual((len(seen), len(fake.requests)), (1, 3))

    def test_robots_denial_never_requests_search(self):
        fake = FakeNetwork(Reply('User-agent: *\nDisallow: /s'))
        with self.assertRaises(bsr.BsrError):
            fake.collector().search('AE', ['bag'])
        self.assertEqual(len(fake.requests), 1)

    def test_background_search_block_retains_bsr_and_search_checkpoints(self):
        fake = FakeNetwork(Reply('User-agent: *\nAllow: /'), Reply(page()),
                           Reply('User-agent: *\nAllow: /'), Reply(search_card('B000TEST02')), Reply('Robot Check'))
        with tempfile.TemporaryDirectory() as temp:
            service = competitors.Service(Path(temp)/'competitors.sqlite3', collector=fake.collector())
            batch = service.start(EDITOR, {'marketplace': 'AE', 'source': URL, 'keywords': ['bag'], 'consent': True})
            service._thread.join(5)
            self.assertFalse(service._thread.is_alive())
            result = service.get(EDITOR, batch['id'])
            self.assertEqual(result['status'], 'blocked')
            self.assertEqual({i['source'] for i in result['items']}, {'bsr', 'search'})
            self.assertEqual(result['pages_done'], 2)
            self.assertEqual(len(fake.requests), 5)

    def test_merge_unique_keywords_best_position_sources_and_export(self):
        with tempfile.TemporaryDirectory() as temp:
            service = competitors.Service(Path(temp)/'competitors.sqlite3')
            data = service.import_html(EDITOR, {'marketplace': 'AE', 'source_url': URL, 'html': page(), 'keywords': ['bag', 'case']})
            for word, number, html in [('bag', 2, search_card()), ('bag', 1, search_card()),
                                       ('case', 1, search_card('B000TEST02') + search_card())]:
                parsed = bsr.parse_search_page(html, 'AE', bsr.search_url('AE', word, number))
                service._merge_page(data, parsed)
                service._merge_page(data, parsed)
            first, second = data['items']
            self.assertEqual((len(data['items']), first['keyword_hit_count'], first['source']), (2, 2, 'both'))
            self.assertEqual(second['source'], 'search')
            self.assertEqual(first['search_keywords'], ['bag', 'case'])
            self.assertEqual((first['best_position']['page'], first['best_position']['position']), (1, 1))
            self.assertEqual(first['hits'], 4)
            self.assertEqual(data['pages_done'], 4)
            service._transaction(lambda c: service._write(c, data))
            csv, _, _ = service.export(EDITOR, data['id'], 'csv')
            self.assertIn('命中次数', csv.decode('utf-8'))
            self.assertIn('BSR 榜和搜索页', csv.decode('utf-8'))

    def test_candidate_cap_300_and_all_can_be_saved(self):
        with tempfile.TemporaryDirectory() as temp:
            service = competitors.Service(Path(temp)/'competitors.sqlite3')
            data = service.import_html(EDITOR, {'marketplace': 'AE', 'source_url': URL, 'html': page(), 'keywords': ['bag']})
            for number in range(1, 5):
                html = ''.join(search_card('B%09d' % ((number-1)*100+i)) for i in range(100))
                service._merge_page(data, bsr.parse_search_page(html, 'AE', bsr.search_url('AE', 'bag', number)))
            self.assertEqual(len(data['items']), 300)
            service._transaction(lambda c: service._write(c, data))
            saved = service.save(EDITOR, {'id': data['id']})
            self.assertEqual(len(saved['items']), 300)
