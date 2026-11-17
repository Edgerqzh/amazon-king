"""Store operations over SP-API: orders, sales, FBA stock, reports, template comparison,
and price / stock changes.

Everything except `Changes.submit` only reads. A price or stock change is prepared from the
listing's current values, validated by Amazon (VALIDATION_PREVIEW), shown to a person, and
sent only when that person confirms that one SKU. Exactly what was previewed is what is sent.
No buyer names, addresses or contact details are requested or kept.
"""
import copy
import csv
import datetime
import decimal
import gzip
import io
import json
import re
import secrets
import threading
import time

from . import listings, processing, spapi

CURRENCY = {'AE': 'AED', 'SA': 'SAR'}
UTC_OFFSET = {'AE': 4, 'SA': 3}
ZONE = {'AE': 'Asia/Dubai', 'SA': 'Asia/Riyadh'}
ORDER_FILTERS = {'': '', 'open': 'Unshipped,PartiallyShipped', 'pending': 'Pending', 'shipped': 'Shipped', 'canceled': 'Canceled'}
REPORTS = {
    'listings': ('GET_MERCHANT_LISTINGS_ALL_DATA', '全部商品清单', False),
    'fba': ('GET_FBA_MYI_UNSUPPRESSED_INVENTORY_DATA', '亚马逊物流库存', False),
    'orders': ('GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL', '订单明细（按下单日期）', True),
}
# Columns that describe a buyer or a delivery address are dropped from every report.
PRIVATE_COLUMN = re.compile(r'^(buyer|recipient|ship-(address|city|state|postal|phone|country)|bill|customer|delivery-instructions|gift-message)', re.I)
MAX_CHANGES = 20
TOKEN_LIFE = 600
UTC = datetime.timezone.utc


