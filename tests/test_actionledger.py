#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Action Ledger 组（PROJECT-03-R1 任务书 §7 组 C）：幂等键分离与崩溃恢复验收。"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import actionledger  # noqa: E402
import sp_errors as SP  # noqa: E402

INTENT = {"action": "send_message", "target": "chatgpt", "text": "hello"}


class TestActionLedger(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="sp-ledger-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.db = os.path.join(self.d, "ledger.sqlite")
        self.L = actionledger.ActionLedger(self.db)

    def tearDown(self):
        self.L.close()

    # ---- C1：同 (task, intent, seq) 重复提交 → ACTION_DUPLICATE ----
    def test_same_action_id_duplicate(self):
        r1 = self.L.reserve("t1", INTENT, seq=0, budget_units=10)
        self.assertTrue(r1["ok"])
        r2 = self.L.reserve("t1", INTENT, seq=0, budget_units=10)
        self.assertFalse(r2["ok"])
        self.assertEqual(r2["code"], SP.SP_ACT_001)

    # ---- C2：同 intent 跨 run / 新 seq → INTENT_COLLISION ----
    def test_same_intent_different_seq_collides(self):
        self.L.reserve("t1", INTENT, seq=0, budget_units=10)
        r = self.L.reserve("t1", INTENT, seq=1, budget_units=10)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_ACT_002)

    # ---- C3：新 task_id 绕过 UNKNOWN → 拒绝（终审重点 3 核心）----
    def test_new_task_id_cannot_bypass_unknown(self):
        r = self.L.reserve("task-A", INTENT, budget_units=10)
        aid = r["data"]["action_id"]
        self.L.mark_dispatched(aid, 5, "ex-1")
        self.L.recover_unknown()  # 崩溃恢复 → SEND_UNKNOWN
        # 新 run 同 task
        r2 = self.L.reserve("task-A", INTENT, seq=9, budget_units=10)
        self.assertFalse(r2["ok"])
        self.assertIn(r2["code"], (SP.SP_ACT_002, SP.SP_ACT_003))
        # 新 task_id 绕过
        r3 = self.L.reserve("task-B-fresh", INTENT, budget_units=10)
        self.assertFalse(r3["ok"])
        self.assertEqual(r3["code"], SP.SP_ACT_002)
        # locked 状态可见
        locked = self.L.locked_intents()
        self.assertEqual(len(locked), 1)
        self.assertEqual(locked[0]["action_id"], aid)

    # ---- C4：并发预占原子性（同 intent 两次 reserve 只成一个）----
    def test_reserve_atomic_unique(self):
        r1 = self.L.reserve("t1", INTENT, budget_units=10)
        self.assertTrue(r1["ok"])
        # 直接再插同一 intent_key 的另一个 action（不同 task，同一意图载荷）
        r2 = self.L.reserve("t2-different-task", dict(INTENT), budget_units=10)
        self.assertFalse(r2["ok"])
        self.assertEqual(r2["code"], SP.SP_ACT_002)

    def test_budget_exhaustion(self):
        self.L.reserve("t1", dict(INTENT), budget_units=1)
        r = self.L.reserve("t1", dict(INTENT, order_no="2"), budget_units=1)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_ACT_004)

    # ---- C5：dispatch 前崩溃 → NOT_SENT 可恢复（唯一合法路径）----
    def test_crash_before_dispatch_can_be_marked_not_sent(self):
        r = self.L.reserve("t1", INTENT, budget_units=10)
        aid = r["data"]["action_id"]
        c = self.L.commit_outcome(aid, "NOT_SENT", "crashed before dispatch, nothing sent")
        self.assertTrue(c["ok"])
        self.assertEqual(c["data"]["result_tag"], "NOT_SENT")

    # ---- C6：dispatch 后 outcome 前崩溃 → SEND_UNKNOWN + anti_replay + resend=0 ----
    def test_crash_after_dispatch_before_outcome(self):
        r = self.L.reserve("t1", INTENT, budget_units=10)
        aid = r["data"]["action_id"]
        d = self.L.mark_dispatched(aid, 5, "ex-1")
        self.assertTrue(d["ok"])
        rec = self.L.recover_unknown()
        self.assertEqual(rec["data"]["count"], 1)
        row = self.L.get(aid)
        self.assertEqual(row["state"], "SEND_UNKNOWN")
        self.assertEqual(row["anti_replay_locked"], 1)
        self.assertEqual(row["resend_count"], 0, "自动重发恒 0（INV-8）")
        self.assertEqual(row["dispatch_count"], 1)

    # ---- C7：UNKNOWN 期间重发被拒；任何 unlock 都被拒（G6-HOTFIX）----
    def test_unknown_refuses_resend_and_unsupported_commit(self):
        r = self.L.reserve("t1", INTENT, budget_units=10)
        aid = r["data"]["action_id"]
        self.L.mark_dispatched(aid, 5, "ex-1")
        self.L.recover_unknown()
        # 再次 mark_dispatched（= 重发尝试）→ ANTI_REPLAY_LOCKED
        d2 = self.L.mark_dispatched(aid, 6, "ex-2")
        self.assertFalse(d2["ok"])
        self.assertEqual(d2["code"], SP.SP_ACT_003)
        # 无证据 commit 被拒
        c = self.L.commit_outcome(aid, "SENT")
        self.assertFalse(c["ok"])
        self.assertEqual(c["code"], SP.SP_ACT_003)
        # G6-HOTFIX（2026-11-13）：带人工解锁也不再允许。
        # 旧实现只校验 unlock["kind"] 非空 → 任意 {"kind":"x"} 即可释放 L-biz，
        # 该兼容分支已移除；当前无可信裁决源，一律 fail-closed。
        c2 = self.L.commit_outcome(aid, "SENT",
                                   unlock={"kind": "manual", "operator": "user-fan",
                                           "reason": "page shows message delivered"})
        self.assertFalse(c2["ok"])
        self.assertEqual(c2["code"], SP.SP_ACT_003)
        self.assertEqual(c2["data"]["reason"], "unknown_unlock_disabled")
        # 状态未被推进、锁仍占用（H6：任何失败不得释放 L-biz）
        row = self.L.get(aid)
        self.assertEqual(row["state"], "SEND_UNKNOWN")
        self.assertEqual(row["anti_replay_locked"], 1)
        # 该 use 未传 business_target → L-biz 不参与；此处改断言 L-int 仍占用
        self.assertEqual(len(self.L.locked_intents()), 1)

    # ---- C8：NOT_SENT 禁止在 dispatch 之后（四条禁止推断的接口级强制）----
    def test_not_sent_forbidden_after_dispatch(self):
        r = self.L.reserve("t1", INTENT, budget_units=10)
        aid = r["data"]["action_id"]
        self.L.mark_dispatched(aid, 5, "ex-1")
        # SP1-A：账本已记录 fencing=5 → 必须声明 expected_fencing（否则 SP-ACT-011）
        c = self.L.commit_outcome(aid, "NOT_SENT", "local exception",
                                  expected_fencing=5)
        self.assertFalse(c["ok"])
        self.assertEqual(c["code"], SP.SP_ACT_007)

    # ---- C9：intent_key 规范化 —— 相似文本是不同意图；键序不绕过 ----
    def test_intent_normalization_rules(self):
        self.L.reserve("t1", dict(INTENT), budget_units=10)
        same = self.L.reserve("t1", {"target": "chatgpt", "text": "hello",
                                     "action": "send_message"}, budget_units=10)
        self.assertFalse(same["ok"], "键序不同但语义相同 → 仍命中")
        different = self.L.reserve("t1", dict(INTENT, text="hello 2"),
                                   budget_units=10)
        self.assertTrue(different["ok"], "相似但不同文本是不同意图")

    # ---- C10：跨实例持久化（SQLite 文件）----
    def test_persistence_across_instances(self):
        r = self.L.reserve("t1", INTENT, budget_units=10)
        aid = r["data"]["action_id"]
        self.L.close()
        L2 = actionledger.ActionLedger(self.db)
        try:
            row = L2.get(aid)
            self.assertEqual(row["state"], "RESERVED")
            self.assertEqual(row["intent_key"], r["data"]["intent_key"])
        finally:
            L2.close()


if __name__ == "__main__":
    unittest.main()
