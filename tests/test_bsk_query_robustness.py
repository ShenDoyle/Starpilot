# -*- coding: utf-8 -*-
"""bsk 查询与页面信号的健壮性（2026-11-03）。

这一层管的是一类很脏的故障：**工具说不上话**。
「查不到」被当成「没有了」之后，程序会去做破坏性的动作（重开窗口、重发消息），
用户端看到的就是「网页各种故障、窗口越开越多」。核心三条纪律：

1. 解析失败 ≠ 空集合（`BskQueryError` / 三态 `session_state`）。
2. 「还在生成」不能只依赖停止按钮 —— 平台改版第一个删的就是它。
3. 选择器多匹配要**说清是歧义**，不能伪装成页面坏了让人反复重试。
"""
import io
import json
import os
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import platforms  # noqa: E402
import wa         # noqa: E402


class TestParseBskJson(unittest.TestCase):
    def test_clean_json(self):
        ok, d = wa.parse_bsk_json('{"sessions": [{"id": "ab12"}]}')
        self.assertTrue(ok)
        self.assertEqual(d["sessions"][0]["id"], "ab12")

    def test_banner_before_json_is_not_read_as_empty(self):
        """bsk 偶尔在 JSON 前带一行提示（daemon 自拉起 / 版本 skew 警告）。"""
        out = "daemon auto-started\n" + json.dumps({"sessions": [{"id": "zz99"}]})
        ok, d = wa.parse_bsk_json(out)
        self.assertTrue(ok, "带 banner 的输出必须还能解析出来")
        self.assertEqual(d["sessions"][0]["id"], "zz99")

    def test_pretty_printed_multiline(self):
        out = json.dumps({"browsers": [{"instance_id": "7cb0cac4"}]}, indent=2)
        ok, d = wa.parse_bsk_json(out)
        self.assertTrue(ok)
        self.assertEqual(d["browsers"][0]["instance_id"], "7cb0cac4")

    def test_nested_object_inside_array_keeps_outer_identity(self):
        """`{"sessions":[{"id":"a"}]}` 这种「对象里套数组再套对象」：
        括号边界必须用**栈**判。第一版用单个 close 变量，遇到 } 之后就不知道
        下一个该收 ] 了，只能解析到内层、返回一个 list —— 上层拿不到 sessions
        键，又回到「当成空」的老坑。这条就是把那个 bug 钉住的。"""
        out = "warning line\n" + json.dumps(
            {"sessions": [{"id": "ab12"}, {"id": "cd34"}], "count": 2})
        ok, d = wa.parse_bsk_json(out)
        self.assertTrue(ok)
        self.assertIsInstance(d, dict, "必须解析到**最外层对象**，不是内层数组")
        self.assertEqual([s["id"] for s in d["sessions"]], ["ab12", "cd34"])

    def test_braces_inside_strings_survive(self):
        """页面正文里带 { } 时，朴素计数会把边界算错。"""
        payload = {"value": "看这段：{不是} 边界 {而是字符串}"}
        ok, d = wa.parse_bsk_json("noise\n" + json.dumps(payload))
        self.assertTrue(ok)
        self.assertEqual(d["value"], payload["value"])

    def test_no_json_is_a_miss_not_a_crash(self):
        ok, d = wa.parse_bsk_json("Connection closed by daemon")
        self.assertFalse(ok)
        self.assertIsNone(d)

    def test_empty_output_is_a_miss(self):
        self.assertEqual(wa.parse_bsk_json("   "), (False, None))


class _BskStub(unittest.TestCase):
    def setUp(self):
        self._orig = {}
        for name in ("bsk", "evaluate", "read_state", "list_sessions",
                     "require_window_verified"):
            self._orig[name] = getattr(wa, name)
        self._env = os.environ.copy()

    def tearDown(self):
        for n, f in self._orig.items():
            setattr(wa, n, f)
        os.environ.clear()
        os.environ.update(self._env)

    def _stub_bsk(self, rc=0, out="", err=""):
        wa.bsk = lambda args, timeout=120, check=False: (rc, out, err)


class TestStrictQueries(_BskStub):
    def test_list_sessions_raises_when_unparseable(self):
        self._stub_bsk(0, "no json here")
        self.assertEqual(wa.list_sessions(), [])           # 旧行为仍可拿空表
        with self.assertRaises(wa.BskQueryError):
            wa.list_sessions(strict=True)                  # 要决策的调用方拿异常

    def test_list_sessions_raises_on_nonzero_rc(self):
        self._stub_bsk(rc=1, err="daemon not running")
        with self.assertRaises(wa.BskQueryError):
            wa.list_sessions(strict=True)
        self.assertEqual(wa.list_sessions(), [])

    def test_list_browsers_distinguishes_empty_from_unreadable(self):
        self._stub_bsk(0, json.dumps({"browsers": []}))
        self.assertEqual(wa.list_browsers(strict=True), [])   # 真的一个都没连
        self._stub_bsk(0, "warning: version skew\n")
        self.assertEqual(wa.list_browsers(), [])              # 查不清
        with self.assertRaises(wa.BskQueryError):
            wa.list_browsers(strict=True)