def whole(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('%s应为 %d 到 %d 之间的整数' % (label, low, high))
    return value


def stamp(moment):
    return moment.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def money(value):
    value = value if isinstance(value, dict) else {}
    return {'amount': str(value.get('Amount', value.get('amount', '')) or ''), 'currency': value.get('CurrencyCode', value.get('currencyCode', '')) or ''}


# --- orders (read-only, no buyer details) ---------------------------------------
def orders(client, marketplace, days=7, status='', now=None):
    market = spapi.marketplace_id(marketplace)
    whole(days, 1, 30, '天数')
    if status not in ORDER_FILTERS:
        raise ValueError('订单状态筛选无效')
    now = now or datetime.datetime.now(UTC)
    query = {'MarketplaceIds': market, 'CreatedAfter': stamp(now - datetime.timedelta(days=days)), 'MaxResultsPerPage': 100}
    if ORDER_FILTERS[status]:
        query['OrderStatuses'] = ORDER_FILTERS[status]
    rows, token = [], None
    for _ in range(5):
        payload = client.call('GET', '/orders/v0/orders', query if token is None else {'MarketplaceIds': market, 'NextToken': token}).get('payload') or {}
        for order in payload.get('Orders') or []:
            rows.append({'id': order.get('AmazonOrderId', ''), 'purchased': order.get('PurchaseDate', ''), 'status': order.get('OrderStatus', ''),
                         'channel': order.get('FulfillmentChannel', ''), 'total': money(order.get('OrderTotal')),
                         'shipped': order.get('NumberOfItemsShipped', 0), 'unshipped': order.get('NumberOfItemsUnshipped', 0),
                         'ship_by': order.get('LatestShipDate', ''), 'service': order.get('ShipServiceLevel', ''), 'business': bool(order.get('IsBusinessOrder'))})
        token = payload.get('NextToken')
        if not token:
            break
    counts, totals = {}, {}
    for row in rows:
        counts[row['status']] = counts.get(row['status'], 0) + 1
        if row['status'] != 'Canceled' and row['total']['amount']:
            try:
                totals[row['total']['currency']] = totals.get(row['total']['currency'], decimal.Decimal(0)) + decimal.Decimal(row['total']['amount'])
            except decimal.InvalidOperation:
                pass
    rows.sort(key=lambda r: r['purchased'], reverse=True)
    return {'marketplace': marketplace, 'days': days, 'orders': rows, 'counts': counts, 'totals': {k: str(v) for k, v in totals.items()}, 'truncated': bool(token)}


def order_items(client, order_id):
    if not isinstance(order_id, str) or not re.fullmatch(r'\d{3}-\d{7}-\d{7}', order_id):
        raise ValueError('订单编号格式无效')
    payload = client.call('GET', '/orders/v0/orders/' + order_id + '/orderItems').get('payload') or {}
    return {'id': order_id, 'items': [{'sku': i.get('SellerSKU', ''), 'asin': i.get('ASIN', ''), 'title': i.get('Title', ''), 'ordered': i.get('QuantityOrdered', 0), 'shipped': i.get('QuantityShipped', 0), 'price': money(i.get('ItemPrice'))} for i in payload.get('OrderItems') or []]}


# --- sales per day (read-only) --------------------------------------------------
def sales(client, marketplace, days=14, now=None):
    market = spapi.marketplace_id(marketplace)
    whole(days, 1, 90, '天数')
    zone = datetime.timezone(datetime.timedelta(hours=UTC_OFFSET[marketplace]))
    local = (now or datetime.datetime.now(UTC)).astimezone(zone)
    start = (local - datetime.timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    interval = start.isoformat(timespec='seconds') + '--' + local.isoformat(timespec='seconds')
    payload = client.call('GET', '/sales/v1/orderMetrics', {'marketplaceIds': market, 'interval': interval, 'granularity': 'Day', 'granularityTimeZone': ZONE[marketplace]}).get('payload') or []
    rows, units, count, total, currency = [], 0, 0, decimal.Decimal(0), ''
    for item in payload:
        amount = money(item.get('totalSales'))
        row = {'date': str(item.get('interval', ''))[:10], 'units': item.get('unitCount') or 0, 'orders': item.get('orderCount') or 0, 'sales': amount['amount'], 'currency': amount['currency'], 'average': money(item.get('averageUnitPrice'))['amount']}
        rows.append(row)
        units += row['units'] if isinstance(row['units'], int) else 0
        count += row['orders'] if isinstance(row['orders'], int) else 0
        currency = currency or amount['currency']
        try:
            total += decimal.Decimal(amount['amount'] or '0')
        except decimal.InvalidOperation:
            pass
    return {'marketplace': marketplace, 'days': days, 'rows': rows, 'units': units, 'orders': count, 'sales': str(total), 'currency': currency}


def overview(client, marketplace, now=None):
    """Home page figures: a week of sales and the orders waiting to ship. Each part may fail on its own."""
    spapi.marketplace_id(marketplace)
    client.require()
    result = {'marketplace': marketplace}
    for name, read in (('sales', lambda: sales(client, marketplace, 7, now)), ('open_orders', lambda: orders(client, marketplace, 7, 'open', now))):
        try:
            result[name] = read()
        except spapi.SpApiError as e:
            if e.status in (None, 401):
                raise
            result[name] = {'error': str(e)}
    return result


# --- FBA stock (read-only) ------------------------------------------------------
def fba_inventory(client, marketplace, skus=None):
    market = spapi.marketplace_id(marketplace)
    query = {'details': 'true', 'granularityType': 'Marketplace', 'granularityId': market, 'marketplaceIds': market}
    if skus:
        query['sellerSkus'] = ','.join(listings.clean_skus(skus, 50))
    rows, token = [], None
    for _ in range(10):
        reply = client.call('GET', '/fba/inventory/v1/summaries', {**query, 'nextToken': token} if token else query)
        for item in (reply.get('payload') or {}).get('inventorySummaries') or []:
            detail = item.get('inventoryDetails') or {}
            rows.append({'sku': item.get('sellerSku', ''), 'asin': item.get('asin', ''), 'title': item.get('productName', ''), 'total': item.get('totalQuantity', 0),
                         'fulfillable': detail.get('fulfillableQuantity', 0),
                         'inbound': sum(detail.get(key) or 0 for key in ('inboundWorkingQuantity', 'inboundShippedQuantity', 'inboundReceivingQuantity')),
                         'reserved': (detail.get('reservedQuantity') or {}).get('totalReservedQuantity', 0),
                         'unfulfillable': (detail.get('unfulfillableQuantity') or {}).get('totalUnfulfillableQuantity', 0)})
        token = (reply.get('pagination') or {}).get('nextToken')
        if not token:
            break
    return {'marketplace': marketplace, 'items': rows, 'truncated': bool(token)}


# --- reports (creating a report request reads data; it changes nothing in the store) ---
class Reports:
    def __init__(self):
        self.lock = threading.Lock()
        self.items = {}

    def create(self, client, kind, marketplace, days=7, now=None):
        market = spapi.marketplace_id(marketplace)
        if kind not in REPORTS:
            raise ValueError('报告类型无效')
        report_type, label, dated = REPORTS[kind]
        body = {'reportType': report_type, 'marketplaceIds': [market]}
        if dated:
            whole(days, 1, 30, '天数')
            now = now or datetime.datetime.now(UTC)
            body.update(dataStartTime=stamp(now - datetime.timedelta(days=days)), dataEndTime=stamp(now))
        report_id = str(client.call('POST', '/reports/2021-06-30/reports', None, body).get('reportId') or '')
        if not re.fullmatch(r'[A-Za-z0-9-]{1,80}', report_id):
            raise spapi.SpApiError('亚马逊没有返回报告编号')
        with self.lock:
            while len(self.items) >= 8:
                del self.items[next(iter(self.items))]
            self.items[report_id] = {'kind': kind, 'label': label, 'marketplace': marketplace, 'table': None, 'store': getattr(client, 'store_id', spapi.DEFAULT_STORE), 'store_name': getattr(client, 'label', spapi.DEFAULT_STORE_NAME)}
        return {'id': report_id, 'label': label, 'status': 'IN_QUEUE'}

    def entry(self, report_id, store=spapi.DEFAULT_STORE):
        with self.lock:
            entry = self.items.get(report_id) if isinstance(report_id, str) else None
        if entry is None:
            raise ValueError('报告不存在或已过期，请重新生成')
        if entry.get('store', spapi.DEFAULT_STORE) != store:
            raise ValueError('这份报告属于店铺“%s”，请切回该店铺查看' % entry.get('store_name', ''))
        return entry

    def get(self, client, report_id):
        entry = self.entry(report_id, getattr(client, 'store_id', spapi.DEFAULT_STORE))
        if entry['table'] is None:
            info = client.call('GET', '/reports/2021-06-30/reports/' + report_id)
            state = info.get('processingStatus', '')
            if state in ('CANCELLED', 'FATAL'):
                return {'id': report_id, 'label': entry['label'], 'status': state, 'note': '亚马逊取消了这份报告，通常表示所选范围内没有数据' if state == 'CANCELLED' else '亚马逊生成报告失败，请稍后重试'}
            if state != 'DONE':
                return {'id': report_id, 'label': entry['label'], 'status': state or 'IN_QUEUE'}
            document = client.call('GET', '/reports/2021-06-30/documents/' + str(info.get('reportDocumentId') or ''))
            raw = client.fetch_raw(document.get('url'))
            if document.get('compressionAlgorithm') == 'GZIP':
                raw = gzip.decompress(raw)
            entry['table'] = parse_table(raw)
        columns, rows = entry['table']
        return {'id': report_id, 'label': entry['label'], 'marketplace': entry['marketplace'], 'status': 'DONE', 'columns': columns, 'rows': rows[:300], 'total': len(rows)}

    def download(self, report_id, store=spapi.DEFAULT_STORE):
        entry = self.entry(report_id, store)
        if entry['table'] is None:
            raise ValueError('报告尚未生成完成')
        columns, rows = entry['table']
        out = io.StringIO()
        writer = csv.writer(out, lineterminator='\r\n')
        for row in [columns] + rows:
            # A leading = + - @ would make spreadsheet apps run the cell as a formula.
            writer.writerow(["'" + cell if re.match(r'[=+\-@\t\r]', cell) else cell for cell in row])
        return ('﻿' + out.getvalue()).encode('utf-8'), '%s_%s_%s.csv' % (entry['label'], entry.get('store_name', ''), entry['marketplace'])


def parse_table(raw):
    if len(raw) > 64 * 1024 * 1024:
        raise spapi.SpApiError('报告过大，请缩小日期范围')
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = raw.decode('cp1252', errors='replace')
    lines = list(csv.reader(io.StringIO(text, newline=''), delimiter='\t', quoting=csv.QUOTE_NONE))
    if not lines:
        return [], []
    keep = [i for i, name in enumerate(lines[0]) if not PRIVATE_COLUMN.match(name.strip())]
    pick = lambda row: [row[i].strip() if i < len(row) else '' for i in keep]
    return pick(lines[0]), [pick(row) for row in lines[1:200001] if any(cell.strip() for cell in row)]


# --- template vs. Amazon's current definition (read-only) -------------------------
IGNORED_BASES = {'product_type', 'contribution_sku', 'sku'}


def template_check(client, definitions, fields, product_types, marketplace):
    """Compare a library template's columns with the product type definition Amazon serves today."""
    bases, by_base, has_identifier = set(), {}, False
    for field in fields:
        if field.get('canonical') in ('product_id', 'product_id_type'):
            has_identifier = True
            continue
        if field['key'] == '::record_action' or listings.parse_key(field['key']) is None:
            continue
        base = processing.base_key(field['key'])
        bases.add(base)
        by_base.setdefault(base, field)
    if has_identifier:
        bases |= {'externally_assigned_product_identifier', 'merchant_suggested_asin'}
    results = []
    for product_type in list(product_types)[:8]:
        try:
            definition = definitions.definition(client, product_type, marketplace)
        except spapi.SpApiError as e:
            if e.status in (None, 401, 403, 429):
                raise
            results.append({'product_type': product_type, 'error': str(e)})
            continue
        schema = definition['schema']
        properties = schema.get('properties') or {}
        required = [name for name in schema.get('required') or [] if isinstance(name, str)]
        missing = [{'attribute': name, 'title': (properties.get(name) or {}).get('title', '')} for name in required if name not in bases]
        unknown = [{'attribute': base, 'label': by_base[base].get('label', ''), 'column': by_base[base].get('column', '')} for base in sorted(bases - set(properties) - IGNORED_BASES) if base in by_base]
        results.append({'product_type': product_type, 'version': definition['version'], 'attributes': len(properties), 'required': len(required), 'missing_required': missing, 'unknown_columns': unknown})
    checked = [r for r in results if 'error' not in r]
    differs = any(r['missing_required'] or r['unknown_columns'] for r in checked)
    return {'marketplace': marketplace, 'template_columns': len(bases), 'types': results, 'more_types': max(0, len(product_types) - 8),
            'status': 'unknown' if not checked else 'review' if differs else 'match'}


# --- price and stock changes: preview, confirm, submit ----------------------------
def parse_price(value):
    if value in (None, ''):
        return None
    if not isinstance(value, str) or not re.fullmatch(r'\d{1,7}(\.\d{1,2})?', value.strip()):
        raise ValueError('售价应为最多两位小数的正数，例如 19.90')
    price = decimal.Decimal(value.strip())
    if price <= 0:
        raise ValueError('售价必须大于 0')
    return price


def parse_quantity(value):
    if value in (None, ''):
        return None
    if type(value) is not int or not 0 <= value <= 999999:
        raise ValueError('库存应为 0 到 999999 的整数')
    return value


def number(value):
    return int(value) if value == value.to_integral_value() else float(value)


class Changes:
    """Pending previews, kept in memory; each can be submitted once, by the member who made it."""

    def __init__(self, log_path=None, clock=time.monotonic):
        self.lock = threading.Lock()
        self.pending, self.history = {}, []
        self.log_path, self.clock = log_path, clock

    def preview(self, client, user, marketplace, items):
        market, seller = spapi.marketplace_id(marketplace), client.seller_id()
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_CHANGES:
            raise ValueError('一次预检 1 到 %d 个 SKU' % MAX_CHANGES)
        wanted, seen = [], set()
        for item in items:
            if not isinstance(item, dict):
                raise ValueError('改动格式无效')
            sku = listings.clean_skus([item.get('sku')], 1)[0]
            if sku in seen:
                raise ValueError('SKU 重复：' + sku)
            seen.add(sku)
            price, quantity = parse_price(item.get('price')), parse_quantity(item.get('quantity'))
            if price is None and quantity is None:
                raise ValueError('SKU %s 没有填写新售价或新库存' % sku)
            wanted.append((sku, price, quantity))
        rows = []
        for sku, price, quantity in wanted:
            rows.append(self.prepare(client, user, marketplace, market, seller, sku, price, quantity))
        return {'marketplace': marketplace, 'currency': CURRENCY[marketplace], 'items': rows, 'valid_minutes': TOKEN_LIFE // 60}

    def prepare(self, client, user, marketplace, market, seller, sku, price, quantity):
        row = {'sku': sku, 'token': '', 'issues': [], 'notes': [], 'changes': []}
        path = listings.item_path(seller, sku)
        try:
            item = client.call('GET', path, {'marketplaceIds': market, 'includedData': 'summaries,attributes'})
        except spapi.SpApiError as e:
            if e.status in (None, 401, 403, 429):
                raise
            row['error'] = '亚马逊该站点下没有这个 SKU' if e.status == 404 else str(e)
            return row
        summary = next((s for s in item.get('summaries') or [] if s.get('marketplaceId') == market), None) or next(iter(item.get('summaries') or []), {})
        attributes = item.get('attributes') or {}
        row.update(title=summary.get('itemName', ''), asin=summary.get('asin', ''), product_type=summary.get('productType', ''))
        patches = []
        if price is not None:
            offers = [copy.deepcopy(o) for o in attributes.get('purchasable_offer') or [] if isinstance(o, dict) and o.get('marketplace_id', market) == market]
            offer = next((o for o in offers if o.get('audience', 'ALL') == 'ALL'), None)
            current = None
            if offer is None:
                offer = {'marketplace_id': market, 'audience': 'ALL', 'currency': CURRENCY[marketplace]}
                offers.append(offer)
            else:
                try:
                    current = offer['our_price'][0]['schedule'][0]['value_with_tax']
                except (KeyError, IndexError, TypeError):
                    current = None
                if offer.get('currency') and offer['currency'] != CURRENCY[marketplace]:
                    row['error'] = '该 SKU 的报价币种是 %s，与站点币种不一致，请到卖家后台处理' % offer['currency']
                    return row
                if offer.get('discounted_price'):
                    row['notes'].append('该 SKU 设有促销价，本次只改标准售价，促销价保持不变')
            offer['our_price'] = [{'schedule': [{'value_with_tax': number(price)}]}]
            row['changes'].append({'kind': 'price', 'label': '售价', 'old': '' if current is None else str(current), 'new': str(price), 'unit': CURRENCY[marketplace]})
            if current is not None and decimal.Decimal(str(current)) == price:
                row['changes'][-1]['same'] = True
            else:
                patches.append({'op': 'replace', 'path': '/attributes/purchasable_offer', 'value': offers})
                if current not in (None, 0) and not decimal.Decimal('0.5') <= price / decimal.Decimal(str(current)) <= decimal.Decimal('2'):
                    row['notes'].append('新售价与当前售价相差超过一倍，请确认没有输错小数点')
        if quantity is not None:
            stock = [copy.deepcopy(f) for f in attributes.get('fulfillment_availability') or [] if isinstance(f, dict)]
            own = next((f for f in stock if f.get('fulfillment_channel_code') == 'DEFAULT'), None)
            if own is None and stock:
                row['error'] = '该 SKU 由亚马逊物流配送，库存以入库数量为准，不能在这里修改'
                return row
            if own is None:
                own = {'fulfillment_channel_code': 'DEFAULT'}
                stock.append(own)
            current = own.get('quantity')
            own['quantity'] = quantity
            row['changes'].append({'kind': 'quantity', 'label': '自配送库存', 'old': '' if current is None else str(current), 'new': str(quantity), 'unit': '件'})
            if current == quantity:
                row['changes'][-1]['same'] = True
            else:
                patches.append({'op': 'replace', 'path': '/attributes/fulfillment_availability', 'value': stock})
        if not patches:
            row['error'] = '新值与亚马逊当前值相同，无需提交'
            return row
        body = {'productType': row['product_type'] or 'PRODUCT', 'patches': patches}
        try:
            reply = client.call('PATCH', path, {'marketplaceIds': market, 'mode': spapi.PREVIEW, 'issueLocale': 'en_US'}, body)
        except spapi.SpApiError as e:
            if e.status in (None, 401, 403, 429):
                raise
            row['error'] = str(e)
            return row
        row['issues'] = [listings.explain_issue(i) for i in reply.get('issues') or [] if isinstance(i, dict)]
        row['status'] = reply.get('status') or ''
        if row['status'] != 'VALID' or any(i['severity'] == 'error' for i in row['issues']):
            row['error'] = '亚马逊预检未通过，不能提交'
            return row
        row['token'] = self.hold(user, sku, marketplace, 'PATCH', path, {'marketplaceIds': market, 'issueLocale': 'en_US'}, body, [c for c in row['changes'] if not c.get('same')], getattr(client, 'store_id', spapi.DEFAULT_STORE), getattr(client, 'label', spapi.DEFAULT_STORE_NAME))
        return row

    def hold(self, user, sku, marketplace, method, path, query, body, changes, store=spapi.DEFAULT_STORE, store_name=spapi.DEFAULT_STORE_NAME):
        """Keep a request Amazon has just validated; returns the one-use token that can send it."""
        token = secrets.token_urlsafe(24)
        with self.lock:
            now = self.clock()
            self.pending = {k: v for k, v in self.pending.items() if v['expires'] > now}
            while len(self.pending) >= 400:
                del self.pending[next(iter(self.pending))]
            self.pending[token] = {'user': user, 'sku': sku, 'marketplace': marketplace, 'method': method, 'path': path, 'query': query, 'body': body, 'changes': changes, 'expires': now + TOKEN_LIFE, 'store': store, 'store_name': store_name}
        return token

    def hold_listing(self, user, marketplace, sku, path, query, body, store=spapi.DEFAULT_STORE, store_name=spapi.DEFAULT_STORE_NAME):
        return self.hold(user, sku, marketplace, 'PUT', path, query, body, [{'kind': 'listing', 'label': '上架商品', 'old': '', 'new': body['productType'], 'unit': ''}], store, store_name)

    def claim(self, user, token, sku, take):
        with self.lock:
            entry = self.pending.get(token) if isinstance(token, str) else None
            if entry is not None and entry['expires'] <= self.clock():
                del self.pending[token]
                entry = None
            if entry is None or entry['user'] != user or entry['sku'] != sku:
                raise ValueError('这条预检已失效（超过 %d 分钟、已提交过，或不是你发起的），请重新预检' % (TOKEN_LIFE // 60))
            if take:
                del self.pending[token]  # one use, even if the request that follows fails
            return entry

    def submit(self, client, user, token, sku, confirmed, overwrite=False):
        """Send one previewed change to the live store."""
        if confirmed is not True:
            raise ValueError('请先勾选确认，再提交这一个 SKU')
        entry = self.claim(user, token, sku, take=False)
        if entry.get('store', spapi.DEFAULT_STORE) != getattr(client, 'store_id', spapi.DEFAULT_STORE):
            # A preview is only valid for the store it was made in.
            raise ValueError('这条预检是在店铺“%s”下做的，请切回该店铺再提交' % entry.get('store_name', ''))
        if entry['method'] == 'PUT' and overwrite is not True:
            # A full submit replaces a listing that already exists, so look first.
            try:
                existing = client.call('GET', entry['path'], {'marketplaceIds': entry['query']['marketplaceIds'], 'includedData': 'summaries'})
            except spapi.SpApiError as e:
                if e.status != 404:
                    raise
            else:
                summary = next(iter(existing.get('summaries') or []), {})
                return {'needs_overwrite': True, 'sku': sku, 'title': summary.get('itemName', ''), 'asin': summary.get('asin', '')}
        entry = self.claim(user, token, sku, take=True)
        record = {'time': stamp(datetime.datetime.now(UTC)), 'user': user, 'store': entry.get('store_name', ''), 'sku': sku, 'marketplace': entry['marketplace'], 'changes': entry['changes']}
        try:
            reply = client.call(entry['method'], entry['path'], entry['query'], entry['body'], live=True)
        except spapi.SpApiError as e:
            record.update(status='FAILED', note=str(e))
            self.remember(record)
            raise
        issues = [listings.explain_issue(i) for i in reply.get('issues') or [] if isinstance(i, dict)]
        record.update(status=reply.get('status') or '', submission=str(reply.get('submissionId') or ''))
        self.remember(record)
        return {**record, 'issues': issues, 'accepted': record['status'] == 'ACCEPTED'}

    def remember(self, record):
        with self.lock:
            self.history.append(record)
            del self.history[:-200]
            if self.log_path:
                try:
                    with open(self.log_path, 'a', encoding='utf-8') as log:
                        log.write(json.dumps(record, ensure_ascii=False) + '\n')
                except OSError:
                    pass

    def recent(self):
        with self.lock:
            return list(reversed(self.history[-50:]))
