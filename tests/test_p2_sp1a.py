#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SP1-A 专项验收：账本层 fencing 下沉 + takeover_fencing（PROJECT-04）。

背景（P2-3-0 调查 C0）：`commit_outcome` 原本不接收 fencing，所有权校验只在
`ExecutionGate.commit()` 应用层做；任何直连账本的入口都能绕过。SP1-A 把校验
下沉到账本，并新增显式 `takeover_fencing()`（CAS + 状态白名单 + 不推进状态）。

GPT 对 SP1-A 的禁止项（本文件逐条守住）：
- 不降低三锁约束：`test_no_lock_weakening`
- 不改变 Envelope / Node 验签语义：静态断言（文件未触碰）
- 不引入 UNKNOWN 自动释放：`test_unknown_still_rejects_even_with_valid_fencing`
- 不实现 SP2 生产代码：`test_no_sp2_symbols_in_ledger`
- 不修改三份保护文件：静态断言

注：原「不实现 SP3」断言已被 SP3-1 正式授权变更取代（裁决协议上线）。
现由三条更强断言接管：SP3 入口收敛到 3 个（`test_sp3_symbols_confined_to
_three_entrypoints`）、commit_outcome 不得读裁决表或调裁决 API
（`test_commit_outcome_never_reads_adjudications`）。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import actionledger  # noqa: E402
import sp_errors as SP  # noqa: E402


class Sp1aBase(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="sp-sp1a-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.db = os.path.join(self.d, "ledger.sqlite")
        self.L = actionledger.ActionLedger(self.db)

    def tearDown(self):
        self.L.close()

    @staticmethod
    def intent(tag):
        return {"action": "send_message", "target": tag, "text": "hi"}

    def reserved(self, tag, fencing=None, task=None):
        r = self.L.reserve(task or ("t-" + tag), self.intent(tag), budget_units=10,
                           business_target=tag, fencing_generation=fencing)
        self.assertTrue(r["ok"], r)
        return r["data"]["action_id"]

    def dispatched(self, tag, fencing=7):
        aid = self.reserved(tag, fencing=None)
        d = self.L.mark_dispatched(aid, fencing, "exec-a")
        self.assertTrue(d["ok"], d)
        return aid, fencing


# ================================================ A 组：commit_outcome 所有权校验
class TestOwnerCheck(Sp1aBase):
    def test_a1_ledger_has_fencing_caller_silent_rejected(self):
        aid, f = self.dispatched("a1", 7)
        r = self.L.commit_outcome(aid, "SENT", "e")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_ACT_011)
        self.assertEqual(r["data"]["locked_fencing"], 7)
        # 状态未变、不算完成
        self.assertEqual(self.L.get(aid)["state"], "DISPATCHED")

    def test_a2_ledger_has_fencing_caller_wrong_rejected(self):
        aid, f = self.dispatched("a2", 7)
        for bad in (0, 1, 6, 8, 999, -1):
            r = self.L.commit_outcome(aid, "SENT", "e", expected_fencing=bad)
            self.assertFalse(r["ok"], bad)
            self.assertEqual(r["code"], SP.SP_ACT_012, bad)
            self.assertEqual(r["data"]["locked_fencing"], 7, bad)
            self.assertEqual(r["data"]["claimed"], bad, bad)
        self.assertEqual(self.L.get(aid)["state"], "DISPATCHED")

    def test_a3_ledger_has_no_fencing_caller_claims_one(self):
        aid = self.reserved("a3")
        self.assertIsNone(self.L.get(aid)["fencing_generation"])
        r = self.L.commit_outcome(aid, "NOT_SENT", "e", expected_fencing=3)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_ACT_012)
        self.assertIsNone(r["data"]["locked_fencing"])
        self.assertEqual(self.L.get(aid)["state"], "RESERVED")

    def test_a4_no_fencing_on_both_sides_allowed(self):
        """RESERVED + 账本无 fencing + 调用方不声明 → 仍允许（派发前取消路径）。"""
        aid = self.reserved("a4")
        r = self.L.commit_outcome(aid, "NOT_SENT", "cancelled before dispatch")
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.L.get(aid)["state"], "COMPLETED")

    def test_a5_correct_fencing_succeeds(self):
        aid, f = self.dispatched("a5", 42)
        r = self.L.commit_outcome(aid, "SENT", "ok", expected_fencing=42)
        self.assertTrue(r["ok"], r)
        row = self.L.get(aid)
        self.assertEqual(row["state"], "COMPLETED")
        self.assertEqual(row["result_tag"], "SENT")

    def test_a6_completed_reports_completed_not_fencing(self):
        """已完成的动作：优先返回「已完成」，而不是 fencing 错误（语义清晰）。"""
        aid, f = self.dispatched("a6", 5)
        self.assertTrue(self.L.commit_outcome(aid, "SENT", "e",
                                              expected_fencing=5)["ok"])
        r = self.L.commit_outcome(aid, "SENT", "e", expected_fencing=5)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_ACT_001)

    def test_a7_unknown_still_rejects_even_with_valid_fencing(self):
        """G6 不得被 SP1-A 削弱：UNKNOWN 下 fencing 正确也照样拒绝。"""
        aid, f = self.dispatched("a7", 9)
        self.L.recover_unknown()
        self.assertEqual(self.L.get(aid)["state"], "SEND_UNKNOWN")
        r = self.L.commit_outcome(aid, "SENT", "e", expected_fencing=9)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_ACT_003)
        self.assertEqual(r["data"]["reason"], "unknown_unlock_disabled")


