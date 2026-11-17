"""梗百科 AstrBot 插件。

本地梗词库 + 自动查梗注入上下文，让机器人认得群里的梗。

功能：
  - 自动预查：收到消息时识别梗词，把词条（用法/出处/标签）注入 LLM 上下文；
  - 模型工具 search_meme_wiki(query)：模型主动查梗，查不到返回 found=false 语义，绝不编造；
  - /梗 <词>：手动查梗；/梗库状态：看词库规模与版本；
  - /更新词库（管理员）：从 cnmeme.wiki 同步最新词库；
  - /加梗 词名 | 用法、/删梗 <词名>（管理员）：自建词条，同步时不会被覆盖；
  - 每周自动同步（可在配置里改间隔或关闭）。

词库来源：dsh-memewiki 的公开词库快照（MIT），首次启动时下载；
之后通过 cnmeme.wiki 公开 API 增量同步。详见 README.md 与 NOTICE。
"""

from __future__ import annotations

import asyncio
import os
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import TextPart
from astrbot.core.star.filter.command import GreedyStr

try:
    from .memewiki.bootstrap import download_snapshot, validate_db
    from .memewiki.db import MemeStore
    from .memewiki.extract import extract_query
    from .memewiki.format import NOT_FOUND_TEMPLATE, USAGE_GUIDE, format_entry
    from .memewiki.online import CnMemeClient
    from .memewiki.sync import _parse_time, run_sync
except ImportError:  # 独立加载（测试）时走绝对导入
    from memewiki.bootstrap import download_snapshot, validate_db
    from memewiki.db import MemeStore
    from memewiki.extract import extract_query
    from memewiki.format import NOT_FOUND_TEMPLATE, USAGE_GUIDE, format_entry
    from memewiki.online import CnMemeClient
    from memewiki.sync import _parse_time, run_sync


class MemeWikiPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.store: MemeStore | None = None
        self._bg_task: asyncio.Task | None = None
        self._sync_running = False
        self._last_sync: str | None = None

    def _cfg(self, key, default):
        try:
            v = self.config.get(key, default)
        except Exception:
            return default
        return default if v is None else v

    def _guide_text(self) -> str:
        custom = self._cfg("guidance", "")
        if isinstance(custom, str) and custom.strip():
            return custom.strip()
        return USAGE_GUIDE if self._cfg("guide", True) else ""

    # ---------- 生命周期 ----------

    async def initialize(self):
        data_dir = StarTools.get_data_dir("astrbot_plugin_memewiki")
        db_path = os.path.join(data_dir, "memes.db")

        # 首次启动：本地没词库就下载公开快照；失败也不影响加载。
        ok, n = validate_db(db_path)
        if not ok:
            logger.info("[梗百科] 本地词库不存在，正在下载公开快照…")
            dl_ok, info = await asyncio.to_thread(download_snapshot, db_path)
            if dl_ok:
                logger.info(f"[梗百科] 词库快照下载完成（{info}）")
            else:
                logger.warning(f"[梗百科] 词库快照下载失败：{info}，以降级模式运行")

        self.store = MemeStore(
            data_dir,
            online_client=CnMemeClient(),
            online_enabled=bool(self._cfg("online", False)),
        )
        st = self.store.status()
        logger.info(
            f"[梗百科] 已加载：本地 {st['count']} 条，自建 {st['custom_count']} 条，"
            f"在线兜底 {'开' if st['online_enabled'] else '关'}"
        )
        self._bg_task = asyncio.create_task(self._sync_loop())

    async def terminate(self):
        if self._bg_task is not None:
            self._bg_task.cancel()
            try:
                await self._bg_task
            except asyncio.CancelledError:
                pass
            self._bg_task = None
        if self.store is not None:
            self.store.close()
            self.store = None

    # ---------- 自动同步 ----------

    def _sync_due(self) -> bool:
        try:
            interval = int(self._cfg("sync_interval_days", 7))
        except Exception:
            interval = 7
        if interval <= 0 or self.store is None:
            return False
        built_at = self.store.db.meta_get("built_at")
        if not built_at:
            return True
        ts = _parse_time(built_at)
        if ts <= 0:
            return True
        return (time.time() - ts) > interval * 86400

    async def _sync_loop(self):
        await asyncio.sleep(60)  # 让启动先完成
        try:
            while True:
                if self._sync_due():
                    await self._run_sync(auto=True)
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise

    async def _run_sync(self, auto: bool):
        if self._sync_running or self.store is None:
            return
        self._sync_running = True
        try:
            result = await asyncio.to_thread(
                run_sync, self.store.db_path, None, False, False,
                lambda m: logger.info(f"[梗百科] 同步：{m}"),
            )
            self._last_sync = time.strftime("%Y-%m-%d %H:%M:%S")
            logger.info(
                f"[梗百科] 同步完成（{'自动' if auto else '手动'}）："
                f"新增 {result.get('inserted', 0)} 条，"
                f"更新 {result.get('updated_count', 0)} 条，"
                f"失败 {result.get('failed', 0)} 条，"
                f"总数 {result.get('total', '?')} 条"
            )
        except Exception as e:
            logger.warning(f"[梗百科] 同步失败：{e}")
        finally:
            self._sync_running = False

    # ---------- 查词（本地 + 可选在线兜底） ----------

    async def _lookup(self, query: str):
        if self.store is None:
            return None, None
        row, source = self.store.lookup_local(query)
        if row is None and self._cfg("online", False):
            row, source = await asyncio.to_thread(self.store.lookup_online, query)
        return row, source

    # ---------- 自动预查注入 ----------

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self._cfg("preload", True) or self.store is None:
            return
        q = extract_query(event.message_str or "", self._cfg("max_query_chars", 10))
        if not q:
            return
        row, _source = await self._lookup(q)
        if row is None:
            return  # 查不到就什么都不注入，别让模型硬猜
        parts = []
        guide = self._guide_text()
        if guide:
            parts.append(guide)
        parts.append(format_entry(row))
        req.extra_user_content_parts.append(
            TextPart(text="\n".join(parts)).mark_as_temp()
        )

    # ---------- 模型工具 ----------

    @filter.llm_tool(name="search_meme_wiki")
    async def search_meme_wiki(self, event: AstrMessageEvent, query: str) -> str:
        """查网络梗、缩写、黑话的意思。

        Args:
            query(string): 要查的词（梗名、缩写、黑话）
        """
        q = (query or "").strip()
        if not q:
            return "要查什么？给我一个词。"
        if self.store is None:
            return "梗百科还没准备好，稍后再试。"
        row, _source = await self._lookup(q)
        if row is None:
            return NOT_FOUND_TEMPLATE.format(q=q)
        parts = []
        guide = self._guide_text()
        if guide:
            parts.append(guide)
        parts.append(format_entry(row))
        return "\n".join(parts)
    # ---------- 指令 ----------

    @filter.command("梗")
    async def cmd_meme(self, event: AstrMessageEvent, query: str = ""):
        q = (query or "").strip()
        if not q:
            yield event.plain_result("用法：/梗 <词>，例如：/梗 牛马")
            return
        row, _source = await self._lookup(q)
        if row is None:
            yield event.plain_result(f"梗百科里没收录「{q}」。")
            return
        yield event.plain_result(format_entry(row))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("更新词库")
    async def cmd_sync(self, event: AstrMessageEvent):
        if self._sync_running:
            yield event.plain_result("词库正在同步中，稍后再试。")
            return
        asyncio.create_task(self._run_sync(auto=False))
        yield event.plain_result(
            "开始同步词库，大约需要 4–5 分钟，完成后用 /梗库状态 查看结果。"
        )
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("加梗", alias={"添加梗"})
    async def cmd_add_term(self, event: AstrMessageEvent, text: GreedyStr):
        raw = str(text or "").strip()
        if not raw:
            yield event.plain_result(
                "用法：/加梗 词名 | 用法\n例如：/加梗 牛马 | 打工人自嘲，指没日没夜干活像牛马一样"
            )
            return
        if "|" in raw:
            title, usage = raw.split("|", 1)
        else:
            parts = raw.split(None, 1)
            title = parts[0]
            usage = parts[1] if len(parts) > 1 else ""
        title, usage = title.strip(), usage.strip()
        if not title or not usage:
            yield event.plain_result(
                "用法：/加梗 词名 | 用法\n例如：/加梗 牛马 | 打工人自嘲，指没日没夜干活像牛马一样"
            )
            return
        try:
            self.store.add_custom(title, usage)
        except Exception as e:
            yield event.plain_result(f"添加失败：{e}")
            return
        yield event.plain_result(f"已记下「{title}」。同步词库时不会被覆盖。")
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("删梗")
    async def cmd_del_term(self, event: AstrMessageEvent, term: str = ""):
        t = (term or "").strip()
        if not t:
            yield event.plain_result("用法：/删梗 <词名>")
            return
        if self.store.remove_custom(t):
            yield event.plain_result(f"已删掉自建词条「{t}」。")
        else:
            yield event.plain_result(
                f"自建词条里没有「{t}」。注意：/删梗 只删自己加的词，删不了内置词库。"
            )
    @filter.command("梗库状态")
    async def cmd_status(self, event: AstrMessageEvent):
        if self.store is None:
            yield event.plain_result("梗百科还没准备好，稍后再试。")
            return
        st = self.store.status()
        lines = [
            "[梗百科]",
            f"本地词条：{st['count']} 条",
            f"词库版本：{st['built_at'] or '未知'}",
            f"自建词条：{st['custom_count']} 条",
            f"在线缓存：{st['cache_count']} 条",
            f"在线兜底：{'开' if st['online_enabled'] else '关'}",
        ]
        if self._last_sync:
            lines.append(f"上次同步：{self._last_sync}")
        yield event.plain_result("\n".join(lines))
