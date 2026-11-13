#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_p2_lock.py —— PROJECT-04 P2-1 强制验收（GPT 任务书 §三 T01-T16）。

核心命题（GPT 终审原文）：即使两个执行者同时犯错、同时发起相同业务对象的
认领请求，底层数据库也只能允许一个有效认领成立。

并发测试纪律（GPT §三）：T12/T13 必须使用**独立数据库连接**；T13 使用
**独立进程**并发，不得只用同进程顺序模拟。
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

SCRIPTS = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, SCRIPTS)

import actionledger as AL  # noqa: E402
import sp_errors as SP  # noqa: E402

T = 0  # fencing generation 占位


def _pay(scope):
    return {"action": "send_reply", "scope": scope}


class _Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="p2lock-")
        self.db = os.path.join(self.d, "ledger.db")
        self.L = AL.ActionLedger(self.db)

    def tearDown(self):
        try:
            self.L.close()
        except Exception:
            pass
        shutil.rmtree(self.d, ignore_errors=True)

    def _to_unknown(self, aid):
        r = self.L.mark_dispatched(aid, 1, "u-exec")
        assert r["ok"], r
        r = self.L.recover_unknown()
        assert r["ok"], r


# --------------------------------------------------------------- normalize 单元
class TestNormalize(unittest.TestCase):
    def test_case_and_whitespace_variants_same_key(self):
        # P1S2-A §3 案例 7：大小写/首尾空白变体归一
        base = AL.normalize_business_target("order:123")
        for v in ("Order:123", "ORDER:123", "order:123 ", " order:123",
                  "\torder:123\n"):
            self.assertEqual(AL.normalize_business_target(v), base, v)

    def test_unicode_nfc_equivalence(self):
        self.assertEqual(AL.normalize_business_target("caf\u00e9:1"),
                         AL.normalize_business_target("cafe\u0301:1"))

    def test_missing_empty_none(self):
        self.assertIsNone(AL.normalize_business_target(None))
        self.assertIsNone(AL.normalize_business_target(""))
        self.assertIsNone(AL.normalize_business_target("   "))

    def test_non_str_fail_closed(self):
        for bad in (123, 1.5, ["a"], {"a": 1}, True):
            with self.assertRaises(ValueError):
                AL.normalize_business_target(bad)

    def test_no_over_normalization(self):
        # 防过度归一：内部空白、全半角、不同语义对象不得合并
        self.assertNotEqual(AL.normalize_business_target("order: 1"),
                            AL.normalize_business_target("order:1"))
        self.assertNotEqual(AL.normalize_business_target("Ｏｒｄｅｒ１"),
                            AL.normalize_business_target("Order1"))


