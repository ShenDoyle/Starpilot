#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ExecutionGate 组（PROJECT-03-R1 任务书 §7 组 D）+ gate_lint 静态检查验收。

组 D：没有锁/状态不明/ownership 未核验/页面身份过期/observation 失效/
fencing 不匹配 → 不得执行；outcome 落盘前不得释放锁；锁恢复后不自动重放。
gate_lint：业务模块绕过 Gate 直调后端必须被静态检出。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import statelock  # noqa: E402
import pageidentity  # noqa: E402
import actionledger  # noqa: E402
import execution_gate  # noqa: E402
import gate_lint  # noqa: E402
import procidentity  # noqa: E402
import sp_errors as SP  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # TESTS_DIR
import p2_plane  # noqa: E402

IDENT = {"url": "data:x", "title": "T", "marker": "M", "timeOrigin": 1.0}


class GateHarness:
    """标准夹具：gate + ledger + page identity + 探针（显式标注测试桩）。"""

    def __init__(self, d):
        self.d = d
        self.lock_path = os.path.join(d, "gate.lock")
        self.ledger = actionledger.ActionLedger(os.path.join(d, "ledger.sqlite"))
        self.pi = pageidentity.PageIdentityManager(os.path.join(d, "pages.json"))
        self.ownership = mock.Mock(return_value="agent")
        self.gate = execution_gate.ExecutionGate(
            self.lock_path, self.ledger, self.pi,
            ownership_probe=self.ownership, resource="taskspace-32",
            plane=p2_plane.make_plane(d))

    def new_ctx(self, page_key="ts32|p1", bind=True, fencing=None, executor=None):
        if bind:
            b = self.pi.bind(page_key, IDENT, executor or procidentity.executor_id(),
                             fencing if fencing is not None else 0, "bind")
        exec_id = executor or procidentity.executor_id()
        ac = self.gate.acquire({"executor_id": exec_id, "run_id": "r1"})
        gen = ac["data"]["fencing_generation"]
        if bind:
            # 绑定要挂当前 fencing 才能通过 verify
            self.pi.bind(page_key, IDENT, exec_id, gen, "bind")
        ctx = {"executor_id": exec_id, "run_id": "r1",
               "fencing_generation": gen,
               "page_generation": self.pi.status(page_key).get("page_generation", "")}
        return ctx, ac


