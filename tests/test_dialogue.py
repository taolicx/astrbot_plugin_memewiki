import unittest

from memewiki.dialogue import DialogueMatcher, accept_candidate, augment_entries, is_generic_match, render_hint, semantic_allowed


class DialogueTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {"title": "电子榨菜", "usage": "吃饭时看的视频"},
            {"title": "栓Q", "usage": "用谐音表达感谢或无语"},
            {"title": "早上好中国现在我有冰淇淋"}, {"title": "牛马家庭"},
            {"title": "好汉饶命", "aliases": "少侠饶命"}, {"title": "米线"},
            {"title": "主任", "_custom": True, "usage": "小群内对猫的昵称"},
            {"title": "nice"},
        ]
        self.matcher = DialogueMatcher(self.rows)

    def test_full_sentence_and_aliases(self):
        for line, title in [("需求又变了，我真的会谢……", "我真的会谢"),
                            ("这个报错怎么又来了，栓Ｑ", "栓Q"),
                            ("我还没写完呢，少侠饶命！", "好汉饶命"),
                            ("给主任加个罐头吧", "主任")]:
            with self.subTest(line=line):
                self.assertEqual(self.matcher.match(line, "")["title"], title)

    def test_topic_controls_melon(self):
        self.assertEqual(self.matcher.match("你这瓜包熟吗？", "这个键盘轴体不错")["title"], "这瓜保熟吗")
        self.assertEqual(self.matcher.match("你这键盘保不保熟？", "能推荐个键盘吗")["title"], "这瓜保熟吗")
        self.assertIsNone(self.matcher.match("你这瓜保熟吗？", "我正在超市挑西瓜"))
        self.assertFalse(semantic_allowed("你这瓜保熟吗？", "西瓜一斤多少钱"))
        self.assertFalse(accept_candidate({"title": "这瓜保熟吗"}, "在看这台键盘", "昨天在挑瓜"))

    def test_smalltalk_and_short_word_ambiguity(self):
        for line in ("早上好", "你好！", "牛马", "晚上吃米线", "我真的会谢幕", "我是unicef的成员", "去吃饭了"):
            with self.subTest(line=line):
                self.assertIsNone(self.matcher.match(line, "在聊键盘"))
        self.assertFalse(semantic_allowed("我去吃饭了", ""))
        self.assertFalse(accept_candidate(self.rows[0], "吃饭时我看了一段视频", ""))
        self.assertTrue(accept_candidate({"title": "这个需求我接不住啊"}, "需求我接不住了", ""))
        self.assertIsNone(self.matcher.match("收到啦", "你这瓜保熟吗"))

    def test_semantic_examples_and_generic_exclamations(self):
        rows = {row["title"]: row for row in self.matcher.entries}
        for title in ("电子榨菜", "遥遥领先", "我真的会谢", "听君一席话，如听一席话"):
            variant = rows[title]["variants"][0]
            self.assertIsNone(self.matcher.match(variant))
            self.assertTrue(accept_candidate(rows[title], variant))
        self.assertTrue(is_generic_match({"title": "绝绝子"}))
        self.assertFalse(is_generic_match({"title": "绝绝子", "source": "custom"}))
        matcher = DialogueMatcher([{"title": "绝绝子"}, {"title": "栓Q"}])
        self.assertEqual(matcher.match("绝绝子，我先说一句栓Q")["title"], "栓Q")
        self.assertEqual(matcher.match("绝绝子，你这键盘保熟的吗")["title"], "这瓜保熟吗")

    def test_shared_alias_does_not_overwrite_exact_title(self):
        rows = [{"title": "少侠饶命"}, {"title": "另一句古风词", "aliases": "少侠饶命"}]
        for ordered in [rows, rows[::-1]]:
            self.assertEqual(DialogueMatcher(ordered).match("少侠饶命")['title'], "少侠饶命")
        self.assertIsNone(DialogueMatcher([{"title": "一句古风词", "aliases": "少侠饶命"}, rows[1]]).match("少侠饶命"))

    def test_augmentation_preserves_existing_customization(self):
        rows = [{"title": "我真的会谢", "usage": "自建测试用法", "_custom": True}]
        entries = augment_entries(rows)
        self.assertEqual(augment_entries(entries), entries)
        row = next(row for row in entries if row["title"] == "我真的会谢")
        self.assertIn("自建测试用法", render_hint(row))
        self.assertNotIn("aliases", rows[0])


if __name__ == "__main__":
    unittest.main()
