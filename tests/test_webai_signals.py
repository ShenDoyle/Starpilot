#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""webai 交互识别的口径测试（2026-11-09 八条需求的核心判定，全部离线）。

用户口径逐条对应的测试：
  1. 正常回复 ≤2 分钟 → ttft_s=30 / stall_s=45（platforms._DEFAULTS）
  2. 弹窗 ≠ 一定是限制类 → classify_modal 不匹配就 other，不改变判定路径
  3. 上下文满弹窗有「新聊天」→ STALL_CONTEXT_FULL + hint 指到 modal-click
  4. 限流 vs 其它 → STALL_RATE_LIMITED 独立档位
  6. 回复完成看图标/发送键 → sel.reply_done 注入 js_probe
  7. 上下文按 token 不按轮数 → 分类关键词不含轮数；msg_count 不进容量判断
"""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import platforms as pl  # noqa: E402
import wa  # noqa: E402


class TimeBudgetTest(unittest.TestCase):
    """口径 1：正常回复很快，不会超过 2 分钟。"""

    def test_default_clocks(self):
        p = pl.get("chatgpt")
        self.assertEqual(p["ttft_s"], 30)
        self.assertEqual(p["stall_s"], 45)

    def test_wait_cli_defaults(self):
        # 总时钟默认 150s（口径：回复 ≤2 分钟）；首字/停滞时钟帮助文本对齐新默认
        captured = {}

        def fake(a):
            captured["timeout"] = a.timeout
            captured["first_token"] = a.first_token
            captured["stall"] = a.stall
            return 0

        with mock.patch.object(wa, "cmd_wait", fake):
            rc = wa.main(["wait"])
        self.assertEqual(rc, 0)
        self.assertEqual(captured["timeout"], 150)
        self.assertEqual(captured["first_token"], 0)   # 0 = 用平台 ttft_s(30)
        self.assertEqual(captured["stall"], 0)         # 0 = 用平台 stall_s(45)


class ClassifyModalTest(unittest.TestCase):
    """口径 2/4/7：弹窗分类只认文案，不猜；弹窗≠限制类。"""

    def test_context_full_zh(self):
        r = pl.classify_modal("此对话中的上下文太长了。开始新聊天以继续。")
        self.assertEqual(r["kind"], pl.MODAL_CONTEXT_FULL)
        self.assertTrue(r["basis"])

    def test_context_full_en(self):
        r = pl.classify_modal("This conversation has reached its maximum length.")
        self.assertEqual(r["kind"], pl.MODAL_CONTEXT_FULL)

    def test_rate_limit_kimi(self):
        r = pl.classify_modal("和Kimi聊天的人太多了，订阅会员可进入优先队列")
        # kimi 的「人太多」是限流不是额度：限流优先级高于 quota 关键词
        self.assertEqual(r["kind"], pl.MODAL_RATE_LIMIT)
        self.assertEqual(r["basis"], "人太多")

    def test_quota(self):
        r = pl.classify_modal("今日额度已用完，升级后可继续使用")
        self.assertEqual(r["kind"], pl.MODAL_QUOTA)

    def test_other_modal_is_not_restricted(self):
        # 弹窗 ≠ 限制类：确认框/公告落 other，不许歪进任何限制档
        for text in ("确认要删除这条消息吗？", "系统维护公告：今晚 02:00 升级", "接受新的服务条款"):
            self.assertEqual(pl.classify_modal(text)["kind"], pl.MODAL_OTHER,
                             text[:20])

    def test_empty(self):
        self.assertEqual(pl.classify_modal("")["kind"], pl.MODAL_NONE)
        self.assertEqual(pl.classify_modal(None)["kind"], pl.MODAL_NONE)

    def test_no_turn_count_keyword(self):
        # 口径 7：上下文按 token 总数，不用「轮数/条数」近似判断——关键词表里不许混入轮数
        words = " ".join(pl._MODAL_PATTERNS[0][1]).lower()
        for bad in ("轮", "turn", "round", "message count", "条消息"):
            self.assertNotIn(bad, words)


class JudgeStallModalTest(unittest.TestCase):
    """judge_stall 的弹窗分支：限制类优先于等回复逻辑，其它弹窗只留证。"""

    BASE = {"msg_count": 2, "reply_count": 1, "doc_len": 100, "doc_nodes": 50,
            "editor_len": 0, "conv_id": "/c/x"}

    def _cur(self, **kw):
        d = {"url": "https://chatgpt.com/c/x", "modal": True,
             "modal_text": "", "generating": False, "last_len": 0,
             "msg_count": 2, "reply_count": 1, "doc_len": 100,
             "doc_nodes": 50, "editor_len": 0}
        d.update(kw)
        return d

    def test_context_full_wins(self):
        cur = self._cur(modal_text="上下文太长，请开始新聊天。")
        out = pl.judge_stall(pl.get("chatgpt"), cur, self.BASE)
        self.assertEqual(out["verdict"], pl.STALL_CONTEXT_FULL)
        self.assertEqual(out["modal"]["kind"], pl.MODAL_CONTEXT_FULL)
        self.assertIn("modal-click", out["hint"])
        self.assertIn("token", out["hint"])  # 口径 7 写进处置提示

    def test_rate_limit(self):
        cur = self._cur(modal_text="当前访问人数过多，请稍后再试")
        out = pl.judge_stall(pl.get("chatgpt"), cur, self.BASE)
        self.assertEqual(out["verdict"], pl.STALL_RATE_LIMITED)
        self.assertIn("冷却", out["hint"])

    def test_other_modal_keeps_existing_path(self):
        # 其它弹窗：不改变判定路径——编辑器有字 + 消息数没涨 = 发送没生效（老结论）
        cur = self._cur(modal_text="确认要删除吗？", editor_len=25, msg_count=2)
        out = pl.judge_stall(pl.get("chatgpt"), cur, self.BASE)
        self.assertEqual(out["verdict"], pl.STALL_NOT_SENT)
        self.assertIn("modal_text", out["evidence"])  # 但弹窗文案要留证

    def test_rate_limit_even_when_generating(self):
        # 限制类优先于「还在生成」——对着限流弹窗干等是最差的处置
        cur = self._cur(modal_text="服务器繁忙，请稍后再试", generating=True,
                        doc_len=200)
        out = pl.judge_stall(pl.get("chatgpt"), cur, self.BASE)
        self.assertEqual(out["verdict"], pl.STALL_RATE_LIMITED)


class ReplyDoneWiringTest(unittest.TestCase):
    """口径 6：回复完成看消息下方图标（平台显式声明，不许通用猜）。"""

    def test_chatgpt_declares_reply_done(self):
        s = pl.get("chatgpt")["sel"]
        self.assertTrue(s.get("reply_done"))
        joined = " ".join(s["reply_done"])
        self.assertIn('aria-label="复制"', joined)
        self.assertIn("Copy", joined)  # 英文界面也要覆盖（无空格写法，见选择器注释）

    def test_reply_done_css_from_sel_list(self):
        p = pl.get("chatgpt")
        self.assertIn("复制", " ".join(pl._reply_done_css(p)))

    def test_reply_done_fallback_selector(self):
        p = {"sel": {}, "reply_done_selector": "button.x-done"}
        self.assertEqual(pl._reply_done_css(p), ["button.x-done"])
        self.assertEqual(pl._reply_done_css({"sel": {}}), [])

    def test_probe_js_carries_reply_done(self):
        js = pl.js_probe(pl.get("chatgpt"))
        self.assertIn("reply_done", js)
        self.assertNotIn("__REPLY_DONE_SEL__", js)  # 占位符必须被替换掉
        self.assertIn("aria-label", js)

    def test_probe_without_declaration_is_false(self):
        # 没声明的平台：reply_done 恒 false（不许拿通用猜测当判据）
        p = pl.get("kimi")
        self.assertEqual(pl._reply_done_css(p), [])
        js = pl.js_probe(p)
        self.assertIn("[]", js)  # 空候选注入


class StallRcMappingTest(unittest.TestCase):
    """新档位要有稳定退出码，且不与既有码段冲突。"""

    def test_mapping(self):
        self.assertEqual(wa.STALL_RC[pl.STALL_CONTEXT_FULL], 16)
        self.assertEqual(wa.STALL_RC[pl.STALL_RATE_LIMITED], 17)

    def test_no_unintended_collision(self):
        # rc=10 由 risk/login 共享是刻意设计（处置同为「需要人」）；
        # 除此之外每档必须有独立码，否则上层没法按 rc 分支
        vals = {}
        for verdict, rc in wa.STALL_RC.items():
            vals.setdefault(rc, set()).add(verdict)
        shared = {rc: v for rc, v in vals.items() if len(v) > 1}
        self.assertEqual(set(shared), {10})
        self.assertEqual(shared.get(10),
                         {pl.STALL_RISK, pl.STALL_LOGIN})

    def test_modal_click_js_scoped_to_modal(self):
        js = pl.js_modal_click("new ?chat|新聊天")
        # 只在弹窗内部找按钮：全局 querySelector('button') 绝不允许出现
        self.assertIn("visibleModal", js)
        self.assertIn("visibleModal.m.querySelectorAll", js)
        self.assertNotIn("document.querySelectorAll('button'", js)
        self.assertIn("新聊天", js)  # 正则安全注入


if __name__ == "__main__":
    unittest.main()
