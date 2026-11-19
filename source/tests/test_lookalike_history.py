import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from template_manager import ai, lookalike, lookalike_history
from test_lookalike import Settings, PNG, FakeClient, item

OWNER = {'username': 'writer', 'role': 'editor'}
OTHER = {'username': 'other', 'role': 'editor'}
ADMIN = {'username': 'admin', 'role': 'admin'}
VIEWER = {'username': 'reader', 'role': 'viewer'}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'lookalike.sqlite3'
        self.service = lookalike.Service(Settings())
        self.history = lookalike_history.History(self.path, self.service)

    def queried(self, name='pen.png'):
        with patch.object(ai, 'request_json', return_value={'keywords': ['black pen'], 'description': '笔'}):
            data = self.service.keywords(OWNER, {'marketplace': 'AE', 'image': PNG, 'ai_consent': True})
        self.service.search(OWNER, FakeClient([{'items': [item('B000000001', '=Formula pen')]}]), {'token': data['token'], 'keywords': ['black pen']})
        return {'token': data['token'], 'name': name}

    def test_save_reopen_export_and_default_no_originals_or_tokens(self):
        entries = [self.queried('angle-1.png'), self.queried('angle-2.png')]
        saved = self.history.save(OWNER, {'title': 'Pens', 'entries': entries})
        self.assertEqual(len(saved['groups']), 2)
        self.assertFalse(saved['images_saved'])
        raw = self.path.read_bytes()
        self.assertNotIn(PNG.encode(), raw)
        for entry in entries:
            self.assertNotIn(entry['token'].encode(), raw)
        restored = lookalike_history.History(self.path, lookalike.Service(None)).get(VIEWER, saved['id'])
        self.assertFalse(restored['can_delete'])
        self.assertEqual(restored['groups'][0]['keywords'], ['black pen'])
        self.assertTrue(restored['groups'][0]['queried_at'])
        self.assertEqual(len(self.history.list(VIEWER)['history']), 1)
        for kind in ('csv', 'xlsx', 'json'):
            data, filename, _ = self.history.export(VIEWER, saved['id'], kind)
            self.assertTrue(data);self.assertTrue(filename.endswith('.'+kind))
            if kind == 'csv':
                self.assertIn("'=Formula pen", data.decode('utf-8'))
                self.assertIn('哪张图', data.decode('utf-8'))
        self.assertEqual(restored['groups'][1]['name'], 'angle-2.png')

    def test_originals_only_with_explicit_boolean_and_delete_permissions(self):
        entry = self.queried()
        for value in ('true', 1, None):
            with self.assertRaises(ValueError):
                self.history.save(OWNER, {'entries': [entry], 'save_images': value})
        saved = self.history.save(OWNER, {'entries': [entry], 'save_images': True})
        self.assertEqual(saved['groups'][0]['original_image']['data'], PNG)
        with self.assertRaises(PermissionError):
            self.history.delete(OTHER, {'id': saved['id'], 'confirmed': True})
        with self.assertRaises(ValueError):
            self.history.delete(OWNER, {'id': saved['id']})
        self.history.delete(ADMIN, {'id': saved['id'], 'confirmed': True})
        self.assertEqual(self.history.list(OWNER)['history'], [])

    def test_untrusted_client_cannot_supply_another_users_results_or_more_than_ten(self):
        entry = self.queried()
        with self.assertRaises(ValueError):
            self.history.save(OTHER, {'entries': [entry]})
        for entries in ([], [entry]*2, [entry]*11):
            with self.assertRaises(ValueError):
                self.history.save(OWNER, {'entries': entries})
        with self.assertRaises(ValueError):
            self.history.save(OWNER, {'entries': [{'token': 'expired', 'name': 'x.png'}]})
        with self.assertRaises(ValueError):
            self.history.save(VIEWER, {'entries': [entry]})

    def test_table_export_keeps_numbers_and_neutralises_formulas(self):
        import io as _io
        import zipfile as _zip
        data = lookalike_history.table({'rows': [['ASIN', 'price'], ['B000000001', 88.99], ['=HYPERLINK(1)', None]]})
        sheet = _zip.ZipFile(_io.BytesIO(data)).read('xl/worksheets/sheet1.xml').decode()
        self.assertIn('<v>88.99</v>', sheet);self.assertIn("'=HYPERLINK(1)", sheet)
        for body in ({'rows': []}, {'rows': [[{'x': 1}]]}, {'rows': [['a'] * 41]}, {'rows': [['a']] * 3001}):
            with self.assertRaises(ValueError):lookalike_history.table(body)

    def test_does_not_modify_template_database(self):
        path = Path(self.temp.name)/'templates.sqlite3'
        with sqlite3.connect(path) as connection:
            connection.execute('CREATE TABLE templates (id TEXT)')
        connection.close()
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            lookalike_history.History(path, self.service)
        self.assertEqual(before, path.read_bytes())
