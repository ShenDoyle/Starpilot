# -*- coding: utf-8 -*-
"""卡住判定（三时钟 + 分诊阶梯）的口径测试。

这些用例全部离线：判定规则在 platforms.judge_stall 里，是纯函数。
理由和别的口径测试一样——规则埋在 JS 里就只能连浏览器才验得到，
而这条规则最容易退化成的样子是「风控被当成 DOM 改版」，处置完全相反。
"""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import platforms  # noqa: E402
import wa  # noqa: E402


def probe(**kw):
    """一份干净的探针事实，按需覆盖。"""
    d = {
        "url": "https://chat.deepseek.com/a/chat/s/aaaa1111-2222-3333-4444-555566667777",
        "generating": False, "regen": False, "editor": True, "editor_len": 0,
        "msg_count": 2, "reply_count": 1, "last_len": 0,
        "doc_len": 1000, "doc_nodes": 300,
        "modal": False, "modal_text": "", "overlay": False, "overlay_rect": "",
        "captcha": False, "risk_text": "", "login_hint": False,
    }
    d.update(kw)
    return d


BASE = {"msg_count": 1, "reply_count": 0, "doc_len": 1000, "doc_nodes": 300,
        "editor_len": 0, "conv_id": ""}


class TestRiskPriority(unittest.TestCase):
    """风控 > 漂移 > 生成 > 读取 > 发送：顺序错了处置就反了。"""

    def setUp(self):
        self.p = platforms.get("deepseek")

    def test_captcha_beats_overlay_and_banner(self):
        cur = probe(captcha=True, overlay=True, risk_text="请求过于频繁")
        out = platforms.judge_stall(self.p, cur, BASE, unchanged_s=0)
        self.assertEqual(out["verdict"], platforms.STALL_RISK)
        self.assertEqual(out["risk"], "captcha")

    def test_overlay_beats_banner(self):
        cur = probe(overlay=True, overlay_rect="1280x720", risk_text="稍后再试")
        self.assertEqual(platforms.risk_kind(cur), "overlay")

    def test_banner_from_ui_text_only(self):
        self.assertEqual(platforms.risk_kind(probe(risk_text="verify you are human")), "banner")

    def test_login_needs_hint(self):
        self.assertEqual(platforms.risk_kind(probe(editor=False)), "")
        self.assertEqual(platforms.risk_kind(probe(editor=False, login_hint=True)), "login")

    def test_risk_beats_everything_else(self):
        # 遮罩会让 editor=false —— 这正是「风控被误判成 DOM 改版」的来路。
        cur = probe(editor=False, overlay=True, doc_len=1000)
        out = platforms.judge_stall(self.p, cur, dict(BASE, editor_len=50), unchanged_s=99)
        self.assertEqual(out["verdict"], platforms.STALL_RISK)

    def test_hint_names_the_detail(self):
        out = platforms.judge_stall(self.p, probe(captcha=True, captcha_src="challenges.cloudflare.com"),
                                    BASE, unchanged_s=0)
        self.assertIn("challenges.cloudflare.com", out["hint"])


class TestDrift(unittest.TestCase):

    def setUp(self):
        self.p = platforms.get("deepseek")

    def test_drift_when_conv_changes(self):
        base = dict(BASE, conv_id="/a/chat/s/aaaa1111-2222-3333-4444-555566667777")
        cur = probe(url="https://chat.deepseek.com/a/chat/s/bbbb9999-2222-3333-4444-555566667777")
        out = platforms.judge_stall(self.p, cur, base, unchanged_s=0)
        self.assertEqual(out["verdict"], platforms.STALL_DRIFT)

    def test_no_drift_when_same_conv(self):
        base = dict(BASE, conv_id="/a/chat/s/aaaa1111-2222-3333-4444-555566667777")
        out = platforms.judge_stall(self.p, probe(generating=True, doc_len=1400), base, unchanged_s=3)
        self.assertEqual(out["verdict"], platforms.STALL_ALIVE)


class TestGenerating(unittest.TestCase):

    def setUp(self):
        self.p = platforms.get("deepseek")

    def test_growing_text_is_alive(self):
        out = platforms.judge_stall(self.p, probe(generating=True, doc_len=1100), BASE, unchanged_s=30)
        self.assertEqual(out["verdict"], platforms.STALL_ALIVE)

    def test_new_nodes_are_alive(self):
        out = platforms.judge_stall(self.p, probe(generating=True, doc_nodes=320), BASE, unchanged_s=30)
        self.assertEqual(out["verdict"], platforms.STALL_ALIVE)

    def test_frozen_but_generating_is_dead(self):
        # 停止按钮在、整棵子树一字不动 15 秒 = 假生成（流死了，UI 没复位）
        out = platforms.judge_stall(self.p, probe(generating=True), BASE, unchanged_s=16)
        self.assertEqual(out["verdict"], platforms.STALL_DEAD)

    def test_frozen_briefly_is_still_alive(self):
        out = platforms.judge_stall(self.p, probe(generating=True), BASE, unchanged_s=5)
        self.assertEqual(out["verdict"], platforms.STALL_ALIVE)


