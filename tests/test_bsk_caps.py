# -*- coding: utf-8 -*-
"""bsk 0.4.1 能力层 + doctor --explain 的决策表。

两件一起测，因为它们是同一个目的的两面：**把「新版本有什么」和「下一步该敲什么」
都变成代码里的明账**，而不是靠人记文档、靠 agent 猜。

能力探测一律问 CLI（`--help`），不信版本号：版本相同、构建不同也会有差异。
"""
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import bskcompat    # noqa: E402
import platforms    # noqa: E402
import wa           # noqa: E402


class _Runner(object):
    """可编程的 bsk 调用器：记录调用、按脚本返回。"""

    def __init__(self, replies=None):
        self.calls = []
        self.replies = list(replies or [])

    def __call__(self, args, timeout=120):
        self.calls.append(list(args))
        if self.replies:
            return self.replies.pop(0)
        return 0, "", ""


class TestCapabilityProbe(unittest.TestCase):
    def setUp(self):
        bskcompat.reset_caps()

    def tearDown(self):
        bskcompat.reset_caps()

    def test_supported_when_help_mentions_usage(self):
        r = _Runner([(0, "Usage: bsk step [OPTIONS]", "")])
        self.assertTrue(bskcompat.supports(r, "step"))

    def test_unsupported_on_nonzero_rc(self):
        r = _Runner([(1, "", "error: unrecognized subcommand 'step'")])
        self.assertFalse(bskcompat.supports(r, "step"))

    def test_rc0_without_help_shape_is_not_supported(self):
        """光看 rc 会误判：有的构建 rc=0 但打不出帮助。"""
        r = _Runner([(0, "", "")])
        self.assertFalse(bskcompat.supports(r, "dialog"))

    def test_probe_is_cached_and_resets(self):
        r = _Runner([(0, "Usage:", ""), (0, "Usage:", "")])
        self.assertTrue(bskcompat.supports(r, "observe"))
        self.assertTrue(bskcompat.supports(r, "observe"))     # 第二次不再调
        self.assertEqual(len(r.calls), 1)
        bskcompat.reset_caps()
        self.assertTrue(bskcompat.supports(r, "observe"))
        self.assertEqual(len(r.calls), 2)


class TestStepPrimitive(unittest.TestCase):
    def test_params_are_json_and_never_carries_session(self):
        r = _Runner([(0, '{"ok":true}', "")])
        bskcompat.step(r, "click", {"selector": "form button"}, "sid1",
                       until_visible="div.thread", policy="target")
        args = r.calls[0]
        self.assertEqual(args[0], "step")
        self.assertIn("--action", args)
        self.assertEqual(args[args.index("--action") + 1], "click")
        self.assertEqual(json.loads(args[args.index("--params") + 1]),
                         {"selector": "form button"})
        self.assertEqual(args[args.index("--policy") + 1], "target")
        self.assertEqual(args[args.index("--until-visible") + 1], "div.thread")
        # 会话只能来自 CLI flag，不能混进 params（协议明确写了这点）
        self.assertNotIn("session", json.loads(args[args.index("--params") + 1]))

    def test_unknown_action_refused_without_calling(self):
        r = _Runner()
        rc, out, err = bskcompat.step(r, "teleport", {}, "sid1")
        self.assertEqual(rc, 2)
        self.assertIn("不支持的动作", err)
        self.assertEqual(r.calls, [], "非法动作不该发出去")

    def test_expect_is_json_array(self):
        r = _Runner([(0, "{}", "")])
        bskcompat.step(r, "fill", {"selector": "form div.ProseMirror"}, "s",
                       expect=[{"target": "@e1", "field": "text", "equals": "保存"}])
        args = r.calls[0]
        got = json.loads(args[args.index("--expect") + 1])
        self.assertEqual(got[0]["equals"], "保存")


