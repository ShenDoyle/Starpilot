#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无人值守的三种「情况」怎么走（W 裁定 2026-10-30）。

三条口径全部改过，别按老文档写：
  1. 同目标连续失败两次 → **回网页 AI 换方案**（replan），不是停下来找人；
     只有轮次预算烧完才 HANDOFF。
  2. 对话地址/身份漂移 → **自愈**：收旧窗口、开新的独立对话、校验后继续（rebind），
     不暂停；对话彻底消失也走同一条路，只是下一轮要先贴项目现状摘要。
  3. 冒出新歧义 → 由网页 AI 按「符合项目要求的最优解」裁定（decide 记账），
     收口/人介入时必须逐条告知，**不许悄悄决定后无声交付**。

共享接口：`PAUSED_HUMAN → READY` 的修补型迁移 actor 是 `recovery`，
所以这两条自愈路径不需要人点头，但都会在 journal 里留一条可审计的事件。
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import orchestrator as orch  # noqa: E402
import write_gate as wg  # noqa: E402


def _ns(**kw):
    base = dict(workspace="", run="", reason="", state="", platform="",
                question="", choice="", why="", decided_by="", reversible="",
                scope="", goal="g", stage="s1", slug="t", max_rounds=0,
                max_same_error=0, round_timeout=0, send_timeout=0, card="",
                exec_mode="")
    base.update(kw)
    return SimpleNamespace(**base)


def _capture(fn, *a):
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        rc = fn(*a)
    finally:
        sys.stdout = old
    txt = buf.getvalue().strip()
    try:
        return rc, json.loads(txt) if txt else {}
    except Exception:
        return rc, {"raw": txt}


class _Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="sp-auto-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        rc, out = _capture(orch.cmd_init, _ns(workspace=self.root, exec_mode="auto"))
        self.assertEqual(rc, 0, out)
        self.run_id = wg.read_pointer(self.root)

    def _paused(self, **fields):
        st = orch.load_run_state(self.root, self.run_id)
        st.update({"state": "PAUSED_HUMAN", "platform": "kimi"})
        st.update(fields)
        orch.save_run_state(self.root, st, self.run_id)
        return st

    def _state(self):
        return orch.load_run_state(self.root, self.run_id)

    def _journal(self, action):
        return [e for e in wg.read_journal(orch.run_dir(self.root, self.run_id))
                if e.get("action") == action]


class TestReplan(_Base):
    def test_two_failures_go_back_to_ai_not_human(self):
        self._paused(fail_streak=2, last_error_class="click_send_failed")
        rc, out = _capture(orch.cmd_replan,
                           _ns(workspace=self.root, run=self.run_id, reason="两次都点不出去"))
        self.assertEqual(rc, 0, out)
        self.assertTrue(out["ok"])
        st = self._state()
        self.assertEqual(st["state"], "READY")
        self.assertTrue(st["needs_replan"])
        self.assertEqual(st["fail_streak"], 0)
        ev = self._journal("replan")
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["fail_streak_before"], 2)
        self.assertEqual(ev[0]["reason"], "两次都点不出去")

    def test_reset_goes_through_single_write_path(self):
        self._paused(fail_streak=2)
        _capture(orch.cmd_replan, _ns(workspace=self.root, run=self.run_id))
        self.assertEqual(self._state()["last_verdict"], "REPLAN")

    def test_refused_outside_paused(self):
        st = self._state()
        st["state"] = "READY"
        orch.save_run_state(self.root, st, self.run_id)
        rc, out = _capture(orch.cmd_replan, _ns(workspace=self.root, run=self.run_id))
        self.assertEqual(rc, 5)
        self.assertFalse(out["ok"])
        self.assertEqual(self._state()["state"], "READY")

    def test_refused_when_budget_exhausted(self):
        st = self._paused(fail_streak=2)
        st["rounds_used"] = int((st.get("goal") or {}).get("max_rounds") or 12)
        orch.save_run_state(self.root, st, self.run_id)
        rc, out = _capture(orch.cmd_replan, _ns(workspace=self.root, run=self.run_id))
        self.assertEqual(rc, 5)
        self.assertIn("预算", out["reason"])
        self.assertEqual(self._state()["state"], "PAUSED_HUMAN")


