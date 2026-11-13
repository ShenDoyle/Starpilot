#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G6-HOTFIX 专项验收（PROJECT-04 / 独立安全修复）。

背景：P2-3-0 调查发现 G6 —— `commit_outcome` 对 SEND_UNKNOWN /
ANTI_REPLAY_LOCKED 只校验 `unlock.get("kind")` 非空，任意 `{"kind":"x"}`
即可把 UNKNOWN 推成 COMPLETED 并释放 L-biz。GPT 终审裁定 A4：
「G6 是立即停止条件」，并要求修复期临时状态为
**UNKNOWN 解锁入口拒绝所有调用**（无临时开关、无 kind 兼容降级）。

本文件覆盖 H1–H6 与 H9，外加并发专项（两请求同时消费同一裁决引用，
最多一个成功——当前实现为「全部拒绝」，故断言更强：零成功且状态不变）。

H7（正常业务路径不回归）/ H8（三锁、Envelope、Node 验签语义不变）
由全量回归 + 静态断言覆盖（见 TestG6StaticBoundaries）。
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

INTENT = {"action": "send_message", "target": "g6-target", "text": "hi"}

# 旧实现「看起来合法」的 unlock 形态 —— 全部不得放行（H1/H4/H9）
LEGACY_UNLOCKS = [
    {"kind": "manual", "operator": "operator-fan", "reason": "confirmed"},
    {"kind": "page_evidence", "page_generation": "pg1",
     "observation_id": "obs1"},
    {"kind": "x"},                                   # 最简绕过
    {"kind": ""},                                    # 空 kind
    {"kind": None},
    {"adjudication_id": "0" * 64},                   # 伪造裁决引用
    {"adjudication_id": "deadbeef"},                 # 非法长度
    {"adjudication_id": "0" * 64, "kind": "manual"},  # 混合形态
    {},                                              # 空对象
    {"operator": "x"},                               # 只有 operator
    {"evidence_refs": [{"type": "manual_attestation"}]},  # 只有证据无裁决
]
NON_DICT_UNLOCKS = ["kind=manual", 1, True, ["kind"], None]


class G6Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="sp-g6-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.db = os.path.join(self.d, "ledger.sqlite")
        self.L = actionledger.ActionLedger(self.db)

    def tearDown(self):
        self.L.close()

    # ---------- 夹具 ----------
    @staticmethod
    def _intent(target, tag=""):
        """每个用例唯一 intent（intent_key 含 target+text，避免 L-int 干扰）。"""
        return {"action": "send_message", "target": target, "text": "hi" + tag}

    def to_unknown(self, task="t-g6", target=None, tag=""):
        """RESERVED → DISPATCHED → SEND_UNKNOWN（走真实恢复路径）。"""
        target = target or (task + "-bt")
        r = self.L.reserve(task, self._intent(target, tag), budget_units=10,
                           business_target=target)
        self.assertTrue(r["ok"], r)
        aid = r["data"]["action_id"]
        self.assertTrue(self.L.mark_dispatched(aid, 1, "exec-g6")["ok"])
        rec = self.L.recover_unknown()
        self.assertGreaterEqual(rec["data"]["count"], 1, rec)
        row = self.L.get(aid)
        self.assertEqual(row["state"], "SEND_UNKNOWN")
        self.assertEqual(row["anti_replay_locked"], 1)
        return aid

    def to_anti_replay_locked(self, task="t-ar", target=None):
        """直接构造 ANTI_REPLAY_LOCKED（该状态当前无写入路径，见 G5）。"""
        target = target or (task + "-bt")
        r = self.L.reserve(task, self._intent(target, "ar"), budget_units=10,
                           business_target=target)
        self.assertTrue(r["ok"], r)
        aid = r["data"]["action_id"]
        self.assertTrue(self.L.mark_dispatched(aid, 1, "exec-ar")["ok"])
        with self.L._conn as c:  # 测试内直接置状态，模拟文档态
            c.execute("UPDATE actions SET state='ANTI_REPLAY_LOCKED',"
                      " anti_replay_locked=1 WHERE action_id=?", (aid,))
        self.assertEqual(self.L.get(aid)["state"], "ANTI_REPLAY_LOCKED")
        return aid


