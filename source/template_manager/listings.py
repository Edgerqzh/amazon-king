"""Listings Items: read SKU status, and validate fill-page rows without submitting them.

Template columns are attribute paths such as
`purchasable_offer[marketplace_id=X][audience=ALL]#1.our_price#1.schedule#1.value_with_tax`;
they are converted mechanically to the JSON the Listings Items API expects. Nothing here
invents a value: an empty cell is left out and a column that cannot be converted is reported.
"""
import base64
import datetime
import hashlib
import hmac
import json
import re
import threading
import time
import urllib.parse

from template_manager import processing, spapi, schema_utils

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
    market, seller = spapi.marketplace_id(marketplace, getattr(client, 'region', None)), client.seller_id()
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
    """Use the same bounded local reference / allOf resolution as generated fields."""
    return schema_utils.leaf(schema, path)


def schema_item(schema, name):
    try:
        node = schema_utils.attribute_node(schema, name)
        return schema_utils.Resolver(schema).resolve(node.get('items') or {}) if isinstance(node, dict) else {}
    except schema_utils.SchemaError:
        return {}


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
    json_roots = set()
    scalar_roots = {p[0][0] for k, v in values.items() if isinstance(v, str) and v.strip() and (p := parse_key(k))}
    for key, raw in values.items():
        if not isinstance(raw, str) or not raw.strip():
            continue
        field = by_key.get(key, {})
        if key.endswith('::json'):
            name = key[:-6]
            if field.get('json_attribute') != name or field.get('generated_format') != schema_utils.GENERATED_FORMAT or not schema_utils.NAME.fullmatch(name):
                raise ValueError('高级 JSON 列未由生成模板规则声明：' + key[:120])
            try:
                # Resolve the root container, but retain ambiguous branches for structural validation.
                root = schema_utils.Resolver(schema).resolve(schema or {})
                node = (root.get('properties') or {}).get(name)
            except schema_utils.SchemaError as e:
                raise ValueError('无法取得最新属性规则，必须重新预检') from e
            if not isinstance(node, dict):raise ValueError('最新规则中不存在 JSON 属性：' + name)
            if name in scalar_roots:raise ValueError('完整 JSON 与标量字段冲突：' + name)
            value = schema_utils.strict_json(raw)
            attributes[name] = schema_utils.validate_json_attribute(value, node, schema)
            json_roots.add(name)
            continue
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
                selector_path = path[:depth + 1] + [(selector, {}, None)]
                node[selector] = coerce(chosen, schema_leaf(schema, selector_path))
    if identifier.get('product_id'):
        kind = identifier.get('product_id_type', '').casefold()
        if kind == 'asin':
            if 'merchant_suggested_asin' in json_roots:raise ValueError('完整 JSON 与商品编码字段冲突：merchant_suggested_asin')
            attributes['merchant_suggested_asin'] = [{'value': identifier['product_id']}]
        elif kind in ('ean', 'upc', 'gtin', 'isbn', 'jan'):
            if 'externally_assigned_product_identifier' in json_roots:raise ValueError('完整 JSON 与商品编码字段冲突：externally_assigned_product_identifier')
            attributes['externally_assigned_product_identifier'] = [{'type': kind, 'value': identifier['product_id']}]
        else:
            skipped.append('商品编码（未提供可识别的编码类型）')
    # Drop list slots that stayed empty (e.g. only bullet 1 and 3 were filled).
    for name in list(attributes):
        if isinstance(attributes[name], list) and name not in json_roots:
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
def _checksum_bytes(text):
    """A schema checksum as raw digest bytes: hex or base64 (Amazon documents an MD5 digest; tolerate either encoding)."""
    text = text.strip()
    if re.fullmatch(r'[0-9a-fA-F]{32}|[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', text):
        return bytes.fromhex(text)
    try:
        return base64.b64decode(text, validate=True)
    except (ValueError, TypeError):
        return None


