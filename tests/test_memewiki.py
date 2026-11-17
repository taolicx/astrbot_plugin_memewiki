#!/usr/bin/env python3
"""astrbot_plugin_memewiki 独立测试。

不依赖 AstrBot、不联网（在线部分用 mock）。覆盖：
  1. SQLite 五步级联查找（精确 / 别名 / 前缀 / 反向 / 包含 / 查不到）
  2. 消息提取（问梗模式 / 裸词模式 / 停用词 / @提及）
  3. 词条格式化（含截断）
  4. 在线兜底：节流 + 缓存 + 只认完全同名（mock urlopen）
  5. 同步：HTML 解析、dry-run、--no-existing、真实写入 + 备份
  6. 自建词条 add/remove
  7. main.py：用 AstrBot 桩模块加载插件，测预查注入、工具与指令

用法：python3 tests/test_memewiki.py
"""

import asyncio
import importlib.util
import json
import logging
import os
import sqlite3
import sys
import tempfile
import types
import urllib.error
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from memewiki.bootstrap import validate_db  # noqa: E402
from memewiki.db import MemeDB, MemeStore  # noqa: E402
from memewiki.extract import extract_query  # noqa: E402
from memewiki.format import USAGE_GUIDE, format_entry  # noqa: E402
from memewiki.online import CnMemeClient  # noqa: E402
from memewiki.sync import row_from_item, run_sync, split_sections  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


FIXTURE_ROWS = [
    ("牛马", "牛马", "打工人", "自嘲打工人：牛马是……", "出自贴吧", "", "打工人的自嘲", "niuma,牛马人"),
    ("48上单", "48上单", "", "用法说明", "", "", "", ""),
    ("去码头整点薯条", "去码头整点薯条", "", "用法说明", "", "", "肯德基疯狂星期四梗", "kfc"),
    ("KFC", "KFC", "", "用法说明", "", "", "", "肯德基疯狂星期四"),
    ("典", "典", "", "用法说明", "", "", "", "dian"),
]


def make_fixture_db(path):
    db = MemeDB(path)
    for r in FIXTURE_ROWS:
        db.conn.execute("INSERT INTO memes VALUES (?,?,?,?,?,?,?,?)", r)
    db.conn.execute("INSERT OR REPLACE INTO meta VALUES ('built_at', '2026-11-05 12:48:06')")
    db.conn.execute("INSERT OR REPLACE INTO meta VALUES ('count', ?)", (str(len(FIXTURE_ROWS)),))
    db.conn.commit()
    return db


# ---------------------------------------------------------------- 1. 查找
def test_lookup(tmp):
    print("== 1. SQLite 五步级联查找 ==")
    db = make_fixture_db(os.path.join(tmp, "memes.db"))
    check("精确", db.lookup("牛马")["title"] == "牛马")
    check("别名（不区分大小写）", db.lookup("NIUMA")["title"] == "牛马")
    check("前缀", db.lookup("48")["title"] == "48上单")
    check("反向（问句里含标题）", db.lookup("去码头整点薯条是什么梗")["title"] == "去码头整点薯条")
    check("包含（中文≥3字）", db.lookup("码头整")["title"] == "去码头整点薯条")
    check("包含（纯字母2字）", db.lookup("FC")["title"] == "KFC")
    check("查不到", db.lookup("不存在的梗xyz") is None)
    check("空查询", db.lookup("  ") is None)
    check("条数", db.count() == 5)
    db.close()


# ---------------------------------------------------------------- 2. 提取
def test_extract():
    print("== 2. 消息提取 ==")
    check("问梗模式", extract_query("牛马是什么梗") == "牛马")
    check("问梗模式（什么意思）", extract_query("牛马是什么意思") == "牛马")
    check("问梗模式（怎么用）", extract_query("牛马怎么用") == "牛马")
    check("裸词模式", extract_query("牛马") == "牛马")
    check("裸词模式（4字）", extract_query("蚌埠住了") == "蚌埠住了")
    check("停用词", extract_query("在吗") is None)
    check("无语义词", extract_query("这是什么梗") is None)
    check("长句不提取（带句子成分）", extract_query("今天天气怎么样") is None)
    check("长句不提取（带标点）", extract_query("你好，今天天气不错") is None)
    check("@提及剥离", extract_query("@小明 牛马是什么梗") == "牛马")
    check("句子成分", extract_query("你说牛马") is None)
    check("超长词头不提取", extract_query("这是一个非常非常长的词是什么梗") is None)


