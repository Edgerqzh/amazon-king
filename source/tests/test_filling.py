import base64
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch
import unittest
import zipfile

from app import create_server
from template_manager import filling, library, metadata
from test_inventory import make_book

MARKET='A2VIGQ35RCS4UG'
FIELDS=[('sku','SKU'),('product_type#1.value','Product Type'),('::record_action','Listing Action'),('item_name#1.value','Item Name'),('brand#1.value','Brand Name'),('purchasable_offer[marketplace_id='+MARKET+'][audience=ALL]#1.our_price#1.schedule#1.value_with_tax','Selling Price'),('fulfillment_availability#1.quantity','Quantity'),('list_price[marketplace_id='+MARKET+']#1.currency','Currency'),('color#1.value','Color'),('size#1.value','Size'),('parentage_level#1.value','Parentage Level'),('child_parent_sku_relationship#1.parent_sku','Parent SKU'),('variation_theme#1.name','Variation Theme Name'),('purchasable_offer[marketplace_id=A17E79C6D8DWNP][audience=ALL]#1.our_price#1.schedule#1.value_with_tax','Saudi Price')]
PRICE=FIELDS[5][0]


def template_book(prefilled=False,conditional=False,formula=False,macro=True,market='AE'):
    settings='settings=contributorId=amzn1.cr.o.ATEST001&primaryMarketplaceId=amzn1.mp.o.'+('A17E79C6D8DWNP' if market=='SA' else MARKET)+'&attributeRow=3&dataRow=5&ptds='+base64.b64encode(b'SUITCASE').decode()
    definitions=[['Field Name','Required?']]+[[key,'Required' if label in ('SKU','Product Type','Listing Action','Item Name','Brand Name') else 'Conditionally Required' if conditional and label=='Color' else 'Optional'] for key,label in FIELDS]
    rows=[[settings],[label for _,label in FIELDS],[key for key,_ in FIELDS],[]]
    if prefilled:rows+=[['OLD-SKU','SUITCASE','Create or Replace (Full Update)','Old Title','Old Brand','9','4']]
    source=make_book({'Template':rows,'Data Definitions':definitions,'Dropdown Lists':[['Create or Replace (Full Update)','BrandA'],['Edit (Partial Update)','BrandB'],['Delete','']]})
    output=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(source)) as src,zipfile.ZipFile(output,'w') as dest:
        for part in src.infolist():
            data=src.read(part.filename)
            if part.filename=='xl/workbook.xml':data=data.replace(b'</workbook>',b'<definedNames><definedName name="record_action">\'Dropdown Lists\'!$A$1:$A$3</definedName><definedName name="SUITCASEbrand">\'Dropdown Lists\'!$B$1:$B$2</definedName></definedNames></workbook>')
            if part.filename=='xl/worksheets/sheet1.xml':
                validation='<dataValidations count="4"><dataValidation type="list" showErrorMessage="1" sqref="B5:B1048576"><formula1>"SUITCASE"</formula1></dataValidation><dataValidation type="list" showErrorMessage="1" sqref="C5:C1048576"><formula1>record_action</formula1></dataValidation><dataValidation type="list" showErrorMessage="1" sqref="E5:E1048576"><formula1>INDIRECT(B5 &amp;"brand")</formula1></dataValidation><dataValidation type="list" showErrorMessage="1" sqref="K5:K1048576"><formula1>"parent,child"</formula1></dataValidation><dataValidation type="list" sqref="F5:F1048576"><formula1>"Delete Offer (Sell on Amazon)"</formula1></dataValidation></dataValidations>'
                data=data.replace(b'</worksheet>',validation.encode()+b'<extLst><ext uri="test-preserve"/></extLst></worksheet>')
                if formula:data=data.replace(b'</sheetData>',b'<row r="6"><c r="A6"><f>1+1</f><v>2</v></c></row></sheetData>')
            dest.writestr(part,data)
        dest.writestr('xl/styles.xml','<styleSheet xmlns="'+metadata.N['s']+'"><cellXfs count="1"><xf/></cellXfs></styleSheet>')
        if macro:dest.writestr('xl/vbaProject.bin',b'fixture-macros-unchanged')
        dest.writestr('xl/media/fixture.png',b'fixture-image-unchanged')
    return output.getvalue()


