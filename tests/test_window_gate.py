#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""窗口归属三态门单测（2026-11-02 GPT 审计 P0-3 + D08/D09；复审阶段一收紧版）。

GPT 裁定：window.ok=null 不得放行受保护操作（send/clear 是写路径，打错窗口=
往用户窗口打字）。三态：verified 放行 / misplaced 禁止 / unknown 禁止。
复审阶段一（2026-11-02）：逃生阀 WA_ALLOW_UNVERIFIED_WINDOW 已删除——env 置了
也必须拒绝、不落 P0-ESC 台账；无会话 = unknown 同拒；clear 同受门约束；
人工接管走 wa.py window status / release 显式流程。
不联网：window_report / heal_window_placement / _transport_is_iab 全替身。
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
import wa  # noqa: E402


def _state(tmp, **kw):
    st = {"url": "https://chatgpt.com/c/11111111-2222-3333-4444-555555555555",
          "platform": "chatgpt", "session": "sess1", "browser": "inst1"}
    st.update(kw)
    p = os.path.join(tmp, "wa-state.json")
    wa.save_state(p, st)
    return p, st


class TestWindowVerifiedTriState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path, self.st = _state(self.tmp)

    def test_d08_unknown_blocks_send(self):
        """D08：归属未知（探测连续失败）→ send 门拒绝。"""
        with mock.patch.object(wa, "window_report", return_value={"ok": None}), \
                mock.patch.object(wa, "_transport_is_iab", return_value=False), \
                mock.patch.object(wa.time, "sleep"):
            v = wa.window_verified(self.st, self.path, "sess1", attempts=2)
        self.assertEqual(v, "unknown")

    def test_false_blocks_even_after_failed_heal(self):
        """misplaced：自愈失败后仍为 false → 禁止。"""
        with mock.patch.object(wa, "window_report", return_value={"ok": False}), \
                mock.patch.object(wa, "heal_window_placement",
                                  return_value={"ok": False}), \
                mock.patch.object(wa, "_transport_is_iab", return_value=False), \
                mock.patch.object(wa.time, "sleep"):
            v = wa.window_verified(self.st, self.path, "sess1")
        self.assertEqual(v, "misplaced")

    def test_verified_true_short_circuit(self):
        with mock.patch.object(wa, "window_report", return_value={"ok": True}), \
                mock.patch.object(wa, "_transport_is_iab", return_value=False):
            v = wa.window_verified(self.st, self.path, "sess1")
        self.assertEqual(v, "verified")
        self.assertEqual(self.st["window_verified"], "verified")
        self.assertLess(time.time() - self.st["window_checked_at"], 10)

    def test_heal_success_flips_to_verified(self):
        calls = {"n": 0}

        def report(st, sid):
            calls["n"] += 1
            return {"ok": calls["n"] > 1}

        with mock.patch.object(wa, "window_report", report), \
                mock.patch.object(wa, "heal_window_placement",
                                  return_value={"ok": True}) as mh, \
                mock.patch.object(wa, "_transport_is_iab", return_value=False):
            v = wa.window_verified(self.st, self.path, "sess1")
        self.assertEqual(v, "verified")
        mh.assert_called_once()


