"""离线验证索引复用、歧义拒绝、失败恢复和查询超时。"""

import asyncio
import os
import tempfile
import unittest

from memewiki.semantic import SemanticIndex


class FakeProvider:
    def __init__(self):
        self.documents = []
        self.query = [1, 0]
        self.fail_at = None
        self.delay = 0

    async def get_embeddings(self, texts):
        if self.fail_at is not None and len(self.documents) >= self.fail_at:
            raise ConnectionError()
        self.documents.extend(texts)
        return [[1, 0] if t.startswith('甲') else [0, 1] for t in texts]

    async def get_embedding(self, text):
        await asyncio.sleep(self.delay)
        return self.query


class SemanticTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'index.sqlite3')
        self.provider = FakeProvider()
        self.entries = [{'title': '甲', 'usage': '第一条'}, {'title': '乙'}]

    def make(self, model='模型A', **opts):
        return SemanticIndex(self.path, self.provider, model, **opts)

    async def test_ranking_confidence_and_gap(self):
        index = self.make(min_score=0.5, min_margin=0.1)
        self.assertIsNone(await index.match('还没有索引'))
        await index.rebuild(self.entries)
        self.assertEqual(await index.match('第一句改写'), self.entries[0])
        self.provider.query = [0.72, 0.70]
        self.assertIsNone(await index.match('两个意思都像'))
        self.provider.query = [-1, 0]
        self.assertIsNone(await index.match('和哪个梗都不像'))
        self.provider.query = [float('nan'), 0]
        self.assertIsNone(await index.match('服务异常'))

    async def test_incremental_content_and_model_isolation(self):
        await self.make().rebuild(self.entries)
        renamed = [dict(self.entries[0], variants=['甲乙丙']), self.entries[1]]
        index = self.make()
        await index.rebuild(renamed)
        self.assertEqual(len(self.provider.documents), 5)
        self.assertIn('甲乙丙', self.provider.documents[-1])
        await self.make().rebuild(renamed)
        self.assertEqual(len(self.provider.documents), 5)
        await self.make('模型B').rebuild(renamed)
        self.assertEqual(len(self.provider.documents), 9)
        await index.rebuild(renamed[1:])
        self.assertIsNone(await index.match('已经删除的甲'))

    async def test_margin_ignores_other_lines_from_same_entry(self):
        entries = [{'title': '乙类第一条', 'variants': ['甲句', '甲换种说法']}, {'title': '乙类第二条'}]
        index = self.make(min_margin=0.2)
        await index.rebuild(entries)
        self.assertIn('甲句', self.provider.documents)
        self.assertIn('甲换种说法', self.provider.documents)
        self.assertEqual(await index.match('像第一条的两种说法'), entries[0])
        entries[1]['variants'] = ['甲句']
        await index.rebuild(entries)
        self.assertIsNone(await index.match('两个梗都有这句台词'))
        self.assertEqual(self.provider.documents.count('甲句'), 1)

    async def test_failed_build_recovers_from_cached_batches(self):
        self.provider.fail_at = 1
        index = self.make(batch_size=1)
        with self.assertRaises(ConnectionError):
            await index.rebuild(self.entries)
        self.assertFalse(index.ready)
        self.provider.fail_at = None
        await index.rebuild(self.entries)
        self.assertEqual(len(self.provider.documents), 2)
        self.assertTrue(index.ready)

    async def test_timeout_and_busy_queries_do_not_queue(self):
        index = self.make(query_timeout=0.02)
        await index.rebuild(self.entries)
        self.provider.delay = 10
        first = asyncio.create_task(index.match('慢请求'))
        await asyncio.sleep(0)
        self.assertIsNone(await index.match('后续会话'))
        self.assertIsNone(await asyncio.wait_for(first, timeout=1))
        self.provider.delay = 0
        self.assertEqual(await index.match('下轮恢复'), self.entries[0])
