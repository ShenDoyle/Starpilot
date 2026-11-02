from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import winlease  # noqa: E402
import wa  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="sp-lease-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.env = mock.patch.dict(os.environ, {"STARPILOT_LOCK_DIR": self.d,
                                                "STARPILOT_SERIAL_WAIT": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("STARPILOT_LOCK_WAIT", None)
        os.environ.pop("STARPILOT_LEASE_TTL", None)

    def own(self, ws="a", run="r1", platform="chatgpt"):
        return winlease.make_owner(workspace=os.path.join(self.d, ws),
                                   run_id=run, platform=platform)


class TestAcquire(Base):
    def test_free_instance_acquired(self):
        rec = winlease.acquire("inst1", self.own())
        self.assertEqual(rec["owner_key"], winlease.owner_key(self.own()))
        self.assertEqual(winlease.status("inst1")["state"], "busy")
        self.assertGreater(winlease.status("inst1")["remaining"], 0)

    def test_conflict_refused_not_preempted(self):
        winlease.acquire("inst1", self.own(ws="a", run="r1"))
        t0 = time.time()
        with self.assertRaises(winlease.WindowBusy) as cm:
            winlease.acquire("inst1", self.own(ws="b", run="r2"), wait=0)
        self.assertLess(time.time() - t0, 2)
        self.assertEqual(cm.exception.holder["owner_key"],
                         winlease.owner_key(self.own(ws="a", run="r1")))
        # 原租约没被动过
        cur = winlease.read("inst1")
        self.assertEqual(cur["owner_key"], winlease.owner_key(self.own(ws="a", run="r1")))

    def test_same_owner_renews_without_takeover(self):
        winlease.acquire("inst1", self.own())
        rec = winlease.acquire("inst1", self.own())
        self.assertNotIn("takeover_from", rec)

    def test_serial_waits_then_times_out(self):
        winlease.acquire("inst1", self.own(ws="a", run="r1"))
        t0 = time.time()
        with self.assertRaises(winlease.WindowBusy) as cm:
            winlease.acquire("inst1", self.own(ws="b", run="r2"),
                             serial=True, wait=1.2)
        self.assertGreaterEqual(time.time() - t0, 1.0)
        self.assertGreater(cm.exception.waited, 0.9)

    def test_serial_acquires_when_free(self):
        t0 = time.time()
        winlease.acquire("inst1", self.own(), serial=True, wait=30)
        self.assertLess(time.time() - t0, 2)

    def test_serial_busy_message_mentions_busy(self):
        winlease.acquire("inst1", self.own())
        with self.assertRaises(winlease.WindowBusy) as cm:
            winlease.acquire("inst1", self.own(ws="b"), serial=True, wait=0.3)
        self.assertIn("WINDOW_BUSY", str(cm.exception))


class TestStaleAndTakeover(Base):
    def test_stale_reclaimed_with_ledger(self):
        winlease.acquire("inst1", self.own(ws="a", run="r1"), ttl=-1)  # 立刻过期
        self.assertEqual(winlease.status("inst1")["state"], "stale")
        winlease.claim_session("inst1", "s-old", self.own(ws="a", run="r1"))
        rec = winlease.acquire("inst1", self.own(ws="b", run="r2"))
        self.assertEqual(rec["owner_key"], winlease.owner_key(self.own(ws="b", run="r2")))
        self.assertEqual(rec["sessions"], {})  # 过期租约的会话声明作废
        with open(winlease.journal_path("inst1"), encoding="utf-8") as f:
            j = f.read()
        self.assertIn("stale_reclaimed", j)

    def test_takeover_requires_reason(self):
        winlease.acquire("inst1", self.own(ws="a", run="r1"))
        with self.assertRaises(winlease.WindowBusy):
            winlease.acquire("inst1", self.own(ws="b", run="r2"), takeover_reason="  ")

    def test_takeover_records_journal(self):
        winlease.acquire("inst1", self.own(ws="a", run="r1"))
        rec = winlease.acquire("inst1", self.own(ws="b", run="r2"),
                               takeover_reason="用户确认对方进程已死")
        self.assertEqual(rec["owner_key"], winlease.owner_key(self.own(ws="b", run="r2")))
        self.assertEqual(rec["takeover_from"]["run_id"], "r1")
        with open(winlease.journal_path("inst1"), encoding="utf-8") as f:
            j = f.read()
        self.assertIn("takeover", j)
        self.assertIn("用户确认对方进程已死", j)


