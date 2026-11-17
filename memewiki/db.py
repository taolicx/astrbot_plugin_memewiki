"""本地梗词库：SQLite 词库 + 自建词条 + 在线缓存。

词库表结构与 dsh-memewiki 的 data/memes.db 完全一致（MIT）：

    CREATE TABLE memes (
        title TEXT PRIMARY KEY, slug TEXT, tags TEXT,
        usage TEXT, origin TEXT, other TEXT, summary TEXT, aliases TEXT
    );
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);

查找分五步（与 dsh-memewiki 的查词库逻辑一致）：
    1. 标题完全相等；
    2. 别名整项相等（逗号分隔，不区分大小写）；
    3. 标题以查询词开头（取最短）；
    4. 查询词里包含标题（取最长）；
    5. 标题包含查询词（中文至少 3 字，纯数字/字母 2 字放宽；取最短）。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS memes (
    title TEXT PRIMARY KEY,
    slug TEXT,
    tags TEXT,
    usage TEXT,
    origin TEXT,
    other TEXT,
    summary TEXT,
    aliases TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

COLUMNS = ("title", "slug", "tags", "usage", "origin", "other", "summary", "aliases")

_ALNUM2 = re.compile(r"^[0-9a-z]+$", re.IGNORECASE)


def row_to_dict(row):
    """把 sqlite3.Row / dict 统一成 {列名: str} 的词条字典。"""
    if row is None:
        return None
    if isinstance(row, dict):
        return {k: ("" if row.get(k) is None else str(row.get(k))) for k in COLUMNS}
    return {k: ("" if row[k] is None else str(row[k])) for k in COLUMNS}


class MemeDB:
    """SQLite 词库。所有访问都加锁，避免同步线程与事件循环并发冲突。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 8000")
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def close(self):
        with self._lock:
            try:
                self.conn.close()
            except Exception:
                pass

    def lookup(self, query: str):
        """五步级联查找，返回词条 dict 或 None。"""
        q = (query or "").strip()
        if not q:
            return None
        with self._lock:
            cur = self.conn.cursor()
            row = cur.execute("SELECT * FROM memes WHERE title = ?", (q,)).fetchone()
            if row:
                return row_to_dict(row)
            row = cur.execute(
                "SELECT * FROM memes WHERE aliases IS NOT NULL "
                "AND instr(',' || lower(aliases) || ',', ',' || lower(?) || ',') > 0 LIMIT 1",
                (q,),
            ).fetchone()
            if row:
                return row_to_dict(row)
            row = cur.execute(
                "SELECT * FROM memes WHERE lower(title) LIKE lower(?) || '%' "
                "ORDER BY length(title) ASC LIMIT 1",
                (q,),
            ).fetchone()
            if row:
                return row_to_dict(row)
            row = cur.execute(
                "SELECT * FROM memes WHERE length(title) >= 2 AND instr(?, title) > 0 "
                "ORDER BY length(title) DESC LIMIT 1",
                (q,),
            ).fetchone()
            if row:
                return row_to_dict(row)
            if len(q) >= 3 or (len(q) >= 2 and _ALNUM2.match(q)):
                row = cur.execute(
                    "SELECT * FROM memes WHERE instr(title, ?) > 0 "
                    "ORDER BY length(title) ASC LIMIT 1",
                    (q,),
                ).fetchone()
                if row:
                    return row_to_dict(row)
        return None

    def count(self) -> int:
        with self._lock:
            try:
                return int(self.conn.execute("SELECT COUNT(*) FROM memes").fetchone()[0])
            except Exception:
                return 0

    def meta_get(self, key: str, default: str = "") -> str:
        with self._lock:
            try:
                row = self.conn.execute(
                    "SELECT value FROM meta WHERE key = ?", (key,)
                ).fetchone()
                return str(row[0]) if row and row[0] is not None else default
            except Exception:
                return default

    def titles(self):
        """全部 (title, slug)，供同步对比用。"""
        with self._lock:
            rows = self.conn.execute("SELECT title, slug FROM memes").fetchall()
            return [(r[0], r[1]) for r in rows]


def _load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else default
    except Exception:
        return default


def _save_json(path: str, data: dict):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


