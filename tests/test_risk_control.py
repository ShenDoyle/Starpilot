# -*- coding: utf-8 -*-
"""风控取证与冷却（2026-11-04）。

用户反馈的现象是「ChatGPT 风控导致回复加载失败，连之前的回复也不显示」，
并归因于网络环境 + 操作拟人化。本文件锁住的是我对它的拆解：

  · 「风控 / 网络 / 页面没动」在 DOM 上长得一样，处置却相反 ——
    必须用 HTTP 状态码分，而不是用「看起来像」分；
  · 撞限速时**重试是最像机器人的行为**，所以要有冷却门，而不是更聪明的重试；
  · 真正有效的「拟人化」是降低轮询负载（一次 evaluate 而不是滚动+探测+sync），
    不是模拟鼠标轨迹 —— 后者对第一方站点无收益，还会往链路里塞进不可诊断的行为。
"""
import io
import json
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import platforms   # noqa: E402
import wa          # noqa: E402


class TestHttpAttribution(unittest.TestCase):
    def test_429_is_rate_limit_not_generic_risk(self):
        got = platforms.classify_http([200, 429, 200])
        self.assertEqual(got["kind"], platforms.HTTP_RATE_LIMITED)

    def test_403_outranks_429_because_it_needs_a_human(self):
        got = platforms.classify_http([429, 403])
        self.assertEqual(got["kind"], platforms.HTTP_BLOCKED)

    def test_5xx_and_zero_are_network_not_risk_control(self):
        for codes in ([500], [0], [200, 503]):
            self.assertEqual(platforms.classify_http(codes)["kind"],
                             platforms.HTTP_SERVER, codes)

    def test_clean_means_do_not_call_it_risk(self):
        self.assertEqual(platforms.classify_http([200, 200, 304])["kind"],
                         platforms.HTTP_CLEAN)

    def test_no_evidence_is_unknown_never_clean(self):
        """把「查不到」当「没问题」是本项目反复犯的那类错。"""
        for empty in ([], None, ["x", None]):
            self.assertEqual(platforms.classify_http(empty)["kind"],
                             platforms.HTTP_UNKNOWN)

    def test_counts_are_kept_for_the_report(self):
        got = platforms.classify_http([429, 429, 200])
        self.assertEqual(got["counts"], {"429": 2, "200": 1})


class TestNetEvidence(unittest.TestCase):
    def setUp(self):
        self._orig = wa.bsk
        import bskcompat
        self._compat = bskcompat
        self._caps = dict(bskcompat._CAPS)
        bskcompat._CAPS["debug"] = True

    def tearDown(self):
        wa.bsk = self._orig
        self._compat._CAPS.clear()
        self._compat._CAPS.update(self._caps)

    def test_parses_status_codes_from_request_list(self):
        wa.bsk = lambda args, timeout=120: (
            0, json.dumps({"requests": [{"status": 200}, {"status": 429}]}), "")
        got = wa.net_evidence("s1")
        self.assertTrue(got["ok"])
        self.assertEqual(got["kind"], platforms.HTTP_RATE_LIMITED)
        self.assertEqual(got["n"], 2)

    def test_query_flag_shape_is_session_scoped(self):
        seen = {}

        def fake(args, timeout=120):
            seen["args"] = list(args)
            return 0, '{"requests":[]}', ""
        wa.bsk = fake
        wa.net_evidence("abcd", limit=9)
        a = seen["args"]
        self.assertEqual(a[:2], ["debug", "requests"])
        self.assertEqual(a[a.index("--session") + 1], "abcd")
        self.assertEqual(a[a.index("--limit") + 1], "9")

    def test_failure_returns_unknown_not_clean(self):
        wa.bsk = lambda args, timeout=120: (1, "", "daemon gone")
        got = wa.net_evidence("s1")
        self.assertFalse(got["ok"])
        self.assertEqual(got["kind"], platforms.HTTP_UNKNOWN)

    def test_unparseable_output_is_unknown(self):
        wa.bsk = lambda args, timeout=120: (0, "banner, no json", "")
        self.assertEqual(wa.net_evidence("s1")["kind"], platforms.HTTP_UNKNOWN)

    def test_missing_debug_capability_is_reported(self):
        self._compat._CAPS["debug"] = False
        got = wa.net_evidence("s1")
        self.assertFalse(got["ok"])
        self.assertIn("debug", got["detail"])


