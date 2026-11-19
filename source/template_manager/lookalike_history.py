"""Explicitly saved photo-search snapshots in an independent database."""
import csv
from contextlib import contextmanager
from datetime import datetime, timezone
import io
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import uuid
import xml.etree.ElementTree as ET
import zipfile

DENIED = '记录不存在或无权访问'


def _text(value, cap, name='文本', empty=True):
    if not isinstance(value, str) or len(value) > cap or (not empty and not value.strip()) or any(ord(c) < 32 and c not in '\n\r\t' for c in value):
        raise ValueError(name + '格式无效')
    return value.strip()


def _identity(user, edit=False):
    if not isinstance(user, dict):
        raise ValueError(DENIED)
    name, role = user.get('username'), user.get('role')
    if not isinstance(name, str) or not name.strip() or len(name) > 200 or role not in ('admin', 'editor', 'viewer'):
        raise ValueError(DENIED)
    if edit and role not in ('admin', 'editor'):
        raise ValueError(DENIED)
    return name


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value):
        raise ValueError(DENIED)
    return value


def _csv_cell(value):
    value = str(value)
    return "'" + value if value.lstrip(' \t\r\n﻿').startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')) else value


def _xml_text(value):
    return ''.join(c for c in str(value) if c in '\t\n\r' or 0x20 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF)


