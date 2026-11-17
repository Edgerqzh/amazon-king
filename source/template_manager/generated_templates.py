"""Official SP-API schema -> immutable blank API-workflow workbook.

Generation only performs GETs. Saving is an explicit, provenance-checked library add;
there is no alternate Amazon submission path here.
"""
import base64
import copy
import datetime
import hashlib
import io
import json
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlencode
from xml.sax.saxutils import escape, quoteattr
import zipfile

from . import library, schema_utils, spapi

TOKEN_LIFE = 600
MAX_TOKENS = 16
MAX_BYTES = 32 * 1024 * 1024
XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
NOTICE = '本软件 API 上架填写表 — 不是 Seller Central 原版 Excel，不能直接上传卖家后台。'


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _column(index):
    result = ''
    while index:
        index, part = divmod(index - 1, 26)
        result = chr(65 + part) + result
    return result


def _text(value):
    value = str(value)
    if len(value) > 30000 or re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff]', value):
        raise ValueError('规则文字过长或包含工作簿不支持的字符')
    return escape(value)


def _xlsx(sheets, validations=None, defined_names=None):
    """Narrow OOXML writer: all cells inline text; no formulas, macros or external parts.

    Only internally generated enum named ranges appear in validation formula1 elements.
    """
    validations, defined_names = validations or {}, defined_names or {}
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        def put(path, content):archive.writestr(path, ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + content).encode('utf-8'))
        overrides = ''.join('<Override PartName="/xl/worksheets/sheet%d.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % i for i in range(1, len(sheets) + 1))
        put('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>' + overrides + '</Types>')
        put('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        names = '<definedNames>' + ''.join('<definedName name=' + quoteattr(name) + '>' + escape(ref) + '</definedName>' for name, ref in defined_names.items()) + '</definedNames>' if defined_names else ''
        put('xl/workbook.xml', '<workbook xmlns="' + NS + '" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>' + ''.join('<sheet name=' + quoteattr(name) + ' sheetId="%d" r:id="rId%d"/>' % (i, i) for i, (name, _) in enumerate(sheets, 1)) + '</sheets>' + names + '</workbook>')
        put('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + ''.join('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet%d.xml"/>' % (i, i) for i in range(1, len(sheets) + 1)) + '<Relationship Id="styles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        put('xl/styles.xml', '<styleSheet xmlns="' + NS + '"><fonts count="1"><font><sz val="11"/><name val="Arial"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="49" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="49" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment wrapText="1" vertical="top"/></xf></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
        for i, (name, rows) in enumerate(sheets, 1):
            body = []
            for row, values in enumerate(rows, 1):
                header = (name == 'Template' and row == 2) or (name not in ('Template', schema_utils.MANIFEST_SHEET, 'Enums') and row == 1)
                cells = ''.join('<c r="%s%d" t="inlineStr" s="%d"><is><t xml:space="preserve">%s</t></is></c>' % (_column(col), row, int(header), _text(value)) for col, value in enumerate(values, 1) if value != '')
                body.append('<row r="%d"%s>%s</row>' % (row, ' ht="48" customHeight="1"' if header else '', cells))
            rules = validations.get(name, [])
            validation_xml = '<dataValidations count="%d">' % len(rules) + ''.join('<dataValidation type="list" allowBlank="1" showErrorMessage="1" errorStyle="stop" sqref=' + quoteattr(ref) + '><formula1>' + escape(formula) + '</formula1></dataValidation>' for ref, formula in rules) + '</dataValidations>' if rules else ''
            frozen = 4 if name == 'Template' else 1
            put('xl/worksheets/sheet%d.xml' % i, '<worksheet xmlns="' + NS + '"><sheetViews><sheetView workbookViewId="0"><pane ySplit="%d" topLeftCell="A%d" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><sheetFormatPr defaultRowHeight="15"/><cols><col min="1" max="%d" width="28" customWidth="1"/></cols><sheetData>' % (frozen, frozen + 1, max([len(r) for r in rows] or [1])) + ''.join(body) + '</sheetData>' + validation_xml + '</worksheet>')
    return out.getvalue()


def build_artifacts(definition, seller, token, generated_at):
    """Pure helper for fixtures: return (preview, {kind: (bytes, filename, MIME)})."""
    marketplace, product_type = definition['marketplace'], definition['product_type']
    market = spapi.marketplace_id(marketplace)
    fields, warnings = schema_utils.compile_fields(definition['schema'], market)
    fields[1]['options'] = [product_type]
    manifest = {'format': schema_utils.GENERATED_FORMAT, 'marketplace': marketplace, 'product_type': product_type,
                'version': definition['version'], 'generated_at': generated_at, 'schema_sha256': definition['schema_sha256'],
                'fields': [{k:f[k] for k in ('key', 'json_attribute') if k in f} for f in fields], 'schema': definition['schema']}
    manifest_bytes = _json(manifest)
    manifest_text = manifest_bytes.decode()
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    settings = 'settings=' + urlencode({'contributorId': 'amzn1.cr.o.' + seller, 'primaryMarketplaceId': market,
                                       'ptds': base64.b64encode(_json([product_type])).decode(), 'attributeRow': 3, 'dataRow': 5,
                                       'timestamp': generated_at, 'generator': schema_utils.GENERATED_FORMAT,
                                       'schemaSha256': definition['schema_sha256'], 'manifestSha256': manifest_hash,
                                       'productTypeVersion': definition['version']['number']})
    enums, names, rules = [], {}, []
    for index, field in enumerate(fields, 1):
        options = field.get('options')
        if not options:continue
        column = len(enums) + 1
        enums.append(options)
        name = 'spapi_enum_%d' % column
        names[name] = "'Enums'!$%s$1:$%s$%d" % (_column(column), _column(column), len(options))
        rules.append(('%s5:%s1004' % (_column(index), _column(index)), name))
    enum_rows = [[options[row] if row < len(options) else '' for options in enums] for row in range(max([len(v) for v in enums] or [0]))]
    definitions = [['Field Name', 'Required?', 'Label', 'Value Type', 'Description']]
    definitions += [[f['key'], 'Required' if f['required'] else 'Conditionally Required' if f['conditional'] else 'Optional', f['label'], f['value_type'], f['description']] for f in fields]
    readme = [[NOTICE], ['填写说明 / Legend'], ['只编辑 Template 第5行起的数据区；第3行是技术字段，不能改名。'],
              ['也可填写配套资料空表：第一行机器字段，第二行起填写自己的商品资料，再导入本软件。'],
              ['必填：所有商品均需要；条件必填：仅满足亚马逊条件时需要，不代表全部必填。'],
              ['高级 JSON 列须填写完整属性；对象、数组、数字、布尔值须保持 JSON 类型。'],
              ['没有示例商品行：避免虚构 SKU、品牌或其他商品事实；SKU 必须由操作者提供。'],
              ['结构示意（不是商品事实）：数组写为 [{"value":"你自己的文字"}]；布尔值写 true / false，数字不加引号。'],
              ['保存并去填写 → 检查 → Amazon VALIDATION_PREVIEW → 逐 SKU 人工确认提交；生成/下载/保存不会上架。']]
    source = [['Source', 'Value'], ['Purpose', NOTICE], ['Product type', product_type], ['Display name', definition['display_name']],
              ['Marketplace', marketplace], ['Version', definition['version']['number']], ['Latest at generation', str(definition['version']['latest'])],
              ['Generated UTC', generated_at], ['Schema SHA256', definition['schema_sha256']], ['Schema checksum (Base64 MD5)', definition.get('checksum', '')],
              ['Official API', '/definitions/2020-09-01/productTypes/' + product_type], ['Requirements', 'LISTING / ENFORCED'], ['Schema resolution', 'Local references only; unsupported shapes use full JSON.']]
    source += [['Warning', warning] for warning in warnings]
    sheets = [('Template', [[settings], [f['label'] for f in fields], [f['key'] for f in fields], []]),
              ('Data Definitions', definitions), ('Enums', enum_rows), ('Read Me', readme), ('Sources', source),
              (schema_utils.MANIFEST_SHEET, [[manifest_text[i:i + 28000]] for i in range(0, len(manifest_text), 28000)])]
    template = _xlsx(sheets, {'Template': rules}, names)
    source_book = _xlsx([('Products', [[f['key'] for f in fields]]), ('Read Me', readme)])
    base = 'API填写表_%s_%s_%s' % (marketplace, product_type, token[:10])
    preview = {'marketplace': marketplace, 'product_type': product_type, 'display_name': definition['display_name'],
               'version': copy.deepcopy(definition['version']), 'generated_at': generated_at, 'expires_in': TOKEN_LIFE,
               'fields': [{k:v for k,v in f.items() if k != 'json_attribute'} for f in fields], 'field_count': len(fields),
               'required_count': sum(f['required'] for f in fields), 'conditional_count': sum(f['conditional'] for f in fields),
               'json_count': sum(f['value_type'] == 'json' for f in fields), 'warnings': warnings, 'filename': base + '.xlsx'}
    return preview, {'template': (template, base + '.xlsx', XLSX_MIME), 'source': (source_book, '资料空表_' + base + '.xlsx', XLSX_MIME),
                     'schema': (manifest_bytes, base + '_schema.json', 'application/json; charset=utf-8')}


class Service:
    def __init__(self, client, definitions, root, library_lock, clock=time.monotonic, resolve=None):
        self.client, self.definitions, self.root, self.library_lock = client, definitions, Path(root), library_lock
        self.resolve = resolve  # store id -> Client; without it every request uses `client`
        self.clock = clock
        self.lock = threading.Lock()
        self.items = {}

    @staticmethod
    def _user(user):
        if not isinstance(user, dict) or user.get('role') not in ('admin', 'editor') or not isinstance(user.get('username'), str) or not user['username']:
            raise PermissionError('仅管理员或编辑成员可生成、下载或保存填写模板')
        return user['username']

    @staticmethod
    def _body(body):
        if not isinstance(body, dict):raise ValueError('请求内容必须为对象')
        return body

    def _client(self, body):
        return self.resolve(body.get('store')) if self.resolve else self.client

    def _purge(self):
        for token in list(self.items):
            if self.items[token]['expires'] <= self.clock():del self.items[token]

    def search(self, user, body):
        self._user(user);self._body(body)
        return self.definitions.search(self._client(body), body.get('marketplace'), body.get('query'), body.get('mode', 'keywords'), body.get('search_locale', 'zh_CN'))

    def generate(self, user, body):
        owner = self._user(user);self._body(body)
        marketplace, product_type = body.get('marketplace'), body.get('product_type')
        spapi.marketplace_id(marketplace)
        client = self._client(body)
        seller = client.seller_id()
        if not isinstance(seller, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', seller):raise ValueError('当前连接没有可用的卖家来源标识')
        definition = self.definitions.definition(client, product_type, marketplace, force=True, strict=True)
        if not definition.get('strict_verified'):raise spapi.SpApiError('未取得已校验的官方规则，不能生成')
        token = secrets.token_urlsafe(32)
        generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='microseconds')
        preview, artifacts = build_artifacts(definition, seller, token, generated_at)
        if client.seller_id() != seller:raise ValueError('连接卖家已变更，请重新生成')
        size = sum(len(a[0]) for a in artifacts.values())
        if size > MAX_BYTES:raise ValueError('生成文件超过内存上限，请使用更小的商品类型规则')
        with self.lock:
            self._purge()
            while self.items and (len(self.items) >= MAX_TOKENS or sum(v['size'] for v in self.items.values()) + size > MAX_BYTES):
                del self.items[next(iter(self.items))]
            self.items[token] = {'owner': owner, 'seller': seller, 'marketplace': marketplace, 'product_type': product_type,
                                 'schema_sha256': definition['schema_sha256'], 'hashes': {kind:hashlib.sha256(a[0]).hexdigest() for kind,a in artifacts.items()},
                                 'expires': self.clock() + TOKEN_LIFE, 'size': size, 'artifacts': artifacts,
                                 'display_name': definition['display_name'], 'version': definition['version']['number'], 'saved': None}
        return {'token': token, **preview}

    def _entry(self, owner, token, client):
        self._purge()
        entry = self.items.get(token) if isinstance(token, str) else None
        if entry is None:raise ValueError('生成预览不存在或已过期，请重新生成')
        if owner != entry['owner']:raise PermissionError('该生成预览不属于当前成员')
        if client.seller_id() != entry['seller']:raise ValueError('这份生成预览属于另一个店铺或卖家已变更，请切回原店铺或重新生成')
        return entry

    def artifact(self, user, body):
        owner = self._user(user);self._body(body)
        kind = body.get('kind')
        if kind not in ('template', 'source', 'schema'):raise ValueError('下载类型无效')
        with self.lock:
            entry = self._entry(owner, body.get('token'), self._client(body))
            artifact = entry['artifacts'][kind]
            if hashlib.sha256(artifact[0]).hexdigest() != entry['hashes'][kind]:raise ValueError('预览文件完整性校验失败，请重新生成')
            return artifact

    def save(self, user, body):
        owner = self._user(user);self._body(body)
        shop = body.get('shop')
        if not isinstance(shop, str) or not shop.strip() or len(shop.strip()) > 120 or shop.strip() == library.GLOBAL_SHOP:
            raise ValueError('请指定具体店铺（1–120字），不能存为通用模板')
        shop = shop.strip()
        client = self._client(body)
        # Serialize token consumption; library exclusivity is acquired only for the actual add.
        with self.lock:
            entry = self._entry(owner, body.get('token'), client)
            category = body.get('category', entry['display_name'] or entry['product_type'])
            version = body.get('version', 'SPAPI-' + re.sub(r'[^A-Za-z0-9_.-]', '_', entry['version'])[:24] + '-' + entry['schema_sha256'][:8] + '-' + body['token'][:10])
            if not isinstance(category, str) or not category.strip() or len(category) > 500 or not isinstance(version, str) or not version.strip() or len(version) > 160:
                raise ValueError('类目或版本标签无效')
            identity = (shop, category.strip(), version.strip())
            if entry['saved'] is not None:
                if identity != entry['save_identity']:raise ValueError('该预览已保存到另一个店铺或版本，请重新生成')
                return {**entry['saved'], 'reused': True}
            data, filename, _ = entry['artifacts']['template']
            if hashlib.sha256(data).hexdigest() != entry['hashes']['template']:raise ValueError('预览文件完整性校验失败，请重新生成')
            with tempfile.TemporaryDirectory(prefix='spapi-generated-') as tmp:
                path = Path(tmp) / filename
                path.write_bytes(data)
                with self.library_lock.exclusive():
                    self._entry(owner, body.get('token'), client)
                    result = library.run(SimpleNamespace(command='add', library=self.root, source=path, marketplace=entry['marketplace'],
                                                         category=identity[1], version=identity[2], shop=shop, table_type='upload'))
            entry['saved'] = dict(result)
            entry['save_identity'] = identity
            return dict(result)
