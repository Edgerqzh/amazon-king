"""Listings Items: read SKU status, and validate fill-page rows without submitting them.

Template columns are attribute paths such as
`purchasable_offer[marketplace_id=X][audience=ALL]#1.our_price#1.schedule#1.value_with_tax`;
they are converted mechanically to the JSON the Listings Items API expects. Nothing here
invents a value: an empty cell is left out and a column that cannot be converted is reported.
"""
import re
import threading
import urllib.parse

from template_manager import processing, spapi

MAX_STATUS_SKUS = 50
MAX_PRECHECK_SKUS = 100
SEVERITY = {'ERROR': 'error', 'WARNING': 'warning', 'INFO': 'suggestion'}
# Attributes whose template column has a different name.
ATTRIBUTE_FIELDS = {'externally_assigned_product_identifier': 'product_id', 'merchant_suggested_asin': 'product_id', 'purchasable_offer': 'standard_price', 'fulfillment_availability': 'quantity', 'child_parent_sku_relationship': 'parent_sku'}
SEGMENT = re.compile(r'([A-Za-z0-9_]+)((?:\[[^\[\]=]+=[^\[\]]*\])*)(?:#(\d{1,3}))?')
TRUE, FALSE = {'true', 'yes', 'y', '1', '是'}, {'false', 'no', 'n', '0', '否'}


def item_path(seller, sku):
    return spapi.LISTING_PATH + urllib.parse.quote(seller, safe='') + '/' + urllib.parse.quote(sku, safe='')


def clean_skus(skus, limit):
    if isinstance(skus, str):
        skus = re.split(r'[\r\n,;，；\t]+', skus)
    if not isinstance(skus, list):
        raise ValueError('请提供要查询的 SKU')
    result = []
    for sku in skus:
        if not isinstance(sku, str):
            raise ValueError('SKU 必须是文字')
        sku = sku.strip()
        if not sku or sku in result:
            continue
        if len(sku) > 40 or re.search(r'[\x00-\x1f]', sku):
            raise ValueError('SKU 过长或包含无效字符：' + sku[:40])
        result.append(sku)
    if not result:
        raise ValueError('请提供要查询的 SKU')
    if len(result) > limit:
        raise ValueError('一次最多 %d 个 SKU，请分批' % limit)
    return result


def explain_issue(issue):
    code, message = str(issue.get('code') or ''), str(issue.get('message') or '')
    kind, title, hint = processing.explain(code, message)
    return {'severity': SEVERITY.get(issue.get('severity'), 'unknown'), 'code': code, 'message': message, 'kind': kind, 'title': title, 'hint': hint,
            'attributes': [a for a in issue.get('attributeNames') or [] if isinstance(a, str)][:20]}


# --- status of existing SKUs (read-only) ------------------------------------
def status(client, skus, marketplace):
    market, seller = spapi.marketplace_id(marketplace), client.seller_id()
    rows = []
    for sku in clean_skus(skus, MAX_STATUS_SKUS):
        try:
            item = client.call('GET', item_path(seller, sku), {'marketplaceIds': market, 'includedData': 'summaries,issues,offers,fulfillmentAvailability'})
        except spapi.SpApiError as e:
            if e.status == 404:
                rows.append({'sku': sku, 'found': False, 'note': '亚马逊该站点下没有这个 SKU'})
                continue
            if e.status in (None, 401, 403, 429):
                raise
            rows.append({'sku': sku, 'found': False, 'note': str(e)})
            continue
        summary = next((s for s in item.get('summaries') or [] if s.get('marketplaceId') == market), None) or next(iter(item.get('summaries') or []), {})
        offers = [{'type': o.get('offerType', ''), 'amount': (o.get('price') or {}).get('amount', ''), 'currency': (o.get('price') or {}).get('currencyCode') or (o.get('price') or {}).get('currency', '')} for o in item.get('offers') or [] if o.get('marketplaceId') in (None, market)]
        stock = [{'channel': f.get('fulfillmentChannelCode', ''), 'quantity': f.get('quantity')} for f in item.get('fulfillmentAvailability') or []]
        rows.append({'sku': sku, 'found': True, 'asin': summary.get('asin', ''), 'product_type': summary.get('productType', ''), 'title': summary.get('itemName', ''),
                     'status': [s for s in summary.get('status') or [] if isinstance(s, str)], 'condition': summary.get('conditionType', ''),
                     'updated': summary.get('lastUpdatedDate', ''), 'offers': offers, 'stock': stock,
                     'issues': [explain_issue(i) for i in (item.get('issues') or [])[:200] if isinstance(i, dict)]})
    return {'marketplace': marketplace, 'seller_hint': client.public()['seller_hint'], 'items': rows}


