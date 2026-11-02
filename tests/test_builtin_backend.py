#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内置浏览器后端（0.1.6）单测：后端配置、判定顺序、env.py 出入口、
platforms.py spec CLI、wa.py builtin 守卫。

不联网、不碰真实 HOME：配置一律经 STARPILOT_CONFIG 重定向到临时目录。
"""

import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
import env as ev  # noqa: E402
import platforms as pl  # noqa: E402
import wa  # noqa: E402


class _Cfg(object):
    """STARPILOT_CONFIG 指向临时文件的上下文。"""

    def __init__(self):
        self.path = os.path.join(tempfile.mkdtemp(), "config.json")

    def __enter__(self):
        self._m = mock.patch.dict(os.environ, {"STARPILOT_CONFIG": self.path})
        self._m.__enter__()
        return self

    def __exit__(self, *exc):
        return self._m.__exit__(*exc)


def _write_cfg(d):
    p = os.environ["STARPILOT_CONFIG"]
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with io.open(p, "w", encoding="utf-8") as f:
        json.dump(d, f)


class TestBackendConfig(unittest.TestCase):
    def test_default_path_under_home(self):
        with _Cfg():
            del os.environ["STARPILOT_CONFIG"]
            with mock.patch.object(ev.os.path, "expanduser", return_value="/home/x"):
                self.assertEqual(ev.config_path(),
                                 os.path.join("/home/x", ".starpilot", "config.json"))

    def test_missing_config_is_empty(self):
        with _Cfg():
            self.assertEqual(ev.load_config(), {})
            self.assertFalse(os.path.exists(os.environ["STARPILOT_CONFIG"]))

    def test_corrupt_config_is_empty_not_crash(self):
        with _Cfg():
            p = os.environ["STARPILOT_CONFIG"]
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with io.open(p, "w", encoding="utf-8") as f:
                f.write("{not json")
            self.assertEqual(ev.load_config(), {})

    def test_set_backend_roundtrip(self):
        with _Cfg():
            ev.set_backend("builtin")
            self.assertEqual(ev.load_config()["browser_backend"], "builtin")
            ev.set_backend("bsk")
            self.assertEqual(ev.load_config()["browser_backend"], "bsk")
            self.assertIn("backend_decided_at", ev.load_config())

    def test_set_backend_rejects_unknown(self):
        with _Cfg():
            with self.assertRaises(SystemExit):
                ev.set_backend("opera")


class TestResolveBackend(unittest.TestCase):
    def test_flag_wins_over_config(self):
        with _Cfg():
            _write_cfg({"browser_backend": "builtin"})
            self.assertEqual(ev.resolve_backend("bsk"), "bsk")

    def test_config_beats_auto(self):
        with _Cfg():
            _write_cfg({"browser_backend": "builtin"})
            self.assertEqual(ev.resolve_backend(bsk_found="/x/bsk"), "builtin")

    def test_auto_bsk_found(self):
        with _Cfg():
            self.assertEqual(ev.resolve_backend(bsk_found="/x/bsk"), "bsk")

    def test_unconfigured_when_nothing(self):
        with _Cfg():
            self.assertEqual(ev.resolve_backend(), "unconfigured")

    def test_garbage_config_ignored(self):
        with _Cfg():
            _write_cfg({"browser_backend": "opera"})
            self.assertEqual(ev.resolve_backend(), "unconfigured")


class TestCheckBackend(unittest.TestCase):
    def test_builtin_backend_ok_without_bsk(self):
        with _Cfg():
            _write_cfg({"browser_backend": "builtin"})
            with mock.patch.object(ev, "find_bsk", return_value=None):
                rep = ev.check(with_doctor=False)
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["backend"], "builtin")
        self.assertFalse(rep["needs_decision"])
        self.assertTrue(all("--install" not in a for a in rep["actions"]))

    def test_unconfigured_needs_decision(self):
        with _Cfg():
            with mock.patch.object(ev, "find_bsk", return_value=None):
                rep = ev.check(with_doctor=False)
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["backend"], "unconfigured")
        self.assertTrue(rep["needs_decision"])
        self.assertTrue(any("--set-backend" in a for a in rep["actions"]))

    def test_bsk_backend_without_bsk_asks_install(self):
        with _Cfg():
            _write_cfg({"browser_backend": "bsk"})
            with mock.patch.object(ev, "find_bsk", return_value=None):
                rep = ev.check(with_doctor=False)
        self.assertFalse(rep["ok"])
        self.assertFalse(rep["needs_decision"])
        self.assertTrue(any("--install" in a for a in rep["actions"]))

    def test_explicit_flag_builtin_overrides_missing_config(self):
        with _Cfg():
            with mock.patch.object(ev, "find_bsk", return_value=None):
                rep = ev.check(with_doctor=False, backend="builtin")
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["backend"], "builtin")

    def test_iab_transport_exempts_bsk_check(self):
        with _Cfg():
            with mock.patch.dict(os.environ, {"STARPILOT_TRANSPORT": "iab"}), \
                    mock.patch.object(ev, "find_bsk", return_value=None):
                rep = ev.check(with_doctor=False)
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["backend"], "iab")
        self.assertFalse(rep["needs_decision"])
        self.assertTrue(all("--install" not in a for a in rep["actions"]))

    def test_iab_transport_ignored_when_bsk_present(self):
        with _Cfg():
            with mock.patch.dict(os.environ, {"STARPILOT_TRANSPORT": "iab"}), \
                    mock.patch.object(ev, "find_bsk", return_value="/x/bsk"), \
                    mock.patch.object(ev, "bsk_version", return_value="0.4.0"):
                rep = ev.check(with_doctor=False)
        self.assertEqual(rep["backend"], "bsk")
        self.assertTrue(rep["ok"])

    def test_extension_action_suppressed_for_builtin_backend(self):
        with _Cfg():
            _write_cfg({"browser_backend": "builtin"})
            with mock.patch.object(ev, "find_bsk", return_value="/x/bsk"), \
                    mock.patch.object(ev, "bsk_version", return_value="0.4.0"), \
                    mock.patch.object(ev, "doctor",
                                      return_value={"rc": 0, "connected": False}):
                rep = ev.check(with_doctor=True)
        self.assertTrue(rep["ok"])  # builtin 后端下扩展没连不算环境问题
        self.assertEqual(rep["actions"], [])


class TestCli(unittest.TestCase):
    def test_set_backend_writes_and_exits_0(self):
        with _Cfg():
            buf = io.StringIO()
            with mock.patch.object(ev.sys, "stdout", buf):
                rc = ev.main(["--set-backend", "builtin"])
            self.assertEqual(rc, 0)
            self.assertEqual(ev.load_config()["browser_backend"], "builtin")
            self.assertIn("已记录", buf.getvalue())

    def test_json_reports_backend_and_needs_decision(self):
        with _Cfg():
            buf = io.StringIO()
            with mock.patch.object(ev, "find_bsk", return_value=None), \
                    mock.patch.object(ev.sys, "stdout", buf):
                rc = ev.main(["--json", "--no-doctor"])
            self.assertEqual(rc, 4)
            data = json.loads(buf.getvalue())
            self.assertEqual(data["backend"], "unconfigured")
            self.assertTrue(data["needs_decision"])

    def test_json_ok_when_builtin(self):
        with _Cfg():
            _write_cfg({"browser_backend": "builtin"})
            buf = io.StringIO()
            with mock.patch.object(ev, "find_bsk", return_value=None), \
                    mock.patch.object(ev.sys, "stdout", buf):
                rc = ev.main(["--json", "--no-doctor"])
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())
            self.assertTrue(data["ok"])


class TestPlatformsSpecCli(unittest.TestCase):
    def test_spec_json_roundtrip(self):
        buf = io.StringIO()
        with mock.patch.object(pl.sys, "stdout", buf):
            rc = pl._cli(["spec", "chatgpt", "--json"])
        self.assertEqual(rc, 0)
        d = json.loads(buf.getvalue())
        self.assertIn("ttft_s", d)
        self.assertIn("sel", d)
        self.assertEqual(d["key"], "chatgpt")

    def test_spec_unknown_platform_fails(self):
        with self.assertRaises(SystemExit):
            pl._cli(["spec", "nosuch"])

    def test_list_has_known_platforms(self):
        buf = io.StringIO()
        with mock.patch.object(pl.sys, "stdout", buf):
            rc = pl._cli(["list"])
        self.assertEqual(rc, 0)
        for k in ("chatgpt", "kimi", "deepseek", "gemini", "doubao", "claude"):
            self.assertIn(k, buf.getvalue())


class TestWaBuiltinGuard(unittest.TestCase):
    def test_blocks_browser_cmds_when_builtin(self):
        with _Cfg():
            _write_cfg({"browser_backend": "builtin"})
            for c in ("bind", "send", "wait", "read", "triage", "probe",
                      "clear", "doctor", "stop"):
                msg = wa.builtin_guard(c)
                self.assertIsInstance(msg, str, c)
                self.assertIn("builtin-mode.md", msg)

    def test_allows_readonly_cmds_when_builtin(self):
        with _Cfg():
            _write_cfg({"browser_backend": "builtin"})
            for c in ("platforms", "state", "unlock", "plock"):
                self.assertIsNone(wa.builtin_guard(c), c)

    def test_no_block_when_bsk_backend(self):
        with _Cfg():
            _write_cfg({"browser_backend": "bsk"})
            self.assertIsNone(wa.builtin_guard("send"))

    def test_no_block_when_unconfigured(self):
        with _Cfg():
            self.assertIsNone(wa.builtin_guard("send"))

    def test_main_returns_6_when_blocked(self):
        with _Cfg():
            _write_cfg({"browser_backend": "builtin"})
            buf = io.StringIO()
            with mock.patch.object(wa.sys, "argv",
                                   ["wa.py", "--state",
                                    os.path.join(tempfile.mkdtemp(), "s.json"), "bind", "https://x"]), \
                    mock.patch.object(wa.sys, "stderr", buf):
                rc = wa.main()
            self.assertEqual(rc, 6)
            self.assertIn("builtin", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
