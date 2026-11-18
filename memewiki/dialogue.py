"""在当前发言中寻找可以接的话。历史只用来理解本轮的语境。"""

from __future__ import annotations

import re
import unicodedata


# 典型改写、接话方向与词条一起进入索引。自建词条提供的用法优先。
_SEEDS = {
    "这瓜保熟吗": {
        "aliases": "你这瓜保熟吗,这瓜包熟吗,你这瓜包熟吗,你这瓜保不保熟",
        "variants": ["老板，这轴是生的我可不要", "你卖的这键盘包不包熟啊"],
        "usage": "借买瓜台词，开玩笑地追问对方推荐的东西靠不靠谱。可以顺着卖瓜人的口吻，把瓜换成刚才聊的东西。",
    },
    "我真的会谢": {
        "aliases": "我真的栓Q,我真会谢,我真的会栓Q",
        "variants": ["又给我改回第一版，可真是谢谢您了", "一天改八回，我谢谢你全家", "这份福气给我整得想说声谢谢"],
        "usage": "遇到反复折腾自己的事时用反话表达无语。可以顺着对方吐槽的具体事情接一句，先体谅她的处境。",
    },
    "电子榨菜": {
        "variants": ["饭都盛好了，就差给这碗饭配个下饭番", "今天的下饭视频断供了，这饭吃不下去了"],
        "usage": "把吃饭时配着看的剧或视频比作佐餐的小菜。可以顺着这顿饭聊一句今天配什么看。",
    },
    "遥遥领先": {
        "variants": ["领先同行足足半个按钮", "这个加载速度，领先了个寂寞", "别的不说，价格已经领先友商两条街"],
        "usage": "用发布会式的夸张口吻赞叹某项表现，也可能是在拿很小的优势或缺点反讽。顺着对方强调的那个指标接话。",
    },
    "听君一席话，如听一席话": {
        "aliases": "听君一席话如听一席话",
        "variants": ["听你分析了十分钟，我得到了整整十分钟的分析", "听完这番话，简直就像听了这么一番话", "听君一段分析，胜似听了一段分析"],
        "usage": "把前半句话原样绕回来，调侃一番话说了等于没说。可以承认绕了一圈，再给出一条有用的实话。",
    },
}
_GENERIC_TITLES = {"好家伙", "绝绝子", "绷不住了", "绷不住", "我直接好家伙", "笑死", "笑死我了", "救命", "哈哈哈哈"}
_FRUIT = re.compile(r"西瓜|哈密瓜|香瓜|甜瓜|果摊|水果|瓜摊|买瓜|挑瓜|切瓜|瓜皮|瓜瓤|几斤|一斤|果肉|熟没熟|甜不甜")
_MELON_LINE = re.compile(r"(?:保|包)(?:不[保包])?熟(?:吗|嘛|么|吧|啊|不|的)?")
_NEUTRAL = re.compile(
    r"(?:你好[呀啊]?|您好|[早晚]安[呀啊]?|[早晚]上好|中午好|下午好|在吗|谢谢(?:你[啦啊]?)?|"
    r"不客气|辛苦了|我也是|明天见|回头见|收到[了啦]?|没关系|没事|知道了|没有|好的?[呀啊嘛]?|"
    r"嗯+|哦+|哈+|行[吧啊]?|加油|有道理|没问题|真的假的|当然可以|我(?:先)?去(?:吃饭|睡觉|上班|洗澡)[了啦]?|"
    r"我[要在]吃饭[了啦]?|吃[过了]?饭了?[吗嘛]?|今天(?:的)?天气(?:怎么样|如何|不错)?|下班了|我回来了)"
)
_SHORT_SIGNALS = {"栓q", "yyds", "awsl", "xswl", "nsdd", "蚌埠住了", "绷不住", "绝绝子", "会谢", "爷青回"}
_PLAIN_SHORT = {"你说", "我们", "这个", "那个", "现在", "今天", "明天", "因为", "所以", "但是", "可以", "怎么", "真的", "回来了", "有点意思", "你好", "加油"}
_ASCII_WORD = re.compile(r"[a-z0-9_]", re.ASCII)
_SPLIT_ALIASES = re.compile(r"[,，、;；|\n]")
_STYLE = re.compile(r"(?:我真的|我直接|这谁|你这|好家伙|属于是|可还行|是吧|居然|竟然|好一个|给我整|整不会|救命|笑死|笑不活|蚌|绷不住|典中典|[哈笑]{2,}|[？?！!]{2,}|玩[个的]?梗|接[个的]?梗|[😂🤣🐶🙃])")