# ================================================ B 组：takeover_fencing
class TestTakeover(Sp1aBase):
    def test_b1_happy_path_increments_generation(self):
        aid, f = self.dispatched("b1", 7)
        r = self.L.takeover_fencing(aid, 7, "exec-b")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data"]["fencing_generation"], 8)
        self.assertEqual(r["data"]["previous_generation"], 7)
        row = self.L.get(aid)
        self.assertEqual(row["fencing_generation"], 8)
        self.assertEqual(row["executor_id"], "exec-b")

    def test_b2_takeover_does_not_advance_state(self):
        aid, f = self.dispatched("b2", 3)
        before = self.L.get(aid)
        self.assertTrue(self.L.takeover_fencing(aid, 3, "exec-b")["ok"])
        after = self.L.get(aid)
        for k in ("state", "dispatch_count", "result_tag", "resend_count",
                  "anti_replay_locked", "dispatched_at"):
            self.assertEqual(before[k], after[k], k)

    def test_b3_cas_mismatch_rejected(self):
        aid, f = self.dispatched("b3", 7)
        for bad in (5, 6, 8, 99, -1):
            r = self.L.takeover_fencing(aid, bad, "exec-b")
            self.assertFalse(r["ok"], bad)
            self.assertEqual(r["code"], SP.SP_ACT_013, bad)
            self.assertEqual(r["data"]["reason"], "cas_mismatch", bad)
        self.assertEqual(self.L.get(aid)["fencing_generation"], 7)

    def test_b4_unknown_not_takeoverable(self):
        aid, f = self.dispatched("b4", 4)
        self.L.recover_unknown()
        r = self.L.takeover_fencing(aid, 4, "exec-b")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_ACT_013)
        self.assertEqual(r["data"]["reason"], "state_not_takeoverable")
        self.assertEqual(self.L.get(aid)["state"], "SEND_UNKNOWN")

    def test_b5_anti_replay_locked_not_takeoverable(self):
        aid, f = self.dispatched("b5", 4)
        with self.L._conn as c:
            c.execute("UPDATE actions SET state='ANTI_REPLAY_LOCKED',"
                      " anti_replay_locked=1 WHERE action_id=?", (aid,))
        r = self.L.takeover_fencing(aid, 4, "exec-b")
        self.assertFalse(r["ok"])
        self.assertEqual(r["data"]["reason"], "state_not_takeoverable")

    def test_b6_completed_not_takeoverable(self):
        aid, f = self.dispatched("b6", 4)
        self.assertTrue(self.L.commit_outcome(aid, "SENT", "e",
                                              expected_fencing=4)["ok"])
        r = self.L.takeover_fencing(aid, 4, "exec-b")
        self.assertFalse(r["ok"])
        self.assertEqual(r["data"]["reason"], "state_not_takeoverable")

    def test_b7_no_fencing_held_rejected(self):
        aid = self.reserved("b7")
        r = self.L.takeover_fencing(aid, 0, "exec-b")
        self.assertFalse(r["ok"])
        self.assertEqual(r["data"]["reason"], "no_fencing_held")

    def test_b8_blank_executor_rejected(self):
        aid, f = self.dispatched("b8", 2)
        for bad in ("", "   ", None, 123):
            r = self.L.takeover_fencing(aid, 2, bad)
            self.assertFalse(r["ok"], bad)
            self.assertEqual(r["code"], SP.SP_ACT_013, bad)
        self.assertEqual(self.L.get(aid)["fencing_generation"], 2)

    def test_b9_same_executor_rejected(self):
        aid, f = self.dispatched("b9", 2)
        r = self.L.takeover_fencing(aid, 2, "exec-a")
        self.assertFalse(r["ok"])
        self.assertEqual(r["data"]["reason"], "same_executor")

    def test_b10_unknown_action_id(self):
        r = self.L.takeover_fencing("nope", 1, "exec-b")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_ACT_005)

    def test_b11_reserved_takeover_keeps_state(self):
        """RESERVED 且已有 fencing（reserve 时预占）→ 可接管，状态仍 RESERVED。"""
        aid = self.reserved("b11", fencing=11)
        r = self.L.takeover_fencing(aid, 11, "exec-b")
        self.assertTrue(r["ok"], r)
        row = self.L.get(aid)
        self.assertEqual(row["state"], "RESERVED")
        self.assertEqual(row["fencing_generation"], 12)

    def test_b12_event_written(self):
        aid, f = self.dispatched("b12", 3)
        self.assertTrue(self.L.takeover_fencing(aid, 3, "exec-b")["ok"])
        ev = [e for e in self.L.events(aid) if e["kind"] == "fencing_takeover"]
        self.assertEqual(len(ev), 1)
        self.assertIn("from=3", ev[0]["detail"])
        self.assertIn("to=4", ev[0]["detail"])
        self.assertIn("exec-b", ev[0]["detail"])


