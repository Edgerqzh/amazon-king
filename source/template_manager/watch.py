"""Two read-only store checks.

1. Offers watch: who else sells on our own ASINs and who holds the Buy Box. Compared with the
   previous check of the same store, so a seller that newly appears is called out.
2. Restock: units sold per SKU (from Amazon's order report) against FBA stock, giving days of
   stock left and how many units to send.

Nothing here changes the store. Seller IDs and prices are public offer data; no buyer details
are read or kept.
"""
import datetime
import json
import math
from pathlib import Path
import re
import threading
import time

from . import lookalike, spapi

ASIN = re.compile(r'[A-Z0-9]{10}\Z')
MAX_ASINS = 20
OFFERS_INTERVAL = lookalike.OFFERS_INTERVAL
SELLER_PAGE = {'AE': 'https://www.amazon.ae/sp?seller=', 'SA': 'https://www.amazon.sa/sp?seller=', 'US': 'https://www.amazon.com/sp?seller='}
UTC = datetime.timezone.utc


def clean_asins(values):
    if isinstance(values, str):
        values = re.split(r'[\s,;，；]+', values)
    if not isinstance(values, list):
        raise ValueError('请提供 ASIN 列表')
    found = []
    for value in values:
        value = value.strip().upper() if isinstance(value, str) else ''
        if not value:
            continue
        if not ASIN.fullmatch(value):
            raise ValueError('ASIN 格式不对：%s（应为 10 位字母数字）' % value[:20])
        if value not in found:
            found.append(value)
    if not 1 <= len(found) <= MAX_ASINS:
        raise ValueError('每次检查 1–%d 个 ASIN' % MAX_ASINS)
    return found


class Offers:
    """Offer checks with the last result per store and site kept on disk (no secrets in it)."""

    def __init__(self, path=None, sleep=time.sleep, now=None):
        self.path = Path(path) if path else None
        self.sleep, self.now = sleep, now or (lambda: datetime.datetime.now(UTC))
        self.lock = threading.Lock()
        self.last = self._load()

    def _load(self):
        if self.path is None or not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self):
        if self.path is None:
            return
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.last, ensure_ascii=False), encoding='utf-8')
        temp.replace(self.path)

    def check(self, client, stores, marketplace, asins):
        asins = clean_asins(asins)
        market_id = spapi.marketplace_id(marketplace, getattr(client, 'region', None))
        own_seller = client.require()['seller_id']
        own = lookalike.Service._own_sellers(stores)
        own.setdefault(own_seller, getattr(client, 'label', spapi.DEFAULT_STORE_NAME))
        key = '%s:%s' % (getattr(client, 'store_id', spapi.DEFAULT_STORE), marketplace)
        with self.lock:
            before = dict(self.last.get(key, {}).get('items', {}))
            before_at = self.last.get(key, {}).get('checked_at', '')
        rows, remember = [], {}
        for index, asin in enumerate(asins):
            if index:
                self.sleep(OFFERS_INTERVAL)
            try:
                reply = client.call('GET', '/products/pricing/v0/items/' + asin + '/offers', {'MarketplaceId': market_id, 'ItemCondition': 'New'})
            except spapi.SpApiError as e:
                if e.status in (None, 401, 403):
                    raise
                rows.append({'asin': asin, 'error': str(e)})
                continue
            row = self.summarize(asin, reply, own, own_seller, marketplace, before.get(asin))
            rows.append(row)
            remember[asin] = {'others': [o['seller'] for o in row['others']], 'buy_box': row['buy_box_seller']}
        checked_at = self.now().isoformat(timespec='seconds')
        with self.lock:
            entry = self.last.setdefault(key, {'items': {}})
            entry['items'].update(remember)
            entry['checked_at'] = checked_at
            while len(entry['items']) > 500:
                del entry['items'][next(iter(entry['items']))]
            self._save()
        counts = {}
        for row in rows:
            counts[row.get('state', 'error')] = counts.get(row.get('state', 'error'), 0) + 1
        return {'marketplace': marketplace, 'items': rows, 'counts': counts, 'checked_at': checked_at, 'previous_at': before_at}

    @staticmethod
    def summarize(asin, reply, own, own_seller, marketplace, previous):
        payload = reply.get('payload') if isinstance(reply.get('payload'), dict) else {}
        summary = payload.get('Summary') if isinstance(payload.get('Summary'), dict) else {}
        offers = []
        for offer in payload.get('Offers') or []:
            if not isinstance(offer, dict):
                continue
            price, shipping = lookalike._money(offer.get('ListingPrice')), lookalike._money(offer.get('Shipping'))
            if price is None:
                continue
            seller = offer.get('SellerId') if isinstance(offer.get('SellerId'), str) and re.fullmatch(r'[A-Z0-9]{5,30}', offer.get('SellerId')) else ''
            total = round(price['amount'] + (shipping['amount'] if shipping and shipping['currency'] == price['currency'] else 0), 2)
            offers.append({'seller': seller, 'own_store': own.get(seller, ''), 'price': total, 'currency': price['currency'],
                           'buy_box': offer.get('IsBuyBoxWinner') is True, 'fba': offer.get('IsFulfilledByAmazon') is True,
                           'link': SELLER_PAGE[marketplace] + seller if seller else ''})
        offers.sort(key=lambda o: o['price'])
        mine = next((o for o in offers if o['seller'] == own_seller), None)
        others = [o for o in offers if not o['own_store']]
        winner = next((o for o in offers if o['buy_box']), None)
        buy_box_price = next((lookalike._money(b.get('LandedPrice')) for b in summary.get('BuyBoxPrices') or [] if isinstance(b, dict)), None)
        count = summary.get('TotalOfferCount')
        new = [o['seller'] for o in others if previous is not None and o['seller'] and o['seller'] not in previous.get('others', [])]
        if mine is None and not any(o['own_store'] for o in offers):
            state, note = 'not_selling', '没有读到我们自己的报价（可能缺货、未上架或价格未生效）'
        elif winner is not None and winner['own_store']:
            state, note = ('shared' if others else 'ours'), ('购物车在我们这，但有 %d 个其他卖家在跟卖' % len(others) if others else '购物车在我们这，没有其他卖家')
        elif winner is not None:
            state, note = 'lost', '购物车被其他卖家拿走'
        else:
            state, note = 'no_buy_box', '这个商品目前没有购物车' + ('，有 %d 个其他卖家' % len(others) if others else '')
        if previous is not None and previous.get('buy_box') and winner is not None and winner['seller'] != previous.get('buy_box') and state == 'lost':
            note += '（上次检查时购物车还在%s）' % ('我们这' if own.get(previous['buy_box']) else '别的卖家那')
        if new:
            note += '；新出现 %d 个卖家' % len(new)
        return {'asin': asin, 'state': state, 'note': note, 'mine': mine, 'others': others[:20], 'other_count': len(others),
                'offer_count': count if type(count) is int else len(offers), 'buy_box_seller': winner['seller'] if winner else '',
                'buy_box_own': bool(winner and winner['own_store']), 'buy_box_price': buy_box_price, 'new_sellers': new,
                'first_check': previous is None}