class TestSessionStateThreeWay(_BskStub):
    def _with_sessions(self, rc, out):
        self._stub_bsk(rc, out)
        return wa.session_state("ab12")

    def test_alive(self):
        got = self._with_sessions(0, json.dumps({"sessions": [{"session_id": "ab12"}]}))
        self.assertEqual(got, "alive")

    def test_dead(self):
        got = self._with_sessions(0, json.dumps({"sessions": [{"session_id": "zz99"}]}))
        self.assertEqual(got, "dead")

    def test_unknown_when_query_fails(self):
        got = self._with_sessions(1, "")
        self.assertEqual(got, "unknown")

    def test_session_alive_does_not_claim_dead_on_unknown(self):
        """看不清时 `session_alive` 必须**不**说「没了」：说没了就会去重开窗口。"""
        self._stub_bsk(1, "")
        self.assertTrue(wa.session_alive("ab12"))
        self._stub_bsk(0, json.dumps({"sessions": []}))
        self.assertFalse(wa.session_alive("ab12"))   # 查到了、确实不在，才算死

    def test_empty_sid_is_unknown_not_dead(self):
        self.assertEqual(wa.session_state(""), "unknown")


def _snap(**kw):
    base = {"url": "https://chatgpt.com/c/abcdef01-2345", "msg_count": 2,
            "reply_count": 1, "user_msg_count": 2, "editor_len": 0,
            "editor_blank": True, "can_send": True, "generating": False,
            "modal": False, "regen": False, "asst_fps": ["f1"], "user_fps": ["u1"],
            "msg_fps": [], "asst_keys": ["u:a"], "msg_keys": [],
            "last_asst_fp": "f1", "last_user_fp": "u1", "last_len": 100,
            "last_tail": "", "doc_len": 5000, "doc_nodes": 900, "error": ""}
    base.update(kw)
    return base


class TestGeneratingComposite(_BskStub):
    def setUp(self):
        super().setUp()
        wa._GEN_SAMPLE.clear()
        self.p = platforms.get("chatgpt")

    def test_growth_alone_counts_as_generating(self):
        """停止按钮查不到（改版常态），但末条回复在变长 ⇒ 仍在生成。"""
        wa.read_state = lambda p, sid: (True, _snap(last_len=100))
        ok, a = wa.read_state(self.p, "s1")
        wa._GEN_SAMPLE["s1"] = (time.time(), 100)
        d = wa._generating_composite(self.p, "s1", _snap(last_len=1400))
        self.assertTrue(d["generating"], "正文涨了 1300 字必须算仍在生成")
        self.assertEqual(d["generating_basis"], "growth")

    def test_stop_button_is_hard_signal(self):
        d = wa._generating_composite(self.p, "s2", _snap(generating=True, last_len=140))
        self.assertTrue(d["generating"])
        self.assertEqual(d["generating_basis"], "stop")

    def test_static_page_is_not_generating(self):
        wa._GEN_SAMPLE["s3"] = (time.time(), 500)
        d = wa._generating_composite(self.p, "s3", _snap(last_len=505))
        self.assertFalse(d["generating"])
        self.assertEqual(d["generating_basis"], "none")

    def test_stale_sample_is_ignored(self):
        """隔了 5 分钟的两条样本不能算「在变长」，否则会把已完成的回复判成在生成。"""
        wa._GEN_SAMPLE["s4"] = (time.time() - 300, 10)
        d = wa._generating_composite(self.p, "s4", _snap(last_len=9000))
        self.assertFalse(d["generating"])

    def test_first_sample_never_claims_generating(self):
        wa._GEN_SAMPLE.pop("s5", None)
        d = wa._generating_composite(self.p, "s5", _snap(last_len=800))
        self.assertFalse(d["generating"], "没有对照样本时不许凭空判「在生成」")


class TestSelectorAmbiguity(_BskStub):
    def test_count_matches_reports_number(self):
        wa.evaluate = lambda js, sid, timeout=60: (True, {"n": 8})
        self.assertEqual(wa.count_matches("div.ProseMirror", "s"), 8)

    def test_count_matches_failure_is_minus_one_not_zero(self):
        wa.evaluate = lambda js, sid, timeout=60: (False, "timeout")
        self.assertEqual(wa.count_matches("bad(((", "s"), -1)

    def test_fill_native_skips_ambiguous_candidates(self):
        """ChatGPT 实测：裸 div.ProseMirror 命中 8 个 → bsk 拒绝 → 残留清不掉。
        提前数出来就跳过它，并把歧义如实报出来。"""
        p = platforms.get("chatgpt")
        seen = []

        def fake_count(js_seen):
            return 8 if "div.ProseMirror" in js_seen else 1

        wa.evaluate = lambda js, sid, timeout=60: (True, {"n": 8})
        res = wa.fill_native(p, "要写进去的一段话", "s")
        self.assertFalse(res["ok"])
        self.assertEqual(res.get("code"), "SELECTOR_AMBIGUOUS")
        self.assertIn("div.ProseMirror", json.dumps(res.get("facts"), ensure_ascii=False))

    def test_ambiguous_fill_maps_to_its_own_exit_code(self):
        """cmd_send 的失败码要把「选择器歧义」和「平台不接受输入」分开。"""
        p = platforms.get("chatgpt")
        self.assertIn("form div.ProseMirror", p["sel"]["editor"],
                      "ChatGPT 的编辑器首选必须是 form 收窄的那条（夹具已验）")


if __name__ == "__main__":
    unittest.main()
