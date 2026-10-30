#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""平台占用锁 + 执行模式闸门的验收单测（契约 v1.4 §2.7 / §2.8）。

平台锁要证明四件事：
1. 任务 A 持锁时，任务 B 的 bind/send/clear **拿不到锁**（rc=9，不排队死等）；
2. 拿不到锁时 B 能看到 A 是谁（run_id / pid）；
3. 崩溃残留的锁（死 pid / 超时）能被接管；
4. 锁按 <平台>-<key> 隔离（不同浏览器 profile 并行不打架）。

执行模式闸门要证明两件事：
1. 卡片缺「执行模式」→ 拒（EXEC_MODE_MISSING）；
2. 写法不在二选一词表 → 拒（EXEC_MODE_INVALID）；两个词根都放行。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import requirement_gate  # noqa: E402
import statelock  # noqa: E402
import wa  # noqa: E402


class TestPlatformLock(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="sp-plock-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.env = {"STARPILOT_LOCK_DIR": self.d, "STARPILOT_LOCK_KEY": "default",
                    "STARPILOT_LOCK_WAIT": "0"}
        patcher = mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _hold_as_other(self, platform: str, run_id: str = "proj-A") -> str:
        """伪造「另一任务持锁」：pid 必须是**别的活进程**，不能是本进程
        （本进程同 pid 且 allow_reentry=True 会判重入，那是另一条正常路径）。
        用父进程 pid —— 它一定活着。"""
        p = wa.platform_lock_path(platform)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getppid(), "owner": "wa", "run_id": run_id,
                       "ts": __import__("time").time()}, f)
        self.addCleanup(lambda: os.path.exists(p) and os.remove(p))
        return p

    # ---- 1/2：互斥 + 持有者可见 ----

    def test_second_task_is_rejected_with_holder_info(self):
        p = self._hold_as_other("kimi", "proj-A")
        with self.assertRaises(statelock.LockBusy) as cm:
            wa.lock_platform("kimi")
        self.assertIn("proj-A", str(cm.exception))
        self.assertTrue(os.path.exists(p), "被拒时不许动别人的锁")

    def test_busy_holder_names_run_id(self):
        self._hold_as_other("chatgpt", "proj-B")
        with self.assertRaises(statelock.LockBusy) as cm:
            wa.lock_platform("chatgpt")
        self.assertIn("proj-B", str(cm.exception))

    # ---- 装饰器：平台被占 → rc=9；空闲 → 原样执行并放锁 ----

    def test_decorator_returns_9_when_busy(self):
        self._hold_as_other("kimi", "proj-A")

        class A:  # 最小参数对象
            platform = "kimi"
            state = None

        @wa.with_platform_lock
        def fake_cmd(a):
            return 0
        self.assertEqual(fake_cmd(A()), 9)

    def test_decorator_runs_and_releases_when_free(self):
        calls = []

        class A:
            platform = "kimi"
            state = None

        @wa.with_platform_lock
        def fake_cmd(a):
            calls.append(1)
            # 锁在命令执行期间必须是自己的
            h = statelock.read_holder_named(wa.platform_lock_path("kimi"))
            self.assertEqual(h.get("pid"), os.getpid())
            return 0

        self.assertEqual(fake_cmd(A()), 0)
        self.assertEqual(calls, [1])
        self.assertEqual(statelock.read_holder_named(wa.platform_lock_path("kimi")),
                         {}, "命令结束后必须放锁")

    # ---- 3：残留锁接管 ----

    def test_dead_pid_lock_is_taken_over(self):
        p = wa.platform_lock_path("kimi")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"pid": 999999, "run_id": "ghost", "ts": 1.0}, f)
        h = wa.lock_platform("kimi")
        self.assertIn("took_over", h.get("stale", ""))
        statelock.release_named(p, h)

    # ---- 4：key 隔离 ----

    def test_keys_are_isolated(self):
        h1 = wa.lock_platform("kimi")
        os.environ["STARPILOT_LOCK_KEY"] = "profile-b"
        try:
            h2 = wa.lock_platform("kimi")  # 不同 profile = 不同锁，不互斥
            self.assertNotEqual(h1["resource"], h2["resource"])
            statelock.release_named(wa.platform_lock_path("kimi"), h2)
        finally:
            os.environ["STARPILOT_LOCK_KEY"] = "default"
            statelock.release_named(wa.platform_lock_path("kimi"), h1)

    # ---- 5：同进程重入（外层 cmd_send 装饰器 → do_send 再取锁，不许自锁）----

    def test_same_process_reentry_does_not_deadlock_or_unlock(self):
        outer = wa.lock_platform("kimi")
        inner = wa.lock_platform("kimi")   # 同进程再取 = 重入，不抛 LockBusy
        self.assertTrue(inner.get("reentrant"), "同进程再取必须判定为重入")
        statelock.release_named(wa.platform_lock_path("kimi"), inner)
        self.assertTrue(os.path.exists(wa.platform_lock_path("kimi")),
                        "内层释放不许把外层的锁删掉")
        statelock.release_named(wa.platform_lock_path("kimi"), outer)
        self.assertFalse(os.path.exists(wa.platform_lock_path("kimi")),
                         "外层释放才真正放锁")

    # ---- plock 查看 / 强拆 ----

    def test_plock_view_and_force_release(self):
        h = wa.lock_platform("deepseek")
        class A:
            release = False
            pplatform = "deepseek"
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = wa.cmd_plock(A())
        self.assertEqual(rc, 0)
        self.assertIn("deepseek", buf.getvalue())

        class B:
            release = True
            pplatform = "deepseek"
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            rc = wa.cmd_plock(B())
        self.assertEqual(rc, 0)
        self.assertEqual(statelock.read_holder_named(wa.platform_lock_path("deepseek")),
                         {}, "强拆后锁必须消失")