def _normal(value: str) -> str:
    return unicodedata.normalize("NFKC", value or "").casefold().strip()


def _bare(value: str) -> str:
    return re.sub(r"[\s，。？！,.?!~～、：:‘’“”\"']+", "", _normal(value))


def is_generic_match(row):
    """普通感叹可以让位给更贴切的词条；本群自建的专用含义例外。"""
    return bool(row and not (row.get("_custom") or row.get("source") == "custom")
                and _bare(row.get("title") or "") in _GENERIC_TITLES)


def _aliases(row):
    raw = row.get("aliases") or ""
    return [a.strip() for a in (_SPLIT_ALIASES.split(raw) if isinstance(raw, str) else raw) if isinstance(a, str) and a.strip()]


def augment_entries(entries):
    """补齐少量经典台词，同一名字的自建记录优先，重复调用结果相同。"""
    by_title = {}
    for item in entries:
        title = str(item.get("title") or "").strip()
        if title and (title not in by_title or item.get("_custom") or item.get("source") == "custom"):
            by_title[title] = dict(item)
    for title, seed in _SEEDS.items():
        row = by_title.setdefault(title, {"title": title, "usage": seed["usage"]})
        if row.get("_custom") or row.get("source") == "custom":
            continue
        row["aliases"] = ",".join(dict.fromkeys(_aliases(row) + _aliases(seed)))
        existing = row.get("variants") or []
        if isinstance(existing, str):
            existing = [existing]
        row["variants"] = list(dict.fromkeys([*existing, *seed["variants"]]))
        row["dialogue_hint"] = seed["usage"]
    return list(by_title.values())


def _literal_melon(text, history):
    if _FRUIT.search(text):
        return True
    # 当前句明确换成键盘、代码等东西时，优先理解新话题。
    figurative_object = re.search(r"(?:这|那)(?:个|把|台|段|条)?([^，。！？?！,\s]{1,10}?)(?:保|包)(?:不[保包])?熟", text)
    if figurative_object and figurative_object[1] not in {"瓜", "个瓜"}:
        return False
    return bool(_FRUIT.search(history[-180:]))


def _ordinary(text):
    return bool(_NEUTRAL.fullmatch(_bare(text)))


def semantic_allowed(text, history=""):
    """只有本句包含足够线索才发起向量查询，避免用问候搜索整部词典。"""
    current = _normal(text)
    bare = _bare(current)
    if not 3 <= len(bare) <= 400 or current.startswith("/") or _ordinary(current):
        return False
    if _MELON_LINE.search(current) and _literal_melon(current, history or ""):
        return False
    # 单独一句改写台词可能没有语气词，由向量分数及第一、二名差距继续把关。
    return len(bare) >= 6 or bool(_STYLE.search(current)) or bool(_MELON_LINE.search(current))


