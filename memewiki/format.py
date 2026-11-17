"""词条格式化 + 用法指引。

词条文本格式移植自 dsh-memewiki lib/index.js（MIT）；
用法指引是原 astrbot_plugin_memewiki 的 4 条核心规则
（反馈工具那条不在本插件范围内，已略去）。
"""

from __future__ import annotations

# 注入上下文时附带的用法指引（约 150 字）。
# 放在词条前面，告诉模型：查到就先接梗，别科普；查不到就说不知道。
USAGE_GUIDE = """[梗百科·用法]
1. 查到词条就先接梗、别科普：照「怎么用/怎么接」回一句，像本来就会；
2. 只有对方明确问「这是什么梗」才解释出处，一两句就够，不要念词条；
3. 查不到就说不知道，别硬编，也别把两个不相干的梗拼一起；
4. 说话用短句，别用客服腔。"""

NOT_FOUND_TEMPLATE = (
    "词库里没收录「{q}」。别硬解释——说不知道，"
    "或者反问一句「这什么梗」。"
)


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def format_entry(row: dict) -> str:
    """把一条词条格式化成注入上下文 / 回复用的文本。"""
    title = (row.get("title") or "").strip()
    tags = _clip(row.get("tags") or "", 120)
    usage = _clip(row.get("usage") or "", 700)
    summary = (row.get("summary") or "").strip()
    origin = (row.get("origin") or "").strip()
    meaning = _clip(summary or origin, 500)

    lines = [f"[梗百科]「{title}」"]
    if tags:
        lines.append(f"标签：{tags}")
    if usage:
        lines.append("怎么用 / 怎么接：")
        lines.append(usage)
    if meaning:
        lines.append("出处与含义：")
        lines.append(meaning)
    return "\n".join(lines)
