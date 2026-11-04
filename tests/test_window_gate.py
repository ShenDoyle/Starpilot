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
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
import wa  # noqa: E402
import platforms  # noqa: E402
import tabclaims  # noqa: E402
import winlease  # noqa: E402


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


    def test_dead_session_without_conv_id_is_told_to_open_new(self):
        """没 conv_id = 这条对话从未成立，不能建议「拿首页地址去续期」。

        真机输出里犯过一次：state 的 url 是首页、conv_id 空（bind --new 后还没发过），
        doctor 却说「同一个地址接上，别加 --new」——照着做等于把一个不存在的对话
        当已有对话绑，后面每一轮都建在错前提上。
        """
        import unittest.mock as mock
        orig = wa.session_state
        wa.session_state = lambda sid: "dead"
        self.addCleanup(setattr, wa, "session_state", orig)
        with mock.patch.object(wa, "pending_gate_path_for_working_set",
                               return_value=""):
            acts = wa._explain({}, {"url": "https://chatgpt.com/",
                                    "platform": "chatgpt", "conv_id": ""},
                               {"healthy": True, "duplicates": [],
                                "instances": []}, {"ok": True}, "s1", 100,
                               platforms.get("chatgpt"))
        joined = json.dumps(acts, ensure_ascii=False)
        self.assertIn("bind --new", joined)
        self.assertIn("还没成立", joined)

    def test_dead_session_with_conv_id_still_says_do_not_open_new(self):
        import unittest.mock as mock
        orig = wa.session_state
        wa.session_state = lambda sid: "dead"
        self.addCleanup(setattr, wa, "session_state", orig)
        conv = "https://chatgpt.com/c/6ae80db0-2820-83ea-adf6-482630f06332"
        with mock.patch.object(wa, "pending_gate_path_for_working_set",
                               return_value=""):
            acts = wa._explain({}, {"url": conv, "platform": "chatgpt",
                                    "conv_id": "/c/6ae80db0-2820-83ea-adf6-482630f06332"},
                               {"healthy": True, "duplicates": [],
                                "instances": []}, {"ok": True}, "s1", 100,
                               platforms.get("chatgpt"))
        joined = json.dumps(acts, ensure_ascii=False)
        self.assertIn("不要 --new", joined, "已有对话被回收时依旧禁止另起炉灶")
        self.assertIn(conv, joined)


if __name__ == "__main__":
    unittest.main()


class TestTabScopeDictFormat(unittest.TestCase):
    """回归：bsk tab list 实际返回 {"tabs":[...]}，旧代码当 list 解析 → 永远 unknown
    （2026-11-02 现场：发送被 unknown 拒绝，绑定标签明明在 agent 窗口）。"""

    def test_scope_report_accepts_dict_shape(self):
        fake = {"tabs": [
            {"tab_id": 1, "url": "https://x/c/conv", "scope": "agent"},
            {"tab_id": 2, "url": "https://other", "scope": "user"},
        ]}
        with mock.patch.object(wa, "bsk", return_value=(0, json.dumps(fake), "")):
            rep = wa.tab_scope_report("s1")
        # 关键判据：dict 形状被正确解析成 URL 列表（旧代码返回 None = 永远 unknown）
        self.assertEqual(rep["agent"], ["https://x/c/conv", "https://other"])
        self.assertEqual(rep["user"], ["https://x/c/conv", "https://other"])

    def test_scope_report_none_only_when_truly_bad(self):
        with mock.patch.object(wa, "bsk", return_value=(0, "not-json", "")):
            rep = wa.tab_scope_report("s1")
        self.assertIsNone(rep["agent"])
        self.assertIsNone(rep["user"])


