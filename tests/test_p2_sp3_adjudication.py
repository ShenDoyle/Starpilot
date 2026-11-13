#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SP3-1 专项验收：裁决协议（adjudications / adjudication_evidence）。

设计冻结依据：SP3-0R / SP3-0R2 / SP3-0R2-FINAL / SP3-0R3-FINAL（均经终审），
决议 D01–D14。本文件逐条覆盖 GPT 任务书 C 段「强制测试矩阵」：

  生命周期    —— 未封存消费 / 未封存撤销 / 封存与消费同一条 UPDATE
  不可变性    —— 主表 16 个业务字段、生命周期重复改写、DELETE
  REPLACE     —— 主表与证据表，recursive_triggers ON/OFF 各一组
  证据封存    —— 空证据封存 / 封存后增删改 / 直接插入已封存行
  supersedes  —— 不存在 / 跨 action / 未封存 / 自引用 / 循环 / 合法链
  事务原子性  —— 失败后主表与证据表零残留
  并发        —— 同一 action 并发裁决、同一裁决并发消费
  外键        —— 生产连接开启、孤儿证据被拒、连接级语义独立
  回滚        —— 有自引用数据时 FK=ON 失败 / 维护连接 FK=OFF 成功

另含 J 组「协议不变量」，用于守住 P0 分工与 G6-HOTFIX 未被削弱。

设计前提（测试本身不得违反）：
- 本文件**不修改**生产代码，只通过公开 API + 直连 SQL 施加压力。
- 直连 SQL 用独立连接（`_raw`），生产连接的 PRAGMA 不被测试污染。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import actionledger  # noqa: E402
import sp_errors as SP  # noqa: E402

REASON = "manual review of the send evidence confirms the outcome"
EVID = ("manual_attestation", "loc-default", 1700000000000)
OPERATOR = "operator-fan"
AUTH = "AUTH-SP3-1-UNIT"


class AdjBase(unittest.TestCase):
    """公共夹具：真实账本 + 真实恢复路径产出 SEND_UNKNOWN。"""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="sp-sp31-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.db = os.path.join(self.d, "ledger.sqlite")
        self.L = actionledger.ActionLedger(self.db)

    def tearDown(self):
        self.L.close()

    # ------------------------------------------------------------ 夹具
    @staticmethod
    def _intent(target, tag=""):
        return {"action": "send_message", "target": target, "text": "hi" + tag}

    def to_unknown(self, tag, fencing=1):
        """RESERVED → DISPATCHED → SEND_UNKNOWN（走真实恢复路径，不手改状态）。"""
        target = "bt-" + tag
        r = self.L.reserve("t-" + tag, self._intent(target, tag),
                           budget_units=10, business_target=target)
        self.assertTrue(r["ok"], r)
        aid = r["data"]["action_id"]
        self.assertTrue(self.L.mark_dispatched(aid, fencing, "exec-sp31")["ok"])
        self.assertGreaterEqual(self.L.recover_unknown()["data"]["count"], 1)
        row = self.L.get(aid)
        self.assertEqual(row["state"], "SEND_UNKNOWN")
        return aid

    def evidence(self, tag, ms=None, typ="manual_attestation"):
        return (typ, "loc-" + tag, int(ms if ms is not None else time.time() * 1000) - 1000)

    def decide(self, aid, tag, decision="DECIDED_SENT", fencing=1, **kw):
        kw.setdefault("evidence", [self.evidence(tag)])
        kw.setdefault("operator", OPERATOR)
        kw.setdefault("authorization_ref", AUTH)
        kw.setdefault("reason", REASON)
        kw.setdefault("scope", "action:" + aid)
        kw.setdefault("expected_fencing", fencing)
        return self.L.decide_adjudication(aid, decision, **kw)

    def sealed(self, tag, decision="DECIDED_SENT", fencing=1, **kw):
        aid = kw.pop("aid", None) or self.to_unknown(tag, fencing=fencing)
        r = self.decide(aid, tag, decision=decision, fencing=fencing, **kw)
        self.assertTrue(r["ok"], r)
        return aid, r["data"]["adjudication_id"]

    # -------------------------------------------------------- 直连辅助
    def _raw(self, foreign_keys=False, recursive_triggers=False):
        c = sqlite3.connect(self.db, isolation_level=None)
        c.execute("PRAGMA foreign_keys=%s" % ("ON" if foreign_keys else "OFF"))
        c.execute("PRAGMA recursive_triggers=%s" % ("ON" if recursive_triggers else "OFF"))
        self.addCleanup(c.close)
        return c

    def count(self, table):
        return int(self.L._conn.execute(
            "SELECT COUNT(*) FROM %s" % table).fetchone()[0])

    def row(self, adj_id):
        return self.L._conn.execute(
            "SELECT * FROM adjudications WHERE adjudication_id=?",
            (adj_id,)).fetchone()

    def raw_insert_adj(self, c, adj_id, action_id, sealed=None,
                       supersedes=None, decision="DECIDED_SENT",
                       or_replace=False, **over):
        """直连插入一条裁决（默认未封存），用于构造 API 不会产出的状态。"""
        act = self.L.get(action_id)
        now = time.time()
        vals = {
            "adjudication_id": adj_id,
            "action_id": action_id,
            "intent_key": act["intent_key"],
            "business_target_norm": act["business_target_norm"] or "",
            "fencing_generation": int(act["fencing_generation"] or 0),
            "decision": decision,
            "evidence_hash": "0" * 64,
            "operator": OPERATOR,
            "reviewer": ("reviewer-x" if decision == "DECIDED_NOT_SENT" else None),
            "authorization_ref": AUTH,
            "reason": REASON,
            "scope": "action:" + action_id,
            "decided_at": now - 5,
            "reviewed_at": (now - 5 if decision == "DECIDED_NOT_SENT" else None),
            "supersedes_id": supersedes,
            "created_at": now - 5,
            "sealed_at": sealed,
        }
        vals.update(over)
        cols = ",".join(vals)
        qs = ",".join("?" * len(vals))
        verb = "INSERT OR REPLACE" if or_replace else "INSERT"
        c.execute("%s INTO adjudications(%s) VALUES (%s)" % (verb, cols, qs),
                  tuple(vals.values()))
        return adj_id

    def raw_add_evidence(self, c, adj_id, seq=1, locator="loc-raw",
                         typ="page_observation", ms=1700000000000):
        c.execute("INSERT INTO adjudication_evidence"
                  "(adjudication_id, seq, type, locator, captured_at_ms)"
                  " VALUES (?,?,?,?,?)", (adj_id, seq, typ, locator, ms))

    def raw_seal(self, c, adj_id):
        c.execute("UPDATE adjudications SET sealed_at=? WHERE adjudication_id=?",
                  (time.time(), adj_id))


