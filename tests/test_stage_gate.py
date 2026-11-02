#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段管理防绕过单测（2026-11-02 GPT 审计 P0-1 + D01/D02）。

GPT 裁定原文：agent 不能仅凭自行申报绕过大任务拆阶段规则；收口≠验收通过。
本文件盯四件事：
  D01 复杂任务伪装成简单任务 → plan 拒 / audit 出 undeclared_complexity；
  D01b 申报超阈值但 plan 无阶段表 → 拒绝进 PLAN_OK；
  D02 上一阶段未 accepted 就开下一阶段 → 拒绝；
  close --stage 只到 submitted（accepted 必须 web_ai/user 带证据）。
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import orchestrator as orch  # noqa: E402
import write_gate as wg  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="starpilot-stage-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.makedirs(os.path.join(self.root, "state"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "references"), exist_ok=True)
        with open(os.path.join(self.root, "references", "lessons.md"), "w",
                  encoding="utf-8") as f:
            f.write("# lessons\n")

    def make_run(self, **extra):
        run_id = "20260101T000000-stage"
        d = os.path.join(self.root, "runs", run_id)
        for sub in ("evidence", "writes"):
            os.makedirs(os.path.join(d, sub), exist_ok=True)
        st = {"run_id": run_id, "stage_id": "", "state": "BOUND", "round": 0,
              "pending": {}, "last_send": {}, "lessons_candidates": [],
              "lessons_merged": 0, "closed": False, "stages": []}
        st.update(extra)
        wg.atomic_write_json(os.path.join(d, "state.json"), st)
        wg.set_pointer(self.root, run_id)
        return run_id, d, st

    def run_cmd(self, func, **kw):
        a = argparse.Namespace(workspace=self.root, **kw)
        return func(a)


class TestD01UndeclaredComplexity(Base):
    def test_audit_flags_small_declaration_big_reality(self):
        """D01：申报 3 轮/1 交付物，实际 10 轮/3 文件/3 模块且无阶段表 → FAIL 异常。"""
        rid, d, st = self.make_run(
            round=10,
            task_scope={"rounds": 3, "deliverables": 1, "modules": 1,
                        "declared_at": "2026-11-02T12:00:00", "declared_by": "agent"})
        for p in ("a/1.md", "b/2.md", "c/3.md"):
            wg.append_journal(d, {"ts": "2026-11-02T12:01:00", "action": "write_send",
                                  "result": "ok", "path": p})
        a = argparse.Namespace(workspace=self.root, run=rid, trace=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = orch.cmd_audit(a)
        out = json.loads(buf.getvalue())
        self.assertEqual(rc, 5)
        self.assertEqual(out["verdict"], "FAIL")
        self.assertTrue(any(x.startswith("undeclared_complexity")
                            for x in out["scope_anomalies"]),
                        out["scope_anomalies"])
        self.assertGreaterEqual(out["actual_scope"]["deliverables"], 3)

    def test_plan_refused_when_scope_exceeds_without_stage_table(self):
        """D01b：scope 超阈值 + plan 无阶段表 → 拒绝进 PLAN_OK。"""
        self.make_run(task_scope={"rounds": 9, "deliverables": 4, "modules": 3})
        pf = os.path.join(self.root, "plan.md")
        with open(pf, "w", encoding="utf-8") as f:
            f.write("# 计划\n\n一口气做完。\n")
        rc = self.run_cmd(orch.cmd_plan, run=None, file=pf)
        self.assertEqual(rc, 5)

    def test_plan_imports_stage_headers(self):
        self.make_run(task_scope={"rounds": 9, "deliverables": 4, "modules": 3})
        pf = os.path.join(self.root, "plan.md")
        with open(pf, "w", encoding="utf-8") as f:
            f.write("# 计划\n\n## 阶段 S1：抽取\n- 交付：x\n\n## 阶段 S2：实现\n")
        rc = self.run_cmd(orch.cmd_plan, run=None, file=pf)
        self.assertEqual(rc, 0)
        st = orch.load_run_state(self.root, wg.read_pointer(self.root))
        self.assertEqual([s["id"] for s in st["stages"]], ["S1", "S2"])
        self.assertTrue(all(s["status"] == "pending" for s in st["stages"]))

    def test_scope_revision_requires_reason(self):
        rid, d, st = self.make_run(task_scope={"rounds": 3, "deliverables": 1,
                                               "modules": 1})
        rc = self.run_cmd(orch.cmd_scope, run=rid, rounds=1, deliverables=None,
                          modules=None, revise_reason="")
        self.assertEqual(rc, 5)  # 静默改小 = 拒
        rc = self.run_cmd(orch.cmd_scope, run=rid, rounds=2, deliverables=None,
                          modules=None, revise_reason="范围确实缩小：需求删减")
        self.assertEqual(rc, 0)
        evs = wg.read_journal(d)
        self.assertTrue(any(e.get("action") == "scope_revised" for e in evs))


class TestD02StageOrder(Base):
    def test_cannot_start_next_before_accept(self):
        """D02：S1 submitted 未 accepted，start S2 → 拒绝。"""
        rid, d, st = self.make_run(stages=[
            {"id": "S1", "status": "submitted", "goal": ""},
            {"id": "S2", "status": "pending", "goal": ""}])
        rc = self.run_cmd(orch.cmd_stage, run=rid, stage_action="start",
                          stage_id="S2", goal="", by="", evidence="", list=False)
        self.assertEqual(rc, 5)

    def test_accept_then_start_ok(self):
        rid, d, st = self.make_run(stages=[
            {"id": "S1", "status": "submitted", "goal": ""},
            {"id": "S2", "status": "pending", "goal": ""}])
        rc = self.run_cmd(orch.cmd_stage, run=rid, stage_action="accept",
                          stage_id="S1", goal="", by="web_ai",
                          evidence="阶段报告 + 测试全绿，WebAI 确认", list=False)
        self.assertEqual(rc, 0)
        rc = self.run_cmd(orch.cmd_stage, run=rid, stage_action="start",
                          stage_id="S2", goal="", by="", evidence="", list=False)
        self.assertEqual(rc, 0)
        st = orch.load_run_state(self.root, rid)
        self.assertEqual(st["stages"][1]["status"], "executing")
        self.assertEqual(st["stage_id"], "S2")

    def test_accept_needs_evidence_and_non_agent(self):
        for by, ev, expect in (("agent", "x", 5), ("web_ai", "", 5),
                               ("web_ai", "证据", 0)):
            rid, d, st = self.make_run(stages=[{"id": "S1", "status": "submitted"}])
            rc = self.run_cmd(orch.cmd_stage, run=rid, stage_action="accept",
                              stage_id="S1", goal="", by=by, evidence=ev, list=False)
            self.assertEqual(rc, expect, "by=%s ev=%r" % (by, ev))

    def test_close_marks_submitted_not_accepted(self):
        """收口≠验收：close 后状态必须是 submitted。"""
        rid, d, st = self.make_run(stages=[{"id": "S1", "status": "executing"}])
        rc = self.run_cmd(orch.cmd_close, run=rid, stage="S1",
                          require_lessons=False)
        self.assertEqual(rc, 0)
        st = orch.load_run_state(self.root, rid)
        self.assertEqual(st["stages"][0]["status"], "submitted")
        self.assertNotEqual(st["stages"][0]["status"], "accepted")


if __name__ == "__main__":
    unittest.main()
