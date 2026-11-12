import base64
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from xml.sax.saxutils import escape
import zipfile

from app import create_server
from template_manager import filling, library, metadata, processing
from test_filling import template_book

S = metadata.N['s']
TEXT_REPORT = (
    'Feed Processing Summary:\n'
    '\tNumber of records processed\t\t3\n'
    '\tNumber of records successful\t\t1\n\n'
    'original-record-number\tsku\terror-code\terror-type\terror-message\n'
    "1\tA-1\t90220\tError\t'brand' is required but not supplied.\n"
    '2\tA-2\t8560\tError\tSKU A-2, Missing Attributes product_type. SKU A-2 does not match any ASIN.\n'
    '2\tA-2\t99999\tWarning\tThe value "Redd" for "Color" is not valid.\n'
    "1\tA-1\t90220\tError\t'brand' is required but not supplied.\n"
    '3\tA-3\t5665\tError\tYou need approval to list products under brand TravelCo.\n'
)


def library_fields():
    book = filling.Workbook(template_book())
    try:
        return filling.template_info(book, 'AE', 'upload')['fields']
    finally:
        book.close()


def cell(ref, value, style=None):
    s = f' s="{style}"' if style is not None else ''
    return f'<c r="{ref}"{s} t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'


def report_book(comments=True):
    """An Excel processing report: the submitted template with a status column added in front."""
    rows = {
        1: [('A1', 'settings=attributeRow=3&dataRow=5')],
        2: [('A2', 'Error Messages'), ('B2', 'SKU'), ('C2', 'Product Type'), ('D2', 'Item Name'), ('E2', 'Brand Name'), ('F2', 'Color')],
        3: [('B3', 'contribution_sku#1.value'), ('C3', 'product_type#1.value'), ('D3', 'item_name#1.value'), ('E3', 'brand#1.value'), ('F3', 'color#1.value')],
        5: [('A5', '2'), ('B5', 'A-1'), ('C5', 'SUITCASE'), ('D5', 'Case'), ('E5', '', 1), ('F5', 'Redd', 2)],
        6: [('B6', 'A-2'), ('C6', 'SUITCASE'), ('D6', 'x' * 300, 3), ('E6', 'BrandA'), ('F6', 'Red')],
    }
    sheet = '<worksheet xmlns="' + S + '"><sheetData>' + ''.join(
        f'<row r="{r}">' + ''.join(cell(*c) for c in cells) + '</row>' for r, cells in rows.items()) + '</sheetData></worksheet>'
    summary = '<worksheet xmlns="' + S + '"><sheetData>' + ''.join(
        f'<row r="{r}">' + ''.join(cell(chr(65 + i) + str(r), v) for i, v in enumerate(values)) + '</row>'
        for r, values in enumerate([['Number of records processed', '3'], ['SKU', 'Error Code', 'Error Message'], ['A-3', '8572', 'UPC is invalid for this brand']], 1)) + '</sheetData></worksheet>'
    notes = {'E5': "Amazon:\n[90220] 'Brand Name' is required but not supplied.", 'F5': "The value 'Redd' is not a valid value.", 'D6': 'Error: Item Name exceeds the maximum length of 200 characters.'}
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        z.writestr('[Content_Types].xml', '<Types/>')
        z.writestr('xl/workbook.xml', '<workbook xmlns="' + S + '" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Template" sheetId="1" r:id="rId1"/><sheet name="Summary" sheetId="2" r:id="rId2"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml', sheet)
        z.writestr('xl/worksheets/sheet2.xml', summary)
        fills = ''.join(f'<fill><patternFill patternType="solid"><fgColor rgb="FF{rgb}"/></patternFill></fill>' for rgb in ('F4B183', 'FFFF00', '9BC2E6'))
        z.writestr('xl/styles.xml', '<styleSheet xmlns="' + S + '"><fills><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>' + fills + '</fills><cellXfs><xf fillId="0"/><xf fillId="2"/><xf fillId="3"/><xf fillId="4"/></cellXfs></styleSheet>')
        if comments:
            z.writestr('xl/worksheets/_rels/sheet1.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="../comments1.xml"/></Relationships>')
            z.writestr('xl/comments1.xml', '<comments xmlns="' + S + '"><authors><author>Amazon</author></authors><commentList>' + ''.join(
                f'<comment ref="{ref}" authorId="0"><text><r><t xml:space="preserve">{escape(text)}</t></r></text></comment>' for ref, text in notes.items()) + '</commentList></comments>')
    return out.getvalue()


