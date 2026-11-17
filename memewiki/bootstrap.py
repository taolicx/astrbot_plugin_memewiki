"""首次启动：从 dsh-memewiki 的公开 raw 地址下载词库快照。

仓库里不带二进制 memes.db（GitHub 推送只接受 UTF-8 文本），
插件第一次启动时从这里下载；下载失败也不影响加载，
插件会以降级模式运行（本地查不到 + 在线兜底）。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import time
import urllib.error
import urllib.request

SNAPSHOT_URL = "https://raw.githubusercontent.com/thawline/dsh-memewiki/main/data/memes.db"
SNAPSHOT_SOURCE = "thawline/dsh-memewiki@main"
USER_AGENT = "astrbot-memewiki/0.1.0"


def validate_db(path: str):
    """校验一个 db 文件：能打开、有 memes 表、至少 1 条。返回 (ok, 条数)。"""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            n = conn.execute("SELECT COUNT(*) FROM memes").fetchone()[0]
        finally:
            conn.close()
        return (True, int(n)) if n > 0 else (False, 0)
    except Exception:
        return (False, 0)


def download_snapshot(dest_path: str, timeout: int = 180, retries: int = 3,
                      user_agent: str = USER_AGENT):
    """下载快照到 dest_path。返回 (True, "6792 条") 或 (False, "原因")。"""
    tmp = dest_path + ".tmp"
    last_exc = None
    for i in range(retries):
        try:
            req = urllib.request.Request(SNAPSHOT_URL, headers={"user-agent": user_agent})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    raise OSError(f"HTTP {resp.status}")
                with open(tmp, "wb") as f:
                    shutil.copyfileobj(resp, f, 1024 * 256)
            ok, n = validate_db(tmp)
            if not ok:
                raise OSError("下载的文件不是有效的词库（memes 表为空或损坏）")
            os.replace(tmp, dest_path)
            return (True, f"{n} 条")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_exc = e
            try:
                os.remove(tmp)
            except OSError:
                pass
            if i < retries - 1:
                time.sleep(2 * (i + 1))
    return (False, str(last_exc) if last_exc else "未知错误")
