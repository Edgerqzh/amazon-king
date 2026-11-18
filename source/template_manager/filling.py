"""Deterministic Amazon template drafts; change only Template's data area."""
import copy
import csv
from decimal import Decimal, InvalidOperation
import io
import json
import posixpath
import re
from xml.sax.saxutils import escape, quoteattr
import zipfile

from . import inventory, library, metadata
from .product_types import CategoryResolver, declared_types, norm

MAX_ROWS = 1000
MAX_FIELDS = 3000
N = metadata.N
ALIASES = {
    'sku': ['sku','seller sku','item_sku','contribution_sku','商家sku','商品sku','产品sku','sku编码','卖家sku','货号','商品编码'],
    'feed_product_type': ['product_type','feed_product_type','product type','商品类型','产品类型','商品类别','商品类目','产品类目','产品类别','分类','类别','类目'],
    'update_delete': ['update_delete','listing action','操作类型','更新类型'],
    'item_name': ['item_name','item name','product_name','product name','name','title','名称','商品名','产品名','商品标题','产品标题','标题','商品名称','产品名称','品名'],
    'item_highlight': ['item_highlight','item highlight','item highlights','title_differentiation','highlight','短卖点','卖点简述','商品亮点','产品亮点','亮点','卖点'],
    'brand_name': ['brand_name','brand','brand name','品牌','品牌名称'],
    'manufacturer': ['manufacturer','制造商','生产商'],
    'product_description': ['product_description','description','product description','商品描述','产品描述','描述'],
    'standard_price': ['standard_price','selling price','sale price','price','售价','销售价格','销售价','价格','定价'],
    'list_price': ['list_price','list price','建议零售价','含税标价'],
    'currency': ['currency','currency code','币种','货币'],
    'quantity': ['quantity','stock','inventory','库存','库存数量','可售库存','数量'],
    'fulfillment_channel': ['fulfillment_channel','fulfillment channel','fulfillment_channel_code','配送渠道'],
    'color_name': ['color_name','color','colour','color name','颜色','色名'],
    'size_name': ['size_name','size','size name','尺码','尺寸名称','大小'],
    'material_type': ['material_type','material','材质','材料'],
    'model_number': ['model_number','model number','型号','型号编号'],
    'model_name': ['model_name','model name','型号名称'],
    'part_number': ['part_number','part number','零件编号','部件编号'],
    'country_of_origin': ['country_of_origin','country of origin','原产国','原产地'],
    'product_id': ['external_product_id','product_id','product id','barcode','gtin','ean','upc','条形码','商品条码'],
    'product_id_type': ['external_product_id_type','product_id_type','product id type','条码类型'],
    'main_image_url': ['main_image_url','main image url','main image','image url','主图','主图链接','图片链接'],
    'parent_sku': ['parent_sku','parent sku','父sku','父体sku'],
    'parentage': ['parentage','parentage level','父子关系','父子级别'],
    'variation_theme': ['variation_theme','variation theme','variation theme name','变体主题'],
    'condition_type': ['condition_type','condition','商品状况'],
    'number_of_items': ['number_of_items','number of items','件数'],
}
for i in range(1, 6):
    ALIASES['bullet_point_'+str(i)] = ['bullet_point'+str(i),'bullet point '+str(i),'bullet'+str(i),'卖点'+str(i),'五点'+str(i),'要点'+str(i)]
ALIASES = {key: {norm(v) for v in values+[key]} for key,values in ALIASES.items()}
COPY_FIELDS = {'item_name','item_highlight','feed_product_type'} | {'bullet_point_'+str(i) for i in range(1,6)}
PRICE_FIELDS = {'sku','feed_product_type','update_delete','standard_price','quantity','currency','fulfillment_channel'}
NUMERIC = {'standard_price','list_price','quantity','number_of_items'}
OFFER_FIELDS = {'standard_price','list_price','quantity','currency','condition_type','fulfillment_channel'}
MARKET_CURRENCY = {'AE':'AED','SA':'SAR','US':'USD'}


def column_index(ref):
    match=re.fullmatch(r'\$?([A-Z]+)\$?([0-9]+)',ref or '')
    if not match:raise ValueError('工作簿单元格坐标无效')
    result=0
    for letter in match[1]:result=result*26+ord(letter)-64
    if result>16384:raise ValueError('工作簿列数超出 Excel 上限')
    return result-1,int(match[2])


def column_name(index):
    result='';index+=1
    while index:index,part=divmod(index-1,26);result=chr(65+part)+result
    return result