class TestOtherVerdicts(unittest.TestCase):

    def setUp(self):
        self.p = platforms.get("deepseek")

    def test_reply_ready_when_counts_moved(self):
        cur = probe(reply_count=1, last_len=800, doc_len=1800)
        out = platforms.judge_stall(self.p, cur, BASE, unchanged_s=2)
        self.assertEqual(out["verdict"], platforms.STALL_READY)

    def test_selector_stale_when_page_grew_without_reply(self):
        # 页面文本涨了 200 字、节点涨了 50，但回复选择器读不到 → 改版，不是没回复
        cur = probe(doc_len=1200, doc_nodes=350)
        out = platforms.judge_stall(self.p, cur, BASE, unchanged_s=20)
        self.assertEqual(out["verdict"], platforms.STALL_STALE)
        self.assertIn("选择器", out["hint"])

    def test_send_not_taken_when_text_still_in_box(self):
        cur = probe(editor_len=42, msg_count=1, doc_len=1000, doc_nodes=300)
        out = platforms.judge_stall(self.p, cur, BASE, unchanged_s=20)
        self.assertEqual(out["verdict"], platforms.STALL_NOT_SENT)

    def test_silent_when_nothing_moves(self):
        out = platforms.judge_stall(self.p, probe(), BASE, unchanged_s=45)
        self.assertEqual(out["verdict"], platforms.STALL_SILENT)

    def test_rc_does_not_collide_with_platform_lock(self):
        # 9 = 平台占用锁被占，分诊结论必须另起码段，否则上层分不清「换个平台等」
        # 和「去把验证码点了」。
        self.assertNotIn(9, wa.STALL_RC.values())

    def test_rc_map_covers_every_failure_verdict(self):
        for v in (platforms.STALL_RISK, platforms.STALL_LOGIN, platforms.STALL_DRIFT,
                  platforms.STALL_DEAD, platforms.STALL_STALE,
                  platforms.STALL_NOT_SENT, platforms.STALL_SILENT):
            self.assertIn(v, wa.STALL_RC)
        # 正常态不该有失败码
        for v in (platforms.STALL_ALIVE, platforms.STALL_READY):
            self.assertNotIn(v, wa.STALL_RC)

    def test_triage_wrapper_adds_rc_and_stage(self):
        st = {"conv_id": ""}
        cur = probe(overlay=True, overlay_rect="1280x720")
        out = wa.triage(self.p, st, cur, BASE, unchanged_s=1)
        self.assertEqual(out["stage"], platforms.STALL_RISK)
        self.assertEqual(out["rc"], 10)


class TestProbeFacts(unittest.TestCase):
    """探针必须真的把事实报出来，否则上面的口径全是空转。"""

    def setUp(self):
        self.js = platforms.js_probe(platforms.get("chatgpt"))

    def test_reports_new_facts(self):
        for key in ("overlay:", "overlay_rect:", "overlay_text:", "captcha:",
                    "captcha_src:", "risk_text:", "login_hint:", "doc_len:",
                    "doc_nodes:", "iframes:"):
            self.assertIn(key, self.js)

    def test_overlay_needs_area_and_layer(self):
        self.assertIn("0.8", self.js)          # 面积门槛（八成视口）
        self.assertIn("zIndex", self.js)       # 有层级才算遮罩
        self.assertIn("el.contains(editor)", self.js)  # 含编辑器的容器是布局不是遮罩

    def test_risk_text_never_scans_conversation(self):
        # 助手刚答过「请求过于频繁」时，扫全页会把答案当风控（真实误报）。
        start = self.js.index("const riskSrc")
        end = self.js.index("const RISK_RE")
        block = self.js[start:end]
        self.assertNotIn("full", block)
        self.assertNotIn("document.body.innerText", block)
        self.assertIn("modalText", block)

    def test_captcha_covers_main_vendors(self):
        for token in ("turnstile", "geetest", "hcaptcha", "recaptcha", "challenges"):
            self.assertIn(token, self.js)