# ======================================================= A 生命周期（D05）
class TestA_Lifecycle(AdjBase):
    def test_a1_consume_before_seal_rejected_by_check(self):
        """未封存却写 consumed_* —— CHECK(consumed_at IS NULL OR sealed_at IS NOT NULL)。"""
        aid = self.to_unknown("a1")
        c = self._raw()
        self.raw_insert_adj(c, "adj-a1", aid, sealed=None)
        self.raw_add_evidence(c, "adj-a1")
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            c.execute("UPDATE adjudications SET consumed_at=?, consumed_by=?"
                      " WHERE adjudication_id=?", (time.time(), OPERATOR, "adj-a1"))
        self.assertIn("CHECK constraint failed", str(ctx.exception))
        self.assertIsNone(self.row("adj-a1")["consumed_at"])

    def test_a2_revoke_before_seal_rejected_by_check(self):
        aid = self.to_unknown("a2")
        c = self._raw()
        self.raw_insert_adj(c, "adj-a2", aid, sealed=None)
        self.raw_add_evidence(c, "adj-a2")
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            c.execute("UPDATE adjudications SET revoked_at=?, revoked_by=?,"
                      " revocation_reason=? WHERE adjudication_id=?",
                      (time.time(), OPERATOR, REASON, "adj-a2"))
        self.assertIn("CHECK constraint failed", str(ctx.exception))
        self.assertIsNone(self.row("adj-a2")["revoked_at"])

    def test_a3_seal_and_consume_in_one_update_rejected(self):
        """同一条 UPDATE 既封存又消费 —— trg_adj_seal_exclusive（P1c）。"""
        aid = self.to_unknown("a3")
        c = self._raw()
        self.raw_insert_adj(c, "adj-a3", aid, sealed=None)
        self.raw_add_evidence(c, "adj-a3")
        now = time.time()
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            c.execute("UPDATE adjudications SET sealed_at=?, consumed_at=?,"
                      " consumed_by=? WHERE adjudication_id=?",
                      (now, now, OPERATOR, "adj-a3"))
        self.assertIn("seal_must_be_separate_from_lifecycle", str(ctx.exception))
        r = self.row("adj-a3")
        self.assertIsNone(r["sealed_at"])
        self.assertIsNone(r["consumed_at"])

    def test_a4_api_refuses_unsealed_adjudication(self):
        """API 层同样不放行未封存裁决（DB 造态，API 消费）。"""
        aid = self.to_unknown("a4")
        c = self._raw()
        self.raw_insert_adj(c, "adj-a4", aid, sealed=None)
        self.raw_add_evidence(c, "adj-a4")
        r = self.L.consume_adjudication(aid, "adj-a4", "SENT", expected_fencing=1)
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["code"], SP.SP_ACT_016)
        self.assertEqual(r["evidence"], "adjudication not sealed")
        self.assertEqual(self.L.get(aid)["state"], "SEND_UNKNOWN")

    def test_a5_consumed_and_revoked_are_mutually_exclusive(self):
        """已消费不得再撤销；已撤销不得再消费（SP-ACT-014）。"""
        aid, adj = self.sealed("a5")
        self.assertTrue(self.L.consume_adjudication(
            aid, adj, "SENT", expected_fencing=1, consumed_by=OPERATOR)["ok"])
        r = self.L.revoke_adjudication(adj, OPERATOR, REASON)
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["code"], SP.SP_ACT_014)

        aid2, adj2 = self.sealed("a5b")
        self.assertTrue(self.L.revoke_adjudication(adj2, OPERATOR, REASON)["ok"])
        r = self.L.consume_adjudication(aid2, adj2, "SENT", expected_fencing=1)
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["code"], SP.SP_ACT_014)


