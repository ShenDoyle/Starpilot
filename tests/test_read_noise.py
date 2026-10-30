"""读正文的噪声剔除：思考过程不得混进回复正文。

背景（2026-10-30 实测）：Kimi 末条回复 `wa.py read` 返回 7103 字，其中 5841 字是
它的英文推理（"The user wants a gap analysis..."），真正答复只有 1241 字，从
「1. 需求解读」才开始。两个后果：
  ① agent 会把推理当指令执行；
  ② 推理是**逐字流式增长**的，算进消息指纹后，同一条回复的指纹一直在变，
     「末条是否变化」判据失效，锚点计数跟着崩（§2.4 的信任根）。

修法：跨平台黑名单 READ_IGNORE_COMMON + 平台显式 sel.read_ignore，
统一由 read_ignore_of() 合并；js_read_last / js_probe 全部走净化文本。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import platforms  # noqa: E402


class TestReadIgnoreSet(unittest.TestCase):
    def test_common_list_covers_known_thinking_containers(self):
        joined = " ".join(platforms.READ_IGNORE_COMMON)
        for sel in (".toolcall-flow", "[data-testid*=\"thought\"]",
                    "[class*=\"think-block\"]", "[class*=\"reasoning\"]"):
            self.assertIn(sel, joined, "通用黑名单漏了推理容器 %s" % sel)

    def test_common_list_has_no_over_greedy_prefix(self):
        # [class*="think"] 会误伤正常正文类名（如 thoughtful-note），只许精确到 think-block
        for sel in platforms.READ_IGNORE_COMMON:
            self.assertNotEqual(sel, "[class*=\"think\"]")
            self.assertNotEqual(sel, "[class*=\"toolcall\"]")

    def test_kimi_declares_toolcall_flow(self):
        p = platforms.get("kimi")
        self.assertIn(".toolcall-flow", platforms.read_ignore_of(p))

    def test_merge_dedups_platform_and_common(self):
        p = platforms.get("kimi")
        merged = platforms.read_ignore_of(p)
        self.assertEqual(len(merged), len(set(merged)), "合并且去重，不许出现重复选择器")
        # 平台声明必须完整出现在合并结果里（不能只有通用那几条）
        for sel in p["sel"].get("read_ignore") or []:
            self.assertIn(sel, merged)

    def test_platform_without_own_list_still_gets_common(self):
        p = platforms.get("chatgpt")
        self.assertTrue(platforms.read_ignore_of(p))


class TestReadJsWiring(unittest.TestCase):
    """JS 片段必须真的带上 IGNORE，而不是只在 Python 侧算好了没人用。"""

    def test_read_last_injects_ignore(self):
        js = platforms.js_read_last(platforms.get("kimi"))
        self.assertIn("toolcall-flow", js)
        self.assertNotIn("%s", js, "格式化占位符没吃完")
        self.assertIn("cloneNode", js, "必须在 clone 上剔除，不能动页面原文")

    def test_probe_strips_before_fingerprint(self):
        js = platforms.js_probe(platforms.get("kimi"))
        self.assertIn("const IGNORE =", js)
        self.assertNotIn("__IGNORE__", js, "哨兵没被替换掉")
        # 指纹必须走净化文本：fpT 里带 textOf
        self.assertIn("fpOf(el, textOf(el))", js)
        # 末条正文兜底也必须净化
        self.assertIn("lastText = textOf(asst[asst.length - 1])", js)

    def test_fallback_path_also_strips(self):
        js = platforms.js_read_last(platforms.get("kimi"))
        self.assertIn("mainClean", js, "messages 选择器失效时，兜底分支也要剔思考块")


if __name__ == "__main__":
    unittest.main()
