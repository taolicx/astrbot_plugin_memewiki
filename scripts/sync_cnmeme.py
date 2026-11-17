#!/usr/bin/env python3
"""命令行同步梗词库（与插件内同步同一套逻辑）。

用法：
    python scripts/sync_cnmeme.py [--db 词库路径] [--dry-run] [--no-existing]

默认词库路径：插件数据目录下的 memes.db；如果找不到，就用仓库内 data/memes.db。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memewiki.sync import run_sync


def _default_db_path() -> str:
    candidates = [
        os.path.expanduser("~/.astrbot/data/plugin_data/astrbot_plugin_memewiki/memes.db"),
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data",
            "memes.db",
        ),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return candidates[1]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="从 cnmeme.wiki 同步梗词库")
    ap.add_argument("--db", default=None, help="词库路径")
    ap.add_argument("--dry-run", action="store_true", help="只汇报，不写库")
    ap.add_argument("--no-existing", action="store_true", help="只新增，不更新已有条目")
    args = ap.parse_args(argv)

    db_path = args.db or _default_db_path()
    result = run_sync(
        db_path,
        dry_run=args.dry_run,
        no_existing=args.no_existing,
        progress=print,
    )
    print(
        f"远端 {result['site_total']} 条，新增 {result['new']} 条，"
        f"更新 {result['updated']} 条，站点已删 {result['removed_reported']} 条（本地保留），"
        f"详情拉取失败 {result['failed']} 条"
    )
    if result.get("dry_run"):
        print("dry-run：未写库。")
        if result.get("would_add"):
            print("将新增：", "、".join(result["would_add"][:20]))
        if result.get("would_update"):
            print("将更新：", "、".join(result["would_update"][:20]))
    else:
        print(
            f"已写入：新增 {result.get('inserted', 0)} 条，"
            f"更新 {result.get('updated_count', 0)} 条，"
            f"总数 {result.get('total', '?')} 条，备份：{result.get('backup', '无')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