def canonical(key, label=''):
    raw=key.casefold()
    if raw.endswith('::json'):return ''
    short=re.sub(r'\[[^\]]*\]','',raw)
    # Match actual value leaves, not selector / language / currency sibling columns.
    basic={'item_name':'item_name','product_description':'product_description','manufacturer':'manufacturer',
           'model_number':'model_number','model_name':'model_name','part_number':'part_number',
           'country_of_origin':'country_of_origin','condition_type':'condition_type','number_of_items':'number_of_items'}
    leaf=re.fullmatch(r'([a-z0-9_]+)(?:#[1-5])?\.value',short)
    if leaf and leaf[1] in basic:return basic[leaf[1]]
    if re.fullmatch(r'main_product_image_locator(?:#[1-5])?\.media_location',short):return 'main_image_url'
    if raw=='::record_action':return 'update_delete'
    if short.startswith('contribution_sku'):return 'sku'
    if short.startswith('purchasable_offer') and '.our_price' in short and short.endswith('.value_with_tax'):return 'standard_price'
    if short.startswith('fulfillment_availability') and short.endswith('.quantity'):return 'quantity'
    if short.startswith('fulfillment_availability') and short.endswith('.fulfillment_channel_code'):return 'fulfillment_channel'
    if short.startswith('list_price') and short.endswith('.currency'):return 'currency'
    if short.startswith('list_price') and short.endswith('.value_with_tax'):return 'list_price'
    if short.startswith('child_parent_sku_relationship') and short.endswith('.parent_sku'):return 'parent_sku'
    if short.startswith('amzn1.volt.ca.product_id_'):return 'product_id_type' if short.endswith('_type') else 'product_id'
    base=re.split(r'[#.]',short)[0]
    roots={'product_type':'feed_product_type','brand':'brand_name','color':'color_name','size':'size_name','material':'material_type','parentage_level':'parentage','variation_theme':'variation_theme'}
    if base in ('title_differentiation','item_highlight','item_highlights') and (short==base or short.endswith('.value')):return 'item_highlight'
    if base=='bullet_point' and (short==base or short.endswith('.value')):
        number=re.search(r'#([1-5])(?:\.|$)',short)
        return 'bullet_point_'+(number[1] if number else '1')
    if base in roots and (short==base or re.fullmatch(re.escape(base)+r'(?:#[1-5])?\.'+('name' if base=='variation_theme' else 'value'),short)):return roots[base]
    for name,aliases in ALIASES.items():
        if norm(key) in aliases or norm(label) in aliases:return name
    return ''


class Workbook:
    def __init__(self,data):
        library.workbook(data,'.xlsx')
        self.data=data;self.archive=zipfile.ZipFile(io.BytesIO(data));self.cache={}
        if len(set(self.archive.namelist()))!=len(self.archive.namelist()):raise ValueError('工作簿 ZIP 含重复部件')
        if any(n.startswith('_xmlsignatures/') for n in self.archive.namelist()):raise ValueError('模板含数字签名，当前填写会使签名失效，请使用未签名的原模板')
        self.root=metadata.parse_xml(self.archive.read('xl/workbook.xml'))
        rels={r.get('Id'):r for r in metadata.parse_xml(self.archive.read('xl/_rels/workbook.xml.rels'))}
        self.paths={}
        for sheet in self.root.findall('s:sheets/s:sheet',N):
            rel=rels.get(sheet.get(metadata.R))
            if rel is None or rel.get('TargetMode')=='External':continue
            target=rel.get('Target','');target=posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/'+target)
            if not target.startswith('xl/') or '..' in target.split('/'):raise ValueError('工作表部件路径无效')
            self.paths[sheet.get('name')]=target
        self.shared=[]
        if 'xl/sharedStrings.xml' in self.archive.namelist():
            self.shared=[''.join(t.text or '' for t in item.findall('.//s:t',N)) for item in metadata.parse_xml(self.archive.read('xl/sharedStrings.xml')).findall('s:si',N)]
        self.names={n.get('name'):n.text or '' for n in self.root.findall('s:definedNames/s:definedName',N) if not n.get('localSheetId')}

    def sheet(self,name):
        if name not in self.paths:raise ValueError('模板缺少工作表：'+name)
        if name not in self.cache:
            root=metadata.parse_xml(self.archive.read(self.paths[name]));cells={}
            for cell in root.findall('s:sheetData/s:row/s:c',N):
                if cell.get('t')=='s':
                    try:value=self.shared[int(cell.findtext('s:v','',N))]
                    except (ValueError,IndexError):raise ValueError('模板共享字符串索引无效') from None
                elif cell.get('t')=='inlineStr':value=''.join(t.text or '' for t in cell.findall('.//s:t',N))
                else:value=cell.findtext('s:v','',N)
                cells[cell.get('r')]=value
            self.cache[name]=(root,cells)
        return self.cache[name]

    def options(self,formula,product_type=''):
        formula=formula.strip().lstrip('=')
        if not formula:return None
        if formula.startswith('"') and formula.endswith('"') and '&' not in formula:
            return next(csv.reader([formula[1:-1]]))
        if formula in self.names:formula=self.names[formula]
        elif formula.upper().startswith('INDIRECT('):
            suffix=re.search(r'&\s*"([^"]+)"\s*\)$',formula)
            if not suffix or not product_type:return None
            key=('_' if product_type[:1].isdigit() else '')+product_type.replace('-','_').replace(' ','')+suffix[1]
            if key not in self.names:return None
            formula=self.names[key]
        match=re.fullmatch(r"(?:'((?:[^']|'')+)'|([^!]+))!\$?([A-Z]+)\$?([0-9]+)(?::\$?([A-Z]+)\$?([0-9]+))?",formula)
        if not match:return None
        sheet=(match[1] or match[2]).replace("''", "'")
        if sheet not in self.paths:return None
        x1,y1=column_index(match[3]+match[4]);x2,y2=column_index((match[5] or match[3])+(match[6] or match[4]))
        if x2<x1 or y2<y1 or (x2-x1+1)*(y2-y1+1)>5000:return None
        cells=self.sheet(sheet)[1]
        return list(dict.fromkeys(cells.get(column_name(x)+str(y),'') for y in range(y1,y2+1) for x in range(x1,x2+1) if cells.get(column_name(x)+str(y),'')))

    def close(self):self.archive.close()