# --------------------------------------------------------------- T01-T09
class TestLockMatrix(_Base):
    def test_t01_same_target_diff_intent(self):
        r1 = self.L.reserve("t1", _pay("s1"), business_target="order:1")
        self.assertTrue(r1["ok"], r1)
        r2 = self.L.reserve("t2", _pay("s2"), business_target="order:1")
        self.assertFalse(r2["ok"], r2)
        self.assertEqual(r2["code"], SP.SP_ACT_009)

    def test_t02_same_target_diff_task(self):
        r1 = self.L.reserve("tA", _pay("s1"), business_target="order:1")
        self.assertTrue(r1["ok"], r1)
        r2 = self.L.reserve("tB", _pay("s2"), business_target="order:1")
        self.assertFalse(r2["ok"], r2)
        self.assertEqual(r2["code"], SP.SP_ACT_009)
        self.assertEqual(r2["data"]["holder_task_id"], "tA")

    def test_t03_same_target_diff_run(self):
        r1 = self.L.reserve("t1", _pay("s1"), run_id="run-1", business_target="order:1")
        self.assertTrue(r1["ok"], r1)
        r2 = self.L.reserve("t2", _pay("s2"), run_id="run-2", business_target="order:1")
        self.assertFalse(r2["ok"], r2)
        self.assertEqual(r2["code"], SP.SP_ACT_009)

    def test_t04_same_target_diff_digest(self):
        r1 = self.L.reserve("t1", _pay("s1"), business_target="order:1")
        self.assertTrue(r1["ok"], r1)
        r2 = self.L.reserve("t2", _pay("s2"), seq=1, business_target="order:1")
        self.assertFalse(r2["ok"], r2)  # 新 seq → 新 digest/action_id，仍拒
        self.assertEqual(r2["code"], SP.SP_ACT_009)

    def test_t05_target_normalized_variants_one_success(self):
        r1 = self.L.reserve("t1", _pay("s1"), business_target="Order:1")
        self.assertTrue(r1["ok"], r1)
        for i, v in enumerate(("order:1", "ORDER:1", " order:1 ")):
            r = self.L.reserve("v-%d" % i, _pay("v%d" % i), business_target=v)
            self.assertFalse(r["ok"], (v, r))
            self.assertEqual(r["code"], SP.SP_ACT_009, v)

    def test_t06_diff_target_pass(self):
        r1 = self.L.reserve("t1", _pay("s1"), business_target="order:1")
        r2 = self.L.reserve("t2", _pay("s2"), business_target="order:2")
        self.assertTrue(r1["ok"], r1)
        self.assertTrue(r2["ok"], r2)

    def test_t07_target_missing_side_effect_fail_closed(self):
        r = self.L.reserve("t1", _pay("s1"), side_effect=True, business_target=None)
        self.assertEqual(r["code"], SP.SP_ACT_010, r)
        r = self.L.reserve("t1", _pay("s1"), side_effect=True, business_target="   ")
        self.assertEqual(r["code"], SP.SP_ACT_010, r)
        r = self.L.reserve("t1", _pay("s1"), side_effect=True, business_target=42)
        self.assertEqual(r["code"], SP.SP_ACT_010, r)
        # 只读档（默认）不强制 target，也不触发 L-biz（P1S2-A 案例 10，向后兼容）
        r = self.L.reserve("t1", _pay("s1"))
        self.assertTrue(r["ok"], r)

    def test_t07b_gate_dispatch_refuses_without_target(self):
        import execution_gate
        r = self.L.reserve("t1", _pay("s1"))  # 只读 reserve，无 target
        aid = r["data"]["action_id"]
        g = execution_gate.ExecutionGate(os.path.join(self.d, "l.lock"), self.L, None,
                                         ownership_probe=lambda tid: "agent")
        out = g.dispatch({"fencing_generation": 1, "executor_id": "e1"},
                         "noop", lambda op, a, c: {"ok": True}, {},
                         action_id=aid, require_business_target=True)
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["code"], SP.SP_ACT_010, out)

    def test_t08_unknown_then_new_intent_refused(self):
        r1 = self.L.reserve("t1", _pay("s1"), business_target="order:1")
        self._to_unknown(r1["data"]["action_id"])
        r2 = self.L.reserve("t2", _pay("s2"), business_target="order:1")
        self.assertFalse(r2["ok"], r2)
        self.assertEqual(r2["code"], SP.SP_ACT_009)
        self.assertEqual(r2["data"]["holder_state"], "SEND_UNKNOWN")

    def test_t09_authorization_ref_does_not_bypass(self):
        r1 = self.L.reserve("t1", _pay("s1"), business_target="order:1")
        self._to_unknown(r1["data"]["action_id"])
        r2 = self.L.reserve("t2", _pay("s2"), business_target="order:1",
                            authorization_ref="audit-20261114-001")
        self.assertFalse(r2["ok"], r2)
        self.assertEqual(r2["code"], SP.SP_ACT_009, r2)
        # ref 仅作审计记录；原 UNKNOWN action 原样保留；未产生新有效 action
        self.assertTrue(r2["data"].get("authorization_ref_recorded") is True)
        self.assertIsNone(self.L.get(r2["data"].get("action_id")))
        self.assertEqual(self.L.business_risk("order:1")["count"], 1)