# ---------------------------------------------------------------- 3. 格式化
def test_format():
    print("== 3. 词条格式化 ==")
    row = {
        "title": "牛马", "tags": "打工人", "usage": "自嘲用法",
        "origin": "出自贴吧", "summary": "", "aliases": "niuma",
    }
    text = format_entry(row)
    check("含标题", "[梗百科]「牛马」" in text)
    check("含标签", "标签：打工人" in text)
    check("含用法", "自嘲用法" in text)
    check("含出处", "出自贴吧" in text)
    long_row = dict(row, usage="用" * 800)
    check("用法截断700字+", format_entry(long_row).endswith("…") or "…" in format_entry(long_row))
    check("指引约150字", 100 < len(USAGE_GUIDE) < 300, f"len={len(USAGE_GUIDE)}")


# ---------------------------------------------------------------- 4. 在线兜底
class FakeResp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_online(tmp):
    print("== 4. 在线兜底（mock） ==")

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        if url.endswith("/%E7%89%9B%E9%A9%AC"):
            body = json.dumps({
                "title": "牛马", "slug": "牛马", "tags": ["打工人"],
                "aliases": ["niuma"], "summary": "打工人自嘲",
                "content_html": "<h2>来源</h2><p>出自贴吧</p><h2>用法</h2><p>自嘲用法</p>",
            }).encode("utf-8")
            return FakeResp(200, body)
        if url.endswith("/mismatch"):
            body = json.dumps({"title": "另一个梗", "slug": "mismatch"}).encode("utf-8")
            return FakeResp(200, body)
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    client = CnMemeClient()
    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        item = client.fetch_exact("牛马")
        check("完全同名命中", item is not None and item["title"] == "牛马")
        check("标题不一致算没查到", client.fetch_exact("mismatch") is None)
        check("404 算没查到", client.fetch_exact("不存在的词") is None)

    # 节流 + 缓存
    calls = {"n": 0}

    class FakeClient:
        def fetch_exact(self, term):
            calls["n"] += 1
            return {
                "title": term, "slug": term, "tags": "", "usage": "用法",
                "origin": "", "other": "", "summary": "", "aliases": "",
            }

    store = MemeStore(tmp, online_client=FakeClient(), online_enabled=True)
    row, src = store.lookup_online("新梗")
    check("在线命中", row is not None and src == "online")
    check("命中后进缓存", store.cache_count() == 1)
    check("10分钟内不重复问", store.lookup_online("新梗") == (None, None) and calls["n"] == 1)
    store._last_online_attempt["新梗"] = 0  # 模拟节流过期
    row, src = store.lookup_online("新梗")
    check("节流过期后重问", row is not None and calls["n"] == 2)
    check("缓存可查", store.lookup_local("新梗")[0] is not None)
    store.close()


# ---------------------------------------------------------------- 5. 同步
class FakeSyncClient:
    def __init__(self):
        self.detail_calls = []

    def list_memes(self, limit=30, offset=0):
        return {
            "items": [
                {"slug": "牛马", "title": "牛马", "updated_at": "2026-11-06T00:00:00"},
                {"slug": "新梗", "title": "新梗", "updated_at": "2026-11-06T00:00:00"},
            ],
            "has_more": False,
        }

    def get_meme(self, slug):
        self.detail_calls.append(slug)
        if slug == "牛马":
            return {
                "title": "牛马", "slug": "牛马", "tags": ["打工人"],
                "aliases": ["niuma"], "summary": "新版含义",
                "content_html": "<h2>来源</h2><p>新来源</p><h2>用法</h2><p>新用法</p>",
            }
        if slug == "新梗":
            return {
                "title": "新梗", "slug": "新梗", "tags": [], "aliases": [],
                "summary": "新梗含义",
                "content_html": "<h2>来源</h2><p>来源说明</p><h2>用法</h2><p>用法说明</p>",
            }
        return None


def test_sync(tmp):
    print("== 5. 同步 ==")
    secs = split_sections(
        "<h2>来源</h2><p>出自贴吧</p>"
        "<h2>用法</h2><ul><li>第一</li><li>第二</li></ul>"
        "<h2>其他</h2><p>备注</p>"
    )
    check("拆节3段", len(secs) == 3)
    check("用法列表转行", secs[1]["content"] == "- 第一\n\n- 第二", secs[1]["content"])
    row = row_from_item({
        "title": "测", "slug": "测", "tags": ["a", "b"], "aliases": ["c"],
        "summary": "摘要",
        "content_html": "<h2>来源</h2><p>来</p><h2>用法</h2><p>用</p><h2>其他</h2><p>备</p>",
    })
    check("row_from_item", row["usage"] == "用" and row["origin"] == "来"
          and row["other"] == "其他\n备" and row["tags"] == "a、b" and row["aliases"] == "c")

    db_path = os.path.join(tmp, "sync.db")
    make_fixture_db(db_path).close()

    r = run_sync(db_path, client=FakeSyncClient(), dry_run=True)
    check("dry-run 识别新增", r["new"] == 1, str(r))
    check("dry-run 识别更新", r["updated"] == 1, str(r))
    check("dry-run 不写库", MemeDB(db_path).count() == 5)
    check("dry-run 列出标题", "新梗" in r.get("would_add", []))

    r2 = run_sync(db_path, client=FakeSyncClient(), dry_run=True, no_existing=True)
    check("--no-existing 不更新", r2["updated"] == 0 and r2["new"] == 1, str(r2))

    r3 = run_sync(db_path, client=FakeSyncClient())
    check("真实写入新增", r3.get("inserted") == 1, str(r3))
    check("真实写入更新", r3.get("updated_count") == 1, str(r3))
    check("写后总数", r3.get("total") == 6, str(r3))
    check("备份文件存在", bool(r3.get("backup")) and os.path.exists(r3["backup"]))
    db = MemeDB(db_path)
    check("新词可查", db.lookup("新梗")["usage"] == "用法说明")
    check("旧词已更新", db.lookup("牛马")["usage"] == "新用法")
    check("meta 已更新", db.meta_get("count") == "6")
    db.close()


