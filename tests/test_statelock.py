#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""state 单写锁的验收单测（架构评审 F3 / S3）。

锁要证明三件事：
1. 有人在写时，第二个写者**拿不到**锁（而不是静默覆盖）；
2. 崩溃残留的锁能被**接管**（否则一次 kill -9 让项目永久卡死）；
3. 只删自己的锁（不误删别人的）。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import statelock  # noqa: E402
import wa  # noqa: E402


class TestStateLock(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="sp-lock-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.sp = os.path.join(self.d, ".wa-state.json")
        with open(self.sp, "w", encoding="utf-8") as f:
            json.dump({"url": "u", "platform": "kimi"}, f)

    def test_roundtrip(self):
        with statelock.state_lock(self.sp, run_id="r1") as h:
            self.assertEqual(statelock.read_holder(self.sp)["run_id"], "r1")
            self.assertEqual(h["pid"], os.getpid())
        self.assertEqual(statelock.read_holder(self.sp), {}, "退出后必须释放")

    def test_second_writer_is_blocked(self):
        with statelock.state_lock(self.sp, run_id="r1"):
            with self.assertRaises(statelock.LockBusy):
                statelock.acquire(self.sp, run_id="r2", timeout=0.3)

    def test_busy_message_names_holder(self):
        with statelock.state_lock(self.sp, run_id="r1"):
            try:
                statelock.acquire(self.sp, run_id="r2", timeout=0.2)
                self.fail("应该抛 LockBusy")
            except statelock.LockBusy as e:
                self.assertIn("r1", str(e))

    def test_stale_lock_from_dead_pid_is_taken_over(self):
        p = statelock.lock_path_for(self.sp)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"pid": 999999, "run_id": "ghost", "ts": 1.0}, f)
        h = statelock.acquire(self.sp, run_id="r2", timeout=0.5, stale_after=10)
        self.assertIn("took_over", h.get("stale", ""), "死进程的锁必须能被接管")
        statelock.release(self.sp, h)

    def test_old_lock_is_taken_over_by_age(self):
        p = statelock.lock_path_for(self.sp)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "run_id": "me", "ts": 1.0}, f)
        h = statelock.acquire(self.sp, run_id="r2", timeout=0.5, stale_after=1)
        self.assertIn("took_over:age", h.get("stale", ""))
        statelock.release(self.sp, h)

    def test_release_does_not_remove_foreign_lock(self):
        p = statelock.lock_path_for(self.sp)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"pid": 999999, "run_id": "ghost", "ts": 1.0}, f)
        statelock.release(self.sp, {"pid": os.getpid()})
        self.assertTrue(os.path.exists(p), "别人的锁不许被误删")
        statelock.release(self.sp, None)

    def test_wa_save_state_honours_lock(self):
        with statelock.state_lock(self.sp, run_id="other"):
            with self.assertRaises(SystemExit):
                wa.save_state(self.sp, {"url": "u2"}, timeout=0.3)
        # 锁放掉后能正常写
        wa.save_state(self.sp, {"url": "u2", "platform": "kimi"})
        self.assertEqual(wa.load_state(self.sp)["url"], "u2")


if __name__ == "__main__":
    unittest.main()
