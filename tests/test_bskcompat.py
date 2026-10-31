# -*- coding: utf-8 -*-
"""bskcompat：bsk 0.4.0 cooperate 上下文兼容层的回归测试。

背景见 lessons.md L-BSK040：0.4.0 起触页面动作要求 --context（来自 cooperate
sync），一个 context 只授权一次动作。兼容层必须在「缺 context」错误上自动
sync→重试，且本地命令不做补救、上下文竞态最多补救两轮。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bskcompat  # noqa: E402

NEED_CTX_ERR = (
    1, "", "error: shared page control or observation changed\n"
           "details: This action requires the context returned by the latest cooperate sync"
)
CONTENT_CHANGED_ERR = (
    1, "", "error: shared page control or observation changed\n"
           "details: The action's observed content changed; synchronize and replan"
)
SYNC_OK = (0, '{"context_id": "ctx-1", "state": {"mode": "ready"}}', "")


class TestBskCompat(unittest.TestCase):
    def setUp(self):
        bskcompat.set_bin("bsk-fake")
        self._fake = None
        bskcompat.set_run_fn(self._fake_run)

    def _fake_run(self, args, timeout):
        if self._fake is None:
            raise AssertionError("意外触达真实 bsk 调用：%s" % args)
        return self._fake(args, timeout)

    def tearDown(self):
        bskcompat.set_run_fn(None)

    def test_no_error_no_sync(self):
        calls = []

        def fake(args, timeout):
            calls.append(list(args))
            return 0, '{"value": 1}', ""

        self._fake = fake
        rc, out, err = bskcompat.run(fake, ["evaluate", "1+1", "--session", "s1"], 30)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)          # 没补救：不额外 sync
        self.assertNotIn("--context", calls[0])

    def test_missing_context_triggers_sync_and_retry(self):
        calls = []

        def fake(args, timeout):
            calls.append(list(args))
            if args[0] == "cooperate":
                return SYNC_OK
            if "--context" not in args:
                return NEED_CTX_ERR
            return 0, '{"value": 2}', ""

        self._fake = fake
        rc, _, _ = bskcompat.run(fake, ["evaluate", "1+1", "--session", "s1"], 30)
        self.assertEqual(rc, 0)
        ev = [c for c in calls if c[0] == "evaluate"]
        self.assertEqual(len(ev), 2)
        self.assertEqual(ev[1][ev[1].index("--context") + 1], "ctx-1")

    def test_content_changed_retries_fresh_context(self):
        """页面内容竞态：第二次也要重新 sync 拿新 context，最多两轮。"""
        calls = []
        n_sync = [0]

        def fake(args, timeout):
            calls.append(list(args))
            if args[0] == "cooperate" and "sync" in args:
                n_sync[0] += 1
            if args[0] == "cooperate":
                return (0, '{"context_id": "ctx-%d"}' % max(1, n_sync[0]), "")
            return CONTENT_CHANGED_ERR

        self._fake = fake
        rc, _, _ = bskcompat.run(fake, ["evaluate", "x", "--session", "s1"], 30)
        self.assertNotEqual(rc, 0)               # 页面持续变动：两轮补救后如实失败
        self.assertEqual(n_sync[0], 2)

    def test_local_cmds_never_rescued(self):
        calls = []

        def fake(args, timeout):
            calls.append(list(args))
            return NEED_CTX_ERR

        self._fake = fake
        bskcompat.run(fake, ["session", "start", "--no-focus"], 30)
        self.assertEqual(len(calls), 1)          # 本地命令不 sync
        self.assertNotIn("cooperate", calls[0])

    def test_stopped_window_not_rescued(self):
        """窗口被关（stopped=true）救不了：原样失败，不上抛假成功。"""
        calls = []

        def fake(args, timeout):
            calls.append(list(args))
            if args[0] == "cooperate" and args[1] == "wait":
                return (0, '{"stopped": true, "ready": false}', "")
            if args[0] == "cooperate":
                return SYNC_OK
            return NEED_CTX_ERR

        self._fake = fake
        rc, _, _ = bskcompat.run(fake, ["evaluate", "x", "--session", "s1"], 30)
        self.assertNotEqual(rc, 0)

    def test_unrelated_error_not_rescued(self):
        def fake(args, timeout):
            return 1, "", "error: permission denied for tab"

        self._fake = fake
        rc, _, _ = bskcompat.run(fake, ["evaluate", "x", "--session", "s1"], 30)
        self.assertNotEqual(rc, 0)

    def test_old_context_stripped_before_retry(self):
        calls = []

        def fake2(args, timeout):
            calls.append(list(args))
            if args[0] == "cooperate":
                return (0, '{"context_id": "ctx-2"}', "")
            if "--context" in args and args[args.index("--context") + 1] == "ctx-1":
                return CONTENT_CHANGED_ERR
            if "--context" in args and args[args.index("--context") + 1] == "ctx-2":
                return 0, '{"value": 9}', ""
            return NEED_CTX_ERR

        self._fake = fake2
        rc, _, _ = bskcompat.run(
            fake2, ["evaluate", "x", "--session", "s1", "--context", "ctx-1"], 30)
        self.assertEqual(rc, 0)
        last = [c for c in calls if c[0] == "evaluate"][-1]
        self.assertNotIn("ctx-1", last)          # 旧 context 已剥掉，不会叠两个


if __name__ == "__main__":
    unittest.main()
