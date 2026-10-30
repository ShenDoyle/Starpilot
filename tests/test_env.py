#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""环境自检单测：解释器版本判定、bsk 定位顺序、安装命令、自动安装与复检。

不联网、不真的装东西：subprocess 与 which 全部替身。
"""

import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import env as ev  # noqa: E402


def _touch(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with io.open(path, "w", encoding="utf-8") as f:
        f.write("x")
    return path


class _Proc(object):
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


class TestFindBsk(unittest.TestCase):
    def test_bsk_bin_wins(self):
        d = tempfile.mkdtemp()
        p = _touch(os.path.join(d, "mybsk"))
        self.assertEqual(ev.find_bsk(env={"BSK_BIN": p}, which=lambda n: "/usr/bin/bsk"), p)

    def test_falls_back_to_path(self):
        d = tempfile.mkdtemp()
        p = _touch(os.path.join(d, "bsk"))
        self.assertEqual(ev.find_bsk(env={}, which=lambda n: p), p)

    def test_none_when_nothing(self):
        with mock.patch.object(ev.os.path, "expanduser", side_effect=lambda s: "/nonexistent" + s):
            self.assertIsNone(ev.find_bsk(env={}, which=lambda n: None))

    def test_bsk_bin_pointing_nowhere_is_ignored(self):
        with mock.patch.object(ev.os.path, "expanduser", side_effect=lambda s: "/nonexistent" + s):
            self.assertIsNone(ev.find_bsk(env={"BSK_BIN": "/nope/bsk"}, which=lambda n: None))


class TestInstallCommand(unittest.TestCase):
    def test_windows_uses_powershell(self):
        cmd = ev.install_command("windows")
        self.assertEqual(cmd[0], "powershell")
        self.assertIn(ev.INSTALL_PS1, cmd[-1])

    def test_unix_uses_sh(self):
        cmd = ev.install_command("linux")
        self.assertEqual(cmd[:2], ["sh", "-c"])
        self.assertIn(ev.INSTALL_SH, cmd[-1])

    def test_unknown_platform_none(self):
        self.assertIsNone(ev.install_command("plan9"))


class TestVersions(unittest.TestCase):
    def test_bsk_version_parsed(self):
        r = lambda argv, **kw: _Proc(0, "bsk 0.3.3\n")  # noqa: E731
        self.assertEqual(ev.bsk_version("/x/bsk", runner=r), "0.3.3")

    def test_bsk_version_absent_when_missing(self):
        self.assertIsNone(ev.bsk_version(None))

    def test_doctor_connected(self):
        r = lambda argv, **kw: _Proc(0, "daemon: running\nextension: Connected\n")  # noqa: E731
        self.assertTrue(ev.doctor("/x/bsk", runner=r)["connected"])

    def test_doctor_not_connected(self):
        r = lambda argv, **kw: _Proc(0, "extension: not connected\n")  # noqa: E731
        self.assertFalse(ev.doctor("/x/bsk", runner=r)["connected"])

    def test_doctor_unknown_on_empty(self):
        r = lambda argv, **kw: _Proc(1, "")  # noqa: E731
        self.assertIsNone(ev.doctor("/x/bsk", runner=r)["connected"])


class TestCheck(unittest.TestCase):
    def test_missing_bsk_gives_action_and_not_ok(self):
        with mock.patch.object(ev, "find_bsk", return_value=None):
            rep = ev.check(with_doctor=False)
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["bsk"]["path"], None)
        self.assertTrue(any("--install" in a for a in rep["actions"]))

    def test_ready_when_bsk_present(self):
        with mock.patch.object(ev, "find_bsk", return_value="/x/bsk"), \
                mock.patch.object(ev, "bsk_version", return_value="0.3.3"):
            rep = ev.check(with_doctor=False)
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["actions"], [])

    def test_no_doctor_skips_probe(self):
        with mock.patch.object(ev, "find_bsk", return_value="/x/bsk"), \
                mock.patch.object(ev, "bsk_version", return_value="0.3.3"), \
                mock.patch.object(ev, "doctor") as d:
            ev.check(with_doctor=False)
        d.assert_not_called()

    def test_extension_not_connected_is_an_action(self):
        with mock.patch.object(ev, "find_bsk", return_value="/x/bsk"), \
                mock.patch.object(ev, "bsk_version", return_value="0.3.3"), \
                mock.patch.object(ev, "doctor", return_value={"rc": 0, "connected": False}):
            rep = ev.check(with_doctor=True)
        self.assertFalse(rep["ok"])
        self.assertTrue(any("扩展" in a for a in rep["actions"]))


class TestInstall(unittest.TestCase):
    def test_install_success_rechecks(self):
        good = {"ok": True, "bsk": {"ok": True, "path": "/x/bsk", "version": "0.3.3", "doctor": None},
                "python": {"ok": True}, "actions": []}
        r = lambda argv, **kw: _Proc(0, "installed\n")  # noqa: E731
        res = ev.install(runner=r, rep=good)
        self.assertTrue(res["ok"])
        self.assertEqual(res["path"], "/x/bsk")

    def test_install_failure_reported(self):
        bad = {"ok": False, "bsk": {"ok": False, "path": None, "version": None, "doctor": None},
               "python": {"ok": True}, "actions": ["x"]}
        r = lambda argv, **kw: _Proc(1, "", "curl: (7) Failed to connect")  # noqa: E731
        res = ev.install(runner=r, rep=bad)
        self.assertFalse(res["ok"])
        self.assertEqual(res["rc"], 1)
        self.assertTrue(any("Failed to connect" in t for t in res["tail"]))

    def test_install_unknown_platform(self):
        with mock.patch.object(ev, "install_command", return_value=None):
            res = ev.install()
        self.assertFalse(res["ok"])
        self.assertIsNone(res["rc"])


class TestCli(unittest.TestCase):
    def test_json_exit_4_when_missing(self):
        buf = io.StringIO()
        with mock.patch.object(ev, "find_bsk", return_value=None), \
                mock.patch.object(ev.sys, "stdout", buf):
            rc = ev.main(["--json", "--no-doctor"])
        self.assertEqual(rc, 4)
        data = json.loads(buf.getvalue())
        self.assertFalse(data["ok"])
        self.assertTrue(data["actions"])

    def test_exit_0_when_ready(self):
        buf = io.StringIO()
        with mock.patch.object(ev, "find_bsk", return_value="/x/bsk"), \
                mock.patch.object(ev, "bsk_version", return_value="0.3.3"), \
                mock.patch.object(ev.sys, "stdout", buf):
            rc = ev.main(["--no-doctor"])
        self.assertEqual(rc, 0)
        self.assertIn("环境就绪", buf.getvalue())

    def test_install_called_and_exit_5_on_failure(self):
        buf = io.StringIO()
        with mock.patch.object(ev, "find_bsk", return_value=None), \
                mock.patch.object(ev, "install", return_value={"ok": False, "rc": 1, "tail": []}), \
                mock.patch.object(ev.sys, "stdout", buf):
            rc = ev.main(["--install", "--no-doctor"])
        self.assertEqual(rc, 5)


if __name__ == "__main__":
    unittest.main()