# ==================================================== B 不可变性（D02/D04）
IMMUTABLE_COLS = {
    "adjudication_id": "other-id", "action_id": "other-act",
    "intent_key": "k2", "business_target_norm": "other-bt",
    "fencing_generation": 99, "decision": "DEFER",
    "evidence_hash": "1" * 64, "operator": "other-op",
    "reviewer": "other-rev", "authorization_ref": "other-auth",
    "reason": "another reason that is long enough to pass",
    "scope": "other-scope", "decided_at": 1.0, "reviewed_at": 2.0,
    "supersedes_id": "adj-x", "created_at": 3.0,
}


class TestB_Immutability(AdjBase):
    def test_b1_all_business_columns_immutable(self):
        _aid, adj = self.sealed("b1")
        c = self._raw(foreign_keys=False)
        for col, val in IMMUTABLE_COLS.items():
            with self.assertRaises(sqlite3.IntegrityError, msg=col) as ctx:
                c.execute("UPDATE adjudications SET %s=? WHERE adjudication_id=?"
                          % col, (val, adj))
            self.assertIn("adjudication_immutable_field", str(ctx.exception), col)

    def test_b2_sealed_at_irreversible(self):
        _aid, adj = self.sealed("b2")
        c = self._raw()
        for bad in (None, self.row(adj)["sealed_at"] - 1.0,
                    self.row(adj)["sealed_at"] + 100.0):
            with self.assertRaises(sqlite3.IntegrityError) as ctx:
                c.execute("UPDATE adjudications SET sealed_at=? WHERE adjudication_id=?",
                          (bad, adj))
            self.assertIn("adjudication_immutable_field", str(ctx.exception))

    def test_b3_lifecycle_written_once_only(self):
        """consumed_* / revoked_* 一旦落值不得再改写。"""
        aid, adj = self.sealed("b3")
        self.assertTrue(self.L.consume_adjudication(
            aid, adj, "SENT", expected_fencing=1)["ok"])
        c = self._raw()
        first = self.row(adj)["consumed_at"]
        for sql, params in (
            ("UPDATE adjudications SET consumed_at=? WHERE adjudication_id=?",
             (first + 5, adj)),
            ("UPDATE adjudications SET consumed_by=? WHERE adjudication_id=?",
             ("attacker", adj)),
        ):
            with self.assertRaises(sqlite3.IntegrityError) as ctx:
                c.execute(sql, params)
            self.assertIn("adjudication_immutable_field", str(ctx.exception))

    def test_b4_main_table_delete_forbidden(self):
        _aid, adj = self.sealed("b4")
        c = self._raw()
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            c.execute("DELETE FROM adjudications WHERE adjudication_id=?", (adj,))
        self.assertIn("adjudication_append_only", str(ctx.exception))
        self.assertEqual(self.count("adjudications"), 1)

    def test_b5_evidence_update_delete_forbidden(self):
        _aid, adj = self.sealed("b5")
        c = self._raw()
        for sql in ("UPDATE adjudication_evidence SET locator='x'"
                    " WHERE adjudication_id=?",
                    "DELETE FROM adjudication_evidence WHERE adjudication_id=?"):
            with self.assertRaises(sqlite3.IntegrityError) as ctx:
                c.execute(sql, (adj,))
            self.assertIn("evidence_immutable", str(ctx.exception))
        self.assertEqual(self.count("adjudication_evidence"), 1)


