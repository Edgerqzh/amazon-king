"""Read Amazon's template source identity without exposing raw contributor IDs."""
import base64
import hashlib
import io
import json
import re
from urllib.parse import parse_qs
import xml.etree.ElementTree as ET
import zipfile
from .product_types import MAX_PRODUCT_TYPES, PRODUCT_TYPE_PATTERN, canonical_category

PRODUCT_TYPES_VERSION = 3

N = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
MARKETS = {'A2VIGQ35RCS4UG': 'AE', 'A17E79C6D8DWNP': 'SA', 'ATVPDKIKX0DER': 'US', 'A1F83G8C2ARO7P': 'UK', 'A1PA6795UKMFR9': 'DE', 'A13V1IB3VIYZZH': 'FR', 'A1VC38T7YXB528': 'JP', 'A2EUQ1WTGCTBG2': 'CA', 'A39IBJ37TRP1C6': 'AU'}


def parse_xml(raw):
    if re.search(br'<!\s*(DOCTYPE|ENTITY)', raw, re.I):
        raise ValueError('模板 XML 不支持 DTD/实体定义')
    return ET.fromstring(raw)


def inspect(data, include_seller=False):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        if 'xl/_rels/workbook.xml.rels' not in z.namelist():
            return {}
        workbook = parse_xml(z.read('xl/workbook.xml'))
        sheet = next((s for s in workbook.findall('s:sheets/s:sheet', N) if s.get('name', '').casefold() in ('template', '模板')), None)
        if sheet is None:
            return {}
        rel = next((r for r in parse_xml(z.read('xl/_rels/workbook.xml.rels')) if r.get('Id') == sheet.get(R) and r.get('TargetMode') != 'External'), None)
        if rel is None:
            return {}
        import posixpath
        target = rel.get('Target', '')
        target = posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join('xl', target))
        if not target.startswith('xl/') or '..' in target.split('/'):
            raise ValueError('模板工作表路径无效')
        sheet_xml = parse_xml(z.read(target))
        shared = None
        def value(cell):
            nonlocal shared
            if cell.get('t') == 's':
                if shared is None:
                    shared = [''.join(t.text or '' for t in n.findall('.//s:t', N)) for n in parse_xml(z.read('xl/sharedStrings.xml')).findall('s:si', N)]
                return shared[int(cell.findtext('s:v', '', N))]
            if cell.get('t') == 'inlineStr':
                return ''.join(t.text or '' for t in cell.findall('.//s:t', N))
            return cell.findtext('s:v', '', N)
        settings = next((value(c) for c in sheet_xml.findall('s:sheetData/s:row/s:c', N) if c.get('r') == 'A1'), '')
        if not settings.startswith('settings='):
            return {}
        raw = {k: v[0] for k, v in parse_qs(settings[len('settings='):]).items()}
        contributor = raw.get('contributorId', '')
        valid = bool(re.fullmatch(r'amzn1\.cr\.o\.[A-Za-z0-9_-]{1,100}', contributor))
        result = {'account_fingerprint': hashlib.sha256(('amazon-contributor:'+contributor).encode()).hexdigest() if valid else '', 'account_hint': '…'+contributor[-5:] if valid else '', 'marketplace': MARKETS.get(raw.get('primaryMarketplaceId', '').split('.')[-1], ''), 'generated_at': raw.get('timestamp', ''), 'product_types': [], 'data_row': None, 'attribute_row': None, 'field_count': 0, 'has_prefilled_data': False}
        if include_seller and valid:
            result['seller_id']=contributor[len('amzn1.cr.o.'):]
        result['product_type_warnings'] = []
        if raw.get('ptds'):
            try:
                types = base64.b64decode(raw['ptds'], validate=True).decode().strip()
                parsed = json.loads(types) if types.startswith('[') else [types]
                if not isinstance(parsed, list):
                    raise ValueError('not a list')
                valid_types = list(dict.fromkeys(p for p in parsed if isinstance(p, str) and PRODUCT_TYPE_PATTERN.fullmatch(p)))
                result['product_types'] = valid_types[:MAX_PRODUCT_TYPES]
                if len(valid_types) > MAX_PRODUCT_TYPES:
                    result['product_type_warnings'].append('模板声明商品类型超过 '+str(MAX_PRODUCT_TYPES)+' 种，仅使用前 '+str(MAX_PRODUCT_TYPES)+' 种，请人工核对其余类型')
                if len(valid_types) != len(set(p for p in parsed if isinstance(p, str))) or any(not isinstance(p, str) for p in parsed):
                    result['product_type_warnings'].append('部分商品类型格式无效，未用于匹配')
            except (ValueError, UnicodeDecodeError, TypeError):
                result['product_type_warnings'].append('模板声明的商品类型清单无法解析，请人工核对适用范围')
        result['product_type_labels'] = {code: canonical_category(code) or code for code in result['product_types']}
        for field, key in [('data_row', 'dataRow'), ('attribute_row', 'attributeRow')]:
            if raw.get(key, '').isdigit() and 0 < int(raw[key]) < 10000:
                result[field] = int(raw[key])
        for row in sheet_xml.findall('s:sheetData/s:row', N):
            number = int(row.get('r', '0'))
            if number == result['attribute_row']:
                result['field_count'] = sum(bool(value(c)) for c in row.findall('s:c', N))
            if result['data_row'] and number >= result['data_row'] and any(value(c) or c.find('s:f', N) is not None for c in row.findall('s:c', N)):
                result['has_prefilled_data'] = True
        if raw.get('generator') == 'spapi-fill-v1':
            # Only this versioned format can declare advanced complete-JSON columns.
            # Never publish the embedded schema, contributor ID or arbitrary settings extras.
            from . import schema_utils
            manifest_sheet = next((s for s in workbook.findall('s:sheets/s:sheet', N) if s.get('name') == schema_utils.MANIFEST_SHEET), None)
            manifest_rel = next((r for r in parse_xml(z.read('xl/_rels/workbook.xml.rels')) if manifest_sheet is not None and r.get('Id') == manifest_sheet.get(R) and r.get('TargetMode') != 'External'), None)
            if manifest_rel is None:raise ValueError('生成模板缺少规则清单')
            target = manifest_rel.get('Target', '')
            target = posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join('xl', target))
            if not target.startswith('xl/') or '..' in target.split('/') or z.getinfo(target).file_size > 32 * 1024 * 1024:
                raise ValueError('生成模板规则清单路径或大小无效')
            chunks = []
            for row in parse_xml(z.read(target)).findall('s:sheetData/s:row', N):
                cells = row.findall('s:c', N)
                if len(cells) != 1 or cells[0].find('s:f', N) is not None:raise ValueError('生成模板规则清单格式无效')
                chunk = value(cells[0])
                if len(chunk) >= 30000:raise ValueError('生成模板规则清单单元格过长')
                chunks.append(chunk)
            encoded = ''.join(chunks).encode('utf-8')
            if not re.fullmatch(r'[0-9a-f]{64}', raw.get('manifestSha256', '')) or hashlib.sha256(encoded).hexdigest() != raw['manifestSha256']:
                raise ValueError('生成模板规则清单哈希不符')
            manifest = schema_utils.strict_json(encoded, limit=24 * 1024 * 1024)
            if not isinstance(manifest, dict) or manifest.get('format') != schema_utils.GENERATED_FORMAT or manifest.get('marketplace') != result['marketplace'] or result['product_types'] != [manifest.get('product_type')]:
                raise ValueError('生成模板规则清单与模板来源不一致')
            if manifest.get('schema_sha256') != raw.get('schemaSha256') or not re.fullmatch(r'[0-9a-f]{64}', raw.get('schemaSha256', '')):
                raise ValueError('生成模板规则哈希缺失或不一致')
            declarations = manifest.get('fields')
            technical = [value(c) for row in sheet_xml.findall('s:sheetData/s:row', N) if int(row.get('r', '0')) == result['attribute_row'] for c in row.findall('s:c', N)]
            if not isinstance(declarations, list) or len(declarations) > 3000 or any(not isinstance(f, dict) for f in declarations) or [f.get('key') for f in declarations] != technical:
                raise ValueError('生成模板技术字段与规则清单不一致')
            json_fields = {}
            for field in declarations:
                key, name = field.get('key'), field.get('json_attribute')
                if name is not None:
                    if not isinstance(name, str) or not schema_utils.NAME.fullmatch(name) or key != name + '::json':raise ValueError('生成模板 JSON 属性声明无效')
                    if any(other != key and re.match(re.escape(name) + r'(?:\[|#|\.)', other) for other in technical):raise ValueError('生成模板 JSON 与标量字段冲突')
                    json_fields[key] = name
            result['generated_format'] = 'spapi-v1'
            result['generated'] = {'format': schema_utils.GENERATED_FORMAT, 'purpose': 'software_api_listing', 'schema_sha256': raw['schemaSha256'],
                                   'manifest_sha256': raw['manifestSha256'], 'version': str(raw.get('productTypeVersion', ''))[:160]}
            result['json_attributes'] = json_fields
        return result
