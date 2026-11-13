"""PROJECT-04 P2-2-3 强制验收矩阵：DG01-DG08 + 任务书 §5 补充项（真实执行）。

- 独立事务边界用**双连接可见性**证明（调用方连接开事务 → 另一连接可见已提交事件）。
- 崩溃测试用子进程（提交前后 os._exit）。
- 事件不可覆盖：普通状态更新/恢复后 DELEGATED 仍在且内容不变。
- 不调用浏览器（本包边界）。
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import actionledger as AL  # noqa: E402
import dispatch_marker as DM  # noqa: E402
import sp_errors as SP  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
AID = "a-dg-0001"
OP = "op-dg-0001"
DG = dict(op_id=OP, envelope_digest="c" * 64, fencing="5",
          worker_id="w-dg", key_version="1", business_target_norm="order:9")


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="p2dg-")
        self.db = os.path.join(self.d, "l.sqlite")
        self.L = AL.ActionLedger(self.db)

    def tearDown(self):
        self.L.close()
        shutil.rmtree(self.d, ignore_errors=True)

    def _dg(self, **over):
        return self.L.write_delegated(AID, **{**DG, **over})


class TestDG01WriteFailure(Base):
    def test_dg01_write_failure_forbids_dispatch(self):
        """DG01：DELEGATED 写入失败 → 禁止后续派发（ok=False）。"""
        with mock.patch.object(self.L, "_write_delegated_once",
                               side_effect=sqlite3.OperationalError("injected")):
            r = self._dg()
        self.assertFalse(r["ok"], r)
        self.assertEqual(self.L.delegated_events(AID)["present"], False)

    def test_dg01b_submit_failure_via_bad_path(self):
        L2 = AL.ActionLedger(os.path.join(self.d, "ok.sqlite"))
        L2.close()
        L3 = AL.ActionLedger(os.path.join(self.d, "ok.sqlite"))
        # 指向不存在目录的库 → 打开/提交失败（fail-closed）
        Lbad = AL.ActionLedger.__new__(AL.ActionLedger)
        Lbad.db_path = os.path.join(self.d, "no-such-dir", "x.sqlite")
        Lbad._conn = L3._conn
        r = Lbad.write_delegated(AID, **DG)
        self.assertFalse(r["ok"], r)
        L3.close()


class TestDG02CommittedThenCrash(Base):
    def test_dg02_commit_then_crash_recovery_finds_event(self):
        """DG02：事件提交后、mark 前崩溃 → 恢复可查到权威事件。"""
        CHILD = (
            "import sys\n"
            "sys.path.insert(0, r'%s')\n"
            "import actionledger as AL\n"
            "L = AL.ActionLedger(sys.argv[1])\n"
            "r = L.write_delegated('a-crash', op_id='op-crash', envelope_digest=%r,\n"
            "                      fencing='5', worker_id='w', key_version='1',\n"
            "                      business_target_norm='t:c')\n"
            "assert r['ok'], r\n"
            "import os; os._exit(7)\n" % (os.path.join(REPO, "scripts"), "d" * 64)
        )
        p = subprocess.run([sys.executable, "-c", CHILD, self.db],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 7, p.stderr)   # 提交成功后硬退出
        st = self.L.delegated_events("a-crash")
        self.assertTrue(st["ok"] and st["present"] and st["complete"])
        self.assertEqual(st["events"][0]["op_id"], "op-crash")


class TestDG03MarkFails(Base):
    def test_dg03_mark_failure_keeps_delegated(self):
        """DG03：mark_dispatched 失败 → 保留 DELEGATED，不得自动重派。"""
        r1 = self.L.reserve("t-dg", {"a": 1}, business_target="order:9",
                            side_effect=True)
        self.assertTrue(r1["ok"], r1)
        aid = r1["data"]["action_id"]
        r = self.L.write_delegated(aid, **DG)  # 独立事务提交
        self.assertTrue(r["ok"], r)
        # mark_dispatched 失败（不存在的 action_id → ok=False）
        mr = self.L.mark_dispatched("a-nonexistent", 99, "x")
        self.assertFalse(mr["ok"], mr)
        st = self.L.delegated_events(aid)
        self.assertTrue(st["present"])
        # 权威事件存在 → 恢复不得推断未派发
        chk = self.L.check_delegated(aid, {"op_id": OP, "fencing": "5"})
        self.assertEqual(chk["state"], "PRESENT")


class TestDG04DG05DG06Recovery(Base):
    def test_dg04_no_state_based_inference(self):
        """DG04：backend 前崩溃 → 不得仅凭状态推断未派发。"""
        r1 = self.L.reserve("t-dg4", {"a": 1}, business_target="order:9",
                            side_effect=True)
        aid = r1["data"]["action_id"]
        self.L.write_delegated(aid, **DG)
        self.L.mark_dispatched(aid, 1, "worker")
        # 状态为 DISPATCHED 但无 outcome：recover_unknown 只能给 UNKNOWN
        self.L.recover_unknown()
        row = dict(self.L._conn.execute(
            "SELECT state FROM actions WHERE action_id=?", (aid,)).fetchone())
        self.assertEqual(row["state"], "SEND_UNKNOWN")
        # 且 DELEGATED 证据仍在（恢复过程不删除权威事件）
        self.assertTrue(self.L.delegated_events(aid)["present"])

    def test_dg05_after_backend_crash_no_auto_recall(self):
        """DG05/DG06：backend 后崩溃/超时 → 不自动重试（resend 恒 0）。"""
        r1 = self.L.reserve("t-dg5", {"a": 1}, business_target="order:9",
                            side_effect=True)
        aid = r1["data"]["action_id"]
        self.L.write_delegated(aid, **DG)
        self.L.mark_dispatched(aid, 1, "worker")
        self.L.recover_unknown()
        row = dict(self.L._conn.execute(
            "SELECT state, resend_count, dispatch_count FROM actions "
            "WHERE action_id=?", (aid,)).fetchone())
        self.assertEqual(row["resend_count"], 0)      # 绝不自动重派
        self.assertEqual(row["dispatch_count"], 1)    # 不追加派发
        # 且没有任何自动重派接口：事件写入保持幂等、不产生新事件
        r2 = self.L.write_delegated(aid, **DG)
        self.assertTrue(r2["ok"] and r2["data"]["idempotent"], r2)


class TestDG07LedgerUnavailable(Base):
    def test_dg07_missing_not_interpreted_as_not_dispatched(self):
        """DG07：Ledger 不可用 → 不得把事件缺失解释成未派发。"""
        self._dg()
        self.L._conn.close()
        st = self.L.delegated_events(AID)
        self.assertFalse(st["ok"])
        self.assertFalse(st["present"])          # present=False 但 ok=False
        self.assertIn("ledger unavailable", st["reason"])
        chk = self.L.check_delegated(AID, {"op_id": OP})
        self.assertFalse(chk["ok"])              # 拒绝给出安全结论
        self.assertEqual(chk["state"], "UNKNOWN")


class TestDG08IdempotencyAndConflict(Base):
    def test_dg08_same_identity_idempotent(self):
        r1 = self._dg()
        r2 = self._dg()
        self.assertTrue(r1["ok"] and r2["ok"])
        self.assertFalse(r1["data"]["idempotent"])
        self.assertTrue(r2["data"]["idempotent"])
        evs = self.L.delegated_events(AID)["events"]
        self.assertEqual(len(evs), 1)            # 不重复写入

    def test_dg08b_identity_conflicts_rejected(self):
        self._dg()
        for over in ({"envelope_digest": "e" * 64},   # 不同 digest
                     {"fencing": "6"},                 # 不同 fencing
                     {"worker_id": "w-other"},         # 不同 worker
                     {"key_version": "2"},             # 不同 key_version
                     {"op_id": "op-other"}):           # 不同 op
            r = self._dg(**over)
            self.assertFalse(r["ok"], over)
            self.assertEqual(r["code"], SP.SP_OP_002, over)
            self.assertIn("conflict_fields", r["data"])
        # 原事件未被覆盖
        ev = self.L.delegated_events(AID)["events"][0]
        self.assertEqual(ev["op_id"], OP)
        self.assertEqual(ev["envelope_digest"], "c" * 64)


class TestExtras(Base):
    def test_state_updates_never_delete_evidence(self):
        """普通状态更新/恢复/提交结果不能删除权威事件。"""
        r1 = self.L.reserve("t-ex", {"a": 1}, business_target="order:9",
                            side_effect=True)
        aid = r1["data"]["action_id"]
        self.L.write_delegated(aid, **DG)
        self.L.mark_dispatched(aid, 1, "worker")
        # SP1-A 核验（2026-11-13 终审第 13 项）：此处必须使用与 mark_dispatched
        # 同一代 fencing，并断言提交真的成功——否则本用例会在 commit 静默失败
        # 的情况下仍然通过（等于掩盖了缺失参数）。
        c = self.L.commit_outcome(aid, "SENT", "evidence", expected_fencing=1)
        self.assertTrue(c["ok"], c)
        evs = self.L.delegated_events(aid)
        self.assertTrue(evs["present"])
        # events 表无 UPDATE/DELETE 路径（全文断言）
        src = open(os.path.join(REPO, "scripts", "actionledger.py"),
                   encoding="utf-8").read()
        self.assertNotIn("DELETE FROM events", src)
        self.assertNotIn("UPDATE events", src)

    def test_independent_transaction_boundary(self):
        """独立事务真实边界三重证明：
        ①调用方连接持有未提交**读**事务时不阻塞写入（WAL：读不挡写）；
        ②写入返回后全新连接立即可见该事件（已 COMMIT）；
        ③事件不在调用方连接的事务内（主连接 in_transaction=False）。"""
        self.L._conn.execute("BEGIN")           # 调用方读事务（快照）
        try:
            self.assertTrue(self.L._conn.in_transaction)
            # WAL：首次读即建立快照——先读一次（空），锁定旧快照
            self.L._conn.execute(
                "SELECT COUNT(*) FROM events").fetchone()
            r = self._dg()                      # 独立连接 BEGIN IMMEDIATE…COMMIT
            self.assertTrue(r["ok"], r)
            probe = sqlite3.connect(self.db, timeout=15.0)
            try:
                n = probe.execute(
                    "SELECT COUNT(*) FROM events WHERE action_id=? AND kind=?",
                    (AID, AL.DELEGATED_KIND)).fetchone()[0]
            finally:
                probe.close()
            self.assertEqual(n, 1)              # ② 新连接可见 → 已独立提交
            # ③ 调用方快照内看不到该事件（旧快照）→ 必属两个独立事务
            m = self.L._conn.execute(
                "SELECT COUNT(*) FROM events WHERE action_id=? AND kind=?",
                (AID, AL.DELEGATED_KIND)).fetchone()[0]
            self.assertEqual(m, 0)
        finally:
            try:
                self.L._conn.execute("ROLLBACK")
            except Exception:
                pass

    def test_infra_retry_reruns_full_txn_no_duplicate(self):
        """_infra_retry 语义：瞬态失败重试后仅一条事件（幂等重查）。"""
        real = self.L._write_delegated_once
        calls = {"n": 0}

        def flaky(fields):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("transient")
            return real(fields)

        with mock.patch.object(self.L, "_write_delegated_once", side_effect=flaky):
            r = self._dg()
        self.assertTrue(r["ok"], r)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(len(self.L.delegated_events(AID)["events"]), 1)

    def test_integrity_error_never_retried(self):
        """IntegrityError（触发器真实注入）→ 不重试、不覆盖、fail-closed。"""
        self.L._conn.execute(
            "CREATE TRIGGER t_boom BEFORE INSERT ON events "
            "WHEN NEW.kind='%s' BEGIN SELECT RAISE(ABORT, 'injected'); END"
            % AL.DELEGATED_KIND)
        calls = {"n": 0}
        real = self.L._write_delegated_once

        def counting(fields):
            calls["n"] += 1
            return real(fields)

        import time as _t
        before = _t.time()
        with mock.patch.object(self.L, "_write_delegated_once",
                               side_effect=counting):
            r = self._dg()
        elapsed = _t.time() - before
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["code"], SP.SP_OP_002, r)
        self.assertEqual(calls["n"], 1)        # 仅一次，无重试
        self.assertLess(elapsed, 1.0)          # 无重试退避等待
        self.assertFalse(self.L.delegated_events(AID)["present"])

    def test_both_evidence_lost_conservative_boundary(self):
        """Marker 与 Ledger 同时丢失 → 保守恢复边界（无证据≠可证未派发）。"""
        ops = os.path.join(self.d, "ops")
        dd = DM.dispatching_dir(ops)
        # 场景：既有 action 已 mark_dispatched 但 marker 与 DELEGATED 均无
        r1 = self.L.reserve("t-both", {"a": 1}, business_target="order:9",
                            side_effect=True)
        aid = r1["data"]["action_id"]
        self.L.mark_dispatched(aid, 1, "worker")
        self.assertFalse(os.path.exists(DM.marker_path(dd, OP)))
        self.assertFalse(self.L.delegated_events(aid)["present"])
        # 恢复语义：证据双缺 + 权威存储完整性未证明 → 只能 UNKNOWN（P1S2-B TI）
        # 本包只保证查询面如实返回「双缺」；合并判定在 P2-2-4
        self.assertTrue(self.L.delegated_events(aid)["complete"])

    def test_field_validation_strict(self):
        for over in ({"op_id": ""}, {"envelope_digest": "XYZ"},
                     {"fencing": "-1"}, {"worker_id": ""},
                     {"key_version": ""}):
            r = self._dg(**over)
            self.assertFalse(r["ok"], over)
            self.assertEqual(r["code"], SP.SP_OP_001, over)


if __name__ == "__main__":
    unittest.main()
