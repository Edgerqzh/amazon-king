"""Bounded, credential-free public Amazon BSR reading. No login or challenge bypass.

Collector.discover(marketplace, asin, cancel=None, checkpoint=None) returns categories.
Collector.collect(marketplace, url, cancel=None, checkpoint=None) returns page counts;
checkpoint receives a parsed page (items, page, category_url, category, fetched_at).
All network dependencies can be injected for offline tests.
"""
from datetime import datetime, timezone
from html.parser import HTMLParser
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

HOSTS = {'AE': 'www.amazon.ae', 'SA': 'www.amazon.sa', 'US': 'www.amazon.com'}
ASIN = re.compile(r'[A-Z0-9]{10}\Z')
MAX_HTML = 4 * 1024 * 1024
USER_AGENT = 'TemplateManagerBSR/1.0'


class BsrError(ValueError):
    def __init__(self, message, status='failed'):
        super().__init__(message)
        self.status = status


def marketplace(value):
    if not isinstance(value, str) or value not in HOSTS:
        raise ValueError('仅支持 AE、SA、US 站点')
    return value


def asin(value):
    if not isinstance(value, str) or not ASIN.fullmatch(value):
        raise ValueError('ASIN 须为 10 位大写字母或数字')
    return value


def product_url(market, value):
    return 'https://' + HOSTS[marketplace(market)] + '/dp/' + asin(value)


def validate_url(value, market, kind='bsr', base=None):
    """Return a normalized same-market URL; never accept arbitrary fetch paths."""
    marketplace(market)
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or re.search(r'[\s\\\x00-\x1f\x7f]', value):
        raise ValueError('Amazon 链接无效')
    if base:
        value = urllib.parse.urljoin(base, value)
    if kind == 'search':
        return validate_search_url(value, market)
    try:
        u = urllib.parse.urlsplit(value)
        if u.scheme != 'https' or u.username is not None or u.password is not None or u.port not in (None, 443):
            raise ValueError()
        host = HOSTS[market]
        if u.hostname not in (host, host[4:]) or u.fragment:
            raise ValueError()
        path = urllib.parse.unquote(u.path, errors='strict')
        if '%' in path or not path.startswith('/') or '//' in path or any(p in ('.', '..') for p in path.split('/')):
            raise ValueError()
        if re.search(r'[\s\\\x00-\x1f\x7f?#]', path):
            raise ValueError()
        # Keep path alphabet deliberately narrow; product slugs may be non-ASCII.
        if any(not (c.isalnum() or c in '/-_.~=') for c in path):
            raise ValueError()
        path = re.sub(r'/ref=[^/]*/*$', '', path).rstrip('/') or '/'
        pairs = urllib.parse.parse_qsl(u.query, keep_blank_values=True, strict_parsing=True, max_num_fields=10) if u.query else []
        allowed = {'pg', 'ref', 'ref_', 'ie', '_encoding'}
        if any(k not in allowed or len(v) > 200 or not re.fullmatch(r'[A-Za-z0-9_.=-]*', v) for k, v in pairs):
            raise ValueError()
        if len({k for k, v in pairs}) != len(pairs):
            raise ValueError()
        pg = dict(pairs).get('pg', '')
        if pg and pg not in ('1', '2'):
            raise ValueError()
        is_bsr = bool(re.fullmatch(r'/(?:gp/bestsellers|zgbs)(?:/[\w~.=-]+)*', path) or re.fullmatch(r'/[\w~.=-]+/zgbs(?:/[\w~.=-]+)*', path))
        product = re.fullmatch(r'(?:/[^/]+)?/(?:dp|gp/product)/([A-Z0-9]{10})', path)
        if kind == 'robots':
            valid = path == '/robots.txt' and not pairs
        elif kind == 'product':
            valid = bool(product) and not pg
        elif kind == 'bsr':
            valid = is_bsr
        else:
            valid = is_bsr or bool(product)
        if not valid:
            raise ValueError()
        if product and kind != 'robots':
            path = '/dp/' + product.group(1)
        return 'https://' + host + urllib.parse.quote(path, safe='/-_.~=') + (('?pg=' + pg) if pg else '')
    except (ValueError, UnicodeError):
        raise ValueError('只允许所选站点的 HTTPS Amazon 商品或 BSR 链接') from None


def category_url(url, market):
    return validate_url(url, market).split('?', 1)[0]


