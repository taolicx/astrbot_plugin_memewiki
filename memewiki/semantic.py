"""按词条内容和嵌入模型缓存向量。查询只使用一次完整构建的索引。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import operator
import sqlite3
import sys
from array import array
from contextlib import closing

logger = logging.getLogger(__name__)


def _field(value, limit):
    if isinstance(value, (tuple, list)):
        value = '、'.join(str(item) for item in value)
    return str(value or '').strip()[:limit]


def _documents(entries):
    by_title = {row.get('title'): row for row in entries}
    docs = []
    for row in by_title.values():
        title = _field(row.get('title'), 200)
        if not title:
            continue
        parts = [title]
        for key, limit in (('aliases', 160), ('phrases', 320), ('variants', 320),
                           ('scene', 160), ('dialogue_hint', 220), ('usage', 380), ('summary', 120)):
            value = _field(row.get(key), limit)
            if value:
                parts.append(value)
        texts = ['\n'.join(parts)]
        variants = row.get('variants')
        if isinstance(variants, (list, tuple)) and variants:
            texts.append(title)
            texts.extend(_field(phrase, 160) for phrase in variants[:4] if isinstance(phrase, str))
        for text in dict.fromkeys(texts):
            if text:
                docs.append((row, hashlib.sha256(text.encode()).hexdigest(), text))
    return docs


def _normalize(raw):
    values = array('f', raw)
    length = math.sqrt(sum(x * x for x in values))
    if not math.isfinite(length) or length == 0:
        raise ValueError('嵌入模型返回了空向量或无效数值')
    return array('f', (x / length for x in values))


def _connect(path):
    conn = sqlite3.connect(path, timeout=5)
    conn.execute('CREATE TABLE IF NOT EXISTS vectors ('
                 'model TEXT, digest TEXT, data BLOB NOT NULL, PRIMARY KEY(model, digest))')
    return conn


def _load(path, model, docs):
    wanted = {key for _, key, _ in docs}
    found = {}
    with closing(_connect(path)) as db:
        for key, blob in db.execute('SELECT digest, data FROM vectors WHERE model=?', (model,)):
            if key not in wanted or not blob or len(blob) % 4:
                continue
            vec = array('f')
            vec.frombytes(blob)
            if sys.byteorder != 'little':
                vec.byteswap()
            found[key] = vec
    return found


def _save(path, model, batch):
    def records():
        for key, vec in batch:
            if sys.byteorder != 'little':
                vec = array('f', vec)
                vec.byteswap()
            yield model, key, vec.tobytes()
    with closing(_connect(path)) as db, db:
        db.executemany('INSERT OR REPLACE INTO vectors VALUES (?, ?, ?)', records())


def _make_snapshot(docs, found):
    # _documents 为每个词条连续排列原文、短标题、台词变体。
    rows, starts = [], []
    for idx, (row, _, _) in enumerate(docs):
        if not rows or row['title'] != rows[-1]['title']:
            rows.append(row)
            starts.append(idx)
    vecs = [found[key] for _, key, _ in docs]
    if not vecs:
        return None
    if len({len(vec) for vec in vecs}) != 1:
        raise ValueError('同一模型返回了不同维数的向量，请确认模型配置及缓存身份')
    try:
        import numpy as np
    except ImportError:
        return rows, vecs, starts, None
    return rows, np.asarray(vecs, dtype=np.float32), starts, np


def _nearest(snapshot, vec, min_score, min_margin):
    rows, vectors, starts, np = snapshot
    if len(vec) != len(vectors[0]):
        return None
    if np is not None:
        scores = np.einsum('ij,j->i', vectors, np.asarray(vec, dtype=np.float32))
        scores = np.maximum.reduceat(scores, starts)
        first = int(scores.argmax())
        score = float(scores[first])
        scores[first] = -math.inf
        second = float(scores.max())
    else:
        first, score, second = -1, -math.inf, -math.inf
        for idx, start in enumerate(starts):
            end = starts[idx + 1] if idx + 1 < len(starts) else len(vectors)
            value = max(sum(map(operator.mul, vec, vectors[j])) for j in range(start, end))
            if value > score:
                first, score, second = idx, value, score
            elif value > second:
                second = value
    if score >= min_score and score - second >= min_margin:
        return rows[first]
    return None


class SemanticIndex:
    """rebuild 在调用方管理的后台任务中运行；match 可随时调用。"""

    def __init__(self, cache_path, provider, model_key, *, min_score=0.80,
                 min_margin=0.06, query_timeout=1.2, batch_size=32):
        if not model_key:
            raise ValueError('向量缓存需要模型身份')
        self.path = cache_path
        self.provider = provider
        self.model_key = model_key
        self.min_score = min_score
        self.min_margin = min_margin
        self.query_timeout = query_timeout
        self.batch_size = max(1, int(batch_size))
        self._snapshot = None
        self._build_lock = asyncio.Lock()
        self._query_lock = asyncio.Lock()

    @property
    def ready(self):
        return self._snapshot is not None

    async def rebuild(self, entries):
        async with self._build_lock:
            self._snapshot = None
            docs = await asyncio.to_thread(_documents, entries)
            found = await asyncio.to_thread(_load, self.path, self.model_key, docs)
            pending = list({key: text for _, key, text in docs if key not in found}.items())
            for offset in range(0, len(pending), self.batch_size):
                batch = pending[offset:offset + self.batch_size]
                texts = [text for _, text in batch]
                raw = await asyncio.wait_for(self.provider.get_embeddings(texts), timeout=45)
                if len(raw) != len(batch):
                    raise ValueError('嵌入模型返回的向量数量和词条数不一致')
                vectors = await asyncio.to_thread(lambda: [_normalize(item) for item in raw])
                records = list(zip((key for key, _ in batch), vectors))
                await asyncio.to_thread(_save, self.path, self.model_key, records)
                found.update(records)
            self._snapshot = await asyncio.to_thread(_make_snapshot, docs, found)

    async def match(self, text):
        snapshot = self._snapshot
        if snapshot is None or not text.strip() or self._query_lock.locked():
            return None
        async with self._query_lock:
            try:
                raw = await asyncio.wait_for(self.provider.get_embedding(text[:500]),
                                             timeout=self.query_timeout)
                row = await asyncio.to_thread(_nearest, snapshot, _normalize(raw),
                                              self.min_score, self.min_margin)
                return row if snapshot is self._snapshot else None
            except Exception as exc:
                # 提供商的错误可能含 URL、请求或凭据；仅记录异常类型。
                logger.debug('跳过本轮语义识别 (%s)', type(exc).__name__)
                return None
