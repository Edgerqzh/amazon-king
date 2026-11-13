"""Read an Amazon processing report and say, per SKU and field, what went wrong.

Amazon returns either a tab-delimited text report (original-record-number, sku,
error-code, error-type, error-message) or an Excel copy of the submitted
template with the problems written as cell comments and coloured cells. Its
exact Excel layout is not documented, so this reader looks for both shapes
instead of fixed positions. It only reads; nothing is sent anywhere.
"""
import colorsys
import csv
import io
import posixpath
import re
import zipfile
from pathlib import Path

from . import library, metadata
from .filling import Workbook, canonical, column_index, column_name, template_info
from .product_types import norm

N = metadata.N
MAX_ISSUES = 20000
SEVERITIES = ('error', 'warning', 'suggestion', 'unknown')
TEXT_SUFFIXES = {'.txt', '.tsv', '.csv'}
BOOK_SUFFIXES = {'.xlsx', '.xlsm', '.xls'}

# Explanations are deliberately cautious: codes only where the meaning is well
# established, otherwise the wording of Amazon's own message decides.
RULES = [
    ('brand', '品牌未获批准或需要品牌授权', '该品牌需要在亚马逊获得批准或授权。核对品牌名拼写是否与品牌备案完全一致；不是品牌方时，需先在卖家后台申请品牌授权。',
     {'5665', '5461'}, r'brand.{0,60}(approv|authori[sz]|registry|protected|restrict)|(approv|authori[sz]).{0,60}brand'),
    ('catalog_conflict', '与亚马逊已有商品的信息冲突', '这个条码已对应亚马逊上的现有商品（ASIN），而填写的标题、品牌等关键信息与之不一致。先核对条码是否用对；确是同一商品时，按目录现有信息填写。',
     {'8541', '8542'}, r'conflict|differ(s|ent)? from|does not match.{0,40}(catalog|asin)|match(es)? (multiple|more than one) asin'),
    ('not_enough', '资料不足，无法匹配或创建商品', '亚马逊无法凭现有资料识别或新建这个商品。按英文原文列出的属性补全（常见是品牌、商品类型、条码或关键规格），再重新生成。',
     {'8560'}, r'not enough|insufficient|(cannot|unable to) (create|match)|missing attributes'),
    ('product_id', '条码（UPC/EAN）问题', '核对条码是否有效、是否属于该品牌，条码类型是否选对；没有条码的商品需要先申请 GTIN 豁免。',
     {'8572'}, r'\b(upc|ean|gtin|isbn|jan|barcode)\b|product[ _]id|gs1'),
    ('approval', '类目或商品需要销售许可', '这个类目、商品或站点需要先申请销售许可，获批后再上传。',
     set(), r'approval|approved|authori[sz]|restricted|gated|not (eligible|permitted|allowed to sell)'),
    ('variation', '变体（父子体）关系有问题', '检查父 SKU 是否已存在或与子体一起上传、父子关系与变体主题是否一致，以及子体的变体属性（如颜色、尺码）是否都已填写。',
     set(), r'variation|\bparent|\bchild|parentage|relationship'),
    ('image', '图片链接有问题', '确认图片链接可以公开访问、直接指向图片文件，并满足亚马逊的尺寸和主图要求。',
     set(), r'\bimage|\.jpe?g\b|\.png\b|\burl\b'),
    ('too_long', '内容超过长度或数量上限', '缩短这一格的内容后重新生成（标题、卖点等字段有字数上限）。',
     set(), r'exceed|maximum (length|number|of)|too long|longer than|more than \d+ (characters|values)|character limit'),
    ('number_format', '这一格需要填写数字', '只保留数字，去掉货币符号、单位和千位分隔符。',
     set(), r'must be (a |an )?(positive |valid )?(number|numeric|decimal|integer|whole number)|not a (valid )?number|non-?numeric'),
    ('date_format', '日期格式不对', '按模板要求的日期格式填写，例如 2026-11-12。',
     set(), r'date.{0,40}(invalid|format)|(invalid|incorrect).{0,40}date'),
    ('missing', '必填信息缺失', '补全这一项后重新生成。资料表里明明有这一列时，检查“填写表格”中该字段是否对应到了正确的来源列。',
     {'99010'}, r'required|not supplied|missing|must be (provided|supplied)|cannot be (blank|empty)|is empty|必填|缺少'),
    ('invalid_value', '填写的值不被接受', '对照模板的下拉选项填写，注意拼写、大小写和单位；有下拉列表的字段只能填列表里的值。',
     set(), r'not (a )?valid|invalid|valid values?|not one of|not allowed|not (recogni[sz]ed|supported)|unrecogni[sz]ed|无效|不是有效'),
    ('price', '价格有问题', '核对售价和币种；价格明显偏离时亚马逊会拦截。',
     set(), r'price'),
    ('sku', 'SKU 有问题', '确认 SKU 与卖家后台一致；改价或部分更新只能用于已经存在的 SKU。',
     set(), r'\bsku\b'),
    ('duplicate', '内容重复', '同一批里出现了重复的行或值，删除重复项后再上传。',
     set(), r'duplicate'),
]
FALLBACK = ('other', '其他问题', '暂无对应的中文解释，请按亚马逊英文原文处理。')
# When a message names no field, these problem types still point at one obvious field.
KIND_FIELDS = {'brand': 'brand_name', 'product_id': 'product_id', 'price': 'standard_price', 'image': 'main_image_url'}
COMPILED = [(kind, title, hint, codes, re.compile(pattern, re.I)) for kind, title, hint, codes, pattern in RULES]

