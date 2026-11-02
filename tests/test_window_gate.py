#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""窗口归属三态门单测（2026-11-02 GPT 审计 P0-3 + D08/D09）。

GPT 裁定：window.ok=null 不得放行受保护操作（send 是写路径，打错窗口=往用户
窗口打字）。三态：verified 放行 / misplaced 禁止 / unknown 禁止；逃生阀
WA_ALLOW_UNVERIFIED_WINDOW 单次消费 + 落账 P0-ESC（D09：授权路径不打扰）。
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
        def rep_ok_first_false_then_true(st, sid):
            # 第一次探测 False → 自愈 → 第二次 True
            return {"ok": (self._calls.setdefault("n", 0) or True)}
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

    def test_d08_unknown_refused_with_escape_hint(self):
        self.st["window_verified"] = "unknown"
        self.st["window_checked_at"] = time.time()
        with mock.patch.object(wa, "_transport_is_iab", return_value=False):
            ok, msg = wa.require_window_verified(self.st, self.path, "sess1")
        self.assertFalse(ok)
        self.assertIn("WA_ALLOW_UNVERIFIED_WINDOW", msg)

    def test_iab_transport_exempt(self):
        """IAB 传输无窗口概念，不受门约束（SKILL §5.1）。"""
        with mock.patch.object(wa, "_transport_is_iab", return_value=True):
            ok, _ = wa.require_window_verified(self.st, self.path, "sess1")
        self.assertTrue(ok)

    def test_no_session_passes_through(self):
        ok, _ = wa.require_window_verified(self.st, self.path, "")
        self.assertTrue(ok)


class TestEscapeHatch(unittest.TestCase):
    """D09：逃生门单次消费 + 落账；授权范围内的正常操作不受影响。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path, self.st = _state(self.tmp)

    def test_escape_allows_once_and_consumed(self):
        self.st["window_verified"] = "unknown"
        self.st["window_checked_at"] = time.time()
        env = {"WA_ALLOW_UNVERIFIED_WINDOW": "用户明示允许（测试）"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(wa, "_transport_is_iab", return_value=False):
            ok1, _ = wa.require_window_verified(self.st, self.path, "sess1")
            self.assertTrue(ok1)
            self.assertNotIn("WA_ALLOW_UNVERIFIED_WINDOW", os.environ)  # 单次消费
            ok2, msg2 = wa.require_window_verified(self.st, self.path, "sess1")
            self.assertFalse(ok2)  # 第二次没票了，照拒
        # 落账 P0-ESC
        led = None
        for root, dirs, files in os.walk(self.tmp):
            for f in files:
                if f.endswith(".md") and "pending" in f.lower():
                    led = os.path.join(root, f)
        self.assertIsNotNone(led, "P0-ESC 台账未落盘")
        body = io.open(led, encoding="utf-8").read()
        self.assertIn("P0-ESC", body)
        self.assertIn("unverified_window_send", body)

    def test_d09_no_decision_needed_for_authorized_detail(self):
        """D09（裁定侧）：归属已验证 = 已授权范围内的普通操作，不要求任何裁定记录。"""
        self.st["window_verified"] = "verified"
        self.st["window_checked_at"] = time.time()
        with mock.patch.object(wa, "_transport_is_iab", return_value=False):
            ok, _ = wa.require_window_verified(self.st, self.path, "sess1")
        self.assertTrue(ok)  # 无 decisions 台账、无裁定记录 → 照常放行


if __name__ == "__main__":
    unittest.main()