def template_info(book,marketplace,table_type):
    name=next((n for n in book.paths if n.casefold() in ('template','模板')),None)
    if not name:raise ValueError('请选择含 Template 工作表的亚马逊原始模板')
    root,cells=book.sheet(name);info=metadata.inspect(book.data)
    if info.get('marketplace') and info['marketplace']!=marketplace:raise ValueError('模板内部站点与所选地区不一致')
    header=info.get('attribute_row');start=info.get('data_row')
    if not header:
        found={column_index(ref)[1] for ref,value in cells.items() if column_index(ref)[1]<=30 and canonical(value)=='sku'}
        if len(found)!=1:raise ValueError('无法唯一识别模板技术字段行，请使用原始亚马逊模板')
        header=found.pop()
    if not start:start=header+2
    if start<=header:raise ValueError('模板数据起始行无效')
    definitions={}
    for sheet_name in book.paths:
        if sheet_name.casefold() not in ('data definitions','数据定义'):continue
        _,values=book.sheet(sheet_name);rows={}
        for ref,value in values.items():x,y=column_index(ref);rows.setdefault(y,{})[x]=value
        headers=next((r for r in rows.values() if any(v.casefold().strip()=='field name' for v in r.values()) and any(v.casefold().strip()=='required?' for v in r.values())),None)
        if headers:
            field_col=next(x for x,v in headers.items() if v.casefold().strip()=='field name');required_col=next(x for x,v in headers.items() if v.casefold().strip()=='required?')
            for r in rows.values():
                if r.get(field_col):definitions[r[field_col]]=r.get(required_col,'').strip()
    fields=[]
    expected_market=next((code for code,label in metadata.MARKETS.items() if label==marketplace),None)
    for ref,key in cells.items():
        x,y=column_index(ref)
        if y!=header or not key.strip():continue
        label=cells.get(column_name(x)+str(header-1),'') or key;name_key=canonical(key,label)
        embedded=set(re.findall(r'\[marketplace_id=([^\]]+)\]',key))
        active=not embedded or expected_market in embedded
        required=definitions.get(key,'')
        fields.append({'key':key,'column':column_name(x),'index':x,'label':label,'canonical':name_key,'defined':key in definitions, 'required':required.casefold() in ('required','必填','是'), 'conditional':bool(re.search(r'condition|条件',required,re.I)), 'active':active and (table_type!='price_update' or (name_key in PRICE_FIELDS and not key.casefold().startswith('list_price'))),'validation':[]})
    fields.sort(key=lambda f:f['index'])
    if info.get('generated_format') == 'spapi-v1':
        for field in fields:
            field['generated_format'] = info['generated']['format']
            if field['key'] in info.get('json_attributes', {}):
                field['json_attribute'] = info['json_attributes'][field['key']]
    if not fields or len(fields)>MAX_FIELDS or len({f['key'] for f in fields})!=len(fields):raise ValueError('模板字段为空、重复或超过 3000 列，请核对原模板')
    if len([f for f in fields if f['canonical']=='sku' and f['active']])!=1:raise ValueError('模板须包含唯一的 SKU 字段')
    for rule in root.findall('s:dataValidations/s:dataValidation',N):
        refs=rule.get('sqref','').split();selected=[]
        for ref in refs:
            try:
                first,last=(ref.split(':')+[ref])[:2];x1,y1=column_index(first);x2,y2=column_index(last)
            except ValueError:continue
            if y1<=start<=y2:selected.extend(f for f in fields if x1<=f['index']<=x2)
        for f in selected:
            f['validation'].append({'type':rule.get('type','none'),'operator':rule.get('operator','between'),'formula1':rule.findtext('s:formula1','',N),'formula2':rule.findtext('s:formula2','',N),'enforce':rule.get('showErrorMessage','0') in ('1','true'),'error_style':rule.get('errorStyle','stop')})
    for merge in root.findall('s:mergeCells/s:mergeCell',N):
        ref=merge.get('ref','').split(':')[-1]
        if ref and column_index(ref)[1]>=start:raise ValueError('模板数据区含合并单元格，请使用空白原始模板')
    if root.find('s:drawing',N) is not None:raise ValueError('模板数据页含嵌入图片，请使用不含商品图片的空白原模板；其他工作表图片会原样保留')
    for link in root.findall('s:hyperlinks/s:hyperlink',N):
        if column_index(link.get('ref','').split(':')[-1])[1]>=start:raise ValueError('模板数据区含旧超链接，请使用空白原始模板')
    has_data=False;formula_data=False;style_cells={};row_style={}
    for row in root.findall('s:sheetData/s:row',N):
        number=int(row.get('r','0'))
        if number<start:continue
        if not row_style and row.findall('s:c',N):row_style={k:v for k,v in row.attrib.items() if k not in ('r','spans') and not k.startswith('{')}
        for cell in row.findall('s:c',N):
            x,_=column_index(cell.get('r'));style_cells.setdefault(x,cell.get('s'))
            has_data=has_data or bool(cells.get(cell.get('r'),'')) or cell.find('s:f',N) is not None
            formula_data=formula_data or cell.find('s:f',N) is not None
    return {'sheet':name,'part':book.paths[name],'header_row':header,'data_row':start,'fields':fields,'product_types':declared_types(info),'prefilled':has_data,'formula_data':formula_data,'styles':style_cells,'row_style':row_style,'definition_fields':len(definitions),'generated_format':info.get('generated_format', '')}


def source_table(data,filename,sheet=None,header_row=None):
    tables=inventory.read_tables(data,filename)
    if not tables:raise ValueError('产品资料没有可读取的工作表')
    table=next((t for t in tables if t['name']==sheet),None) if sheet else tables[0]
    if table is None:raise ValueError('所选产品工作表不存在')
    if not table['rows']:raise ValueError('所选产品工作表为空')
    header_row=header_row or table['rows'][0][0]
    if type(header_row) is not int:raise ValueError('表头行须为正整数')
    headers=next((values for row,values in table['rows'] if row==header_row),None)
    if not headers:raise ValueError('所选表头行不存在或为空')
    rows=[(number,values) for number,values in table['rows'] if number>header_row and any(values)]
    if not rows or len(rows)>MAX_ROWS:raise ValueError('一次须提供 1–1000 行产品资料')
    return table,headers,rows,[t['name'] for t in tables],header_row


