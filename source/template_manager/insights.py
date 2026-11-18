"""Store analysis over SP-API: listing issues, inbound shipments, profit, returns and review requests.

Everything here only reads, except `Reviews.send`: it asks Amazon to send its standard
"review and seller feedback" request for orders a person picked and confirmed. That is the
same request as the button on an order in Seller Central; Amazon decides eligibility and
writes the message itself. No buyer name, address or contact detail is requested or kept.
"""
import datetime
import decimal
import json
from pathlib import Path
import re
import threading
import time

from . import listings, operations, spapi

UTC = datetime.timezone.utc
ORDER_ID = re.compile(r'\d{3}-\d{7}-\d{7}\Z')
MARKET_NAMES = {'AE': 'Amazon.ae', 'SA': 'Amazon.sa', 'US': 'Amazon.com'}
VAT = {'AE': 5, 'SA': 15, 'US': 0}
D = decimal.Decimal


def _whole(value, low, high, label):
    return operations.whole(value, low, high, label)


def _amount(value):
    """SP-API money in either {'CurrencyAmount', 'CurrencyCode'} or {'Amount', 'CurrencyCode'} form."""
    if not isinstance(value, dict):
        return D(0), ''
    raw = value.get('CurrencyAmount', value.get('Amount', 0))
    try:
        number = D(str(raw))
    except decimal.InvalidOperation:
        number = D(0)
    if not number.is_finite():
        number = D(0)
    return number, value.get('CurrencyCode') if isinstance(value.get('CurrencyCode'), str) else ''


def _money(number):
    return str(number.quantize(D('0.01')))


def _market(client, marketplace):
    return spapi.marketplace_id(marketplace, getattr(client, 'region', None))


# --- listing issues (read-only) -------------------------------------------------
def listing_issues(client, marketplace, sleep=time.sleep, max_pages=25):
    """Every listing of this seller that Amazon marks with an error or a warning."""
    market, seller = _market(client, marketplace), client.seller_id()
    path = spapi.LISTING_PATH + seller
    query = {'marketplaceIds': market, 'includedData': 'summaries,issues', 'withIssueSeverity': 'ERROR,WARNING', 'pageSize': 20}
    rows, token = [], None
    for page in range(max_pages):
        if page:
            sleep(0.25)
        reply = client.call('GET', path, {**query, 'pageToken': token} if token else query)
        for item in reply.get('items') or []:
            if not isinstance(item, dict):
                continue
            summary = next((s for s in item.get('summaries') or [] if isinstance(s, dict) and s.get('marketplaceId') == market), None) or next(iter(item.get('summaries') or []), {})
            issues = [listings.explain_issue(i) for i in (item.get('issues') or [])[:50] if isinstance(i, dict)]
            if not issues:
                continue
            status = [s for s in summary.get('status') or [] if isinstance(s, str)]
            rows.append({'sku': str(item.get('sku', '')), 'asin': summary.get('asin', ''), 'title': summary.get('itemName', ''),
                         'status': status, 'buyable': 'BUYABLE' in status, 'issues': issues,
                         'errors': sum(1 for i in issues if i['severity'] == 'error'), 'warnings': sum(1 for i in issues if i['severity'] == 'warning')})
        token = (reply.get('pagination') or {}).get('nextToken')
        if not token:
            break
    rows.sort(key=lambda r: (r['buyable'], -r['errors'], -r['warnings'], r['sku']))
    return {'marketplace': marketplace, 'items': rows, 'truncated': bool(token),
            'counts': {'not_buyable': sum(1 for r in rows if not r['buyable']), 'errors': sum(1 for r in rows if r['errors']), 'warnings_only': sum(1 for r in rows if not r['errors'])}}


# --- inbound shipments (read-only) ----------------------------------------------
INBOUND_STATES = {'WORKING': '创建中', 'READY_TO_SHIP': '待发货', 'SHIPPED': '已发货', 'IN_TRANSIT': '运输中', 'DELIVERED': '已送达',
                  'CHECKED_IN': '已签到', 'RECEIVING': '接收中', 'CLOSED': '已完成', 'CANCELLED': '已取消', 'DELETED': '已删除', 'ERROR': '出错'}
