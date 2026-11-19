"""Offline schema-generation contract and end-to-end confirmed listing tests."""
import base64
import copy
from contextlib import contextmanager
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from template_manager import filling, generated_templates as generated, library, listings, metadata, operations, schema_utils, spapi

MARKET = 'A2VIGQ35RCS4UG'
USER = {'username': 'writer', 'role': 'editor'}


def array(properties, required=('value',), minimum=1, maximum=1):
    return {'type': 'array', 'minItems': minimum, 'maxItems': maximum,
            'items': {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}}


def fixture_schema():
    return {'type': 'object', '$defs': {'text': {'type': 'string'}, 'count': {'type': 'integer'}, 'flag': {'type': 'boolean'}},
            'properties': {
                'item_name': array({'value': {'$ref': '#/$defs/text', 'title': 'Item Name'}, 'marketplace_id': {'type': 'string', 'enum': [MARKET]}, 'language_tag': {'type': 'string', 'enum': ['en_AE']}}),
                'brand': array({'value': {'type': 'string', 'title': 'Brand', 'enum': ['BrandA', '=NOT_A_FORMULA']}, 'marketplace_id': {'type': 'string'}}),
                'bullet_point': array({'value': {'type': 'string', 'title': 'Bullet Point'}}, maximum=5),
                'number_of_items': array({'value': {'$ref': '#/$defs/count', 'title': 'Number of Items'}}),
                'batteries_required': array({'value': {'$ref': '#/$defs/flag', 'default': True}}),
                'main_product_image_locator': array({'media_location': {'type': 'string', 'title': 'Main Image'}}, required=('media_location',)),
                'compliance': {'type': 'object', 'properties': {'approved': {'type': 'boolean'}, 'count': {'type': 'integer'}}, 'required': ['approved', 'count'], 'additionalProperties': False},
            }, 'required': ['item_name', 'brand', 'compliance'],
            'allOf': [{'if': {'properties': {'batteries_required': {'const': True}}}, 'then': {'required': ['number_of_items']}}]}


class FakeClient:
    def __init__(self, schema=None):
        self.seller = 'ATEST001'
        self.schema = schema or fixture_schema()
        self.calls, self.downloads = [], []
        self.error = None
        self.meta_change = None
        self.raw_change = None
        self.reply = {'status': 'VALID', 'issues': []}

    def seller_id(self):return self.seller
    def public(self):return {'seller_hint': '…ST001'}
    def raw(self):return json.dumps(self.schema, ensure_ascii=False, separators=(',', ':')).encode()
    def call(self, method, path, query=None, body=None, live=False):
        self.calls.append((method, path, copy.deepcopy(query), copy.deepcopy(body), live))
        if self.error:raise self.error
        if path == '/definitions/2020-09-01/productTypes':
            return {'productTypes': [{'name': 'SUITCASE', 'displayName': 'Suitcases', 'marketplaceIds': [MARKET]}], 'productTypeVersion': 'v1'}
        if path.startswith('/definitions/'):
            meta = {'productType': 'SUITCASE', 'displayName': 'Suitcases', 'marketplaceIds': [MARKET], 'requirements': 'LISTING', 'requirementsEnforced': 'ENFORCED',
                    'productTypeVersion': {'version': 'version-1', 'latest': True, 'releaseCandidate': False},
                    'schema': {'checksum': base64.b64encode(hashlib.md5(self.raw()).digest()).decode(),
                               'link': {'resource': 'https://definitions.s3.amazonaws.com/schema.json?secret-signed-query=NEVER-IN-ARTIFACTS', 'verb': 'GET'}}}
            if self.meta_change:self.meta_change(meta)
            return meta
        if method == 'GET':raise spapi.SpApiError('not found', 404)
        return {'status': 'ACCEPTED', 'submissionId': 'fixture-only', 'issues': []} if live else copy.deepcopy(self.reply)
    def fetch_raw(self, url):
        self.downloads.append(url)
        return self.raw_change if self.raw_change is not None else self.raw()
    def fetch_document(self, url):return json.loads(self.fetch_raw(url))


