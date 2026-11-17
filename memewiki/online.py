"""cnmeme.wiki 公开 API 客户端。

端点（来自 dsh-memewiki scripts/sync-cnmeme.mjs，已验证）：
  列表：GET https://cnmeme.wiki/api/v1/public/memes?limit=30&offset=N
        -> {"items": [{"slug", "title", "updated_at", ...}], "has_more": bool}
  详情：GET https://cnmeme.wiki/api/v1/public/memes/{slug}
        -> 词条本身，或 {"schema_version": ..., "item": {...}}

在线兜底只用详情接口：把要查的词当 slug 去问，回来先核对 title
与查询词完全一致，不一致就当没查到（不编造）。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://cnmeme.wiki/api/v1/public/memes"
USER_AGENT = "astrbot-memewiki/0.1.0"


class CnMemeError(Exception):
    pass


class CnMemeClient:
    def __init__(self, timeout: int = 20, user_agent: str = USER_AGENT):
        self.timeout = timeout
        self.user_agent = user_agent

    def _get_json(self, path: str, retries: int = 4):
        url = API_BASE + path
        last_exc = None
        for i in range(retries):
            try:
                req = urllib.request.Request(url, headers={"user-agent": self.user_agent})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    if resp.status != 200:
                        raise CnMemeError(f"HTTP {resp.status}")
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code == 429 or e.code >= 500:
                    last_exc = e
                    time.sleep(min(15, 1.5 * (2 ** i)))
                    continue
                raise CnMemeError(f"HTTP {e.code}") from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_exc = e
                if i == retries - 1:
                    raise CnMemeError(str(e)) from e
                time.sleep(1.5 * (i + 1))
        raise CnMemeError(str(last_exc) if last_exc else "重试次数用尽")

    def list_memes(self, limit: int = 30, offset: int = 0) -> dict:
        """取一页词条清单。"""
        data = self._get_json(f"?limit={int(limit)}&offset={int(offset)}")
        return data or {}

    def get_meme(self, slug: str):
        """取一条词条详情；不存在返回 None。"""
        data = self._get_json("/" + urllib.parse.quote(slug))
        if not data:
            return None
        if isinstance(data, dict) and isinstance(data.get("item"), dict):
            return data["item"]
        return data if isinstance(data, dict) else None

    def fetch_exact(self, term: str):
        """在线兜底：把词当 slug 问一次，只认 title 与查询词完全一致的条目。"""
        item = self.get_meme(term)
        if not item:
            return None
        if str(item.get("title", "")).strip() != term.strip():
            return None
        return item
