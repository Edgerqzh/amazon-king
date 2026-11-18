"""Explicitly saved photo-search snapshots in an independent database."""
import csv
from contextlib import contextmanager
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sqlite3
import threading
import uuid

from .competitors import _identity, _id, _text, _csv_cell, _xlsx


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