# ================================================ H1：SEND_UNKNOWN 拒绝普通 kind
class TestH1UnknownRejectsKind(G6Base):
    def test_h1_send_unknown_rejects_all_legacy_shapes(self):
        for i, fake in enumerate(LEGACY_UNLOCKS):
            aid = self.to_unknown(task="t-h1-%d" % i,
                                 target="g6-h1-%d" % i)
            r = self.L.commit_outcome(aid, "SENT", "attempt", unlock=fake)
            self.assertFalse(r["ok"], fake)
            self.assertEqual(r["code"], SP.SP_ACT_003, fake)
            self.assertEqual(r["data"]["reason"], "unknown_unlock_disabled", fake)
            self.assertEqual(r["data"]["state"], "SEND_UNKNOWN", fake)
            self.assertEqual(r["data"]["unlock_present"], bool(fake), fake)
            self.L.commit_outcome  # noqa: B018  (显式保留调用面可读性)
            self.assertEqual(self.L.get(aid)["state"], "SEND_UNKNOWN", fake)

    def test_h1_non_dict_unlock_also_rejected(self):
        for i, fake in enumerate(NON_DICT_UNLOCKS):
            aid = self.to_unknown(task="t-h1n-%d" % i,
                                 target="g6-h1n-%d" % i)
            r = self.L.commit_outcome(aid, "SENT", "attempt", unlock=fake)
            self.assertFalse(r["ok"], fake)
            self.assertEqual(r["code"], SP.SP_ACT_003, fake)
            self.assertEqual(self.L.get(aid)["state"], "SEND_UNKNOWN", fake)

    def test_h1_not_sent_also_rejected(self):
        """UNKNOWN 下改判 NOT_SENT 同样被拒（不允许「反向解锁」）。"""
        aid = self.to_unknown(task="t-h1s", target="g6-h1s")
        r = self.L.commit_outcome(aid, "NOT_SENT", "wishful",
                                  unlock={"kind": "manual", "operator": "x"})
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_ACT_003)
        self.assertEqual(self.L.get(aid)["state"], "SEND_UNKNOWN")


# ================================================ H2：ANTI_REPLAY_LOCKED 同样拒绝
class TestH2AntiReplayRejects(G6Base):
    def test_h2_anti_replay_locked_rejects_all_shapes(self):
        for i, fake in enumerate(LEGACY_UNLOCKS):
            aid = self.to_anti_replay_locked(task="t-h2-%d" % i,
                                            target="g6-h2-%d" % i)
            r = self.L.commit_outcome(aid, "SENT", "attempt", unlock=fake)
            self.assertFalse(r["ok"], fake)
            self.assertEqual(r["code"], SP.SP_ACT_003, fake)
            self.assertEqual(r["data"]["state"], "ANTI_REPLAY_LOCKED", fake)
            self.assertEqual(self.L.get(aid)["state"],
                             "ANTI_REPLAY_LOCKED", fake)

    def test_h2_no_unlock_also_rejected(self):
        aid = self.to_anti_replay_locked(task="t-h2n", target="g6-h2n")
        r = self.L.commit_outcome(aid, "SENT", "no unlock")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_ACT_003)