def auto_mapping(fields,headers,marketplace):
    counts={}
    for f in fields:
        if f['active'] and f['canonical']:counts[f['canonical']]=counts.get(f['canonical'],0)+1
    result={}
    for f in fields:
        if not f['active']:continue
        exact=[i for i,h in enumerate(headers) if h and h==f['key']]
        if len(exact)==1:result[f['key']]=exact[0];continue
        if len(exact)>1:continue
        visible=[i for i,h in enumerate(headers) if h and norm(h)==norm(f['label'])]
        same_label=sum(other['active'] and norm(other['label'])==norm(f['label']) for other in fields)
        if len(visible)==1 and same_label==1:result[f['key']]=visible[0];continue
        code=f['canonical']
        aliases=ALIASES.get(code,set()).copy()
        if code=='standard_price':aliases|={norm(marketplace+'价格'),norm(MARKET_CURRENCY.get(marketplace,'')+'价格')}
        candidates=[i for i,h in enumerate(headers) if norm(h) in aliases]
        if len(candidates)==1 and counts.get(code)==1:result[f['key']]=candidates[0]
    return result


def prepare(template_data,source_data,filename,marketplace,table_type,sheet=None,header_row=None,mapping=None,defaults=None,alias_rules=None,replace_existing=False,by_sku=None,copy_confirmed=False,copy_language='en'):
    if marketplace not in MARKET_CURRENCY:raise ValueError('填写支持 AE、SA 和 US 模板，请选择正确地区')
    library.validate_table_type(table_type)
    if type(replace_existing) is not bool:raise ValueError('替换数据选项无效')
    book=Workbook(template_data)
    try:
        tpl=template_info(book,marketplace,table_type);table,headers,rows,sheets,header=source_table(source_data,filename,sheet,header_row)
        allowed={f['key']:f for f in tpl['fields'] if f['active']}
        automatic=auto_mapping(tpl['fields'],headers,marketplace)
        selected=automatic if mapping is None else mapping
        if not isinstance(selected,dict) or len(selected)>MAX_FIELDS:raise ValueError('字段映射格式无效')
        selected=dict(selected);defaults={} if defaults is None else defaults
        if not isinstance(defaults,dict) or len(defaults)>MAX_FIELDS:raise ValueError('统一值格式无效')
        for key,index in selected.items():
            if key not in allowed or (index is not None and (type(index) is not int or not 0<=index<len(headers))):raise ValueError('字段映射超出本模板、地区或填写类型的范围')
        for key,value in defaults.items():
            if key not in allowed or not isinstance(value,str) or len(value)>4000:raise ValueError('统一值超出字段范围或文字过长')
            if value.strip() and selected.get(key) is not None:raise ValueError('同一字段不能同时指定来源列和统一值')
        if copy_language not in ('en','ar','zh'):raise ValueError('文案语言无效')
        by_sku={} if by_sku is None else by_sku
        if not isinstance(by_sku,dict) or len(by_sku)>MAX_ROWS:raise ValueError('逐 SKU 文案格式无效')
        if type(copy_confirmed) is not bool or (by_sku and not copy_confirmed):raise ValueError('请先核对并采用文案，再填写到模板')
        if by_sku and table_type!='upload':raise ValueError('改价模式不会填写标题、卖点或商品类目建议')
        for sku,overrides in by_sku.items():
            if not isinstance(sku,str) or not sku or len(sku)>100 or not isinstance(overrides,dict):raise ValueError('文案的 SKU 或字段格式无效')
            for key,value in overrides.items():
                field=allowed.get(key)
                if not field or field['canonical'] not in COPY_FIELDS:raise ValueError('文案只能填写标题、短卖点、五点和本模板商品类型，不能修改 SKU、价格或其他字段')
                locale=re.search(r'\[language_tag=([^\]]+)\]',key)
                if locale and field['canonical']!='feed_product_type' and locale[1].split('_')[0].split('-')[0]!=copy_language:raise ValueError('文案语言与所选模板字段的语言标签不一致')
                if not isinstance(value,str) or len(value)>(75 if field['canonical']=='item_name' else 125) or re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff]',value):raise ValueError('文案过长或包含无效字符，请核对标题 75 字符、卖点 125 字符限制')
                if field['canonical']=='feed_product_type' and value and value not in tpl['product_types']:raise ValueError('采用的商品类型超出原模板声明范围')
        if tpl['formula_data']:raise ValueError('模板数据区含公式，请使用空白原模板；当前填写不会覆盖数据区公式')
        if len(allowed)*len(rows)>500000:raise ValueError('本批模板字段与商品行合计超过 50 万格，请拆分资料')
        report,output=build(book,tpl,headers,rows,selected,defaults,marketplace,table_type,CategoryResolver(alias_rules or []),replace_existing,by_sku)
        if set(by_sku)-{r['sku'] for r in output}:raise ValueError('文案含原资料中不存在的 SKU，请重新生成或核对')
        report['reviewed_copy_skus']=list(by_sku)
        report['copy_language']=copy_language if by_sku else None
        report['mapping']={key:index for key,index in selected.items() if index is not None};report['defaults']=defaults
        report['row_sources']=[{'output_row':tpl['data_row']+i,'sku':r['sku'],'source_rows':r['source_rows']} for i,r in enumerate(output)]
        fields=[]
        for f in tpl['fields']:
            if not f['active']:continue
            item={k:f[k] for k in ('key','column','label','canonical','required','conditional')}
            item.update({k:f[k] for k in ('generated_format','json_attribute') if k in f})
            item.update(source_index=selected.get(f['key']),default=defaults.get(f['key'],''),automatic=automatic.get(f['key']))
            options=book.options(next((v['formula1'] for v in f['validation'] if v['type']=='list'),''),tpl['product_types'][0] if len(tpl['product_types'])==1 else '') or []
            item['options']=options[:60];item['options_count']=len(options);item['options_enforced']=any(v.get('enforce') for v in f['validation'] if v['type']=='list')
            fields.append(item)
        report.update(source={'filename':filename,'sheet':table['name'],'sheets':sheets,'header_row':header,'headers':headers,'warnings':table['warnings']},fields=fields,template={'sheet':tpl['sheet'],'data_row':tpl['data_row'],'header_row':tpl['header_row'],'prefilled':tpl['prefilled'],'product_types':tpl['product_types'],'generated_format':tpl.get('generated_format','')},marketplace=marketplace,table_type=table_type)
        return book,tpl,report,output
    except Exception:book.close();raise


