"""从 cnmeme.wiki 公开 API 同步词库。

dsh-memewiki scripts/sync-cnmeme.mjs 的 Python 移植（MIT）：
  扫全量清单 -> 对比本地 -> 只拉新增/更新的详情 -> 事务写入，
  写之前先备份 DB；--dry-run 只汇报不写；--no-existing 只新增不更新；
  站点已删的条目只汇报，本地保留。
"""

from __future__ import annotations

import html as _html
import os
import re
import shutil
import sqlite3
import time

from .db import SCHEMA
from .online import CnMemeClient

SECTION_RE = re.compile(r"<h2[^>]*>([\s\S]*?)</h2>([\s\S]*?)(?=<h2|$)", re.IGNORECASE)
BLOCK_RE = re.compile(r"<(p|ul|ol|blockquote|pre)[^>]*>([\s\S]*?)</\1>", re.IGNORECASE)
LI_RE = re.compile(r"<li[^>]*>([\s\S]*?)</li>", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_SPACE_BEFORE_NL = re.compile(r"[ \t]+\n")


def _inline(html_text) -> str:
    s = BR_RE.sub("\n", str(html_text or ""))
    s = TAG_RE.sub("", s)
    s = _html.unescape(s).replace("\u00a0", " ")
    s = _SPACE_BEFORE_NL.sub("\n", s)
    return s.strip()


def _blocks(html_text) -> str:
    out = []
    for m in BLOCK_RE.finditer(str(html_text or "")):
        tag = m.group(1).lower()
        body = m.group(2)
        if tag in ("ul", "ol"):
            items = []
            for li in LI_RE.finditer(body):
                t = re.sub(r"\s*\n\s*", " ", _inline(li.group(1))).strip()
                if t:
                    items.append(t)
            if items:
                out.append("\n\n".join("- " + t for t in items))
        elif tag == "blockquote":
            inner = _blocks(body) or _inline(body)
            if inner:
                out.append(inner)
        else:
            t = _inline(body)
            if t:
                out.append(t)
    return "\n\n".join(out)


def split_sections(content_html):
    """按 <h2> 把 content_html 拆成 [{title, content}]。"""
    sections = []
    for m in SECTION_RE.finditer(str(content_html or "")):
        title = _inline(m.group(1)).strip()
        content = _blocks(m.group(2)).strip()
        if title:
            sections.append({"title": title, "content": content})
    return sections


def _text(v) -> str:
    return "" if v is None else str(v)


def row_from_item(item: dict) -> dict:
    """把 API 的词条 item 转成 memes 表的一行。"""
    sections = split_sections(item.get("content_html") or "")

    def get(name: str) -> str:
        for s in sections:
            if s["title"] == name:
                return s["content"].strip()
        return ""

    other = "\n\n".join(
        f"{s['title']}\n{s['content']}" if s["content"] else s["title"]
        for s in sections
        if s["title"] not in ("来源", "用法")
    )
    tags = item.get("tags")
    aliases = item.get("aliases")
    return {
        "title": _text(item.get("title")).strip(),
        "slug": _text(item.get("slug")).strip() or _text(item.get("title")).strip(),
        "tags": "、".join(_text(t) for t in tags) if isinstance(tags, list) else _text(tags),
        "usage": get("用法"),
        "origin": get("来源"),
        "other": other,
        "summary": _text(item.get("summary")),
        "aliases": ",".join(_text(a) for a in aliases)
        if isinstance(aliases, list)
        else _text(aliases),
        "updated_at": _text(item.get("updated_at")),
    }


def _parse_time(value: str) -> float:
    """把 "2026-11-05 12:48:06" / "2026-11-05T12:48:06" 转成本地时间戳。"""
    if not value:
        return 0
    try:
        return time.mktime(time.strptime(str(value)[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        pass
    try:
        return time.mktime(time.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return 0


def run_sync(db_path: str, client=None, dry_run: bool = False,
             no_existing: bool = False, progress=None) -> dict:
    """同步一次词库，返回统计字典。

    progress: 可选回调 progress(str)，用于打日志。
    """
    def log(msg):
        if progress:
            progress(msg)

    client = client or CnMemeClient()
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 8000")
    conn.executescript(SCHEMA)

    local = {r[0]: r[1] for r in conn.execute("SELECT title, slug FROM memes")}
    local_titles = set(local.keys())
    local_slugs = {s for _, s in local.items()}
    built_at = ""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'built_at'").fetchone()
        built_at = row[0] if row else ""
    except sqlite3.Error:
        pass
    cutoff = _parse_time(built_at)

    scan_started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    # 1. 扫全量清单
    site = []
    offset = 0
    while True:
        data = client.list_memes(limit=30, offset=offset)
        items = data.get("items") or []
        if not items:
            break
        for it in items:
            site.append({
                "slug": _text(it.get("slug")),
                "title": _text(it.get("title")),
                "updated_at": _text(it.get("updated_at")),
            })
        offset += len(items)
        if not data.get("has_more"):
            break
        time.sleep(0.12)
    log(f"远端清单：{len(site)} 条")

    # 2. 对比
    site_slugs = {x["slug"] for x in site}
    new, to_update = [], []
    for it in site:
        exists = it["title"] in local_titles or it["slug"] in local_slugs
        if not exists:
            new.append(it)
        elif not no_existing and cutoff and it.get("updated_at"):
            if _parse_time(it["updated_at"]) > cutoff:
                to_update.append(it)
    removed = [t for t, s in local.items() if s not in site_slugs]
    log(f"新增 {len(new)} 条，更新 {len(to_update)} 条，站点已删 {len(removed)} 条（本地保留）")

    result = {
        "site_total": len(site),
        "new": len(new),
        "updated": len(to_update),
        "removed_reported": len(removed),
        "removed": removed,
        "failed": 0,
        "dry_run": dry_run,
        "up_to_date": not new and not to_update,
    }
    if not new and not to_update:
        conn.close()
        return result

    # 3. 拉详情
    details = []
    failed = 0
    for it in new + to_update:
        try:
            item = client.get_meme(it["slug"])
            if item:
                details.append(item)
            else:
                failed += 1
        except Exception:
            failed += 1
        time.sleep(0.25)
    result["failed"] = failed

    if dry_run:
        result["would_add"] = [x["title"] for x in new]
        result["would_update"] = [x["title"] for x in to_update]
        conn.close()
        return result

    # 4. 备份后事务写入
    stamp = time.strftime("%Y%m%d%H%M%S", time.localtime())
    backup = db_path + f".bak-{stamp}"
    shutil.copyfile(db_path, backup)
    result["backup"] = backup

    inserted = updated = 0
    conn.execute("BEGIN")
    try:
        for item in details:
            row = row_from_item(item)
            if not row["title"]:
                continue
            old = conn.execute(
                "SELECT * FROM memes WHERE title = ?", (row["title"],)
            ).fetchone()
            if old is None:
                conn.execute(
                    "INSERT OR REPLACE INTO memes "
                    "(title, slug, tags, usage, origin, other, summary, aliases) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (row["title"], row["slug"], row["tags"], row["usage"],
                     row["origin"], row["other"], row["summary"], row["aliases"]),
                )
                inserted += 1
            else:
                merged = {}
                for k in ("slug", "tags", "usage", "origin", "other", "summary", "aliases"):
                    merged[k] = row[k] or (old[k] or "")
                if any(str(old[k] or "") != str(merged[k]) for k in merged):
                    conn.execute(
                        "UPDATE memes SET slug=?, tags=?, usage=?, origin=?, "
                        "other=?, summary=?, aliases=? WHERE title=?",
                        (merged["slug"], merged["tags"], merged["usage"],
                         merged["origin"], merged["other"], merged["summary"],
                         merged["aliases"], row["title"]),
                    )
                    updated += 1
        total = conn.execute("SELECT COUNT(*) FROM memes").fetchone()[0]
        # 保留失败的已有词条在下轮的更新时间范围里。
        if not failed and not no_existing:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('built_at', ?)", (scan_started,))
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('count', ?)", (str(total),))
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('source', ?)",
            ("cnmeme.wiki /api/v1/public/memes",),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        conn.close()
        raise

    result.update({"inserted": inserted, "updated_count": updated, "total": total})
    conn.close()
    return result
