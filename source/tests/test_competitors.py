import csv
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

from template_manager import ai, bsr, competitors
from test_bsr import URL, card, page, FakeNetwork, Reply

EDITOR = {'username': 'alice', 'role': 'editor'}
OTHER = {'username': 'bob', 'role': 'editor'}
VIEWER = {'username': 'reader', 'role': 'viewer'}
ADMIN = {'username': 'root', 'role': 'admin'}
VALUE = 'B000TEST01'
SECOND = 'B000TEST02'


class FakeCollector:
    def __init__(self, categories=None, pages=None, hold=False, failure=None):
        self.categories = categories or [{'url': URL, 'label': 'Bags'}]
        self.pages = pages or [bsr.parse_page(page(), 'AE', URL)]
        self.calls = []
        self.entered, self.release = threading.Event(), threading.Event()
        if not hold:
            self.release.set()
        self.failure = failure
    def discover(self, market, asin, cancel=None, checkpoint=None):
        self.calls.append(('discover', market, asin))
        return self.categories
    def collect(self, market, url, cancel=None, checkpoint=None):
        self.calls.append(('collect', market, url))
        for p in self.pages:
            checkpoint(p)
        self.entered.set()
        self.release.wait(5)
        if self.failure:
            raise self.failure
        return {'pages_done': len(self.pages)}