def build(book,tpl,headers,rows,mapping,defaults,marketplace,table_type,resolver,replace_existing,by_sku=None):
    by_sku=by_sku or {}
    fields=[f for f in tpl['fields'] if f['active']];by_name={}
    for f in fields:by_name.setdefault(f['canonical'],[]).append(f)
    issues=[];evidence=[];issue_counts={'error':0,'warning':0};evidence_count=0
    def issue(level,row,sku,field,message):
        issue_counts[level]+=1
        if len(issues)<10000:issues.append({'level':level,'row':row,'sku':sku,'field':field,'message':message})
    def single(name):return by_name.get(name,[None])[0] if len(by_name.get(name,[]))==1 else None
    def get_value(values,f):
        index=mapping.get(f['key']);value=values[index] if index is not None and index<len(values) else ''
        value=str(value or defaults.get(f['key'],'')).strip()
        if re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]',value):raise ValueError('产品资料含 XML 不支持的控制字符，请清理后重试')
        return value
    sku_field=single('sku');product_field=single('feed_product_type');action_field=single('update_delete')
    original_count=len(rows);merged=[];raw_known={};origins={};source_groups={}
    sku_index=mapping.get(sku_field['key'])
    for number,values in rows:
        values=list(values);sku=values[sku_index] if sku_index is not None and sku_index<len(values) else defaults.get(sku_field['key'],'')
        prior=raw_known.get(sku) if sku else None
        compatible=prior is not None and all(not left or not right or left==right for left,right in zip(prior[1]+['']*max(0,len(values)-len(prior[1])),values+['']*max(0,len(prior[1])-len(values))))
        if compatible:
            first,previous=prior
            previous.extend(['']*max(0,len(values)-len(previous)))
            for index,value in enumerate(values):
                if value and not previous[index]:previous[index]=value;origins[first][index]=number
            source_groups[first].append(number)
        else:
            merged.append((number,values));origins[number]={index:number for index,value in enumerate(values) if value};source_groups[number]=[number]
            if sku:raw_known.setdefault(sku,(number,values))
    rows=merged
    output=[];duplicate_groups=0;known={};price_keys=[f['key'] for f in fields if f['canonical'] in ('standard_price','quantity')]
    source_currency=[i for i,h in enumerate(headers) if norm(h) in ALIASES['currency']]
    for row,values in rows:
        data={f['key']:get_value(values,f) for f in fields};sku=data.get(sku_field['key'],'')
        reviewed={key:value.strip() for key,value in by_sku.get(sku,{}).items() if value.strip()}
        data.update(reviewed)
        derived=set();product=data.get(product_field['key'],'') if product_field else '';explicit_product=bool(product)
        if product and product not in tpl['product_types'] and tpl['product_types']:
            category=resolver.canonical(product);candidates=[p for p in tpl['product_types'] if category and resolver.canonical(p)==category]
            if len(candidates)==1:product=candidates[0]
            else:issue('error',row,sku,product_field['label'],'商品类型不在本模板声明范围内；请明确选择对应类型');product=''
        if not product and not explicit_product and len(tpl['product_types'])==1:
            product=tpl['product_types'][0]
            if product_field:derived.add(product_field['key'])
        if product_field:data[product_field['key']]=product
        parent_field=single('parentage');parentage=data.get(parent_field['key'],'').casefold() if parent_field else ''
        parent=parentage in ('parent','父体','父')
        if not sku:issue('error',row,sku,'SKU','缺少 SKU，不能作为最终上传文件')
        elif len(sku)>100:issue('error',row,sku,'SKU','SKU 长度超过 100 字符，请核对模板限制')
        if table_type=='price_update':
            if not action_field:issue('error',row,sku,'Listing Action','模板没有可识别的部分更新操作字段，请使用完整的亚马逊改价模板')
            else:
                options=next((book.options(v['formula1'],product) for v in action_field['validation'] if v['type']=='list'),None)
                partial=[o for o in (options or []) if norm(o) in ('partialupdate','editpartialupdate')]
                if len(partial)==1:data[action_field['key']]=partial[0];derived.add(action_field['key'])
                elif options:issue('error',row,sku,action_field['label'],'模板未提供明确的部分更新选项，不能安全生成改价文件');data[action_field['key']]=''
                elif norm(action_field['key'])=='updatedelete':data[action_field['key']]='PartialUpdate';derived.add(action_field['key'])
                else:issue('error',row,sku,action_field['label'],'无法解析部分更新选项，请使用带原始下拉规则的改价模板');data[action_field['key']]=''
            if not any(data.get(key) for key in price_keys):issue('error',row,sku,'价格／库存','每个 SKU 至少需要明确的售价或库存数量')
            channel=single('fulfillment_channel');quantity=single('quantity')
            if quantity and data.get(quantity['key']) and channel and not data.get(channel['key']):issue('error',row,sku,channel['label'],'更新库存时须明确提供配送渠道，不能从数量推测卖家自配送或 FBA')
            source_parent=[i for i,h in enumerate(headers) if norm(h) in ALIASES['parentage']]
            if any(i<len(values) and values[i].casefold() in ('parent','父','父体') for i in source_parent):issue('error',row,sku,'父体','父体不能更新报价或库存，请只提供可售子体或独立 SKU')
        elif action_field and not data.get(action_field['key']):
            options=next((book.options(v['formula1'],product) for v in action_field['validation'] if v['type']=='list'),None)
            full=[o for o in (options or []) if norm(o) in ('update','createorreplacefullupdate','fullupdate')]
            if len(full)==1:data[action_field['key']]=full[0];derived.add(action_field['key'])
        if action_field and norm(data.get(action_field['key'],'')) in ('delete','删除','deleteoffer'):
            issue('error',row,sku,action_field['label'],'填写功能不生成删除操作；该操作已留空，请改用新增或部分更新')
            data[action_field['key']]=''
        if source_currency and len(source_currency)==1 and source_currency[0]<len(values) and values[source_currency[0]].strip():
            if values[source_currency[0]].strip().upper()!=MARKET_CURRENCY[marketplace]:issue('error',row,sku,'币种','产品资料币种与目标站点不一致，不会自动换汇')
        for f in fields:
            key=f['key'];code=f['canonical'];value=data[key]
            if parent and code in OFFER_FIELDS:
                if value:issue('warning',row,sku,f['label'],'父体不填写报价与库存；已在副本中留空')
                data[key]='';continue
            if code=='currency' and not value and not parent and not tpl.get('generated_format'):value=MARKET_CURRENCY[marketplace];data[key]=value;derived.add(key)
            if value:
                for rule in f['validation']:
                    if rule['type']=='list':
                        options=book.options(rule['formula1'],product)
                        if options is None and rule.get('enforce'):issue('warning',row,sku,f['label'],'此字段的动态下拉规则未能解析，请人工核对当前商品类型')
                        elif options and value not in options and rule.get('enforce'):
                            matches=[v for v in options if v.casefold()==value.casefold()]
                            if len(matches)==1:value=matches[0];data[key]=value
                            else:issue('error' if rule.get('error_style')=='stop' else 'warning',row,sku,f['label'],'值不在模板当前下拉选项中')
                    elif rule['type'] not in ('none',''):issue('warning',row,sku,f['label'],'模板另有 '+rule['type']+' 规则，请人工核对；规则会原样保留')
                if code in NUMERIC:
                    try:
                        if not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)',value):raise InvalidOperation()
                        number=Decimal(value)
                        if not number.is_finite() or number<0 or (code in ('quantity','number_of_items') and number!=number.to_integral_value()):raise InvalidOperation()
                        if code in ('standard_price','list_price','number_of_items') and number<=0:raise InvalidOperation()
                        if len(value)>30 or len(number.normalize().as_tuple().digits)>15:raise InvalidOperation()
                        data[key]=format(number,'f')
                    except InvalidOperation:
                        data[key]=''
                        issue('error',row,sku,f['label'],'无效数字未写入；请提供非负整数数量或大于零、有效数字不超过 15 位的价格，不含货币符号与千位分隔')
                if code=='fulfillment_channel' and value in ('DEFAULT_AE','AMAZON_AE','DEFAULT_SA','AMAZON_SA','DEFAULT_US','AMAZON_US') and not value.endswith('_'+marketplace):issue('error',row,sku,f['label'],'配送渠道所属地区与所选站点不一致')
                if code=='currency' and value.upper()!=MARKET_CURRENCY[marketplace]:issue('error',row,sku,f['label'],'币种须与 '+marketplace+' 站点一致')
                if code in ('item_name',) and len(value)>75:issue('warning',row,sku,f['label'],'标题超过 75 字符，请核对并精简；未自动删除产品事实')
                if (code.startswith('bullet_point_') or code=='item_highlight') and len(value)>125:issue('warning',row,sku,f['label'],'卖点超过 125 字符，请核对并精简')
                if code.endswith('image_url') and not re.fullmatch(r'https?://[^\s]+',value):issue('error',row,sku,f['label'],'主图须提供可访问的 HTTP/HTTPS 链接；不会上传内嵌图片')
                index=mapping.get(key)
                evidence_count+=bool(data[key])
                if data[key] and len(evidence)<5000:evidence.append({'source_row':origins.get(row,{}).get(index,row),'sku':sku,'target_field':key,'source_header':headers[index] if index is not None and key not in derived and key not in reviewed else '', 'source_column':column_name(index) if index is not None and key not in derived and key not in reviewed else '', 'method':'reviewed_copy' if key in reviewed else 'template_or_marketplace' if key in derived else 'source' if index is not None and index<len(values) and values[index] else 'explicit_default' if defaults.get(key) else 'template_or_marketplace'})
            required=(f['required'] or code in ('sku','feed_product_type','update_delete')) if table_type=='upload' else code in ('sku','feed_product_type','update_delete')
            if required and not data[key]:issue('error',row,sku,f['label'],'缺少必填字段')
            if f['conditional'] and table_type=='upload' and code not in ('parentage','parent_sku','variation_theme','color_name','size_name'):issue('warning',row,sku,f['label'],'模板标记为条件必填，需要结合实际商品人工确认')
        if reviewed:
            bullet_fields=[f for f in fields if f['canonical'].startswith('bullet_point_')]
            count=sum(bool(data[f['key']]) for f in bullet_fields)
            if bullet_fields and count<len(bullet_fields):issue('warning',row,sku,'五点描述','当前仅有 '+str(count)+'/'+str(len(bullet_fields))+' 个卖点；资料不足时保持空白，请补充真实信息')
        if sku and sku in known:
            prior=known[sku];conflicts=[k for k,v in data.items() if v and prior['values'].get(k) and v!=prior['values'][k]]
            if not conflicts:
                for k,v in data.items():
                    if v:prior['values'][k]=v
                prior['source_rows'].extend(source_groups[row]);duplicate_groups+=1;continue
            issue('error',row,sku,'SKU','相同 SKU 的字段内容冲突，草稿保留重复行，请先核对资料')
        record={'sku':sku,'values':data,'source_rows':source_groups[row]};output.append(record)
        if sku:known.setdefault(sku,record)
    check_variations(output,fields,issue)
    if tpl['prefilled'] and not replace_existing:issue('error',0,'','模板已有数据','请确认在生成副本中替换旧数据，原模板库文件不会修改')
    if not tpl['definition_fields'] and table_type=='upload':issue('warning',0,'','模板校验','模板没有可读取的 Data Definitions，无法完整验证类目必填要求')
    elif table_type=='upload':
        for f in fields:
            if not f['defined']:issue('warning',0,'',f['label'],'Data Definitions 未说明此字段要求，请人工核对')
    report={'source_rows':original_count,'output_rows':len(output),'merged_rows':original_count-len(output),'written_cells':sum(bool(v) for r in output for v in r['values'].values()),'issues':issues,'evidence':evidence,'preview_rows':[{'sku':r['sku'],'source_rows':r['source_rows'],'values':r['values']} for r in output[:5]],'notes':['只填写所选店铺、地区和表格类型的模板副本，不修改模板库原件。','不猜测缺失的品牌、条码、尺寸、电池或合规声明；未完成的项见问题清单。','仅检查可解析的模板要求；最终上传结果以亚马逊处理报告为准。','内嵌图片不自动上传，主图字段需提供图片链接。']}
    report['errors']=issue_counts['error'];report['warnings']=issue_counts['warning'];report['issues_truncated']=sum(issue_counts.values())-len(issues);report['evidence_truncated']=evidence_count-len(evidence);report['status']='draft' if any(issue_counts.values()) else 'ready'
    return report,output