# ============================================ C REPLACE 绕过（D13，P1-2）
class TestC_ReplaceBypass(AdjBase):
    """`INSERT OR REPLACE` 在 recursive_triggers=OFF 时会先删冲突行**且不触发
    DELETE 触发器**。这是实测确认的真实绕过面，因此生产连接强制 ON。
    本组同时记录 OFF 下的绕过后果（证明「必须 ON」不是假设）。"""

    def test_c1_replace_main_table_off_leaks(self):
        aid = self.to_unknown("c1")
        c = self._raw(recursive_triggers=False)
        self.raw_insert_adj(c, "adj-c1", aid, sealed=None)
        self.raw_add_evidence(c, "adj-c1")
        self.raw_seal(c, "adj-c1")
        before = self.row("adj-c1")
        self.assertIsNotNone(before["sealed_at"])
        # OFF：整行被替换，封存状态被抹掉（失败开放，故生产必须 ON）
        self.raw_insert_adj(c, "adj-c1", aid, sealed=None, operator="attacker",
                            or_replace=True)
        after = self.row("adj-c1")
        self.assertEqual(after["operator"], "attacker")
        self.assertIsNone(after["sealed_at"])

    def test_c2_replace_main_table_on_blocked(self):
        aid = self.to_unknown("c2")
        c = self._raw(recursive_triggers=True)
        self.raw_insert_adj(c, "adj-c2", aid, sealed=None)
        self.raw_add_evidence(c, "adj-c2")
        self.raw_seal(c, "adj-c2")
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            self.raw_insert_adj(c, "adj-c2", aid, sealed=None, operator="attacker",
                                or_replace=True)
        self.assertIn("adjudication_append_only", str(ctx.exception))
        self.assertEqual(self.row("adj-c2")["operator"], OPERATOR)

    def test_c3_replace_evidence_off_leaks(self):
        """未封存证据行：OFF 下 DELETE 触发器不触发 → 内容被整体替换。"""
        aid = self.to_unknown("c3")
        c = self._raw(recursive_triggers=False)
        self.raw_insert_adj(c, "adj-c3", aid, sealed=None)
        self.raw_add_evidence(c, "adj-c3", locator="orig-locator")
        c.execute("INSERT OR REPLACE INTO adjudication_evidence"
                  "(adjudication_id, seq, type, locator, captured_at_ms)"
                  " VALUES (?,1,'manual_attestation','HACKED',1700000000001)",
                  ("adj-c3",))
        got = c.execute("SELECT locator FROM adjudication_evidence"
                        " WHERE adjudication_id='adj-c3' AND seq=1").fetchone()[0]
        self.assertEqual(got, "HACKED")

    def test_c4_replace_evidence_on_blocked(self):
        aid = self.to_unknown("c4")
        c = self._raw(recursive_triggers=True)
        self.raw_insert_adj(c, "adj-c4", aid, sealed=None)
        self.raw_add_evidence(c, "adj-c4", locator="orig-locator")
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            c.execute("INSERT OR REPLACE INTO adjudication_evidence"
                      "(adjudication_id, seq, type, locator, captured_at_ms)"
                      " VALUES (?,1,'manual_attestation','HACKED',1700000000001)",
                      ("adj-c4",))
        self.assertIn("evidence_immutable", str(ctx.exception))
        got = c.execute("SELECT locator FROM adjudication_evidence"
                        " WHERE adjudication_id='adj-c4' AND seq=1").fetchone()[0]
        self.assertEqual(got, "orig-locator")

    def test_c5_sealed_evidence_replace_blocked_on_both(self):
        """已封存证据行：INSERT 侧触发器独立生效，ON/OFF 都拒（双重防线）。"""
        for rec in (False, True):
            aid = self.to_unknown("c5-%d" % rec)     # 独立 action，避开唯一槽位
            adj = "adj-c5-%d" % rec
            c = self._raw(recursive_triggers=rec)
            self.raw_insert_adj(c, adj, aid, sealed=None)
            self.raw_add_evidence(c, adj, locator="orig")
            self.raw_seal(c, adj)
            with self.assertRaises(sqlite3.IntegrityError) as ctx:
                c.execute("INSERT OR REPLACE INTO adjudication_evidence"
                          "(adjudication_id, seq, type, locator, captured_at_ms)"
                          " VALUES (?,1,'manual_attestation','HACKED',1700000000001)",
                          (adj,))
            self.assertIn("evidence_sealed", str(ctx.exception), rec)

    def test_c6_production_connection_has_triggers_on(self):
        """生产连接必须 ON：否则 c1/c3 两条 leak 用例就是生产行为。"""
        for v in ("foreign_keys", "recursive_triggers"):
            got = int(self.L._conn.execute("PRAGMA %s" % v).fetchone()[0])
            self.assertEqual(got, 1, v)


