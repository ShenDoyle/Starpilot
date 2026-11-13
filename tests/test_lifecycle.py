#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R1-R4 依赖生命周期单测：ego 锚点检测 / 升级检查第三路 / 反馈信息收集。

全部离线 mock（不碰真 ego CLI、不联网），传输钉 bsk 基线（见 tests/__init__.py）。
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
import env as envmod      # noqa: E402
import update_check       # noqa: E402
import feedback           # noqa: E402


def _runner(out="", err=""):
    from types import SimpleNamespace
    def r(argv, timeout=20, **kw):
        return SimpleNamespace(returncode=0, stdout=out, stderr=err)
    return r


class TestEgoVersion(unittest.TestCase):
    def test_parse_version_line(self):
        v, g, first = envmod.ego_version("/x/ego", runner=_runner(
            "ego-browser 0.6.2.10\n  chromium 156\n"))
        self.assertEqual(v, (0, 6, 2, 10))
        self.assertEqual(first, "ego-browser 0.6.2.10")

    def test_unparseable_is_honest(self):
        v, g, first = envmod.ego_version("/x/ego", runner=_runner("什么都没有"))
        self.assertEqual(v, ())
        self.assertEqual(first, "什么都没有")

    def test_no_path(self):
        self.assertEqual(envmod.ego_version(None)[0], ())


class TestEgoInfo(unittest.TestCase):
    def setUp(self):
        self.env = {"EGO_BROWSER_BIN": "/x/ego-browser"}

    def test_anchor_match(self):
        info = envmod.ego_info(env=self.env, runner=_runner("ego-browser 0.6.2.10\n"))
        self.assertEqual(info["status"], "ok")
        self.assertTrue(info["ok"])

    def test_older_than_min(self):
        info = envmod.ego_info(env=self.env, runner=_runner("ego-browser 0.5.0\n"))
        self.assertEqual(info["status"], "older")
        self.assertFalse(info["ok"])
        self.assertIn("upgrade", info["hint"])

    def test_newer_than_anchor(self):
        info = envmod.ego_info(env=self.env, runner=_runner("ego-browser 0.7.0\n"))
        self.assertEqual(info["status"], "newer")
        self.assertTrue(info["ok"])   # 只记录不拦
        self.assertIn("§F", info["hint"])

    def test_missing_binary(self):
        with mock.patch.object(envmod, "find_ego_browser", return_value=""):
            info = envmod.ego_info(env={})
        self.assertEqual(info["status"], "unknown")
        self.assertFalse(info["ok"])

    def test_unparseable(self):
        info = envmod.ego_info(env=self.env, runner=_runner("garbage"))
        self.assertEqual(info["status"], "unknown")
        self.assertFalse(info["ok"])


class TestCheckEgoliteReport(unittest.TestCase):
    def _cfg(self):
        d = tempfile.mkdtemp()
        return d

    def test_check_reports_ego_status_newer_note_not_block(self):
        d = self._cfg()
        with mock.patch.dict(os.environ, {
                "STARPILOT_TRANSPORT": "egolite",
                "STARPILOT_CONFIG": os.path.join(d, "nope.json")}), \
                mock.patch.object(envmod, "find_bsk", return_value=None), \
                mock.patch.object(envmod, "ego_info", return_value={
                    "ok": True, "path": "/x/ego", "version": "0.7.0",
                    "anchor": "0.6.2.10", "status": "newer", "hint": "h"}):
            rep = envmod.check(with_doctor=False)
        self.assertEqual(rep["backend"], "egolite")
        self.assertEqual(rep["ego_browser"]["status"], "newer")
        # newer 只记录不拦：进 notes，不进 actions，ok 不因此变 False
        self.assertTrue(any("锚点" in n for n in rep.get("notes") or []))
        self.assertFalse(any("锚点" in a for a in rep["actions"]))
        self.assertTrue(rep["ok"])

    def test_check_missing_ego_gives_install_action(self):
        d = self._cfg()
        with mock.patch.dict(os.environ, {
                "STARPILOT_TRANSPORT": "egolite",
                "STARPILOT_CONFIG": os.path.join(d, "nope.json")}), \
                mock.patch.object(envmod, "find_bsk", return_value=None), \
                mock.patch.object(envmod, "find_ego_browser", return_value=""):
            rep = envmod.check(with_doctor=False)
        self.assertFalse(rep["ok"])
        self.assertTrue(any("lite.ego.app" in a for a in rep["actions"]))

    def test_install_guide_for_egolite(self):
        with mock.patch.dict(os.environ, {"STARPILOT_TRANSPORT": "egolite"}):
            r = envmod.install()
        self.assertEqual(r.get("mode"), "ego-guide")
        self.assertTrue(any("onboarding" in s and "导入" in s for s in r["steps"]))
        self.assertTrue(any("lite.ego.app" in s for s in r["steps"]))


class TestUpdateCheckEgo(unittest.TestCase):
    def test_human_lines_for_ego_statuses(self):
        base = {"starpilot": {"local": "0.1.7.1", "remote": "0.1.7.1", "newer": False,
                              "source": "tags", "err": ""},
                "bsk": {"local": "0.4.1", "remote": "0.4.1", "newer": False,
                        "source": "tags", "err": ""}}
        newer = dict(base, ego={"status": "newer", "version": "0.7.0", "anchor": "0.6.2.10"})
        text = update_check.human(newer)
        self.assertIn("⚠", text)
        self.assertIn("无人值守", text)
        ok = dict(base, ego={"status": "ok", "version": "0.6.2.10", "anchor": "0.6.2.10"})
        self.assertIn("无感使用", update_check.human(ok))
        older = dict(base, ego={"status": "older", "version": "0.5.0", "anchor": "0.6.2.10"})
        self.assertIn("ego-browser upgrade", update_check.human(older))

    def test_check_output_contains_ego(self):
        with mock.patch.object(update_check, "load_cache", return_value=None), \
                mock.patch.object(update_check, "save_cache"), \
                mock.patch.object(update_check, "fetch_remote",
                                  return_value={"version": (), "source": "x", "err": "e"}), \
                mock.patch.object(envmod, "ego_info", return_value={
                    "ok": True, "status": "ok", "version": "0.6.2.10",
                    "anchor": "0.6.2.10", "hint": ""}):
            rep = update_check.check("/tmp", force=True, timeout=1)
        self.assertEqual(rep["ego"]["status"], "ok")


class TestFeedback(unittest.TestCase):
    def test_build_has_versions_and_privacy_redlines(self):
        d = tempfile.mkdtemp()
        text = feedback.build("bug", root=d)
        self.assertIn("github.com/ShenDoyle/Starpilot/issues", text)
        self.assertIn("starpilot", text)
        self.assertNotIn(".wa-state.json", text.split("### 环境")[0])

    def test_state_summary_strips_full_url(self):
        d = tempfile.mkdtemp()
        with io.open(os.path.join(d, ".wa-state.json"), "w", encoding="utf-8") as f:
            json.dump({"url": "https://chat.deepseek.com/a/chat/s/abc?x=1",
                       "platform": "deepseek", "conv_id": "/a/chat/s/abc"}, f)
        s = feedback.state_summary(d)
        self.assertEqual(s["url_host"], "chat.deepseek.com")
        self.assertNotIn("https://chat.deepseek.com", json.dumps(s))
        self.assertNotIn("?x=1", json.dumps(s))

    def test_platform_drift_kind(self):
        d = tempfile.mkdtemp()
        text = feedback.build("platform-drift", root=d)
        self.assertIn("平台改版", text)


if __name__ == "__main__":
    unittest.main()