class Definitions:
    """Seller-scoped, bounded TTL cache. Generation force-refreshes strict official metadata."""

    def __init__(self, clock=time.monotonic, ttl=600):
        self.lock = threading.Lock()
        self.cache = {}  # Kept public for existing callers and tests.
        self.clock, self.ttl = clock, ttl
        self.cached_at = {}

    def schema(self, client, product_type, marketplace):
        return self.definition(client, product_type, marketplace)['schema']

    def search(self, client, marketplace, query, mode='keywords', search_locale='zh_CN'):
        market = spapi.marketplace_id(marketplace, getattr(client, 'region', None))
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200 or re.search(r'[\x00-\x1f]', query):
            raise ValueError('搜索文字须为 1–200 字，不含控制字符')
        if mode not in ('keywords', 'item_name') or search_locale not in ('zh_CN', 'en_US'):
            raise ValueError('搜索方式或搜索语言无效')
        query = query.strip()
        params = {'marketplaceIds': market, 'locale': 'en_US', 'searchLocale': search_locale}
        if mode == 'keywords':
            words = [word.strip() for word in re.split('[,，]', query) if word.strip()]
            if not words:raise ValueError('请提供搜索关键词')
            params['keywords'] = ','.join(words)
        else:
            params['itemName'] = query
        reply = client.call('GET', '/definitions/2020-09-01/productTypes', params)
        products = reply.get('productTypes') if isinstance(reply, dict) else None
        if not isinstance(products, list):raise spapi.SpApiError('亚马逊商品类型搜索格式异常')
        items, seen = [], set()
        for item in products:
            if not isinstance(item, dict):continue
            name = item.get('name')
            if not isinstance(name, str) or not re.fullmatch(r'[A-Z0-9_]{1,80}', name) or name in seen:continue
            if market not in (item.get('marketplaceIds') or []):continue
            seen.add(name)
            items.append({'name': name, 'display_name': str(item.get('displayName') or name)[:500]})
        return {'marketplace': marketplace, 'query': query, 'items': items[:100], 'truncated': len(items) > 100}

    def definition(self, client, product_type, marketplace, force=False, strict=False):
        if not isinstance(product_type, str) or not re.fullmatch(r'[A-Z0-9_]{1,80}', product_type):
            raise spapi.SpApiError('商品类型代码无效')
        market, seller = spapi.marketplace_id(marketplace, getattr(client, 'region', None)), client.seller_id()
        key = (seller, marketplace, product_type)
        with self.lock:
            hit = self.cache.get(key)
            if not force and hit is not None and self.clock() - self.cached_at.get(key, float('-inf')) < self.ttl and (not strict or hit.get('strict_verified')):
                return hit
        meta = client.call('GET', '/definitions/2020-09-01/productTypes/' + product_type,
                           {'marketplaceIds': market, 'sellerId': seller, 'productTypeVersion': 'LATEST', 'requirements': 'LISTING', 'requirementsEnforced': 'ENFORCED', 'locale': 'en_US'})
        if not isinstance(meta, dict):raise spapi.SpApiError('亚马逊字段定义元数据格式异常')
        version = meta.get('productTypeVersion') or {}
        # Amazon names the version string "version" (e.g. {"version": "U...", "latest": true}); older mocks used "number".
        version_text = (version.get('version') or version.get('number')) if isinstance(version, dict) else None
        source = meta.get('schema') or {}
        if not isinstance(source, dict) or not isinstance(version, dict):raise spapi.SpApiError('亚马逊规则版本或下载元数据格式异常')
        link = source.get('link') or {}
        if not isinstance(link, dict):raise spapi.SpApiError('亚马逊规则下载元数据格式异常')
        checksum = source.get('checksum')
        if strict:
            if meta.get('productType') != product_type or meta.get('marketplaceIds') != [market] or meta.get('sellerId', seller) != seller or meta.get('requirements') != 'LISTING' or meta.get('requirementsEnforced') != 'ENFORCED':
                raise spapi.SpApiError('亚马逊字段定义的类型、站点或要求上下文不符，未生成模板')
            if not isinstance(version, dict) or not isinstance(version_text, str) or not version_text or version.get('latest') is not True:
                raise spapi.SpApiError('亚马逊未返回已确认的最新规则版本')
            if link.get('verb') != 'GET' or not isinstance(checksum, str) or not checksum:
                raise spapi.SpApiError('亚马逊规则缺少 GET 下载元数据或校验和')
        resource = link.get('resource')
        schema_hash = ''
        if checksum is not None or strict:
            if not isinstance(checksum, str):raise spapi.SpApiError('亚马逊规则校验和格式异常')
            expected = _checksum_bytes(checksum)
            algorithm = {16: 'md5', 20: 'sha1', 32: 'sha256'}.get(len(expected or b''))
            if algorithm is None:raise spapi.SpApiError('亚马逊规则校验和格式异常')
            raw = client.fetch_raw(resource)
            if not isinstance(raw, bytes) or not hmac.compare_digest(hashlib.new(algorithm, raw).digest(), expected):
                raise spapi.SpApiError('亚马逊规则校验和不符，未生成模板')
            schema_hash = hashlib.sha256(raw).hexdigest()
            try:schema = schema_utils.strict_json(raw, limit=16 * 1024 * 1024)
            except ValueError:raise spapi.SpApiError('亚马逊字段定义不是有效的有界 JSON') from None
        else:
            # Legacy minimal mocks and older clients remain usable for preview, never generation.
            schema = client.fetch_document(resource)
            schema_hash = hashlib.sha256(json.dumps(schema, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        if not isinstance(schema, dict):raise spapi.SpApiError('亚马逊字段定义格式异常')
        if client.seller_id() != seller:raise spapi.SpApiError('连接卖家已变更，请重新获取规则')
        result = {'schema': schema, 'version': {'number': str(version_text or ''), 'latest': bool(version.get('latest'))},
                  'product_type': product_type, 'display_name': str(meta.get('displayName') or product_type)[:500], 'marketplace': marketplace,
                  'schema_sha256': schema_hash, 'checksum': checksum or '', 'fetched_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  'requirements': meta.get('requirements', ''), 'requirements_enforced': meta.get('requirementsEnforced', ''), 'strict_verified': strict}
        with self.lock:
            if len(self.cache) >= 40 and key not in self.cache:
                oldest = next(iter(self.cache))
                del self.cache[oldest]
                self.cached_at.pop(oldest, None)
            self.cache[key] = result
            self.cached_at[key] = self.clock()
        return result


# --- pre-check (VALIDATION_PREVIEW) -----------------------------------------
def precheck(client, definitions, rows, fields, marketplace, table_type, start=1, limit=20, progress=None, hold=None):
    """Ask Amazon to validate rows without saving them. Returns a processing-report shaped result."""
    if type(start) is not int or type(limit) is not int or start < 1 or not 1 <= limit <= MAX_PRECHECK_SKUS:
        raise ValueError('预检范围无效：一次最多 %d 个 SKU' % MAX_PRECHECK_SKUS)
    market, seller = spapi.marketplace_id(marketplace, getattr(client, 'region', None)), client.seller_id()
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
                    if any(f.get('generated_format') == schema_utils.GENERATED_FORMAT for f in fields):
                        schemas[product_type] = definitions.definition(client, product_type, marketplace, force=True, strict=True)['schema']
                    else:
                        schemas[product_type] = definitions.schema(client, product_type, marketplace)
                except spapi.SpApiError as e:
                    if e.status in (None, 401, 403):
                        raise
                    schemas[product_type] = None
                    notes.append('未能取得商品类型 %s 的字段定义（%s），数值和选项按原文发送，结果可能出现本可避免的格式提示' % (product_type, e))
            schema = schemas[product_type]
        try:
            _, attributes, skipped = convert(row['values'], index.fields, market, schema)
        except ValueError as e:
            collector.add(sku=sku, message='属性转换失败，未发送预检：' + str(e), severity='error')
            continue
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