class Lock:
    def __init__(self):self.active = False;self.entries = 0
    @contextmanager
    def exclusive(self):
        self.active = True;self.entries += 1
        try:yield
        finally:self.active = False


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'library'
        self.clock = [10]
        self.client = FakeClient()
        self.definitions = listings.Definitions(clock=lambda: self.clock[0])
        self.lock = Lock()
        self.service = generated.Service(self.client, self.definitions, self.root, self.lock, clock=lambda: self.clock[0])
    def generate(self):return self.service.generate(USER, {'marketplace': 'AE', 'product_type': 'SUITCASE'})
    def artifact(self, token, kind='template'):return self.service.artifact(USER, {'token': token, 'kind': kind})

    def test_search_exact_official_exclusivity_caps_and_permissions(self):
        for mode in ('keywords', 'item_name'):
            result = self.service.search(USER, {'marketplace': 'AE', 'query': '旅行,箱包', 'mode': mode, 'search_locale': 'zh_CN'})
            params = self.client.calls[-1][2]
            self.assertEqual(params['searchLocale'], 'zh_CN');self.assertEqual(params['locale'], 'en_US')
            self.assertEqual(set(params) & {'keywords', 'itemName'}, {'keywords' if mode == 'keywords' else 'itemName'})
            self.assertEqual(result['items'], [{'name': 'SUITCASE', 'display_name': 'Suitcases'}]);self.assertFalse(result['truncated'])
        for body in ({'query': ''}, {'query': 'x'*201}, {'query': 'x', 'mode': 'bad'}, {'query': 'x', 'search_locale': 'ar_AE'}):
            with self.assertRaises(ValueError):self.service.search(USER, {'marketplace': 'AE', **body})
        with self.assertRaises(PermissionError):self.service.search({'username': 'v', 'role': 'viewer'}, {'marketplace': 'AE', 'query': 'case'})
        self.client.error = spapi.SpApiError('denied', 403)
        with self.assertRaises(spapi.SpApiError) as caught:self.service.search(USER, {'marketplace': 'AE', 'query': 'case'})
        self.assertEqual(caught.exception.status, 403)

    def test_search_truncation_and_market_filtering(self):
        original = self.client.call
        def call(*args, **kwargs):
            return {'productTypes': [{'name': 'TYPE_'+str(i), 'displayName': 'Type', 'marketplaceIds': [MARKET]} for i in range(105)] + [{'name': 'OTHER', 'marketplaceIds': ['wrong']} ]}
        self.client.call = call
        result = self.service.search(USER, {'marketplace': 'AE', 'query': 'case'})
        self.assertEqual(len(result['items']), 100);self.assertTrue(result['truncated'])
        self.client.call = original

    def test_force_refresh_ttl_and_legacy_minimal_definition(self):
        first = self.definitions.definition(self.client, 'SUITCASE', 'AE', strict=True)
        self.assertIs(self.definitions.definition(self.client, 'SUITCASE', 'AE'), first)
        self.assertEqual(len(self.client.calls), 1)
        self.clock[0] += 601
        self.definitions.definition(self.client, 'SUITCASE', 'AE')
        self.assertEqual(len(self.client.calls), 2)
        self.definitions.definition(self.client, 'SUITCASE', 'AE', force=True)
        self.assertEqual(len(self.client.calls), 3)
        self.generate();self.generate();self.assertEqual(len(self.client.calls), 5)
        params = self.client.calls[-1][2]
        self.assertEqual((params['productTypeVersion'], params['requirements'], params['requirementsEnforced'], params['sellerId']), ('LATEST', 'LISTING', 'ENFORCED', 'ATEST001'))
        self.assertIn(('ATEST001', 'AE', 'SUITCASE'), self.definitions.cache)
        self.client.meta_change = lambda m: m.clear() or m.update({'schema': {'link': {'resource': 'https://definitions.s3.amazonaws.com/schema.json'}}})
        self.assertIsInstance(self.definitions.schema(self.client, 'SUITCASE', 'AE'), dict)
        self.definitions.definition(self.client, 'SUITCASE', 'AE', force=True)
        with self.assertRaises(spapi.SpApiError):self.generate()

    def test_generation_requires_strict_checksum_and_actual_context(self):
        changes = [lambda m:m.update(productType='OTHER'), lambda m:m.update(marketplaceIds=['wrong']), lambda m:m.update(requirements='LISTING_OFFER_ONLY'),
                   lambda m:m.update(requirementsEnforced='NOT_ENFORCED'), lambda m:m['productTypeVersion'].update(latest=False),
                   lambda m:m['productTypeVersion'].pop('version'), lambda m:m['schema'].pop('checksum'), lambda m:m['schema']['link'].update(verb='POST'),
                   lambda m:m['schema'].update(checksum='not base64')]
        for change in changes:
            with self.subTest(change=change):
                self.client.meta_change = change
                with self.assertRaises(spapi.SpApiError):self.generate()
                self.assertFalse(self.root.exists())
        self.client.meta_change = None;self.client.raw_change = b'{}'
        with self.assertRaisesRegex(spapi.SpApiError, '校验和'):self.generate()
        self.client.raw_change = None
        self.assertIsNotNone(self.generate()['token'])
        # Hex-encoded MD5 and the older 'number' field are accepted too.
        self.client.meta_change = lambda m:m['schema'].update(checksum=hashlib.md5(self.client.raw()).hexdigest())
        self.assertEqual(self.generate()['version']['number'], 'version-1')
        self.client.meta_change = lambda m:m.update(productTypeVersion={'number': 'legacy-1', 'latest': True})
        self.assertEqual(self.generate()['version']['number'], 'legacy-1')
        self.client.meta_change = None

    def test_empty_valid_workbook_enums_manifest_and_row1_source(self):
        preview = self.generate();token = preview['token']
        self.assertFalse(self.root.exists());self.assertEqual(self.lock.entries, 0)
        self.assertTrue(all(call[0] == 'GET' for call in self.client.calls))
        data, name, mime = self.artifact(token)
        self.assertTrue(name.endswith('.xlsx'));self.assertEqual(mime, generated.XLSX_MIME)
        info = metadata.inspect(data, include_seller=True)
        self.assertEqual(info['seller_id'], 'ATEST001');self.assertEqual(info['generated_format'], 'spapi-v1')
        self.assertFalse(info['has_prefilled_data']);self.assertEqual((info['attribute_row'], info['data_row']), (3, 5))
        self.assertEqual(info['product_types'], ['SUITCASE']);self.assertEqual(info['json_attributes'], {'compliance::json': 'compliance'})
        self.assertEqual(preview['json_count'], 1)
        book = filling.Workbook(data)
        try:
            tpl = filling.template_info(book, 'AE', 'upload')
            self.assertFalse(tpl['prefilled']);self.assertEqual(len(tpl['fields']), preview['field_count'])
            self.assertEqual(tpl['fields'][0]['canonical'], 'sku');self.assertTrue(tpl['fields'][0]['required'])
            brand = next(f for f in tpl['fields'] if f['canonical'] == 'brand_name')
            self.assertEqual(book.options(brand['validation'][0]['formula1']), ['BrandA', '=NOT_A_FORMULA'])
            self.assertTrue(any(f.get('json_attribute') == 'compliance' for f in tpl['fields']))
            self.assertEqual(tpl['generated_format'], 'spapi-v1')
        finally:book.close()
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for part in archive.namelist():
                self.assertNotIn('vba', part.lower());self.assertNotIn('external', part.lower())
                raw = archive.read(part)
                self.assertNotIn(b'<f>', raw);self.assertNotIn(b'secret-signed-query', raw)
            self.assertIn('不能直接上传卖家后台', archive.read('xl/worksheets/sheet4.xml').decode())
        source, source_name, _ = self.artifact(token, 'source')
        book = filling.Workbook(source)
        try:
            cells = book.sheet('Products')[1]
            self.assertEqual(cells['A1'], 'contribution_sku');self.assertEqual(cells['B1'], 'product_type')
            self.assertFalse(any(filling.column_index(key)[1] > 1 for key in cells))
        finally:book.close()
        schema, _, _ = self.artifact(token, 'schema')
        self.assertNotIn(b'secret-signed-query', schema);self.assertEqual(json.loads(schema)['schema'], self.client.schema)
        self.assertTrue(source_name.endswith('.xlsx'))

    def test_ownership_expiry_seller_recheck_capacity_and_hash(self):
        token = self.generate()['token']
        for method, body in ((self.service.artifact, {'token': token, 'kind': 'template'}), (self.service.save, {'token': token, 'shop': 'Shop'})):
            with self.assertRaises(PermissionError):method({'username': 'other', 'role': 'admin'}, body)
            with self.assertRaises(PermissionError):method({'username': 'writer', 'role': 'viewer'}, body)
        self.client.seller = 'ATEST002'
        with self.assertRaisesRegex(ValueError, '卖家'):self.artifact(token)
        self.client.seller = 'ATEST001';self.clock[0] += 600
        with self.assertRaisesRegex(ValueError, '过期'):self.artifact(token)
        token = self.generate()['token']
        entry = self.service.items[token]
        old = entry['artifacts']['template'];entry['artifacts']['template'] = (b'changed', old[1], old[2])
        with self.assertRaisesRegex(ValueError, '完整性'):self.artifact(token)
        with patch.object(generated, 'MAX_TOKENS', 2):
            self.generate();latest = self.generate()['token']
            self.assertEqual(len(self.service.items), 2);self.assertNotIn(token, self.service.items);self.artifact(latest)

    def test_save_exact_bytes_idempotence_distinct_versions_and_roster(self):
        token = self.generate()['token'];data = self.artifact(token)[0]
        with patch.object(library, 'run', wraps=library.run) as add:
            saved = self.service.save(USER, {'token': token, 'shop': 'Shop A'})
            again = self.service.save(USER, {'token': token, 'shop': 'Shop A'})
            self.assertEqual(add.call_count, 1);self.assertTrue(again['reused']);self.assertEqual(saved['id'], again['id'])
        self.assertEqual(saved['table_type'], 'upload');self.assertEqual(saved['shop'], 'Shop A')
        self.assertEqual(library.verified(self.root, saved), data);self.assertEqual(saved['sha256'], hashlib.sha256(data).hexdigest())
        token2 = self.generate()['token'];saved2 = self.service.save(USER, {'token': token2, 'shop': 'Shop A'})
        self.assertNotEqual(saved['version'], saved2['version'])
        with self.assertRaises(ValueError):self.service.save(USER, {'token': token2, 'shop': 'Shop A', 'version': saved['version']})
        token3 = self.generate()['token']
        with self.assertRaises(ValueError):self.service.save(USER, {'token': token3, 'shop': 'Shop B'})
        with self.assertRaises(ValueError):self.service.save(USER, {'token': token3, 'shop': library.GLOBAL_SHOP})
        # Different seller cannot enter an existing shop / market roster.
        self.client.seller = 'ATEST002';token4 = self.generate()['token']
        with self.assertRaises(ValueError):self.service.save(USER, {'token': token4, 'shop': 'Shop A'})

    def test_generate_save_fill_typed_json_preview_single_confirmed_submit_chain(self):
        preview = self.generate();saved = self.service.save(USER, {'token': preview['token'], 'shop': 'Shop A'})
        template = library.verified(self.root, saved)
        fields = preview['fields'];keys = [f['key'] for f in fields]
        values = {'contribution_sku': 'MY-00007', 'compliance::json': '{"approved":true,"count":2}'}
        values.update({key: 'Travel case' if key.startswith('item_name[') else 'BrandA' if key.startswith('brand[') else '3' if key.startswith('number_of_items#') else 'false' if key.startswith('batteries_required#') else '' for key in keys if key not in values})
        # Same row1 key layout as the source artifact, populated only by explicit test inputs.
        source = generated._xlsx([('Products', [keys, [values.get(k, '') for k in keys]])])
        book, tpl, report, rows = filling.prepare(template, source, 'source.xlsx', 'AE', 'upload')
        self.addCleanup(book.close)
        self.assertEqual(report['errors'], 0, report['issues']);self.assertEqual(rows[0]['sku'], 'MY-00007')
        self.assertEqual(rows[0]['values']['product_type'], 'SUITCASE');self.assertEqual(report['template']['generated_format'], 'spapi-v1')
        product_type, attributes, skipped = listings.convert(rows[0]['values'], report['fields'], MARKET, self.client.schema)
        self.assertEqual(product_type, 'SUITCASE');self.assertFalse(skipped)
        self.assertEqual(attributes['item_name'], [{'marketplace_id': MARKET, 'language_tag': 'en_AE', 'value': 'Travel case'}])
        self.assertEqual(attributes['number_of_items'][0]['value'], 3);self.assertIs(attributes['batteries_required'][0]['value'], False)
        self.assertEqual(attributes['compliance'], {'approved': True, 'count': 2});self.assertNotIn('marketplace_id', attributes['compliance'])
        changes = operations.Changes(clock=lambda: self.clock[0])
        result = listings.precheck(self.client, self.definitions, rows, report['fields'], 'AE', 'upload', hold=lambda *a: changes.hold_listing('writer', 'AE', *a))
        self.assertEqual(result['summary'], {'processed': 1, 'successful': 1})
        held = result['submittable'][0];token = held['token']
        calls_before = len(self.client.calls)
        with self.assertRaises(ValueError):changes.submit(self.client, 'writer', token, held['sku'], False)
        with self.assertRaises(ValueError):changes.submit(self.client, 'other', token, held['sku'], True)
        self.assertEqual(len(self.client.calls), calls_before)
        preview_call = next(c for c in self.client.calls if c[0] == 'PUT')
        self.assertEqual(preview_call[2]['mode'], spapi.PREVIEW);self.assertFalse(preview_call[4])
        reply = changes.submit(self.client, 'writer', token, held['sku'], True)
        self.assertTrue(reply['accepted']);live = self.client.calls[-1]
        self.assertTrue(live[4]);self.assertNotIn('mode', live[2]);self.assertEqual(live[3], preview_call[3])
        with self.assertRaises(ValueError):changes.submit(self.client, 'writer', token, held['sku'], True)
        # New invalid JSON produces a row error and no alternate write/hold route.
        rows[0]['values']['compliance::json'] = '{bad'
        result = listings.precheck(self.client, self.definitions, rows, report['fields'], 'AE', 'upload')
        self.assertEqual(result['summary']['processed'], 0);self.assertFalse(result['submittable']);self.assertTrue(result['issues'])