class FillingTests(unittest.TestCase):
    def source(self,rows):return ('SKU,标题,品牌,售价,库存,币种\n'+rows).encode()
    def preview(self,data=None,template=None,kind='upload',**kwargs):
        book,tpl,report,rows=filling.prepare(template or template_book(),data or self.source('00007,Travel case,BrandA,19.50,0,AED\n'),'products.csv','AE',kind,**kwargs)
        book.close();return report,rows

    def test_upload_mapping_numbers_identifiers_and_complete_readback(self):
        original=template_book();report,rows=self.preview(template=original)
        self.assertEqual(report['status'],'ready',report['issues']);self.assertEqual(rows[0]['sku'],'00007');self.assertEqual(rows[0]['values'][PRICE],'19.50');self.assertEqual(rows[0]['values']['fulfillment_availability#1.quantity'],'0')
        archive,name,exported=filling.export(original,self.source('00007,Travel case,BrandA,19.50,0,AED\n'),'products.csv','AE','upload','上传表格_测试_AE.xlsm',strict=True)
        self.assertTrue(exported['readback_verified']);self.assertTrue(name.endswith('.zip'))
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            filename=next(n for n in z.namelist() if n.endswith('.xlsm'));filled=z.read(filename);self.assertIn('已填写',filename);self.assertIn('填写检查报告.json',z.namelist());self.assertIn('待核对问题.csv',z.namelist())
        with zipfile.ZipFile(io.BytesIO(original)) as a,zipfile.ZipFile(io.BytesIO(filled)) as b:
            self.assertEqual(a.namelist(),b.namelist())
            for part in a.namelist():
                if part!='xl/worksheets/sheet1.xml':self.assertEqual(a.read(part),b.read(part),part)
            self.assertIn(b'<extLst>',b.read('xl/worksheets/sheet1.xml'))
        book=filling.Workbook(filled)
        try:self.assertEqual(book.sheet('Template')[1]['A5'],'00007');self.assertEqual(book.sheet('Template')[1]['D5'],'Travel case')
        finally:book.close()

    def test_price_mode_only_changes_offer_fields_and_partial_action(self):
        report,rows=self.preview(kind='price_update');self.assertEqual(report['status'],'ready',report['issues'])
        values=rows[0]['values'];self.assertNotIn('item_name#1.value',values);self.assertNotIn('brand#1.value',values);self.assertEqual(values['::record_action'],'Edit (Partial Update)');self.assertEqual(values[PRICE],'19.50')
        self.assertFalse(any('A17E79C6D8DWNP' in k for k in values))
        with self.assertRaisesRegex(ValueError,'范围'):self.preview(kind='price_update',mapping={'item_name#1.value':1})
        report,_=self.preview(self.source('A,Title,BrandA,,,AED'),kind='price_update');self.assertGreater(report['errors'],0)

    def test_missing_facts_and_invalid_product_type_are_not_copied_from_old_rows(self):
        data=self.source('A,New case,,20,10,AED');report,rows=self.preview(data,template=template_book(prefilled=True),replace_existing=True)
        self.assertGreater(report['errors'],0);self.assertEqual(rows[0]['values']['brand#1.value'],'');self.assertNotIn('Old Brand',json.dumps(report))
        with self.assertRaisesRegex(ValueError,'待人工确认'):filling.export(template_book(),data,'p.csv','AE','upload','a.xlsm',strict=True)
        report,rows=self.preview(b'SKU,product_type,title,brand\nA,PHONE_CASE,Case,BrandA')
        self.assertGreater(report['errors'],0);self.assertEqual(rows[0]['values']['product_type#1.value'],'')

    def test_prefilled_confirmation_clear_and_formula_protection(self):
        original=template_book(prefilled=True)
        with self.assertRaisesRegex(ValueError,'确认'):filling.export(original,self.source('A,New case,BrandA,20,1,AED'),'p.csv','AE','upload','a.xlsm')
        archive,_,_=filling.export(original,self.source('A,New case,BrandA,20,1,AED'),'p.csv','AE','upload','a.xlsm',replace_existing=True)
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            data=z.read(next(n for n in z.namelist() if n.endswith('.xlsm')))
        book=filling.Workbook(data)
        try:self.assertEqual(book.sheet('Template')[1]['A5'],'A');self.assertNotIn('OLD-SKU',book.sheet('Template')[1].values())
        finally:book.close()
        with self.assertRaisesRegex(ValueError,'公式'):self.preview(template=template_book(formula=True),replace_existing=True)

    def test_currency_numeric_dropdown_and_wrong_market(self):
        for text in ['A,Case,BrandA,10,1,SAR','A,Case,BrandA,-1,1,AED','A,Case,BrandA,10,1.5,AED','A,Case,UnknownBrand,10,1,AED']:
            with self.subTest(text=text):self.assertGreater(self.preview(self.source(text))[0]['errors'],0)
        with self.assertRaisesRegex(ValueError,'站点'):self.preview(template=template_book(market='SA'))
        with self.assertRaisesRegex(ValueError,'范围'):self.preview(mapping={FIELDS[-1][0]:3})
        with self.assertRaisesRegex(ValueError,'范围'):self.preview(defaults={FIELDS[-1][0]:'10'})

    def test_explicit_mapping_constants_and_ambiguous_headers(self):
        data=b'code,title,brand,customprice\nA,Case,BrandA,25'
        report,_=self.preview(data);self.assertGreater(report['errors'],0)
        mapping={f['key']:f['source_index'] for f in report['fields']};mapping['sku']=0;mapping[PRICE]=3
        report,rows=self.preview(data,mapping=mapping,defaults={'color#1.value':'Black'})
        self.assertEqual(rows[0]['values'][PRICE],'25');self.assertEqual(rows[0]['values']['color#1.value'],'Black')
        with self.assertRaisesRegex(ValueError,'同时'):self.preview(mapping={'brand#1.value':2},defaults={'brand#1.value':'BrandA'})
        report,rows=self.preview(b'SKU,title,title,brand\nA,Case1,Case2,BrandA');self.assertEqual(rows[0]['values']['item_name#1.value'],'');self.assertGreater(report['errors'],0)

    def test_duplicate_complementary_rows_merge_and_conflicts_stay_drafts(self):
        report,rows=self.preview(self.source('A,Case,,20,,AED\nA,,BrandA,,10,AED'))
        self.assertEqual((report['source_rows'],report['output_rows'],report['merged_rows']),(2,1,1));self.assertEqual(report['status'],'ready',report['issues']);self.assertEqual(rows[0]['source_rows'],[2,3])
        brand_evidence=next(v for v in report['evidence'] if v['target_field']=='brand#1.value');self.assertEqual(brand_evidence['source_row'],3)
        report,rows=self.preview(self.source('A,Case,BrandA,20,10,AED\nA,Case,BrandA,30,10,AED'))
        self.assertEqual(report['output_rows'],2);self.assertTrue(any('冲突' in i['message'] for i in report['issues']))

    def test_source_sheet_header_selection_and_formula_as_text(self):
        source=make_book({'说明':[['说明']], '产品':[['标题'],['SKU','标题','品牌'],['00001','=1+1','BrandA']]})
        inspection=filling.inspect_source(source,'p.xlsx');self.assertEqual(inspection['sheet'],'产品');self.assertEqual(inspection['header_row'],2)
        book,_,report,rows=filling.prepare(template_book(),source,'p.xlsx','AE','upload',sheet='产品',header_row=2)
        try:
            result=filling.render(book,_,rows)
        finally:book.close()
        parsed=filling.Workbook(result)
        try:self.assertEqual(parsed.sheet('Template')[1]['D5'],'=1+1');self.assertIsNone(parsed.sheet('Template')[0].find("s:sheetData/s:row/s:c[@r='D5']/s:f",metadata.N))
        finally:parsed.close()

    def test_parent_child_and_standalone_rules(self):
        data=b'SKU,title,brand,parentage,parent_sku,variation_theme,color,size,price,quantity\nP,Parent,BrandA,parent,,COLOR,,,,\nC,Child,BrandA,child,P,COLOR,Black,,20,1'
        report,rows=self.preview(data);self.assertFalse(any('父子' in i['message'] or '颜色变体' in i['message'] for i in report['issues']))
        self.assertEqual(rows[0]['values'][PRICE],'')
        bad=data.replace(b'P,COLOR,Black',b'P,SIZE,Black');report,_=self.preview(bad);self.assertTrue(any('不一致' in i['message'] or '区分值' in i['message'] for i in report['issues']))

    def test_sa_currency_price_scope_and_inventory_channel(self):
        source=b'SKU,price,quantity,currency\nA,20,1,SAR'
        book,_,report,rows=filling.prepare(template_book(market='SA'),source,'p.csv','SA','price_update')
        try:
            self.assertEqual(report['status'],'ready',report['issues']);self.assertNotIn(PRICE,rows[0]['values']);self.assertEqual(rows[0]['values'][FIELDS[-1][0]],'20')
        finally:book.close()
        with patch(__name__+'.FIELDS',FIELDS+[('fulfillment_availability#1.fulfillment_channel_code','Fulfillment Channel Code')]):original=template_book()
        report,_=self.preview(template=original,kind='price_update');self.assertTrue(any('配送渠道' in i['message'] for i in report['issues']))
        data=b'SKU,price,quantity,currency,fulfillment_channel\nA,20,1,AED,DEFAULT_AE'
        self.assertEqual(self.preview(data,template=original,kind='price_update')[0]['status'],'ready')
        report,_=self.preview(data.replace(b'DEFAULT_AE',b'DEFAULT_SA'),template=original,kind='price_update');self.assertGreater(report['errors'],0)
        report,_=self.preview(b'SKU,price,parentage\nA,20,parent',kind='price_update');self.assertTrue(any('父体不能' in i['message'] for i in report['issues']))

    def test_zero_formatted_excel_sku_and_large_numeric_precision(self):
        source=make_book({'Products':[['SKU','title','brand','price'],['7','Case','BrandA','20']]},zero_sku=True)
        book,_,report,rows=filling.prepare(template_book(),source,'p.xlsx','AE','upload')
        try:self.assertEqual(rows[0]['sku'],'00007');self.assertEqual(report['status'],'ready',report['issues'])
        finally:book.close()
        report,_=self.preview(self.source('A,Case,BrandA,1234567890123456,1,AED'))
        self.assertGreater(report['errors'],0)

    def test_delete_operations_and_invalid_numeric_commands_never_written(self):
        data=b'SKU,title,brand,listing action,price\nA,Case,BrandA,Delete,Delete Offer (Sell on Amazon)'
        report,rows=self.preview(data)
        self.assertGreater(report['errors'],0);self.assertEqual(rows[0]['values']['::record_action'],'');self.assertEqual(rows[0]['values'][PRICE],'')

    def test_source_and_output_limits_invalid_mappings(self):
        with self.assertRaises(ValueError):self.preview(mapping={'sku':True})
        with self.assertRaises(ValueError):self.preview(mapping={'sku':100})
        with self.assertRaisesRegex(ValueError,'控制字符'):self.preview(self.source('A,Case\x01,BrandA,20,1,AED'))
        with self.assertRaises(ValueError):self.preview(self.source('\n'.join(f'S{i},Case,BrandA,20,1,AED' for i in range(1001))))


class FillingAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name)/'library';cls.server=create_server(cls.root,0)
        for user,role in [('owner','admin'),('reader','viewer')]:cls.server.auth.create(user,'filling-test-password',role)
        source=Path(cls.temp.name)/'original.xlsm';source.write_bytes(template_book())
        cls.template=library.run(SimpleNamespace(command='add',library=str(cls.root),source=str(source),shop='测试甲',marketplace='AE',category='行李箱',version='v1',table_type='upload'))
        threading.Thread(target=cls.server.serve_forever,daemon=True).start()

    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.temp.cleanup()

    def request(self,path,body,cookie=None):
        c=HTTPConnection('127.0.0.1',self.server.server_port,timeout=20);headers={'Content-Type':'application/json','X-Template-Request':'1'}
        if cookie:headers['Cookie']=cookie
        c.request('POST',path,json.dumps(body),headers);r=c.getresponse();result=r.status,r.read(),dict(r.getheaders());c.close();return result

    def login(self,user):return self.request('/api/login',{'username':user,'password':'filling-test-password'})[2]['Set-Cookie'].split(';')[0]

    def test_preview_export_permissions_scope_and_original_preservation(self):
        owner,reader=self.login('owner'),self.login('reader')
        body={'filename':'products.csv','data':base64.b64encode(b'SKU,title,brand,price,quantity\n00001,Case,BrandA,20,1').decode(),'template_id':self.template['id'],'shop':'测试甲','marketplace':'AE','table_type':'upload'}
        before=library.verified(self.root,self.template)
        for route in ('source','preview','export'):self.assertEqual(self.request('/api/fill/'+route,body,reader)[0],403)
        status,raw,_=self.request('/api/fill/source',body,owner);self.assertEqual(status,200);self.assertEqual(json.loads(raw)['sheet'],'CSV')
        status,raw,_=self.request('/api/fill/preview',body,owner);self.assertEqual(status,200,raw);report=json.loads(raw);self.assertEqual(report['status'],'ready',report['issues'])
        status,raw,headers=self.request('/api/fill/export',{**body,'strict':True},owner);self.assertEqual(status,200,raw[:200]);self.assertIn('zip',headers['Content-Type'])
        with zipfile.ZipFile(io.BytesIO(raw)) as z:self.assertIn('填写检查报告.json',z.namelist())
        self.assertEqual(library.verified(self.root,self.template),before)
        self.assertEqual(self.request('/api/fill/export',{**body,'alias_revision':'stale'},owner)[0],400)
        for changes in ({'shop':'其他店'},{'marketplace':'SA'},{'table_type':'price_update'},{'template_id':'missing'}):self.assertEqual(self.request('/api/fill/preview',{**body,**changes},owner)[0],400)
        self.server.fill_job_lock.acquire()
        try:self.assertEqual(self.request('/api/fill/preview',body,owner)[0],409)
        finally:self.server.fill_job_lock.release()
