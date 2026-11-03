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
        bskcompat._VER = None        # 版本缓存跨用例隔离
        self._fake = None
        bskcompat.set_run_fn(self._fake_run)

    def _fake_run(self, args, timeout):
        if self._fake is None:
            raise AssertionError("意外触达真实 bsk 调用：%s" % args)
        return self._fake(args, timeout)

    def tearDown(self):
        bskcompat.set_run_fn(None)
        bskcompat._VER = None

    def test_no_error_no_sync(self):
        calls = []

        def fake(args, timeout):
            calls.append(list(args))
            return 0, '{"value": 1}', ""

        self._fake = fake
        rc, out, err = bskcompat.run(fake, ["evaluate", "1+1", "--session", "s1"], 30)
        self.assertEqual(rc, 0)
        ev = [c for c in calls if c[0] == "evaluate"]
        self.assertEqual(len(ev), 1)             # 没补救：不额外 sync
        self.assertNotIn("--context", ev[0])
        self.assertEqual([c for c in calls if c[0] == "cooperate"], [])

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


V041_SYNC = (0, '{"context_id": "ctx-9", "observation_id": "obs-9", "state": {}}', "")


class TestBsk041ControlContext(unittest.TestCase):
    """bsk 0.4.1（2026-11-02 自动升级）新增 strict observation 要求。

    0.4.1 起触页面动作缺的不只是 --context，还要一次性的 --observation
    （来自同一次 cooperate sync）。错误文案也换了（control context /
    unused strict observation / media source changed），必须认得并预注入，
    否则整条发送链每步都「一上页面就报错」。
    """

    def setUp(self):
        bskcompat.set_bin("bsk-fake")
        bskcompat._VER = None
        self.calls = []

        def unexpected(args, timeout):
            raise AssertionError("意外触达真实 bsk 调用：%s" % args)
        bskcompat.set_run_fn(unexpected)

    def tearDown(self):
        bskcompat.set_run_fn(None)
        bskcompat._VER = None

    def _run(self, ver=(0, 4, 1), sync=V041_SYNC, act=(0, '{"ok": 1}', "")):
        def fake(args, timeout):
            self.calls.append(list(args))
            if args[0] == "--version":
                return 0, "bsk %d.%d.%d" % ver, ""
            if args[0] == "cooperate":
                return sync
            return act(args, timeout) if callable(act) else act
        return fake

    def test_preinjects_context_and_observation(self):
        fake = self._run()
        bskcompat.set_run_fn(fake)
        rc, _, _ = bskcompat.run(fake, ["navigate", "https://x", "--session", "s1"], 30)
        self.assertEqual(rc, 0)
        act = [c for c in self.calls if c[0] == "navigate"]
        self.assertEqual(len(act), 1, "0.4.1 应预注入，一次做成，不必先失败一次")
        self.assertEqual(act[0][act[0].index("--context") + 1], "ctx-9")
        self.assertEqual(act[0][act[0].index("--observation") + 1], "obs-9")

    def test_040_no_observation_and_no_preinject(self):
        """0.4.0 只认 --context：不预注入、不传 --observation（多给参数会报错）。"""
        def fake(args, timeout):
            self.calls.append(list(args))
            if args[0] == "--version":
                return 0, "bsk 0.4.0", ""
            if args[0] == "cooperate":
                return V041_SYNC
            if "--context" not in args:
                return NEED_CTX_ERR
            return 0, '{"ok": 1}', ""

        bskcompat.set_run_fn(fake)
        rc, _, _ = bskcompat.run(fake, ["navigate", "https://x", "--session", "s1"], 30)
        self.assertEqual(rc, 0)
        act = [c for c in self.calls if c[0] == "navigate"]
        self.assertEqual(len(act), 2, "0.4.0 保持老路径：先执行、失败才补救")
        for c in act:
            self.assertNotIn("--observation", c)

    def test_041_error_text_rescued(self):
        """0.4.1 的新错误文案必须能触发补救，否则等于不兼容。"""
        def fake(args, timeout):
            self.calls.append(list(args))
            if args[0] == "--version":
                return 0, "bsk 0.4.1", ""
            if args[0] == "cooperate":
                return V041_SYNC
            if "--observation" not in args:
                return (1, "", "error: This action requires a valid control context "
                               "from cooperate sync")
            return 0, '{"ok": 1}', ""

        bskcompat.set_run_fn(fake)
        rc, _, _ = bskcompat.run(fake, ["fill", "--value", "x", "--session", "s1"], 30)
        self.assertEqual(rc, 0)
        self.assertTrue(any("--observation" in c for c in self.calls if c[0] == "fill"))

    def test_strip_removes_stale_observation(self):
        fake = self._run()
        bskcompat.set_run_fn(fake)
        rc, _, _ = bskcompat.run(fake, ["press", "Enter", "--session", "s1",
                                        "--context", "old", "--observation", "old-o"], 30)
        self.assertEqual(rc, 0)
        act = [c for c in self.calls if c[0] == "press"][-1]
        self.assertEqual(act.count("--context"), 1)
        self.assertEqual(act.count("--observation"), 1)
        self.assertNotIn("old-o", act)

    def test_tab_cmds_need_no_context(self):
        """tab select/create/close 是会话内操作，不该被注入页面上下文。"""
        fake = self._run()
        bskcompat.set_run_fn(fake)
        rc, _, _ = bskcompat.run(fake, ["tab", "select", "--session", "s1", "123"], 30)
        self.assertEqual(rc, 0)
        self.assertEqual([c for c in self.calls if c[0] == "cooperate"], [])

    def test_sync_rc_nonzero_is_not_success(self):
        """sync 返回 rc!=0（frame not responding）绝不能当成功用。

        2026-11-02 现场病因：旧实现只看返回里有没有 context_id —— frame 卡住时
        sync 返回错误 JSON（无 context_id）本该判失败，但因为没检查 rc、且错误
        文案没进 _NEEDS_CONTEXT，动作被当成「带了上下文」发出去，实际不带，
        于是回 `requires an unused strict observation`，把真实病因（页面 frame
        不响应）完全盖住，排查绕了一大圈。
        """
        seq = {"sync": 0, "wait": 0}

        def fake(args, timeout):
            self.calls.append(list(args))
            if args[0] == "--version":
                return 0, "bsk 0.4.1", ""
            if args[0] == "cooperate" and "wait" in args:
                seq["wait"] += 1
                return 0, '{"ready": true, "stopped": false}', ""
            if args[0] == "cooperate" and "sync" in args:
                seq["sync"] += 1
                if seq["sync"] == 1:      # 第一次：frame 不响应
                    return (1, '{"code": "permission_denied", '
                               '"message": "A page frame is not responding", '
                               '"data": {"reason": "cooperation_sync_required"}}', "")
                return V041_SYNC            # wait 之后就绪
            return 0, '{"ok": 1}', ""

        bskcompat.set_run_fn(fake)
        rc, _, _ = bskcompat.run(fake, ["navigate", "https://x", "--session", "s1"], 30)
        self.assertEqual(rc, 0)
        self.assertEqual(seq["wait"], 1, "sync 失败后应按 hint 先 wait 再重试")
        self.assertEqual(seq["sync"], 2)
        act = [c for c in self.calls if c[0] == "navigate"][-1]
        self.assertIn("--observation", act)

    def test_sync_never_returns_bad_pair(self):
        """sync 一直失败 → 动作照常发（不带上下文）并如实失败，不伪造上下文。"""
        def fake(args, timeout):
            self.calls.append(list(args))
            if args[0] == "--version":
                return 0, "bsk 0.4.1", ""
            if args[0] == "cooperate":
                return (1, '{"message": "A page frame is not responding"}', "")
            return (1, "", "error: page stuck")

        bskcompat.set_run_fn(fake)
        self.assertIsNone(bskcompat._sync_context("s1"))


if __name__ == "__main__":
    unittest.main()