class TestReleaseAndClaims(Base):
    def test_release_by_other_refused(self):
        winlease.acquire("inst1", self.own(ws="a", run="r1"))
        self.assertFalse(winlease.release("inst1", self.own(ws="b", run="r2")))
        self.assertEqual(winlease.status("inst1")["state"], "busy")

    def test_release_by_owner_removes_file(self):
        winlease.acquire("inst1", self.own())
        self.assertTrue(winlease.release("inst1", self.own()))
        self.assertEqual(winlease.status("inst1")["state"], "free")

    def test_heartbeat_only_by_owner(self):
        winlease.acquire("inst1", self.own(ws="a", run="r1"), ttl=10)
        self.assertFalse(winlease.heartbeat("inst1", self.own(ws="b", run="r2"), ttl=9999))
        self.assertTrue(winlease.heartbeat("inst1", self.own(ws="a", run="r1"), ttl=9999))
        self.assertGreater(winlease.remaining(winlease.read("inst1")), 9000)

    def test_session_claim_blocks_other_owner(self):
        winlease.acquire("inst1", self.own(ws="a", run="r1"))
        winlease.claim_session("inst1", "s1", self.own(ws="a", run="r1"))
        ok, why = winlease.may_stop_session("inst1", "s1", self.own(ws="b", run="r2"))
        self.assertFalse(ok)
        self.assertIn("不许停", why)
        ok2, _ = winlease.may_stop_session("inst1", "s1", self.own(ws="a", run="r1"))
        self.assertTrue(ok2)

    def test_unclaimed_session_is_stoppable(self):
        winlease.acquire("inst1", self.own(ws="a", run="r1"))
        ok, why = winlease.may_stop_session("inst1", "s-orphan", self.own(ws="a", run="r1"))
        self.assertTrue(ok)
        self.assertIn("无主", why)

    def test_claim_void_when_lease_stale(self):
        winlease.acquire("inst1", self.own(ws="a", run="r1"), ttl=-1)
        winlease.claim_session("inst1", "s1", self.own(ws="a", run="r1"))
        ok, _ = winlease.may_stop_session("inst1", "s1", self.own(ws="b", run="r2"))
        self.assertTrue(ok)


class TestWaIntegration(Base):
    """接进 wa.py 后：清理边界止于自己的租约；冲突返回 9 而不是去抢。"""

    def test_enforce_skips_other_live_owner_session(self):
        mine = self.own(ws="a", run="r1")
        theirs = self.own(ws="b", run="r2")
        winlease.acquire("inst1", theirs)
        winlease.claim_session("inst1", "s-theirs", theirs)
        sessions = [{"session": "s-theirs", "browser": "inst1"},
                    {"session": "s-orphan", "browser": "inst1"},
                    {"session": "s-mine", "browser": "inst1"}]
        stopped = []
        with mock.patch.object(wa, "list_sessions", return_value=sessions), \
                mock.patch.object(wa, "bsk",
                                  side_effect=lambda args, timeout=30: stopped.append(args[-1]) or (0, "", "")):
            wa.enforce_single_session("s-mine", "inst1", mine)
        self.assertEqual(sorted(stopped), ["s-orphan"])  # 只清无主的，别人的不动

    def test_acquire_window_exits_9_when_busy(self):
        theirs = self.own(ws="b", run="r2")
        winlease.acquire("inst1", theirs)
        st = {"browser": "inst1", "platform": "deepseek"}
        path = os.path.join(self.d, "a", ".wa-state.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with mock.patch.object(wa, "instance_report",
                               return_value={"instances": [], "duplicates": []}), \
                mock.patch.dict(os.environ, {"STARPILOT_LOCK_WAIT": "1"}), \
                self.assertRaises(SystemExit) as cm:
            wa.acquire_window(st, path, sid="", rep={"instances": [], "duplicates": []})
        self.assertEqual(cm.exception.code, 9)

    def test_acquire_window_ok_when_free(self):
        st = {"browser": "inst1", "platform": "chatgpt"}
        path = os.path.join(self.d, "a", ".wa-state.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with mock.patch.object(wa, "instance_report",
                               return_value={"instances": [], "duplicates": []}), \
                mock.patch.object(wa, "_supports_isolation", return_value=True):
            inst = wa.acquire_window(st, path, sid="s-new")
        self.assertEqual(inst, "inst1")
        self.assertEqual(winlease.status("inst1")["state"], "busy")
        self.assertIn("s-new", winlease.status("inst1")["sessions"])

    def test_instance_id_falls_back_to_unique_online(self):
        st = {}
        rep = {"instances": [{"instance_id": "7cb0cac4"}], "duplicates": []}
        os.environ.pop("WA_BROWSER", None)
        self.assertEqual(wa._instance_id(st, rep), "7cb0cac4")
        self.assertEqual(st.get("browser"), "7cb0cac4")