class TestDialogStatus(unittest.TestCase):
    def setUp(self):
        bskcompat.reset_caps()

    def tearDown(self):
        bskcompat.reset_caps()

    def test_none_when_capability_missing(self):
        bskcompat._CAPS["dialog"] = False
        self.assertIsNone(bskcompat.dialog_status(_Runner(), "sid"))

    def test_pending_beforeunload_is_surfaced(self):
        bskcompat._CAPS["dialog"] = True
        payload = {"dialogs": [{"type": "beforeunload", "message": "确定离开？"}]}
        r = _Runner([(0, json.dumps(payload), "")])
        got = bskcompat.dialog_status(r, "sid")
        self.assertTrue(got["ok"])
        self.assertEqual(got["pending"][0]["type"], "beforeunload")

    def test_unparseable_is_reported_not_silently_empty(self):
        """和 list_sessions 同一个纪律：查不到 ≠ 没有挂起对话框。"""
        bskcompat._CAPS["dialog"] = True
        r = _Runner([(0, "warning line", "")])
        got = bskcompat.dialog_status(r, "sid")
        self.assertFalse(got["ok"])
        self.assertIn("解析失败", got["detail"])


class TestExplainDecisionTable(unittest.TestCase):
    """doctor --explain 的表：每条都要给出**可复制的命令**，禁止项要写明。"""

    def setUp(self):
        self._orig = {n: getattr(wa, n) for n in ("session_state", "duplicate_hint")}
        wa.session_state = lambda sid: "alive"
        wa.duplicate_hint = lambda rep: "把重复的扩展卸掉"
        self._bsk = wa.BSK
        self._orig_bsk_exists = wa.os.path.exists

    def tearDown(self):
        for n, f in self._orig.items():
            setattr(wa, n, f)
        wa.BSK = self._bsk
        wa.os.path.exists = self._orig_bsk_exists

    def _call(self, st=None, rep=None, wr=None, sid="ab12", left=100, p=None):
        st = st or {"platform": "chatgpt", "url": "https://chatgpt.com/c/x"}
        rep = rep or {"healthy": True, "instances": [{"id": "b1", "skew": False}],
                      "duplicates": []}
        p = p or platforms.get("chatgpt")
        return wa._explain({}, st, rep, wr, sid, left, p)

    def test_healthy_env_still_gets_a_next_step(self):
        acts = self._call()
        self.assertTrue(acts)
        self.assertTrue(all(a["command"] for a in acts),
                        "体检没问题时也要给出唯一该敲的那条（triage），不能只说一切正常")

    def test_awaiting_reply_forbids_sending_next(self):
        acts = self._call(st={"platform": "chatgpt", "url": "u", "awaiting_reply": True})
        joined = json.dumps(acts, ensure_ascii=False)
        self.assertIn("wa.py wait", joined)
        self.assertIn("不要发下一条", joined)

    def test_maybe_sent_forbids_force_resend(self):
        acts = self._call(st={"platform": "chatgpt", "url": "u",
                              "maybe_sent": {"verdict": "INCONCLUSIVE_NOT_SUCCESS"}})
        joined = json.dumps(acts, ensure_ascii=False)
        self.assertIn("triage", joined)
        self.assertIn("不要带 --force 重发", joined)

    def test_unverified_write_path_says_calibrate_not_bypass(self):
        """这是 32 条 P0-ESC 的源头：闸门拦写时，必须指明「该标定」而不是让人去撞逃生门。"""
        p = platforms.get("claude")
        acts = self._call(p=p)
        joined = json.dumps(acts, ensure_ascii=False)
        self.assertIn("probe", joined)
        self.assertIn("STARPILOT_ALLOW_DIRECT_SEND", joined)
        self.assertIn("禁用", joined)

    def test_extension_version_skew_is_called_out(self):
        """0.4.1 起 protocol 1.8、**混版不支持**：扩展落后会表现成「时好时坏的页面故障」。"""
        acts = self._call(rep={"healthy": True, "duplicates": [],
                               "instances": [{"id": "b1", "skew": True}]})
        joined = json.dumps(acts, ensure_ascii=False)
        self.assertIn("混版", joined)
        self.assertIn("扩展", joined)

    def test_no_browser_connected_blocks_session_start_gambling(self):
        acts = self._call(rep={"healthy": False, "duplicates": [], "instances": []})
        joined = json.dumps(acts, ensure_ascii=False)
        self.assertIn("bsk doctor", joined)
        self.assertIn("别用 session start 赌", joined)


if __name__ == "__main__":
    unittest.main()
