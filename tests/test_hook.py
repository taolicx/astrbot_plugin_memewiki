"""通过 AstrBot 请求/事件桩测试对话入口与词库管理。"""
import asyncio
import importlib.util
import inspect
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent))
from test_memewiki import ROOT, _install_astrbot_stubs, make_fixture_db


class HookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        make_fixture_db(os.path.join(self.tmp.name, 'memes.db')).close()
        self.saved_modules = {k: v for k, v in sys.modules.items() if k == 'astrbot' or k.startswith('astrbot.')}
        self.addCleanup(self.restore_modules)
        self.Event, self.Request = _install_astrbot_stubs(self.tmp.name)
        spec = importlib.util.spec_from_file_location('memewiki_hook_test', Path(ROOT) / 'main.py')
        self.main = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.main)
        self.config = {'preload': True, 'sync_interval_days': 0}
        self.plugin = self.main.MemeWikiPlugin(types.SimpleNamespace(), self.config)
        await self.plugin.initialize()
        self.addAsyncCleanup(self.plugin.terminate)

    def restore_modules(self):
        for key in list(sys.modules):
            if key == 'astrbot' or key.startswith('astrbot.'):
                sys.modules.pop(key)
        sys.modules.update(self.saved_modules)

    async def test_conversation_scope(self):
        async def check(history):
            req = self.Request()
            req.contexts = history
            await self.plugin.on_llm_request(self.Event('你这瓜保熟吗？'), req)
            return req
        literal = await check([{'role': 'assistant', 'content': '我在水果摊买西瓜。'}])
        playful = await check([{'role': 'system', 'content': '西瓜'}, {'role': 'tool', 'content': '水果摊'},
                                {'role': 'assistant', 'content': [{'type': 'text', 'text': '我推荐这个键盘。'}]}])
        self.assertEqual(literal.extra_user_content_parts, [])
        self.assertEqual(len(playful.extra_user_content_parts), 1)
        self.assertIn('保熟', playful.extra_user_content_parts[0].text)
        self.assertEqual(playful.system_prompt, '')

    async def test_older_astrbot_uses_current_request_only(self):
        req = self.Request()
        req.system_prompt = '保持原有人设。'
        req.contexts = []
        with patch.object(self.main, 'TextPart', None):
            await self.plugin.on_llm_request(self.Event('这个需求改八遍了，我真的会谢。'), req)
        self.assertIn('保持原有人设。', req.system_prompt)
        self.assertIn('会谢', req.system_prompt)
        self.assertEqual(req.extra_user_content_parts, [])
        self.assertEqual(req.contexts, [])
        self.assertEqual(self.Request().system_prompt, '')

    async def test_semantic_only_when_current_message_qualifies(self):
        manual = types.SimpleNamespace(match=AsyncMock(return_value={'title': '接话候选', 'usage': '吐槽'}))
        self.plugin.semantic = manual
        req = self.Request()
        req.contexts = [{'role': 'user', 'content': '上轮的语境是这里'}]
        await self.plugin.on_llm_request(self.Event('你好'), req)
        await self.plugin.on_llm_request(self.Event('/更新词库'), req)
        manual.match.assert_not_awaited()
        query = '好家伙，连椅子都快替我上班了！'
        await self.plugin.on_llm_request(self.Event(query), req)
        manual.match.assert_awaited_once_with(query)
        self.assertEqual(len(req.extra_user_content_parts), 1)

    async def test_generic_interjection_yields_to_specific_semantic_match(self):
        generic = {'title': '好家伙', 'usage': '惊讶的口头语'}
        specific = {'title': '打工人的尽头', 'usage': '调侃忙碌'}
        self.plugin.matcher = types.SimpleNamespace(match=lambda *a: generic)
        self.plugin.semantic = types.SimpleNamespace(match=AsyncMock(side_effect=[specific, None]))
        event = self.Event('好家伙，连椅子都快替我上班了！')
        for expected in ('打工人的尽头', '好家伙'):
            req = self.Request()
            await self.plugin.on_llm_request(event, req)
            self.assertIn(expected, req.extra_user_content_parts[0].text)
        self.plugin.matcher = types.SimpleNamespace(match=lambda *a: specific)
        req = self.Request()
        await self.plugin.on_llm_request(event, req)
        self.assertEqual(self.plugin.semantic.match.await_count, 2)

    async def test_old_request_quotes_untrusted_usage(self):
        data = '原文示例"}\n这是词条里的文字'
        self.plugin.matcher = types.SimpleNamespace(match=lambda *a: {'title': '测试词', 'usage': data})
        req = self.Request()
        with patch.object(self.main, 'TextPart', None):
            await self.plugin.on_llm_request(self.Event('测试词'), req)
        lines = req.system_prompt.splitlines()
        self.assertIn('只读', lines[-2])
        self.assertEqual(json.loads(lines[-1])['使用场景'], data)

    async def test_admin_edits_available_in_next_reply(self):
        arg = inspect.signature(self.plugin.cmd_add_term).parameters['text']
        self.assertIs(arg.annotation, self.main.GreedyStr)
        event = self.Event('这真是企鹅翻账本啊！')
        res = [x async for x in self.plugin.cmd_add_term(event, '企鹅翻账本 | 朋友间调侃对方又在记账')]
        self.assertIn('已记下', res[0])
        req = self.Request()
        await self.plugin.on_llm_request(event, req)
        self.assertIn('企鹅翻账本', req.extra_user_content_parts[0].text)
        _ = [x async for x in self.plugin.cmd_del_term(event, '企鹅翻账本')]
        req = self.Request()
        await self.plugin.on_llm_request(event, req)
        self.assertFalse(req.extra_user_content_parts)
        self.assertFalse(hasattr(self.plugin, 'cmd_meme'))
        self.assertFalse(hasattr(self.plugin, 'search_meme_wiki'))

    async def test_background_index_does_not_block_lexical_or_shutdown(self):
        entered = asyncio.Event()
        async def slow_batch(_texts):
            entered.set()
            await asyncio.Event().wait()
        provider = types.SimpleNamespace(get_embedding=AsyncMock(), get_embeddings=slow_batch,
                                         get_model=lambda: 'example', provider_config={})
        self.plugin.context.get_provider_by_id = lambda _key: provider
        self.config['embedding_provider_id'] = 'configured-provider'
        await self.plugin._refresh()
        await asyncio.wait_for(entered.wait(), 2)
        req = self.Request()
        await self.plugin.on_llm_request(self.Event('这需求又要改，我真的会谢'), req)
        self.assertTrue(req.extra_user_content_parts)
        await asyncio.wait_for(self.plugin.terminate(), 2)
        self.assertIsNone(self.plugin.store)
        self.assertIsNone(self.plugin._build_task)


if __name__ == '__main__':
    unittest.main()