if __name__ == "__main__":
    unittest.main()


class TestGroupWindow(Base):
    """组窗口：组名、journal 登记、跨任务复用（2026-11-02 用户指令）。"""

    def test_group_default_and_env(self):
        self.assertEqual(winlease.group(), "starpilot")
        with mock.patch.dict(os.environ, {"STARPILOT_GROUP": "mygrp"}):
            self.assertEqual(winlease.group(), "mygrp")

    def test_claim_journaled_and_reusable_across_lease_generations(self):
        o1 = self.own("ws1", "r1")
        winlease.acquire("inst1", o1)
        winlease.claim_session("inst1", "sessA", o1)
        # 租约释放（轮换）：租约文件没了，但 journal 登记仍在
        winlease.release("inst1", o1, reason="turn end")
        self.assertEqual(winlease.group_sessions("inst1"), ["sessA"])
        # 下一棒（另一个任务）注销会话后，复用列表随之清空
        o2 = self.own("ws2", "r2")
        winlease.acquire("inst1", o2)
        winlease.drop_session("inst1", "sessA", o2, reason="reaped")
        self.assertEqual(winlease.group_sessions("inst1"), [])

    def test_journal_claims_live_lease_flag(self):
        o1 = self.own("ws1", "r1")
        winlease.acquire("inst1", o1)
        winlease.claim_session("inst1", "sessA", o1)
        claims = winlease.journal_claims("inst1")
        self.assertTrue(claims["sessA"]["live_lease"])
        self.assertEqual(claims["sessA"]["group"], "starpilot")
        winlease.release("inst1", o1)
        claims = winlease.journal_claims("inst1")
        self.assertFalse(claims["sessA"]["live_lease"])

    def test_group_filter(self):
        o1 = self.own("ws1", "r1")
        winlease.acquire("inst1", o1)
        winlease.claim_session("inst1", "sessA", o1)
        with mock.patch.dict(os.environ, {"STARPILOT_GROUP": "other"}):
            self.assertEqual(winlease.group_sessions("inst1"), [])
            self.assertEqual(winlease.group_sessions("inst1", "starpilot"),
                             ["sessA"])


class TestGlobalStartCounter(Base):
    """禁止无脑重开：全局跨任务计数（锁目录共享）。"""

    def test_counts_across_tasks(self):
        winlease.note_start("inst1")           # 任务 A 记的
        many, n, mx = winlease.too_many_starts("inst1")
        self.assertFalse(many)
        # 任务 B（不同 workspace）也记到同一本账
        winlease.note_start("inst1")
        winlease.note_start("inst1")
        winlease.note_start("inst1")
        many, n, mx = winlease.too_many_starts("inst1")
        self.assertTrue(many)
        self.assertEqual((n, mx), (4, 4))

    def test_per_instance_isolation(self):
        winlease.note_start("inst1")
        many, n, _ = winlease.too_many_starts("inst2")
        self.assertFalse(many)
        self.assertEqual(n, 0)

    def test_old_entries_expire(self):
        p = winlease.starts_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"inst1": [time.time() - 700, time.time() - 650]}, f)
        many, n, _ = winlease.too_many_starts("inst1")
        self.assertFalse(many)
        self.assertEqual(n, 0)


class TestWaitBudget(Base):
    """轮换预算。2026-11-02 用户裁定：人工介入**不打断执行**——
    ATTENDED_WAIT_S 降级为「提醒阈值」（ATTENDED_NOTIFY_WAIT），
    轮换总预算有人/无人一致（UNATTENDED_WAIT_S）。"""

    def test_budget_by_mode(self):
        # wait_budget 保留历史语义（提醒阈值），但 acquire_window 的总预算
        # 一律用 wait_budget(False)——介入≠打断，只是先提醒再继续等。
        self.assertEqual(winlease.wait_budget(True), 30.0)
        self.assertEqual(winlease.wait_budget(False), 600.0)
        self.assertEqual(winlease.ATTENDED_NOTIFY_WAIT, 30.0)

    def test_attended_from_env(self):
        self.assertFalse(winlease.is_attended())
        with mock.patch.dict(os.environ, {"WA_ATTENDED": "1"}):
            self.assertTrue(winlease.is_attended())
            self.assertEqual(winlease.wait_budget(), 30.0)
            # 有人值守的轮换预算=无人值守（用户裁定：人工介入不打断）
            self.assertEqual(winlease.wait_budget(False), 600.0)