# --- template row -> Listings JSON ------------------------------------------
def split_key(key):
    """Split on dots outside [selectors]."""
    parts, depth, current = [], 0, ''
    for char in key:
        if char == '[':
            depth += 1
        elif char == ']':
            depth -= 1
        if char == '.' and depth == 0:
            parts.append(current)
            current = ''
        else:
            current += char
    return parts + [current]


def parse_key(key):
    """[(name, {selector: value}, index or None), ...], or None if the column is not an attribute path."""
    path = []
    for part in split_key(key):
        match = SEGMENT.fullmatch(part)
        if not match:
            return None
        selectors = dict(re.findall(r'\[([^\[\]=]+)=([^\[\]]*)\]', match[2]))
        path.append((match[1], selectors, int(match[3]) if match[3] else None))
    if not path or path[0][2] is None or path[-1][2] is not None or path[-1][1] or path[0][2] < 1:
        return None
    return path


def schema_leaf(schema, path):
    """The JSON-schema node for the value at this path, or {} when unknown."""
    node = ((schema or {}).get('properties') or {}).get(path[0][0])
    for name, _, _ in path[1:]:
        if not isinstance(node, dict):
            return {}
        if node.get('type') == 'array' or 'items' in node:
            node = node.get('items') or {}
        node = (node.get('properties') or {}).get(name) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else {}


def schema_item(schema, name):
    node = ((schema or {}).get('properties') or {}).get(name)
    return (node.get('items') or {}) if isinstance(node, dict) else {}


def coerce(value, leaf):
    """Use the product type definition to send numbers, booleans and option codes, not display text."""
    variants = [leaf] + [v for key in ('anyOf', 'oneOf') for v in leaf.get(key) or [] if isinstance(v, dict)]
    text = value.strip()
    for variant in variants:
        codes, names = variant.get('enum'), variant.get('enumNames')
        if isinstance(codes, list):
            if text in codes:
                return text
            if isinstance(names, list):
                for code, name in zip(codes, names):
                    if isinstance(name, str) and name.strip().casefold() == text.casefold():
                        return code
            for code in codes:
                if isinstance(code, str) and code.casefold() == text.casefold():
                    return code
    types = {t for variant in variants for t in ([variant.get('type')] if isinstance(variant.get('type'), str) else variant.get('type') or [])}
    if 'boolean' in types and text.casefold() in TRUE | FALSE:
        return text.casefold() in TRUE
    if types & {'integer', 'number'} and 'string' not in types:
        try:
            number = float(text.replace(',', ''))
        except ValueError:
            return text
        if number != number or number in (float('inf'), float('-inf')):
            return text
        return int(number) if number == int(number) and ('integer' in types or re.fullmatch(r'-?\d+', text)) else number
    return text