# ================================================ C 组：旧持有者失效
class TestStaleHolderFencedOut(Sp1aBase):
    def test_c1_old_holder_cannot_commit_after_takeover(self):
        aid, f = self.dispatched("c1", 7)
        self.assertTrue(self.L.takeover_fencing(aid, 7, "exec-b")["ok"])
        # 旧持有者仍拿 7
        r = self.L.commit_outcome(aid, "SENT", "stale", expected_fencing=7)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_ACT_012)
        self.assertEqual(r["data"]["locked_fencing"], 8)
        self.assertEqual(self.L.get(aid)["state"], "DISPATCHED")

    def test_c2_new_holder_can_commit(self):
        aid, f = self.dispatched("c2", 7)
        self.assertTrue(self.L.takeover_fencing(aid, 7, "exec-b")["ok"])
        r = self.L.commit_outcome(aid, "SENT", "new", expected_fencing=8)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.L.get(aid)["state"], "COMPLETED")

    def test_c3_silent_caller_still_rejected_after_takeover(self):
        aid, f = self.dispatched("c3", 7)
        self.assertTrue(self.L.takeover_fencing(aid, 7, "exec-b")["ok"])
        r = self.L.commit_outcome(aid, "SENT", "silent")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_ACT_011)

    def test_c4_concurrent_takeover_only_one_wins(self):
        """并发接管同一 generation：CAS 保证只有一个成功。"""
        aid, f = self.dispatched("c4", 5)
        results = []
        lock = threading.Lock()
        barrier = threading.Barrier(3)

        def worker(name):
            L2 = actionledger.ActionLedger(self.db)
            try:
                barrier.wait()
                r = L2.takeover_fencing(aid, 5, name)
            finally:
                L2.close()
            with lock:
                results.append((name, r))

        ts = [threading.Thread(target=worker, args=("e%d" % i,)) for i in range(3)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=25)
        self.assertEqual(len(results), 3)
        wins = [n for n, r in results if r["ok"]]
        self.assertEqual(len(wins), 1, results)
        # 最终 generation 只 +1
        self.assertEqual(self.L.get(aid)["fencing_generation"], 6)


