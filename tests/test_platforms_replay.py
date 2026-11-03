#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""平台选择器的**离线回放**测试（架构评审 S5）。

为什么要它：选择器是平台侧最高频变动的东西。以前改一个 `sel` 只能靠
「真人登录 + 真发一条消息」验证，风险高、而且受硬规则 1 约束连探测都不许。
这里用**录制下来的真实 DOM** 在 unittest 里重放：改坏了当场红，不需要登录。

**它验什么 / 不验什么**（写死边界，别拿它当浏览器）：
- 验：选择器在这份真机 DOM 上命中谁、命中几个；编辑器是什么标签；写白名单是否自洽。
- 不验：JS 执行、交互时序、`innerText`/`value` 的真实取值。
  真机验证（`wa.py probe` + 一轮真实收发）仍是准出条件，二者职责分离。

重录夹具的方法见 `tests/fixtures/<platform>/meta.json` 的 note 字段。
"""
from __future__ import annotations

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import domsel  # noqa: E402
import platforms  # noqa: E402


def _meta(platform):
    with open(domsel.fixture_path(platform, "meta.json"), encoding="utf-8") as f:
        return json.load(f)


class TestSelectorSyntax(unittest.TestCase):
    """所有平台的选择器都必须能被解析——挡住「手滑写错一个括号」。"""

    def test_every_selector_parses(self):
        for key in platforms.PLATFORMS:
            p = platforms.get(key)
            for group, cands in p["sel"].items():
                for s in cands or []:
                    with self.subTest(platform=key, group=group, sel=s):
                        domsel.parse_compound(domsel._split_top(s)[0].split()[-1])


class TestDeepSeekReplay(unittest.TestCase):
    """DeepSeek 是 textarea + div 发送键 + 虚拟列表——三个坑都在这份夹具里。"""

    @classmethod
    def setUpClass(cls):
        cls.p = platforms.get("deepseek")
        cls.comp = domsel.load_fixture("deepseek", "composer.html")
        cls.msgs = domsel.load_fixture("deepseek", "messages.html")
        cls.meta = _meta("deepseek")

    def test_editor_is_textarea(self):
        sel, node, n = domsel.first_hit(self.comp, self.p["sel"]["editor"])
        self.assertIsNotNone(node, "编辑器选择器一个都没命中：%s" % self.p["sel"]["editor"])
        self.assertEqual(node.tag, "textarea",
                         "DeepSeek 的编辑器是 textarea；命中 %s 说明选择器退化了" % node.tag)

    def test_send_button_hits_primary_circle(self):
        sel, node, n = domsel.first_hit(self.comp, self.p["sel"]["send"])
        self.assertIsNotNone(node, "发送按钮没命中")
        cls = node.attrs.get("class", "")
        self.assertIn("ds-button--primary", cls)
        self.assertIn("circle", cls)

    def test_disabled_variant_is_recognised_by_js(self):
        """空输入时发送键带 `ds-button--disabled`（BEM 变体）。

        旧的 isDisabled 正则只认 `disabled`/`is-disabled`，认不出这个变体 ——
        会把「不能点」当成「能点」，点击后什么都不发生。
        """
        js = platforms.js_click_send(self.p)
        self.assertIn("--", js, "isDisabled 必须认 BEM 变体（--disabled）")
        # 夹具里此刻输入框是空的 → 按钮确实带 disabled 类（实测事实）
        sel, node, n = domsel.first_hit(self.comp, self.p["sel"]["send"])
        self.assertIn("ds-button--disabled", node.attrs.get("class", ""))

    def test_message_counts_match_recorded_meta(self):
        asst = domsel.select_all(self.msgs, self.p["sel"]["messages"][0])
        allmsg = domsel.select_all(self.msgs, self.p["sel"]["msg_containers"][0])
        users = domsel.select_all(self.msgs, self.p["sel"]["user_messages"][0])
        self.assertEqual(len(asst), self.meta["expected"]["assistant"],
                         "助手气泡数应与录制时一致")
        self.assertEqual(len(allmsg), self.meta["expected"]["all_messages"],
                         "全部消息数应与录制时一致")
        self.assertEqual(len(users), self.meta["expected"]["user"],
                         "用户消息数应与录制时一致（:not(:has(...)) 生效）")

    def test_fill_js_has_textarea_branch(self):
        js = platforms.js_fill(self.p, "x")
        self.assertIn("TEXTAREA", js, "textarea 必须走 el.select() 分支")
        self.assertIn("value", js, "textarea 的长度只能从 value 读")

    def test_probe_js_uses_editor_value_helper(self):
        js = platforms.js_probe(self.p)
        self.assertIn("edText", js, "editor_len 必须走 edText（textarea 读 value）")
        self.assertIn("asst_fps", js, "探测必须返回锚点指纹")


class TestKimiReplay(unittest.TestCase):
    """Kimi 是 contenteditable + div 发送键 + 消息条目 div.chat-content-item-*。"""

    @classmethod
    def setUpClass(cls):
        cls.p = platforms.get("kimi")
        cls.comp = domsel.load_fixture("kimi", "composer.html")
        cls.msgs = domsel.load_fixture("kimi", "messages.html")
        cls.meta = _meta("kimi")

    def test_editor_hits_lexical_container(self):
        sel, node, n = domsel.first_hit(self.comp, self.p["sel"]["editor"])
        self.assertIsNotNone(node, "编辑器没命中")
        self.assertTrue(
            "chat-input-editor" in node.attrs.get("class", "")
            or node.attrs.get("contenteditable") == "true")

    def test_send_button_fallback_when_disabled(self):
        """空输入时首选（:not(.disabled)）命中 0 个，退化候选必须还能命中。

        这正是 `js_click_send` 自己做 disabled 检查的原因：选择器只负责定位，
        「能不能点」由 isDisabled 判。
        """
        primary = domsel.select_all(self.comp, self.p["sel"]["send"][0])
        fallback = domsel.select_all(self.comp, self.p["sel"]["send"][1])
        self.assertEqual(len(primary), 0, "空输入时首选应被 .disabled 过滤掉")
        self.assertGreaterEqual(len(fallback), 1, "退化候选必须仍能定位到发送键")

    def test_message_counts_match_recorded_meta(self):
        asst = domsel.select_all(self.msgs, self.p["sel"]["messages"][0])
        allmsg = domsel.select_all(self.msgs, self.p["sel"]["msg_containers"][0])
        users = domsel.select_all(self.msgs, self.p["sel"]["user_messages"][0])
        self.assertEqual(len(asst), self.meta["expected"]["assistant"])
        self.assertEqual(len(allmsg), self.meta["expected"]["items"])
        self.assertEqual(len(users), self.meta["expected"]["user"])


class TestGeminiReplay(unittest.TestCase):
    """Gemini = Quill（contenteditable）+ 条件渲染发送键 + 轮次容器 id。

    这份夹具最关键的价值不是「选择器命中」，而是**空态的证据**：
    composer.html 是编辑器为空时录的，所以 `button[aria-label="发送"]` 必须命中 **0 个**。
    谁要是把它改成「找不到发送键就等一下」，这条会立刻红。
    """

    @classmethod
    def setUpClass(cls):
        cls.p = platforms.get("gemini")
        cls.comp = domsel.load_fixture("gemini", "composer.html")
        cls.msgs = domsel.load_fixture("gemini", "messages.html")
        cls.meta = _meta("gemini")

    def test_editor_is_quill_contenteditable(self):
        sel, node, n = domsel.first_hit(self.comp, self.p["sel"]["editor"])
        self.assertIsNotNone(node, "编辑器选择器一个都没命中：%s" % self.p["sel"]["editor"])
        self.assertEqual(node.tag.lower(), "div", "Quill 是 contenteditable 的 div，不是 textarea")
        self.assertIn("ql-editor", node.attrs.get("class", ""),
                      "命中的不是 Quill 编辑器，实测是 %s" % node.attrs.get("class"))
        self.assertEqual(n, 1)

    def test_editor_is_blank_and_that_is_visible_in_the_dom(self):
        """空态必须能从 DOM 读出来（ql-blank）：这是「innerText 会骗人」的对照物。"""
        node = domsel.select_all(self.comp, "rich-textarea div.ql-editor")[0]
        self.assertIn("ql-blank", node.attrs.get("class", ""))
        self.assertTrue(self.p["blank_class"] and
                        self.p["blank_class"] in node.attrs.get("class", ""))

    def test_send_button_does_not_exist_when_blank(self):
        """条件渲染：空输入时页面上没有发送键。这是 `send_requires_text` 的事实依据。"""
        sel, node, n = domsel.first_hit(self.comp, self.p["sel"]["send"])
        self.assertIsNone(node, "空态下不该命中发送键，实际命中了 %s" % sel)
        self.assertEqual(n, self.meta["expected"]["send_button_when_blank"])

    def test_editor_candidates_never_hit_the_clipboard_scratchpad(self):
        """Quill 有个隐藏的 .ql-clipboard 也是 contenteditable。

        退化选择器 `div[contenteditable="true"]` 一旦先命中它，字会打进一个不可见的
        剪贴板暂存区 —— 不报错、不发出去、看起来「写成功了」。这条把顺序钉住。
        """
        allced = domsel.select_all(self.comp, 'div[contenteditable="true"]')
        self.assertEqual(len(allced), self.meta["expected"]["contenteditable"],
                         "contenteditable 数量变了：夹具里应有编辑器 + 剪贴板两个")
        first = domsel.select_all(self.comp, 'div[contenteditable="true"]')[0]
        self.assertNotIn("ql-clipboard", first.attrs.get("class", ""),
                         "退化选择器先命中了剪贴板区：写入会静默丢失")

    def test_messages_and_user_queries(self):
        asst = domsel.select_all(self.msgs, "model-response")
        users = domsel.select_all(self.msgs, "user-query")
        self.assertEqual(len(asst), self.meta["expected"]["assistant"])
        self.assertEqual(len(users), self.meta["expected"]["user"])

    def test_anchor_box_has_unique_hex_ids(self):
        """轮次容器 id 是锚点的来源：必须十六进制、必须唯一。"""
        import re
        boxes = domsel.select_all(self.msgs, self.p["anchor_box"])
        self.assertEqual(len(boxes), self.meta["expected"]["turns"])
        ids = [b.attrs.get("id", "") for b in boxes]
        self.assertEqual(len(set(ids)), len(ids), "轮次 id 有重复：不能当消息身份")
        for i in ids:
            self.assertRegex(i, r"^[0-9a-f]{8,}$",
                             "轮次 id 不是纯十六进制：hex 兜底规则会失效")


class TestPlatformContract(unittest.TestCase):
    """平台元信息自洽性：写白名单、verified、编辑器取值方式。"""

    def test_write_whitelist_consistent(self):
        for key in platforms.PLATFORMS:
            p = platforms.get(key)
            if p.get("verified"):
                self.assertIn(p.get("write_method"), p.get("write_allowed") or [],
                              "%s 已实测，但 write_method 不在白名单里" % key)
            elif not p.get("write_verified"):
                self.assertEqual(p.get("write_allowed") or [], [],
                                 "%s 未实测且写路径未单独复测，不许有真实写路径" % key)

    def test_verified_count_is_honest(self):
        verified = [k for k in platforms.PLATFORMS if platforms.get(k).get("verified")]
        self.assertIn("deepseek", verified, "DeepSeek 2026-10-29 已实测通过")
        self.assertIn("gemini", verified, "Gemini 2026-10-29 已实测通过")
        self.assertIn("kimi", verified)
        self.assertNotIn("chatgpt", verified,
                         "2026-11-02 DOM 改版后计数选择器失效：重标定前不许标 verified")
        self.assertIn("重标定", platforms.get("chatgpt").get("notes", ""))
        self.assertIn("doubao", verified, "豆包 2026-10-29 已实测通过（Tiptap + exec + enter 发送）")
        for k in ("claude",):
            self.assertNotIn(k, verified, "%s 没实测过，不许标 verified" % k)


class TestChatGPTReplay(unittest.TestCase):
    """ChatGPT 2026-11-02 改版把 `data-message-author-role` 整个删了，而仓库里
    **没有 ChatGPT 夹具** —— 于是选择器失效在 unittest 阶段一条都不红，一路红到线上：
    `reply_count` 恒 0 ⇒ 每次发送都判 `SENT_NO_REPLY` ⇒ agent 以为没发出去 ⇒ 重发 ⇒
    写闸门又被 `verified=false` 拦 ⇒ 走逃生门 ⇒ pending.md 攒下 32 条 P0-ESC。
    这条链的起点就是一个没人录夹具的选择器。本类把它钉死。

    夹具来源见 `meta.json`：真机 `bsk get-html` 快照（只读 session），裁剪只删
    script/style 正文与 svg 图形数据，元素树与 data-* 原样保留。
    """

    @classmethod
    def setUpClass(cls):
        cls.p = platforms.get("chatgpt")
        cls.comp = domsel.load_fixture("chatgpt", "composer.html")
        cls.msgs = domsel.load_fixture("chatgpt", "messages.html")
        cls.meta = _meta("chatgpt")

    def test_editor_narrow_selector_hits_exactly_one(self):
        """页面上有 5 个 div.ProseMirror（消息渲染块 + 写作块同名），
        裸选择器命中多个时 bsk 直接拒绝（selector matched N elements），
        clear/fill 全失败、输入框残留清不掉。唯一命中主输入框的是 form 前缀。"""
        sel, node, n = domsel.first_hit(self.comp, self.p["sel"]["editor"])
        self.assertEqual(sel, "form div.ProseMirror",
                         "编辑器首选被换掉了：它会命中多个节点")
        self.assertEqual(n, self.meta["expected"]["editor_in_composer"])
        broad = domsel.select_all(self.msgs, 'div.ProseMirror')
        self.assertGreater(len(broad), 1,
                           "正文区里确实存在同名 ProseMirror：必须靠 form 收窄")

    def test_reply_anchor_is_self_attached_uuid(self):
        """助手消息身份：节点自带 data-chatgpt-selection-message-id，且互不重复。"""
        asst = domsel.select_all(self.msgs, self.p["sel"]["messages"][0])
        self.assertEqual(len(asst), self.meta["expected"]["assistant"])
        uuids = [a.attrs.get("data-chatgpt-selection-message-id") or "" for a in asst]
        self.assertTrue(all(uuids), "有助手节点不带身份 UUID：锚点会退化成正文指纹")
        self.assertEqual(len(set(uuids)), len(uuids), "助手 UUID 有重复：不能当身份")

    def test_user_anchor_needs_ancestor_walk(self):
        """用户气泡自己**没有** UUID，UUID 在轮次容器上 —— key_up 必须够长。

        这一条是 `key_attrs` 与 `key_up` 两个字段存在的理由：只查节点自身的话，
        用户侧永远取不到身份，anchor_delta 分不清「新提问」和「重发」。
        """
        users = domsel.select_all(self.msgs, self.p["sel"]["user_messages"][0])
        self.assertEqual(len(users), self.meta["expected"]["user"])
        self.assertEqual(sum(1 for u in users
                             if u.attrs.get("data-chatgpt-search-message-ids")), 0,
                         "用户气泡若自带 UUID，key_up 就可以缩短——先如实记录现状")
        got = []
        for u in users:
            got.append(next((a.attrs.get("data-chatgpt-search-message-ids")
                             for a in u.ancestors()
                             if a.attrs.get("data-chatgpt-search-message-ids")), ""))
        self.assertTrue(all(got), "向上走 key_up 层仍取不到轮次 UUID：会退回正文指纹口径")
        self.assertEqual(len(set(got)), len(got))
        self.assertGreaterEqual(self.p.get("key_up", 0), 4,
                                "祖先深度不足：ChatGPT 的 UUID 在轮次容器上")

    def test_message_container_count_is_language_independent(self):
        """msg_containers 走 DOM（12 个轮次容器）：英文界面也算得出来。

        旧口径靠中文 marker「你说：」数总数，界面一换语言 msg_count 就变 0 ——
        发送校验的主信号因此消失，被判「没发出去」。
        """
        allm = domsel.select_all(self.msgs, self.p["sel"]["msg_containers"][0])
        self.assertEqual(len(allm), self.meta["expected"]["all_messages"])
        self.assertTrue(self.p.get("count_from_users"),
                        "ChatGPT 必须允许 marker 落空时退回 DOM 口径")

    def test_revoked_attribute_is_not_relied_on(self):
        """已废弃的 role 属性在这份真机快照里命中 0：只许当兼容候选，不许当依据。"""
        self.assertEqual(
            len(domsel.select_all(self.msgs, '[data-message-author-role]')),
            self.meta["expected"]["old_role_attr"])

    def test_probe_js_carries_key_attrs(self):
        js = platforms.js_probe(self.p)
        self.assertIn("data-chatgpt-selection-message-id", js,
                      "key_attrs 没进生成的 JS：锚点等于没接上")
        self.assertIn("data-chatgpt-search-message-ids", js)
        self.assertIn("new Set(asstKeys).size", js,
                      "回复计数没按身份去重：虚拟列表重挂会把一条算成两条")

    def test_send_control_absence_is_a_declared_gap(self):
        """录制时编辑器是空的 → 发送键**条件渲染**，页面上根本没有它（实测 type=submit 命中 0）。

        这条不许变绿成「选择器没问题」：它要求平台必须声明 `send_requires_text`，
        这样「写完但发送键没出现」会当失败报出来（FILL_NO_SEND），
        而不是对空气点一次然后白等一轮。发送键的真实标记仍待**有字时**重录夹具。
        """
        hits = sum(len(domsel.select_all(self.comp, s)) for s in self.p["sel"]["send"])
        self.assertEqual(hits, self.meta["expected"]["send_candidates_in_composer"])
        self.assertTrue(self.p.get("send_requires_text"),
                        "ChatGPT 的发送键是条件渲染的：必须声明，否则空态会被当成可点")

    def test_verified_stays_false_until_loop_acceptance(self):
        """计数锚点重标定 ≠ 整链路实测。5/5 独立问答验收 + 发送键定标前不许翻 True。"""
        self.assertFalse(self.p.get("verified"))
        self.assertIn("重标定", self.p.get("notes", ""))


if __name__ == "__main__":
    unittest.main()