# ================================================ H3：无真实裁决记录 → fail-closed
class TestH3FailClosed(G6Base):
    def test_h3_adjudication_table_exists_yet_g6_still_rejects(self):
        """SP3-1 已实施：裁决表存在，但 UNKNOWN 的解锁**不因数据源存在而开启**。

        原用例断言「adjudications 表不存在，故不存在合法解锁」，其前提被 SP3-1
        授权变更取代。这里改的是**前提**而不是**结论**：数据源有了，commit_outcome
        仍然无条件拒绝任何自述 unlock —— 强度不降反升。
        """
        names = [r[0] for r in self.L._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        self.assertIn("adjudications", names)
        self.assertIn("adjudication_evidence", names)
        aid = self.to_unknown(task="t-h3b", target="g6-h3b")
        for unlock in ({"adjudication_id": "a" * 64},
                       {"adjudication_id": "a" * 64, "kind": "manual"},
                       {"kind": "manual"}):
            r = self.L.commit_outcome(aid, "SENT", "src", unlock=unlock)
            self.assertFalse(r["ok"], unlock)
            self.assertEqual(r["code"], SP.SP_ACT_003, unlock)
            self.assertEqual(r["data"]["reason"], "unknown_unlock_disabled", unlock)
        self.assertEqual(self.L.get(aid)["state"], "SEND_UNKNOWN")

    def test_h3_rejects_even_with_plausible_adjudication_id(self):
        aid = self.to_unknown(task="t-h3", target="g6-h3")
        r = self.L.commit_outcome(
            aid, "SENT", "src",
            unlock={"adjudication_id": "a" * 64})
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_ACT_003)
        self.assertEqual(r["data"]["reason"], "unknown_unlock_disabled")


# ================================================ H4：伪造/缺失/过期/重复消费全部拒绝
class TestH4ForgeryRejected(G6Base):
    def test_h4_forged_ids_all_rejected(self):
        aid = self.to_unknown(task="t-h4", target="g6-h4")
        cases = [
            "0" * 64,                       # 全零
            "f" * 64,                       # 全 f
            "Z" * 64,                       # 非 hex
            "0" * 63,                       # 长度不足
            "0" * 65,                       # 超长
            "../../etc/passwd",             # 路径穿越尝试
            "' OR 1=1 --",                  # 注入尝试
            "\x00" * 64,                     # 空字节
        ]
        for c in cases:
            r = self.L.commit_outcome(aid, "SENT", "e",
                                      unlock={"adjudication_id": c})
            self.assertFalse(r["ok"], c)
            self.assertEqual(r["code"], SP.SP_ACT_003, c)
        self.assertEqual(self.L.get(aid)["state"], "SEND_UNKNOWN")

    def test_h4_repeat_consumption_attempt_keeps_state(self):
        """连续多次尝试消费 → 每次都拒绝，状态不累积变化。"""
        aid = self.to_unknown(task="t-h4r", target="g6-h4r")
        states = []
        for _ in range(5):
            r = self.L.commit_outcome(aid, "SENT", "e",
                                      unlock={"adjudication_id": "b" * 64})
            self.assertFalse(r["ok"])
            states.append(self.L.get(aid)["state"])
        self.assertEqual(set(states), {"SEND_UNKNOWN"})


# ================================================ H5：作用域/action_id/状态/fencing 匹配
class TestH5ScopeAndOwner(G6Base):
    def test_h5_cross_action_unlock_does_not_leak(self):
        """A 的解锁尝试不得影响 B；两条各自保持 UNKNOWN。"""
        a1 = self.to_unknown(task="t-h5a", target="g6-h5a")
        a2 = self.to_unknown(task="t-h5b", target="g6-h5b")
        for aid in (a1, a2):
            r = self.L.commit_outcome(aid, "SENT", "e",
                                      unlock={"kind": "manual", "operator": "x"})
            self.assertFalse(r["ok"])
        self.assertEqual(self.L.get(a1)["state"], "SEND_UNKNOWN")
        self.assertEqual(self.L.get(a2)["state"], "SEND_UNKNOWN")

    def test_h5_fencing_change_does_not_grant_unlock(self):
        """即便 fencing 被推进（模拟接管），UNKNOWN 仍不可解锁。"""
        aid = self.to_unknown(task="t-h5f", target="g6-h5f")
        with self.L._conn as c:
            c.execute("UPDATE actions SET fencing_generation=999 WHERE action_id=?",
                      (aid,))
        r = self.L.commit_outcome(aid, "SENT", "e",
                                  unlock={"adjudication_id": "c" * 64})
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_ACT_003)
        self.assertEqual(self.L.get(aid)["state"], "SEND_UNKNOWN")

    def test_h5_unknown_action_id_rejected(self):
        r = self.L.commit_outcome("no-such-action", "SENT", "e",
                                  unlock={"kind": "manual"})
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_ACT_005)


