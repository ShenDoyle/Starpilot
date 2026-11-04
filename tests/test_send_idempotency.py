# -*- coding: utf-8 -*-
"""发送幂等与三态失败分报（2026-11-03）。

要治的两个真实故障（都来自 pending.md 的违例记录本身）：

1. **同一条提问被发两遍**：`last_send_hash` 以前只写不读，CLI 层没有任何内容
   幂等；「已发出但没确认」时输入框已空、`awaiting_reply` 也没置上，
   agent 的本能就是再敲一次 `wa.py send`。实测有「文本滞留在输入框未发出」
   与「前两次尝试超时未确认发出」这类条目。
2. **三种失败被压成同一个退出码**：没写进去 / 写了没发出去 / 发出去但没确认，
   接下来该做的事**相反**（前两者重发是安全的，第三者重发就是重复提问），
   但机器输出里分不开，agent 只能猜。
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import confirm        # noqa: E402
import platforms      # noqa: E402
import wa             # noqa: E402


def _snap(**kw):
    """造一份 probe 快照（字段与 platforms.js_probe 输出对齐）。"""
    base = {"url": "https://chatgpt.com/c/abcdef01-2345-6789-abcd-ef0123456789",
            "msg_count": 2, "reply_count": 1, "user_msg_count": 2,
            "editor_len": 0, "editor_blank": True, "can_send": True,
            "generating": False, "modal": False, "regen": True,
            "asst_fps": ["f1"], "user_fps": ["u1"], "msg_fps": ["f1", "u1"],
            "asst_keys": ["u:aaa"], "msg_keys": ["u:aaa", "u:bbb"],
            "last_asst_fp": "f1", "last_user_fp": "u1", "last_len": 500,
            "last_tail": "x", "doc_len": 26933, "doc_nodes": 3349,
            "error": ""}
    base.update(kw)
    return base


class _Harness(unittest.TestCase):
    """把 cmd_send 会碰浏览器的四个出口全部换成可编程的假件。"""

    URL = "https://chatgpt.com/c/abcdef01-2345-6789-abcd-ef0123456789"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.oldcwd = os.getcwd()
        os.chdir(self.tmp)
        os.environ["STARPILOT_LOCK_DIR"] = os.path.join(self.tmp, "locks")
        self.st_path = wa.state_path(None)
        st = wa.load_state(self.st_path)
        st.update({"url": self.URL, "platform": "chatgpt",
                   "conv_id": platforms.conv_id_of(platforms.get("chatgpt"), self.URL)})
        wa.save_state(self.st_path, st)
        self._orig = {}
        for name in ("ensure_session", "read_state", "do_fill", "do_send",
                     "evaluate", "require_window_verified"):
            self._orig[name] = getattr(wa, name)
        # 窗口归属门是真机才有的东西（session/lease 账本），本文件测的是
        # 幂等与失败分报，不是它 —— 直接放行，让它不参与断言。
        wa.require_window_verified = lambda st, path, sid: (True, "测试放行")
        # 默认可用的成功链路，测试各自覆盖
        self.before = _snap()
        self.after = _snap(msg_count=3, user_msg_count=3, reply_count=2,
                           asst_keys=["u:aaa", "u:ccc"],
                           asst_fps=["f1", "f3"], last_len=900)
        wa.ensure_session = lambda *a, **k: "sid-fake"
        self._seq = []
        wa.read_state = self._read_state
        wa.do_fill = lambda p, t, sid: {"ok": True, "code": platforms.FILL_OK,
                                        "facts": {}, "detail": ""}
        wa.do_send = lambda p, sid: (True, {"ok": True})
        wa.evaluate = lambda js, sid: (True, {"ok": True})
        # 别在测试里真等 30 秒
        self._sleep = wa.time.sleep
        wa.time.sleep = lambda s: None

    def tearDown(self):
        for name, fn in self._orig.items():
            setattr(wa, name, fn)
        wa.time.sleep = self._sleep
        os.chdir(self.oldcwd)
        os.environ.pop("STARPILOT_LOCK_DIR", None)

    def _read_state(self, p, sid):
        if self._seq:
            return True, self._seq.pop(0)
        return True, self.before

    def _send(self, text="同一句话", *args):
        buf, ebuf = io.StringIO(), io.StringIO()
        with redirect_stdout(buf), redirect_stderr(ebuf):
            rc = wa.main(["send", "--text", text] + list(args))
        return rc, buf.getvalue(), ebuf.getvalue()


class TestContentIdempotency(_Harness):
    def test_same_text_second_time_is_blocked(self):
        # 第一轮：正常成功
        self._seq = [self.after] * 25
        rc, out, _ = self._send("同一句话")
        self.assertEqual(rc, 0, out)
        st = wa.load_state(self.st_path)
        self.assertTrue(st.get("last_send_hash"))
        self.assertEqual(st.get("last_send_conv"), st.get("conv_id"))
        # 第二轮：同一条正文、还没确认回复 → 必须拦，且要给下一步
        st2 = wa.load_state(self.st_path)
        st2["awaiting_reply"] = False      # 模拟 awaiting_reply 已被别的路径清掉
        wa.save_state(self.st_path, st2)
        rc2, _, err2 = self._send("同一句话")
        self.assertEqual(rc2, 12, err2)
        self.assertIn("--force", err2)
        self.assertIn("已经发过一次", err2)

    def test_force_overrides_idempotency(self):
        st = wa.load_state(self.st_path)
        st["last_send_hash"] = __import__("hashlib").sha256(
            "同一句话".encode("utf-8")).hexdigest()[:16]
        st["last_send_conv"] = st.get("conv_id") or ""
        st["last_send_at"] = "2026-11-03 18:00:00"
        wa.save_state(self.st_path, st)
        self._seq = [self.after] * 25
        rc, out, err = self._send("同一句话", "--force")
        self.assertEqual(rc, 0, err + out)

    def test_different_text_is_not_blocked(self):
        st = wa.load_state(self.st_path)
        st["last_send_hash"] = "0123456789abcdef"
        st["last_send_conv"] = st.get("conv_id") or ""
        st["last_send_at"] = "2026-11-03 18:00:00"
        wa.save_state(self.st_path, st)
        self._seq = [self.after] * 25
        rc, out, err = self._send("另一句完全不同的话")
        self.assertEqual(rc, 0, err + out)

    def test_same_text_in_another_conversation_is_not_blocked(self):
        """幂等判据带 conv_id：换对话了就不算重复（硬规则 1 之外的情形）。"""
        h = __import__("hashlib").sha256("同一句话".encode("utf-8")).hexdigest()[:16]
        st = wa.load_state(self.st_path)
        st.update({"last_send_hash": h, "last_send_conv": "/c/other",
                   "last_send_at": "2026-11-03 18:00:00"})
        wa.save_state(self.st_path, st)
        self._seq = [self.after] * 25
        rc, out, err = self._send("同一句话")
        self.assertEqual(rc, 0, err + out)


class TestThreeStateFailure(_Harness):
    def test_not_sent_is_rc8_and_says_retry_is_safe(self):
        """主信号全无 + 输入框仍有字 = 确实没发出去（rc=8，可以清空重发）。"""
        stuck = _snap(editor_len=4747, editor_blank=False, msg_count=2,
                      reply_count=1, asst_keys=["u:aaa"])
        # 发送前那次探测必须是干净的：否则会被「输入框已有残留」的**前置**守卫
        # 拦在 rc=3（那条守卫本身就是对的——实测过的「文本滞留」就靠它发现）
        self._seq = [self.before] + [stuck] * 30
        wa.evaluate = lambda js, sid: (True, {"ok": False})
        rc, _, err = self._send("发不出去的话")
        self.assertEqual(rc, 8, err)
        self.assertIn("确实没发出去", err)
        st = wa.load_state(self.st_path)
        self.assertFalse(st.get("maybe_sent"), "确认没送达时不该留滞留标记")

    def test_residual_text_in_editor_is_refused_before_sending(self):
        """前置守卫：进 send 时输入框已经有字，直接拒（不许把新话接在残留后面）。"""
        self._seq = [_snap(editor_len=99, editor_blank=False)] * 5
        rc, _, err = self._send("接着残留往下写")
        self.assertEqual(rc, 3, err)
        self.assertIn("未清空", err)

    def test_unconfirmed_send_is_rc9_and_marks_maybe_sent(self):
        """输入框已空但没有主信号 = 可能已发出（rc=9，禁止盲目重发）。"""
        cleared = _snap(editor_len=0, editor_blank=True, msg_count=2,
                        reply_count=1, asst_keys=["u:aaa"])
        self._seq = [cleared] * 30
        wa.evaluate = lambda js, sid: (True, {"ok": False})
        rc, _, err = self._send("疑似发出去了")
        self.assertEqual(rc, 12, err)
        self.assertIn("不要直接重试", err)
        st = wa.load_state(self.st_path)
        self.assertTrue(st.get("maybe_sent"), "滞留态必须落进 state，否则下一轮拦不住")
        self.assertEqual(st["maybe_sent"]["text_sha"],
                         __import__("hashlib").sha256(
                             "疑似发出去了".encode("utf-8")).hexdigest()[:16])
        # 紧接着再敲一次同样的话：必须被滞留态拦住
        rc2, _, err2 = self._send("疑似发出去了", "--platform", "chatgpt")
        self.assertEqual(rc2, 12, err2)
        self.assertIn("疑似已发出但未确认", err2)

    def test_sent_no_reply_rc0_tells_agent_to_wait_not_resend(self):
        """送达但还没认出回复：rc=0，但输出必须把「等」而不是「重发」说清楚。"""
        delivered = _snap(msg_count=3, user_msg_count=3, reply_count=1,
                          asst_keys=["u:aaa"])
        self._seq = [delivered] * 30
        rc, out, err = self._send("已送达等回复")
        self.assertEqual(rc, 0, err + out)
        data = json.loads(out)
        self.assertEqual(data["verdict"], confirm.SENT_NO_REPLY)
        self.assertIn("wa.py wait", data.get("next", ""))
        self.assertIn("不要重发", data.get("next", ""))


class TestWritePathWhitelistIsSharedSource(_Harness):
    """CLI 与闸门必须同源判断写权限（platforms.write_path_ok），否则白名单是装饰。"""

    def test_unverified_platform_refused_by_cli(self):
        rc, _, err = self._send("任何内容", "--platform", "claude")
        self.assertEqual(rc, 11, err)
        self.assertIn("不具备真实写条件", err)
        self.assertIn("unverified", err)

    def test_write_only_platform_allowed_by_cli(self):
        """ChatGPT：写路径复测过、整链路未验收 ⇒ 允许真实写（否则只能走逃生门）。"""
        self.assertTrue(platforms.write_path_ok(platforms.get("chatgpt")))
        self.assertFalse(platforms.loop_accepted(platforms.get("chatgpt")))
        self._seq = [self.after] * 25
        rc, out, err = self._send("走正路的一句话")
        self.assertEqual(rc, 0, err + out)


class TestNewChatAdoptionIsShared(unittest.TestCase):
    """新对话首轮发出后的地址采纳，CLI 与闸门必须共用一个实现。

    真机实测的坑（2026-11-04 5/5 验收第 2 轮）：`bind --new` 绑的是首页，
    发出第一条后 ChatGPT 把地址换成 `/c/<uuid>`。采纳逻辑以前只写在
    `wa.cmd_send` 里，而闸门走 `write_gate.execute_send`（不碰 state），
    于是第 2 轮 `plan` 被自己的 `guard_ok` 判「地址漂移」拦死：
    **同一条正路，第一轮通、第二轮必挂。**
    """

    HOME = "https://chatgpt.com/"
    CONV = "https://chatgpt.com/c/6aea973d-7680-83ec-b5f3-fe2d76ad949b"

    def test_adopt_swaps_home_to_conversation(self):
        p = platforms.get("chatgpt")
        st = {"url": self.HOME, "new_chat": True, "platform": "chatgpt"}
        res = wa.adopt_new_chat(st, self.CONV, p)
        self.assertTrue(res["adopted"])
        self.assertEqual(st["url"], self.CONV)
        self.assertEqual(st["conv_id"], "/c/6aea973d-7680-83ec-b5f3-fe2d76ad949b")
        self.assertEqual(st["conv_origin"], "new_chat")
        self.assertFalse(st["new_chat"], "不翻掉就每轮都当首轮，采纳会重复发生")

    def test_not_new_chat_is_left_alone(self):
        p = platforms.get("chatgpt")
        st = {"url": self.CONV, "conv_id": "/c/6aea973d-7680-83ec-b5f3-fe2d76ad949b"}
        res = wa.adopt_new_chat(st, "https://chatgpt.com/c/other1234", p)
        self.assertEqual(res, {"adopted": False, "reason": "not_new_chat"})
        self.assertEqual(st["url"], self.CONV, "已有对话的地址不许被顺手改掉")

    def test_second_round_would_pass_guard_after_adoption(self):
        """采纳之后，guard_ok 不能再判漂移（这就是第 2 轮被拦的那道门）。"""
        import write_gate
        p = platforms.get("chatgpt")
        st = {"url": self.HOME, "new_chat": True}
        write_gate  # 仅表明依赖关系：闸门 cmd_send 成功后必须调 wa.adopt_new_chat
        wa.adopt_new_chat(st, self.CONV, p)
        snap = {"url": self.CONV, "raw": {"editor": True}, "editor_len": 0,
                "modal": False, "error": ""}
        ok, why = write_gate.guard_ok(snap, st["url"])
        self.assertTrue(ok, "采纳后第 2 轮必须放行，实际：%s" % why)
        # 反向对照：不采纳就会复现第 2 轮的真机故障（绑定首页 vs 当前会话页）
        st2 = {"url": self.HOME, "new_chat": True}
        ok2, why2 = write_gate.guard_ok(snap, st2["url"])
        self.assertFalse(ok2, "没采纳却放行，说明守卫判不住地址漂移——那才是真危险")
        self.assertIn("地址漂移", why2)

    def test_gate_calls_the_shared_adopter(self):
        """结构约束：闸门不许再自己写一套采纳（两处各写必然漂移）。"""
        src = io.open(os.path.join(os.path.dirname(wa.__file__), "write_gate.py"),
                      encoding="utf-8").read()
        self.assertIn("wa.adopt_new_chat", src,
                      "闸门必须复用 wa.adopt_new_chat，别再留第二份实现")
        self.assertNotIn('st["new_chat"] = False', src,
                         "闸门里不该出现自己的采纳实现（那正是这次的成因）")


class TestClearEditorFallsBackToWritePrimitive(unittest.TestCase):
    """键盘清不掉时，clear_editor 必须回落到写入原语（L-39 的修法）。

    实测依据：ChatGPT 把草稿恢复进新开对话，聚焦+Ctrl+A+Delete 连试两轮
    「仍有 25 字」；而同一个编辑器 exec 写 19 字是成功的 —— 说明键盘事件没落到
    编辑器的输入管线，exec 那条路落到了。
    """

    def setUp(self):
        self._orig = {n: getattr(wa, n) for n in ("bsk", "read_state", "evaluate")}
        self.calls = []

    def tearDown(self):
        for n, f in self._orig.items():
            setattr(wa, n, f)

    def _fake_read(self, seq):
        it = iter(seq)

        def f(p, sid):
            return True, next(it)
        wa.read_state = f

    def test_keyboard_success_does_not_use_fallback(self):
        wa.bsk = lambda args, timeout=120: (0, "", "")
        self._fake_read([{"editor_len": 25, "editor_blank": False},
                         {"editor_len": 0, "editor_blank": True}])
        wa.evaluate = lambda js, sid, timeout=60: self.fail("不该走 exec 回落")
        res = wa.clear_editor(platforms.get("chatgpt"), "s")
        self.assertTrue(res["ok"])
        self.assertEqual(res["via"], "keyboard")

    def test_fallback_to_exec_when_keyboard_leaves_text(self):
        p = platforms.get("chatgpt")          # write_method = exec
        self.assertEqual(p.get("write_method"), "exec")
        wa.bsk = lambda args, timeout=120: (0, "", "")
        # 键盘两轮都没清掉 → exec 回落之后清了
        self._fake_read([{"editor_len": 25, "editor_blank": False},
                         {"editor_len": 25, "editor_blank": False},
                         {"editor_len": 25, "editor_blank": False},
                         {"editor_len": 0, "editor_blank": True}])

        def fake_eval(js, sid, timeout=60):
            self.calls.append(js)
            return True, {"ok": True}
        wa.evaluate = fake_eval
        res = wa.clear_editor(p, "s")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["via"], "write-primitive")
        self.assertEqual(len(self.calls), 1, "回落只该调一次 exec 写入")
        self.assertIn("insertText", self.calls[0], "回落必须走写入原语而不是另编一套")

    def test_native_platforms_skip_fallback_with_a_reason(self):
        """native（Gemini 的 Quill）不适用空串替换：如实说明，不假装清了。"""
        p = platforms.get("gemini")
        self.assertEqual(p.get("write_method"), "native")
        wa.bsk = lambda args, timeout=120: (0, "", "")
        self._fake_read([{"editor_len": 20, "editor_blank": False},
                         {"editor_len": 20, "editor_blank": False},
                         {"editor_len": 20, "editor_blank": False}])
        wa.evaluate = lambda js, sid, timeout=60: self.fail("native 不该走 exec 回落")
        res = wa.clear_editor(p, "s")
        self.assertFalse(res["ok"])
        self.assertIn("exec/paste", res.get("fallback_skipped", ""),
                      "必须写清为什么没回落，而不是只报失败")

    def test_still_reports_failure_when_both_paths_fail(self):
        p = platforms.get("chatgpt")
        wa.bsk = lambda args, timeout=120: (0, "", "")
        dirty = {"editor_len": 25, "editor_blank": False}
        self._fake_read([dirty, dict(dirty), dict(dirty), dict(dirty)])
        wa.evaluate = lambda js, sid, timeout=60: (True, {"ok": True})
        res = wa.clear_editor(p, "s")
        self.assertFalse(res["ok"], "两条路都没清掉就必须报失败，不许假装干净")
        self.assertIn("exec 回落也没清掉", res["detail"])


if __name__ == "__main__":
    unittest.main()
