# -*- coding: utf-8 -*-
"""写入接受判定 / 流式补全判定 / 写权限同源 —— 2026-10-29 Gemini 实测驱动。

三条实测事实各自对应一组断言：

1. **`innerText` 会骗人**：Quill 类受控编辑器能让你「看着有字、model 是空」
   （`editor_len=15` 而 `ql-blank` 仍在）。所以判「写进去了没有」必须听空态类，
   不能听文本长度。
2. **发送键是条件渲染的**：没内容时按钮根本不存在。写完却没出现发送键 = 平台没接受，
   必须失败，不能去点空气。
3. **同容器流式补全**：回复写进**已有**容器时，锚点集合差恒为 0，只有正文长度能作证。

这些断言全部是纯函数级，不连浏览器——判定口径只有能离线验，才不会悄悄退化。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import confirm  # noqa: E402
import orchestrator as orch  # noqa: E402
import platforms  # noqa: E402
import wa  # noqa: E402


class TestNormalizeProbe(unittest.TestCase):
    """JS 报事实、Python 定口径：口径必须可离线验。"""

    def setUp(self):
        self.gemini = platforms.get("gemini")
        self.kimi = platforms.get("kimi")

    def test_blank_class_wins_over_innertext(self):
        """Quill 陷阱的原样回放：innerText 有 15 字，空态类还在。"""
        raw = {"editor": True, "editor_blank": True, "editor_len": 15,
               "can_send": False}
        d = platforms.normalize_probe(self.gemini, raw)
        self.assertTrue(d["editor_blank"])
        self.assertEqual(d["editor_len"], 0,
                         "空态类在 = model 为空：字数必须归零，否则会把「没写进去」判成成功")

    def test_blank_false_keeps_len(self):
        raw = {"editor_blank": False, "editor_len": 1519, "can_send": True}
        d = platforms.normalize_probe(self.gemini, raw)
        self.assertEqual(d["editor_len"], 1519)

    def test_bad_len_becomes_minus_one_not_zero(self):
        """探测失败（拿不到值）必须与「确实空」区分开：-1 ≠ 0。"""
        d = platforms.normalize_probe(self.gemini, {"editor_len": None})
        self.assertEqual(d["editor_len"], -1)

    def test_conditional_send_button_never_reads_as_ready(self):
        d = platforms.normalize_probe(self.gemini, {"can_send": None})
        self.assertFalse(d["can_send"], "发送键不存在时不许被算成「可发送」")

    def test_platform_without_blank_class_is_untouched(self):
        d = platforms.normalize_probe(self.kimi, {"editor_blank": False,
                                                 "editor_len": 42, "can_send": False})
        self.assertEqual(d["editor_len"], 42, "没声明空态类的平台不该被凭空改口径")


class TestFillCheck(unittest.TestCase):
    """三条见证：字数 / 空态消失 / 条件渲染的发送键。"""

    def setUp(self):
        self.gemini = platforms.get("gemini")
        self.kimi = platforms.get("kimi")

    def test_clean_fill_passes(self):
        cur = {"editor_len": 1519, "editor_blank": False, "can_send": True}
        r = platforms.fill_check(self.gemini, "x" * 1519, cur)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["code"], platforms.FILL_OK)

    def test_blank_beats_length(self):
        """字数够、但有空态类 → 仍然失败。这是「静默失败」唯一能被抓住的地方。"""
        cur = {"editor_len": 1519, "editor_blank": True, "can_send": True}
        r = platforms.fill_check(self.gemini, "x" * 1519, cur)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], platforms.FILL_BLANK)

    def test_short_write_fails(self):
        cur = {"editor_len": 10, "editor_blank": False, "can_send": True}
        r = platforms.fill_check(self.gemini, "x" * 1519, cur)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], platforms.FILL_SHORT)

    def test_missing_send_button_fails_when_conditional(self):
        """有内容但发送键没出现：平台没接受这份输入 —— 不许点空气。"""
        cur = {"editor_len": 1519, "editor_blank": False, "can_send": False}
        r = platforms.fill_check(self.gemini, "x" * 1519, cur)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], platforms.FILL_NO_SEND)

    def test_send_button_not_required_for_other_platforms(self):
        """没声明 send_requires_text 的平台不该被这条拦住（不许凭空加限制）。"""
        cur = {"editor_len": 100, "editor_blank": False, "can_send": False}
        r = platforms.fill_check(self.kimi, "x" * 100, cur)
        self.assertTrue(r["ok"], "Kimi 的发送键不是条件渲染，不能按 Gemini 的规矩卡它")


class TestReplyGrowth(unittest.TestCase):
    """同容器流式补全：锚点看不出新消息，但正文在变长。"""

    def test_growth_needs_both_abs_and_ratio(self):
        self.assertTrue(confirm.reply_grew({"last_len": 100}, {"last_len": 300})["grew"])
        self.assertFalse(confirm.reply_grew({"last_len": 5000}, {"last_len": 5010})["grew"],
                         "10 字的抖动不是回复")
        self.assertFalse(confirm.reply_grew({"last_len": 5000}, {"last_len": 5040})["grew"],
                         "长文本里 40 字可能只是重排：按比例再兜一层")

    def test_no_growth_when_unchanged_or_shrunk(self):
        self.assertFalse(confirm.reply_grew({"last_len": 800}, {"last_len": 800})["grew"])
        self.assertFalse(confirm.reply_grew({"last_len": 800}, {"last_len": 300})["grew"])

    def test_streaming_growth_is_accepted_as_witness(self):
        """Gemini 第 5 条裁定：不加这条判据必漏消息。"""
        seq = [{"reply_count": 3, "generating": False, "last_len": 900,
                "asst_fps": ["a"], "last_asst_fp": "a"}] * 3
        before = {"reply_count": 3, "last_len": 300,
                  "asst_fps": ["a"], "last_asst_fp": "a"}
        orig = orch.wa.read_state
        orch.wa.read_state = lambda _p, _s: (True, seq[0])
        try:
            res = orch.wait_for_reply({}, "sid", 3, timeout=10, interval=0, before=before)
        finally:
            orch.wa.read_state = orig
        self.assertTrue(res["ok"], "锚点没变但正文从 300 涨到 900：必须判回复到了")
        self.assertEqual(res["witness"], "growth")

    def test_generating_state_blocks_growth_witness(self):
        """生成中不许判完成，否则会把半截话当成品。"""
        cur = {"reply_count": 3, "generating": True, "last_len": 900,
               "asst_fps": ["a"], "last_asst_fp": "a"}
        before = {"reply_count": 3, "last_len": 300,
                  "asst_fps": ["a"], "last_asst_fp": "a"}
        orig = orch.wa.read_state
        orch.wa.read_state = lambda _p, _s: (True, cur)
        try:
            res = orch.wait_for_reply({}, "sid", 3, timeout=1, interval=0, before=before)
        finally:
            orch.wa.read_state = orig
        self.assertFalse(res["ok"], "还在生成就不许判完成")


class TestGeminiContract(unittest.TestCase):
    """Gemini 的平台事实必须落到配置里，而不是散在代码注释里。"""

    def setUp(self):
        self.p = platforms.get("gemini")

    def test_verified_and_write_path_whitelisted(self):
        self.assertTrue(self.p["verified"])
        self.assertEqual(self.p["write_method"], "native")
        self.assertIn("native", self.p["write_allowed"])

    def test_empty_state_and_conditional_send_declared(self):
        self.assertEqual(self.p["blank_class"], "ql-blank")
        self.assertTrue(self.p["send_requires_text"])

    def test_anchor_box_declared(self):
        self.assertEqual(self.p.get("anchor_box"), "div.conversation-container",
                         "轮次容器是平台事实，必须显式声明而不是靠十六进制去猜")

    def test_url_pattern_rejects_bare_app(self):
        self.assertIsNotNone(__import__("re").search(self.p["url_pattern"],
                                                    "https://gemini.google.com/app/a8b8143806a8b296"))
        self.assertIsNone(__import__("re").search(self.p["url_pattern"],
                                                  "https://gemini.google.com/app"),
                          "新对话页（无 id）不能当「已有对话」绑")

    def test_js_probe_injects_anchor_box(self):
        js = platforms.js_probe(self.p)
        self.assertIn('const ANCHOR_BOX = "div.conversation-container";', js)
        self.assertNotIn("__ANCHOR_BOX__", js)


class TestWriteWhitelistIsShared(unittest.TestCase):
    """写权限必须同源：CLI 与闸门不能一个放行一个拦。"""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="sp-wh-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.state = os.path.join(self.d, ".wa-state.json")
        with open(self.state, "w", encoding="utf-8") as f:
            json.dump({"url": "https://example.com/x", "platform": "claude"}, f)

    def test_cli_refuses_unverified_write_method(self):
        """claude 的 write_allowed 是空的 → CLI 也不许写。

        这条以前只在闸门里有，于是 `wa.py send` 能绕过去写。
        """
        class A(object):
            state = self.state
            platform = "claude"
            text = "hello"
            file = ""
            force = True
        rc = wa.cmd_send(A())
        self.assertEqual(rc, 8, "写路径不在白名单时必须拒绝（退出码 8），且不碰浏览器")


if __name__ == "__main__":
    unittest.main()