# ==================================================== D 证据封存（D01/D03）
class TestD_SealProtocol(AdjBase):
    def test_d1_seal_requires_at_least_one_evidence(self):
        aid = self.to_unknown("d1")
        c = self._raw()
        self.raw_insert_adj(c, "adj-d1", aid, sealed=None)
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            self.raw_seal(c, "adj-d1")
        self.assertIn("seal_requires_evidence", str(ctx.exception))
        self.assertIsNone(self.row("adj-d1")["sealed_at"])

    def test_d2_insert_with_sealed_at_rejected(self):
        aid = self.to_unknown("d2")
        c = self._raw()
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            self.raw_insert_adj(c, "adj-d2", aid, sealed=time.time())
        self.assertIn("must_seal_after_evidence", str(ctx.exception))
        self.assertIsNone(self.row("adj-d2"))

    def test_d3_no_evidence_insert_after_seal(self):
        _aid, adj = self.sealed("d3")
        c = self._raw()
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            self.raw_add_evidence(c, adj, seq=2, locator="late")
        self.assertIn("evidence_sealed", str(ctx.exception))
        self.assertEqual(self.count("adjudication_evidence"), 1)

    def test_d4_api_requires_non_empty_evidence(self):
        aid = self.to_unknown("d4")
        for bad in ([], (), None, "str"):
            r = self.decide(aid, "d4", evidence=bad)
            self.assertFalse(r["ok"], bad)
            self.assertEqual(r["code"], SP.SP_ACT_015, bad)
        self.assertEqual(self.count("adjudications"), 0)
        self.assertEqual(self.count("adjudication_evidence"), 0)

    def test_d5_evidence_hash_bound_to_persisted_set(self):
        """主表摘要必须由**落库的证据集合**复算得出（D14）。"""
        aid = self.to_unknown("d5")
        ev = [self.evidence("d5a"), self.evidence("d5b", typ="backend_receipt")]
        r = self.decide(aid, "d5", evidence=ev)
        self.assertTrue(r["ok"], r)
        rows = self.L._conn.execute(
            "SELECT seq, type, locator, captured_at_ms FROM adjudication_evidence"
            " WHERE adjudication_id=? ORDER BY seq",
            (r["data"]["adjudication_id"],)).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(actionledger._adj_hash([tuple(x) for x in rows]),
                         self.row(r["data"]["adjudication_id"])["evidence_hash"])


# ================================================== E supersedes（D11）
class TestE_Supersedes(AdjBase):
    def test_e1_missing_target(self):
        aid = self.to_unknown("e1")
        r = self.decide(aid, "e1", supersedes_id="adj-nope")
        self.assertFalse(r["ok"])
        self.assertEqual(r["evidence"], "supersedes target missing")
        self.assertEqual(self.count("adjudications"), 0)

    def test_e2_cross_action(self):
        aid1, _adj1 = self.sealed("e2a")
        aid2 = self.to_unknown("e2b")
        r = self.decide(aid2, "e2b", supersedes_id=_adj1)
        self.assertFalse(r["ok"])
        self.assertEqual(r["evidence"], "supersedes must reference the same action_id")

    def test_e3_unsealed_target(self):
        aid = self.to_unknown("e3")
        c = self._raw()
        self.raw_insert_adj(c, "adj-e3-open", aid, sealed=None)
        self.raw_add_evidence(c, "adj-e3-open")
        r = self.decide(aid, "e3", supersedes_id="adj-e3-open")
        self.assertFalse(r["ok"])
        self.assertEqual(r["evidence"], "supersedes target not sealed")

    def test_e4_self_reference_rejected_by_check(self):
        aid = self.to_unknown("e4")
        c = self._raw()
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            self.raw_insert_adj(c, "adj-e4", aid, sealed=None, supersedes="adj-e4")
        self.assertIn("CHECK constraint failed", str(ctx.exception))

    def test_e5_cycle_detected(self):
        """环只能由直连构造（API 永不产出），decide 必须拒绝接进环里。

        环跨两个 action 构造：同 action 的两行无法同时封存（唯一槽位），
        而链检本身不看中间节点归属 —— 正好覆盖「链深处不校验同 action」。
        """
        aid1 = self.to_unknown("e5a")
        aid2 = self.to_unknown("e5b")
        c = self._raw(foreign_keys=False)     # 互指需先插后补，FK=OFF 构造
        self.raw_insert_adj(c, "adj-cyc-a", aid1, sealed=None, supersedes="adj-cyc-b")
        self.raw_insert_adj(c, "adj-cyc-b", aid2, sealed=None, supersedes="adj-cyc-a")
        for a in ("adj-cyc-a", "adj-cyc-b"):
            self.raw_add_evidence(c, a)
            self.raw_seal(c, a)
        # 先退出槽位，才能走到链检（否则先被槽位检查拦下）
        for a in ("adj-cyc-a", "adj-cyc-b"):
            self.assertTrue(self.L.revoke_adjudication(a, OPERATOR, REASON)["ok"])
        r = self.decide(aid1, "e5c", supersedes_id="adj-cyc-a")
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["evidence"], "supersedes chain already cyclic")
        self.assertEqual(self.count("adjudications"), 2)

    def test_e6_supersede_requires_slot_released(self):
        """槽位唯一（D05）× 取代（D11）：旧裁决还占槽位时必须明确拒绝。"""
        aid, first = self.sealed("e6")
        r = self.decide(aid, "e6b", supersedes_id=first)
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["code"], SP.SP_ACT_015)
        self.assertIn("still holds the consumable slot", r["evidence"])
        self.assertTrue(r["data"]["occupies_slot"])
        self.assertEqual(self.count("adjudications"), 1)

    def test_e7_supersede_after_revoke_accepted(self):
        aid, first = self.sealed("e7")
        self.assertTrue(self.L.revoke_adjudication(first, OPERATOR, REASON)["ok"])
        r = self.decide(aid, "e7b", supersedes_id=first)
        self.assertTrue(r["ok"], r)
        second = r["data"]["adjudication_id"]
        self.assertEqual(self.row(second)["supersedes_id"], first)
        # 新裁决必须真正可用（否则「取代」只是摆设）
        ok = self.L.consume_adjudication(aid, second, "SENT", expected_fencing=1)
        self.assertTrue(ok["ok"], ok)
        self.assertEqual(self.L.get(aid)["state"], "COMPLETED")