# ================================================ H6：任何失败不得释放 L-biz
class TestH6NoLockRelease(G6Base):
    def test_h6_all_failure_paths_keep_biz_lock(self):
        aid = self.to_unknown(task="t-h6", target="g6-h6")
        before = self.L.business_risk("g6-h6")
        self.assertEqual(before["count"], 1)
        # 各种失败路径轮番试
        attempts = [
            ("SENT", None),
            ("SENT", {"kind": "manual"}),
            ("SENT", {"adjudication_id": "d" * 64}),
            ("NOT_SENT", {"kind": "manual"}),
            ("NOT_SENT", None),
            ("SENT", {}),
        ]
        for tag, ul in attempts:
            r = self.L.commit_outcome(aid, tag, "e", unlock=ul)
            self.assertFalse(r["ok"], (tag, ul))
            self.assertEqual(self.L.business_risk("g6-h6")["count"], 1, (tag, ul))
        row = self.L.get(aid)
        self.assertEqual(row["state"], "SEND_UNKNOWN")
        self.assertEqual(row["dispatch_count"], 1)
        self.assertEqual(row["resend_count"], 0)

    def test_h6_no_unlocked_event_written(self):
        """解锁被拒时不得写入 'unlocked' 事件（否则审计会误报已解锁）。"""
        aid = self.to_unknown(task="t-h6e", target="g6-h6e")
        self.L.commit_outcome(aid, "SENT", "e",
                              unlock={"kind": "manual", "operator": "x"})
        kinds = [e["kind"] for e in self.L.events(aid)]
        self.assertNotIn("unlocked", kinds)
        self.assertNotIn("outcome", kinds)