HEADERS = {
    'sku': {'sku', 'sellersku', 'itemsku', 'contributionsku', '卖家sku'},
    'record': {'originalrecordnumber', 'recordnumber', 'record', 'row', 'rownumber', 'linenumber', '行号'},
    'code': {'errorcode', 'code', 'errorwarningcode', 'messagecode', 'errorcodes', '错误代码', '代码'},
    'type': {'errortype', 'type', 'severity', 'level', 'messagetype', '错误类型', '类型'},
    'message': {'errormessage', 'message', 'messages', 'description', 'details', 'errordescription', 'messagetext', 'errormessages', '错误信息', '错误消息', '说明', '信息'},
    'field': {'columnname', 'attribute', 'attributename', 'attributenames', 'field', 'fieldname', 'column', '属性', '字段', '列名'},
}
REPORT_COLUMN = re.compile(r'error|warning|message|feedback|processing|错误|警告|信息', re.I)


def is_report_header(text):
    """A column Amazon added to the template (status, messages), not a template field."""
    return bool(text) and bool(REPORT_COLUMN.search(text)) and not re.search(r'[#\[]|^[a-z0-9]+(_[a-z0-9]+)+$', text)


def explain(code, message):
    """Chinese title and advice for one Amazon message."""
    code = (code or '').strip()
    for kind, title, hint, codes, pattern in COMPILED:
        if code and code in codes:
            return kind, title, hint
    for kind, title, hint, codes, pattern in COMPILED:
        if pattern.search(message or ''):
            return kind, title, hint
    return FALLBACK


def severity_of(text):
    value = norm(text)
    if not value:
        return None
    if value.startswith(('error', 'fatal', '错误')):
        return 'error'
    if value.startswith(('warning', 'warn', '警告')):
        return 'warning'
    if value.startswith(('suggestion', 'info', 'recommend', '建议')):
        return 'suggestion'
    return None