OPEN_STATES = ('WORKING', 'READY_TO_SHIP', 'SHIPPED', 'IN_TRANSIT', 'DELIVERED', 'CHECKED_IN', 'RECEIVING')


def inbound(client, marketplace, days=90, now=None, sleep=time.sleep, max_detail=20):
    market = _market(client, marketplace)
    _whole(days, 7, 365, '天数')
    now = now or datetime.datetime.now(UTC)
    query = {'MarketplaceId': market, 'QueryType': 'DATE_RANGE', 'ShipmentStatusList': ','.join(INBOUND_STATES),
             'LastUpdatedAfter': operations.stamp(now - datetime.timedelta(days=days)), 'LastUpdatedBefore': operations.stamp(now)}
    shipments, token = [], None
    for page in range(10):
        if page:
            sleep(0.55)
        reply = client.call('GET', '/fba/inbound/v0/shipments', query if token is None else {'MarketplaceId': market, 'QueryType': 'NEXT_TOKEN', 'NextToken': token})
        payload = reply.get('payload') or {}
        for item in payload.get('ShipmentData') or []:
            if isinstance(item, dict) and re.fullmatch(r'[A-Z0-9]{6,20}', str(item.get('ShipmentId', ''))):
                status = str(item.get('ShipmentStatus', ''))
                shipments.append({'id': item['ShipmentId'], 'name': str(item.get('ShipmentName', ''))[:200], 'status': status,
                                  'status_label': INBOUND_STATES.get(status, status), 'center': str(item.get('DestinationFulfillmentCenterId', '')),
                                  'shipped': None, 'received': None, 'skus': None, 'short': None})
        token = payload.get('NextToken')
        if not token:
            break
    detailed = [s for s in shipments if s['status'] not in ('CANCELLED', 'DELETED')][:max_detail]
    for shipment in detailed:
        sleep(0.55)  # the inbound operations allow about two requests per second
        try:
            reply = client.call('GET', '/fba/inbound/v0/shipments/' + shipment['id'] + '/items', {'MarketplaceId': market})
        except spapi.SpApiError as e:
            if e.status in (None, 401, 403):
                raise
            shipment['error'] = str(e)
            continue
        items = [i for i in (reply.get('payload') or {}).get('ItemData') or [] if isinstance(i, dict)]
        shipment['shipped'] = sum(int(i.get('QuantityShipped') or 0) for i in items)
        shipment['received'] = sum(int(i.get('QuantityReceived') or 0) for i in items)
        shipment['skus'] = [{'sku': str(i.get('SellerSKU', '')), 'shipped': int(i.get('QuantityShipped') or 0), 'received': int(i.get('QuantityReceived') or 0)} for i in items[:200]]
        # A closed shipment that received fewer units than were sent is worth a claim check.
        shipment['short'] = shipment['status'] == 'CLOSED' and shipment['received'] < shipment['shipped']
    order = {state: index for index, state in enumerate(OPEN_STATES + ('CLOSED', 'ERROR', 'CANCELLED', 'DELETED'))}
    shipments.sort(key=lambda s: (not s['short'], order.get(s['status'], 99)))
    return {'marketplace': marketplace, 'days': days, 'items': shipments, 'truncated': bool(token), 'detailed': len(detailed),
            'counts': {'open': sum(1 for s in shipments if s['status'] in OPEN_STATES), 'short': sum(1 for s in shipments if s['short'])}}