class TestReapOwnTabs(unittest.TestCase):
    """tab 回收（2026-11-02 裁定后重写）：判据来自所有权账本，不再靠 URL 猜。

    bsk 的 tab list 不提供「谁开的」，所以旧用例（不登记账本就期望关标签）
    已被裁定推翻——现在**没有 owner_key / 账本里没有记录就一个都不关**。
    这里按新契约重建：先记账，再验证「只关闭自己持有 + 非活动 + 重复/空白」。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._db = os.environ.get(tabclaims.DB_ENV)
        self._au = os.environ.get(tabclaims.AUDIT_ENV)
        os.environ[tabclaims.DB_ENV] = os.path.join(self.tmp, "tab-claims.db")
        os.environ[tabclaims.AUDIT_ENV] = os.path.join(self.tmp, "audit.jsonl")

    def tearDown(self):
        for k, v in ((tabclaims.DB_ENV, self._db), (tabclaims.AUDIT_ENV, self._au)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self, tab_ids, owner_key="ws", run_id="r1"):
        conn = tabclaims.connect()
        try:
            for tid in tab_ids:
                tabclaims.claim(conn, "s1", str(tid), owner_key, run_id=run_id,
                                lease_id="L1", fencing_token=7)
        finally:
            conn.close()

    def _run(self, tabs, dry=False, owner_key="ws", run_id="r1"):
        closes = []

        def fake_bsk(args, timeout=30, **kw):
            if args[:2] == ["tab", "list"]:
                return 0, json.dumps({"tabs": tabs}), ""
            if args[:2] == ["tab", "close"]:
                closes.append(args[4])  # ["tab","close","--session",sid,tab_id]
                return 0, "{}", ""
            return 0, "{}", ""

        with mock.patch.object(wa, "bsk", side_effect=fake_bsk):
            rep = wa.reap_own_tabs("s1", "https://chatgpt.com/c/conv", dry=dry,
                                   owner_key=owner_key, run_id=run_id)
        return rep, closes

    def test_closes_duplicates_and_blanks_keeps_newest(self):
        tabs = [
            {"tab_id": 1, "url": "about:blank", "scope": "agent"},
            {"tab_id": 2, "url": "https://chatgpt.com/c/conv", "scope": "agent"},
            {"tab_id": 3, "url": "https://chatgpt.com/c/conv", "scope": "agent",
             "active": True},
            {"tab_id": 4, "url": "https://other.example/x", "scope": "agent"},
            {"tab_id": 5, "url": "https://user.example/y", "scope": "user"},
        ]
        self._seed([1, 2, 3, 4])
        rep, closes = self._run(tabs)
        # 空白残留（有正常标签）+ 同地址重复的旧副本被回收；活动副本和独立标签保留
        self.assertEqual(sorted(closes), ["1", "2"])
        self.assertEqual(rep["user_scope"], 1)   # 用户标签只报告不动
        self.assertNotIn("5", closes)

    def test_no_owner_key_closes_nothing(self):
        """没有 owner_key = 无法证明归属 → 一个都不关（裁定：宁可积累也不误关）。"""
        tabs = [{"tab_id": 1, "url": "about:blank", "scope": "agent"},
                {"tab_id": 2, "url": "https://chatgpt.com/c/conv", "scope": "agent"}]
        self._seed([1, 2])
        rep, closes = self._run(tabs, owner_key="")
        self.assertEqual(closes, [])
        self.assertTrue(all(r.get("why") == "no_owner_key"
                            for r in rep["report_only"]))

    def test_unclaimed_tab_never_closed(self):
        """账本里没有记录的标签不属于任何任务 → 只报告，不关。"""
        tabs = [{"tab_id": 1, "url": "about:blank", "scope": "agent"},
                {"tab_id": 2, "url": "https://chatgpt.com/c/conv", "scope": "agent"},
                {"tab_id": 3, "url": "https://chatgpt.com/c/conv", "scope": "agent"}]
        self._seed([3])          # 只有 3 是「我」的
        rep, closes = self._run(tabs)
        self.assertEqual(closes, [])
        why = {r["tab_id"]: r["why"] for r in rep["report_only"]}
        self.assertEqual(why.get("1"), "not_owner")

    def test_other_owners_tab_never_closed(self):
        tabs = [{"tab_id": 1, "url": "about:blank", "scope": "agent"},
                {"tab_id": 2, "url": "https://chatgpt.com/c/conv", "scope": "agent"}]
        self._seed([1, 2], owner_key="other-ws", run_id="r9")
        rep, closes = self._run(tabs)
        self.assertEqual(closes, [])

    def test_all_blank_keeps_one(self):
        tabs = [
            {"tab_id": 1, "url": "about:blank", "scope": "agent"},
            {"tab_id": 2, "url": "about:blank", "scope": "agent", "active": True},
        ]
        self._seed([1, 2])
        rep, closes = self._run(tabs)
        self.assertEqual(closes, ["1"])  # 只剩空白页时保留最新一个（导航目标）

    def test_dry_run_closes_nothing(self):
        tabs = [
            {"tab_id": 1, "url": "about:blank", "scope": "agent"},
            {"tab_id": 2, "url": "https://chatgpt.com/c/conv", "scope": "agent"},
            {"tab_id": 3, "url": "https://chatgpt.com/c/conv", "scope": "agent"},
        ]
        self._seed([1, 2, 3])
        rep, closes = self._run(tabs, dry=True)
        self.assertEqual(closes, [])
        self.assertEqual(len(rep["closed"]), 2)
        self.assertTrue(all(c.get("note") == "DRY-RUN" for c in rep["closed"]))

    def test_ledger_unavailable_closes_nothing(self):
        """账本打不开 = 无法证明归属 = 一个都不许关（fail-closed）。"""
        tabs = [{"tab_id": 1, "url": "about:blank", "scope": "agent"},
                {"tab_id": 2, "url": "https://chatgpt.com/c/conv", "scope": "agent"}]
        os.environ[tabclaims.DB_ENV] = self.tmp   # 目录当文件 → connect 失败
        rep, closes = self._run(tabs)
        self.assertEqual(closes, [])
        self.assertIn("error", rep)
        self.assertTrue(all(r.get("why") == "ledger_unavailable"
                            for r in rep["report_only"]))

    def test_close_failure_recorded_not_raised(self):
        def fake_bsk(args, timeout=30, **kw):
            if args[:2] == ["tab", "list"]:
                return 0, json.dumps({"tabs": [
                    {"tab_id": 1, "url": "about:blank", "scope": "agent"},
                    {"tab_id": 2, "url": "https://x", "scope": "agent"}]}), ""
            return 1, "", "borrow required"

        self._seed([1, 2])
        with mock.patch.object(wa, "bsk", side_effect=fake_bsk):
            # 默认 dry=True（裁定：默认只报告）；这里要验证真实关闭失败被记录不抛
            rep = wa.reap_own_tabs("s1", owner_key="ws", run_id="r1", dry=False)
        self.assertEqual(rep["closed"], [])
        self.assertTrue(rep["close_failed"])   # 失败进 close_failed，不抛异常
        self.assertEqual(len(rep["close_failed"]), 1)

    def test_tab_list_failure_is_reported_not_fatal(self):
        with mock.patch.object(wa, "bsk", return_value=(1, "", "session gone")):
            rep = wa.reap_own_tabs("s1")
        self.assertIn("error", rep)
        self.assertEqual(rep["closed"], [])


class TestHealReuseBeforeCreate(unittest.TestCase):
    """heal 复用优先（2026-11-02 用户指令）：同址标签 → 空白残留 → 最后才 create。
    禁止无脑 tab create（历史现场：heal 反复 create 留下 about:blank 孤儿）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path, self.st = _state(self.tmp)

    def _heal(self, find_ret, blank_ret):
        calls = []

        def fake_bsk(args, timeout=60, **kw):
            calls.append(args)
            return 0, "{}", ""

        with mock.patch.object(wa, "window_report",
                               side_effect=[{"ok": False}, {"ok": True}]), \
                mock.patch.object(wa, "find_tab_by_url", return_value=find_ret), \
                mock.patch.object(wa, "_find_blank_tab", return_value=blank_ret), \
                mock.patch.object(wa, "navigate_soft", return_value=(0, "", "")), \
                mock.patch.object(wa, "bsk", side_effect=fake_bsk):
            rep = wa.heal_window_placement(self.st, self.path, "sess1")
        return rep, calls

    def test_reuse_same_url_tab_no_create(self):
        rep, calls = self._heal("9", "")
        self.assertTrue(rep["ok"])
        self.assertNotIn(["tab", "create", "--session", "sess1"], calls)
        self.assertIn(["tab", "select", "--session", "sess1", "9"], calls)

    def test_blank_tab_reused_before_create(self):
        rep, calls = self._heal("", "8")
        self.assertTrue(rep["ok"])
        self.assertNotIn(["tab", "create", "--session", "sess1"], calls)
        self.assertIn(["tab", "select", "--session", "sess1", "8"], calls)

    def test_create_only_as_last_resort(self):
        rep, calls = self._heal("", "")
        self.assertTrue(rep["ok"])
        self.assertIn(["tab", "create", "--session", "sess1"], calls)