class TestExecModeGate(unittest.TestCase):
    """requirement_gate 的执行模式校验（fail-closed）。"""

    @staticmethod
    def _card(mode: str) -> str:
        return f"""# 需求卡片

## 基本信息

| 项 | 内容 |
|---|---|
| 项目名 | t |
| 提出日期 | 2026-10-30 |
| 一句话目标 | 做个东西 |
| 原始往来记录 | docs/requirements/t-transcript.md |
| 执行模式 | {mode} |
| 授权范围 | 整个任务 |
| 无人值守方案 | 两条路都通时选改动最小的；不装新依赖；外部服务挂了换备用；连续失败 2 次就停 |

## 一、已明确

| # | 要求（原话） | 落地方式 | 验收怎么看 |
|---|---|---|---|
| 1 | 要能跑 | 写代码 | 跑通 |

## 二、未明确

| # | 待确认点 | 方案 A | 方案 B | 不同答案的影响 |
|---|---|---|---|---|
| 1 | | | | |

## 三、有冲突

| # | 冲突点 | 冲突双方 | 谁优先 | 依据 |
|---|---|---|---|---|
| 1 | | | | |

## 四、明确不做的事

| # | 不做的事 | 原因 |
|---|---|---|
| 1 | | |

## 五、确认记录

| 轮次 | 日期 | 谁说的 | 原话 | 确认状态 |
|---|---|---|---|---|
| 1 | 2026-10-30 | 用户 | 就这样 | 已确认 |
"""

    def test_missing_mode_rejected(self):
        text = self._card("").replace("| 执行模式 |  |", "")  # 整行删掉
        res = requirement_gate.validate(text)
        codes = [v["code"] for v in res["violations"]]
        self.assertIn("EXEC_MODE_MISSING", codes)
        self.assertFalse(res["ok"])

    def test_placeholder_mode_rejected(self):
        res = requirement_gate.validate(
            self._card("（逐步确认 / 直接执行——记用户原话，二选一；「首次人工确认门」的裁定结果，未确认不许进 LOOP）"))
        codes = [v["code"] for v in res["violations"]]
        self.assertIn("EXEC_MODE_MISSING", codes)

    def test_unknown_wording_rejected(self):
        res = requirement_gate.validate(self._card("看着办"))
        codes = [v["code"] for v in res["violations"]]
        self.assertIn("EXEC_MODE_INVALID", codes)

    def test_both_wordings_accepted(self):
        for mode in ("逐步确认", "直接执行"):
            res = requirement_gate.validate(self._card(mode))
            self.assertTrue(res["ok"], "「%s」应放行，违规：%s" % (mode, res["violations"]))
            self.assertEqual(res["stats"]["exec_mode"], 1)


if __name__ == "__main__":
    unittest.main()
