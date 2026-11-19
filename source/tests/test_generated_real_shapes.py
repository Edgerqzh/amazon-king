"""Regression tests on Amazon-shaped product type schemas: open enums, selector groups and identifiers."""
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
import zipfile

from template_manager import filling, generated_templates as generated, inventory, library, listings, schema_utils
import amazon_schema_shape as shape
from test_generated_templates import FakeClient, Lock, MARKET, USER, array

SAUDI = 'A17E79C6D8DWNP'
SELECT = '[marketplace_id=%s]' % MARKET


def keyed(fields):
    return {f['key']: f for f in fields}


class OpenEnumTests(unittest.TestCase):
    def test_open_enum_defs_and_values_become_plain_columns(self):
        schema = shape.realistic_schema()
        fields, _ = schema_utils.compile_fields(schema, MARKET)
        json_attributes = {f['json_attribute'] for f in fields if f['value_type'] == 'json'}
        # Only the genuinely unsupported shapes stay advanced JSON: an array of strings, and string-or-number branches.
        self.assertEqual(json_attributes, {'color', 'capacity_hint'})
        self.assertGreaterEqual(1 - len(json_attributes) / len(schema['properties']), 0.95)
        by = keyed(fields)
        for key in ('item_name' + SELECT + '#1.value', 'bullet_point' + SELECT + '#5.value', 'externally_assigned_product_identifier' + SELECT + '#1.value',
                    'externally_assigned_product_identifier' + SELECT + '#1.type', 'fulfillment_availability#1.quantity',
                    'purchasable_offer[currency=AED]' + SELECT + '#1.our_price#1.schedule#1.value_with_tax'):
            self.assertIn(key, by)
        # Two suggested languages: an editable, still required column with a non-binding list.
        language = by['item_name' + SELECT + '#1.language_tag']
        self.assertEqual(language['suggestions'], ['en_AE', 'ar_AE']);self.assertNotIn('options', language);self.assertTrue(language['required'])
        material = by['material' + SELECT + '#1.value']
        self.assertEqual((material['value_type'], material['suggestions']), ('string', ['leather', 'polyester']))
        self.assertEqual(by['batteries_required' + SELECT + '#1.value']['options'], ['false', 'true'])  # binding enums unchanged
        # The repository's earlier Amazon-shape fixture: nothing is forced into JSON any more.
        fields, _ = schema_utils.compile_fields(shape.schema(market=MARKET), MARKET)
        self.assertEqual([f['key'] for f in fields if f['value_type'] == 'json'], [])

    def test_single_suggestion_is_the_selector_and_marketplace_check_uses_suggestions(self):
        one_language = shape.realistic_schema(languages=('en_AE',), n_text=2, n_enum=1, n_conditions=2)
        keys = [f['key'] for f in schema_utils.compile_fields(one_language, MARKET)[0]]
        self.assertTrue(any(k.startswith('item_name[') and '[language_tag=en_AE]' in k and SELECT in k and k.endswith('#1.value') for k in keys))
        self.assertFalse(any(k.startswith('item_name') and k.endswith('.language_tag') for k in keys))
        # An AE definition compiled for SA: the suggested marketplace differs, so those attributes keep the JSON fallback.
        fields, warnings = schema_utils.compile_fields(shape.realistic_schema(n_text=2, n_enum=1, n_conditions=2), SAUDI)
        by = keyed(fields)
        self.assertIn('item_name::json', by);self.assertIn('fulfillment_availability#1.quantity', by)
        self.assertTrue(any('站点规则与所选站点不同' in warning for warning in warnings))

    def test_branches_that_really_constrain_keep_the_json_fallback(self):
        string = {'type': 'string'}
        cases = {'two_types': {'anyOf': [string, {'type': 'integer'}]},
                 'range': {'type': 'number', 'anyOf': [{'type': 'number'}, {'type': 'number', 'minimum': 5}]},
                 'length': {'anyOf': [{'type': 'string', 'maxLength': 20}, {'type': 'string', 'enum': ['a']}]},
                 'only_enums': {'anyOf': [{'type': 'string', 'enum': ['a']}, {'type': 'string', 'enum': ['b']}]},
                 'one_of': {'oneOf': [string, {'type': 'string', 'enum': ['a'], 'enumNames': ['A']}]}}
        properties = {name: array({'value': node}) for name, node in cases.items()}
        properties['requirement_sets'] = array({'value': string, 'other': string}, required=())
        properties['requirement_sets']['items']['anyOf'] = [{'required': ['value']}, {'required': ['other']}]
        fields, _ = schema_utils.compile_fields({'type': 'object', 'properties': properties}, MARKET)
        self.assertEqual(sorted(f['json_attribute'] for f in fields if f['value_type'] == 'json'), sorted(properties))

    def test_json_with_language_tag_validates_and_suggestions_never_reject(self):
        schema = shape.realistic_schema(n_text=2, n_enum=1, n_conditions=2)
        field = {'key': 'item_name::json', 'json_attribute': 'item_name', 'generated_format': schema_utils.GENERATED_FORMAT}
        for raw in ('[{"value":"Travel case","language_tag":"en_AE"}]', '[{"value":"Travel case","language_tag":"en_AE","marketplace_id":"%s"}]' % MARKET,
                    '[{"value":"Valise","language_tag":"fr_FR"}]'):
            with self.subTest(raw=raw):
                self.assertEqual(listings.convert({'item_name::json': raw}, [field], MARKET, schema)[1]['item_name'], json.loads(raw))
        for raw in ('[{"value":"Travel case"}]', '[{"value":"Travel case","language_tag":5}]'):  # language_tag is required, and a string
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):listings.convert({'item_name::json': raw}, [field], MARKET, schema)
        # A $ref whose target keeps real branches is checked branch by branch, as if it were written inline.
        refs = {'type': 'object', '$defs': {'size': {'anyOf': [{'type': 'string'}, {'type': 'integer'}]}},
                'properties': {'size': array({'value': {'$ref': '#/$defs/size'}})}}
        for value in ('XL', 3):
            schema_utils.validate_json_attribute([{'value': value}], refs['properties']['size'], refs)
        with self.assertRaises(ValueError):schema_utils.validate_json_attribute([{'value': [1]}], refs['properties']['size'], refs)

    def test_display_names_of_suggestions_still_map_to_codes(self):
        schema = shape.realistic_schema(n_text=2, n_enum=1, n_conditions=2)
        values = {'material' + SELECT + '#1.value': 'Leather', 'material' + SELECT + '#1.language_tag': 'Language ar_AE'}
        attributes = listings.convert(values, [], MARKET, schema)[1]
        self.assertEqual(attributes['material'], [{'marketplace_id': MARKET, 'value': 'leather', 'language_tag': 'ar_AE'}])
        # Free text outside the suggestions is valid and stays exactly as typed.
        attributes = listings.convert({'material' + SELECT + '#1.value': 'Recycled canvas'}, [], MARKET, schema)[1]
        self.assertEqual(attributes['material'][0]['value'], 'Recycled canvas')


