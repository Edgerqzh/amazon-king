import base64
from copy import deepcopy
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

from app import create_server
from template_manager import ai, copywriting, filling, library
import test_filling


def copy_template(localized=False):
    fields=test_filling.FIELDS+[('title_differentiation#1.value','Item Highlight')]+[('bullet_point#'+str(i)+'.value','Bullet '+str(i)) for i in range(1,6)]
    if localized:fields=[(key.replace('item_name#','item_name[language_tag=en_AE]#').replace('title_differentiation#','title_differentiation[language_tag=en_AE]#').replace('bullet_point#','bullet_point[language_tag=en_AE]#'),label) for key,label in fields]
    with patch.object(test_filling,'FIELDS',fields):return test_filling.template_book()


def products(count=1,sku=True):
    return ('SKU,name,brand,material,color,features,price,库存,卖家记号,联系电话\n'+ '\n'.join((f'KEEP-{i:03}' if sku else '')+',Travel case,BrandA,ABS shell,Blue,Zipper closure; Trolley handle; Carry handle,999,80,DO-NOT-SEND,DO-NOT-SEND' for i in range(1,count+1))).encode()


def answer_for(payload):
    items=[]
    for product in payload['products']:
        facts=product['facts'];get=lambda header:next(f for f in facts if f['header']==header)
        name=get('name');material=get('material');color=get('color');features=get('features')
        items.append({'sku':product['sku'],'product_type':{'value':product['known_product_type'] or 'SUITCASE','confidence':'high','source_refs':[name['ref']]},'title':{'text':'Blue Travel Case','source_refs':[name['ref'],color['ref']]},'highlight':{'text':'ABS shell with a zipper closure','source_refs':[material['ref'],features['ref']]},'bullets':[{'text':'ABS shell','source_refs':[material['ref']]},{'text':'Blue color','source_refs':[color['ref']]},{'text':'Zipper closure','source_refs':[features['ref']]},{'text':'Trolley handle','source_refs':[features['ref']]},{'text':'Carry handle','source_refs':[features['ref']]}],'missing_information':[]})
    return {'items':items}


class CopywritingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.template=copy_template();self.source=products();self.settings=ai.Settings(Path(self.temp.name)/'ai.json');self.settings.configure('qwen','qwen3-vl-plus','fake-copy-test-key')

    def tearDown(self):self.temp.cleanup()

    def context(self,source=None,**kwargs):return copywriting.context(self.template,source or self.source,'products.csv','AE','upload',**kwargs)

    def test_context_uses_original_sku_and_only_needed_product_facts(self):
        ctx=self.context();self.assertEqual(ctx['products'][0]['sku'],'KEEP-001');self.assertEqual(ctx['products'][0]['known_product_type'],'SUITCASE')
        serialized=json.dumps(ctx['products']);self.assertNotIn('DO-NOT-SEND',serialized);self.assertNotIn('999',serialized);self.assertNotIn('库存',serialized)
        self.assertTrue(any(f['canonical']=='item_highlight' for f in ctx['fields']))
        selected=self.context(products(4),start=3,limit=2);self.assertEqual([p['sku'] for p in selected['products']],['KEEP-003','KEEP-004']);self.assertEqual(selected['total_skus'],4)
        with self.assertRaises(ValueError):self.context(start=True)
        with self.assertRaisesRegex(ValueError,'SKU 来源'):self.context(products(sku=False))
        with self.assertRaisesRegex(ValueError,'冲突'):self.context(products()+b'\nKEEP-001,Other case,BrandA,Steel,Black,Other facts,50,20,x,x\n')
        with self.assertRaisesRegex(ValueError,'改价'):copywriting.context(self.template,self.source,'p.csv','AE','price_update')

    def test_unknown_category_can_be_suggested_but_incompatible_codes_are_blocked(self):
        source=products().replace(b'SKU,name,',b'SKU,product_type,name,').replace(b'KEEP-001,Travel case', 'KEEP-001,旅行硬壳箱,Travel case'.encode())
        ctx=self.context(source);self.assertEqual(ctx['products'][0]['known_product_type'],'')
        answer=answer_for(ctx);accepted=copywriting.validate_answer(answer,ctx['products'],ctx['supported_product_types'],'en')
        self.assertEqual(accepted['KEEP-001']['product_type']['value'],'SUITCASE')
        book,_,report,rows=filling.prepare(self.template,source,'p.csv','AE','upload',by_sku={'KEEP-001':{'product_type#1.value':'SUITCASE'}},copy_confirmed=True)
        book.close();self.assertEqual(rows[0]['values']['product_type#1.value'],'SUITCASE')
        with self.assertRaisesRegex(ValueError,'不兼容'):self.context(source.replace('旅行硬壳箱'.encode(),b'PHONE_CASE'))

    def test_valid_proposals_and_five_factual_bullets(self):
        ctx=self.context();answer=answer_for(ctx);accepted=copywriting.validate_answer(answer,ctx['products'],ctx['supported_product_types'],'en')
        self.assertEqual(len(accepted['KEEP-001']['bullets']),5);self.assertEqual(accepted['KEEP-001']['title']['text'],'Blue Travel Case')

    def test_hallucinated_numbers_claims_refs_and_scope_are_rejected(self):
        ctx=self.context();original=answer_for(ctx)
        mutations=[lambda x:x['items'][0].update(sku='NEW-SKU'),lambda x:x['items'][0].update(price='1'),lambda x:x['items'][0]['product_type'].update(value='PHONE_CASE'),lambda x:x['items'][0]['title'].update(text='100 L Travel Case'),lambda x:x['items'][0]['highlight'].update(text='Waterproof and FDA certified'),lambda x:x['items'][0]['title'].update(source_refs=['R999C1']),lambda x:x['items'][0]['title'].update(text='A'*76),lambda x:x['items'][0]['bullets'].append(x['items'][0]['bullets'][0]),lambda x:x['items'][0]['bullets'].__setitem__(1,x['items'][0]['bullets'][0]),lambda x:x['items'].clear(),lambda x:x['items'][0]['title'].update(text='中文标题')]
        for mutate in mutations:
            answer=deepcopy(original);mutate(answer)
            with self.subTest(answer=answer),self.assertRaises(ValueError):copywriting.validate_answer(answer,ctx['products'],ctx['supported_product_types'],'en')
        other=self.context(products(2));answer=answer_for(other);answer['items'][0]['title']['source_refs']=[other['products'][1]['facts'][0]['ref']]
        with self.assertRaises(ValueError):copywriting.validate_answer(answer,other['products'],other['supported_product_types'],'en')

    def test_generation_cache_is_scoped_and_never_changes_sku(self):
        ctx=self.context(products(4));cache=ai.ResultCache()
        def remote(settings,system,payload,**kwargs):self.assertNotIn('DO-NOT-SEND',json.dumps(payload));return answer_for(payload)
        with patch.object(ai,'request_json',side_effect=remote) as mock:
            first=copywriting.generate(self.settings,ctx,cache=cache,cache_scope='owner');self.assertEqual((first['generated'],first['calls']),(4,2))
            second=copywriting.generate(self.settings,ctx,cache=cache,cache_scope='owner');self.assertEqual((second['cached'],second['calls']),(4,0));self.assertEqual(mock.call_count,2)
            copywriting.generate(self.settings,ctx,cache=cache,cache_scope='other');self.assertEqual(mock.call_count,4)
        self.assertEqual([i['sku'] for i in first['items']],['KEEP-001','KEEP-002','KEEP-003','KEEP-004'])

    def test_insufficient_points_stay_incomplete_and_cache_does_not_accumulate_notes(self):
        ctx=self.context();cache=ai.ResultCache()
        def remote(settings,system,payload,**kwargs):
            answer=answer_for(payload);answer['items'][0]['bullets']=answer['items'][0]['bullets'][:2];return answer
        with patch.object(ai,'request_json',side_effect=remote):
            first=copywriting.generate(self.settings,ctx,cache=cache,cache_scope='a');second=copywriting.generate(self.settings,ctx,cache=cache,cache_scope='a')
        self.assertEqual(len(first['items'][0]['bullets']),2);self.assertEqual(first['items'][0]['missing_information'],second['items'][0]['missing_information']);self.assertEqual(second['cached'],1)

    def test_first_batch_failure_stops_additional_paid_calls(self):
        ctx=self.context(products(7))
        with patch.object(ai,'request_json',side_effect=ValueError('mock failure')) as remote:
            result=copywriting.generate(self.settings,ctx);self.assertEqual(remote.call_count,1)
        self.assertEqual(result['generated'],0);self.assertEqual(len(result['errors']),7);self.assertEqual(result['calls'],1)

    def test_only_reviewed_copy_fields_can_be_adopted_and_written(self):
        overrides={'KEEP-001':{'item_name#1.value':'Reviewed Travel Case','title_differentiation#1.value':'Reviewed highlight',**{'bullet_point#'+str(i)+'.value':'Factual point '+str(i) for i in range(1,6)}}}
        with self.assertRaisesRegex(ValueError,'核对'):filling.prepare(self.template,self.source,'p.csv','AE','upload',by_sku=overrides)
        book,tpl,report,rows=filling.prepare(self.template,self.source,'p.csv','AE','upload',by_sku=overrides,copy_confirmed=True)
        try:
            self.assertEqual(rows[0]['sku'],'KEEP-001');self.assertEqual(rows[0]['values'][test_filling.PRICE],'999');self.assertEqual(rows[0]['values']['brand#1.value'],'BrandA');self.assertEqual(rows[0]['values']['item_name#1.value'],'Reviewed Travel Case')
            self.assertTrue(any(e['method']=='reviewed_copy' for e in report['evidence']));filled=filling.render(book,tpl,rows)
        finally:book.close()
        with zipfile.ZipFile(io.BytesIO(self.template)) as original,zipfile.ZipFile(io.BytesIO(filled)) as output:
            for name in original.namelist():
                if name!='xl/worksheets/sheet1.xml':self.assertEqual(original.read(name),output.read(name))
        for key,value in [('sku','RENAMED'),(test_filling.PRICE,'1'),('brand#1.value','Invented')]:
            with self.subTest(key=key),self.assertRaises(ValueError):filling.prepare(self.template,self.source,'p.csv','AE','upload',by_sku={'KEEP-001':{key:value}},copy_confirmed=True)
        with self.assertRaisesRegex(ValueError,'不存在'):filling.prepare(self.template,self.source,'p.csv','AE','upload',by_sku={'UNKNOWN':{'item_name#1.value':'Title'}},copy_confirmed=True)
        with self.assertRaisesRegex(ValueError,'声明范围'):filling.prepare(self.template,self.source,'p.csv','AE','upload',by_sku={'KEEP-001':{'product_type#1.value':'PHONE_CASE'}},copy_confirmed=True)
        with self.assertRaisesRegex(ValueError,'改价'):filling.prepare(self.template,self.source,'p.csv','AE','price_update',by_sku=overrides,copy_confirmed=True)

    def test_incomplete_adopted_bullets_and_locale_stay_visible(self):
        book,_,report,_=filling.prepare(self.template,self.source,'p.csv','AE','upload',by_sku={'KEEP-001':{'bullet_point#1.value':'ABS shell'}},copy_confirmed=True)
        book.close();self.assertEqual(report['status'],'draft');self.assertTrue(any('仅有' in i['message'] for i in report['issues']))
        localized=copy_template(localized=True)
        with self.assertRaisesRegex(ValueError,'所选语言'):copywriting.context(localized,self.source,'p.csv','AE','upload',language='ar')
        with self.assertRaisesRegex(ValueError,'语言标签'):filling.prepare(localized,self.source,'p.csv','AE','upload',by_sku={'KEEP-001':{'item_name[language_tag=en_AE]#1.value':'Title'}},copy_confirmed=True,copy_language='ar')

    def test_ai_request_json_redacts_secrets_and_does_not_send_to_unconfigured_service(self):
        payload={'choices':[{'message':{'content':'{"items":[]}'}}]}
        with patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps(payload).encode())) as remote:
            self.assertEqual(ai.request_json(self.settings,'test',{'products':[]}),{'items':[]});request=remote.call_args.args[0];self.assertEqual(json.loads(request.data)['response_format'],{'type':'json_object'})
        error=urllib.error.HTTPError('https://example.invalid',401,'failure',{},io.BytesIO(json.dumps({'error':{'code':'auth','message':'fake-copy-test-key'}}).encode()))
        with patch('urllib.request.urlopen',side_effect=error):
            with self.assertRaises(ValueError) as caught:ai.request_json(self.settings,'test',{})
        self.assertNotIn('fake-copy-test-key',str(caught.exception));self.assertIn('HTTP 401',str(caught.exception))


class CopywritingAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name)/'library';cls.server=create_server(cls.root,0);cls.server.ai.configure('qwen','qwen3-vl-plus','fake-api-copy-key')
        for user,role in [('owner','admin'),('writer','editor'),('reader','viewer')]:cls.server.auth.create(user,'copy-test-password',role)
        original=Path(cls.temp.name)/'original.xlsm';original.write_bytes(copy_template());cls.targets={}
        for kind in ('upload','price_update'):cls.targets[kind]=library.run(SimpleNamespace(command='add',library=str(cls.root),source=str(original),shop='测试甲',marketplace='AE',category='行李箱',version='v1',table_type=kind))
        threading.Thread(target=cls.server.serve_forever,daemon=True).start()

    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.temp.cleanup()

    def request(self,path,body,cookie=None):
        c=HTTPConnection('127.0.0.1',self.server.server_port,timeout=20);headers={'Content-Type':'application/json','X-Template-Request':'1'}
        if cookie:headers['Cookie']=cookie
        c.request('POST',path,json.dumps(body),headers);r=c.getresponse();result=r.status,r.read(),dict(r.getheaders());c.close();return result

    def login(self,user):return self.request('/api/login',{'username':user,'password':'copy-test-password'})[2]['Set-Cookie'].split(';')[0]

    def test_explicit_consent_roles_cache_original_preservation_and_immutable_sku(self):
        owner,writer,reader=[self.login(u) for u in ('owner','writer','reader')];target=self.targets['upload'];before=library.verified(self.root,target)
        body={'filename':'products.csv','data':base64.b64encode(products()).decode(),'template_id':target['id'],'shop':'测试甲','marketplace':'AE','table_type':'upload','copy_language':'en','copy_limit':20}
        with patch.object(ai,'request_json',side_effect=lambda settings,system,payload,**kwargs:answer_for(payload)) as remote:
            self.assertEqual(self.request('/api/fill/generate',body,reader)[0],403);self.assertEqual(self.request('/api/fill/generate',body,owner)[0],400);remote.assert_not_called()
            status,raw,_=self.request('/api/fill/generate',{**body,'copy_consent':True},owner);self.assertEqual(status,200,raw);result=json.loads(raw);self.assertEqual(result['generated'],1);self.assertEqual(result['items'][0]['sku'],'KEEP-001')
            status,raw,_=self.request('/api/fill/generate',{**body,'copy_consent':True},owner);self.assertEqual(json.loads(raw)['cached'],1);self.assertEqual(remote.call_count,1)
            self.assertEqual(self.request('/api/fill/generate',{**body,'copy_consent':True},writer)[0],200);self.assertEqual(remote.call_count,2)
            price={**body,'template_id':self.targets['price_update']['id'],'table_type':'price_update','copy_consent':True};self.assertEqual(self.request('/api/fill/generate',price,owner)[0],400);self.assertEqual(remote.call_count,2)
            self.assertEqual(self.request('/api/fill/generate',{**body,'copy_consent':True,'shop':'其他店'},owner)[0],400)
            self.server.ai_job_lock.acquire()
            try:self.assertEqual(self.request('/api/fill/generate',{**body,'copy_consent':True},owner)[0],409)
            finally:self.server.ai_job_lock.release()
        overrides={'KEEP-001':{'item_name#1.value':'Reviewed title'}}
        self.assertEqual(self.request('/api/fill/preview',{**body,'by_sku':overrides},owner)[0],400)
        status,raw,_=self.request('/api/fill/export',{**body,'by_sku':overrides,'copy_confirmed':True},owner);self.assertEqual(status,200,raw[:300])
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            report=json.loads(z.read('填写检查报告.json'));filled=filling.Workbook(z.read(report['output_name']))
            try:self.assertEqual(filled.sheet('Template')[1]['A5'],'KEEP-001');self.assertEqual(filled.sheet('Template')[1]['D5'],'Reviewed title')
            finally:filled.close()
        self.assertEqual(library.verified(self.root,target),before)