# --- profit (read-only) -----------------------------------------------------------
class Costs:
    """Unit cost per SKU (purchase + freight, in the marketplace currency), per store and site."""

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.lock = threading.Lock()
        try:
            data = json.loads(self.path.read_text(encoding='utf-8')) if self.path and self.path.is_file() else {}
        except (OSError, ValueError):
            data = {}
        self.data = data if isinstance(data, dict) else {}

    def get(self, store, marketplace):
        with self.lock:
            return dict(self.data.get(store, {}).get(marketplace, {}))

    def set(self, store, marketplace, values):
        if marketplace not in VAT:
            raise ValueError('站点无效')
        if not isinstance(values, dict) or len(values) > 2000:
            raise ValueError('成本格式无效，一次最多 2000 个 SKU')
        clean = {}
        for sku, cost in values.items():
            if not isinstance(sku, str) or not sku.strip() or len(sku) > 40:
                raise ValueError('SKU 无效：%s' % str(sku)[:40])
            if cost in (None, ''):
                clean[sku.strip()] = None
                continue
            try:
                number = D(str(cost))
            except decimal.InvalidOperation:
                raise ValueError('SKU %s 的成本不是数字' % sku[:40]) from None
            if not number.is_finite() or number < 0 or number > 1000000:
                raise ValueError('SKU %s 的成本超出范围' % sku[:40])
            clean[sku.strip()] = _money(number)
        with self.lock:
            current = self.data.setdefault(store, {}).setdefault(marketplace, {})
            for sku, cost in clean.items():
                if cost is None:
                    current.pop(sku, None)
                else:
                    current[sku] = cost
            if self.path is not None:
                temp = self.path.with_suffix('.tmp')
                temp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding='utf-8')
                temp.replace(self.path)
            return dict(current)


def financial_events(client, days, now=None, sleep=time.sleep, max_pages=20):
    _whole(days, 1, 30, '天数')
    now = now or datetime.datetime.now(UTC)
    end = now - datetime.timedelta(minutes=5)  # Amazon requires the end to be at least two minutes ago
    query = {'PostedAfter': operations.stamp(end - datetime.timedelta(days=days)), 'PostedBefore': operations.stamp(end), 'MaxResultsPerPage': 100}
    shipments, refunds, services, token = [], [], [], None
    for page in range(max_pages):
        if page:
            sleep(2.1)  # listFinancialEvents allows about one request every two seconds
        payload = client.call('GET', '/finances/v0/financialEvents', query if token is None else {'NextToken': token}).get('payload') or {}
        events = payload.get('FinancialEvents') or {}
        shipments += [e for e in events.get('ShipmentEventList') or [] if isinstance(e, dict)]
        refunds += [e for e in events.get('RefundEventList') or [] if isinstance(e, dict)]
        services += [e for e in events.get('ServiceFeeEventList') or [] if isinstance(e, dict)]
        token = payload.get('NextToken')
        if not token:
            break
    return shipments, refunds, services, bool(token)