# --------------------------------------------------------------- T10-T11
class TestThreeLockIndependence(_Base):
    def test_t10_lint_release_keeps_lbiz(self):
        # A(target X) UNKNOWN；另一意图（target Y）走到终态 —— L-int/L-opd
        # 的变化不得触碰 X 的 L-biz
        ra = self.L.reserve("tA", _pay("X"), business_target="X")
        self._to_unknown(ra["data"]["action_id"])
        rb = self.L.reserve("tA", _pay("Y"), budget_units=5, business_target="Y")
        self.assertTrue(rb["ok"], rb)
        rc = self.L.commit_outcome(rb["data"]["action_id"], "NOT_SENT",
                                   evidence="pre-dispatch crash")
        self.assertTrue(rc["ok"], rc)
        # Y 的意图已终态（L-int 解除），X 的 L-biz 必须原样保持
        self.assertEqual(self.L.business_risk("X")["count"], 1)
        r2 = self.L.reserve("tC", _pay("X-new"), business_target="X")
        self.assertEqual(r2["code"], SP.SP_ACT_009, r2)

    def test_t11_lopd_dup_attempt_keeps_lbiz(self):
        # 同 (task, intent, seq) 重复提交（L-opd 冲突 SP-ACT-001）不得释放 L-biz
        ra = self.L.reserve("tA", _pay("X"), business_target="X")
        self.assertTrue(ra["ok"], ra)
        self._to_unknown(ra["data"]["action_id"])
        rd = self.L.reserve("tA", _pay("X"))  # 同 identity → SP-ACT-001
        self.assertEqual(rd["code"], SP.SP_ACT_001, rd)
        self.assertEqual(self.L.business_risk("X")["count"], 1)
        r2 = self.L.reserve("tB", _pay("X2"), business_target="X")
        self.assertEqual(r2["code"], SP.SP_ACT_009, r2)


# --------------------------------------------------------------- T12 独立连接并发
class TestConcurrentConnections(_Base):
    def test_t12_threaded_independent_connections(self):
        rounds, workers = 100, 8  # GPT §三：≥100 轮并发竞争
        for rnd in range(rounds):
            db = os.path.join(self.d, "r%d.db" % rnd)
            # 单连接预初始化 schema，避免并发建表竞态；**并在整轮内保持常开**。
            # P2-2-4 实测（100 轮复现 + 200 轮对照实验）：若初始化连接在本轮
            # 开始前关闭，Windows 上「最后一个连接关闭 → WAL/-shm 被回收 →
            # 8 线程同时首次写入」会触发 SQLite 瞬态 "attempt to write a
            # readonly database"（BEGIN IMMEDIATE 直接失败，非 busy 超时）；
            # 常开连接使 WAL 持续存活时 200 轮 0 次异常。这是**测试夹具**
            # 的建库竞态，不是账本语义问题——常开后仍严格要求「恰 1 赢家」。
            _pre = AL.ActionLedger(db)
            results = []
            lock = threading.Lock()
            def worker(i):
                lg = AL.ActionLedger(db)
                try:
                    r = lg.reserve("task-%d" % i, _pay("w%d" % i),
                                   business_target="order:%d" % rnd)
                except Exception as e:  # 基础设施异常也显式计为结果，绝不静默缺失
                    r = {"ok": False, "code": "EXC:" + type(e).__name__,
                         "err": str(e)[:120]}
                finally:
                    lg.close()
                with lock:
                    results.append(r)
            ts = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(120)
            _pre.close()
            ok = [r for r in results if r["ok"]]
            self.assertEqual(len(results), workers, "round %d: missing results" % rnd)
            self.assertEqual(len(ok), 1,
                             "round %d: %d succeeded (expect 1)" % (rnd, len(ok)))
            winner = ok[0]
            losers = [r for r in results if not r["ok"]]
            # 输家只能 SP-ACT-009（L-biz 唯一索引/查询裁决），不得出现其它成功形态
            self.assertTrue(all(r["code"] == SP.SP_ACT_009 for r in losers),
                            "round %d codes: %s" % (rnd, [r["code"] for r in losers]))
            # 每轮有效业务对象锁最多一个
            lg = AL.ActionLedger(db)
            try:
                self.assertEqual(lg.business_risk("order:%d" % rnd)["count"], 1)
            finally:
                lg.close()