def check_variations(rows,fields,issue):
    names={}
    for f in fields:
        if f['canonical']:names.setdefault(f['canonical'],[]).append(f['key'])
    def value(row,name):return row['values'].get(names[name][0],'') if len(names.get(name,[]))==1 else ''
    parents={r['sku']:r for r in rows if value(r,'parentage').casefold() in ('parent','父体','父')};seen=set()
    for row in rows:
        level=value(row,'parentage').casefold();theme=value(row,'variation_theme');parent=value(row,'parent_sku');number=row['source_rows'][0]
        if not level:
            if theme or parent:issue('error',number,row['sku'],'变体','提供了父 SKU 或变体主题，但未明确父子关系')
            continue
        if level not in ('parent','child','父','父体','子','子体'):issue('error',number,row['sku'],'父子关系','请使用模板允许的父体或子体选项');continue
        if not theme:issue('error',number,row['sku'],'变体主题','父体和子体都必须明确提供一致的变体主题')
        normalized=norm(theme).replace('name','')
        if normalized not in ('size','color','sizecolor','colorsize') and theme:issue('error',number,row['sku'],'变体主题','当前填写仅校验 SIZE／COLOR 变体，请人工处理其他主题')
        if level in ('child','子','子体'):
            if not parent:issue('error',number,row['sku'],'父 SKU','子体缺少父 SKU')
            if parent==row['sku']:issue('error',number,row['sku'],'父 SKU','子体不能关联自身')
            if parent in parents and value(parents[parent],'variation_theme')!=theme:issue('error',number,row['sku'],'变体主题','父子体的变体主题不一致')
            elif parent and parent not in parents:issue('warning',number,row['sku'],'父 SKU','父体不在本次资料中，请确认已经存在且变体主题一致')
            color=value(row,'color_name');size=value(row,'size_name')
            if 'color' in normalized and not color:issue('error',number,row['sku'],'颜色','颜色变体缺少颜色')
            if 'size' in normalized and not size:issue('error',number,row['sku'],'尺码','尺寸变体缺少区分值')
            combination=(parent,theme,color,size)
            if combination in seen:issue('error',number,row['sku'],'变体','同一父体下的变体组合重复')
            seen.add(combination)