def profit(shipments, refunds, services, marketplace, costs, vat_rate=None, vat_included=True):
    """Per-SKU profit from financial events. Amounts are in the marketplace currency; fees are negative."""
    if marketplace not in MARKET_NAMES:
        raise ValueError('站点无效')
    rate = D(str(VAT[marketplace] if vat_rate is None else vat_rate))
    if not D(0) <= rate <= D(30):
        raise ValueError('增值税率应在 0–30 之间')
    name = MARKET_NAMES[marketplace].lower()
    rows, currency = {}, ''

    def row(sku):
        return rows.setdefault(sku, {'sku': sku, 'units': 0, 'refund_units': 0, 'sales': D(0), 'other_income': D(0), 'tax_collected': D(0),
                                     'promotions': D(0), 'fees': D(0), 'refunds': D(0)})

    def charges(entry, charge_key, fee_key, promo_key, sign_units, target):
        nonlocal currency
        for charge in entry.get(charge_key) or []:
            amount, code = _amount(charge.get('ChargeAmount'))
            currency = currency or code
            kind = charge.get('ChargeType', '')
            if kind == 'Principal':
                target['refunds' if sign_units < 0 else 'sales'] += amount
            elif kind in ('Tax', 'ShippingTax', 'GiftWrapTax'):
                target['tax_collected'] += amount
            else:
                target['refunds' if sign_units < 0 else 'other_income'] += amount
        for fee in entry.get(fee_key) or []:
            amount, code = _amount(fee.get('FeeAmount'))
            currency = currency or code
            target['fees'] += amount
        for promo in entry.get(promo_key) or []:
            amount, code = _amount(promo.get('PromotionAmount'))
            target['promotions'] += amount

    for event in shipments:
        if str(event.get('MarketplaceName', '')).lower() != name:
            continue
        for item in event.get('ShipmentItemList') or []:
            target = row(str(item.get('SellerSKU', '')) or '（无 SKU）')
            target['units'] += int(item.get('QuantityShipped') or 0)
            charges(item, 'ItemChargeList', 'ItemFeeList', 'PromotionList', 1, target)
    for event in refunds:
        if str(event.get('MarketplaceName', '')).lower() != name:
            continue
        for item in event.get('ShipmentItemAdjustmentList') or []:
            target = row(str(item.get('SellerSKU', '')) or '（无 SKU）')
            target['refund_units'] += abs(int(item.get('QuantityShipped') or 0))
            charges(item, 'ItemChargeAdjustmentList', 'ItemFeeAdjustmentList', 'PromotionAdjustmentList', -1, target)
    service_total = D(0)
    for event in services:
        for fee in event.get('FeeList') or []:
            service_total += _amount(fee.get('FeeAmount'))[0]
    result, missing = [], []
    totals = {k: D(0) for k in ('sales', 'other_income', 'promotions', 'fees', 'refunds', 'vat', 'cost', 'profit')}
    for sku, r in rows.items():
        income = r['sales'] + r['refunds']  # refunds of the item price are negative
        vat = (income * rate / (D(100) + rate)) if vat_included else D(0)
        unit_cost = costs.get(sku)
        cost = D(unit_cost) * r['units'] if unit_cost is not None else None
        if cost is None and r['units']:
            missing.append(sku)
        net = r['sales'] + r['other_income'] + r['promotions'] + r['fees'] + r['refunds'] - vat - (cost or D(0))
        out = {'sku': sku, 'units': r['units'], 'refund_units': r['refund_units'], 'sales': _money(r['sales']), 'other_income': _money(r['other_income']),
               'promotions': _money(r['promotions']), 'fees': _money(r['fees']), 'refunds': _money(r['refunds']), 'vat': _money(vat),
               'unit_cost': unit_cost, 'cost': _money(cost) if cost is not None else None, 'profit': _money(net),
               'margin': str((net / r['sales'] * 100).quantize(D('0.1'))) if r['sales'] > 0 else None, 'cost_missing': cost is None and bool(r['units'])}
        result.append(out)
        for key, value in (('sales', r['sales']), ('other_income', r['other_income']), ('promotions', r['promotions']), ('fees', r['fees']),
                           ('refunds', r['refunds']), ('vat', vat), ('cost', cost or D(0)), ('profit', net)):
            totals[key] += value
    result.sort(key=lambda r: D(r['profit']))
    totals['service_fees'] = service_total
    totals['profit_after_service'] = totals['profit'] + service_total
    return {'marketplace': marketplace, 'currency': currency, 'items': result, 'totals': {k: _money(v) for k, v in totals.items()},
            'vat_rate': str(rate), 'vat_included': bool(vat_included), 'cost_missing': missing}


# --- returns (read-only) ----------------------------------------------------------
REASONS = {
    'DEFECTIVE': '商品有缺陷', 'QUALITY_UNACCEPTABLE': '质量不能接受', 'NOT_AS_DESCRIBED': '与描述不符', 'NOT_COMPATIBLE': '不兼容',
    'DAMAGED_BY_CARRIER': '运输中损坏', 'DAMAGED_BY_FC': '仓库损坏', 'CUSTOMER_DAMAGED': '买家损坏', 'MISSING_PARTS': '缺少配件',
    'MISSED_ESTIMATED_DELIVERY': '超过预计送达时间', 'ORDERED_WRONG_ITEM': '买家买错', 'UNWANTED_ITEM': '不想要了', 'APPAREL_TOO_SMALL': '尺码偏小',
    'APPAREL_TOO_LARGE': '尺码偏大', 'APPAREL_STYLE': '款式不喜欢', 'FOUND_BETTER_PRICE': '找到更便宜的', 'EXTRA_ITEM': '多发了商品',
    'SWITCHEROO': '退回的不是原商品', 'UNDELIVERABLE_REFUSED': '拒收', 'UNDELIVERABLE_UNKNOWN': '无法投递', 'UNAUTHORIZED_PURCHASE': '非本人购买',
    'NEVER_ARRIVED': '没收到货', 'NO_REASON_GIVEN': '未说明原因', 'OTHER': '其他'}