class SelectorGroupTests(unittest.TestCase):
    def test_entries_differing_only_in_selectors_stay_separate_and_in_column_order(self):
        schema = shape.realistic_schema(n_text=2, n_enum=1, n_conditions=2)
        price = '#1.our_price#1.schedule#1.value_with_tax'
        values = {'item_name' + SELECT + '[language_tag=en_AE]#1.value': 'Travel case 20 inch',
                  'item_name' + SELECT + '[language_tag=ar_AE]#1.value': 'حقيبة سفر',
                  'bullet_point' + SELECT + '[language_tag=en_AE]#1.value': 'Light', 'bullet_point' + SELECT + '[language_tag=en_AE]#2.value': 'Strong',
                  'bullet_point' + SELECT + '[language_tag=ar_AE]#1.value': 'خفيفة',
                  'purchasable_offer' + SELECT + '[audience=ALL]' + price: '100', 'purchasable_offer' + SELECT + '[audience=B2B]' + price: '90',
                  # A column naming only some of a slot's selectors joins the first matching entry, as before.
                  'purchasable_offer' + SELECT + '#1.currency': 'AED'}
        _, attributes, skipped = listings.convert(values, [], MARKET, schema)
        self.assertEqual(skipped, [])
        self.assertEqual(attributes['item_name'], [{'marketplace_id': MARKET, 'language_tag': 'en_AE', 'value': 'Travel case 20 inch'},
                                                   {'marketplace_id': MARKET, 'language_tag': 'ar_AE', 'value': 'حقيبة سفر'}])
        self.assertEqual([(b['language_tag'], b['value']) for b in attributes['bullet_point']], [('en_AE', 'Light'), ('en_AE', 'Strong'), ('ar_AE', 'خفيفة')])
        offers = attributes['purchasable_offer']
        self.assertEqual([(o['audience'], o['our_price'][0]['schedule'][0]['value_with_tax']) for o in offers], [('ALL', 100), ('B2B', 90)])
        self.assertEqual(offers[0]['currency'], 'AED');self.assertNotIn('currency', offers[1])

    def test_conflicting_paths_are_a_row_error_not_a_crash(self):
        with self.assertRaises(ValueError):listings.convert({'x#1.a.b': '1', 'x#1.a#1.b': '2'}, [], MARKET, None)


class GeneratedFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'library'
        self.clock = [10]
        self.client = FakeClient(shape.realistic_schema())
        self.definitions = listings.Definitions(clock=lambda: self.clock[0])
        self.service = generated.Service(self.client, self.definitions, self.root, Lock(), clock=lambda: self.clock[0])

    def test_identifier_value_and_type_survive_fill_and_precheck_and_suggestions_do_not_block(self):
        preview = self.service.generate(USER, {'marketplace': 'AE', 'product_type': 'SUITCASE'})
        self.assertEqual(preview['json_count'], 2);self.assertLess(preview['field_count'], 512)
        saved = self.service.save(USER, {'token': preview['token'], 'shop': 'Shop A'})
        template = library.verified(self.root, saved)
        keys = [f['key'] for f in preview['fields']]
        identifier = 'externally_assigned_product_identifier' + SELECT + '#1.'
        values = {'contribution_sku': 'MY-00042', identifier + 'type': 'EAN', identifier + 'value': '6291041500213',
                  'item_name' + SELECT + '#1.value': 'Travel case', 'item_name' + SELECT + '#1.language_tag': 'en_AE',
                  'brand' + SELECT + '#1.value': 'BrandA', 'brand' + SELECT + '#1.language_tag': 'fr_FR'}
        # The language list is a suggestion in the workbook too: no stop error for other values.
        with zipfile.ZipFile(io.BytesIO(template)) as archive:
            sheet = archive.read('xl/worksheets/sheet1.xml').decode()
        stops = {m[1]: m[0] for m in re.findall(r'showErrorMessage="(\d)" errorStyle="stop" sqref="([A-Z]+)5:', sheet)}
        column = lambda key: generated._column(keys.index(key) + 1)
        self.assertEqual(stops[column('brand' + SELECT + '#1.language_tag')], '0');self.assertEqual(stops[column(identifier + 'type')], '1')
        source = generated._xlsx([('Products', [keys, [values.get(k, '') for k in keys]])])
        book, tpl, report, rows = filling.prepare(template, source, 'source.xlsx', 'AE', 'upload')
        self.addCleanup(book.close)
        labels = {f['key']: f['label'] for f in report['fields']}
        flagged = [i for i in report['issues'] if i['field'] == labels['brand' + SELECT + '#1.language_tag'] and '下拉' in i['message']]
        self.assertEqual(flagged, [])
        self.assertEqual(rows[0]['values'][identifier + 'type'], 'ean')
        _, attributes, skipped = listings.convert(rows[0]['values'], report['fields'], MARKET, self.client.schema)
        expected = [{'marketplace_id': MARKET, 'type': 'ean', 'value': '6291041500213'}]
        self.assertEqual(attributes['externally_assigned_product_identifier'], expected);self.assertEqual(skipped, [])
        self.assertEqual(attributes['brand'], [{'marketplace_id': MARKET, 'value': 'BrandA', 'language_tag': 'fr_FR'}])
        result = listings.precheck(self.client, self.definitions, rows, report['fields'], 'AE', 'upload')
        self.assertEqual(result['summary'], {'processed': 1, 'successful': 1})
        body = next(call for call in self.client.calls if call[0] == 'PUT')[3]
        self.assertEqual(body['attributes']['externally_assigned_product_identifier'], expected)
        self.assertEqual(body['attributes']['item_name'], [{'marketplace_id': MARKET, 'value': 'Travel case', 'language_tag': 'en_AE'}])