class ProcessingReportTests(unittest.TestCase):
    def test_text_report_maps_skus_to_template_fields_and_explains(self):
        result = processing.parse(TEXT_REPORT.encode(), 'report.txt', library_fields())
        self.assertEqual(result['format'], 'text')
        self.assertEqual(result['summary'], {'processed': 3, 'successful': 1})
        self.assertEqual(len(result['issues']), 4)
        brand, missing, colour, approval = result['issues']
        self.assertEqual((approval['kind'], approval['field_key']), ('brand', 'brand#1.value'))
        self.assertEqual((brand['sku'], brand['severity'], brand['code'], brand['record']), ('A-1', 'error', '90220', 1))
        self.assertEqual((brand['field_key'], brand['field_label'], brand['column'], brand['kind']), ('brand#1.value', 'Brand Name', 'E', 'missing'))
        self.assertEqual((missing['kind'], missing['field_key']), ('not_enough', 'product_type#1.value'))
        self.assertEqual((colour['severity'], colour['field_key'], colour['kind']), ('warning', 'color#1.value', 'invalid_value'))
        self.assertIn('"Redd"', colour['message'])
        self.assertEqual(result['counts'], {'error': 3, 'warning': 1, 'suggestion': 0, 'unknown': 0})
        self.assertEqual((result['skus'], result['error_skus']), (3, 3))
        self.assertEqual([g['severity'] for g in result['groups']], ['error', 'error', 'error', 'warning'])

    def test_text_report_without_template_keeps_amazon_wording(self):
        result = processing.parse(TEXT_REPORT.encode('utf-16'), 'report.txt')
        self.assertEqual(len(result['issues']), 4)
        self.assertEqual(result['issues'][0]['field_key'], '')
        self.assertTrue(all(i['message'] and i['title'] and i['hint'] for i in result['issues']))

    def test_excel_report_reads_comments_colours_and_inserted_columns(self):
        result = processing.parse(report_book(), 'report.xlsm', library_fields())
        self.assertEqual(result['format'], 'excel')
        self.assertEqual(result['summary'].get('processed'), 3)
        found = {(i['sku'], i['field_key']): i for i in result['issues']}
        brand = found[('A-1', 'brand#1.value')]
        self.assertEqual((brand['severity'], brand['code'], brand['kind'], brand['row']), ('error', '90220', 'missing', 5))
        colour = found[('A-1', 'color#1.value')]
        self.assertEqual((colour['severity'], colour['column'], colour['kind']), ('warning', 'I', 'invalid_value'))
        title = found[('A-2', 'item_name#1.value')]
        self.assertEqual((title['severity'], title['kind']), ('error', 'too_long'))
        upc = next(i for i in result['issues'] if i['sku'] == 'A-3')
        self.assertEqual((upc['code'], upc['kind'], upc['severity']), ('8572', 'product_id', 'unknown'))
        self.assertFalse(any(i['message'] == '2' for i in result['issues']))
        alone = processing.parse(report_book(), 'report.xlsx')
        self.assertEqual(next(i for i in alone['issues'] if i['field_key'] == 'color#1.value')['column'], 'F')

    def test_explanations_use_codes_then_wording(self):
        self.assertEqual(processing.explain('5665', 'anything')[0], 'brand')
        self.assertEqual(processing.explain('', 'You need approval to list this brand')[0], 'brand')
        self.assertEqual(processing.explain('', 'Your selling price appears to be higher than expected')[0], 'price')
        self.assertEqual(processing.explain('', 'Parent SKU P-1 does not exist')[0], 'variation')
        self.assertEqual(processing.explain('', 'something new')[0], 'other')
        self.assertEqual(processing.severity_of_colour('FFC7CE'), 'error')
        self.assertEqual(processing.severity_of_colour('FFFFFF'), None)

    def test_rejects_files_that_are_not_processing_reports(self):
        with self.assertRaisesRegex(ValueError, '没有找到处理报告的错误信息'):
            processing.parse(template_book(), 'mine.xlsm')
        with self.assertRaisesRegex(ValueError, '表头'):
            processing.parse(b'hello\tworld\n', 'notes.txt')
        with self.assertRaisesRegex(ValueError, '另存为'):
            processing.parse(b'\xd0\xcf\x11\xe0legacy', 'old.xls')
        with self.assertRaisesRegex(ValueError, '扩展名'):
            processing.parse(report_book(), 'report.csv')
        with self.assertRaisesRegex(ValueError, '只支持'):
            processing.parse(b'%PDF', 'report.pdf')
        with self.assertRaisesRegex(ValueError, '损坏'):
            processing.parse(b'PK\x03\x04broken', 'report.xlsx')


class ProcessingReportAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name) / 'library'
        cls.server = create_server(cls.root, 0)
        cls.server.auth.create('reader', 'processing-test-password', 'viewer')
        source = Path(cls.temp.name) / 'original.xlsm'
        source.write_bytes(template_book())
        cls.template = library.run(SimpleNamespace(command='add', library=str(cls.root), source=str(source), shop='测试甲', marketplace='AE', category='行李箱', version='v1', table_type='upload'))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.temp.cleanup()

    def request(self, body, cookie=None):
        c = HTTPConnection('127.0.0.1', self.server.server_port, timeout=20)
        headers = {'Content-Type': 'application/json', 'X-Template-Request': '1'}
        if cookie:
            headers['Cookie'] = cookie
        c.request('POST', '/api/processing-report', json.dumps(body), headers)
        r = c.getresponse()
        result = r.status, json.loads(r.read())
        c.close()
        return result

    def test_any_member_can_read_a_report_against_a_library_template(self):
        c = HTTPConnection('127.0.0.1', self.server.server_port, timeout=20)
        c.request('POST', '/api/login', json.dumps({'username': 'reader', 'password': 'processing-test-password'}), {'Content-Type': 'application/json', 'X-Template-Request': '1'})
        cookie = c.getresponse().getheader('Set-Cookie').split(';')[0]
        c.close()
        body = {'filename': 'report.txt', 'data': base64.b64encode(TEXT_REPORT.encode()).decode()}
        self.assertEqual(self.request(body)[0], 401)
        status, result = self.request(body, cookie)
        self.assertEqual(status, 200, result)
        self.assertEqual(result['issues'][0]['field_key'], '')
        status, result = self.request({**body, 'template_id': self.template['id']}, cookie)
        self.assertEqual(status, 200, result)
        self.assertEqual(result['issues'][0]['field_key'], 'brand#1.value')
        self.assertEqual(result['template']['id'], self.template['id'])
        self.assertEqual(self.request({**body, 'template_id': 'missing'}, cookie)[0], 400)


if __name__ == '__main__':
    unittest.main()
