"""从用户消息里提取“要查的梗”。

移植自 dsh-memewiki lib/index.js 的 提取查询 / 清文本逻辑
（MIT）。已去掉 DSH 专属的唤醒词剥离（“小鲸鱼”等），只保留
@提及剥离；闲聊短语表里也去掉了 DSH 实例专属的几条。

两条路：
  问梗模式：“牛马是什么梗” -> “牛马”；
  裸词模式：整条消息本身就是一个 2~max_chars 字的词 -> 直接当词。
"""

from __future__ import annotations

import re

# 问梗模式：……是什么梗 / 啥梗 / 什么来头 / 什么意思 / 怎么用 ……
ASK_RE = re.compile(
    r"(?:是)?(?:什么梗|啥梗|什么来头|什么来历|什么出处|什么意思|啥意思|怎么用|怎么接|怎么玩|如何用)"
    r"(?:啊|呀|呢|的|啊这是)?[？?。！!~～]*$"
)

# 裸词模式：2~max_chars 个中/英/数字/·
BARE_RE_TEMPLATE = r"^[\u4e00-\u9fa5A-Za-z0-9·]{2,%d}$"

# 无语义词：指代不明，没法查
NO_MEANING = {
    "这", "那", "它", "他", "她", "这个", "那个", "你",
    "这句话", "那句话", "上面", "刚才", "下文", "前面",
}

# 停用词：日常打招呼/语气词，别当梗查
STOPWORDS = {
    "在吗", "在不在", "哈哈", "哈哈哈", "笑死", "好的", "好吧",
    "晚安", "早安", "睡觉", "吃饭", "干嘛", "没事", "收到",
    "谢谢", "好耶", "草", "典", "乐",
}

# 闲聊短语：看起来像词，其实是聊天，别当梗查
IDLE_PHRASES = {
    "我是谁", "错了", "情绪", "接下一句", "你也学一个",
    "今天看什么", "到底多少了", "搜出来了吗", "来点贴吧评价",
    "都是同龄人", "你同事", "道德这么高", "这是谁的手臂",
}

# 句子成分：带这些词的基本是问句/句子，不是梗名
SENTENCE_PARTS_RE = re.compile(
    r"怎么样|怎么|多少|为什么|能不能|是不是|什么|了吗|了没|一下|你说|要不要|有没有"
)

_MENTION_RE = re.compile(r"^(?:@\S+\s*)+")
_PUNCT_EDGE = re.compile(r"^[ ，,。.！!？?]+|[ ，,。.！!？?]+$")


def clean_text(text: str) -> str:
    """剥离开头的 @提及，返回干净的文本。"""
    t = (text or "").strip()
    t = _MENTION_RE.sub("", t).strip()
    return t


def extract_query(text: str, max_chars: int = 10):
    """从消息里提取候选梗词；提不出就返回 None。"""
    t = clean_text(text)
    if not t:
        return None

    # 问梗模式
    m = ASK_RE.search(t)
    if m:
        head = t[: m.start()]
        head = _PUNCT_EDGE.sub("", head)
        head = re.sub(r"^[是]+|[是]+$", "", head).strip()
        head = re.sub(r"(这个梗|那个梗|这梗|那梗|这个|那个)$", "", head).strip()
        head = re.sub(r"(到底|究竟)$", "", head).strip()
        if not head:
            return None
        if head in NO_MEANING:
            return None
        if len(head) <= 2 and head[0] in "这那它他她":
            return None
        if 1 < len(head) <= max_chars:
            return head
        return None

    # 裸词模式
    bare = re.compile(BARE_RE_TEMPLATE % max_chars)
    if bare.match(t):
        if t in STOPWORDS or t in IDLE_PHRASES:
            return None
        if SENTENCE_PARTS_RE.search(t):
            return None
        return t
    return None