# --- restock ------------------------------------------------------------------
STATES = {'out': '已断货', 'urgent': '马上补', 'soon': '该补货', 'ok': '库存充足', 'slow': '滞销', 'idle': '无销量无库存'}
ORDER = ['out', 'urgent', 'soon', 'slow', 'ok', 'idle']


def _column(columns, *names):
    lowered = [c.strip().lower() for c in columns]
    for name in names:
        if name in lowered:
            return lowered.index(name)
    return None


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    return number if math.isfinite(number) and number > 0 else 0


def sold_per_sku(columns, rows, days, now=None):
    """Units sold through FBA per SKU: over the whole period and over its last 7 days."""
    sku, qty = _column(columns, 'sku'), _column(columns, 'quantity', 'quantity-purchased')
    status, channel, date = _column(columns, 'order-status'), _column(columns, 'fulfillment-channel'), _column(columns, 'purchase-date')
    if sku is None or qty is None:
        raise ValueError('订单报告里没有 sku 或 quantity 列，无法统计销量')
    now = now or datetime.datetime.now(UTC)
    week = now - datetime.timedelta(days=7)
    total, recent = {}, {}
    for row in rows:
        get = lambda i: row[i].strip() if i is not None and i < len(row) else ''
        name = get(sku)
        if not name or get(status).lower() in ('cancelled', 'canceled'):
            continue
        if channel is not None and get(channel) and get(channel).lower() not in ('amazon', 'afn'):
            continue
        units = _number(get(qty))
        total[name] = total.get(name, 0) + units
        try:
            when = datetime.datetime.fromisoformat(get(date).replace('Z', '+00:00'))
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
        except ValueError:
            when = None
        if when is not None and when >= week:
            recent[name] = recent.get(name, 0) + units
    return total, recent


def restock(inventory, total, recent, days, lead_days, cover_days):
    """Suggest units to send. Daily sales weight the last 7 days and the whole period equally."""
    for value, low, high, label in ((days, 7, 30, '统计天数'), (lead_days, 1, 120, '补货到货天数'), (cover_days, 1, 180, '到货后要覆盖的天数')):
        if type(value) is not int or not low <= value <= high:
            raise ValueError('%s应为 %d 到 %d 之间的整数' % (label, low, high))
    rows, seen = [], set()
    items = list(inventory) + [{'sku': name, 'asin': '', 'title': '', 'fulfillable': 0, 'inbound': 0, 'reserved': 0} for name in total if name not in {i.get('sku') for i in inventory}]
    for item in items:
        name = item.get('sku', '')
        if not name or name in seen:
            continue
        seen.add(name)
        sold, week = total.get(name, 0), recent.get(name, 0)
        daily = round((sold / days + week / 7) / 2, 2) if sold else 0
        stock = int(item.get('fulfillable') or 0)
        inbound = int(item.get('inbound') or 0)
        left = round(stock / daily, 1) if daily else None
        left_all = round((stock + inbound) / daily, 1) if daily else None
        need = math.ceil(daily * (lead_days + cover_days) - stock - inbound) if daily else 0
        if not daily:
            state = 'slow' if stock + inbound else 'idle'
        elif stock == 0:
            state = 'out'
        elif left < lead_days:
            state = 'urgent'
        elif need > 0:
            state = 'soon'
        else:
            state = 'ok'
        rows.append({'sku': name, 'asin': item.get('asin', ''), 'title': item.get('title', ''), 'sold': int(sold), 'sold_7': int(week), 'daily': daily,
                     'fulfillable': stock, 'inbound': inbound, 'reserved': int(item.get('reserved') or 0), 'days_left': left, 'days_left_with_inbound': left_all,
                     'suggest': max(0, need), 'state': state, 'state_label': STATES[state]})
    rows.sort(key=lambda r: (ORDER.index(r['state']), r['days_left'] if r['days_left'] is not None else 1e9, -r['daily']))
    counts = {}
    for row in rows:
        counts[row['state']] = counts.get(row['state'], 0) + 1
    return {'items': rows, 'counts': counts, 'days': days, 'lead_days': lead_days, 'cover_days': cover_days,
            'labels': STATES}