# ================================================= F 事务原子性（零残留）
class TestF_Atomicity(AdjBase):
    def test_f1_duplicate_id_rolls_back_completely(self):
        aid1, adj1 = self.sealed("f1")
        aid2 = self.to_unknown("f1b")
        before_adj = self.count("adjudications")
        before_ev = self.count("adjudication_evidence")
        r = self.decide(aid2, "f1b", adjudication_id=adj1)
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["code"], SP.SP_ACT_015)
        self.assertEqual(self.count("adjudications"), before_adj)
        self.assertEqual(self.count("adjudication_evidence"), before_ev)

    def test_f2_precheck_failures_leave_no_residue(self):
        aid = self.to_unknown("f2")
        cases = [
            dict(decision="NOPE"),
            dict(operator=""),
            dict(operator="system"),
            dict(operator="AUTO"),
            dict(reason="too short"),
            dict(scope=""),
            dict(authorization_ref=""),
            dict(evidence=[("unknown_type", "loc", 1700000000000)]),
            dict(evidence=[("manual_attestation", "  ", 1700000000000)]),
            dict(evidence=[("manual_attestation", "loc", 0)]),
            dict(evidence=[("manual_attestation", "loc", 1700000000000)]),  # 窗口外
            dict(evidence=[("manual_attestation", "loc")]),                 # 缺列
        ]
        for kw in cases:
            r = self.decide(aid, "f2", **kw)
            self.assertFalse(r["ok"], kw)
            self.assertEqual(r["code"], SP.SP_ACT_015, kw)
        self.assertEqual(self.count("adjudications"), 0)
        self.assertEqual(self.count("adjudication_evidence"), 0)
        self.assertEqual(self.L.get(aid)["state"], "SEND_UNKNOWN")

    def test_f3_two_person_rule(self):
        aid = self.to_unknown("f3")
        r = self.decide(aid, "f3", decision="DECIDED_NOT_SENT",
                        reviewer=OPERATOR, reviewed_at=time.time())
        self.assertFalse(r["ok"])
        self.assertIn("reviewer must differ", r["evidence"])
        r = self.decide(aid, "f3", decision="DECIDED_NOT_SENT")
        self.assertFalse(r["ok"])
        self.assertIn("reviewer", r["evidence"])
        r = self.decide(aid, "f3", decision="DECIDED_NOT_SENT",
                        reviewer="reviewer-y", reviewed_at=time.time())
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.count("adjudications"), 1)


# ============================================================ G 并发
class TestG_Concurrency(AdjBase):
    def _race(self, fn, n=2):
        lock, out = threading.Lock(), []
        barrier = threading.Barrier(n)

        def worker(i):
            L2 = actionledger.ActionLedger(self.db)
            try:
                barrier.wait()
                r = fn(L2, i)
            finally:
                L2.close()
            with lock:
                out.append(r)

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=30)
        self.assertEqual(len(out), n)
        return out

    def test_g1_concurrent_decide_single_pending_slot(self):
        """同一 action 两方各自裁决 → 唯一槽位索引只放一个（D05）。"""
        aid = self.to_unknown("g1")
        ms = int(time.time() * 1000) - 1000
        res = self._race(lambda L2, i: L2.decide_adjudication(
            aid, "DECIDED_SENT",
            [("manual_attestation", "loc-g1-%d" % i, ms)],
            OPERATOR, AUTH, REASON, "action:" + aid, expected_fencing=1))
        wins = [r for r in res if r["ok"]]
        self.assertEqual(len(wins), 1, res)
        losers = [r for r in res if not r["ok"]]
        self.assertTrue(all(r["code"] == SP.SP_ACT_015 for r in losers), losers)
        self.assertEqual(self.count("adjudications"), 1)

    def test_g2_concurrent_consume_single_winner(self):
        aid, adj = self.sealed("g2")
        res = self._race(lambda L2, i: L2.consume_adjudication(
            aid, adj, "SENT", expected_fencing=1, consumed_by=OPERATOR))
        wins = [r for r in res if r["ok"]]
        self.assertEqual(len(wins), 1, res)
        losers = [r for r in res if not r["ok"]]
        # 先到的把 action 推成 COMPLETED，后到的看到 state not adjudicable
        # （SP-ACT-016）；若两者都读到 UNKNOWN，则由占位 UPDATE 决出 SP-ACT-014。
        self.assertTrue(all(r["code"] in (SP.SP_ACT_014, SP.SP_ACT_016)
                            for r in losers), losers)
        self.assertEqual(self.L.get(aid)["state"], "COMPLETED")