# --------------------------------------------------------------- T13 独立进程并发
CHILD_SRC = (
    "import sys, json\n"
    "sys.path.insert(0, r'{scripts}')\n"
    "import actionledger as AL\n"
    "db, target, task, scope = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]\n"
    "L = AL.ActionLedger(db)\n"
    "r = L.reserve(task, {{'action': 'send_reply', 'scope': scope}},\n"
    "              business_target=target, side_effect=True)\n"
    "print(json.dumps({{'ok': r['ok'], 'code': r['code']}}))\n"
)


class TestConcurrentProcesses(_Base):
    def test_t13_subprocess_concurrent_reserve(self):
        child = CHILD_SRC.format(scripts=SCRIPTS)
        rounds, workers = 6, 8
        for rnd in range(rounds):
            db = os.path.join(self.d, "p%d.db" % rnd)
            _pre = AL.ActionLedger(db)  # 单连接预初始化 schema
            _pre.close()
            target = "proc-order:%d" % rnd
            procs = [subprocess.Popen(
                [sys.executable, "-c", child, db, target,
                 "ptask-%d-%d" % (rnd, i), "ps%d" % i],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", cwd=SCRIPTS) for i in range(workers)]
            results = []
            for p in procs:
                out, err = p.communicate(120)
                if p.returncode != 0:
                    self.fail("worker rc=%s err=%s" % (p.returncode, err[-300:]))
                results.append(json.loads(out.strip().splitlines()[-1]))
            ok = [r for r in results if r["ok"]]
            self.assertEqual(len(ok), 1,
                             "round %d: %d succeeded (expect 1): %s"
                             % (rnd, len(ok), results))
            self.assertTrue(
                all(r["code"] in (SP.SP_ACT_009, SP.SP_ACT_002)
                    for r in results if not r["ok"]),
                "round %d unexpected codes: %s" % (rnd, results))


# --------------------------------------------------------------- T14 事务原子性
class TestTransactionAtomicity(_Base):
    def test_t14_no_partial_success_on_event_failure(self):
        r1 = self.L.reserve("t0", _pay("warm"), business_target="warm-target")
        self.assertTrue(r1["ok"], r1)
        aid = r1["data"]["action_id"]
        # 注入：事件写入失败（模拟数据库异常）
        orig = self.L._ev
        calls = {"n": 0}
        def boom(a, k, d=""):
            calls["n"] += 1
            raise sqlite3.OperationalError("injected event failure")
        self.L._ev = boom
        try:
            with self.assertRaises(sqlite3.OperationalError):
                self.L.reserve("t1", _pay("s1"), business_target="order:1")
        finally:
            self.L._ev = orig
        try:
            self.L._conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass  # 无活跃事务
        # 无部分成功：actions 无行、events 无 reserved 事件
        self.assertIsNone(self.L.get(aid + "-x"))
        row = self.L._conn.execute(
            "SELECT COUNT(*) FROM actions WHERE intent_key=?",
            (AL.intent_key_of(_pay("s1")),)).fetchone()[0]
        self.assertEqual(row, 0, "partial insert survived rollback")
        ev = self.L._conn.execute(
            "SELECT COUNT(*) FROM events WHERE action_id LIKE 'a-%' AND kind='reserved'"
        ).fetchone()[0]
        # warm-up 的 reserved 事件在（1 条），失败事务的 0 条
        self.assertEqual(ev, 1)
        # 事务干净后同 intent 重新 reserve 成功（未留下幽灵行占用唯一索引）
        r2 = self.L.reserve("t1", _pay("s1"), business_target="order:1")
        self.assertTrue(r2["ok"], r2)

    def test_t14b_integrityerror_loser_leaves_no_row(self):
        # 直接制造唯一索引冲突：手工插入同 norm 活跃行，再 reserve → 分流拒绝且无新行
        self.L._conn.execute(
            "INSERT INTO actions(action_id, task_id, intent_key, intent_digest,"
            " intent_payload, seq, state, business_target, business_target_norm,"
            " created_at, updated_at) VALUES ('a-manual','tM','ik-manual','dg','{}',"
            "0,'RESERVED','order:1','order:1',1,1)")
        self.L._conn.commit()
        r = self.L.reserve("t1", _pay("s1"), business_target="order:1")
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["code"], SP.SP_ACT_009, r)
        n = self.L._conn.execute(
            "SELECT COUNT(*) FROM actions WHERE intent_key=?",
            (AL.intent_key_of(_pay("s1")),)).fetchone()[0]
        self.assertEqual(n, 0)


# --------------------------------------------------------------- T15-T16 迁移
class TestMigration(_Base):
    def _make_legacy_conflict(self, db):
        lg = AL.ActionLedger(db)
        lg.close()
        conn = sqlite3.connect(db)
        # 模拟旧库：两行活跃态、无 norm 列值（历史数据），规范化后同键
        for i, (aid, ik, tgt) in enumerate([
                ("a-old1", "ik-old1", "Order:1"), ("a-old2", "ik-old2", "order:1 ")]):
            conn.execute(
                "INSERT INTO actions(action_id, task_id, intent_key, intent_digest,"
                " intent_payload, seq, state, business_target, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?, 'RESERVED', ?, 1, 1)",
                (aid, "tL%d" % i, ik, "dg", "{}", 0, tgt))
        conn.commit()
        conn.close()

    def test_t15_migration_conflict_blocks_and_preserves(self):
        db = os.path.join(self.d, "conflict.db")
        self._make_legacy_conflict(db)
        with self.assertRaises(AL.MigrationBlocked) as cm:
            AL.ActionLedger(db)
        conflicts = cm.exception.conflicts
        self.assertEqual(len(conflicts), 1, conflicts)
        self.assertEqual(conflicts[0]["n"], 2)
        # 原始记录与证据链原样保留（禁止自动删除/合并/覆盖）
        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT action_id, business_target, business_target_norm FROM actions "
            "WHERE state='RESERVED' ORDER BY action_id").fetchall()
        conn.close()
        self.assertEqual(len(rows), 2, rows)
        self.assertEqual({r[0] for r in rows}, {"a-old1", "a-old2"})
        # 迁移被阻止：norm 回填未执行（两段式预检先于回填），原始行原样保留
        self.assertEqual({r[2] for r in rows}, {None})

    def test_t15b_legacy_variant_rows_get_normalized_on_backfill(self):
        db = os.path.join(self.d, "legacy.db")
        self._make_legacy_conflict(db)  # 复用造数（含冲突）
        # 无冲突旧库：只放一行历史
        db2 = os.path.join(self.d, "legacy2.db")
        lg = AL.ActionLedger(db2)
        lg.close()
        conn = sqlite3.connect(db2)
        conn.execute(
            "INSERT INTO actions(action_id, task_id, intent_key, intent_digest,"
            " intent_payload, seq, state, business_target, created_at, updated_at)"
            " VALUES ('a-legacy','tL','ik-legacy','dg','{}',0,'SEND_UNKNOWN','Order:9',1,1)")
        conn.commit()
        conn.close()
        lg2 = AL.ActionLedger(db2)  # 迁移成功
        try:
            # 历史变体行已回填 norm，且占住 L-biz
            self.assertEqual(lg2.business_risk("order:9")["count"], 1)
            r = lg2.reserve("tN", _pay("s1"), business_target="ORDER:9")
            self.assertEqual(r["code"], SP.SP_ACT_009, r)
        finally:
            lg2.close()

    def test_t16_repeat_migration_idempotent(self):
        db = os.path.join(self.d, "idem.db")
        lg = AL.ActionLedger(db)
        r = lg.reserve("t1", _pay("s1"), business_target="order:1")
        self.assertTrue(r["ok"], r)
        lg.close()
        # 重复 init（模拟重复迁移）：不报错、不破坏数据、索引不重复创建报错
        for _ in range(3):
            lg2 = AL.ActionLedger(db)
            try:
                self.assertEqual(lg2.business_risk("ORDER:1")["count"], 1)
                r2 = lg2.reserve("t2", _pay("s2"), business_target="order:1")
                self.assertEqual(r2["code"], SP.SP_ACT_009, r2)
            finally:
                lg2.close()
        idx = sqlite3.connect(db).execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='ux_biz_active'"
        ).fetchall()
        self.assertEqual(len(idx), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
