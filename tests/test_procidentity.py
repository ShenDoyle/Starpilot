#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""procidentity 单元测试：process_start_token 可验证进程身份（03-R1 §3.2）。"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import procidentity  # noqa: E402


class TestProcIdentity(unittest.TestCase):
    def test_self_token_format(self):
        tok = procidentity.self_token()
        self.assertNotEqual(tok, "unknown", "本进程身份必须可取")
        self.assertTrue(tok.startswith(("win-ft:", "posix:")), tok)

    def test_self_token_is_stable(self):
        self.assertEqual(procidentity.self_token(), procidentity.self_token())

    def test_executor_id_contains_pid_host_token(self):
        ex = procidentity.executor_id(run_id="r1", owner="wa")
        self.assertIn(str(os.getpid()), ex)
        self.assertIn(procidentity.self_token(), ex)
        self.assertTrue(ex.startswith("ex-"))

    def test_dead_pid_is_dead(self):
        # 999999 不是合理存在的 pid；即便偶发存在，状态也绝不可能是 match
        status = procidentity.token_status(999999, "win-ft:1")
        self.assertIn(status, ("dead", "mismatch"))

    def test_alive_pid_with_self_token_matches(self):
        self.assertEqual(procidentity.token_status(os.getpid(),
                                                   procidentity.self_token()),
                         "match")

    def test_alive_pid_with_wrong_token_is_mismatch_pid_reuse(self):
        self.assertEqual(procidentity.token_status(os.getpid(), "win-ft:12345"),
                         "mismatch", "PID 复用（token 不一致）必须可检出")

    def test_missing_token_is_unknown_fail_closed(self):
        self.assertEqual(procidentity.token_status(os.getpid(), ""),
                         "unknown", "缺 token 一律 fail-closed")

    def test_spawned_process_token_differs_from_parent(self):
        import subprocess
        import time
        code = "import procidentity;print(procidentity.self_token())"
        p = subprocess.Popen([sys.executable, "-c", code],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, cwd=os.path.join(ROOT, "scripts"))
        out, _err = p.communicate(timeout=30)
        child_tok = out.strip()
        self.assertTrue(child_tok.startswith(("win-ft:", "posix:")), child_tok)
        self.assertNotEqual(child_tok, procidentity.self_token(),
                            "子进程与父进程身份必须不同（防 PID 混淆）")
        # 子进程已退出：先释放父进程持有的句柄（Windows 进程对象在句柄关闭
        # 前仍可被 OpenProcess 查到），再验尸应判 dead
        # （若 pid 被瞬间复用则判 mismatch，两者都证明原进程已不存在）
        p.wait()
        pid_saved = p.pid
        del p
        import gc
        gc.collect()
        deadline = time.time() + 3.0
        st = procidentity.token_status(pid_saved, child_tok)
        while st == "match" and time.time() < deadline:
            time.sleep(0.2)
            st = procidentity.token_status(pid_saved, child_tok)
        self.assertIn(st, ("dead", "mismatch"), st)


if __name__ == "__main__":
    unittest.main()