# ================================================ H9：绕过专项 + 并发专项
class TestH9BypassMatrix(G6Base):
    def test_h9_resend_still_refused_after_unlock_attempt(self):
        """解锁失败后，重发预算仍为 0（不能靠失败尝试松动预算）。"""
        aid = self.to_unknown(task="t-h9", target="g6-h9")
        self.L.commit_outcome(aid, "SENT", "e",
                              unlock={"kind": "manual", "operator": "x"})
        d = self.L.mark_dispatched(aid, 2, "exec-2")
        self.assertFalse(d["ok"])
        self.assertEqual(d["code"], SP.SP_ACT_003)
        self.assertEqual(self.L.get(aid)["dispatch_count"], 1)

    def test_h9_new_intent_for_same_target_blocked(self):
        """同业务对象新意图仍被 L-biz 拦住（锁没被失败尝试释放）。"""
        self.to_unknown(task="t-h9b", target="g6-h9b")
        r = self.L.reserve("t-h9b-new", INTENT, budget_units=10,
                           business_target="g6-h9b")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_ACT_009)

    def test_h9_concurrent_consume_at_most_one(self):
        """并发专项（GPT 额外要求）：两线程同时试图用同一引用消费同一 UNKNOWN。

        当前实现为「全部拒绝」，故断言更强：**零成功**、状态不变、
        且不出现任何 unlocked/outcome 事件。若将来 SP3 落地改为真实裁决，
        本用例应改为「最多一个成功」，语义不得放宽为「都可以成功」。
        """
        aid = self.to_unknown(task="t-h9c", target="g6-h9c")
        payload = {"adjudication_id": "e" * 64}
        results = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def worker(i):
            # 独立连接（多实例真实并发；共享 sqlite 连接跨线程不可用）
            L2 = actionledger.ActionLedger(self.db)
            try:
                barrier.wait()
                r = L2.commit_outcome(aid, "SENT", "concurrent-%d" % i,
                                      unlock=dict(payload))
            finally:
                L2.close()
            with lock:
                results.append(r)

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=20)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(not r["ok"] for r in results), results)
        self.assertTrue(all(r["code"] == SP.SP_ACT_003 for r in results), results)
        self.assertEqual(self.L.get(aid)["state"], "SEND_UNKNOWN")
        kinds = [e["kind"] for e in self.L.events(aid)]
        self.assertNotIn("unlocked", kinds)
        self.assertNotIn("outcome", kinds)

    def test_h9_two_distinct_unknowns_concurrent(self):
        """两条不同 UNKNOWN 并发解锁尝试：互不影响，均保持锁态。"""
        a1 = self.to_unknown(task="t-h9d1", target="g6-h9d1")
        a2 = self.to_unknown(task="t-h9d2", target="g6-h9d2")
        errs = []
        barrier = threading.Barrier(2)

        def worker(aid, i):
            L2 = actionledger.ActionLedger(self.db)
            try:
                barrier.wait()
                L2.commit_outcome(aid, "SENT", "c%d" % i,
                                  unlock={"kind": "manual"})
            except Exception as e:  # noqa: BLE001
                errs.append(e)
            finally:
                L2.close()

        ts = [threading.Thread(target=worker, args=(a1, 1)),
              threading.Thread(target=worker, args=(a2, 2))]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=20)
        self.assertEqual(errs, [])
        self.assertEqual(self.L.get(a1)["state"], "SEND_UNKNOWN")
        self.assertEqual(self.L.get(a2)["state"], "SEND_UNKNOWN")


# ================================================ H7/H8：静态边界（不许反向放松）
class TestG6StaticBoundaries(unittest.TestCase):
    """H7/H8：修复不得夹带、不得反向放松 P2-2 已建立的安全边界。"""

    def _src(self, name):
        with open(os.path.join(ROOT, "scripts", name), encoding="utf-8") as f:
            return f.read()

    @staticmethod
    def _code_only(src):
        """剥掉 docstring 与 # 注释，只留可执行代码（避免说明性提及被误判）。"""
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

    def test_h8_no_kind_compat_branch_remains(self):
        """可执行代码里不得残留 unlock["kind"] 判定（G6 的成因）。"""
        code = self._code_only(self._src("actionledger.py"))
        self.assertIsNone(
            re.search(r'unlock\s*\.\s*get\s*\(\s*[\'"]kind', code),
            "unlock.get('kind') 必须已被移除（不得保留 kind 兼容分支）")
        self.assertNotIn('unlock["kind"]', code)
        self.assertNotIn("unlock['kind']", code)

    def test_h8_commit_outcome_single_definition(self):
        src = self._src("actionledger.py")
        self.assertEqual(src.count("def commit_outcome"), 1)

    def test_h8_group_and_envelope_untouched_markers(self):
        """三锁谓词与 Envelope 关键常量仍在（未被本次修复改写）。"""
        led = self._src("actionledger.py")
        for needle in ("ux_biz_active", "ux_intent_active",
                       "business_target_norm", "dispatch_count"):
            self.assertIn(needle, led)
        env = self._src("envelope.py")
        self.assertIn("envelope_digest", env)

    def test_h8_no_new_bypass_switch(self):
        """不得新增「跳过解锁校验」的环境变量/开关。"""
        src = self._src("actionledger.py").lower()
        for bad in ("skip_unlock", "allow_unlock", "unlock_bypass",
                    "g6_bypass", "disable_unlock_check"):
            self.assertNotIn(bad, src)


if __name__ == "__main__":
    unittest.main()