def search_url(market, keyword, page=1):
    marketplace(market)
    if not isinstance(keyword, str) or not keyword.strip() or len(keyword) > 100 or re.search(r'[\x00-\x1f\x7f]', keyword):
        raise ValueError('搜索关键词无效')
    if type(page) is not int or not 1 <= page <= 5:
        raise ValueError('搜索页码须为 1–5')
    return 'https://' + HOSTS[market] + '/s?' + urllib.parse.urlencode({'k': keyword.strip(), 'page': page})


def validate_search_url(value, market):
    try:
        u = urllib.parse.urlsplit(value)
        pairs = urllib.parse.parse_qsl(u.query, keep_blank_values=True, strict_parsing=True, max_num_fields=2)
        query = dict(pairs)
        if (u.scheme != 'https' or u.netloc != HOSTS[marketplace(market)] or u.path != '/s' or u.fragment
                or len(pairs) != 2 or set(query) != {'k', 'page'} or query['page'] not in ('1', '2', '3', '4', '5')):
            raise ValueError()
        canonical = search_url(market, query['k'], int(query['page']))
        if value != canonical:
            raise ValueError()
        return canonical
    except (ValueError, KeyError, TypeError):
        raise ValueError('只允许程序生成的所选站点 /s?k=关键词&page=1–5 搜索地址') from None


def parse_search_page(html, market, source_url, fetched_at=None):
    source_url = validate_search_url(source_url, market)
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(source_url).query))
    root = Document(html).root
    nodes = list(root.nodes())
    cards = [n for n in nodes if n.attrs.get('data-component-type') == 's-search-result']
    if len(cards) > 100:
        raise BsrError('搜索结果卡片数量超过单页限制')
    # An explicit no-results message is valid; an unrecognized empty shell is not.
    if not cards and not any(n.attrs.get('data-component-type') == 's-search-results' and
                            re.search(r'no results|did not match|没有结果|لم نعثر|لا توجد نتائج', n.text(), re.I) for n in nodes):
        raise BsrError('未识别到搜索结果或明确的无结果提示；已停止，请检查页面。')
    items = []
    for position, card in enumerate(cards, 1):
        value = card.attrs.get('data-asin', '')
        if not ASIN.fullmatch(value):
            continue
        descendants = list(card.nodes())
        title = next((n.text() for n in descendants if n.tag == 'h2' and n.text()), '')
        title = title or next((n.attrs['alt'] for n in descendants if n.tag == 'img' and n.attrs.get('alt')), '')
        price, currency, raw = _price(card, market)
        sponsored = any(n.attrs.get('data-component-type') == 'sp-sponsored-result' or
                        _has(n, 'puis-sponsored-label-text', 's-sponsored-label-text') or
                        re.fullmatch(r'Sponsored|إعلان|برعاية|赞助', n.text(), re.I) for n in descendants)
        items.append({'asin': value, 'title': title[:1000], 'url': product_url(market, value),
                      'price': price, 'currency': currency, 'raw_price': raw, 'position': position, 'sponsored': sponsored})
    if cards and not items:
        raise BsrError('搜索卡片存在但没有有效 ASIN，未把解析失败视为无结果。')
    return {'source': 'search', 'source_url': source_url, 'keyword': query['k'], 'page': int(query['page']),
            'items': items, 'fetched_at': fetched_at or datetime.now(timezone.utc).isoformat()}


def check_challenge(text):
    if re.search(r'\bcaptcha\b|validatecaptcha|/captcha/|robot check|automated access|type the characters you see|enter the characters you see|sorry[, ]+we just need to make sure|verify you are (?:a )?human|challenge-platform|awswaf|captcha-input|bm-verify|name=[\"\']amzn[\"\']', text, re.I):
        raise BsrError('Amazon 返回验证或访问挑战；已停止，不尝试绕过。可导入已保存的 HTML。', 'blocked')


class Node:
    __slots__ = ('tag', 'attrs', 'parts', 'parent')
    def __init__(self, tag='', attrs=(), parent=None):
        self.tag, self.attrs, self.parts, self.parent = tag, dict(attrs), [], parent

    def nodes(self):
        stack = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(p for p in reversed(node.parts) if isinstance(p, Node))

    def text(self):
        chunks, stack = [], [self]
        while stack:
            part = stack.pop()
            if isinstance(part, str):
                chunks.append(part)
            elif part.tag not in ('script', 'style', 'noscript'):
                stack.extend(reversed(part.parts))
        return re.sub(r'\s+', ' ', ''.join(chunks)).strip()

    def classes(self):
        return self.attrs.get('class', '').split()