def _xlsx(sheets):
    """Minimal OOXML with inline strings only; no formulas, links or remote parts."""
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    relns = 'http://schemas.openxmlformats.org/package/2006/relationships'
    docrel = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    contentns = 'http://schemas.openxmlformats.org/package/2006/content-types'
    def xml(node):
        return ET.tostring(node, encoding='utf-8', xml_declaration=True)
    workbook = ET.Element('workbook', xmlns=ns, attrib={'xmlns:r': docrel})
    names = ET.SubElement(workbook, 'sheets')
    workbook_rels = ET.Element('Relationships', xmlns=relns)
    types = ET.Element('Types', xmlns=contentns)
    ET.SubElement(types, 'Default', Extension='rels', ContentType='application/vnd.openxmlformats-package.relationships+xml')
    ET.SubElement(types, 'Default', Extension='xml', ContentType='application/xml')
    ET.SubElement(types, 'Override', PartName='/xl/workbook.xml', ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml')
    ET.SubElement(types, 'Override', PartName='/xl/styles.xml', ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml')
    ET.SubElement(workbook_rels, 'Relationship', Id='rIdStyles', Type=docrel + '/styles', Target='styles.xml')
    styles = ET.Element('styleSheet', xmlns=ns)
    fonts = ET.SubElement(styles, 'fonts', count='1')
    font = ET.SubElement(fonts, 'font')
    ET.SubElement(font, 'sz', val='11')
    ET.SubElement(font, 'name', val='Arial')
    fills = ET.SubElement(styles, 'fills', count='2')
    for pattern in ('none', 'gray125'):
        ET.SubElement(ET.SubElement(fills, 'fill'), 'patternFill', patternType=pattern)
    borders = ET.SubElement(styles, 'borders', count='1')
    ET.SubElement(borders, 'border')
    for name in ('cellStyleXfs', 'cellXfs'):
        ET.SubElement(ET.SubElement(styles, name, count='1'), 'xf', numFmtId='0', fontId='0', fillId='0', borderId='0')
    ET.SubElement(ET.SubElement(styles, 'cellStyles', count='1'), 'cellStyle', name='Normal', xfId='0', builtinId='0')
    root_rels = ET.Element('Relationships', xmlns=relns)
    ET.SubElement(root_rels, 'Relationship', Id='rId1', Type=docrel + '/officeDocument', Target='xl/workbook.xml')
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('_rels/.rels', xml(root_rels))
        for number, (name, rows) in enumerate(sheets, 1):
            ET.SubElement(names, 'sheet', name=name, sheetId=str(number), attrib={'r:id': 'rId' + str(number)})
            ET.SubElement(workbook_rels, 'Relationship', Id='rId' + str(number), Type=docrel + '/worksheet', Target='worksheets/sheet%s.xml' % number)
            ET.SubElement(types, 'Override', PartName='/xl/worksheets/sheet%s.xml' % number, ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml')
            sheet = ET.Element('worksheet', xmlns=ns)
            sheet_data = ET.SubElement(sheet, 'sheetData')
            for r, row in enumerate(rows, 1):
                row_node = ET.SubElement(sheet_data, 'row', r=str(r))
                for c, value in enumerate(row, 1):
                    letter, column = '', c
                    while column:
                        column, remainder = divmod(column-1, 26)
                        letter = chr(65+remainder) + letter
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                        # Real numbers, so prices can be sorted and filtered in Excel.
                        ET.SubElement(ET.SubElement(row_node, 'c', r=letter+str(r)), 'v').text = repr(value)
                        continue
                    cell = ET.SubElement(row_node, 'c', r=letter+str(r), t='inlineStr')
                    inline = ET.SubElement(cell, 'is')
                    ET.SubElement(inline, 't', attrib={'xml:space': 'preserve'}).text = _xml_text(value)
            z.writestr('xl/worksheets/sheet%s.xml' % number, xml(sheet))
        z.writestr('xl/styles.xml', xml(styles))
        z.writestr('xl/workbook.xml', xml(workbook))
        z.writestr('xl/_rels/workbook.xml.rels', xml(workbook_rels))
        z.writestr('[Content_Types].xml', xml(types))
    return stream.getvalue()


TABLE_ROWS = 3000
TABLE_COLUMNS = 40
TABLE_CELL = 5000


def table(body):
    """The rows the page is showing -> one Excel sheet. Text and numbers only; nothing is stored."""
    rows = body.get('rows') if isinstance(body, dict) else None
    if not isinstance(rows, list) or not 1 <= len(rows) <= TABLE_ROWS:
        raise ValueError('表格行数须为 1–%d 行' % TABLE_ROWS)
    clean = []
    for row in rows:
        if not isinstance(row, list) or len(row) > TABLE_COLUMNS:
            raise ValueError('表格每行最多 %d 列' % TABLE_COLUMNS)
        cells = []
        for value in row:
            if value is None:value = ''
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise ValueError('表格单元格只能是文字或数字')
            if isinstance(value, float) and not math.isfinite(value):value = ''
            if isinstance(value, str):value = _csv_cell(value[:TABLE_CELL])
            cells.append(value)
        clean.append(cells)
    return _xlsx([('以图找同款', clean)])

class History:
    def __init__(self, path, service):
        self.path, self.service = Path(path), service
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and tables != {'lookalike_history', 'lookalike_meta'}:
                raise ValueError('以图找同款历史必须使用独立数据库')
            if not tables:
                connection.execute('CREATE TABLE lookalike_meta (version INTEGER NOT NULL)')
                connection.execute('INSERT INTO lookalike_meta VALUES (1)')
                connection.execute('CREATE TABLE lookalike_history (id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL, created_at TEXT NOT NULL, snapshot TEXT NOT NULL)')
            if connection.execute('SELECT version FROM lookalike_meta').fetchall() != [(1,)]:
                raise ValueError('以图找同款历史数据库版本不受支持')

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(str(self.path), timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def save(self, user, body):
        owner = _identity(user, True)
        if not isinstance(body, dict):
            raise ValueError('保存参数无效')
        title = _text(body.get('title', '以图找同款'), 200, '历史名称', empty=False)
        groups = self.service.snapshot(user, body.get('entries'), body.get('save_images', False))
        ident, stamp = uuid.uuid4().hex, datetime.now(timezone.utc).isoformat()
        data = {'id': ident, 'owner': owner, 'title': title, 'created_at': stamp,
                'images_saved': body.get('save_images', False), 'groups': groups}
        with self.lock, self.connect() as connection:
            connection.execute('INSERT INTO lookalike_history VALUES (?,?,?,?,?)',
                               (ident, owner, title, stamp, json.dumps(data, ensure_ascii=False, allow_nan=False)))
        return self.get(user, ident)

    def list(self, user):
        _identity(user)
        with self.connect() as connection:
            rows = connection.execute('SELECT id, owner, title, created_at FROM lookalike_history ORDER BY created_at DESC').fetchall()
        return {'history': [dict(zip(('id', 'owner', 'title', 'created_at'), row)) for row in rows]}

    def get(self, user, ident):
        name = _identity(user)
        with self.connect() as connection:
            row = connection.execute('SELECT snapshot FROM lookalike_history WHERE id=?', (_id(ident),)).fetchone()
        if row is None:
            raise ValueError('历史记录不存在')
        data = json.loads(row[0])
        data['can_delete'] = name == data['owner'] or user['role'] == 'admin'
        return data

    def delete(self, user, body):
        _identity(user, True)
        data = self.get(user, body.get('id'))
        if not data['can_delete']:
            raise PermissionError('只有保存此记录的成员和管理员能删除')
        if body.get('confirmed') is not True:
            raise ValueError('请明确确认删除历史记录')
        with self.lock, self.connect() as connection:
            connection.execute('DELETE FROM lookalike_history WHERE id=?', (data['id'],))
        return {'ok': True}

    def export(self, user, ident, kind='csv'):
        data = self.get(user, ident)
        name = 'lookalike-' + data['id'][:8] + '.' + kind
        if kind == 'json':
            return json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8'), name, 'application/json; charset=utf-8'
        if kind not in ('csv', 'xlsx'):
            raise ValueError('导出格式无效')
        rows = [['哪张图', '站点', '核心词', '查询时间', 'ASIN', '标题', '品牌', '类目', 'AI判断', '相似度', 'AI依据', '依据', '最低价', '币种', '报价查询时间', '各卖家报价']]
        for group in data['groups']:
            for item in group['items']:
                offers = item.get('offers') or {}
                lowest = offers.get('lowest') or {}
                rows.append([group['name'], group['marketplace'], '；'.join(group['keywords']), group['queried_at'],
                             item['asin'], item['title'], item.get('brand', ''), item.get('category', ''),
                             {'same': '同款', 'similar': '相似', 'different': '不同'}.get(item.get('verdict'), '未比对'),
                             item.get('similarity') if item.get('similarity') is not None else '', item.get('ai_reason', ''),
                             '；'.join(item.get('evidence', [])), str(lowest.get('total', '')), lowest.get('currency', ''),
                             offers.get('queried_at', ''), json.dumps(offers, ensure_ascii=False)])
        if kind == 'xlsx':
            return _xlsx([('以图找同款', rows)]), name, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        stream = io.StringIO(newline='')
        csv.writer(stream).writerows([[_csv_cell(cell) for cell in row] for row in rows])
        return ('\ufeff' + stream.getvalue()).encode('utf-8'), name, 'text/csv; charset=utf-8'