class Settings:
    def __init__(self, provider='openai', model='gpt-4.1-mini', key='private-test-secret', endpoint=None):
        self.values = provider, model, key, endpoint or ai.PROVIDERS['openai']['url']
    def connection(self):
        return self.values


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'competitors.sqlite3'
        self.fake = FakeCollector()
        self.service = competitors.Service(self.path, collector=self.fake)
        self.services = [self.service]
    def tearDown(self):
        for service in self.services:
            if service._cancel:
                service._cancel.set()
            if isinstance(service.collector, FakeCollector):
                service.collector.release.set()
            if service._thread:
                service._thread.join(5)
                self.assertFalse(service._thread.is_alive())
        self.temp.cleanup()
    def imported(self, html=None, **extra):
        return self.service.import_html(EDITOR, {'marketplace': 'AE', 'source_url': URL, 'html': html or page(), 'keywords': ['bag'], **extra})
    def start(self, source=URL, **extra):
        return self.service.start(EDITOR, {'marketplace': 'AE', 'source': source, 'consent': True, **extra})
    def join(self):
        self.service._thread.join(5)
        self.assertFalse(self.service._thread.is_alive())

    def test_init_no_network_import_offline_and_public_contract(self):
        self.assertEqual(self.fake.calls, [])
        with patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')):
            data = self.imported()
            for kind in ('csv', 'xlsx', 'json'):
                self.service.export(EDITOR, data['id'], kind)
        self.assertEqual(data['status'], 'completed')
        self.assertEqual(data['pages_done'], 1)
        self.assertFalse(data['saved'])
        self.assertTrue(data['imported'])
        self.assertIn('未访问网络', data['message'])
        required = {'id', 'marketplace', 'target_asin', 'title', 'status', 'message', 'created_at', 'categories', 'selected_category', 'pages_done', 'items', 'saved', 'revision'}
        self.assertTrue(required <= data.keys())
        item = data['items'][0]
        self.assertTrue({'asin', 'title', 'url', 'price', 'currency', 'raw_price', 'observations', 'hits', 'keyword_hits', 'evidence', 'classification', 'note', 'score', 'ai_classification'} <= item.keys())
        self.assertEqual((item['classification'], item['ai_classification'], item['score']), ('pending', '', None))
        self.assertEqual(item['keyword_hits'], ['bag'])
        self.assertEqual(item['hits'], 1)
        self.assertEqual(self.service.list(EDITOR)['batches'], [])

    def test_import_accepts_target_brand_from_full_ui_payload(self):
        data = self.imported(target_title='Travel bag', target_brand='Example')
        self.assertEqual(data['target_brand'], 'Example')
        self.assertEqual(self.fake.calls, [])
        with self.assertRaises(ValueError):
            self.imported(target_brand=['invalid'])

    def test_dedup_observations_same_page_not_extra_hits_manual_retained(self):
        data = self.imported(page(card()+card()))
        data = self.service.review(EDITOR, {'id': data['id'], 'asin': VALUE, 'classification': 'direct', 'note': 'confirmed', 'revision': data['revision']})
        parsed = bsr.parse_page(page(card(rank=4)), 'AE', URL)
        self.service._merge_page(data, parsed)
        self.assertEqual(len(data['items']), 1)
        self.assertEqual(data['items'][0]['hits'], 1)
        parsed['page'] = 2
        self.service._merge_page(data, parsed)
        self.assertEqual(data['items'][0]['hits'], 2)
        self.assertEqual(data['items'][0]['classification'], 'direct')
        self.assertEqual(data['items'][0]['note'], 'confirmed')
        # Different categories are not collapsed into an overall "best rank".
        data['selected_category'] = ''
        data['pages'] = []
        parsed['category_url'] = URL.replace('123', '456')
        parsed['category'] = 'Accessories'
        self.service._merge_page(data, parsed)
        self.assertEqual(data['items'][0]['hits'], 3)
        self.assertNotIn('best_rank', data['items'][0])
        self.assertEqual({o['category_url'] for o in data['items'][0]['observations']}, {URL, URL.replace('123', '456')})

    def test_background_progress_one_active_cancel_and_partial_checkpoint(self):
        self.fake = FakeCollector(hold=True)
        self.service.collector = self.fake
        data = self.start()
        self.assertEqual(data['status'], 'running')
        self.assertTrue(self.fake.entered.wait(2))
        progress = self.service.get(EDITOR, data['id'])
        self.assertEqual((progress['pages_done'], len(progress['items'])), (1, 1))
        with self.assertRaises(ValueError):
            self.start()
        with self.assertRaises(ValueError):
            self.service.delete(EDITOR, {'id': data['id'], 'confirmed': True})
        with self.assertRaises(ValueError):
            self.service.cancel(OTHER, {'id': data['id']})
        cancelled = self.service.cancel(EDITOR, {'id': data['id']})
        self.assertEqual(cancelled['status'], 'cancelled')
        self.fake.release.set()
        self.join()
        result = self.service.get(EDITOR, data['id'])
        self.assertEqual((result['status'], len(result['items'])), ('cancelled', 1))
        self.assertIsNone(self.service._active)

    def test_category_selection_resumes_same_job_only_once(self):
        self.fake.categories = [{'url': URL, 'label': 'Bags'}, {'url': URL.replace('123', '456'), 'label': 'Cases'}]
        data = self.start(VALUE)
        self.join()
        waiting = self.service.get(EDITOR, data['id'])
        self.assertEqual(waiting['status'], 'awaiting_category')
        self.assertEqual(len(self.fake.calls), 1)
        for body in ({'id': data['id'], 'category_url': URL, 'consent': False}, {'id': data['id'], 'category_url': URL.replace('123', '999'), 'consent': True}):
            with self.assertRaises(ValueError):
                self.service.choose(EDITOR, body)
        chosen = self.service.choose(EDITOR, {'id': data['id'], 'category_url': URL, 'consent': True})
        self.assertEqual(chosen['id'], data['id'])
        self.join()
        self.assertEqual(self.service.get(EDITOR, data['id'])['status'], 'completed')
        with self.assertRaises(ValueError):
            self.service.choose(EDITOR, {'id': data['id'], 'category_url': URL, 'consent': True})
        self.assertEqual(sum(c[0] == 'collect' for c in self.fake.calls), 1)

    def test_single_category_auto_collect_and_direct_page_two_source(self):
        data = self.start(VALUE)
        self.join()
        self.assertEqual(self.service.get(EDITOR, data['id'])['status'], 'completed')
        self.assertEqual(self.fake.calls, [('discover', 'AE', VALUE), ('collect', 'AE', URL)])
        self.fake.calls = []
        self.fake.pages = [bsr.parse_page(page(card(rank=51)), 'AE', URL+'?pg=2')]
        data = self.start(URL+'?pg=2')
        self.join()
        self.assertEqual(self.fake.calls[0][2], URL+'?pg=2')
        self.assertEqual(self.service.get(EDITOR, data['id'])['items'][0]['observations'][0]['page'], 2)

    def test_network_block_and_error_are_not_empty_success(self):
        self.service.collector = bsr.Collector(opener=FakeNetwork(Reply('User-agent: *\nAllow: /'), Reply(page(next_page=True)), Reply('', 429)), sleep=lambda s: None)
        data = self.start()
        self.join()
        result = self.service.get(EDITOR, data['id'])
        self.assertEqual((result['status'], result['pages_done'], len(result['items'])), ('blocked', 1, 1))
        self.service.collector = FakeCollector(failure=RuntimeError('untrusted secret-message'))
        data = self.start()
        self.join()
        result = self.service.get(EDITOR, data['id'])
        self.assertEqual(result['status'], 'partial')
        self.assertNotIn('secret-message', result['message'])
        empty = FakeCollector()
        empty.pages = []
        self.service.collector = empty
        data = self.start()
        self.join()
        self.assertEqual(self.service.get(EDITOR, data['id'])['status'], 'failed')

    def test_saved_batches_shared_private_jobs_not_shared_and_permissions(self):
        data = self.imported()
        for user in (OTHER, VIEWER, ADMIN, None, {'username': 'x', 'role': 'owner'}):
            with self.assertRaises(ValueError) as private:
                self.service.get(user, data['id'])
            with self.assertRaises(ValueError) as absent:
                self.service.get(user, 'f'*32)
            self.assertEqual(str(private.exception), str(absent.exception))
        for method, body in ((self.service.start, {'marketplace': 'AE', 'source': URL, 'consent': True}), (self.service.import_html, {'marketplace': 'AE', 'source_url': URL, 'html': page()}), (self.service.review, {'id': data['id'], 'asin': VALUE, 'classification': 'direct', 'revision': 0}), (self.service.save, {'id': data['id']}), (self.service.delete, {'id': data['id'], 'confirmed': True}), (self.service.score, {'id': data['id'], 'ai_consent': True}), (self.service.cancel, {'id': data['id']})):
            with self.assertRaises(ValueError):
                method(VIEWER, body)
        saved = self.service.save(EDITOR, {'id': data['id']})
        self.assertTrue(saved['saved'])
        self.assertEqual(self.service.get(VIEWER, data['id'])['id'], data['id'])
        self.assertEqual(self.service.list(VIEWER, 'AE')['batches'][0]['count'], 1)
        self.assertEqual(self.service.list(VIEWER, 'SA')['batches'], [])
        self.service.export(VIEWER, data['id'], 'json')
        reviewed = self.service.review(OTHER, {'id': data['id'], 'asin': VALUE, 'classification': 'same_type', 'note': 'team review', 'revision': saved['revision']})
        self.assertEqual(reviewed['items'][0]['classification'], 'same_type')
        with self.assertRaises(ValueError):
            self.service.delete(VIEWER, {'id': data['id'], 'confirmed': True})
        self.assertEqual(self.service.delete(OTHER, {'id': data['id'], 'confirmed': True}), {'ok': True})

    def test_persistence_restart_interrupted_no_resume_separate_db(self):
        data = self.imported()
        saved = self.service.save(EDITOR, {'id': data['id']})
        second = self.imported()
        def simulate_active(c):
            current = self.service._load(c, EDITOR, second['id'])
            current['status'] = 'running'
            self.service._write(c, current)
        self.service._transaction(simulate_active)
        fake = FakeCollector()
        reopened = competitors.Service(self.path, collector=fake)
        self.services.append(reopened)
        self.assertEqual(fake.calls, [])
        self.assertEqual(reopened.get(EDITOR, saved['id'])['status'], 'completed')
        interrupted = reopened.get(EDITOR, second['id'])
        self.assertEqual(interrupted['status'], 'interrupted')
        self.assertEqual(len(interrupted['items']), 1)
        self.assertEqual(len(reopened.list(VIEWER)['batches']), 1)
        foreign = Path(self.temp.name)/'foreign.sqlite'
        c = sqlite3.connect(foreign)
        c.execute('CREATE TABLE important (value TEXT)')
        c.execute("INSERT INTO important VALUES ('keep')")
        c.commit()
        c.close()
        with self.assertRaises(ValueError):
            competitors.Service(foreign)
        c = sqlite3.connect(foreign)
        self.assertEqual(c.execute('SELECT * FROM important').fetchall(), [('keep',)])
        c.close()

    def test_optimistic_review_and_selection_validation(self):
        data = self.imported(page(card()+card(SECOND, 2)))
        for body in ({'id': data['id'], 'asins': []}, {'id': data['id'], 'asins': [VALUE, VALUE]}, {'id': data['id'], 'asins': ['B000OTHER1']}, {'id': data['id'], 'asins': [False]}, {'id': data['id'], 'asins': VALUE}):
            with self.assertRaises(ValueError):
                self.service.save(EDITOR, body)
        base = {'id': data['id'], 'asin': VALUE, 'classification': 'direct', 'revision': data['revision']}
        for bad in ({**base, 'revision': True}, {**base, 'revision': '0'}, {**base, 'classification': []}, {**base, 'note': 1}):
            with self.assertRaises(ValueError):
                self.service.review(EDITOR, bad)
        reviewed = self.service.review(EDITOR, base)
        with self.assertRaises(ValueError):
            self.service.review(EDITOR, base)
        saved = self.service.save(EDITOR, {'id': data['id'], 'asins': [VALUE]})
        self.assertEqual(len(saved['items']), 1)
        self.assertEqual(saved['items'][0]['observations'], data['items'][0]['observations'])
        self.assertEqual(saved['items'][0]['classification'], 'direct')
        for confirmed in (False, 1, 'true', None):
            with self.assertRaises(ValueError):
                self.service.delete(EDITOR, {'id': data['id'], 'confirmed': confirmed})

    def test_strict_inputs_no_network_side_effects(self):
        valid = {'marketplace': 'AE', 'source': URL, 'consent': True}
        for body in ([], None, {**valid, 'consent': 1}, {**valid, 'keywords': [str(n) for n in range(6)]}, {**valid, 'keywords': 'bag'}, {**valid, 'keywords': [True]}, {**valid, 'marketplace': []}, {**valid, 'source': 1}, {**valid, 'target_title': []}, {**valid, 'baseURL': 'https://evil.test'}):
            with self.assertRaises(ValueError):
                self.service.start(EDITOR, body)
        for body in ({'marketplace': 'AE', 'source_url': URL, 'html': '<html>unrecognized</html>'}, {'marketplace': 'AE', 'source_url': URL, 'html': 'x'*(bsr.MAX_HTML+1)}, {'marketplace': 'AE', 'source_url': 'http://127.0.0.1', 'html': page()}):
            with self.assertRaises(ValueError):
                self.service.import_html(EDITOR, body)
        self.assertEqual(self.fake.calls, [])

    def test_exports_formula_safety_valid_ooxml_and_portable_json(self):
        data = self.imported(page(card(title='=HYPERLINK(&quot;https://evil.test&quot;)')+card(SECOND, 2, title='Other')))
        data = self.service.review(EDITOR, {'id': data['id'], 'asin': VALUE, 'classification': 'direct', 'note': '\t=1+2', 'revision': data['revision']})
        csv_data, filename, mime = self.service.export(EDITOR, data['id'], 'csv')
        self.assertTrue(csv_data.startswith(b'\xef\xbb\xbf'))
        rows = list(csv.reader(io.StringIO(csv_data.decode('utf-8-sig'))))
        self.assertTrue(rows[1][2].startswith("'="))
        self.assertTrue(filename.endswith('.csv'))
        self.assertIn('text/csv', mime)
        for value in ('=1', '+1', '-1', '@SUM(A1)', '  =cmd', '\tcmd', '\rcommand', '\ncommand'):
            self.assertTrue(competitors._csv_cell(value).startswith("'"))
        raw, filename, mime = self.service.export(EDITOR, data['id'], 'xlsx')
        ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            self.assertIsNone(z.testzip())
            for path in z.namelist():
                ET.fromstring(z.read(path))
            workbook = ET.fromstring(z.read('xl/workbook.xml'))
            self.assertEqual([n.attrib['name'] for n in workbook.findall('s:sheets/s:sheet', ns)], ['概览', '全量候选', '直接竞品'])
            all_rows = ET.fromstring(z.read('xl/worksheets/sheet2.xml')).findall('s:sheetData/s:row', ns)
            direct_rows = ET.fromstring(z.read('xl/worksheets/sheet3.xml')).findall('s:sheetData/s:row', ns)
            self.assertEqual((len(all_rows), len(direct_rows)), (3, 2))
            for name in z.namelist():
                self.assertNotIn(b'<f>', z.read(name))
                self.assertNotIn(b'TargetMode="External"', z.read(name))
        raw, _, _ = self.service.export(EDITOR, data['id'], 'json')
        self.assertEqual(json.loads(raw), self.service.get(EDITOR, data['id']))
        with self.assertRaises(ValueError):
            self.service.export(OTHER, data['id'], 'json')

    def test_ai_consent_fixed_providers_suggestions_not_manual_truth(self):
        self.service.ai_settings = Settings()
        data = self.imported()
        data = self.service.review(EDITOR, {'id': data['id'], 'asin': VALUE, 'classification': 'accessory', 'note': 'manual', 'revision': data['revision']})
        for body in ({'id': data['id'], 'ai_consent': False}, {'id': data['id'], 'ai_consent': 1}, {'id': data['id'], 'ai_consent': True, 'baseURL': 'https://evil.test'}):
            with self.assertRaises(ValueError), patch.object(ai, 'request_json', side_effect=AssertionError('must not call')):
                self.service.score(EDITOR, body)
        answer = {'items': [{'asin': VALUE, 'score': 8.5, 'classification': 'direct', 'evidence_refs': ['0']}]}
        def completion(settings, system, user, max_tokens):
            # A separate reader can transact while the network call is in progress.
            c = sqlite3.connect(self.path, timeout=.1)
            c.execute('BEGIN IMMEDIATE')
            c.rollback()
            c.close()
            self.assertIn('不可信数据', system)
            self.assertEqual(user['products'][0]['asin'], VALUE)
            self.assertNotIn('html', json.dumps(user))
            return answer
        with patch.object(ai, 'request_json', side_effect=completion):
            result = self.service.score(EDITOR, {'id': data['id'], 'ai_consent': True})
        item = result['items'][0]
        self.assertEqual((item['score'], item['ai_classification'], item['classification'], item['note']), (8.5, 'direct', 'accessory', 'manual'))
        self.assertEqual(result['ai_summary']['completed'], 1)
        self.assertIn(item['evidence'][0], item['ai_note'])
        self.assertIn('待人工核对', item['ai_note'])
        self.service.ai_settings = Settings(endpoint='https://evil.test/v1/chat/completions')
        with self.assertRaises(ValueError), patch.object(ai, 'request_json', side_effect=AssertionError('must not call')):
            self.service.score(EDITOR, {'id': data['id'], 'ai_consent': True})

    def test_ai_partial_errors_missing_duplicate_unknown_bad_scores_and_refs(self):
        self.service.ai_settings = Settings()
        data = self.imported(page(card()+card(SECOND, 2)))
        good = {'asin': VALUE, 'score': 7, 'classification': 'same_type', 'evidence_refs': ['0']}
        bad_rows = [None, {'asin': SECOND, 'score': True, 'classification': 'direct', 'evidence_refs': ['0']}, {'asin': SECOND, 'score': 11, 'classification': 'direct', 'evidence_refs': ['0']}, {'asin': SECOND, 'score': float('nan'), 'classification': 'direct', 'evidence_refs': ['0']}, {'asin': SECOND, 'score': 4, 'classification': 'made_up', 'evidence_refs': ['0']}, {'asin': SECOND, 'score': 4, 'classification': 'direct', 'evidence_refs': ['999']}, {'asin': SECOND, 'score': 4, 'classification': 'direct', 'evidence_refs': [0]}, {'asin': SECOND, 'score': 4, 'classification': 'direct', 'evidence_refs': []}, {'asin': 'B000OTHER1', 'score': 4, 'classification': 'direct', 'evidence_refs': ['0']}]
        for bad in bad_rows:
            with self.subTest(bad=bad), patch.object(ai, 'request_json', return_value={'items': [good, bad]}):
                result = self.service.score(EDITOR, {'id': data['id'], 'ai_consent': True})
                self.assertEqual((result['ai_summary']['completed'], result['ai_summary']['failed']), (1, 1))
                self.assertTrue(result['ai_summary']['errors'])
                self.assertIsNone(result['items'][1]['score'])
        for answer in ({'items': [good, good]}, {'items': []}, {'items': 'bad'}, [], None):
            with patch.object(ai, 'request_json', return_value=answer):
                result = self.service.score(EDITOR, {'id': data['id'], 'ai_consent': True})
                self.assertEqual(result['ai_summary']['completed'], 0)
                self.assertTrue(result['ai_summary']['errors'])
        with patch.object(ai, 'request_json', side_effect=ValueError('private-test-secret')):
            result = self.service.score(EDITOR, {'id': data['id'], 'ai_consent': True})
            self.assertEqual(result['ai_summary']['completed'], 0)
            self.assertNotIn('private-test-secret', json.dumps(result))

    def test_ai_network_wait_keeps_reviews_and_rejects_duplicate_work(self):
        self.service.ai_settings = Settings()
        data = self.imported()
        second = self.imported()
        entered, release = threading.Event(), threading.Event()
        result, errors = [], []
        def completion(*args, **kwargs):
            entered.set()
            release.wait(5)
            return {'items': [{'asin': VALUE, 'score': 5, 'classification': 'same_type', 'evidence_refs': ['0']}]}
        def run():
            try:
                result.append(self.service.score(EDITOR, {'id': data['id'], 'ai_consent': True}))
            except Exception as error:
                errors.append(error)
        with patch.object(ai, 'request_json', side_effect=completion):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaises(ValueError):
                    self.service.score(EDITOR, {'id': second['id'], 'ai_consent': True})
                with self.assertRaises(ValueError):
                    self.start()
                with self.assertRaises(ValueError):
                    self.service.delete(EDITOR, {'id': data['id'], 'confirmed': True})
                revised = self.service.review(EDITOR, {'id': data['id'], 'asin': VALUE, 'classification': 'irrelevant', 'note': 'review during AI', 'revision': data['revision']})
            finally:
                release.set()
                worker.join(5)
                self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result[0]['items'][0]['classification'], 'irrelevant')
        self.assertEqual(result[0]['items'][0]['note'], 'review during AI')
        self.assertEqual(result[0]['items'][0]['ai_classification'], 'same_type')

    def test_ai_selection_limit_twenty(self):
        self.service.ai_settings = Settings()
        data = self.imported(page(''.join(card('B%09d' % i, i) for i in range(1, 22))))
        with self.assertRaises(ValueError), patch.object(ai, 'request_json', side_effect=AssertionError('must not call')):
            self.service.score(EDITOR, {'id': data['id'], 'ai_consent': True})
        values = [i['asin'] for i in data['items'][:20]]
        with patch.object(ai, 'request_json', return_value={'items': []}):
            result = self.service.score(EDITOR, {'id': data['id'], 'asins': values, 'ai_consent': True})
        self.assertEqual(result['ai_summary']['requested'], 20)


if __name__ == '__main__':
    unittest.main()