def _book(rows, name='Sheet1', extra=()):
    """Minimal xlsx with sparse inline cells: rows = [(row number, {column number: text})]."""
    sheets = [(name, rows)] + list(extra)
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        archive.writestr('xl/workbook.xml', '<workbook xmlns="%s" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>%s</sheets></workbook>'
                         % (generated.NS, ''.join('<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (n, i, i) for i, (n, _) in enumerate(sheets, 1))))
        archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">%s</Relationships>'
                         % ''.join('<Relationship Id="rId%d" Target="worksheets/sheet%d.xml"/>' % (i, i) for i in range(1, len(sheets) + 1)))
        for i, (_, data) in enumerate(sheets, 1):
            body = ''.join('<row r="%d">%s</row>' % (r, ''.join('<c r="%s%d" t="inlineStr"><is><t>%s</t></is></c>' % (generated._column(c), r, v) for c, v in cells.items())) for r, cells in data)
            archive.writestr('xl/worksheets/sheet%d.xml' % i, '<worksheet xmlns="%s"><sheetData>%s</sheetData></worksheet>' % (generated.NS, body))
    return out.getvalue()


class WideUploadTests(unittest.TestCase):
    """Generated tables can be up to 3000 columns and carry a manifest sheet; the upload reader must accept them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'library'
        self.client = FakeClient(shape.schema())
        self.service = generated.Service(self.client, listings.Definitions(clock=lambda: 10), self.root, Lock(), clock=lambda: 10)

    def test_wide_generated_workbook_round_trips_through_product_data_upload(self):
        preview = self.service.generate(USER, {'marketplace': 'AE', 'product_type': 'SUITCASE'})
        template = library.verified(self.root, self.service.save(USER, {'token': preview['token'], 'shop': 'Shop A'}))
        keys = [f['key'] for f in preview['fields']]
        self.assertGreater(len(keys), 800)
        last = keys[-1]
        values = {'contribution_sku': 'MY-00042', 'item_name' + SELECT + '#1.value': 'Travel case', last: 'far right'}
        # 1. The companion product-data sheet (machine keys in row 1), filled and uploaded on the fill page.
        source = generated._xlsx([('Products', [keys, [values.get(k, '') for k in keys]])])
        # 2. The generated workbook itself, filled from row 5 of the Template sheet; it still has the manifest sheet.
        with zipfile.ZipFile(io.BytesIO(template)) as archive:
            parts = {i.filename: archive.read(i.filename) for i in archive.infolist()}
            manifest_part = 'xl/worksheets/sheet%d.xml' % len(re.findall(r'<sheet ', parts['xl/workbook.xml'].decode()))
            self.assertGreater(max(len(t) for t in re.findall(r'<t xml:space="preserve">([^<]*)</t>', parts[manifest_part].decode())), 4000)
        filled_row = ''.join('<c r="%s5" t="inlineStr"><is><t>%s</t></is></c>' % (generated._column(keys.index(k) + 1), v) for k, v in values.items())
        parts['xl/worksheets/sheet1.xml'] = parts['xl/worksheets/sheet1.xml'].replace(b'</sheetData>', ('<row r="5">%s</row></sheetData>' % filled_row).encode())
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as archive:
            for name, data in parts.items():archive.writestr(name, data)
        filled = out.getvalue()
        for data, options in ((source, {}), (filled, {'sheet': 'Template', 'header_row': 3})):
            book, tpl, report, rows = filling.prepare(template, data, 'upload.xlsx', 'AE', 'upload', **options)
            self.addCleanup(book.close)
            # The saved template's manifest is still read whole (fields carry its format and JSON attribute names).
            self.assertEqual({f.get('generated_format') for f in report['fields']}, {schema_utils.GENERATED_FORMAT});self.assertEqual(len(report['fields']), len(keys))
            self.assertEqual(len(rows), 1);self.assertEqual(rows[0]['sku'], 'MY-00042');self.assertEqual(rows[0]['values'][last], 'far right')
            _, attributes, _ = listings.convert(rows[0]['values'], report['fields'], MARKET, self.client.schema)
            self.assertEqual(attributes['item_name'], [{'marketplace_id': MARKET, 'value': 'Travel case'}])
        # Other readers of uploads see the product sheets only, never the machine sheets.
        self.assertEqual([t['name'] for t in inventory.read_tables(filled, 'filled.xlsx')], ['Template', 'Read Me', 'Sources'])
        self.assertEqual(inventory.inspect(filled, 'filled.xlsx')['sheet'], 'Template')
        self.assertEqual(filling.inspect_source(filled, 'filled.xlsx')['sheet'], 'Template')

    def test_sheet_named_like_a_machine_sheet_is_still_read_without_a_manifest(self):
        data = _book([(1, {1: 'SKU', 600: 'Note'}), (2, {1: 'A-1', 600: 'x' * 3000})], name='Enums')
        self.assertEqual([(t['name'], len(t['rows'][0][1])) for t in inventory.read_tables(data, 'mine.xlsx')], [('Enums', 600)])
        text = ','.join(['SKU'] + ['c%d' % i for i in range(1, 900)]) + '\n' + ','.join(['A-1'] + [''] * 899) + '\n'
        self.assertEqual(len(inventory.read_tables(text.encode(), 'mine.csv')[0]['rows'][0][1]), 900)

    def test_oversized_dense_sheet_is_refused_with_a_split_message(self):
        # Few real cells (well under MAX_CELLS) but each row reaches column 2048: 2500 x 2048 padded cells.
        data = _book([(r, {1: 'A-%d' % r, 2048: 'x'}) for r in range(1, 2501)])
        with self.assertRaisesRegex(ValueError, '商品行数 × 列数超过 500 万格，请按商品行或列拆分'):
            inventory.read_tables(data, 'wide.xlsx')
        with self.assertRaisesRegex(ValueError, '最多支持 3000 列'):
            inventory.read_tables(_book([(1, {3001: 'x'})]), 'wider.xlsx')


if __name__ == '__main__':unittest.main()