# ---------------------------------------------------------------- 6. 自建词条
def test_custom(tmp):
    print("== 6. 自建词条 ==")
    store = MemeStore(tmp, online_client=None, online_enabled=False)
    store.add_custom("测试梗", "测试用法", aliases=["cs"])
    row, src = store.lookup_local("测试梗")
    check("自建精确", row is not None and src == "custom")
    check("自建别名", store.lookup_local("cs")[0] is not None)
    check("自建优先于本地", store.lookup_local("牛马")[0] is None)  # 该库是空的
    check("删除成功", store.remove_custom("测试梗") is True)
    check("删除后查不到", store.lookup_local("测试梗")[0] is None)
    check("删不存在的", store.remove_custom("没有的") is False)
    store.close()


# ---------------------------------------------------------------- 7. main.py（AstrBot 桩）
def _install_astrbot_stubs(data_dir):
    """在 sys.modules 里放最小的 AstrBot 桩，供 main.py 导入。"""
    logging.getLogger("astrbot").setLevel(logging.CRITICAL)

    astrbot = types.ModuleType("astrbot")
    astrbot_api = types.ModuleType("astrbot.api")
    astrbot_api_event = types.ModuleType("astrbot.api.event")
    astrbot_api_provider = types.ModuleType("astrbot.api.provider")
    astrbot_api_star = types.ModuleType("astrbot.api.star")
    astrbot_core = types.ModuleType("astrbot.core")
    astrbot_core_agent = types.ModuleType("astrbot.core.agent")
    astrbot_core_agent_message = types.ModuleType("astrbot.core.agent.message")
    astrbot_core_star = types.ModuleType("astrbot.core.star")
    astrbot_core_star_filter = types.ModuleType("astrbot.core.star.filter")
    astrbot_core_star_filter_command = types.ModuleType("astrbot.core.star.filter.command")

    class AstrBotConfig(dict):
        pass

    astrbot_api.AstrBotConfig = AstrBotConfig
    astrbot_api.logger = logging.getLogger("astrbot")

    class PermissionType:
        ADMIN = "admin"

    def _identity(fn):
        return fn

    class _Filter:
        @staticmethod
        def command(*dargs, **dkwargs):
            return _identity

        @staticmethod
        def llm_tool(*dargs, **dkwargs):
            return _identity

        @staticmethod
        def on_llm_request(*dargs, **dkwargs):
            return _identity

        @staticmethod
        def permission_type(*dargs, **dkwargs):
            return _identity

    _Filter.PermissionType = PermissionType

    class AstrMessageEvent:
        def __init__(self, message_str=""):
            self.message_str = message_str

        def plain_result(self, text):
            return text

    astrbot_api_event.filter = _Filter()
    astrbot_api_event.AstrMessageEvent = AstrMessageEvent

    class ProviderRequest:
        def __init__(self):
            self.prompt = ""
            self.system_prompt = ""
            self.extra_user_content_parts = []

    astrbot_api_provider.ProviderRequest = ProviderRequest

    class Context:
        pass

    class Star:
        def __init__(self, context, config=None):
            self.context = context
            self.config = config

        async def initialize(self):
            pass

        async def terminate(self):
            pass

    class StarTools:
        @classmethod
        def get_data_dir(cls, plugin_name=None):
            return data_dir

    astrbot_api_star.Context = Context
    astrbot_api_star.Star = Star
    astrbot_api_star.StarTools = StarTools

    class TextPart:
        def __init__(self, text=""):
            self.text = text

        def mark_as_temp(self):
            return self

    astrbot_core_agent_message.TextPart = TextPart

    class GreedyStr(str):
        pass

    astrbot_core_star_filter_command.GreedyStr = GreedyStr

    for name, mod in [
        ("astrbot", astrbot),
        ("astrbot.api", astrbot_api),
        ("astrbot.api.event", astrbot_api_event),
        ("astrbot.api.provider", astrbot_api_provider),
        ("astrbot.api.star", astrbot_api_star),
        ("astrbot.core", astrbot_core),
        ("astrbot.core.agent", astrbot_core_agent),
        ("astrbot.core.agent.message", astrbot_core_agent_message),
        ("astrbot.core.star", astrbot_core_star),
        ("astrbot.core.star.filter", astrbot_core_star_filter),
        ("astrbot.core.star.filter.command", astrbot_core_star_filter_command),
    ]:
        sys.modules[name] = mod
    return AstrMessageEvent, ProviderRequest