def convert(values, fields, market, schema=None):
    """(product_type, attributes, skipped column labels) for one output row of the fill page."""
    by_key = {f['key']: f for f in fields}
    attributes, product_type, skipped, identifier = {}, '', [], {}
    for key, raw in values.items():
        if not isinstance(raw, str) or not raw.strip():
            continue
        field = by_key.get(key, {})
        role = field.get('canonical', '')
        if role == 'sku' or key == '::record_action':
            continue
        if role == 'feed_product_type':
            product_type = raw.strip()
            continue
        if role in ('product_id', 'product_id_type'):
            identifier[role] = raw.strip()
            continue
        path = parse_key(key)
        if path is None:
            skipped.append(field.get('label') or key)
            continue
        leaf = schema_leaf(schema, path)
        node = attributes
        for depth, (name, selectors, index) in enumerate(path):
            if depth == len(path) - 1:
                node[name] = coerce(raw, leaf)
                break
            if index is None:
                node = node.setdefault(name, {})
                continue
            items = node.setdefault(name, [])
            while len(items) < index:
                items.append({})
            node = items[index - 1]
            for selector, chosen in selectors.items():
                node[selector] = chosen
    if identifier.get('product_id'):
        kind = identifier.get('product_id_type', '').casefold()
        if kind == 'asin':
            attributes['merchant_suggested_asin'] = [{'value': identifier['product_id']}]
        elif kind in ('ean', 'upc', 'gtin', 'isbn', 'jan'):
            attributes['externally_assigned_product_identifier'] = [{'type': kind, 'value': identifier['product_id']}]
        else:
            skipped.append('商品编码（未提供可识别的编码类型）')
    # Drop list slots that stayed empty (e.g. only bullet 1 and 3 were filled).
    for name in list(attributes):
        if isinstance(attributes[name], list):
            attributes[name] = [item for item in attributes[name] if item]
            # Unknown attribute or no definition: marketplace_id is the safe default.
            item_schema = schema_item(schema, name).get('properties') or None
            for item in attributes[name]:
                if 'marketplace_id' not in item and (item_schema is None or 'marketplace_id' in item_schema):
                    item['marketplace_id'] = market
            if not attributes[name]:
                del attributes[name]
    return product_type, attributes, skipped