def accept_candidate(row, text, history=""):
    """共同校验文字与向量候选；绝不因为上轮用了梗就强行延用。"""
    title = _normal(row.get("title") or "")
    current = _normal(text)
    if not title or not current or current.startswith("/") or _ordinary(current):
        return False
    if title == "这瓜保熟吗":
        if _literal_melon(current, history or ""):
            return False
        return bool(_MELON_LINE.search(current) or re.search(r"(?:[这那]瓜|卖瓜|瓜摊|生的我可不要)", current))
    if title == "我真的会谢" and re.search(r"会谢(?:谢|幕|客|罪|绝|[你您他她大家])", current):
        return False
    # 语义距离也会反映相同话题。例如“我今晚吃饭看视频”容易接近“电子榨菜”，
    # 但它未必是在玩梗。没有台词痕迹时，至少要有明显的戏谑表达。
    forms = [_normal(form) for form in [title, *_aliases(row)]]
    if any(form and form in current for form in forms):
        return True
    if _STYLE.search(current) and not row.get("variants") and not is_generic_match(row):
        return True
    # 允许无语气词的半句和改词台词，但留出足够长的共同片段。
    compact = _bare(current)
    variants = row.get("variants") or []
    if isinstance(variants, str):
        variants = [variants]
    for form in [*forms, *variants]:
        phrase = _bare(form)
        if len(phrase) >= 5 and any(phrase[i:i + 4] in compact for i in range(len(phrase) - 3)):
            return True
    return False


class DialogueMatcher:
    def __init__(self, entries):
        self.entries = augment_entries(entries)
        self._phrases = {}
        for row in self.entries:
            for form in [row["title"], *_aliases(row)]:
                phrase = _normal(form)
                if len(phrase) < 2 or not any(char.isalnum() for char in phrase) or phrase in _PLAIN_SHORT or _ordinary(phrase):
                    continue
                bucket = self._phrases.setdefault(phrase[0], {})
                existing = bucket.get(phrase)
                # 共享别名时使用同名词条，两个词条仅靠同一别名时放弃匹配。
                if phrase not in bucket or phrase == _normal(row["title"]):
                    bucket[phrase] = row
                elif existing is not None and existing["title"] != row["title"] and phrase != _normal(existing["title"]):
                    bucket[phrase] = None

    def match(self, text, history=""):
        current = _normal(text)
        if not current or current.startswith("/") or _ordinary(current):
            return None
        # 分句保留上下文内的明确梗名；短词要求更强依据。
        bare = _bare(current)
        best = None
        longest = 0
        for i, char in enumerate(current[:2000]):
            for phrase, row in self._phrases.get(char, {}).items():
                if row is None or not current.startswith(phrase, i):
                    continue
                if not any(c.isalpha() for c in phrase) and phrase not in _SHORT_SIGNALS:
                    continue
                if len(phrase) <= 3 and phrase not in _SHORT_SIGNALS and not (row.get("_custom") or row.get("source") == "custom"):
                    if len(phrase) < 3 or bare != phrase:
                        continue
                end = i + len(phrase)
                if i and _ASCII_WORD.fullmatch(phrase[0]) and _ASCII_WORD.fullmatch(current[i - 1]):
                    continue
                if end < len(current) and _ASCII_WORD.fullmatch(phrase[-1]) and _ASCII_WORD.fullmatch(current[end]):
                    continue
                if best and (is_generic_match(row), -len(phrase)) >= (is_generic_match(best), -longest):
                    continue
                if accept_candidate(row, current, history):
                    best, longest = row, len(phrase)
        if (not best or is_generic_match(best)) and _MELON_LINE.search(current) and (re.search(r"[这那]", current) or _bare(current) in {"保熟吗", "包熟吗", "保不保熟"}):
            melon = self._phrases.get("这", {}).get("这瓜保熟吗")
            if melon and accept_candidate(melon, current, history):
                return melon
        return best


def render_hint(row):
    def clip(value, limit):
        return " ".join(str(value or "").split())[:limit]

    title = clip(row.get("title"), 48)
    usage = clip(row.get("dialogue_hint") or row.get("usage") or row.get("summary"), 220)
    return (f"本轮对话参考：对方可能在用「{title}」。用法资料：{usage}\n"
            "结合刚才的话题和你自己的口吻，合适时顺势接一句，也要回应对方实际在说的事。"
            "语境不合就忽略这条参考；只有对方主动问起才解释出处。")
