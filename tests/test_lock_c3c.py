#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""锁安全组（PROJECT-03-R1 任务书 §7 组 A）：C3-c 修复的验收单测。

覆盖：活进程超龄不接管 / 死 PID 合法接管 / PID 复用 fail-closed /
executor 身份 / token 不匹配 / 损坏 / 缺字段 / fencing 推进 / 旧 generation 拒绝
（Gate 层）/ 存活进程不因 stale_after 误接管。
两进程接管竞争与真实杀进程见 test_lock_mp.py（E 组，独立 OS 进程）。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import statelock  # noqa: E402
import procidentity  # noqa: E402
import sp_errors as SP  # noqa: E402


def _schema2(pid: int, token: str, gen: int = 5, lock_id: str = "lk-a",
             run_id: str = "ghost") -> dict:
    return {"schema": 2, "pid": pid, "owner": "wa", "run_id": run_id,
            "ts": time.time(),
            "process_start_token": token, "executor_id": "ex-%d-tok" % pid,
            "fencing_generation": gen, "lock_id": lock_id, "host": "h",
            "resource": "r"}


class TestLockC3C(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="sp-c3c-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.lp = os.path.join(self.d, "res.lock")

    # ---- A1：活 PID 持锁超过 stale_after，不得被接管 ----
    def test_live_pid_over_stale_after_never_taken_over(self):
        with open(self.lp, "w", encoding="utf-8") as f:
            json.dump(_schema2(os.getpid(), procidentity.self_token(),
                               lock_id="lk-live"), f)
        with self.assertRaises(statelock.LockBusy) as cm:
            statelock.acquire_named(self.lp, timeout=0.4, stale_after=1,
                                    resource="r")
        self.assertEqual(cm.exception.code, SP.SP_GATE_002)
        self.assertTrue(os.path.exists(self.lp), "活持有者的锁文件不许被动")
        self.assertNotIn("took_over", json.dumps(cm.exception.holder),
                         "绝不出现 took_over")

    # ---- A2：dead PID 合法接管（fencing +1，LOCK_RECOVERED）----
    def test_dead_pid_takeover_bumps_fencing(self):
        dead = 999999
        with open(self.lp, "w", encoding="utf-8") as f:
            json.dump(_schema2(dead, "win-ft:111"), f)
        h = statelock.acquire_named(self.lp, timeout=2.0, stale_after=1,
                                    resource="r")
        self.assertEqual(h.get("stale"), "took_over:dead_pid")
        self.assertEqual(h.get("state"), statelock.ST_RECOVERED)
        self.assertEqual(h["fencing_generation"], 6, "接管必须 fencing+1")
        self.assertNotEqual(h["lock_id"], "lk-a", "新持有者必须有新 lock_id")
        statelock.release_named(self.lp, h)

    # ---- A3：PID 复用（token mismatch）→ fail-closed 禁止接管 ----
    def test_pid_reuse_is_fail_closed(self):
        with open(self.lp, "w", encoding="utf-8") as f:
            json.dump(_schema2(os.getpid(), "win-ft:old-token"), f)
        with self.assertRaises(statelock.LockStateUnknown) as cm:
            statelock.acquire_named(self.lp, timeout=0.4, stale_after=1,
                                    resource="r")
        self.assertEqual(cm.exception.code, SP.SP_GATE_004)
        self.assertTrue(os.path.exists(self.lp))

    # ---- A4：executor_id 不匹配（接管后新执行者身份入账）----
    def test_executor_id_recorded_and_changed_on_takeover(self):
        dead = 999999
        with open(self.lp, "w", encoding="utf-8") as f:
            json.dump(_schema2(dead, "win-ft:1"), f)
        h = statelock.acquire_named(self.lp, timeout=2.0, resource="r")
        self.assertTrue(h["executor_id"].startswith("ex-"))
        self.assertNotEqual(h["executor_id"], "ex-%d-tok" % dead)
        statelock.release_named(self.lp, h)

    # ---- A5：process_start_token 不匹配 → 已在 A3 覆盖；查询失败 → fail-closed
    def test_unknown_identity_is_fail_closed(self):
        # token 记为 unknown（模拟查询不可用）
        with open(self.lp, "w", encoding="utf-8") as f:
            json.dump(_schema2(os.getpid(), "unknown"), f)
        with self.assertRaises(statelock.LockStateUnknown):
            statelock.acquire_named(self.lp, timeout=0.4, resource="r")

    # ---- A6：锁文件损坏 → LOCK_CORRUPT fail-closed ----
    def test_corrupt_lock_fail_closed(self):
        with open(self.lp, "w", encoding="utf-8") as f:
            f.write("{not json at all!!!")
        with self.assertRaises(statelock.LockCorrupt) as cm:
            statelock.acquire_named(self.lp, timeout=0.4, resource="r")
        self.assertEqual(cm.exception.code, SP.SP_GATE_005)
        self.assertTrue(os.path.exists(self.lp), "损坏锁不许自动删")

    # ---- A7：锁文件缺字段（schema-2 缺 process_start_token + pid 存活）----
    def test_missing_fields_fail_closed(self):
        with open(self.lp, "w", encoding="utf-8") as f:
            json.dump({"schema": 2, "pid": os.getpid(), "run_id": "x",
                       "ts": time.time()}, f)
        with self.assertRaises(statelock.LockStateUnknown):
            statelock.acquire_named(self.lp, timeout=0.4, resource="r")

    def test_missing_pid_field_is_corrupt(self):
        with open(self.lp, "w", encoding="utf-8") as f:
            json.dump({"schema": 2, "run_id": "x", "ts": time.time()}, f)
        with self.assertRaises(statelock.LockCorrupt):
            statelock.acquire_named(self.lp, timeout=0.4, resource="r")

    # ---- A8：空锁文件（创建后未上锁崩溃）可接管 ----
    def test_empty_lock_takeover(self):
        with open(self.lp, "w", encoding="utf-8") as f:
            pass
        h = statelock.acquire_named(self.lp, timeout=2.0, resource="r")
        self.assertEqual(h.get("stale"), "took_over:empty_lock")
        statelock.release_named(self.lp, h)

    # ---- A9/A10：fencing 单调 + 分类器七态可见 ----
    def test_fencing_monotonic_across_takeovers(self):
        dead = 999999
        gens = []
        for i in range(3):
            if os.path.exists(self.lp):
                os.remove(self.lp)
            with open(self.lp, "w", encoding="utf-8") as f:
                json.dump(_schema2(dead, "win-ft:%d" % i, gen=(i + 1) * 10), f)
            h = statelock.acquire_named(self.lp, timeout=2.0, resource="r")
            gens.append(h["fencing_generation"])
            statelock.release_named(self.lp, h)
        self.assertEqual(gens, sorted(gens), "fencing 必须单调")
        self.assertTrue(all(b > a for a, b in zip(gens, gens[1:])))

    def test_seven_states_classified(self):
        seen = {}
        seen[statelock.ST_FREE] = statelock.classify_holder_named(
            os.path.join(self.d, "nope.lock"))["state"]
        with open(self.lp, "w", encoding="utf-8") as f:
            f.write("garbage{{{")
        seen[statelock.ST_CORRUPT] = statelock.classify_holder_named(self.lp)["state"]
        with open(self.lp, "w", encoding="utf-8") as f:
            json.dump(_schema2(os.getpid(), procidentity.self_token()), f)
        seen[statelock.ST_OWNER_ALIVE] = statelock.classify_holder_named(self.lp)["state"]
        with open(self.lp, "w", encoding="utf-8") as f:
            json.dump(_schema2(999999, "win-ft:1"), f)
        seen[statelock.ST_OWNER_DEAD] = statelock.classify_holder_named(self.lp)["state"]
        with open(self.lp, "w", encoding="utf-8") as f:
            json.dump(_schema2(os.getpid(), "win-ft:old"), f)
        seen[statelock.ST_UNKNOWN] = statelock.classify_holder_named(self.lp)["state"]
        self.assertEqual(seen[statelock.ST_FREE], statelock.ST_FREE)
        self.assertEqual(seen[statelock.ST_CORRUPT], statelock.ST_CORRUPT)
        self.assertEqual(seen[statelock.ST_OWNER_ALIVE], statelock.ST_OWNER_ALIVE)
        self.assertEqual(seen[statelock.ST_OWNER_DEAD], statelock.ST_OWNER_DEAD)
        self.assertEqual(seen[statelock.ST_UNKNOWN], statelock.ST_UNKNOWN)

    # ---- A11：释放后 fencing 计数不回退（新 acquire ≥ 旧值）----
    def test_fencing_never_goes_backwards_after_release(self):
        dead = 999999
        with open(self.lp, "w", encoding="utf-8") as f:
            json.dump(_schema2(dead, "win-ft:7", gen=42), f)
        h1 = statelock.acquire_named(self.lp, timeout=2.0, resource="r")
        statelock.release_named(self.lp, h1)
        h2 = statelock.acquire_named(self.lp, timeout=2.0, resource="r")
        try:
            self.assertGreaterEqual(h2["fencing_generation"], 43,
                                    "fencing 不得因重新拿锁而回退")
        finally:
            statelock.release_named(self.lp, h2)


if __name__ == "__main__":
    unittest.main()