class TestClocksAreConfigured(unittest.TestCase):

    def test_every_platform_has_ttft_and_stall(self):
        for key in platforms.PLATFORMS:
            p = platforms.get(key)
            self.assertGreaterEqual(p["ttft_s"], 20, key)
            self.assertGreaterEqual(p["stall_s"], 20, key)
            # 首字时钟必须比总超时短一个量级，否则等于没拆
            self.assertLess(p["ttft_s"], 900, key)

    def test_ttft_is_looser_than_observed(self):
        # 实测首字 2–5 秒（DeepSeek，2026-10-30）；阈值至少给 5 倍余量，
        # 否则「开联网搜索」的正常轮次会被误杀。
        self.assertGreaterEqual(platforms.get("deepseek")["ttft_s"], 25)


class TestProgressFile(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, ".wa-state.json")

    def tearDown(self):
        for name in os.listdir(self.dir):
            os.remove(os.path.join(self.dir, name))
        os.rmdir(self.dir)

    def test_write_and_read(self):
        wa.write_progress(self.path, "generating", probe(generating=True), progressed=True)
        rec = wa.read_progress(self.path)
        self.assertEqual(rec["stage"], "generating")
        self.assertEqual(rec["seen"]["generating"], True)
        self.assertAlmostEqual(rec["progress_age_s"], 0, delta=2)

    def test_progress_at_only_moves_when_progressed(self):
        wa.write_progress(self.path, "waiting", probe(), progressed=True)
        first = wa.read_progress(self.path)["progress_at"]
        wa.write_progress(self.path, "waiting", probe(), progressed=False)
        rec = wa.read_progress(self.path)
        self.assertEqual(rec["progress_at"], first)      # 没进展就不刷新
        self.assertGreaterEqual(rec["probe_at"], first)  # 但探测时间照刷

    def test_corrupt_file_is_ignored(self):
        with open(wa.progress_path(self.path), "w", encoding="utf-8") as f:
            f.write("{not json")
        wa.write_progress(self.path, "waiting", None)   # 不许抛
        self.assertEqual(wa.read_progress(self.path)["stage"], "waiting")

    def test_missing_file_reads_as_empty(self):
        self.assertEqual(wa.read_progress(self.path), {})

    def test_progress_is_not_the_state_file(self):
        wa.write_progress(self.path, "waiting", probe())
        self.assertFalse(os.path.exists(self.path))     # state 由状态机独占，观察文件另开
        self.assertTrue(os.path.exists(wa.progress_path(self.path)))


class TestDoctorExposesProgress(unittest.TestCase):

    def test_doctor_includes_progress(self):
        src = open(os.path.join(os.path.dirname(HERE), "scripts", "wa.py"),
                   encoding="utf-8").read()
        i = src.index("def cmd_doctor")
        j = src.index("def cmd_triage")
        self.assertIn("read_progress(path)", src[i:j])


if __name__ == "__main__":
    unittest.main()


class TestStallReport(unittest.TestCase):
    """orchestrator status 里的「卡住」：只读文件，一秒内必须能答。"""

    def setUp(self):
        import orchestrator
        self.orch = orchestrator
        self.root = tempfile.mkdtemp()
        self.path = os.path.join(self.root, ".wa-state.json")

    def tearDown(self):
        for name in os.listdir(self.root):
            os.remove(os.path.join(self.root, name))
        os.rmdir(self.root)

    def test_not_stalled_when_no_state(self):
        out = self.orch.stall_report(self.root)
        self.assertFalse(out["stalled"])

    def test_stalled_when_progress_is_old(self):
        wa.write_progress(self.path, "waiting", probe())
        with open(wa.progress_path(self.path), encoding="utf-8") as f:
            rec = json.load(f)
        rec["progress_at"] = rec["progress_at"] - 600      # 十分钟没进展
        with open(wa.progress_path(self.path), "w", encoding="utf-8") as f:
            json.dump(rec, f)
        out = self.orch.stall_report(self.root)
        self.assertTrue(out["stalled"])
        self.assertIn("triage", out["hint"])

    def test_not_stalled_while_moving(self):
        wa.write_progress(self.path, "generating", probe(generating=True), progressed=True)
        self.assertFalse(self.orch.stall_report(self.root)["stalled"])

    def test_stalled_when_awaiting_reply_without_progress_file(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"url": "https://x/y", "awaiting_reply": True}, f)
        out = self.orch.stall_report(self.root)
        self.assertTrue(out["stalled"])
        self.assertIn("没跑起来", out["hint"])

    def test_stalled_after_timeout_but_still_awaiting(self):
        wa.write_progress(self.path, "timeout", None)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"url": "https://x/y", "awaiting_reply": True}, f)
        out = self.orch.stall_report(self.root)
        self.assertTrue(out["stalled"])
        self.assertIn("超时", out["hint"])