class TestRebind(_Base):
    def test_reclaims_window_and_opens_new_conversation(self):
        self._paused(fail_streak=0)
        calls = []

        def fake(root, argv, state_name="", timeout=180):
            calls.append(argv)
            if argv[0] == "stop":
                return 0, {"ok": True}
            return 0, {"ok": True, "url": "https://www.kimi.com/chat/abc"}

        with mock.patch.object(orch, "_run_wa", side_effect=fake):
            rc, out = _capture(orch.cmd_rebind,
                               _ns(workspace=self.root, run=self.run_id, reason="drift"))
        self.assertEqual(rc, 0, out)
        self.assertTrue(out["ok"])
        self.assertEqual([c[0] for c in calls], ["stop", "bind"])
        self.assertIn("--new", calls[1])
        st = self._state()
        self.assertEqual(st["state"], "READY")
        self.assertTrue(st["needs_context_digest"])
        self.assertEqual(st["bound_url"], "https://www.kimi.com/chat/abc")
        self.assertEqual(st["target_url_sha256"],
                         wg.sha16("https://www.kimi.com/chat/abc"))
        ev = self._journal("rebind")
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["url_sha256"], st["target_url_sha256"])

    def test_bind_failure_keeps_paused_and_says_do_not_improvise(self):
        self._paused()

        def fake(root, argv, state_name="", timeout=180):
            if argv[0] == "stop":
                return 0, {"ok": True}
            return 1, {"ok": False, "detail": "new_chat_redirected"}

        with mock.patch.object(orch, "_run_wa", side_effect=fake):
            rc, out = _capture(orch.cmd_rebind, _ns(workspace=self.root, run=self.run_id))
        self.assertEqual(rc, 5)
        self.assertEqual(out["stage"], "bind_new")
        self.assertEqual(self._state()["state"], "PAUSED_HUMAN")

    def test_refuses_without_platform(self):
        self._paused(platform="")
        rc, out = _capture(orch.cmd_rebind, _ns(workspace=self.root, run=self.run_id))
        self.assertEqual(rc, 5)
        self.assertIn("平台", out["reason"])


class TestDecide(_Base):
    def test_records_decision(self):
        rc, out = _capture(orch.cmd_decide, _ns(
            workspace=self.root, run=self.run_id,
            question="报告用 markdown 还是 pdf", choice="markdown",
            why="项目全程都是 markdown，AI 裁定保持同构", decided_by="web_ai"))
        self.assertEqual(rc, 0, out)
        self.assertEqual(out["count"], 1)
        ev = self._journal("autonomy")
        self.assertEqual(ev[0]["choice"], "markdown")
        self.assertEqual(ev[0]["decided_by"], "web_ai")

    def test_refuses_decision_without_reason(self):
        rc, out = _capture(orch.cmd_decide, _ns(
            workspace=self.root, run=self.run_id, question="q", choice="c"))
        self.assertEqual(rc, 5)
        self.assertIn("--why", out["reason"])
        self.assertEqual(self._journal("autonomy"), [])

    def test_digest_for_close(self):
        _capture(orch.cmd_decide, _ns(workspace=self.root, run=self.run_id,
                                      question="q1", choice="c1", why="w1"))
        _capture(orch.cmd_decide, _ns(workspace=self.root, run=self.run_id,
                                      question="q2", choice="c2", why="w2"))
        self._paused(fail_streak=2)
        _capture(orch.cmd_replan, _ns(workspace=self.root, run=self.run_id))
        dig = orch.autonomy_digest(orch.run_dir(self.root, self.run_id))
        self.assertEqual(dig["autonomy_count"], 2)
        self.assertEqual(dig["replan_count"], 1)
        self.assertTrue(dig["must_report"])
        self.assertEqual(dig["autonomy_decisions"][0]["question"], "q1")

    def test_digest_empty_when_nothing_bold_happened(self):
        dig = orch.autonomy_digest(orch.run_dir(self.root, self.run_id))
        self.assertEqual(dig["autonomy_count"], 0)
        self.assertFalse(dig["must_report"])


if __name__ == "__main__":
    unittest.main()