def render(book,tpl,rows):
    raw=book.archive.read(tpl['part'])
    match=re.search(rb'(<(?P<prefix>[A-Za-z_][\w.-]*:)?sheetData\b[^>]*>)(?P<body>.*?)(</(?:[A-Za-z_][\w.-]*:)?sheetData\s*>)',raw,re.S)
    if not match:raise ValueError('模板数据区域无法安全定位')
    prefix=(match['prefix'] or b'').decode();body=match['body'];row_pattern=re.compile(rb'<(?:[A-Za-z_][\w.-]*:)?row\b[^>]*(?:/>|>.*?</(?:[A-Za-z_][\w.-]*:)?row\s*>)',re.S)
    preserved=[]
    for row in row_pattern.finditer(body):
        number=re.search(rb'\br=["\']([0-9]+)["\']',row.group())
        if not number:raise ValueError('模板行号缺失')
        if int(number[1])<tpl['data_row']:preserved.append(row.group())
    expected={};fields={f['key']:f for f in tpl['fields']};generated=[]
    for offset,record in enumerate(rows):
        number=tpl['data_row']+offset;attrs={'r':str(number),**tpl['row_style']};parts=['<'+prefix+'row '+ ' '.join(k+'='+quoteattr(v) for k,v in attrs.items())+'>']
        for f in tpl['fields']:
            key=f['key'];value=record['values'].get(key,'');ref=f['column']+str(number);style=tpl['styles'].get(f['index']);style_attr=(' s='+quoteattr(style)) if style is not None else ''
            if not value:
                if style is not None:parts.append('<'+prefix+'c r="'+ref+'"'+style_attr+'/>')
                continue
            numeric=f['canonical'] in NUMERIC and bool(re.fullmatch(r'\d+(?:\.\d+)?',value)) and len(Decimal(value).normalize().as_tuple().digits)<=15
            if numeric:cell='<'+prefix+'c r="'+ref+'"'+style_attr+'><'+prefix+'v>'+value+'</'+prefix+'v></'+prefix+'c>'
            else:cell='<'+prefix+'c r="'+ref+'"'+style_attr+' t="inlineStr"><'+prefix+'is><'+prefix+'t xml:space="preserve">'+escape(value)+'</'+prefix+'t></'+prefix+'is></'+prefix+'c>'
            parts.append(cell);expected[ref]=value
        parts.append('</'+prefix+'row>');generated.append(''.join(parts).encode())
    changed=raw[:match.start('body')]+b''.join(preserved+generated)+raw[match.end('body'):]
    last=max((f['index'] for f in tpl['fields']),default=0);end=tpl['data_row']+len(rows)-1
    changed=re.sub(rb'(<(?:[A-Za-z_][\w.-]*:)?dimension\b[^>]*\bref=)["\'][^"\']+["\']',lambda m:m[1]+b'"A1:'+column_name(last).encode()+str(end).encode()+b'"',changed,count=1)
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as dest:
        dest.comment=book.archive.comment
        for part in book.archive.infolist():dest.writestr(copy.copy(part),changed if part.filename==tpl['part'] else book.archive.read(part.filename))
    data=out.getvalue();verified=Workbook(data)
    try:
        if verified.archive.namelist()!=book.archive.namelist():raise ValueError('生成文件部件清单变化，未交付文件')
        if any(verified.archive.read(n)!=book.archive.read(n) for n in book.archive.namelist() if n!=tpl['part']):raise ValueError('出现数据区之外的工作簿部件变化，未交付文件')
        root,cells=verified.sheet(tpl['sheet'])
        if any(cells.get(ref)!=value for ref,value in expected.items()):raise ValueError('填写后的单元格回读不一致，未交付文件')
        actual={ref:v for ref,v in cells.items() if column_index(ref)[1]>=tpl['data_row'] and v}
        if actual!=expected:raise ValueError('数据区存在非本批次的残留内容，未交付文件')
        if root.find('s:sheetData',N) is None:raise ValueError('填写后的工作表结构无效')
    finally:verified.close()
    return data