# ==================================================== H 外键与连接语义
class TestH_ForeignKeys(AdjBase):
    def test_h1_orphan_evidence_rejected_when_fk_on(self):
        c = self._raw(foreign_keys=True)
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            self.raw_add_evidence(c, "adj-ghost")
        self.assertIn("FOREIGN KEY constraint failed", str(ctx.exception))

    def test_h2_orphan_evidence_accepted_when_fk_off(self):
        """对照：连接级 OFF 时同一写入被放行 —— 证明 FK 是连接级语义。"""
        c = self._raw(foreign_keys=False)
        self.raw_add_evidence(c, "adj-ghost")
        self.assertEqual(int(c.execute("PRAGMA foreign_keys").fetchone()[0]), 0)
        self.assertEqual(self.count("adjudication_evidence"), 1)

    def test_h3_pragma_set_is_local_to_connection(self):
        c = self._raw(foreign_keys=False)
        self.assertEqual(int(c.execute("PRAGMA foreign_keys").fetchone()[0]), 0)
        self.assertEqual(int(self.L._conn.execute("PRAGMA foreign_keys").fetchone()[0]), 1)


# ==================================================== I 回滚（维护连接）
class TestI_Rollback(AdjBase):
    def _prepare_chain(self):
        """真实 API 造一条 supersedes 链（旧裁决先退出槽位，再被取代）。"""
        aid, first = self.sealed("i1")
        self.assertTrue(self.L.revoke_adjudication(first, OPERATOR, REASON)["ok"])
        r = self.decide(aid, "i1b", supersedes_id=first)
        self.assertTrue(r["ok"], r)
        return aid, first, r["data"]["adjudication_id"]

    def test_i1_drop_table_fails_with_fk_on(self):
        """自引用外键在 FK=ON 下使 DROP TABLE 失败（不是触发器拦的）。"""
        self._prepare_chain()
        c = self._raw(foreign_keys=True)
        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            c.execute("DROP TABLE adjudication_evidence")
            c.execute("DROP TABLE adjudications")
        self.assertIn("FOREIGN KEY constraint failed", str(ctx.exception))

    def test_i2_maintenance_connection_drops_and_schema_rebuilds(self):
        self._prepare_chain()
        self.L.close()
        c = self._raw(foreign_keys=False)
        c.execute("DROP TABLE adjudication_evidence")
        c.execute("DROP TABLE adjudications")
        gone = c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
                         " AND name IN ('adjudications','adjudication_evidence')"
                         ).fetchone()[0]
        self.assertEqual(gone, 0)
        L2 = actionledger.ActionLedger(self.db)     # 重新初始化
        try:
            self.assertEqual(int(L2._conn.execute(
                "SELECT COUNT(*) FROM adjudications").fetchone()[0]), 0)
            trg = int(L2._conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'"
                " AND name LIKE 'trg_%'").fetchone()[0])
            self.assertGreaterEqual(trg, 7)
        finally:
            L2.close()
        self.L = actionledger.ActionLedger(self.db)  # 供 tearDown 正常收尾