def severity_of_colour(rgb):
    """Amazon marks errors orange, warnings yellow and suggestions blue."""
    if not rgb or len(rgb) != 6:
        return None
    try:
        r, g, b = (int(rgb[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return None
    hue, light, sat = colorsys.rgb_to_hls(r, g, b)
    hue *= 360
    if sat < .3 or light > .97 or light < .15:
        return None
    if hue < 40 or hue >= 330:
        return 'error'
    if hue < 70:
        return 'warning'
    if 170 <= hue < 260:
        return 'suggestion'
    return None


def split_message(text):
    """One comment can hold several messages; keep each line that says something."""
    parts = []
    for line in re.split(r'[\r\n]+', text or ''):
        line = line.strip(' \t-•·')
        if len(line) >= 3 and not re.fullmatch(r'[\w .]{1,40}:', line):
            parts.append(line[:2000])
    return parts


def message_code(message):
    match = re.search(r'(?:\b(?:error|warning|code|错误|警告)\b\s*[:#(\[]?\s*|^\s*\[?)(\d{4,6})\b', message, re.I)
    return match[1] if match else ''


def field_tokens(message):
    tokens = re.findall(r"'([^'\n]{1,160})'|\"([^\"\n]{1,160})\"|“([^”\n]{1,160})”|‘([^’\n]{1,160})’", message)
    found = [next(t for t in group if t) for group in tokens]
    listed = re.search(r'missing attributes?\s*[:：]?\s*([\w ,.#\[\]=-]{1,400})', message, re.I)
    if listed:
        found += [t.strip() for t in re.split(r'[,;]', listed[1]) if t.strip()]
    found += re.findall(r'\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?:\[[^\]\s]{1,80}\])*(?:#\d+(?:\.[a-z_0-9]+)*)?', message)
    return list(dict.fromkeys(found))


def base_key(key):
    return re.split(r'[\[#.]', key, maxsplit=1)[0]


class FieldIndex:
    """Find template fields named in a message, by attribute key or visible label."""

    def __init__(self, fields):
        self.fields = [f for f in fields or [] if f.get('key')]
        self.exact, self.base, self.label, self.canonical = {}, {}, {}, {}
        for f in self.fields:
            if f.get('canonical'):
                self.canonical.setdefault(f['canonical'], f)
            self.exact.setdefault(norm(f['key']), f)
            self.base.setdefault(norm(base_key(f['key'])), f)
            if f.get('label'):
                self.label.setdefault(norm(f['label']), f)
            if f.get('canonical'):
                self.base.setdefault(norm(f['canonical']), f)

    def find(self, message, hint=''):
        for token in ([hint] if hint else []) + field_tokens(message):
            key = norm(token)
            if not key:
                continue
            field = self.exact.get(key) or self.label.get(key) or self.base.get(norm(base_key(token)))
            if field:
                return field
        return None


def header_kind(text):
    value = norm(text)
    return next((kind for kind, names in HEADERS.items() if value in names), None)


def find_table(rows):
    """Locate a header row (sku or record number, plus a message) in up to 30 rows."""
    for number, values in rows[:30]:
        columns = {}
        for index, value in enumerate(values):
            kind = header_kind(value)
            if kind and kind not in columns:
                columns[kind] = index
        if 'message' in columns and ('sku' in columns or 'record' in columns):
            return number, columns
    return None, None


def summary_counts(lines):
    text = '\n'.join(lines)
    result = {}
    for key, pattern in (('processed', r'records? processed\D{0,20}(\d+)'), ('successful', r'records? successful\D{0,20}(\d+)'),
                         ('errors', r'records? with errors?\D{0,20}(\d+)'), ('warnings', r'records? with warnings?\D{0,20}(\d+)')):
        match = re.search(pattern, text, re.I)
        if match:
            result[key] = int(match[1])
    return result


class Collector:
    def __init__(self, fields):
        self.index = FieldIndex(fields)
        self.issues, self.seen, self.truncated = [], set(), False

    def add(self, *, sku='', message, code='', severity=None, record=None, row=None, field=None, field_hint=''):
        message = (message or '').strip()
        if not message:
            return
        code = (code or message_code(message)).strip()[:12]
        if field is None:
            field = self.index.find(message, field_hint)
        elif self.index.fields:
            # Prefer the library template's column: Amazon may insert report columns in front.
            field = self.index.exact.get(norm(field['key'])) or field
        sku = str(sku or '').strip()[:200]
        key = (sku, (field or {}).get('key') or field_hint, code, message)
        if key in self.seen:
            return
        if len(self.issues) >= MAX_ISSUES:
            self.truncated = True
            return
        self.seen.add(key)
        kind, title, hint = explain(code, message)
        if field is None and kind in KIND_FIELDS:
            field = self.index.canonical.get(KIND_FIELDS[kind])
        self.issues.append({
            'severity': severity if severity in SEVERITIES else (severity_of(message) or 'unknown'),
            'sku': sku, 'record': record, 'row': row,
            'column': (field or {}).get('column') or '',
            'field_key': (field or {}).get('key') or '', 'field_label': (field or {}).get('label') or field_hint or '',
            'code': code, 'message': message, 'kind': kind, 'title': title, 'hint': hint,
        })


def decode_text(data):
    if data[:2] in (b'\xff\xfe', b'\xfe\xff'):
        return data.decode('utf-16')
    try:
        return data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return data.decode('cp1252', errors='replace')


def read_text(data, collector):
    text = decode_text(data)
    if '\x00' in text:
        raise ValueError('处理报告不是可读取的文本文件')
    lines = text.splitlines()
    tabbed = any('\t' in line for line in lines[:60])
    # Amazon's text report is plain tab-separated: quotes inside messages are text, not CSV quoting.
    reader = csv.reader(lines, delimiter='\t', quoting=csv.QUOTE_NONE) if tabbed else csv.reader(lines)
    rows = [(i + 1, [c.strip() for c in values]) for i, values in enumerate(reader)]
    header, columns = find_table(rows)
    if header is None:
        raise ValueError('没有找到处理报告的表头（需要含 sku 或 original-record-number 与 error-message 列）。请上传亚马逊卖家后台下载的处理报告原文件')
    read_rows(rows, header, columns, collector)
    return summary_counts(lines[:header])


def read_rows(rows, header, columns, collector, sheet=None):
    def get(values, kind):
        index = columns.get(kind)
        return values[index].strip() if index is not None and index < len(values) else ''
    for number, values in rows:
        if number <= header or not any(values):
            continue
        message, record = get(values, 'message'), get(values, 'record')
        collector.add(sku=get(values, 'sku'), message=message, code=get(values, 'code'), severity=severity_of(get(values, 'type')),
                      record=int(record) if record.isdigit() else None, field_hint=get(values, 'field'),
                      row=number if sheet else None)


def book_styles(archive):
    """Fill colour (RRGGBB) for each cell style index."""
    if 'xl/styles.xml' not in archive.namelist():
        return []
    root = metadata.parse_xml(archive.read('xl/styles.xml'))
    fills = []
    for fill in root.findall('s:fills/s:fill', N):
        pattern, colour = fill.find('s:patternFill', N), None
        if pattern is not None and pattern.get('patternType') not in (None, 'none'):
            for tag in ('s:fgColor', 's:bgColor'):
                node = pattern.find(tag, N)
                if node is not None and node.get('rgb'):
                    colour = node.get('rgb')[-6:].upper()
                    break
        fills.append(colour)
    result = []
    for xf in root.findall('s:cellXfs/s:xf', N):
        try:
            result.append(fills[int(xf.get('fillId', '0'))])
        except (ValueError, IndexError):
            result.append(None)
    return result


def sheet_comments(archive, part):
    """Cell reference -> comment text, from legacy and threaded comments."""
    rels_path = posixpath.join(posixpath.dirname(part), '_rels', posixpath.basename(part) + '.rels')
    if rels_path not in archive.namelist():
        return {}
    comments = {}
    for rel in metadata.parse_xml(archive.read(rels_path)):
        kind, target = rel.get('Type', ''), rel.get('Target', '')
        if rel.get('TargetMode') == 'External' or not kind.endswith(('/comments', '/threadedComment')):
            continue
        path = posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join(posixpath.dirname(part), target))
        if not path.startswith('xl/') or '..' in path.split('/') or path not in archive.namelist():
            continue
        root = metadata.parse_xml(archive.read(path))
        authors = [a.text or '' for a in root.iter() if a.tag.endswith('}author')]
        for node in root.iter():
            if not node.tag.endswith(('}comment', '}threadedComment')) or not node.get('ref'):
                continue
            text = ''.join(t.text or '' for t in node.iter() if t.tag.endswith('}t') or t.tag.endswith('}text') and len(t) == 0)
            for author in authors:
                if author and text.startswith(author + ':'):
                    text = text[len(author) + 1:]
            ref = node.get('ref').split(':')[0]
            if text.strip() and ref not in comments:
                comments[ref] = text.strip()
    return comments


def read_book(data, collector):
    library.workbook(data, '.xlsx')
    book = Workbook(data)
    try:
        styles = book_styles(book.archive)
        summary, used, tables = {}, False, []
        for name, part in book.paths.items():
            root, cells = book.sheet(name)
            grid = {}
            for ref, value in cells.items():
                x, y = column_index(ref)
                grid.setdefault(y, {})[x] = value
            ordered = [(y, [grid[y].get(x, '') for x in range(max(grid[y]) + 1)]) for y in sorted(grid)]
            summary.update({k: v for k, v in summary_counts(['\t'.join(v) for _, v in ordered[:60]]).items() if k not in summary})
            comments = sheet_comments(book.archive, part)
            attribute = max((y for y, values in ordered[:30] for v in values if v and canonical(v) == 'sku'), default=None)
            # A copy of the submitted template has attribute keys (item_sku, brand#1.value) in its header row;
            # a plain error list ("SKU / Error Code / Error Message") does not.
            keylike = sum(bool(re.search(r'[#\[]', v) or re.fullmatch(r'[a-z][a-z0-9]*(_[a-z0-9]+)+', v)) for v in grid.get(attribute, {}).values() if v)
            if attribute and (comments or keylike >= 3):
                used = read_template_sheet(root, grid, attribute, comments, styles, collector) or used
            else:
                tables.append((name, ordered))
        # Error lists on other sheets come second, so they can name fields from the template header.
        for name, ordered in tables:
            header, columns = find_table(ordered)
            if header is not None:
                read_rows(ordered, header, columns, collector, sheet=name)
                used = True
        if not used and not collector.issues:
            raise ValueError('这个 Excel 里没有找到处理报告的错误信息（批注或错误清单）。请确认上传的是卖家后台下载的处理报告，而不是自己提交的原表')
        return summary
    finally:
        book.close()


def read_template_sheet(root, grid, attribute, comments, styles, collector):
    """The submitted template with Amazon's comments and colours on the problem cells."""
    keys, labels = grid.get(attribute, {}), grid.get(attribute - 1, {})
    sku_column = next(x for x, v in keys.items() if v and canonical(v) == 'sku')
    report_columns = [x for x in set(keys) | set(labels) if is_report_header(keys.get(x, '')) or (not keys.get(x) and is_report_header(labels.get(x, '')))]
    fields = {x: {'key': key, 'label': labels.get(x) or key, 'column': column_name(x)} for x, key in keys.items() if key and x not in report_columns}
    if not collector.index.fields:
        collector.index = FieldIndex(list(fields.values()))
    colours = {}
    for cell in root.findall('s:sheetData/s:row/s:c', N):
        style = cell.get('s')
        if style and style.isdigit() and int(style) < len(styles) and styles[int(style)]:
            colours[cell.get('r')] = styles[int(style)]
    found = False
    for ref, text in comments.items():
        try:
            x, y = column_index(ref)
        except ValueError:
            continue
        if y <= attribute:
            continue
        sku = grid.get(y, {}).get(sku_column, '')
        field = fields.get(x)
        for message in split_message(text):
            collector.add(sku=sku, message=message, severity=severity_of(message) or severity_of_colour(colours.get(ref)), row=y, field=field)
            found = True
    for y, values in grid.items():
        if y <= attribute:
            continue
        for x in report_columns:
            text = values.get(x, '')
            if text and not re.fullmatch(r'[\d.\s]+', text):
                header = labels.get(x) or keys.get(x) or ''
                for message in split_message(text):
                    collector.add(sku=values.get(sku_column, ''), message=message, severity=severity_of(message) or severity_of(header), row=y)
                    found = True
    return found


def template_fields(data, record):
    """Field keys, labels and columns of a library template, to name fields in report messages."""
    book = Workbook(data)
    try:
        info = template_info(book, record.get('marketplace') or '', record.get('table_type') or 'upload')
    finally:
        book.close()
    return [{key: f[key] for key in ('key', 'label', 'column', 'canonical')} for f in info['fields']]


def parse(data, filename, fields=None):
    """Return issues with SKU, field, severity and a Chinese explanation."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ValueError('处理报告为空')
    suffix = Path(filename or '').suffix.lower()
    is_book = data[:4] == b'PK\x03\x04'
    if is_book and suffix not in BOOK_SUFFIXES:
        raise ValueError('处理报告扩展名与内容不一致，请上传卖家后台下载的原文件')
    if not is_book and suffix not in TEXT_SUFFIXES:
        if suffix == '.xls':
            raise ValueError('这是旧版 .xls 文件，请用 Excel 另存为 .xlsx 后再导入')
        raise ValueError('处理报告只支持 .xlsx、.xlsm 或 .txt 文件')
    collector = Collector(fields)
    try:
        summary = read_book(bytes(data), collector) if is_book else read_text(bytes(data), collector)
    except zipfile.BadZipFile:
        raise ValueError('处理报告文件已损坏，请重新从卖家后台下载') from None
    return summarise(collector, 'excel' if is_book else 'text', filename, summary)


def summarise(collector, kind, filename, summary):
    """Counts and groups for a set of collected issues (a report file or an API pre-check)."""
    issues = collector.issues
    counts = {level: sum(i['severity'] == level for i in issues) for level in SEVERITIES}
    groups = {}
    for issue in issues:
        group = groups.setdefault((issue['kind'], issue['severity']), {'kind': issue['kind'], 'severity': issue['severity'], 'title': issue['title'], 'hint': issue['hint'], 'count': 0, 'skus': [], 'fields': [], 'codes': []})
        group['count'] += 1
        for key, value, cap in (('skus', issue['sku'], 200), ('fields', issue['field_label'], 12), ('codes', issue['code'], 8)):
            if value and value not in group[key] and len(group[key]) < cap:
                group[key].append(value)
    order = {level: i for i, level in enumerate(SEVERITIES)}
    return {
        'format': kind, 'filename': filename, 'summary': summary, 'counts': counts,
        'skus': len({i['sku'] for i in issues if i['sku']}), 'error_skus': len({i['sku'] for i in issues if i['sku'] and i['severity'] == 'error'}),
        'matched_fields': sum(bool(i['field_key']) for i in issues), 'issues': issues, 'truncated': collector.truncated,
        'groups': sorted(groups.values(), key=lambda g: (order[g['severity']], -g['count'])),
    }