# --- product type definitions (cached) --------------------------------------
class Definitions:
    """Product type schemas, kept in memory for the life of the process."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cache = {}

    def schema(self, client, product_type, marketplace):
        return self.definition(client, product_type, marketplace)['schema']

    def definition(self, client, product_type, marketplace):
        key = (client.seller_id(), marketplace, product_type)
        with self.lock:
            if key in self.cache:
                return self.cache[key]
        if not re.fullmatch(r'[A-Z0-9_]{1,80}', product_type):
            raise spapi.SpApiError('商品类型代码无效：' + product_type[:40])
        meta = client.call('GET', '/definitions/2020-09-01/productTypes/' + product_type, {'marketplaceIds': spapi.marketplace_id(marketplace), 'sellerId': client.seller_id(), 'requirements': 'LISTING', 'locale': 'en_US'})
        link = ((meta.get('schema') or {}).get('link') or {}).get('resource')
        schema = client.fetch_document(link)
        if not isinstance(schema, dict):
            raise spapi.SpApiError('亚马逊字段定义格式异常')
        version = meta.get('productTypeVersion') or {}
        result = {'schema': schema, 'version': {'number': str(version.get('number') or ''), 'latest': bool(version.get('latest'))}}
        with self.lock:
            if len(self.cache) >= 40:
                del self.cache[next(iter(self.cache))]
            self.cache[key] = result
        return result


# --- pre-check (VALIDATION_PREVIEW) -----------------------------------------
def precheck(client, definitions, rows, fields, marketplace, table_type, start=1, limit=20, progress=None, hold=None):
    """Ask Amazon to validate rows without saving them. Returns a processing-report shaped result."""
    if type(start) is not int or type(limit) is not int or start < 1 or not 1 <= limit <= MAX_PRECHECK_SKUS:
        raise ValueError('预检范围无效：一次最多 %d 个 SKU' % MAX_PRECHECK_SKUS)
    market, seller = spapi.marketplace_id(marketplace), client.seller_id()
    chosen = rows[start - 1:start - 1 + limit]
    if not chosen:
        raise ValueError('所选范围内没有商品行，请调整起始序号')
    collector = processing.Collector(fields)
    index = collector.index
    notes, schemas, valid, sent, skipped_fields, submittable = [], {}, 0, 0, {}, []
    progress = progress or (lambda state: None)
    for done, row in enumerate(chosen):
        progress({'stage': 'precheck', 'completed': done, 'total': len(chosen)})
        sku = row.get('sku') or ''
        if not sku or len(sku) > 40:
            collector.add(sku=sku, message='缺少 SKU 或 SKU 超过 40 个字符，未发送预检', severity='error', field=index.canonical.get('sku'))
            continue
        declared = next((v.strip() for k, v in row['values'].items() if isinstance(v, str) and v.strip() and (index.exact.get(processing.norm(k)) or {}).get('canonical') == 'feed_product_type'), '')
        product_type = declared.upper() if re.fullmatch(r'[A-Za-z0-9_]{1,80}', declared) else ''
        if table_type == 'upload' and not product_type:
            collector.add(sku=sku, message='这一行没有可用的商品类型代码，未发送预检', severity='error', field=index.canonical.get('feed_product_type'))
            continue
        schema = None
        if product_type:
            if product_type not in schemas:
                try:
                    schemas[product_type] = definitions.schema(client, product_type, marketplace)
                except spapi.SpApiError as e:
                    if e.status in (None, 401, 403):
                        raise
                    schemas[product_type] = None
                    notes.append('未能取得商品类型 %s 的字段定义（%s），数值和选项按原文发送，结果可能出现本可避免的格式提示' % (product_type, e))
            schema = schemas[product_type]
        _, attributes, skipped = convert(row['values'], index.fields, market, schema)
        for label in skipped:
            skipped_fields[label] = skipped_fields.get(label, 0) + 1
        if not attributes:
            collector.add(sku=sku, message='这一行没有可发送的字段内容，未发送预检', severity='error')
            continue
        query = {'marketplaceIds': market, 'mode': spapi.PREVIEW, 'issueLocale': 'en_US'}
        try:
            if table_type == 'upload':
                body = {'productType': product_type, 'requirements': 'LISTING', 'attributes': attributes}
                reply = client.call('PUT', item_path(seller, sku), query, body)
            else:
                patches = [{'op': 'replace', 'path': '/attributes/' + name, 'value': value} for name, value in attributes.items()]
                reply = client.call('PATCH', item_path(seller, sku), query, {'productType': product_type or 'PRODUCT', 'patches': patches})
        except spapi.SpApiError as e:
            if e.status in (None, 401, 403, 429):
                raise
            collector.add(sku=sku, message=str(e), code=e.code, severity='unknown')
            sent += 1
            continue
        sent += 1
        issues = [i for i in reply.get('issues') or [] if isinstance(i, dict)]
        if reply.get('status') == 'VALID' and not any(i.get('severity') == 'ERROR' for i in issues):
            valid += 1
            if hold and table_type == 'upload':
                # Exactly this request body is what a later confirmed submit sends.
                title = next((str(v) for k, v in row['values'].items() if (index.exact.get(processing.norm(k)) or {}).get('canonical') == 'item_name' and str(v).strip()), '')
                submittable.append({'sku': sku, 'product_type': product_type, 'title': title[:200], 'fields': len(attributes), 'source_rows': row.get('source_rows') or [],
                                    'token': hold(sku, item_path(seller, sku), {'marketplaceIds': market, 'issueLocale': 'en_US'}, body)})
        elif not issues:
            collector.add(sku=sku, message='亚马逊返回状态 %s，但没有给出具体问题' % (reply.get('status') or '未知'), severity='unknown')
        for issue in issues:
            names = [a for a in issue.get('attributeNames') or [] if isinstance(a, str)] or ['']
            for name in names[:10]:
                field = None
                if name:
                    filled = [f for f in index.fields if processing.base_key(f['key']) == name and str(row['values'].get(f['key'], '')).strip()]
                    field = filled[0] if filled else index.base.get(processing.norm(name)) or index.canonical.get(ATTRIBUTE_FIELDS.get(name, ''))
                collector.add(sku=sku, message=str(issue.get('message') or ''), code=str(issue.get('code') or ''), severity=SEVERITY.get(issue.get('severity'), 'unknown'), field=field, field_hint=name)
    progress({'stage': 'done', 'completed': len(chosen), 'total': len(chosen)})
    if skipped_fields:
        notes.append('以下列不是可转换的属性路径，预检时未发送：' + '、'.join('%s（%d 行）' % item for item in list(skipped_fields.items())[:12]))
    if table_type != 'upload':
        notes.append('改价／库存按部分更新方式预检，只校验售价、库存等已填写的字段')
    result = processing.summarise(collector, 'precheck', '亚马逊预检', {'processed': sent, 'successful': valid})
    result.update(notes=notes, submittable=submittable, range={'start': start, 'count': len(chosen), 'total': len(rows)}, seller_hint=client.public()['seller_hint'])
    return result