async def test_main(tmp):
    print("== 7. main.py（AstrBot 桩） ==")
    data_dir = os.path.join(tmp, "plugin_data")
    os.makedirs(data_dir, exist_ok=True)
    make_fixture_db(os.path.join(data_dir, "memes.db")).close()

    AstrMessageEvent, ProviderRequest = _install_astrbot_stubs(data_dir)
    spec = importlib.util.spec_from_file_location(
        "main_test", os.path.join(ROOT, "main.py")
    )
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)

    plugin = main.MemeWikiPlugin(context=None, config={
        "preload": True, "guide": True, "online": False,
        "max_query_chars": 10, "sync_interval_days": 0, "guidance": "",
    })
    await plugin.initialize()
    check("插件初始化", plugin.store is not None and plugin.store.db.count() == 5)

    # 预查注入：命中
    req = ProviderRequest()
    await plugin.on_llm_request(AstrMessageEvent("牛马是什么梗"), req)
    check("预查注入命中", len(req.extra_user_content_parts) == 1
          and "牛马" in req.extra_user_content_parts[0].text)
    check("注入含指引", "梗百科·用法" in req.extra_user_content_parts[0].text)

    # 预查注入：查不到不注入
    req2 = ProviderRequest()
    await plugin.on_llm_request(AstrMessageEvent("不存在的梗xyz是什么梗"), req2)
    check("预查查不到不注入", len(req2.extra_user_content_parts) == 0)

    # 预查注入：普通消息不注入
    req3 = ProviderRequest()
    await plugin.on_llm_request(AstrMessageEvent("今天天气真好"), req3)
    check("普通消息不注入", len(req3.extra_user_content_parts) == 0)

    # 工具
    out = await plugin.search_meme_wiki(AstrMessageEvent(), "牛马")
    check("工具命中", "牛马" in out)
    out2 = await plugin.search_meme_wiki(AstrMessageEvent(), "不存在的梗xyz")
    check("工具查不到", "没收录" in out2)

    # 指令：/梗
    async def collect(gen):
        return [x async for x in gen]

    res = await collect(plugin.cmd_meme(AstrMessageEvent(), "牛马"))
    check("/梗命中", len(res) == 1 and "牛马" in res[0])
    res = await collect(plugin.cmd_meme(AstrMessageEvent(), ""))
    check("/梗无参给用法", len(res) == 1 and "用法" in res[0])

    # 指令：/加梗、/删梗、/梗库状态
    res = await collect(plugin.cmd_add_term(AstrMessageEvent(), "测试梗 | 测试用法"))
    check("/加梗", len(res) == 1 and "已记下" in res[0])
    res = await collect(plugin.cmd_status(AstrMessageEvent()))
    check("/梗库状态", len(res) == 1 and "5 条" in res[0] and "自建词条：1 条" in res[0])
    res = await collect(plugin.cmd_del_term(AstrMessageEvent(), "测试梗"))
    check("/删梗", len(res) == 1 and "已删掉" in res[0])

    await plugin.terminate()
    check("插件终止无残留任务", plugin._bg_task is None)


def main():
    print("astrbot_plugin_memewiki 测试")
    with tempfile.TemporaryDirectory() as tmp:
        test_lookup(os.path.join(tmp, "t1"))
        test_extract()
        test_format()
        test_online(os.path.join(tmp, "t4"))
        test_sync(os.path.join(tmp, "t5"))
        test_custom(os.path.join(tmp, "t6"))
        # bootstrap 校验
        print("== 8. 快照校验 ==")
        db_path = os.path.join(tmp, "t8.db")
        make_fixture_db(db_path).close()
        ok, n = validate_db(db_path)
        check("有效库通过校验", ok and n == 5)
        bad = os.path.join(tmp, "bad.db")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("not a sqlite file")
        check("损坏文件校验失败", validate_db(bad)[0] is False)
        asyncio.run(test_main(os.path.join(tmp, "t7")))
    print(f"\n{PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
