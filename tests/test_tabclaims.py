# -*- coding: utf-8 -*-
"""标签所有权账本（tabclaims）回归测试。

覆盖 2026-11-02 Web AI 裁定要求的验收点：
· 同一 tab 同时只能一个有效所有者（并发认领只有一个成功）
· 当前任务只能操作自己持有 + 租约有效 + fencing 未落后的标签
· 其他任务持有 / 归属未知 / 正在关闭 / 已关闭 → 一律拒绝
· borrowed 标签只读、不可关闭
· UNKNOWN 是隔离态，不能靠 TTL 自行解除；只有「持有者已死 + 租约过期」才允许只读恢复
· 账本 ↔ BSK 实际列表对账：账本有 BSK 无 → closed；BSK 有账本无 → 只报告
· 账本不可用时 fail-closed
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import tabclaims as tc  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._db = os.environ.get(tc.DB_ENV)
        self._au = os.environ.get(tc.AUDIT_ENV)
        os.environ[tc.DB_ENV] = os.path.join(self.tmp, "tab-claims.db")
        os.environ[tc.AUDIT_ENV] = os.path.join(self.tmp, "audit.jsonl")
        self.conn = tc.connect()

    def tearDown(self):
        self.conn.close()
        for k, v in ((tc.DB_ENV, self._db), (tc.AUDIT_ENV, self._au)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestClaim(Base):
    def test_claim_then_get(self):
        tc.claim(self.conn, "s1", "t1", "ownA", run_id="r1", lease_id="L1",
                 fencing_token=5, url="https://x")
        row = tc.get(self.conn, "s1", "t1")
        self.assertEqual(row["owner_key"], "ownA")
        self.assertEqual(row["fencing_token"], 5)
        self.assertEqual(row["state"], tc.STATE_CLAIMED)

    def test_other_owner_rejected(self):
        tc.claim(self.conn, "s1", "t1", "ownA", run_id="r1")
        with self.assertRaises(tc.TabClaimError) as cm:
            tc.claim(self.conn, "s1", "t1", "ownB", run_id="r2")
        self.assertEqual(cm.exception.code, "TAB_OWNED_BY_OTHER")

    def test_same_owner_idempotent(self):
        tc.claim(self.conn, "s1", "t1", "ownA", run_id="r1")
        tc.claim(self.conn, "s1", "t1", "ownA", run_id="r1")
        self.assertEqual(tc.get(self.conn, "s1", "t1")["state"], tc.STATE_CLAIMED)

    def test_closed_tab_cannot_be_reclaimed_without_takeover(self):
        """关闭中的标签不许被认领——否则两个任务会同时操作同一个 tab。"""
        tc.claim(self.conn, "s1", "t1", "ownA", run_id="r1")
        tc.mark_closing(self.conn, "s1", "t1", "ownA")
        with self.assertRaises(tc.TabClaimError):
            tc.claim(self.conn, "s1", "t1", "ownB", run_id="r2")


class TestNavigateGate(Base):
    def test_owner_can_navigate(self):
        tc.claim(self.conn, "s1", "t1", "ownA", run_id="r1", lease_id="L1",
                 fencing_token=3)
        ok, why, _ = tc.navigate_allowed(self.conn, "s1", "t1", "ownA",
                                         run_id="r1", lease_id="L1", fencing_token=3)
        self.assertTrue(ok, why)

    def test_other_owner_blocked(self):
        tc.claim(self.conn, "s1", "t1", "ownA", run_id="r1")
        ok, why, _ = tc.navigate_allowed(self.conn, "s1", "t1", "ownB", run_id="r2")
        self.assertFalse(ok)
        self.assertEqual(why, "owned_by_other")

    def test_unknown_owner_tab_blocked(self):
        """BSK 成功但账本写失败 → owner_unknown，不许当自己的用。"""
        tc.claim(self.conn, "s1", "t1", "ownA", run_id="r1")
        tc.mark_owner_unknown(self.conn, "s1", "t1", "db write failed")
        ok, why, _ = tc.navigate_allowed(self.conn, "s1", "t1", "ownA", run_id="r1")
        self.assertFalse(ok)
        self.assertEqual(why, "owner_unknown")

    def test_unclaimed_blocked(self):
        ok, why, _ = tc.navigate_allowed(self.conn, "s1", "nope", "ownA")
        self.assertFalse(ok)
        self.assertEqual(why, "unclaimed")

    def test_stale_fencing_blocked(self):
        tc.claim(self.conn, "s1", "t1", "ownA", run_id="r1", fencing_token=9)
        ok, why, _ = tc.navigate_allowed(self.conn, "s1", "t1", "ownA",
                                         run_id="r1", fencing_token=4)
        self.assertFalse(ok)
        self.assertEqual(why, "stale_fencing")

    def test_borrowed_read_only(self):
        tc.claim(self.conn, "s1", "t1", "ownA", run_id="r1",
                 intent=tc.INTENT_BORROWED, origin_scope="user")
        ok, why, _ = tc.navigate_allowed(self.conn, "s1", "t1", "ownA", run_id="r1")
        self.assertFalse(ok)
        self.assertEqual(why, "borrowed_read_only")

    def test_same_url_is_not_ownership(self):
        """同地址、同 session 都不是所有权依据——只有账本里的 owner 是。"""
        tc.claim(self.conn, "s1", "t1", "ownA", run_id="r1", url="https://same")
        tc.claim(self.conn, "s1", "t2", "ownB", run_id="r2", url="https://same")
        ok, why, _ = tc.navigate_allowed(self.conn, "s1", "t2", "ownA", run_id="r1")
        self.assertFalse(ok)


class TestTakeover(Base):
    """无主残留接管（组窗口复用的必经之路）。

    判据是「原租约是否还有效」（由 wa 用 winlease 独立判），不是「名字不同」：
    上一个任务结束留下的标签必须能被下一个任务接管，否则新任务连自己窗口都
    导航不了；但原持有者还活着就不许抢。
    """

    def test_takeover_moves_ownership(self):
        tc.claim(self.conn, "s1", "t1", "old", run_id="r1", lease_id="L1",
                 fencing_token=1)
        row = tc.takeover(self.conn, "s1", "t1", "new", run_id="r2",
                          lease_id="L2", fencing_token=2, reason="stale_lease")
        self.assertEqual(row["owner_key"], "new")
        self.assertEqual(row["run_id"], "r2")
        self.assertEqual(row["lease_id"], "L2")
        self.assertEqual(row["fencing_token"], 2)
        self.assertEqual(row["state"], tc.STATE_CLAIMED)
        ok, why, _ = tc.navigate_allowed(self.conn, "s1", "t1", "new",
                                         run_id="r2", lease_id="L2",
                                         fencing_token=2)
        self.assertTrue(ok, why)

    def test_takeover_refuses_borrowed(self):
        tc.claim(self.conn, "s1", "t1", "old", run_id="r1",
                 intent=tc.INTENT_BORROWED)
        with self.assertRaises(tc.TabClaimError) as cm:
            tc.takeover(self.conn, "s1", "t1", "new", run_id="r2")
        self.assertEqual(cm.exception.code, "TAB_BORROWED")

    def test_takeover_refuses_unknown_quarantine(self):
        tc.claim(self.conn, "s1", "t1", "old", run_id="r1")
        tc.mark_unknown(self.conn, "s1", "t1")
        with self.assertRaises(tc.TabClaimError) as cm:
            tc.takeover(self.conn, "s1", "t1", "new", run_id="r2")
        self.assertEqual(cm.exception.code, "TAB_NOT_CLAIMABLE")

    def test_takeover_refuses_must_reinspect(self):
        tc.claim(self.conn, "s1", "t1", "old", run_id="r1")
        with self.conn:
            self.conn.execute("UPDATE tabs SET must_reinspect=1 WHERE tab_id='t1'")
        with self.assertRaises(tc.TabClaimError) as cm:
            tc.takeover(self.conn, "s1", "t1", "new", run_id="r2")
        self.assertEqual(cm.exception.code, "TAB_NEEDS_REINSPECT")

    def test_takeover_unclaimed_rejected(self):
        with self.assertRaises(tc.TabClaimError) as cm:
            tc.takeover(self.conn, "s1", "nope", "new")
        self.assertEqual(cm.exception.code, "TAB_UNCLAIMED")


class TestClose(Base):
    def test_other_owner_cannot_close(self):
        tc.claim(self.conn, "s1", "t1", "ownA", run_id="r1")
        with self.assertRaises(tc.TabClaimError) as cm:
            tc.mark_closing(self.conn, "s1", "t1", "ownB", run_id="r2")
        self.assertEqual(cm.exception.code, "TAB_OWNED_BY_OTHER")

    def test_borrowed_cannot_be_closed(self):
        tc.claim(self.conn, "s1", "t1", "ownA", run_id="r1",
                 intent=tc.INTENT_BORROWED)
        with self.assertRaises(tc.TabClaimError) as cm:
            tc.mark_closing(self.conn, "s1", "t1", "ownA", run_id="r1")
        self.assertEqual(cm.exception.code, "TAB_BORROWED")

    def test_closing_blocks_others(self):
        """关闭期间其他任务不能接管（裁定：close 进 critical section）。"""
        tc.claim(self.conn, "s1", "t1", "ownA", run_id="r1")
        tc.mark_closing(self.conn, "s1", "t1", "ownA", run_id="r1")
        ok, why, _ = tc.navigate_allowed(self.conn, "s1", "t1", "ownB", run_id="r2")
        self.assertFalse(ok)
        self.assertIn(why, ("state_closing", "owned_by_other"))

    def test_unknown_state_quarantines(self):
        """BSK 超时 → unknown + must_reinspect，谁都不能再用这个标签。"""
        tc.claim(self.conn, "s1", "t1", "ownA", run_id="r1")
        tc.mark_closing(self.conn, "s1", "t1", "ownA", run_id="r1")
        tc.mark_unknown(self.conn, "s1", "t1", "cdp timeout")
        ok, why, row = tc.navigate_allowed(self.conn, "s1", "t1", "ownA", run_id="r1")
        self.assertFalse(ok)
        self.assertEqual(why, "state_unknown")
        self.assertEqual(row["must_reinspect"], 1)

    def test_unknown_not_released_by_ttl(self):
        """UNKNOWN 不能靠时间自动解除：持有者还活着 → 继续隔离。"""
        tc.claim(self.conn, "s1", "t1", "ownA", run_id="r1")
        tc.mark_unknown(self.conn, "s1", "t1")
        ok, why = tc.release_unknown(self.conn, "s1", "t1",
                                     holder_alive=True, lease_expired=True)
        self.assertFalse(ok)
        self.assertEqual(why, "holder_alive_quarantine")

    def test_unknown_needs_lease_expiry(self):
        tc.claim(self.conn, "s1", "t1", "ownA", run_id="r1")
        tc.mark_unknown(self.conn, "s1", "t1")
        ok, why = tc.release_unknown(self.conn, "s1", "t1",
                                     holder_alive=False, lease_expired=False)
        self.assertFalse(ok)
        self.assertEqual(why, "lease_not_expired")

    def test_unknown_released_only_after_death_and_expiry(self):
        tc.claim(self.conn, "s1", "t1", "ownA", run_id="r1")
        tc.mark_unknown(self.conn, "s1", "t1")
        ok, why = tc.release_unknown(self.conn, "s1", "t1",
                                     holder_alive=False, lease_expired=True)
        self.assertTrue(ok)
        self.assertEqual(why, "read_only_recovery")
        # 只读恢复 ≠ 可以写：仍须重新对账并重新认领，才可能转为 claimed
        ok2, why2, _ = tc.navigate_allowed(self.conn, "s1", "t1", "ownA", run_id="r1")
        self.assertFalse(ok2)
        self.assertIn(why2, ("state_unknown", "must_reinspect"))


class TestReconcile(Base):
    def test_ledger_gone_in_bsk_marks_closed(self):
        tc.claim(self.conn, "s1", "t1", "ownA", run_id="r1")
        rep = tc.reconcile(self.conn, "s1", [])
        self.assertIn("t1", rep["closed_gone"])
        self.assertEqual(tc.get(self.conn, "s1", "t1")["state"], tc.STATE_CLOSED)

    def test_bsk_tab_not_in_ledger_reported_only(self):
        rep = tc.reconcile(self.conn, "s1", [{"tab_id": 123, "scope": "agent"}])
        self.assertIn("123", rep["unknown_owner"])
        # 只报告：不写账本、不改状态
        self.assertIsNone(tc.get(self.conn, "s1", "123"))

    def test_own_tabs_excludes_borrowed_and_blocked(self):
        tc.claim(self.conn, "s1", "t1", "ownA", run_id="r1")
        tc.claim(self.conn, "s1", "t2", "ownA", run_id="r1",
                 intent=tc.INTENT_BORROWED)
        tc.claim(self.conn, "s1", "t3", "ownA", run_id="r1")
        tc.mark_unknown(self.conn, "s1", "t3")
        ids = [r["tab_id"] for r in tc.own_tabs(self.conn, "s1", "ownA", "r1")]
        self.assertEqual(ids, ["t1"])


class TestFailClosed(Base):
    def test_db_unavailable_raises(self):
        """账本建不了 = 什么都不许做（不是「当作没有标签」）。"""
        blocker = os.path.join(self.tmp, "blocked-dir")
        os.makedirs(blocker, exist_ok=True)
        bad = os.path.join(blocker, "sub", "x.db")
        os.makedirs(bad, exist_ok=True)   # 把「库路径」建成了目录 → connect 必失败
        with self.assertRaises(Exception):
            tc.connect(bad)

    def test_audit_write_failure_does_not_break_claims(self):
        """审计流写失败不影响主流程（它只是记录，不是权限判据）。"""
        # 用一个「把目录当文件写」的路径制造写失败（环境变量不能带 \0）
        blocker = os.path.join(self.tmp, "blocked")
        os.makedirs(blocker, exist_ok=True)
        os.environ[tc.AUDIT_ENV] = blocker
        tc.claim(self.conn, "s1", "t1", "ownA", run_id="r1")
        self.assertEqual(tc.get(self.conn, "s1", "t1")["state"], tc.STATE_CLAIMED)


if __name__ == "__main__":
    unittest.main()
