#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""租约/收割器/窗口归属/无人值守开关单测（0.1.6 未发批次的优化）。

不联网：wa.bsk、wa._transport_is_iab 全部替身；state 落临时目录。
"""

import argparse
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
import env as ev  # noqa: E402
import wa  # noqa: E402
import orchestrator as oc  # noqa: E402


class _Cfg(object):
    def __init__(self, **vals):
        self.path = os.path.join(tempfile.mkdtemp(), "config.json")
        self.vals = vals

    def __enter__(self):
        self._m = mock.patch.dict(os.environ, {"STARPILOT_CONFIG": self.path})
        self._m.__enter__()
        if self.vals:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with io.open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.vals, f)
        return self

    def __exit__(self, *exc):
        return self._m.__exit__(*exc)


def _ns(**kw):
    return argparse.Namespace(**kw)


def _tmp_state(st=None):
    p = os.path.join(tempfile.mkdtemp(), wa.STATE_NAME)
    wa.save_state(p, st or {})
    return p


class TestIdleClose(unittest.TestCase):
    def test_default_900(self):
        with _Cfg():
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("WA_IDLE_CLOSE_S", None)
                self.assertEqual(wa.idle_close_s(), 900)

    def test_env_override(self):
        with _Cfg():
            with mock.patch.dict(os.environ, {"WA_IDLE_CLOSE_S": "120"}):
                self.assertEqual(wa.idle_close_s(), 120)

    def test_config_wins_and_floor(self):
        with _Cfg(idle_close_s=300):
            self.assertEqual(wa.idle_close_s(), 300)
        with _Cfg(idle_close_s=10):
            self.assertEqual(wa.idle_close_s(), 900)  # <60 视为无效配置

    def test_touch_lease_sets_fields(self):
        p = _tmp_state({"session": "s1"})
        st = wa.load_state(p)
        wa.touch_lease(st, p)
        st2 = wa.load_state(p)
        self.assertGreater(st2["lease_until"], time.time())
        self.assertEqual(st2["session"], "s1")

    def test_lease_left(self):
        self.assertGreater(wa.lease_left({"lease_until": time.time() + 100}), 0)
        self.assertLess(wa.lease_left({"lease_until": time.time() - 100}), 0)
        self.assertLessEqual(wa.lease_left({}), 0)


class TestReap(unittest.TestCase):
    def _run(self, a, bsk_side=None):
        with mock.patch.object(wa, "_transport_is_iab", return_value=False), \
                mock.patch.object(wa, "bsk",
                                  side_effect=bsk_side or (lambda args, timeout=0: (0, "ok", ""))):
            return wa.cmd_reap(a)

    def test_expired_session_is_stopped(self):
        p = _tmp_state({"session": "s-old", "lease_until": time.time() - 5})
        rc = self._run(_ns(state=p, ttl=0, watch=False, interval=60,
                           dry_run=False, scan=""))
        self.assertEqual(rc, 0)
        st = wa.load_state(p)
        self.assertEqual(st["session"], "")
        self.assertIn("reaped_at", st)

    def test_live_session_is_untouched(self):
        p = _tmp_state({"session": "s-live", "lease_until": time.time() + 600})
        calls = []
        rc = self._run(_ns(state=p, ttl=0, watch=False, interval=60,
                           dry_run=False, scan=""), bsk_side=lambda a, timeout=0: calls.append(a) or (0, "", ""))
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [])
        self.assertEqual(wa.load_state(p)["session"], "s-live")

    def test_dry_run_reports_without_touching(self):
        p = _tmp_state({"session": "s-old", "lease_until": time.time() - 5})
        rc = self._run(_ns(state=p, ttl=0, watch=False, interval=60,
                           dry_run=True, scan=""))
        self.assertEqual(rc, 0)
        st = wa.load_state(p)
        self.assertEqual(st["session"], "s-old")  # dry-run 不动手

    def test_no_lease_never_killed(self):
        p = _tmp_state({"session": "s-legacy"})  # 老状态没有租约记录
        rc = self._run(_ns(state=p, ttl=0, watch=False, interval=60,
                           dry_run=False, scan=""))
        self.assertEqual(rc, 0)
        self.assertEqual(wa.load_state(p)["session"], "s-legacy")

    def test_scan_multiple_states(self):
        d = tempfile.mkdtemp()
        ps = []
        for sid in ("s1", "s2"):
            sub = os.path.join(d, sid)
            os.makedirs(sub)
            p = os.path.join(sub, wa.STATE_NAME)
            wa.save_state(p, {"session": sid, "lease_until": time.time() - 5})
            ps.append(p)
        rc = self._run(_ns(state="", ttl=0, watch=False, interval=60,
                           dry_run=False, scan=d))
        self.assertEqual(rc, 0)
        for p in ps:
            self.assertEqual(wa.load_state(p)["session"], "")

    def test_iab_transport_noop(self):
        with mock.patch.object(wa, "_transport_is_iab", return_value=True):
            rc = wa.cmd_reap(_ns(state="x", ttl=0, watch=False, interval=60,
                                 dry_run=False, scan=""))
        self.assertEqual(rc, 0)


class TestWindowPlacement(unittest.TestCase):
    def _tab_list(self, agent, user):
        def fake(args, timeout=0):
            if args[:2] == ["tab", "list"]:
                scope = args[args.index("--scope") + 1]
                urls = {"agent": agent, "user": user}[scope]
                return 0, json.dumps([{"url": u} for u in urls]), ""
            return 0, "", ""
        return fake

    def test_bound_in_agent_ok(self):
        st = {"url": "https://chat.deepseek.com/a/chat/s/abc"}
        with mock.patch.object(wa, "bsk", side_effect=self._tab_list(
                [st["url"]], ["https://mail.example.com"])):
            rep = wa.window_report(st, "s1")
        self.assertIs(rep["ok"], True)
        self.assertIs(rep["bound_in_agent"], True)
        self.assertIs(rep["bound_in_user"], False)

    def test_bound_in_user_flagged(self):
        st = {"url": "https://chat.deepseek.com/a/chat/s/abc"}
        with mock.patch.object(wa, "bsk", side_effect=self._tab_list(
                [], [st["url"]])):
            rep = wa.window_report(st, "s1")
        self.assertIs(rep["ok"], False)

    def test_unkown_when_tab_list_fails(self):
        st = {"url": "https://x/a/b"}
        with mock.patch.object(wa, "bsk", return_value=(1, "", "err")):
            rep = wa.window_report(st, "s1")
        self.assertIsNone(rep["ok"])  # 探不到不编

    def test_heal_moves_tab_to_agent_window(self):
        st = {"url": "https://chat.deepseek.com/a/chat/s/abc"}
        calls = []
        seq = {"agent": iter([[], [st["url"]]]),   # 初检空 → 自愈后有
               "user": iter([[st["url"]], []])}    # 初检在用户窗口 → 自愈后无

        def fake(args, timeout=0):
            calls.append(args[:2])
            if args[:2] == ["tab", "list"]:
                scope = args[args.index("--scope") + 1]
                return 0, json.dumps([{"url": u} for u in next(seq[scope])]), ""
            return 0, "", ""
        with mock.patch.object(wa, "bsk", side_effect=fake), \
                mock.patch.object(wa, "navigate_soft", return_value=(0, "", "")):
            rep = wa.heal_window_placement(st, "unused", "s1")
        self.assertIs(rep["ok"], True)
        self.assertIn("成功", rep["heal"])
        self.assertIn(["tab", "create"], calls)

    def test_heal_not_triggered_when_ok(self):
        st = {"url": "https://x/a/b"}
        with mock.patch.object(wa, "bsk", side_effect=self._tab_list([st["url"]], [])):
            rep = wa.heal_window_placement(st, "unused", "s1")
        self.assertNotIn("heal", rep)  # 正常就不动


class TestUnattendedSwitch(unittest.TestCase):
    def test_env_overrides_config(self):
        with _Cfg(unattended=True):
            with mock.patch.dict(os.environ, {"STARPILOT_UNATTENDED": "0"}):
                self.assertFalse(ev.unattended())
            with mock.patch.dict(os.environ, {"STARPILOT_UNATTENDED": "1"}):
                self.assertTrue(ev.unattended())
            self.assertTrue(ev.unattended())  # 无 env → config

    def test_default_off(self):
        with _Cfg():
            self.assertFalse(ev.unattended())

    def test_set_unattended_roundtrip(self):
        with _Cfg():
            ev.set_unattended(True)
            self.assertTrue(ev.load_config()["unattended"])
            ev.set_unattended(False)
            self.assertFalse(ev.load_config()["unattended"])

    def test_cli_set_unattended(self):
        with _Cfg():
            buf = io.StringIO()
            with mock.patch.object(ev.sys, "stdout", buf):
                rc = ev.main(["--set-unattended", "on"])
            self.assertEqual(rc, 0)
            self.assertTrue(ev.load_config()["unattended"])
            self.assertIn("写闸门", buf.getvalue())  # 安全边界必须说清

    def test_json_reports_unattended(self):
        with _Cfg(unattended=True):
            buf = io.StringIO()
            with mock.patch.object(ev, "find_bsk", return_value="/x/bsk"), \
                    mock.patch.object(ev, "bsk_version", return_value="0.4.0"), \
                    mock.patch.object(ev.sys, "stdout", buf):
                ev.main(["--json", "--no-doctor"])
            self.assertTrue(json.loads(buf.getvalue())["unattended"])

    def test_set_idle_close_roundtrip(self):
        with _Cfg():
            ev.set_idle_close(300)
            self.assertEqual(ev.load_config()["idle_close_s"], 300)
            with self.assertRaises(SystemExit):
                ev.set_idle_close(10)


class TestExecModeDefault(unittest.TestCase):
    def test_explicit_cli_wins(self):
        self.assertEqual(oc._resolve_exec_mode("step", None), "step")

    def test_unattended_switch_gives_auto(self):
        with _Cfg(unattended=True):
            self.assertEqual(oc._resolve_exec_mode("", None), "auto")

    def test_without_switch_stays_step(self):
        with _Cfg():
            self.assertEqual(oc._resolve_exec_mode("", None), "step")

    def test_card_mode_beats_switch(self):
        with _Cfg(unattended=True):
            self.assertEqual(oc._resolve_exec_mode(
                "", {"stats": {"exec_mode_code": "step"}}), "step")


if __name__ == "__main__":
    unittest.main()