# ================================================ D 组：不得反向放松（静态）
class TestNoWeakening(unittest.TestCase):
    def _src(self, name):
        with open(os.path.join(ROOT, "scripts", name), encoding="utf-8") as f:
            return f.read()

    def test_no_lock_weakening(self):
        led = self._src("actionledger.py")
        for needle in ("ux_biz_active", "ux_intent_active", "business_target_norm"):
            self.assertIn(needle, led)
        # 三锁谓词覆盖的活跃态不得减少（仍是 5 态）
        m = re.search(r"state IN \('CREATED','RESERVED','DISPATCHED','SEND_UNKNOWN',\s*"
                      r"'ANTI_REPLAY_LOCKED'\)", led)
        self.assertIsNotNone(m, "三锁活跃态谓词被改动")

    def test_envelope_and_node_verify_untouched(self):
        """Envelope / Node 验签文件不得出现 SP1-A 相关改动痕迹。"""
        env = self._src("envelope.py")
        self.assertIn("envelope_digest", env)
        self.assertNotIn("expected_fencing", env)
        self.assertNotIn("takeover_fencing", env)
        nv_path = os.path.join(ROOT, "scripts", "ego", "node_verify.mjs")
        with open(nv_path, encoding="utf-8") as f:
            nv = f.read()
        self.assertNotIn("expected_fencing", nv)

    @staticmethod
    def _code_only(src):
        """剥掉 docstring 与 # 注释，只看可执行代码。"""
        import ast
        tree = ast.parse(src)
        skip = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
                body = getattr(node, "body", [])
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    d = body[0]
                    end = getattr(d, "end_lineno", d.lineno) or d.lineno
                    skip.update(range(d.lineno, end + 1))
        kept = [ln for i, ln in enumerate(src.splitlines(), 1)
                if i not in skip and not ln.strip().startswith("#")]
        return "\n".join(kept)

    def test_no_sp2_symbols_in_ledger(self):
        """SP2 恢复扫描仍不得夹带（注释中的说明性提及不算）。

        SP3-1 已获正式授权引入裁决协议，故 `adjudications` 不再是禁用符号；
        改为下方三条更强的收口断言（入口收敛 + commit_outcome 不得读裁决表）。
        """
        code = self._code_only(self._src("actionledger.py"))
        for bad in ("def recover_one(", "def recovery_scan(", "def adjudicate("):
            self.assertNotIn(bad, code)

    def test_sp3_symbols_confined_to_three_entrypoints(self):
        """SP3 只允许三个公开入口，不得长出旁路 API（同义多入口=绕过面）。"""
        src = self._src("actionledger.py")
        for fn in ("decide_adjudication", "consume_adjudication",
                   "revoke_adjudication"):
            self.assertEqual(src.count("def %s(" % fn), 1, fn)

    def test_commit_outcome_never_reads_adjudications(self):
        """P0 分工：UNKNOWN 的唯一解锁通道是 consume_adjudication。

        commit_outcome 内部一旦出现对裁决表的读取，G6-HOTFIX 的「无条件拒绝」
        就会被悄悄改写成「有裁决就放行」——这正是本断言要钉死的地方。
        """
        import textwrap
        src = self._src("actionledger.py")
        seg = re.search(r"( {4}def commit_outcome\(.*?)"
                        r"(?=\n {4}@|\n {4}def |\Z)", src, re.S)
        self.assertIsNotNone(seg, "commit_outcome 边界无法定位")
        body = self._code_only(textwrap.dedent(seg.group(1)))
        # 允许**文案**里提到 adjudication（G6 的错误说明就提到了「没有可信裁决
        # 数据源」）；禁止的是数据访问与裁决 API 调用。
        for bad in ("FROM adjudications", "INTO adjudications", "adjudications(",
                    "adjudication_evidence", "decide_adjudication",
                    "consume_adjudication", "revoke_adjudication"):
            self.assertNotIn(bad, body, bad)

    def test_exactly_one_commit_outcome(self):
        self.assertEqual(self._src("actionledger.py").count("def commit_outcome"), 1)


if __name__ == "__main__":
    unittest.main()