class Document(HTMLParser):
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}
    def __init__(self, html):
        if not isinstance(html, str) or len(html) > MAX_HTML or len(html.encode('utf-8')) > MAX_HTML:
            raise BsrError('HTML 超过 4 MB 或格式无效')
        check_challenge(html)
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack, self.count, self.text_count = [self.root], 0, 0
        try:
            self.feed(html)
            self.close()
        except (RecursionError, AssertionError):
            raise BsrError('HTML 结构无效') from None

    def handle_starttag(self, tag, attrs):
        self.count += 1
        if self.count > 60000 or len(self.stack) > 128 or len(attrs) > 80 or any(len(k) > 200 or len(v or '') > 16384 for k, v in attrs):
            raise BsrError('HTML 结构超过安全限制')
        node = Node(tag, [(k, v or '') for k, v in attrs], self.stack[-1])
        self.stack[-1].parts.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack)-1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.text_count += 1
        if self.text_count > 120000:
            raise BsrError('HTML 文本结构超过安全限制')
        self.stack[-1].parts.append(data)


def _has(node, *names):
    return any(c == n or c.startswith(n + '_') for c in node.classes() for n in names)


def _price(card, market=None):
    nodes = list(card.nodes())
    whole = next((n.text() for n in nodes if _has(n, 'a-price-whole')), '')
    fraction = next((n.text() for n in nodes if _has(n, 'a-price-fraction')), '')
    price_nodes = [n for n in nodes if _has(n, 'a-price')]
    raw = next((n.text() for p in price_nodes for n in p.nodes() if _has(n, 'a-offscreen') and re.search(r'\d', n.text())), '')
    if not raw:
        raw = next((n.text() for n in nodes if _has(n, 'p13n-sc-price', 'a-price')), '')
    symbols = ' '.join(n.text() for n in nodes if _has(n, 'a-price-symbol'))
    currency_text = raw + ' ' + symbols
    currencies = set()
    if re.search(r'\bAED\b|د\.?إ\.?|د\.إ', currency_text, re.I):
        currencies.add('AED')
    if re.search(r'\bSAR\b|ر\.?س\.?|ر\.س', currency_text, re.I):
        currencies.add('SAR')
    if (re.search(r'\bUSD\b|US\$', currency_text, re.I) or (market == 'US' and re.search(r'(?<![A-Za-z])\$', currency_text))) and not re.search(r'(?:S|SG|CA|C|AU|A|HK|NZ)\$', currency_text, re.I):
        currencies.add('USD')
    currency = next(iter(currencies)) if len(currencies) == 1 else ''
    amount = ''
    if whole:
        w = whole.replace(',', '').replace('٬', '').rstrip('.٫').strip()
        f = fraction.strip()
        if re.fullmatch(r'\d{1,9}', w) and (not f or re.fullmatch(r'\d{2}', f)):
            amount = str(int(w)) + ('.' + ''.join(str(int(c)) for c in f) if f else '')
    elif raw:
        numbers = re.findall(r'(?<!\d)(\d{1,3}(?:[,٬]\d{3})+(?:[.٫]\d{2})?|\d+(?:[.٫]\d{2})?)(?!\d)', raw)
        if len(numbers) == 1 and len(numbers[0]) < 20:
            amount = numbers[0].replace(',', '').replace('٬', '').replace('٫', '.')
            amount = ''.join(str(int(c)) if c.isdecimal() else c for c in amount)
    return amount, currency, raw[:200]