QUALITY = {'DEFECTIVE', 'QUALITY_UNACCEPTABLE', 'NOT_AS_DESCRIBED', 'NOT_COMPATIBLE', 'MISSING_PARTS', 'SWITCHEROO'}
DISPOSITIONS = {'SELLABLE': '可再售', 'DEFECTIVE': '残次', 'CUSTOMER_DAMAGED': '买家损坏', 'CARRIER_DAMAGED': '承运损坏', 'DAMAGED': '损坏', 'EXPIRED': '过期'}


def returns(columns, rows, sold):
    def col(*names):
        lowered = [c.strip().lower() for c in columns]
        return next((lowered.index(n) for n in names if n in lowered), None)
    sku, qty, reason, disposition = col('sku'), col('quantity'), col('reason'), col('detailed-disposition')
    asin, title = col('asin'), col('product-name')
    if sku is None:
        raise ValueError('退货报告里没有 sku 列')
    found = {}
    for line in rows:
        get = lambda i: line[i].strip() if i is not None and i < len(line) else ''
        name = get(sku)
        if not name:
            continue
        try:
            count = max(1, int(get(qty) or 1))
        except ValueError:
            count = 1
        entry = found.setdefault(name, {'sku': name, 'asin': get(asin), 'title': get(title)[:200], 'returned': 0, 'quality': 0, 'reasons': {}, 'dispositions': {}})
        entry['returned'] += count
        code = get(reason).upper() or 'NO_REASON_GIVEN'
        entry['reasons'][code] = entry['reasons'].get(code, 0) + count
        if code in QUALITY:
            entry['quality'] += count
        state = get(disposition).upper() or 'UNKNOWN'
        entry['dispositions'][state] = entry['dispositions'].get(state, 0) + count
    result = []
    for entry in found.values():
        units = int(sold.get(entry['sku'], 0))
        entry['sold'] = units
        entry['rate'] = round(entry['returned'] / units * 100, 1) if units else None
        entry['top_reasons'] = [{'code': c, 'label': REASONS.get(c, c), 'count': n} for c, n in sorted(entry['reasons'].items(), key=lambda kv: -kv[1])[:5]]
        entry['disposition_list'] = [{'code': c, 'label': DISPOSITIONS.get(c, c), 'count': n} for c, n in sorted(entry['dispositions'].items(), key=lambda kv: -kv[1])]
        # Flag a product when a fifth of what sold came back, or most returns point at the product itself.
        entry['flag'] = bool((entry['rate'] is not None and entry['rate'] >= 10 and entry['returned'] >= 3) or (entry['quality'] >= 3 and entry['quality'] * 2 >= entry['returned']))
        del entry['reasons'], entry['dispositions']
        result.append(entry)
    result.sort(key=lambda e: (not e['flag'], -(e['rate'] or 0), -e['returned']))
    reasons = {}
    for entry in result:
        for item in entry['top_reasons']:
            reasons[item['code']] = reasons.get(item['code'], 0) + item['count']
    return {'items': result, 'total_returned': sum(e['returned'] for e in result), 'total_sold': sum(int(v) for v in sold.values()),
            'flagged': sum(1 for e in result if e['flag']),
            'reasons': [{'code': c, 'label': REASONS.get(c, c), 'count': n} for c, n in sorted(reasons.items(), key=lambda kv: -kv[1])[:8]]}


# --- review requests ---------------------------------------------------------------
REVIEW_ACTION = 'productReviewAndSellerFeedback'
MAX_REVIEW_BATCH = 20


def review_path(order_id):
    return '/solicitations/v1/orders/' + order_id + '/solicitations/' + REVIEW_ACTION


