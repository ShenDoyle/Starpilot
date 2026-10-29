# -*- coding: utf-8 -*-
"""绑定归属门禁 —— 2026-10-29 事故驱动（项目挂到了用户正在聊的 Gemini 对话上）。

事故复盘：`wa.py bind <url>` 只检查「地址形态像不像一个对话」，
对「这条对话里已经有 10 条消息」只打一行警告就放行。后果有三层：

  1. **上下文不是本项目的**：AI 看到的是上一件事的历史，回复自然跑偏；
  2. **会污染用户既有对话**：项目的提问被插进用户自己正在用的对话里；
  3. **锚点基线错位**：msg_count/锚点集合全从别人的 10 条起算，增量判定失真。

所以门禁的核心不是「地址对不对」，而是「**这条对话归谁**」：
`conv_id`（平台 `url_pattern` 抽出的会话身份）+ `msg_count`（它是不是空的）
+ state 里记的 `conv_id`（我上次绑的是不是同一条）。

全部纯函数级断言，不连浏览器。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import platforms  # noqa: E402


GEMINI_CONV = "https://gemini.google.com/app/a8b8143806a8b296"
GEMINI_NEW = "https://gemini.google.com/app"
GEMINI_OTHER = "https://gemini.google.com/app/131cec389a0800e1"


def probe(url, msgs):
    return {"url": url, "msg_count": msgs, "editor": True}


class TestConvId(unittest.TestCase):
    """会话身份必须由平台的 url_pattern 决定，不能各处自己写正则。"""

    def setUp(self):
        self.g = platforms.get("gemini")

    def test_conv_id_from_real_url(self):
        self.assertEqual(platforms.conv_id_of(self.g, GEMINI_CONV), "/app/a8b8143806a8b296")

    def test_new_chat_page_has_no_identity(self):
        self.assertEqual(platforms.conv_id_of(self.g, GEMINI_NEW), "",
                         "新对话页 /app 没有会话 id：这正是「它还不是一条对话」的表达")

    def test_home_has_no_identity(self):
        self.assertEqual(platforms.conv_id_of(self.g, "https://gemini.google.com/"), "")

    def test_query_string_does_not_break_identity(self):
        u = GEMINI_CONV + "?hl=zh-CN"
        self.assertEqual(platforms.conv_id_of(self.g, u), "/app/a8b8143806a8b296")


class TestForeignConv(unittest.TestCase):
    """本次事故的原样回放：首次绑定到一条有 10 条消息的对话。"""

    def setUp(self):
        self.g = platforms.get("gemini")

    def test_first_bind_to_nonempty_conv_is_rejected(self):
        st = {"url": "", "conv_id": ""}          # 全新 state（项目刚开始）
        v = platforms.bind_verdict(self.g, GEMINI_CONV, probe(GEMINI_CONV, 10), st)
        self.assertFalse(v["ok"], "首次绑到非空对话必须拒绝——这就是把项目挂到别人对话上的入口")
        self.assertEqual(v["code"], "foreign_conv")
        self.assertEqual(v["msg_count"], 10)
        self.assertIn("--new", v["hint"])

    def test_explicit_adoption_is_allowed_with_warning(self):
        st = {"url": "", "conv_id": ""}
        v = platforms.bind_verdict(self.g, GEMINI_CONV, probe(GEMINI_CONV, 10), st,
                                   reuse_existing=True)
        self.assertTrue(v["ok"])
        self.assertEqual(v["code"], "adopted")
        self.assertTrue(v["warn"], "接管已有对话必须留下警告：上下文不连续是事实")

    def test_empty_conv_is_allowed_but_warned(self):
        st = {"url": "", "conv_id": ""}
        v = platforms.bind_verdict(self.g, GEMINI_CONV, probe(GEMINI_CONV, 0), st)
        self.assertTrue(v["ok"])
        self.assertEqual(v["code"], "empty_conv")
        self.assertTrue(v["warn"])

    def test_legacy_state_without_conv_id_is_not_trusted(self):
        """门禁上线前的 state 只有 url、没有 conv_id：不能靠 url 相等自证清白。"""
        st = {"url": GEMINI_CONV, "conv_id": ""}
        v = platforms.bind_verdict(self.g, GEMINI_CONV, probe(GEMINI_CONV, 10), st)
        self.assertFalse(v["ok"])
        self.assertEqual(v["code"], "foreign_conv")


class TestSameConvResume(unittest.TestCase):
    """硬规则 1：同一对话。续期不能被门禁误伤。"""

    def setUp(self):
        self.g = platforms.get("gemini")

    def test_resume_same_conv_with_origin(self):
        st = {"url": GEMINI_CONV, "conv_id": "/app/a8b8143806a8b296",
              "conv_origin": "new_chat"}
        v = platforms.bind_verdict(self.g, GEMINI_CONV, probe(GEMINI_CONV, 12), st)
        self.assertTrue(v["ok"])
        self.assertEqual(v["code"], "resume")

    def test_legacy_state_with_conv_id_but_no_origin_is_not_trusted(self):
        """有 conv_id 但说不出来源：不许靠 url 相等自证清白（假空落盘后的自愈路径）。"""
        st = {"url": GEMINI_CONV, "conv_id": "/app/a8b8143806a8b296"}  # 无 conv_origin
        v = platforms.bind_verdict(self.g, GEMINI_CONV, probe(GEMINI_CONV, 10), st)
        self.assertFalse(v["ok"])
        self.assertEqual(v["code"], "foreign_conv")

    def test_legacy_state_with_empty_conv_can_pass_after_reconfirm(self):
        st = {"url": GEMINI_CONV, "conv_id": "/app/a8b8143806a8b296"}
        v = platforms.bind_verdict(self.g, GEMINI_CONV, probe(GEMINI_CONV, 0), st)
        self.assertTrue(v["ok"])
        self.assertEqual(v["code"], "empty_conv")

    def test_switching_to_another_conv_is_rejected(self):
        st = {"conv_id": "/app/a8b8143806a8b296", "conv_origin": "new_chat"}
        v = platforms.bind_verdict(self.g, GEMINI_OTHER, probe(GEMINI_OTHER, 3), st)
        self.assertFalse(v["ok"])
        self.assertEqual(v["code"], "conv_changed")
        self.assertIn("硬规则 1", v["hint"])

    def test_switching_is_possible_only_by_explicit_adoption(self):
        st = {"conv_id": "/app/a8b8143806a8b296", "conv_origin": "new_chat"}
        v = platforms.bind_verdict(self.g, GEMINI_OTHER, probe(GEMINI_OTHER, 3), st,
                                   reuse_existing=True)
        self.assertTrue(v["ok"])
        self.assertEqual(v["code"], "adopted")
        self.assertTrue(v["warn"])


class TestNewChat(unittest.TestCase):
    """--new 的唯一合法落点：无会话身份 + 0 条消息。"""

    def setUp(self):
        self.g = platforms.get("gemini")

    def test_platform_declares_new_chat_entry(self):
        self.assertEqual(self.g["new_chat_url"], GEMINI_NEW,
                         "Gemini 的新对话入口是 /app；用 home 会被重定向到最近对话")

    def test_clean_new_chat_page_passes(self):
        st = {"url": "", "conv_id": ""}
        v = platforms.bind_verdict(self.g, GEMINI_NEW, probe(GEMINI_NEW, 0), st, new_chat=True)
        self.assertTrue(v["ok"])
        self.assertEqual(v["code"], "new_chat")
        self.assertEqual(v["conv_id"], "", "新对话首轮之前没有身份，发完第一条才有")

    def test_entry_redirected_back_to_existing_conv_is_rejected(self):
        """真实风险：/app 在登录态下可能落回最近一次对话 —— 那等于没新开。"""
        st = {"url": "", "conv_id": ""}
        v = platforms.bind_verdict(self.g, GEMINI_NEW, probe(GEMINI_CONV, 10), st, new_chat=True)
        self.assertFalse(v["ok"])
        self.assertEqual(v["code"], "new_chat_redirected")
        self.assertEqual(v["conv_id"], "/app/a8b8143806a8b296")

    def test_new_chat_page_with_messages_is_rejected(self):
        st = {"url": "", "conv_id": ""}
        v = platforms.bind_verdict(self.g, GEMINI_NEW, probe(GEMINI_NEW, 4), st, new_chat=True)
        self.assertFalse(v["ok"])
        self.assertEqual(v["code"], "new_chat_not_empty")


class TestBadUrl(unittest.TestCase):

    def test_home_is_not_a_conversation(self):
        g = platforms.get("gemini")
        v = platforms.bind_verdict(g, "https://gemini.google.com/",
                                   probe("https://gemini.google.com/", 0), {"conv_id": ""})
        self.assertFalse(v["ok"])
        self.assertEqual(v["code"], "bad_url")

    def test_error_codes_are_all_documented(self):
        """每个可能返回的 code 都要在 BIND_CODE 里有中文解释，否则报错时人会看不懂。"""
        used = {"new_chat", "resume", "empty_conv", "adopted", "bad_url",
                "new_chat_redirected", "new_chat_not_empty", "conv_changed", "foreign_conv"}
        self.assertEqual(used, set(platforms.BIND_CODE),
                         "BIND_CODE 与实际返回码必须一一对应")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestDoubaoReplay(unittest.TestCase):
    """2026-10-29 豆包实测事实回放（D:/Project/Starpilot runtime/db 侦察链）。"""

    def setUp(self):
        self.p = platforms.get("doubao")

    def test_doubao_is_verified(self):
        self.assertTrue(self.p["verified"])
        self.assertEqual(self.p["write_method"], "exec")
        self.assertEqual(self.p["write_allowed"], ["exec"],
                         "豆包编辑器是 Tiptap：fill 键盘模拟实测必然失败，白名单只能有 exec")

    def test_doubao_send_method_is_enter(self):
        self.assertEqual(self.p["send_method"], "enter",
                         "实测：发送按钮 bsk click 无反应，只有「exec 写入 + 焦点不丢 + Enter」发得出去")

    def test_doubao_real_conversation_url_matches(self):
        u = "https://www.doubao.com/chat/38450203983852802"
        self.assertTrue(platforms.conv_id_of(self.p, u), "实测会话 id 是纯数字，url_pattern 必须认得")
        self.assertEqual(platforms.conv_id_of(self.p, "https://www.doubao.com/chat/"), "",
                         "新对话页 /chat/（无 id）不能被当成已有对话")

    def test_doubao_new_chat_entry(self):
        self.assertEqual(self.p["new_chat_url"], "https://www.doubao.com/chat/")

    def test_doubao_uses_official_testids(self):
        sel = self.p["sel"]
        self.assertIn("send_message", " ".join(sel["user_messages"]))
        self.assertIn("receive_message", " ".join(sel["messages"]))
        self.assertIn("union_message", " ".join(sel["msg_containers"]))

    def test_enter_path_js_focuses_editor(self):
        js = platforms.js_focus_editor(self.p)
        self.assertIn("activeElement === ed", js, "enter 发送的前提断言：焦点确实回到编辑器")