def discover_categories(html, market, source_url):
    """Only use links in an actual SalesRank row within product details."""
    source_url = validate_url(source_url, market, 'product')
    root = Document(html).root
    sections = []
    for node in root.nodes():
        ident = node.attrs.get('id', '').lower()
        if ident in ('salesrank', 'bestsellersrank', 'productdetails_db_sections', 'productdetails_detailbullets_sections1', 'detailbullets_feature_div') or ident.startswith('productdetails'):
            sections.append(node)
    found = {}
    for section in sections:
        # A whole productDetails table is not evidence: identify the BSR row first.
        scopes = []
        if section.attrs.get('id', '').lower() in ('salesrank', 'bestsellersrank'):
            scopes.append(section)
        else:
            for n in section.nodes():
                if n.tag not in ('tr', 'li'):
                    continue
                txt = n.text()
                if re.search(r'best\s*sellers?\s*rank|sales\s*rank|تصنيف.*(?:الأكثر|الأفضل)|الأكثر مبيع', txt, re.I):
                    scopes.append(n)
        for scope in scopes:
            for node in scope.nodes():
                if node.tag != 'a' or not node.attrs.get('href'):
                    continue
                try:
                    url = category_url(validate_url(node.attrs['href'], market, base=source_url), market)
                except ValueError:
                    continue
                label = node.text()[:300]
                # General "see top 100" links are not a specific leaf category.
                path = urllib.parse.urlsplit(url).path
                if path in ('/gp/bestsellers', '/zgbs'):
                    continue
                found.setdefault(url, {'url': url, 'label': label or path.rsplit('/', 1)[-1]})
                if len(found) > 30:
                    raise BsrError('商品 BSR 类目链接过多，无法确认')
    if not found:
        raise BsrError('商品页未识别到产品详情中的 BSR 类目，请直接提供类目榜单链接。')
    return list(found.values())


def parse_page(html, market, source_url, page=None, label='', fetched_at=None):
    source_url = validate_url(source_url, market)
    inferred_page = int(dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(source_url).query)).get('pg', '1'))
    if page is None:
        page = inferred_page
    if type(page) is not int or page not in (1, 2):
        raise ValueError('榜单页码无效')
    root = Document(html).root
    nodes = list(root.nodes())
    cards = [n for n in nodes if _has(n, 'zg-item-immersion', '_cDEzb_grid-cell') or (n.attrs.get('id') == 'gridItemRoot' and any(a.attrs.get('id') == 'zg-grid-ctr' for a in _ancestors(n)))]
    # Bound candidates before doing any ancestry work, including malformed imports.
    if not cards or len(cards) > 400:
        raise BsrError('未识别到 BSR 榜单卡片结构；未把空壳页面视为采集成功。')
    # Some variants wrap a grid-cell in gridItemRoot. Parse only the inner card.
    enclosing = {id(a) for card in cards for a in _ancestors(card)}
    cards = [n for n in cards if id(n) not in enclosing]
    if not cards or len(cards) > 200:
        raise BsrError('未识别到 BSR 榜单卡片结构；未把空壳页面视为采集成功。')
    heading = next((n.text() for n in nodes if n.tag == 'h1'), '')[:300]
    label = label or heading or category_url(source_url, market).rsplit('/', 1)[-1]
    items = []
    for card in cards:
        descendants = list(card.nodes())
        rank_text = next((n.text() for n in descendants if _has(n, 'zg-bdg-text', 'zg-badge-text')), '')
        rank_match = re.fullmatch(r'\s*#?\s*([\d,]+)\s*[.)]?\s*', rank_text)
        if not rank_match:
            continue
        try:
            rank = int(rank_match.group(1).replace(',', ''))
        except ValueError:
            continue
        if not 1 <= rank <= 100:
            continue
        links = []
        for n in descendants:
            if n.tag == 'a' and n.attrs.get('href'):
                try:
                    url = validate_url(n.attrs['href'], market, 'product', source_url)
                    links.append((n, url))
                except ValueError:
                    pass
        asins = {url.rsplit('/', 1)[-1] for n, url in links}
        if len(asins) != 1:
            continue
        value = next(iter(asins))
        title = next((n.text() for n in descendants if (n.tag in ('h2', 'h3') or _has(n, 'p13n-sc-truncate', 'p13n-sc-truncated', '_cDEzb_p13n-sc-css-line-clamp-1', '_cDEzb_p13n-sc-css-line-clamp-2', '_cDEzb_p13n-sc-css-line-clamp-3', '_cDEzb_p13n-sc-css-line-clamp-4')) and n.text()), '')
        if not title:
            title = next((n.attrs.get('alt', '') for n in descendants if n.tag == 'img' and n.attrs.get('alt')), '')
        if not title:
            title = next((n.text() for n, url in links if n.text() and not re.fullmatch(r'[\d\W]+', n.text())), '')
        price, currency, raw = _price(card, market)
        items.append({'asin': value, 'title': title[:1000], 'url': product_url(market, value), 'price': price, 'currency': currency, 'raw_price': raw, 'rank': rank})
        if len(items) >= 100:
            break
    if not items:
        raise BsrError('榜单结构存在但未识别到带明确排名和有效 ASIN 的商品；请检查 HTML。')
    next_url = ''
    for node in nodes:
        if node.tag != 'a' or not node.attrs.get('href'):
            continue
        ancestors = [node] + _ancestors(node)
        in_pagination = any(_has(a, 'a-pagination', 'a-last', 's-pagination-next') or a.attrs.get('id') == 'zg-pagination' for a in ancestors)
        if not in_pagination or any(_has(a, 'a-disabled', 's-pagination-disabled') for a in ancestors):
            continue
        try:
            url = validate_url(node.attrs['href'], market, base=source_url)
        except ValueError:
            continue
        pg = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)).get('pg', '')
        if page == 1 and pg == '2' and category_url(url, market) == category_url(source_url, market):
            next_url = url
            break
    return {'items': items, 'page': page, 'category_url': category_url(source_url, market), 'category': label, 'fetched_at': fetched_at or datetime.now(timezone.utc).isoformat(), 'next_url': next_url}