def export(template_data,source_data,filename,marketplace,table_type,output_name,strict=False,**kwargs):
    if type(strict) is not bool:raise ValueError('导出校验选项无效')
    book,tpl,report,rows=prepare(template_data,source_data,filename,marketplace,table_type,**kwargs)
    try:
        if tpl['prefilled'] and not kwargs.get('replace_existing'):raise ValueError('请先确认在副本中替换原有模板数据')
        if strict and report['status']!='ready':raise ValueError('仍有错误或待人工确认项，请先处理问题清单，或选择生成草稿')
        workbook=render(book,tpl,rows);report['readback_verified']=True
        suffix='草稿' if report['status']=='draft' else '已填写'
        stem,extension=output_name.rsplit('.',1);workbook_name=stem+'_'+suffix+'.'+extension
        report['output_name']=workbook_name
        buffer=io.StringIO();writer=csv.writer(buffer);writer.writerow(['级别','源表行号','SKU','字段','问题'])
        for i in report['issues']:writer.writerow(["'"+str(v) if re.match(r'^\s*[=+\-@]',str(v)) else str(v) for v in (i['level'],i['row'],i['sku'],i['field'],i['message'])])
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as z:
            z.writestr(workbook_name,workbook);z.writestr('填写检查报告.json',json.dumps(report,ensure_ascii=False,indent=2));z.writestr('待核对问题.csv',('\ufeff'+buffer.getvalue()).encode())
        return output.getvalue(),stem+'_填写结果.zip',report
    finally:book.close()


def inspect_source(data,filename):
    tables=inventory.read_tables(data,filename)
    candidates=[]
    for table in tables:
        for number,values in table['rows'][:30]:
            sku=sum(norm(v) in ALIASES['sku'] for v in values)
            score=(10 if sku==1 else 0)+sum(any(norm(v) in names for names in ALIASES.values()) for v in values)
            candidates.append((score,table,number,values))
    if not candidates:raise ValueError('产品资料没有可读取的行')
    _,table,number,headers=max(candidates,key=lambda item:item[0])
    return {'sheets':[t['name'] for t in tables],'sheet':table['name'],'header_row':number,'headers':headers}