class TestCooldownGate(unittest.TestCase):
    def test_exponential_backoff_capped(self):
        st = {}
        secs = [wa.set_cooldown(st, platforms.HTTP_RATE_LIMITED, "429") for _ in range(6)]
        self.assertEqual(secs[:3], [60, 120, 240])
        self.assertTrue(all(s <= wa.RISK_COOLDOWN_MAX_S for s in secs))
        self.assertGreater(wa.cooldown_left(st), 0)

    def test_clean_round_clears_the_streak(self):
        st = {}
        wa.set_cooldown(st, platforms.HTTP_RATE_LIMITED)
        wa.clear_cooldown(st)
        self.assertNotIn("risk_cooldown_until", st)
        # 清零后再撞，从第一档重新开始（不是接着上次的 8 分钟）
        self.assertEqual(wa.set_cooldown(st, platforms.HTTP_RATE_LIMITED), 60)

    def test_network_errors_do_not_trigger_cooldown(self):
        """5xx 是网络问题。拦发送等于把网络故障伪装成平台纪律，会让人以为工具在闹脾气。"""
        st = {}
        secs = wa.note_risk_evidence(st, {"kind": platforms.HTTP_SERVER,
                                         "detail": "502 x2"})
        self.assertEqual(secs, 0)
        self.assertNotIn("risk_cooldown_until", st)

    def test_blocked_and_limited_do_trigger(self):
        for kind in (platforms.HTTP_BLOCKED, platforms.HTTP_RATE_LIMITED):
            st = {}
            self.assertGreater(wa.note_risk_evidence(st, {"kind": kind}), 0)

    def test_unknown_never_cools_down(self):
        st = {}
        self.assertEqual(wa.note_risk_evidence(st, {"kind": platforms.HTTP_UNKNOWN}), 0)


class TestProbeOnceIsLighter(unittest.TestCase):
    """轻量探测必须比 read_state 少页面往返，且判据口径不变。"""

    def setUp(self):
        self._orig = {n: getattr(wa, n) for n in ("evaluate",)}
        self.calls = []

    def tearDown(self):
        for n, f in self._orig.items():
            setattr(wa, n, f)

    def test_single_evaluate_and_same_normalisation(self):
        p = platforms.get("chatgpt")
        wa._GEN_SAMPLE.clear()

        def fake(js, sid, timeout=60):
            self.calls.append(js)
            return True, {"url": "https://chatgpt.com/c/x", "msg_count": 2,
                          "reply_count": 1, "editor_len": 0, "editor_blank": True,
                          "asst_keys": ["u:a"], "msg_keys": ["u:a"],
                          "asst_fps": ["f"], "user_fps": ["u"], "last_len": 10,
                          "generating": False}
        wa.evaluate = fake
        ok, data = wa.probe_once(p, "s1")
        self.assertTrue(ok)
        self.assertEqual(len(self.calls), 1, "只该一次 evaluate，不滚动不重试")
        self.assertEqual(data["editor_len"], 0)
        self.assertIn("generating_basis", data, "复合生成信号必须同样生效")

    def test_failure_is_passed_through_not_fabricated(self):
        wa.evaluate = lambda js, sid, timeout=60: (False, "timeout")
        ok, data = wa.probe_once(platforms.get("chatgpt"), "s1")
        self.assertFalse(ok)
        self.assertEqual(data, "timeout")


class TestPlatformLockStillOnSendPaths(unittest.TestCase):
    """结构体检：`@with_platform_lock` 必须还贴在 cmd_send/cmd_bind/cmd_clear 上。

    2026-11-04 真实事故：新代码块整段插在装饰器和 `def cmd_send` 之间，
    装饰器于是套到了新块的第一个函数上（签名不匹配，调用当场 TypeError），
    而 `cmd_send` **悄悄失去平台锁**——两个任务可以同时往同一平台发送。
    功能单测全绿也照不出来，因为丢的是并发保护，不是返回值。
    """

    def test_send_bind_clear_are_locked(self):
        for name in ("cmd_send", "cmd_bind", "cmd_clear"):
            fn = getattr(wa, name)
            self.assertTrue(hasattr(fn, "__wrapped__"),
                            "%s 不再被 @with_platform_lock 包装：平台锁丢了" % name)

    def test_helpers_are_not_wrapped(self):
        """辅助函数不能被平台锁包装：wrapper 只收一个参数，多参调用会炸。"""
        for name in ("net_evidence", "set_cooldown", "cooldown_left",
                     "note_risk_evidence", "probe_once"):
            fn = getattr(wa, name)
            self.assertFalse(hasattr(fn, "__wrapped__"),
                             "%s 被平台锁装饰器包住了（插入位置错误）" % name)


if __name__ == "__main__":
    unittest.main()