def _ancestors(node):
    result = []
    while node.parent is not None:
        node = node.parent
        result.append(node)
    return result


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _Session:
    def __init__(self, collector, market, cancel, checkpoint, max_requests=12, max_seconds=150):
        self.collector, self.market, self.cancel, self.checkpoint = collector, market, cancel, checkpoint
        self.started, self.requests, self.robots = collector.clock(), 0, None
        self.max_requests, self.max_seconds = max_requests, max_seconds

    def check(self):
        if self.cancel is not None and self.cancel.is_set():
            raise BsrError('采集已取消，已取得结果保留。', 'cancelled')
        if self.collector.clock() - self.started > self.max_seconds or self.requests >= self.max_requests:
            raise BsrError('达到采集时间或请求数量上限，已停止。', 'partial')

    def fetch(self, url, kind='bsr', robots=False):
        url = validate_url(url, self.market, kind)
        for hop in range(4):
            self.check()
            if not robots:
                self.allow(url)
            with self.collector._pace_lock:
                self.check()
                remaining = 5.0 - (self.collector.clock() - self.collector._last_request)
                if remaining > 0:
                    if self.cancel is not None and self.collector.sleep is time.sleep:
                        if self.cancel.wait(remaining):
                            self.check()
                    else:
                        self.collector.sleep(remaining)
                self.check()
                self.collector._last_request = self.collector.clock()
                self.requests += 1
                request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'text/plain' if robots else 'text/html', 'Accept-Encoding': 'identity'})
                try:
                    response = self.collector.opener(request, timeout=12)
                except urllib.error.HTTPError as error:
                    response = error
                except (urllib.error.URLError, TimeoutError, OSError):
                    raise BsrError('公开页面连接失败或超时；未重试。', 'blocked' if robots else 'failed') from None
                try:
                    status = response.status if hasattr(response, 'status') else response.code
                    headers = response.headers
                    final_url = response.geturl() if hasattr(response, 'geturl') else url
                    if final_url != url:
                        # Injected transports must not silently follow redirects either.
                        raise BsrError('传输层发生未经逐跳检查的跳转，已停止。', 'blocked')
                    if status in (403, 429):
                        raise BsrError('Amazon 限制公开访问（HTTP %s），已停止，不重试。' % status, 'blocked')
                    if status in (301, 302, 303, 307, 308):
                        if kind == 'search':
                            raise BsrError('搜索页发生跳转，已停止，不跟随或重试。', 'blocked')
                        location = headers.get('Location', '')
                        try:
                            destination = validate_url(location, self.market, kind, url)
                        except ValueError:
                            raise BsrError('Amazon 跳转地址不在允许范围内，已停止。', 'blocked') from None
                        if destination == url or hop == 3:
                            raise BsrError('跳转循环或超过跳转上限，已停止。', 'blocked')
                        url = destination
                        continue
                    if status != 200:
                        raise BsrError('公开页面返回 HTTP %s，未重试。' % status, 'blocked' if robots else 'failed')
                    if headers.get('Content-Encoding', 'identity').lower() not in ('', 'identity'):
                        raise BsrError('页面压缩格式不受支持，已停止。', 'blocked')
                    cap = 256 * 1024 if robots else MAX_HTML
                    length = headers.get('Content-Length')
                    if length and (not str(length).isdigit() or int(length) > cap):
                        raise BsrError('公开页面响应超过大小限制')
                    raw = bytearray()
                    while True:
                        self.check()
                        part = response.read(min(65536, cap + 1 - len(raw)))
                        if not part:
                            break
                        raw.extend(part)
                        if len(raw) > cap:
                            raise BsrError('公开页面响应超过大小限制')
                    self.check()
                    text = bytes(raw).decode('utf-8', errors='replace')
                    check_challenge(text)
                    return text, url
                except (urllib.error.URLError, TimeoutError, OSError):
                    raise BsrError('公开页面读取失败或超时；已停止。', 'blocked' if robots else 'failed') from None
                finally:
                    response.close()
                    self.collector._last_request = self.collector.clock()
        raise BsrError('超过跳转上限', 'blocked')

    def allow(self, url):
        if self.robots is None:
            try:
                text, _ = self.fetch('https://' + HOSTS[self.market] + '/robots.txt', 'robots', robots=True)
                if '<html' in text.lower() or not re.search(r'^\s*user-agent\s*:', text, re.I | re.M):
                    raise BsrError('robots 无法确认，禁止自动采集。', 'blocked')
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(text.splitlines())
                self.robots = parser
                delays = [parser.crawl_delay(USER_AGENT), parser.crawl_delay('*')]
                # A stricter site delay cannot be met by our fixed cadence: do not crawl.
                if any(d is not None and d > 5 for d in delays):
                    raise BsrError('robots 要求更长等待，本次自动采集停止。', 'blocked')
                rate = parser.request_rate(USER_AGENT)
                if rate and rate.seconds / rate.requests > 5:
                    raise BsrError('robots 请求速率限制无法确认满足，已停止。', 'blocked')
            except (ValueError, OSError) as error:
                raise BsrError('robots 无法确认，禁止自动采集。', 'blocked') from None
        if not self.robots.can_fetch(USER_AGENT, url):
            raise BsrError('robots 禁止访问此公开页面，已停止。', 'blocked')