class TestRequireWindowVerified(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path, self.st = _state(self.tmp)

    def test_fresh_verified_passes_without_probe(self):
        """新鲜的 verified 不再探测（省一次 tab list）。"""
        self.st["window_verified"] = "verified"
        self.st["window_checked_at"] = time.time()
        with mock.patch.object(wa, "window_verified",
                               side_effect=AssertionError("不应再探测")), \
                mock.patch.object(wa, "_transport_is_iab", return_value=False):
            ok, msg = wa.require_window_verified(self.st, self.path, "sess1")
        self.assertTrue(ok)

    def test_stale_verified_reprobes(self):
        """超过 TTL 的 verified 必须重新探测（归属不是永久有效的）。"""
        self.st["window_verified"] = "verified"
        self.st["window_checked_at"] = time.time() - wa.WINDOW_CHECK_TTL_S - 10
        with mock.patch.object(wa, "window_verified",
                               return_value="misplaced") as mv, \
                mock.patch.object(wa, "_transport_is_iab", return_value=False):
            ok, msg = wa.require_window_verified(self.st, self.path, "sess1")
        self.assertFalse(ok)
        mv.assert_called_once()
        self.assertIn("Agent Window", msg)

    def test_d08_unknown_refused_no_escape_hint(self):
        """unknown 拒绝，且拒绝理由里不再出现任何逃生阀（已删除）。"""
        self.st["window_verified"] = "unknown"
        self.st["window_checked_at"] = time.time()
        with mock.patch.object(wa, "_transport_is_iab", return_value=False):
            ok, msg = wa.require_window_verified(self.st, self.path, "sess1")
        self.assertFalse(ok)
        self.assertNotIn("WA_ALLOW_UNVERIFIED_WINDOW", msg)
        self.assertIn("window release", msg)  # 指向显式人工接管流程

    def test_iab_transport_exempt(self):
        """IAB 传输无窗口概念，不受门约束（SKILL §5.1；传输形态而非参数绕过）。"""
        with mock.patch.object(wa, "_transport_is_iab", return_value=True):
            ok, _ = wa.require_window_verified(self.st, self.path, "sess1")
        self.assertTrue(ok)

    def test_no_session_is_unknown_and_refused(self):
        """复审阶段一：无会话 = 归属未知 → 拒绝（堵「无会话直发」入口）。"""
        ok, msg = wa.require_window_verified(self.st, self.path, "")
        self.assertFalse(ok)
        self.assertIn("bind", msg)


class TestEscapeHatchRemoved(unittest.TestCase):
    """复审阶段一验收：逃生阀删除——env 置了也拒绝、不落账、不消费。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path, self.st = _state(self.tmp)

    def test_env_var_has_no_power(self):
        self.st["window_verified"] = "unknown"
        self.st["window_checked_at"] = time.time()
        env = {"WA_ALLOW_UNVERIFIED_WINDOW": "用户明示允许（测试）"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(wa, "_transport_is_iab", return_value=False):
            ok, _ = wa.require_window_verified(self.st, self.path, "sess1")
            self.assertFalse(ok)  # 环境变量不再是钥匙
            self.assertIn("WA_ALLOW_UNVERIFIED_WINDOW", os.environ)  # 也不被消费
        # 不落 P0-ESC 台账
        ledgers = []
        for root, dirs, files in os.walk(self.tmp):
            ledgers += [os.path.join(root, f) for f in files
                        if f.endswith(".md") and "pending" in f.lower()]
        self.assertEqual(ledgers, [], "逃生阀已删除，不应再产生任何 P0-ESC 台账")

    def test_misplaced_refused_no_escape(self):
        self.st["window_verified"] = "misplaced"
        self.st["window_checked_at"] = time.time()
        with mock.patch.object(wa, "_transport_is_iab", return_value=False):
            ok, msg = wa.require_window_verified(self.st, self.path, "sess1")
        self.assertFalse(ok)
        self.assertNotIn("WA_ALLOW_UNVERIFIED_WINDOW", msg)

    def test_d09_no_decision_needed_for_authorized_detail(self):
        """D09（裁定侧）：归属已验证 = 已授权范围内的普通操作，不要求任何裁定记录。"""
        self.st["window_verified"] = "verified"
        self.st["window_checked_at"] = time.time()
        with mock.patch.object(wa, "_transport_is_iab", return_value=False):
            ok, _ = wa.require_window_verified(self.st, self.path, "sess1")
        self.assertTrue(ok)  # 无 decisions 台账、无裁定记录 → 照常放行


class TestWindowCommand(unittest.TestCase):
    """显式人工接管：window status（只读）/ release（须 --confirm，停会话清缓存）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path, self.st = _state(self.tmp)
        self.st["window_verified"] = "misplaced"
        self.st["window_checked_at"] = time.time()
        wa.save_state(self.path, self.st)

    def _run(self, argv):
        import contextlib
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", ["wa.py"] + argv), \
                contextlib.redirect_stdout(buf):
            rc = wa.main()
        return rc, buf.getvalue()

    def test_release_without_confirm_refused(self):
        rc, _ = self._run(["--state", self.path, "window", "release"])
        self.assertEqual(rc, 6)
        st = wa.load_state(self.path)
        self.assertEqual(st.get("window_verified"), "misplaced")  # 未被清

    def test_release_with_confirm_stops_session_and_clears(self):
        with mock.patch.object(wa, "bsk") as mb:
            rc, out = self._run(["--state", self.path, "window", "release",
                                 "--confirm", "人工确认（测试）"])
        self.assertEqual(rc, 0)
        mb.assert_called_once()  # session stop 被调用（释放窗口）
        args = mb.call_args[0][0]
        self.assertEqual(args[:2], ["session", "stop"])
        st = wa.load_state(self.path)
        self.assertIsNone(st.get("window_verified"))  # 归属缓存已清
        self.assertIsNone(st.get("window_checked_at"))
        self.assertIn("bind", out)  # 指引重新 bind

    def test_status_is_readonly(self):
        rc, out = self._run(["--state", self.path, "window", "status"])
        self.assertEqual(rc, 0)
        self.assertIn("misplaced", out)
        st = wa.load_state(self.path)
        self.assertEqual(st.get("window_verified"), "misplaced")  # 未被改动


class TestWriteGateAlsoGated(unittest.TestCase):
    """复审阶段一绕过入口盘点：编排器真实写（write_gate cmd_send）此前完全绕过
    窗口门——现在必须同门约束：unknown → WRITE_BLOCKED_WINDOW_UNVERIFIED。"""

    def setUp(self):
        import json
        import write_gate as wg
        self.wg = wg
        self.root = tempfile.mkdtemp()
        self.run_id = "20261102T000000-wgtest"
        self.aid = "ap-w1"
        wdir = os.path.join(self.root, "runs", self.run_id, "writes")
        os.makedirs(wdir, exist_ok=True)
        url = "https://www.kimi.com/chat/abc12345"
        text = "窗口门测试正文"
        json.dump({"approval_id": self.aid, "platform": "kimi",
                   "write_method": "exec",
                   "target_url_sha256": wg.sha16(url),
                   "prompt_file": os.path.join(self.root, "prompt.txt"),
                   "prompt_sha256": wg.sha16(text)},
                  open(os.path.join(wdir, "%s.dry.json" % self.aid), "w",
                       encoding="utf-8"))
        open(os.path.join(self.root, "prompt.txt"), "w", encoding="utf-8").write(text)
        json.dump({"approval_id": self.aid, "expires_at": 9e12,
                   "target_url_sha256": wg.sha16(url), "prompt_sha256": wg.sha16(text)},
                  open(os.path.join(wdir, "%s.approval.json" % self.aid), "w",
                       encoding="utf-8"))
        st = {"url": url, "platform": "kimi", "session": "sess1",
              "browser": "inst1",
              "window_verified": "unknown", "window_checked_at": time.time()}
        wg.atomic_write_json(os.path.join(self.root, ".wa-state.json"), st)
        os.environ["STARPILOT_WRITE_MODE"] = "single_write"
        self.addCleanup(os.environ.pop, "STARPILOT_WRITE_MODE", None)

    def _run(self):
        import contextlib
        import io as _io
        buf = _io.StringIO()
        args = mock.Mock(approval=self.aid, run=self.run_id,
                         workspace=self.root, state="", platform="",
                         round=1, force=False, send_id="")
        with mock.patch.object(self.wg.wa, "ensure_session",
                               return_value="sess1"), \
                contextlib.redirect_stdout(buf):
            rc = self.wg.cmd_send(args)
        return rc, buf.getvalue()

    def test_real_write_refused_when_window_unknown(self):
        rc, out = self._run()
        res = __import__("json").loads(out)
        self.assertEqual(res.get("error_code"), "WRITE_BLOCKED_WINDOW_UNVERIFIED")
        self.assertNotEqual(rc, 0)

    def test_real_write_passes_when_window_verified(self):
        st = self.wg.wa.load_state(os.path.join(self.root, ".wa-state.json"))
        st["window_verified"] = "verified"
        st["window_checked_at"] = time.time()
        self.wg.wa.save_state(os.path.join(self.root, ".wa-state.json"), st)
        # 门放行后走到 dom_snapshot（联网）——替身掉，验证「没被窗口门拦」即可
        with mock.patch.object(self.wg, "dom_snapshot",
                               return_value={"url": st["url"], "editor": True,
                                             "editor_len": 0, "modal": False,
                                             "raw": {}}):
            rc, out = self._run()
        res = __import__("json").loads(out)
        self.assertNotEqual(res.get("error_code"),
                            "WRITE_BLOCKED_WINDOW_UNVERIFIED",
                            "verified 时不得被窗口门拦截")


class TestClearAlsoGated(unittest.TestCase):
    """clear 也是写路径：unknown 同拒（复审阶段一：受保护操作=向页面写入）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path, self.st = _state(self.tmp)
        self.st["window_verified"] = "unknown"
        self.st["window_checked_at"] = time.time()
        wa.save_state(self.path, self.st)

    def test_clear_refused_when_unknown(self):
        import contextlib
        buf, err = io.StringIO(), io.StringIO()
        ns = mock.Mock(state=self.path, platform="chatgpt")
        with mock.patch.object(wa, "_transport_is_iab", return_value=False), \
                contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(err):
            rc = wa.cmd_clear(ns)
        self.assertEqual(rc, 6)
        self.assertIn("WINDOW_UNVERIFIED", err.getvalue())


if __name__ == "__main__":
    unittest.main()