class TestNavigateGate(unittest.TestCase):
    """导航所有权硬门（裁定 §2）：证明不了归属就不许写。

    判据来自账本 + 窗口租约，不看 URL、不看「同 session」。这里把 bsk 与
    winlease.read 都替身掉，只验证状态迁移本身。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        self._db = os.environ.get(tabclaims.DB_ENV)
        self._au = os.environ.get(tabclaims.AUDIT_ENV)
        os.environ[tabclaims.DB_ENV] = os.path.join(self.tmp, "c.db")
        os.environ[tabclaims.AUDIT_ENV] = os.path.join(self.tmp, "a.jsonl")
        self.path, self.st = _state(self.tmp, run_id="r1", browser="inst1")
        self.st["run_id"] = "r1"
        wa._remember_ctx(self.st, self.path)
        self.lease = {"lease_id": "L1", "fencing": 3, "lease_until": time.time() + 300}
        p = mock.patch.object(wa.winlease, "read", side_effect=lambda inst: self.lease)
        p.start()
        self.addCleanup(p.stop)
        tabs = [{"tab_id": 1, "url": "https://chatgpt.com/c/x", "scope": "agent",
                 "active": True, "window_id": 9}]
        q = mock.patch.object(wa, "_agent_tabs", return_value=tabs)
        q.start()
        self.addCleanup(q.stop)

    def tearDown(self):
        for k, v in ((tabclaims.DB_ENV, self._db), (tabclaims.AUDIT_ENV, self._au)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _owner(self):
        return wa._win_owner(self.st, self.path)["workspace"]

    def _seed(self, owner, lease_id="L1", fencing=3, intent="reused", state=None,
              must=0):
        conn = tabclaims.connect()
        try:
            tabclaims.claim(conn, "sess1", "1", owner, run_id="r1",
                            lease_id=lease_id, fencing_token=fencing, intent=intent)
            if state:
                conn.execute("UPDATE tabs SET state=? WHERE tab_id='1'", (state,))
            if must:
                conn.execute("UPDATE tabs SET must_reinspect=1 WHERE tab_id='1'")
        finally:
            conn.close()

    def test_no_ctx_fails_closed(self):
        wa._LAST_CTX["st"] = None
        try:
            g = wa.navigate_gate("sess1", "https://x")
        finally:
            wa._remember_ctx(self.st, self.path)
        self.assertFalse(g["ok"])
        self.assertEqual(g["why"], "no_owner_ctx")

    def test_iab_exempt(self):
        self.assertTrue(wa.navigate_gate("iab", "https://x")["ok"])

    def test_unclaimed_is_claimed_then_allowed(self):
        g = wa.navigate_gate("sess1", "https://x")
        self.assertTrue(g["ok"], g)
        self.assertEqual(g["why"], "claimed_now")

    def test_own_tab_allowed(self):
        self._seed(self._owner())
        g = wa.navigate_gate("sess1", "https://x")
        self.assertTrue(g["ok"], g)

    def test_other_owner_alive_refused(self):
        self._seed("someone-else", lease_id="L1", fencing=3)   # 租约还是活的
        g = wa.navigate_gate("sess1", "https://x")
        self.assertFalse(g["ok"])
        self.assertEqual(g["why"], "lease_alive")

    def test_expired_lease_but_holder_alive_refused(self):
        """租约过期 ≠ 任务已死（2026-11-02 复审裁定）：进程还在就不许接管。"""
        rec = winlease.make_owner(workspace=self.tmp, run_id="other")
        rec["lease_id"] = "L-OLD"
        rec["lease_until"] = time.time() - 5          # 授权已过期
        self.lease = rec                              # 但 pid 就是本测试进程（活）
        self._seed("someone-else", lease_id="L-OLD", fencing=1)
        g = wa.navigate_gate("sess1", "https://x")
        self.assertFalse(g["ok"])
        self.assertEqual(g["why"], "owner_unknown_pid_alive")

    def test_cross_host_lease_expired_refused(self):
        """跨主机不能凭本地租约失效接管：必须另有隔离/停止证据。"""
        self.lease = {"lease_id": "L-OLD", "lease_until": time.time() - 5,
                      "host": "another-box", "pid": 4242, "pid_start": 1.0}
        self._seed("someone-else", lease_id="L-OLD", fencing=1)
        g = wa.navigate_gate("sess1", "https://x")
        self.assertFalse(g["ok"])
        self.assertEqual(g["why"], "owner_unknown_host_mismatch")

    def test_no_lease_record_refused(self):
        """租约文件没了 = 拿不到进程身份 = 证明不了死亡 → 不接管。"""
        self.lease = None
        self._seed("someone-else", lease_id="L-OLD", fencing=1)
        g = wa.navigate_gate("sess1", "https://x")
        self.assertFalse(g["ok"])
        self.assertEqual(g["why"], "owner_unknown_no_lease")

    def test_owner_dead_taken_over(self):
        """旧授权失效 + 原持有者进程确认死亡 → 接管（组窗口复用的合法移交）。"""
        self.lease = {"lease_id": "L-OLD", "fencing": 1,
                      "lease_until": time.time() - 60,
                      "host": __import__("socket").gethostname(),
                      "pid": 999999, "pid_start": 1.0}     # 不存在的 PID
        self._seed("someone-else", lease_id="L-OLD", fencing=1)
        g = wa.navigate_gate("sess1", "https://x")
        self.assertTrue(g["ok"], g)
        self.assertEqual(g["why"], "took_over_stale")
        self.assertEqual(g["evidence"], "holder_dead_pid_gone")
        conn = tabclaims.connect()
        try:
            row = tabclaims.get(conn, "sess1", "1")
        finally:
            conn.close()
        self.assertEqual(row["owner_key"], self._owner())
        # 本用例里 mock 让「我们的租约」与「死者租约」共用一条记录，所以 lease_id
        # 仍是 L-OLD；关键是 owner_key 换了人、审计记成 stale holder。
        self.assertEqual(row["state"], tabclaims.STATE_CLAIMED)

    def test_pid_reused_taken_over(self):
        """PID 被别的进程复用（启动时间不符）→ 原持有者已死，可接管。"""
        self.lease = {"lease_id": "L-OLD", "fencing": 1,
                      "lease_until": time.time() - 60,
                      "host": __import__("socket").gethostname(),
                      "pid": os.getpid(), "pid_start": 1.0}   # 时间明显不符
        self._seed("someone-else", lease_id="L-OLD", fencing=1)
        g = wa.navigate_gate("sess1", "https://x")
        self.assertTrue(g["ok"], g)
        self.assertEqual(g["evidence"], "holder_dead_pid_reused")

    def test_borrowed_refused(self):
        self._seed(self._owner(), intent="borrowed")
        g = wa.navigate_gate("sess1", "https://x")
        self.assertFalse(g["ok"])
        self.assertEqual(g["why"], "borrowed_read_only")

    def test_unknown_refused(self):
        self._seed(self._owner(), state="unknown")
        g = wa.navigate_gate("sess1", "https://x")
        self.assertFalse(g["ok"])
        self.assertEqual(g["why"], "state_unknown")

    def test_ledger_unavailable_fails_closed(self):
        os.environ[tabclaims.DB_ENV] = self.tmp        # 目录当文件 → 打不开
        g = wa.navigate_gate("sess1", "https://x")
        self.assertFalse(g["ok"])
        self.assertIn("TABCLAIM", g["why"])            # 报清「账本不可用」而不是沉默放行


class TestHomeFormNotPrefixMatched(unittest.TestCase):
    """首页/新对话形态**不许**被前缀匹配成「和用户窗口同一路」（2026-11-04 实测修）。

    真实现场：`bind --new` 把 bound 落成 `https://chatgpt.com/`，而用户窗口里开着
    两条 ChatGPT 会话（`/c/6ae9689e…`、`/c/6ae80db0…`）。旧判据
    `tab.startswith(bound)` 让 `bound_in_user=True` ⇒ `misplaced` ⇒ 写门把
    `bind --new` 之后的第一次发送拦死。绑完整会话 URL 时一切正常，
    绑首页时必然误判——症状就是「换个 agent、换个时刻就不顺」，
    差别只在用户当时开没开着那个网页。
    """

    USER_TABS = ["https://chatgpt.com/c/6ae9689e-6124-83ea-a00c-06e02c123610",
                 "https://chatgpt.com/c/6ae80db0-2820-83ea-adf6-482630f06332"]
    AGENT_TABS = ["https://chatgpt.com/"]

    def _report(self, bound):
        import unittest.mock as mock
        rep = {"agent": list(self.AGENT_TABS), "user": list(self.USER_TABS),
               "ok": True}
        with mock.patch.object(wa, "tab_scope_report", return_value=rep):
            return wa.window_report({"url": bound, "platform": "chatgpt"}, "lhyh")

    def test_home_bound_is_not_confused_with_users_conversations(self):
        out = self._report("https://chatgpt.com/")
        self.assertTrue(out["bound_in_agent"], "首页就在 Agent Window 里")
        self.assertFalse(out["bound_in_user"],
                         "用户窗口里是**会话页**，不是首页，不能算同一路")
        self.assertTrue(out["ok"], "归属必须判正常，否则 bind --new 后第一次发送被误拦")

    def test_conversation_bound_still_matches_query_variants(self):
        """会话形态要继续容忍 `?utm=…` 这类尾巴——原来的宽松匹配是有理由的。"""
        import unittest.mock as mock
        rep = {"agent": ["https://chatgpt.com/c/6ae80db0-2820-83ea?utm=x"],
               "user": ["https://chatgpt.com/c/99999"], "ok": True}
        with mock.patch.object(wa, "tab_scope_report", return_value=rep):
            out = wa.window_report({"url": "https://chatgpt.com/c/6ae80db0-2820-83ea"},
                                   "s")
        self.assertTrue(out["bound_in_agent"])
        self.assertFalse(out["bound_in_user"])
        self.assertTrue(out["ok"])

    def test_same_conversation_open_in_both_windows_is_misplaced(self):
        """同一条会话同时出现在用户窗口 = 真歧义，照旧拒绝（不许放松成放行）。"""
        import unittest.mock as mock
        dup = "https://chatgpt.com/c/6ae80db0-2820-83ea"
        rep = {"agent": [dup], "user": [dup], "ok": True}
        with mock.patch.object(wa, "tab_scope_report", return_value=rep):
            out = wa.window_report({"url": dup}, "s")
        self.assertTrue(out["bound_in_user"])
        self.assertFalse(out["ok"])

    def test_sibling_prefix_is_not_the_same_page(self):
        """`/c/abc` 不能匹配 `/c/abcdef`：那是另一条对话，打错就是往别人对话里打字。"""
        self.assertFalse(wa._same_place("https://x.com/c/abc", "https://x.com/c/abcdef"))
        self.assertTrue(wa._same_place("https://x.com/c/abc", "https://x.com/c/abc?s=1"))
        self.assertTrue(wa._same_place("https://x.com/c/abc", "https://x.com/c/abc/sub"))

    def test_trailing_slash_forms_are_equivalent(self):
        self.assertTrue(wa._same_place("https://chatgpt.com/", "https://chatgpt.com"))
        self.assertTrue(wa._same_place("https://chatgpt.com/app", "https://chatgpt.com/app/"))

    def test_different_host_never_matches(self):
        self.assertFalse(wa._same_place("https://chatgpt.com/", "https://deepseek.com/"))


class TestMisplacedReasonAndExtensionFlags(unittest.TestCase):
    """misplaced 必须说得出**为什么**；扩展开关状态必须被报出来（2026-11-04 实测）。

    现场：Agent Window 里只有 `about:blank`，绑定页面却在用户窗口。
    这与「Agent Window 里有别的页面」处置完全相反（前者要人改扩展设置，
    后者要自查导航），都印成一句 misplaced 就等于让 agent 反复重试同一个动作。
    另一条：扩展 `interaction.request_help=disabled` 时，`wa.py ask-human`
    必然失败——不提前说，会被误读成「这功能/命令是坏的」。
    """

    def _stub_alive(self):
        """把 session_state 固定成 alive，并在本条测试结束后自动还原。"""
        orig = wa.session_state
        wa.session_state = lambda sid: "alive"
        self.addCleanup(setattr, wa, "session_state", orig)

    def _report(self, agent, user, bound="https://chatgpt.com/"):
        import unittest.mock as mock
        rep = {"agent": agent, "user": user}
        with mock.patch.object(wa, "tab_scope_report", return_value=rep):
            return wa.window_report({"url": bound}, "s")

    def test_blank_agent_window_gets_its_own_reason(self):
        out = self._report(["about:blank"], ["https://chatgpt.com/"])
        self.assertFalse(out["ok"])
        self.assertTrue(out["agent_blank_only"])
        self.assertEqual(out["misplaced_reason"],
                         "agent_window_blank_page_in_user_window")

    def test_wrong_page_in_agent_window_is_a_different_reason(self):
        out = self._report(["https://example.com/other"], ["https://chatgpt.com/"])
        self.assertFalse(out["ok"])
        self.assertFalse(out["agent_blank_only"])
        self.assertEqual(out["misplaced_reason"], "page_not_in_agent_window")

    def test_explain_turns_blank_agent_into_an_extension_fix(self):
        import unittest.mock as mock
        self._stub_alive()
        wr = self._report(["about:blank"], ["https://chatgpt.com/"])
        with mock.patch.object(wa, "pending_gate_path_for_working_set",
                               return_value=""):
            acts = wa._explain({}, {"url": "https://chatgpt.com/",
                                    "platform": "chatgpt"},
                               {"healthy": True, "duplicates": [],
                                "instances": []}, wr, "s1", 100,
                               platforms.get("chatgpt"))
        joined = json.dumps(acts, ensure_ascii=False)
        self.assertIn("独立 Agent Window", joined, "要指到人能改的那一项设置上")
        self.assertIn("不要改守卫", joined, "必须禁止用放松守卫来「解决」它")

    def test_explain_warns_when_request_help_is_disabled(self):
        import unittest.mock as mock
        self._stub_alive()
        out = {"extension": {"request_help": "disabled"}}
        with mock.patch.object(wa, "pending_gate_path_for_working_set",
                               return_value=""):
            acts = wa._explain(out, {"url": "https://chatgpt.com/",
                                     "platform": "chatgpt"},
                               {"healthy": True, "duplicates": [],
                                "instances": []}, {"ok": True}, "s1", 100,
                               platforms.get("chatgpt"))
        joined = json.dumps(acts, ensure_ascii=False)
        self.assertIn("ask-human", joined)
        self.assertIn("必然失败", joined, "要说清后果，不是只列一个字段值")

    def test_extension_interaction_read_from_session_list(self):
        sessions = [{"id": "s1", "interaction": {"request_help": "disabled",
                                                 "borrow_confirmation": "never"}},
                    {"id": "s2", "interaction": {"request_help": "enabled"}}]
        got = wa.extension_interaction(sessions, "s1")
        self.assertEqual(got["request_help"], "disabled")
        self.assertIn("扩展自报", got["source"])
        self.assertEqual(wa.extension_interaction(sessions, "nope"), {},
                         "查不到就返回空，不许编一个默认值")