class Collector:
    def __init__(self, opener=None, sleep=time.sleep, clock=time.monotonic):
        # Explicitly disable ambient HTTP(S)_PROXY; use normal TLS verification.
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open
        self.sleep, self.clock = sleep, clock
        self._last_request, self._pace_lock = float('-inf'), threading.Lock()

    def discover(self, marketplace, asin, cancel=None, checkpoint=None):
        url = product_url(marketplace, asin)
        session = _Session(self, marketplace, cancel, checkpoint)
        expected = product_url(marketplace, asin)
        html, url = session.fetch(url, 'product')
        if url != expected:
            raise BsrError('商品跳转改变目标 ASIN，无法确认其 BSR 类目。', 'blocked')
        return discover_categories(html, marketplace, url)

    def search(self, marketplace, keywords, cancel=None, checkpoint=None):
        if not isinstance(keywords, list) or not 1 <= len(keywords) <= 5:
            raise ValueError('请提供 1–5 个搜索关键词')
        words = list(dict.fromkeys(keywords))
        # Validate the entire plan before issuing any network request.
        for word in words:
            search_url(marketplace, word)
        session = _Session(self, marketplace, cancel, checkpoint, max_requests=27, max_seconds=480)
        count, pages = 0, 0
        for word in words:
            for page in range(1, 6):
                url = search_url(marketplace, word, page)
                html, final_url = session.fetch(url, 'search')
                parsed = parse_search_page(html, marketplace, final_url)
                if checkpoint:
                    checkpoint(parsed)
                pages += 1
                count += len(parsed['items'])
                if not parsed['items']:
                    break
        return {'pages_done': pages, 'count': count}

    def collect(self, marketplace, url, cancel=None, checkpoint=None):
        url = validate_url(url, marketplace)
        session = _Session(self, marketplace, cancel, checkpoint)
        visited, pages, count = set(), 0, 0
        label = ''
        while url and url not in visited and pages < 2 and count < 100:
            session.check()
            visited.add(url)
            html, final_url = session.fetch(url)
            # Redirects may not change the selected category or silently skip pages.
            if category_url(final_url, marketplace) != category_url(url, marketplace):
                raise BsrError('榜单跳转改变类目，已停止。', 'blocked')
            parsed = parse_page(html, marketplace, final_url, label=label)
            label = parsed['category']
            parsed['items'] = parsed['items'][:100-count]
            pages += 1
            count += len(parsed['items'])
            if checkpoint:
                checkpoint(parsed)
            session.check()
            url = parsed['next_url']
        return {'pages_done': pages, 'count': count, 'message': '已读取可识别的公开榜单；仅为候选，非销量或流量数据。'}
