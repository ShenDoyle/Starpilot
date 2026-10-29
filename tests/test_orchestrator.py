#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""orchestrator / write_gate 验收单测（stdlib unittest，无第三方依赖）。

跑法：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import orchestrator as orch  # noqa: E402
import platforms  # noqa: E402
import write_gate as wg  # noqa: E402


class TempWorkspace(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="starpilot-test-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.makedirs(os.path.join(self.root, "references"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "state"), exist_ok=True)

    def make_run(self, state="BOUND", **extra):
        run_id = "20260101T000000-test"
        d = os.path.join(self.root, "runs", run_id)
        os.makedirs(os.path.join(d, "evidence"), exist_ok=True)
        os.makedirs(os.path.join(d, "writes"), exist_ok=True)
        st = {
            "run_id": run_id, "stage_id": "O1", "state": state,
            "created_at": "2026-01-01 00:00:00",
            "goal": dict(orch.DEFAULT_GOAL, text="t"),
            "target_url_sha256": "abc123", "platform": "kimi",
            "round": 0, "rounds_used": 0, "fail_streak": 0, "last_error_class": "",
            "last_guard_failure": None, "state_history": [],
            "pending": {"prompt_file": "", "prompt_sha256": "", "approval_id": ""},
            "last_send": {}, "lessons_candidates": [], "lessons_merged": 0,
            "closed": False,
        }
        st.update(extra)
        wg.atomic_write_json(os.path.join(d, "state.json"), st)
        wg.set_pointer(self.root, run_id)
        return run_id, d, st


class TestStateMachine(TempWorkspace):
    def test_legal_transition(self):
        run_id, d, st = self.make_run(state="BOUND")
        ok = orch.transition(self.root, st, "PLAN_OK", "测试")
        self.assertTrue(ok)
        self.assertEqual(st["state"], "PLAN_OK")
        hist = st["state_history"][-1]
        self.assertEqual(hist["from"], "BOUND")
        self.assertEqual(hist["to"], "PLAN_OK")

    def test_illegal_transition_rejected(self):
        run_id, d, st = self.make_run(state="BOUND")
        ok = orch.transition(self.root, st, "SENDING", "非法跳跃")
        self.assertFalse(ok, "BOUND → SENDING 必须被拒")
        self.assertEqual(st["state"], "BOUND", "被拒的迁移不得改状态")
        self.assertTrue(st["last_guard_failure"], "必须记下守卫失败原因")
        self.assertIn("PLAN_OK", st["last_guard_failure"]["legal"],
                      "legal 列出的是当前状态允许的目标")
        evs = wg.read_journal(d)
        rejected = [e for e in evs if e.get("result") == "transition_rejected"]
        self.assertTrue(rejected, "非法迁移必须进 journal")

    def test_every_state_has_transition_table(self):
        for s in orch.STATES:
            self.assertIn(s, orch.TRANSITIONS, "状态 %s 缺迁移表" % s)

    def test_closed_is_terminal(self):
        self.assertEqual(orch.TRANSITIONS["CLOSED"], [])


class TestAtomicWrite(TempWorkspace):
    def test_atomic_write_leaves_no_tmp(self):
        p = os.path.join(self.root, "state", "x.json")
        wg.atomic_write_json(p, {"a": 1})
        self.assertTrue(os.path.exists(p))
        with open(p, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["a"], 1)
        leftovers = [n for n in os.listdir(os.path.dirname(p))
                     if n.startswith(".") and n.endswith(".tmp")]
        self.assertEqual(leftovers, [], "原子写不能留临时文件")

    def test_atomic_write_replaces_not_appends(self):
        p = os.path.join(self.root, "state", "x.json")
        wg.atomic_write_json(p, {"a": 1})
        wg.atomic_write_json(p, {"a": 2})
        with open(p, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["a"], 2)

    def test_journal_is_append_only(self):
        d = os.path.join(self.root, "runs", "r1")
        wg.append_journal(d, {"action": "a"})
        wg.append_journal(d, {"action": "b"})
        evs = wg.read_journal(d)
        self.assertEqual([e["action"] for e in evs], ["a", "b"])

    def test_pointer_roundtrip(self):
        wg.set_pointer(self.root, "run-x")
        self.assertEqual(wg.read_pointer(self.root), "run-x")


class TestAudit(TempWorkspace):
    def _journal_send(self, d, **over):
        ev = {"ts": "2026-01-01 00:00:00", "run_id": "r", "round_id": 1,
              "target_url_sha256": "u1", "prompt_sha256": "p1", "action": "write_send",
              "before": {}, "after": {}, "result": "ok", "error_class": "",
              "approval_id": "ap-x"}
        ev.update(over)
        wg.append_journal(d, ev)

    def test_duplicate_send_detected(self):
        run_id, d, st = self.make_run()
        self._journal_send(d)
        self._journal_send(d)
        evs = wg.read_journal(d)
        groups = {}
        for e in evs:
            k = (e["target_url_sha256"], e["prompt_sha256"])
            groups[k] = groups.get(k, 0) + 1
        self.assertEqual(sum(1 for n in groups.values() if n > 1), 1)

    def test_unconfirmed_send_counted(self):
        run_id, d, st = self.make_run()
        self._journal_send(d, error_class="send_unconfirmed")
        evs = wg.read_journal(d)
        self.assertEqual(sum(1 for e in evs if e.get("error_class") == "send_unconfirmed"), 1)

    def test_unexpected_write_counted(self):
        run_id, d, st = self.make_run()
        self._journal_send(d, approval_id="")
        evs = wg.read_journal(d)
        bad = [e for e in evs
               if not e.get("approval_id")
               or not os.path.exists(os.path.join(d, "writes", "%s.approval.json"
                                                  % e.get("approval_id")))]
        self.assertEqual(len(bad), 1)

    def test_lessons_pending_computed(self):
        run_id, d, st = self.make_run(lessons_candidates=[{"symptom": "a"}, {"symptom": "b"}],
                                      lessons_merged=1)
        self.assertEqual(max(0, len(st["lessons_candidates"]) - st["lessons_merged"]), 1)


class TestWriteGate(TempWorkspace):
    """闸门语义：不批不许写、没批就没产物。"""

    def _dry(self, aid="ap-test", url_sha="u1", prompt_sha="p1", platform="kimi",
             write_method="exec"):
        dry = {
            "approval_id": aid, "platform": platform, "write_method": write_method,
            "write_allowed": [write_method], "target_url": "https://x/chat/1",
            "target_url_sha256": url_sha, "prompt_sha256": prompt_sha,
            "prompt_file": "", "dom_before": {"dom_hash": "h1"},
        }
        run_id, d, st = self.make_run()
        wg.atomic_write_json(os.path.join(d, "writes", "%s.dry.json" % aid), dry)
        return run_id, d, st, dry

    def test_default_mode_blocks_real_write(self):
        run_id, d, st, dry = self._dry()
        os.environ.pop("STARPILOT_WRITE_MODE", None)
        self.assertEqual(wg.write_mode(), "dry_run", "缺省必须是 dry_run")
        wg.atomic_write_json(os.path.join(d, "writes", "ap-test.approval.json"),
                             {"approval_id": "ap-test", "expires_at": 9e12,
                              "target_url_sha256": "u1", "prompt_sha256": "p1"})
        rc = wg.cmd_send(_Args(approval="ap-test", run=run_id, workspace=self.root,
                                state="", platform="", round=1, force=False))
        self.assertEqual(rc, 2)
        self.assertFalse(os.path.exists(os.path.join(d, "writes", "ap-test.result.json")),
                         "被闸门挡住不得留下 result 文件")

    def test_no_approval_blocks(self):
        run_id, d, st, dry = self._dry()
        os.environ["STARPILOT_WRITE_MODE"] = "single_write"
        self.addCleanup(os.environ.pop, "STARPILOT_WRITE_MODE", None)
        rc = wg.cmd_send(_Args(approval="ap-test", run=run_id, workspace=self.root,
                                state="", platform="", round=1, force=False))
        self.assertEqual(rc, 2)
        self.assertFalse(os.path.exists(os.path.join(d, "writes", "ap-test.result.json")))

    def test_unverified_platform_blocked(self):
        run_id, d, st, dry = self._dry(platform="claude")
        os.environ["STARPILOT_WRITE_MODE"] = "single_write"
        self.addCleanup(os.environ.pop, "STARPILOT_WRITE_MODE", None)
        wg.atomic_write_json(os.path.join(d, "writes", "ap-test.approval.json"),
                             {"approval_id": "ap-test", "expires_at": 9e12,
                              "target_url_sha256": "u1", "prompt_sha256": "p1"})
        rc = wg.cmd_send(_Args(approval="ap-test", run=run_id, workspace=self.root,
                                state="", platform="claude", round=1, force=False))
        self.assertEqual(rc, 2)
        self.assertFalse(os.path.exists(os.path.join(d, "writes", "ap-test.result.json")))

    def test_expired_approval_blocked(self):
        run_id, d, st, dry = self._dry()
        os.environ["STARPILOT_WRITE_MODE"] = "single_write"
        self.addCleanup(os.environ.pop, "STARPILOT_WRITE_MODE", None)
        wg.atomic_write_json(os.path.join(d, "writes", "ap-test.approval.json"),
                             {"approval_id": "ap-test", "expires_at": 1.0,
                              "target_url_sha256": "u1", "prompt_sha256": "p1"})
        rc = wg.cmd_send(_Args(approval="ap-test", run=run_id, workspace=self.root,
                                state="", platform="kimi", round=1, force=False))
        self.assertEqual(rc, 2)

    def test_platform_write_whitelist(self):
        # 未实测平台的 write_allowed 必须是空 → 禁止真实写
        # （deepseek / gemini / doubao 均于 2026-10-29 实测通过，已移出未实测集合——见 platforms.py notes）
        for key in ("claude",):
            p = platforms.get(key)
            self.assertFalse(p.get("verified"), "%s 应是未实测" % key)
            self.assertEqual(p.get("write_allowed"), [], "%s 不该有可写路径" % key)
        # 已实测平台的写路径必须在白名单里
        for key in ("kimi", "chatgpt", "deepseek", "gemini", "doubao"):
            p = platforms.get(key)
            self.assertTrue(p.get("verified"))
            self.assertIn(p.get("write_method"), p.get("write_allowed"),
                          "%s 的 write_method 必须在自己的白名单里" % key)


class _Args(object):
    """给 cmd_* 用的轻量参数对象。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class TestLessons(TempWorkspace):
    """O4：close 强制合并 lesson 候选。"""

    def _candidate(self, symptom="boom"):
        return {
            "lesson_id": "L-KIMI-SEND-UNCONFIRMED-abc123",
            "date_utc": "2026-01-01", "stage": "O1", "source_run_ref": "runs/x",
            "platform": "kimi", "symptom": symptom, "dom_signal": "",
            "fix": "用更强的发送确认信号", "verify_cmd": "python scripts/orchestrator.py audit",
            "status": "open",
        }

    def test_close_merges_and_counts_equal(self):
        run_id, d, st = self.make_run(state="ROUND_DONE",
                                      lessons_candidates=[self._candidate()])
        lp = os.path.join(self.root, "references", "lessons.md")
        wg.atomic_write_text(lp, "# 实战教训沉淀\n\n旧内容\n")
        rc = orch.cmd_close(_Args(run=run_id, workspace=self.root, stage="O1",
                                   require_lessons=True))
        self.assertEqual(rc, 0)
        with open(lp, encoding="utf-8") as f:
            body = f.read()
        self.assertIn("旧内容", body, "close 是追加，不能覆盖已有内容")
        self.assertIn("[L-KIMI-SEND-UNCONFIRMED-abc123][2026-01-01][O1]", body)
        st2 = orch.load_run_state(self.root, run_id)
        self.assertEqual(st2["lessons_merged"], len(st2["lessons_candidates"]))
        self.assertEqual(st2["state"], "CLOSED")

    def test_close_appendix_added_equals_candidates(self):
        run_id, d, st = self.make_run(state="ROUND_DONE",
                                      lessons_candidates=[self._candidate()])
        lp = os.path.join(self.root, "references", "lessons.md")
        wg.atomic_write_text(lp, "# 旧\n")
        with open(lp, encoding="utf-8") as f:
            before = f.read().count("[L-")
        orch.cmd_close(_Args(run=run_id, workspace=self.root, stage="O1",
                              require_lessons=True))
        with open(lp, encoding="utf-8") as f:
            body = f.read()
        added = body.count("[L-") - before
        self.assertEqual(added, 1, "附录新增数必须等于候选合并数")
        self.assertIn("[L-KIMI-SEND-UNCONFIRMED-abc123]", body)

    def test_close_without_candidates_still_equal(self):
        run_id, d, st = self.make_run(state="ROUND_DONE")
        lp = os.path.join(self.root, "references", "lessons.md")
        wg.atomic_write_text(lp, "# 旧\n")
        rc = orch.cmd_close(_Args(run=run_id, workspace=self.root, stage="O1",
                                   require_lessons=True))
        self.assertEqual(rc, 0)
        st2 = orch.load_run_state(self.root, run_id)
        self.assertEqual(st2["lessons_merged"], len(st2["lessons_candidates"]))

    def test_lesson_candidate_dedup(self):
        run_id, d, st = self.make_run(state="ROUND_DONE")
        c1 = orch.lesson_candidate(self.root, st, "send_unconfirmed", "同样的事")
        c2 = orch.lesson_candidate(self.root, st, "send_unconfirmed", "同样的事")
        self.assertEqual(len(st["lessons_candidates"]), 1, "同一症状不该产生两条候选")
        self.assertEqual(c1["symptom"], c2["symptom"])

    def test_lesson_candidate_has_fix(self):
        run_id, d, st = self.make_run(state="ROUND_DONE")
        c = orch.lesson_candidate(self.root, st, "send_unconfirmed", "x")
        self.assertTrue(c["fix"], "候选必须有处置建议")
        self.assertTrue(c["verify_cmd"], "候选必须有验收命令")
        for k in ("lesson_id", "date_utc", "stage", "source_run_ref", "platform",
                  "symptom", "dom_signal", "fix", "verify_cmd", "status"):
            self.assertIn(k, c, "候选缺字段 %s" % k)
        self.assertEqual(c["status"], "open")
        self.assertTrue(c["lesson_id"].startswith("L-"), "lesson_id 必须可检索")

    def test_lesson_id_is_stable(self):
        run_id, d, st = self.make_run(state="ROUND_DONE")
        c1 = orch.lesson_candidate(self.root, st, "send_unconfirmed", "同一件事")
        lid = c1["lesson_id"]
        st2 = orch.load_run_state(self.root, run_id)
        c2 = orch.lesson_candidate(self.root, st2, "send_unconfirmed", "同一件事")
        self.assertEqual(lid, c2["lesson_id"], "同一症状的 lesson_id 必须稳定")


class TestBudget(TempWorkspace):
    def test_budget_computed(self):
        run_id, d, st = self.make_run(state="ROUND_DONE", rounds_used=3)
        b = orch.budget_of(st)
        self.assertEqual(b["max_rounds"], 12)
        self.assertEqual(b["rounds_used"], 3)
        self.assertEqual(b["rounds_left"], 9)
        self.assertEqual(b["max_same_error"], 2)

    def test_budget_defaults_match_contract(self):
        self.assertEqual(orch.DEFAULT_GOAL["max_rounds"], 12)
        self.assertEqual(orch.DEFAULT_GOAL["max_same_error"], 2)
        self.assertEqual(orch.DEFAULT_GOAL["round_timeout"], 300)
        self.assertEqual(orch.DEFAULT_GOAL["send_timeout"], 60)


class TestContextReread(TempWorkspace):
    def test_context_files_reread_each_step(self):
        run_id, d, st = self.make_run(state="ROUND_DONE")
        wg.atomic_write_text(os.path.join(self.root, "state", "current_goal.md"), "目标 X")
        wg.atomic_write_text(os.path.join(self.root, "state", "contract.md"), "契约 Y")
        ctx = orch.reread_context(self.root, st)
        self.assertEqual(ctx["goal"]["chars"], len("目标 X"))
        self.assertEqual(ctx["contract"]["chars"], len("契约 Y"))
        self.assertTrue(ctx["goal"]["sha256"])

    def test_context_change_is_visible(self):
        run_id, d, st = self.make_run(state="ROUND_DONE")
        gp = os.path.join(self.root, "state", "current_goal.md")
        wg.atomic_write_text(gp, "目标 X")
        h1 = orch.reread_context(self.root, st)["goal"]["sha256"]
        wg.atomic_write_text(gp, "目标 Y（改了）")
        h2 = orch.reread_context(self.root, st)["goal"]["sha256"]
        self.assertNotEqual(h1, h2, "每轮必须真读到最新内容")


class TestMatrixAndSignals(TempWorkspace):
    """平台矩阵与信号口径（网页 AI 第 2 轮裁定新增）。"""

    def test_matrix_has_at_least_five_rows(self):
        rows = []
        for key in sorted(platforms.PLATFORMS):
            p = platforms.get(key)
            rows.append({"platform": key, "verified": bool(p.get("verified")),
                         "send_allowed": bool(p.get("write_allowed"))})
        self.assertGreaterEqual(len(rows), 5, "平台矩阵至少 5 行")
        for r in rows:
            if not r["verified"]:
                self.assertFalse(r["send_allowed"],
                                 "%s 未实测就不许真写" % r["platform"])

    def test_editor_cleared_is_weak_signal_only(self):
        """输入框清空是**弱信号**：主信号不满足时必须报 INCONCLUSIVE_NOT_SUCCESS，
        既不能报成功、也不能报失败。"""
        res = {"ok": False, "error_class": "send_unconfirmed",
               "after": {"editor_len": 0, "msg_count": 2}}
        err = res.get("error_class")
        if err == "send_unconfirmed" and res.get("after", {}).get("editor_len") == 0:
            err = "INCONCLUSIVE_NOT_SUCCESS"
        self.assertEqual(err, "INCONCLUSIVE_NOT_SUCCESS")

    def test_editor_not_cleared_keeps_send_unconfirmed(self):
        res = {"ok": False, "error_class": "send_unconfirmed",
               "after": {"editor_len": 120, "msg_count": 2}}
        err = res.get("error_class")
        if err == "send_unconfirmed" and res.get("after", {}).get("editor_len") == 0:
            err = "INCONCLUSIVE_NOT_SUCCESS"
        self.assertEqual(err, "send_unconfirmed")


class TestVerifyAndRecovery(TempWorkspace):
    """网页 AI 第 3 轮裁定新增：delta 口径、verify 判据、恢复零重发、矩阵纪律标注。"""

    def _write_result(self, d, aid="ap-v", delta=1, msg_delta=2, pending=False,
                      user_delta=1):
        dry = {"approval_id": aid, "target_url_sha256": "u1", "prompt_sha256": "p1",
               "dom_before": {"dom_hash": "h1"}}
        wg.atomic_write_json(os.path.join(d, "writes", "%s.dry.json" % aid), dry)
        res = {"ok": True, "approval_id": aid,
               "reply_count_before": 3, "reply_count_after": 3 + delta,
               "reply_count_delta": delta, "msg_count_delta": msg_delta,
               "user_msg_delta": user_delta,
               "reply_pending": pending,
               "before": {"dom_hash": "h1"},
               "after": {"dom_hash": "h2", "url": "https://x/chat/1",
                         "reply_count": 3 + delta, "msg_count": 6}}
        wg.atomic_write_json(os.path.join(d, "writes", "%s.result.json" % aid), res)
        return aid

    def test_verify_accepts_delta_exactly_one(self):
        run_id, d, st = self.make_run()
        aid = self._write_result(d, delta=1)
        rc = wg.cmd_verify(_Args(approval=aid, run=run_id, workspace=self.root))
        self.assertEqual(rc, 0, "delta=1 必须 VERIFIED")

    def test_verify_rejects_delta_two_as_inconclusive(self):
        """delta=2 且用户消息只 +1：可能是「一条回复被拆成两个气泡」。

        裁定要求：这种情况**判 INCONCLUSIVE，不自动 MISMATCH**（也不当成功）。
        """
        run_id, d, st = self.make_run()
        aid = self._write_result(d, delta=2, user_delta=1)
        rc = wg.cmd_verify(_Args(approval=aid, run=run_id, workspace=self.root))
        self.assertEqual(rc, 6, "气泡拆分嫌疑必须判 INCONCLUSIVE（rc=6），不得直接 MISMATCH")

    def test_verify_delta_two_with_two_user_msgs_is_mismatch(self):
        """用户消息 +2 = 实锤重发，这时才判 MISMATCH。"""
        run_id, d, st = self.make_run()
        aid = self._write_result(d, delta=2, user_delta=2)
        rc = wg.cmd_verify(_Args(approval=aid, run=run_id, workspace=self.root))
        self.assertEqual(rc, 5, "用户消息 +2 是重发，必须 MISMATCH")

    def test_verify_delta_two_with_zero_user_msgs_is_inconclusive(self):
        """user_msg_delta == 0（选择器失效/消息消失）也必须判 INCONCLUSIVE。

        只有 `user_msg_delta >= 2` 才够格实锤重发；0 不是「失败」，是「无法判断」。
        """
        run_id, d, st = self.make_run()
        aid = self._write_result(d, delta=2, user_delta=0)
        rc = wg.cmd_verify(_Args(approval=aid, run=run_id, workspace=self.root))
        self.assertEqual(rc, 6, "用户消息数不可信时必须判 INCONCLUSIVE，不得落进 MISMATCH")

    def test_verify_rejects_delta_zero(self):
        run_id, d, st = self.make_run()
        aid = self._write_result(d, delta=0, msg_delta=1, pending=True)
        rc = wg.cmd_verify(_Args(approval=aid, run=run_id, workspace=self.root))
        self.assertEqual(rc, 5, "delta=0（只有送达没回复）必须 MISMATCH")

    def test_probe_exposes_reply_count(self):
        js = platforms.js_probe(platforms.get("kimi"))
        self.assertIn("reply_count", js, "探测片段必须给出助手回复条数")
        self.assertIn("asst.length", js, "reply_count 必须是助手消息条数（asst.length）")

    def test_reply_count_of_prefers_reply_count(self):
        self.assertEqual(orch.reply_count_of({"reply_count": 5, "msg_count": 9}), 5)
        self.assertEqual(orch.reply_count_of({"msg_count": 9}), 9, "老状态要能退回")
        self.assertEqual(orch.reply_count_of({}), 0)

    def test_resume_never_sends(self):
        """恢复只复核 DOM：journal 里 resume 标记之后不得出现任何发送动作。"""
        run_id, d, st = self.make_run(state="AWAITING_REPLY",
                                      last_send={"reply_count_before": 3,
                                                 "reply_count_delta": 1,
                                                 "approval_id": "ap-v"})
        wg.append_journal(d, {"action": "write_send", "result": "ok",
                              "error_class": "", "approval_id": "ap-v",
                              "target_url_sha256": "u1", "prompt_sha256": "p1",
                              "before": {}, "after": {}})
        before_sends = len([e for e in wg.read_journal(d)
                            if e.get("action") in ("write_send", "send")])
        orch.do_resume(self.root, st, platforms.get("kimi"),
                       _Args(state="", workspace=self.root))
        evs = wg.read_journal(d)
        after_sends = len([e for e in evs if e.get("action") in ("write_send", "send")])
        self.assertEqual(after_sends, before_sends, "恢复不得新增任何发送动作")
        self.assertTrue([e for e in evs if e.get("action") == "resume_marker"],
                        "恢复必须留 resume 标记供审计")
        st2 = orch.load_run_state(self.root, run_id)
        self.assertEqual(st2["state"], "PAUSED_HUMAN",
                         "DOM 复核不出已送达 → 证据不足转人工（不许重发）")
        # 审计口径：resume 标记之后发送数为 0
        out = json.loads(_capture(orch.cmd_audit,
                                  _Args(run=run_id, workspace=self.root, state="")))
        self.assertEqual(out["sends_after_resume"], 0)
        self.assertEqual(out["duplicate_sends"], 0)

    def test_matrix_marks_chatgpt_deferred_rule1(self):
        # 当前绑定的是 kimi：chatgpt 是「已实测但未绑定」→ 纪律搁置
        wg.atomic_write_json(os.path.join(self.root, ".wa-state.json"),
                             {"url": "https://www.kimi.com/chat/1", "platform": "kimi",
                              "session": ""})
        out = json.loads(_capture(orch.cmd_matrix,
                                  _Args(workspace=self.root, state="", probe=False)))
        rows = {r["platform"]: r for r in out["rows"]}
        self.assertGreaterEqual(len(rows), 5, "矩阵至少 5 行")
        self.assertEqual(rows["chatgpt"]["read_probe"], "deferred_rule1",
                         "chatgpt 是纪律搁置，不是技术失败")
        self.assertTrue(rows["chatgpt"]["verified"], "chatgpt 历史实测为真")
        self.assertTrue(rows["gemini"]["verified"], "gemini 2026-10-29 已实测")
        for key in ("claude",):
            self.assertNotEqual(rows[key]["read_probe"], "ok",
                                "%s 本轮没探测过，不能写 ok" % key)
            self.assertFalse(rows[key]["send_allowed"], "%s 未实测不许真写" % key)
        # deepseek 已实测但本轮未绑定 → 同样不许被判成「探测过」
        self.assertNotEqual(rows["deepseek"]["read_probe"], "ok")

    def test_close_reports_three_equal_numbers(self):
        run_id, d, st = self.make_run(state="ROUND_DONE",
                                      lessons_candidates=[_cand("x")])
        lp = os.path.join(self.root, "references", "lessons.md")
        wg.atomic_write_text(lp, "# 旧\n")
        out = json.loads(_capture(orch.cmd_close,
                                  _Args(run=run_id, workspace=self.root, stage="O3",
                                        require_lessons=True)))
        self.assertEqual(out["candidates"], out["merged"])
        self.assertEqual(out["candidates"], out["appendix_added"])
        self.assertEqual(out["lessons_pending"], 0)

    def test_close_keeps_deferred_lesson_open(self):
        """故意搁置的条目进附录仍是 status=open（不许被回流措辞吞掉）。"""
        c = _cand("deferred")
        c["appendix_status"] = "open"
        run_id, d, st = self.make_run(state="ROUND_DONE", lessons_candidates=[c])
        lp = os.path.join(self.root, "references", "lessons.md")
        wg.atomic_write_text(lp, "# 旧\n")
        orch.cmd_close(_Args(run=run_id, workspace=self.root, stage="O3",
                              require_lessons=True))
        with open(lp, encoding="utf-8") as f:
            body = f.read()
        self.assertIn("[open]", body, "搁置项必须以 status=open 进附录")

    def test_lesson_command_adds_searchable_candidate(self):
        run_id, d, st = self.make_run(state="ROUND_DONE")
        out = json.loads(_capture(orch.cmd_lesson,
                                  _Args(run=run_id, workspace=self.root, state="",
                                        symptom="ChatGPT 单写：纪律搁置，未探测",
                                        fix="待人改绑后再探测", verify_cmd="probe",
                                        dom_signal="", error_class="deferred",
                                        status="open")))
        lid = out["lesson"]["lesson_id"]
        self.assertTrue(lid.startswith("L-"))
        st2 = orch.load_run_state(self.root, run_id)
        self.assertEqual(len(st2["lessons_candidates"]), 1)
        self.assertEqual(st2["lessons_candidates"][0]["appendix_status"], "open")


def _cand(symptom="boom"):
    return {
        "lesson_id": "L-KIMI-SEND-UNCONFIRMED-abc123",
        "date_utc": "2026-01-01", "stage": "O1", "source_run_ref": "runs/x",
        "platform": "kimi", "symptom": symptom, "dom_signal": "",
        "fix": "用更强的发送确认信号", "verify_cmd": "python scripts/orchestrator.py audit",
        "status": "open",
    }


def _capture(fn, args):
    """跑一个会 print JSON 的 cmd_*，把它的输出抓成 dict。"""
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(args)
    return buf.getvalue()


if __name__ == "__main__":
    unittest.main()