# ================================================ J 协议不变量（回归护栏）
class TestJ_Invariants(AdjBase):
    def test_j1_decide_never_changes_action_state(self):
        aid = self.to_unknown("j1")
        r = self.decide(aid, "j1")
        self.assertTrue(r["ok"], r)
        row = self.L.get(aid)
        self.assertEqual(row["state"], "SEND_UNKNOWN")
        self.assertEqual(row["anti_replay_locked"], 1)
        self.assertIsNone(row["result_tag"])

    def test_j2_g6_hotfix_not_weakened(self):
        """裁决存在 ≠ 可自述解锁：commit_outcome 仍无条件拒绝 UNKNOWN。"""
        aid = self.to_unknown("j2")
        r = self.decide(aid, "j2")
        self.assertTrue(r["ok"], r)
        for tag in ("SENT", "NOT_SENT"):
            bad = self.L.commit_outcome(aid, tag, "wishful",
                                        unlock={"adjudication_id":
                                                r["data"]["adjudication_id"]})
            self.assertFalse(bad["ok"], bad)
            self.assertEqual(bad["code"], SP.SP_ACT_003)
        self.assertEqual(self.L.get(aid)["state"], "SEND_UNKNOWN")

    def test_j3_consume_completes_action(self):
        aid, adj = self.sealed("j3")
        r = self.L.consume_adjudication(aid, adj, "SENT", expected_fencing=1,
                                        consumed_by=OPERATOR, evidence="ev-x")
        self.assertTrue(r["ok"], r)
        row = self.L.get(aid)
        self.assertEqual(row["state"], "COMPLETED")
        self.assertEqual(row["result_tag"], "SENT")
        self.assertEqual(row["anti_replay_locked"], 0)
        kinds = [e["kind"] for e in self.L.events(aid)]
        self.assertIn("adjudicated", kinds)
        self.assertIn("adjudication_consumed", kinds)
        self.assertIn("outcome", kinds)

    def test_j4_second_consume_rejected(self):
        aid, adj = self.sealed("j4")
        self.assertTrue(self.L.consume_adjudication(
            aid, adj, "SENT", expected_fencing=1)["ok"])
        r = self.L.consume_adjudication(aid, adj, "SENT", expected_fencing=1)
        self.assertFalse(r["ok"], r)
        self.assertIn(r["code"], (SP.SP_ACT_014, SP.SP_ACT_016))

    def test_j5_decision_result_tag_must_match(self):
        aid, adj = self.sealed("j5")
        r = self.L.consume_adjudication(aid, adj, "NOT_SENT", expected_fencing=1)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_ACT_015)
        self.assertEqual(r["evidence"], "decision/result_tag mismatch")
        self.assertEqual(self.L.get(aid)["state"], "SEND_UNKNOWN")

    def test_j6_non_consumable_decisions(self):
        for i, dec in enumerate(("CONFIRMED_UNKNOWN", "DEFER", "ESCALATE")):
            aid = self.to_unknown("j6-%d" % i)
            extra = (dict(reviewer="reviewer-y", reviewed_at=time.time())
                     if dec == "DECIDED_NOT_SENT" else {})
            r = self.decide(aid, "j6-%d" % i, decision=dec, **extra)
            self.assertTrue(r["ok"], r)
            bad = self.L.consume_adjudication(aid, r["data"]["adjudication_id"],
                                              "SENT", expected_fencing=1)
            self.assertFalse(bad["ok"], dec)
            self.assertEqual(bad["code"], SP.SP_ACT_015, dec)
            self.assertEqual(self.L.get(aid)["state"], "SEND_UNKNOWN")

    def test_j7_fencing_required_and_consistent(self):
        aid, adj = self.sealed("j7")
        r = self.L.consume_adjudication(aid, adj, "SENT")           # 未声明
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_ACT_011)
        r = self.L.consume_adjudication(aid, adj, "SENT", expected_fencing=99)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_ACT_012)

    def test_j8_decide_rejects_wrong_fencing(self):
        aid = self.to_unknown("j8")
        r = self.decide(aid, "j8", fencing=99)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_ACT_012)
        r = self.L.decide_adjudication(
            aid, "DECIDED_SENT", [self.evidence("j8")], OPERATOR, AUTH, REASON,
            "action:" + aid, expected_fencing=None)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_ACT_011)
        self.assertEqual(self.count("adjudications"), 0)

    def test_j9_completed_action_not_adjudicable(self):
        aid, adj = self.sealed("j9")
        self.assertTrue(self.L.consume_adjudication(
            aid, adj, "SENT", expected_fencing=1)["ok"])
        r = self.decide(aid, "j9c")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_ACT_016)

    def test_j10_hash_vectors_are_stable(self):
        self.assertEqual(
            actionledger._adj_hash([(1, "manual_attestation", "locator-1",
                                     1700000000000)]),
            "7c7aec25fc14ee103bcba3ec185d0fccbca52a7c811787cf2d1c072810051115")
        self.assertEqual(
            actionledger._adj_hash([(1, "page_observation", "p 1", 1700000000001),
                                    (2, "backend_receipt", "r2", 1700000000002)]),
            "c41e3427f028c7e9ab252373904ca64684b5fba4a318357a3752bb4082713999")
        # NFC 归一：é 的两种写法必须同摘要
        self.assertEqual(actionledger._adj_hash([(1, "external_query", "café",
                                                  1700000000003)]),
                         actionledger._adj_hash([(1, "external_query", "cafe\u0301",
                                                  1700000000003)]))
        # seq 顺序无关
        a = actionledger._adj_hash([(1, "page_observation", "p 1", 1700000000001),
                                    (2, "backend_receipt", "r2", 1700000000002)])
        b = actionledger._adj_hash([(2, "backend_receipt", "r2", 1700000000002),
                                    (1, "page_observation", "p 1", 1700000000001)])
        self.assertEqual(a, b)

    def test_j11_consumed_by_and_revoked_by_not_null(self):
        aid, adj = self.sealed("j11")
        c = self._raw()
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("UPDATE adjudications SET consumed_at=? WHERE adjudication_id=?",
                      (time.time(), adj))
        self.assertTrue(self.L.revoke_adjudication(adj, OPERATOR, REASON)["ok"])
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("UPDATE adjudications SET revoked_by='' WHERE adjudication_id=?",
                      (adj,))

    def test_j12_revoke_does_not_unlock_action(self):
        """撤销只释放槽位，不改变 action 状态、不构成解锁。"""
        aid, adj = self.sealed("j12")
        self.assertTrue(self.L.revoke_adjudication(adj, OPERATOR, REASON)["ok"])
        self.assertEqual(self.L.get(aid)["state"], "SEND_UNKNOWN")
        kinds = [e["kind"] for e in self.L.events(aid)]
        self.assertIn("adjudication_revoked", kinds)
        self.assertNotIn("outcome", kinds)


if __name__ == "__main__":
    unittest.main(verbosity=2)