class SchemaTests(unittest.TestCase):
    def compile(self, properties, required=(), **extra):
        return schema_utils.compile_fields({'type': 'object', 'properties': properties, 'required': list(required), **extra}, MARKET)

    def test_refs_allof_cycles_oneof_external_and_root_fallback(self):
        schema = {'type': 'object', '$defs': {'number': {'type': 'integer'}, 'cycle': {'$ref': '#/$defs/cycle'}}, 'properties': {
            'number': array({'value': {'allOf': [{'$ref': '#/$defs/number'}, {'minimum': 0}]}}),
            'cycle': array({'value': {'$ref': '#/$defs/cycle'}}), 'external': array({'value': {'$ref': 'https://evil.test/schema'}}),
            'ambiguous': array({'value': {'oneOf': [{'type': 'integer'}, {'type': 'string'}]}}),
            'contradictory': array({'value': {'allOf': [{'type': 'integer'}, {'type': 'boolean'}]}}),
            'object': {'type': 'object'}, 'scalar': {'type': 'string'}}, 'required': ['ambiguous']}
        fields, warnings = schema_utils.compile_fields(schema, MARKET)
        keys = [f['key'] for f in fields]
        self.assertIn('number#1.value', keys)
        for name in ('cycle', 'external', 'ambiguous', 'contradictory', 'object', 'scalar'):
            self.assertEqual([k for k in keys if k.startswith(name)], [name+'::json'])
        self.assertTrue(next(f for f in fields if f['key']=='ambiguous::json')['required'])
        self.assertEqual(listings.convert({'number#1.value': '7'}, [], MARKET, schema)[1]['number'][0]['value'], 7)
        self.assertTrue(warnings)

    def test_conditional_nested_required_and_repeated_slots(self):
        fields, _ = self.compile({'required_attr': array({'value': {'type': 'string'}, 'optional': {'type': 'object', 'properties': {'inside': {'type': 'integer'}}, 'required': ['inside']}}),
                                  'optional_attr': array({'value': {'type': 'string'}}), 'conditional_attr': array({'value': {'type': 'string'}}),
                                  'bullet_point': array({'value': {'type': 'string'}}, maximum=5), 'too_many': array({'value': {'type': 'string'}}, maximum=6)},
                                 required=['required_attr'], allOf=[{'if': {'required': ['optional_attr']}, 'then': {'required': ['conditional_attr']}}])
        by = {f['key']: f for f in fields}
        self.assertTrue(by['required_attr#1.value']['required']);self.assertFalse(by['required_attr#1.optional.inside']['required'])
        self.assertTrue(by['required_attr#1.optional.inside']['conditional']);self.assertFalse(by['optional_attr#1.value']['required'])
        self.assertTrue(by['conditional_attr#1.value']['conditional']);self.assertFalse(by['conditional_attr#1.value']['required'])
        self.assertIn('bullet_point#5.value', by);self.assertNotIn('bullet_point#6.value', by)
        self.assertIn('too_many::json', by);self.assertNotIn('too_many#1.value', by)
        self.assertFalse(by['bullet_point#2.value']['required'])

    def test_selectors_only_unambiguous_defaults_ignored(self):
        fields, _ = self.compile({'offer': array({'value': {'type': 'number', 'default': 99}, 'marketplace_id': {'type': 'string'},
                                                  'audience': {'type': 'string', 'enum': ['ALL', 'B2B']}, 'currency': {'type': 'string', 'enum': ['AED', 'USD']},
                                                  'language_tag': {'type': 'string', 'enum': ['en_AE', 'ar_AE']}})})
        keys = [f['key'] for f in fields]
        self.assertIn('offer[marketplace_id='+MARKET+']#1.audience', keys)
        self.assertTrue(all('[audience=' not in key and '[currency=' not in key for key in keys))
        self.assertTrue(all('default' not in f for f in fields))

    def test_column_cap_collapses_whole_attribute_and_missing_required_refused(self):
        fields, _ = self.compile({'wide': array({'value'+str(i): {'type': 'string'} for i in range(3000)}, required=()), 'other': array({'value': {'type': 'boolean'}})})
        self.assertLessEqual(len(fields), 3000);self.assertIn('wide::json', [f['key'] for f in fields])
        self.assertFalse(any(f['key'].startswith('wide#') for f in fields))
        with self.assertRaises(ValueError):self.compile({'one': array({'value': {'type': 'string'}})}, required=['absent'])

    def test_strict_json_declared_flag_type_depth_duplicate_unknown_conflict(self):
        schema = fixture_schema()
        field = {'key': 'compliance::json', 'json_attribute': 'compliance', 'generated_format': schema_utils.GENERATED_FORMAT}
        for raw in ('NaN', '{"approved":true,"approved":false,"count":2}', '{"approved":true,"count":1e999}', '[]', '{"approved":"true","count":2}', '['*40+'0'+']'*40, 'x'*(schema_utils.MAX_JSON_BYTES+1)):
            with self.subTest(raw=raw[:40]):
                with self.assertRaises(ValueError):listings.convert({'compliance::json': raw}, [field], MARKET, schema)
        with self.assertRaises(ValueError):listings.convert({'compliance::json': '{"approved":true,"count":2}'}, [], MARKET, schema)
        with self.assertRaises(ValueError):listings.convert({'unknown::json': '{}'}, [{'key':'unknown::json', 'json_attribute':'unknown', 'generated_format':schema_utils.GENERATED_FORMAT}], MARKET, schema)
        with self.assertRaises(ValueError):listings.convert({'compliance::json': '{"approved":true,"count":2}', 'compliance#1.value':'x'}, [field], MARKET, schema)
        array_schema = {'properties': {'many': array({'value': {'type': 'string'}}, minimum=1, maximum=6)}}
        f = {'key':'many::json','json_attribute':'many','generated_format':schema_utils.GENERATED_FORMAT}
        result = listings.convert({'many::json':'[{"value":"my fact"}]'}, [f], MARKET, array_schema)
        self.assertEqual(result[1]['many'], [{'value': 'my fact'}])  # no guessed marketplace selector

    def test_human_labels_keep_official_ancestor_titles_and_repetition(self):
        named = array({'value': {'type': 'string'}, 'language_tag': {'type': 'string', 'enum': ['en_AE', 'ar_AE']}})
        named['title'] = 'Item Name'
        bullets = array({'value': {'type': 'string'}}, maximum=5);bullets['title'] = 'Bullet Point'
        nested = array({'our_price': array({'value': {'type': 'number'}}, maximum=1)}, required=('our_price',));nested['title'] = 'Purchasable Offer'
        fields, _ = self.compile({'item_name': named, 'bullet_point': bullets, 'purchasable_offer': nested,
                                  'same_branch': array({'value': {'oneOf': [{'type':'string'}, {'type':'string'}]}})})
        by = {f['key']: f for f in fields}
        self.assertEqual(by['item_name#1.value']['label'], 'Item Name / Value')
        self.assertEqual(by['bullet_point#5.value']['label'], 'Bullet Point 5 / Value')
        self.assertEqual(by['purchasable_offer#1.our_price#1.value']['label'], 'Purchasable Offer / Our Price / Value')
        self.assertIn('same_branch::json', by)
        self.assertFalse(any('#' in f['label'] or '[' in f['label'] for f in fields))
        self.assertEqual(filling.canonical('brand#1.language_tag'), '')
        self.assertEqual(filling.canonical('brand#1.value'), 'brand_name')

    def test_canonical_basic_english_chinese_headers_and_no_json_alias(self):
        for key, expected in [('item_name#1.value','item_name'), ('product_description#1.value','product_description'), ('product_description.value','product_description'),
                              ('main_product_image_locator#1.media_location','main_image_url'), ('main_product_image_locator.media_location','main_image_url')]:
            self.assertEqual(filling.canonical(key), expected)
        fields = [{'key': 'item_name#1.value', 'label': 'Untranslated', 'canonical': 'item_name', 'active': True},
                  {'key': 'product_description#1.value', 'label': 'Untranslated2', 'canonical': 'product_description', 'active': True}]
        self.assertEqual(filling.auto_mapping(fields, ['标题', 'description'], 'AE'), {'item_name#1.value':0, 'product_description#1.value':1})
        self.assertEqual(filling.canonical('item_name::json', 'Item Name'), '')


if __name__ == '__main__':unittest.main()