class Reviews:
    """Orders that can still be asked for a review, and sending Amazon's standard request."""

    def __init__(self, log_path=None, sleep=time.sleep, now=None):
        self.path = Path(log_path) if log_path else None
        self.sleep, self.now = sleep, now or (lambda: datetime.datetime.now(UTC))
        self.lock = threading.Lock()
        self.sent = self._load()

    def _load(self):
        sent = {}
        if self.path is None or not self.path.is_file():
            return sent
        try:
            for line in self.path.read_text(encoding='utf-8').splitlines():
                record = json.loads(line)
                if isinstance(record, dict) and record.get('result') in ('sent', 'not_eligible'):
                    sent[record.get('order', '')] = record['result']
        except (OSError, ValueError):
            pass
        return sent

    def _log(self, record):
        with self.lock:
            if record['result'] in ('sent', 'not_eligible'):
                self.sent[record['order']] = record['result']
            if self.path is not None:
                with self.path.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + '\n')

    def candidates(self, client, marketplace):
        """Shipped orders bought 5–30 days ago; Amazon allows the request 5–30 days after delivery."""
        market = _market(client, marketplace)
        now = self.now()
        query = {'MarketplaceIds': market, 'CreatedAfter': operations.stamp(now - datetime.timedelta(days=35)),
                 'CreatedBefore': operations.stamp(now - datetime.timedelta(days=5)), 'OrderStatuses': 'Shipped', 'MaxResultsPerPage': 100}
        rows, token = [], None
        for page in range(5):
            if page:
                self.sleep(1)
            payload = client.call('GET', '/orders/v0/orders', query if token is None else {'MarketplaceIds': market, 'NextToken': token}).get('payload') or {}
            for order in payload.get('Orders') or []:
                order_id = str(order.get('AmazonOrderId', ''))
                if not ORDER_ID.fullmatch(order_id):
                    continue
                rows.append({'id': order_id, 'purchased': order.get('PurchaseDate', ''), 'channel': order.get('FulfillmentChannel', ''),
                             'total': operations.money(order.get('OrderTotal')), 'done': self.sent.get(order_id, '')})
            token = payload.get('NextToken')
            if not token:
                break
        rows.sort(key=lambda r: r['purchased'])
        return {'marketplace': marketplace, 'orders': rows, 'truncated': bool(token),
                'pending': sum(1 for r in rows if not r['done']), 'done': sum(1 for r in rows if r['done'])}

    def send(self, client, user, marketplace, order_ids, confirmed):
        if confirmed is not True:
            raise ValueError('请先确认要向这些订单发送评论请求')
        market = _market(client, marketplace)
        if not isinstance(order_ids, list) or not 1 <= len(order_ids) <= MAX_REVIEW_BATCH or len(set(order_ids)) != len(order_ids):
            raise ValueError('每次发送 1–%d 个订单' % MAX_REVIEW_BATCH)
        if not all(isinstance(o, str) and ORDER_ID.fullmatch(o) for o in order_ids):
            raise ValueError('订单编号格式无效')
        results = []
        for index, order_id in enumerate(order_ids):
            if self.sent.get(order_id):
                results.append({'id': order_id, 'result': 'skipped', 'note': '之前已经处理过'})
                continue
            if index:
                self.sleep(1.1)  # both solicitation operations allow about one request per second
            try:
                actions = client.call('GET', '/solicitations/v1/orders/' + order_id, {'marketplaceIds': market})
                links = ((actions.get('_links') or {}).get('actions') or []) if isinstance(actions, dict) else []
                if not any(isinstance(link, dict) and str(link.get('href', '')).endswith('/' + REVIEW_ACTION) for link in links):
                    record = {'id': order_id, 'result': 'not_eligible', 'note': '亚马逊表示这个订单目前不能请求评论（未送达、超过 30 天或已请求过）'}
                else:
                    self.sleep(1.1)
                    client.call('POST', review_path(order_id), {'marketplaceIds': market}, None, live=True)
                    record = {'id': order_id, 'result': 'sent', 'note': '已发送'}
            except spapi.SpApiError as e:
                if e.status in (None, 401, 403, 429):
                    raise  # missing permission or throttling: stop the batch, nothing more is sent
                record = {'id': order_id, 'result': 'failed', 'note': str(e)[:300]}
            results.append(record)
            self._log({'order': order_id, 'result': record['result'], 'user': user, 'store': getattr(client, 'store_id', spapi.DEFAULT_STORE),
                       'marketplace': marketplace, 'at': self.now().isoformat(timespec='seconds')})
        return {'items': results, 'sent': sum(1 for r in results if r['result'] == 'sent')}