class TestExecutionGate(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="sp-gate-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.h = GateHarness(self.d)

    def tearDown(self):
        self.h.ledger.close()

    def _intent_and_dispatch(self, backend, ctx, task_id="t1",
                             intent=None, page_key="ts32|p1"):
        intent = intent or {"action": "send", "text": "hi"}
        r = self.h.ledger.reserve(task_id, intent, budget_units=10,
                                  executor_id=ctx["executor_id"],
                                  business_target="test:gate-%s" % task_id)
        aid = r["data"]["action_id"]
        return self.h.gate.dispatch(
            ctx, "send", backend, {"text": "hi"},
            taskspace_id=32, page_key=page_key, identity_tuple=IDENT,
            action_id=aid), aid

    # ---- D1：没有锁不得执行 ----
    def test_no_lock_no_dispatch(self):
        ctx, ac = self.h.new_ctx()
        self.assertTrue(ac["ok"])
        # 模拟锁被夺走：直接删锁文件 → fencing 复核失败
        statelock.release_named(self.h.lock_path, ac["data"]["lease"])
        called = mock.Mock()
        r, aid = self._intent_and_dispatch(lambda *a: called(*a) or {}, ctx)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_GATE_007)
        called.assert_not_called()

    # ---- D2：锁状态不明不得执行 ----
    def test_unknown_lock_state_no_dispatch(self):
        ctx, ac = self.h.new_ctx()
        statelock.release_named(self.h.lock_path, ac["data"]["lease"])
        # 写一个损坏锁 → acquire/verify 路径 fail-closed
        with open(self.h.lock_path, "w", encoding="utf-8") as f:
            f.write("{{{broken")
        r = self.h.gate.acquire({"executor_id": ctx["executor_id"]})
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_GATE_005)

    # ---- D3：ownership 未核验不得执行 ----
    def test_ownership_unverified_blocks(self):
        self.h.ownership = mock.Mock(return_value="user")
        self.h.gate.ownership_probe = self.h.ownership
        ctx, _ = self.h.new_ctx()
        r, _ = self._intent_and_dispatch(lambda *a: {}, ctx)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_GATE_008)
        self.h.ownership.assert_called()

    def test_ownership_probe_missing_fail_closed(self):
        self.h.gate.ownership_probe = None
        ctx, _ = self.h.new_ctx()
        r, _ = self._intent_and_dispatch(lambda *a: {}, ctx)
        self.assertEqual(r["code"], SP.SP_GATE_008)

    # ---- D4：页面身份过期不得执行 ----
    def test_page_identity_stale_blocks(self):
        ctx, _ = self.h.new_ctx()
        # 页面事实已变化（marker 变）→ verify 拒绝
        r, _ = self._intent_and_dispatch(lambda *a: {}, ctx,
                                         intent={"action": "send", "text": "hi"})
        self.assertTrue(r["ok"], "未变化时应通过")
        # 变化后
        r2 = self.h.ledger.reserve("t2", {"action": "send", "text": "hi2"},
                                   budget_units=10,
                                   business_target="test:gate-t2-b")
        aid2 = r2["data"]["action_id"]
        rr = self.h.gate.dispatch(ctx, "send", lambda *a: {"ok": True},
                                  {}, taskspace_id=32, page_key="ts32|p1",
                                  identity_tuple=dict(IDENT, marker="CHANGED"),
                                  action_id=aid2)
        self.assertFalse(rr["ok"])
        self.assertEqual(rr["code"], SP.SP_GATE_009)

    # ---- D5：observation 失效不得执行 ----
    def test_observation_invalid_blocks(self):
        self.h.gate.observation_verify = lambda obs: SP.result(
            False, SP.SP_GATE_010, {}, "observation expired")
        ctx, _ = self.h.new_ctx()
        r, _ = self._intent_and_dispatch(lambda *a: {}, ctx)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_GATE_010)

    # ---- D6：fencing 不匹配不得执行（旧 generation 拒绝）----
    def test_stale_fencing_blocked(self):
        ctx, ac = self.h.new_ctx()
        old_ctx = dict(ctx, fencing_generation=ctx["fencing_generation"] - 1)
        r, _ = self._intent_and_dispatch(lambda *a: {}, old_ctx)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_GATE_007)

    # ---- D7：outcome 落盘前不得释放锁（时序断言）----
    def test_lock_held_until_outcome_committed(self):
        ctx, ac = self.h.new_ctx()
        r, aid = self._intent_and_dispatch(lambda *a: {"ok": True, "result": "posted"}, ctx)
        self.assertTrue(r["ok"])
        # dispatch 成功但未 commit → 锁必须在
        cur = statelock.read_holder_named(self.h.lock_path)
        self.assertIsNotNone(cur.get("lock_id"), "outcome 落盘前锁必须还在")
        c = self.h.gate.commit(ctx, aid, "SENT", evidence="page evidence")
        self.assertTrue(c["ok"])
        # commit 后才允许 release
        self.h.gate.release(ac["data"]["lease"])
        self.assertEqual(statelock.read_holder_named(self.h.lock_path), {})

    # ---- D8：锁恢复后不得自动重放 UNKNOWN ----
    def test_no_auto_replay_after_recovery(self):
        ctx, ac = self.h.new_ctx()
        r, aid = self._intent_and_dispatch(
            lambda *a: (_ for _ in ()).throw(RuntimeError("crash mid-dispatch")), ctx)
        self.assertEqual(r["code"], SP.SP_BE_004)
        row = self.h.ledger.get(aid)
        self.assertEqual(row["state"], "SEND_UNKNOWN")
        self.assertEqual(row["resend_count"], 0)
        # 新执行者接管锁后，重放同 action 仍被 anti-replay 拒绝
        statelock.release_named(self.h.lock_path, ac["data"]["lease"])
        ctx2, ac2 = self.h.new_ctx(executor="ex-new-runner")
        called2 = mock.Mock()
        r2, _ = self._intent_and_dispatch(
            lambda *a: called2(*a) or {"ok": True}, ctx2)
        self.assertFalse(r2["ok"])
        called2.assert_not_called()   # 绝不自动重放/重派
        # P2-2-4：Marker 已存在 → 屏障在更早处即拒（SP-OP-001 保守拒绝）；
        # 未配置屏障的旧路径仍由 anti-replay 状态机拒（SP-ACT-003）。
        self.assertIn(r2["code"], (SP.SP_ACT_003, SP.SP_OP_001))

    # ---- D9：RESULT_UNKNOWN 不得推断 SENT ----
    def test_result_unknown_becomes_send_unknown(self):
        ctx, ac = self.h.new_ctx()
        r, aid = self._intent_and_dispatch(lambda *a: {"ok": False, "unknown": True}, ctx)
        self.assertEqual(r["code"], SP.SP_BE_004)
        self.assertEqual(self.h.ledger.get(aid)["state"], "SEND_UNKNOWN")


class TestGateLint(unittest.TestCase):
    def test_backend_import_in_business_module_is_violation(self):
        import tempfile as tf
        d = tf.mkdtemp(prefix="sp-lint-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        biz = os.path.join(d, "some_business.py")
        with open(biz, "w", encoding="utf-8") as f:
            f.write("import ego_backend\n\ndef go():\n    return ego_backend.call()\n")
        v = gate_lint.lint_files([biz])
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0]["module"], "ego_backend")

    def test_from_import_variant_caught(self):
        import tempfile as tf
        d = tf.mkdtemp(prefix="sp-lint-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        biz = os.path.join(d, "biz2.py")
        with open(biz, "w", encoding="utf-8") as f:
            f.write("from browser_backend import call\n")
        v = gate_lint.lint_files([biz])
        self.assertEqual(len(v), 1)

    def test_execution_gate_allowlisted(self):
        import tempfile as tf
        d = tf.mkdtemp(prefix="sp-lint-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        biz = os.path.join(d, "execution_gate.py")
        with open(biz, "w", encoding="utf-8") as f:
            f.write("import ego_backend\n")
        self.assertEqual(gate_lint.lint_files([biz]), [])

    def test_real_scripts_tree_is_clean(self):
        scripts = os.path.join(ROOT, "scripts")
        self.assertEqual(gate_lint.lint_files([scripts]), [],
                         "现行 scripts/ 不得有绕过 Gate 的后端直调")

    def test_cli_exit_code(self):
        rc = gate_lint.main(["gate_lint", os.path.join(ROOT, "scripts")])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