class MemeStore:
    """词库总入口：自建词条 -> 本地 SQLite -> 在线缓存 -> 在线兜底。

    自建词条和在线缓存都存在独立的 JSON 文件里，同步时不会被覆盖。
    """

    def __init__(
        self,
        data_dir: str,
        online_client=None,
        online_enabled: bool = False,
        online_throttle_seconds: int = 600,
        cache_max: int = 500,
    ):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.db_path = os.path.join(data_dir, "memes.db")
        self.db = MemeDB(self.db_path)
        self.custom_path = os.path.join(data_dir, "custom_terms.json")
        self.cache_path = os.path.join(data_dir, "online_cache.json")
        self.online = online_client
        self.online_enabled = online_enabled
        self.online_throttle_seconds = online_throttle_seconds
        self.cache_max = cache_max
        self._last_online_attempt = {}  # term -> time.monotonic()

    def close(self):
        self.db.close()

    # ---------- 自建词条 ----------

    def _load_custom(self) -> dict:
        return _load_json(self.custom_path, {})

    def add_custom(self, title: str, usage: str, origin: str = "", tags: str = "",
                   summary: str = "", aliases=None):
        title = (title or "").strip()
        if not title:
            raise ValueError("词名不能为空")
        data = self._load_custom()
        data[title] = {
            "title": title,
            "slug": title,
            "tags": (tags or "").strip(),
            "usage": (usage or "").strip(),
            "origin": (origin or "").strip(),
            "other": "",
            "summary": (summary or "").strip(),
            "aliases": ",".join(a.strip() for a in (aliases or []) if a.strip()),
        }
        _save_json(self.custom_path, data)

    def remove_custom(self, title: str) -> bool:
        title = (title or "").strip()
        data = self._load_custom()
        if title in data:
            del data[title]
            _save_json(self.custom_path, data)
            return True
        # 别名删除
        for key, entry in list(data.items()):
            aliases = [a.strip() for a in (entry.get("aliases") or "").split(",") if a.strip()]
            if title.lower() in [a.lower() for a in aliases]:
                del data[key]
                _save_json(self.custom_path, data)
                return True
        return False

    def custom_count(self) -> int:
        return len(self._load_custom())

    def _lookup_custom(self, q: str):
        data = self._load_custom()
        if q in data:
            return row_to_dict(data[q])
        lower_q = q.lower()
        for entry in data.values():
            aliases = [a.strip() for a in (entry.get("aliases") or "").split(",") if a.strip()]
            if lower_q in [a.lower() for a in aliases]:
                return row_to_dict(entry)
        return None

    # ---------- 在线缓存 ----------

    def _load_cache(self) -> dict:
        return _load_json(self.cache_path, {})

    def _save_cache(self, cache: dict):
        if len(cache) > self.cache_max:
            items = sorted(cache.items(), key=lambda kv: kv[1].get("fetched_at", ""))
            cache = dict(items[-self.cache_max:])
        _save_json(self.cache_path, cache)

    def _lookup_cache(self, q: str):
        cache = self._load_cache()
        hit = cache.get(q)
        if hit and isinstance(hit.get("entry"), dict):
            return row_to_dict(hit["entry"])
        return None

    def cache_count(self) -> int:
        return len(self._load_cache())

    # ---------- 统一查找 ----------

    def lookup_local(self, query: str):
        """本地查找：自建词条 -> SQLite -> 在线缓存。不联网。"""
        q = (query or "").strip()
        if not q:
            return None, None
        row = self._lookup_custom(q)
        if row:
            return row, "custom"
        row = self.db.lookup(q)
        if row:
            return row, "local"
        row = self._lookup_cache(q)
        if row:
            return row, "cache"
        return None, None

    def lookup_online(self, query: str):
        """在线兜底：只认与查询词完全同名的条目；同一词 10 分钟内只问一次。

        返回 (row, "online") 或 (None, None)。网络失败也返回 (None, None)。
        """
        q = (query or "").strip()
        if not q or self.online is None:
            return None, None
        now = time.monotonic()
        last = self._last_online_attempt.get(q, 0)
        if now - last < self.online_throttle_seconds:
            return None, None
        self._last_online_attempt[q] = now
        try:
            item = self.online.fetch_exact(q)
        except Exception:
            return None, None
        if not item:
            return None, None
        row = row_to_dict(item)
        cache = self._load_cache()
        cache[q] = {"entry": row, "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        try:
            self._save_cache(cache)
        except Exception:
            pass
        return row, "online"

    def lookup(self, query: str, allow_online: bool = True):
        """完整查找：本地 ->（可选）在线兜底。"""
        row, source = self.lookup_local(query)
        if row:
            return row, source
        if allow_online and self.online_enabled:
            return self.lookup_online(query)
        return None, None

    def status(self) -> dict:
        return {
            "db_path": self.db_path,
            "count": self.db.count(),
            "built_at": self.db.meta_get("built_at"),
            "source": self.db.meta_get("source"),
            "custom_count": self.custom_count(),
            "cache_count": self.cache_count(),
            "online_enabled": self.online_enabled,
        }
