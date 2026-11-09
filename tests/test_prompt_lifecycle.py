# -*- coding: utf-8 -*-
"""活动 prompt 生命周期（G1-A / P0-N1）。

要防的活锁（G1-0 已证实，非假设）：
    replan → READY → loop --once（不带 --file）
    → one_step 读回**上一轮已发送**的 pending.prompt_file
    → 再走一次闸门 → WRITE_BLOCKED_DUPLICATE → 计入失败 → 再 replan
每圈都实打实增加 rounds_used（budget_gate 的口径是「失败的发送也消耗预算」）。

测试一律驱动**生产调用链**（one_step / cmd_loop / cmd_replan），
只把两个外部边界换成替身：run_gate（子进程调 write_gate）与 _window_guard（真机窗口探测）。
四种发送结果必须分开验证——SENT 归档、MAYBE_SENT/INCONCLUSIVE 不归档、
明确失败不归档、幂等拒绝不归档。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import orchestrator as orch   # noqa: E402
import platforms              # noqa: E402
import write_gate as wg       # noqa: E402


class _Args(object):
    def __init__(self, **kw):
        self.__dict__.update(kw)


class PromptWorkspace(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="sp-prompt-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.makedirs(os.path.join(self.root, "references"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "state"), exist_ok=True)
        self.p = platforms.get("kimi")
        # 活动 prompt 文件（放在工作区外一点，验证归档不删它）
        self.pf = os.path.join(self.root, "runtime", "wa", "feedback-1.md")
        os.makedirs(os.path.dirname(self.pf), exist_ok=True)
        with open(self.pf, "w", encoding="utf-8") as f:
            f.write("第一轮：请按判据 X 完成步骤 Y\n")
        self.sha = wg.sha16(open(self.pf, encoding="utf-8").read())

    # ---------- 造 run 状态 ----------
    def make_run(self, state="APPROVED", active=True):
        run_id = "20260101T000000-prompt"
        d = os.path.join(self.root, "runs", run_id)
        os.makedirs(os.path.join(d, "evidence"), exist_ok=True)
        os.makedirs(os.path.join(d, "writes"), exist_ok=True)
        st = {
            "run_id": run_id, "stage_id": "O1", "state": state,
            "goal": dict(orch.DEFAULT_GOAL, text="t"),
            "target_url_sha256": "abc123", "platform": "kimi",
            "round": 0, "rounds_used": 0, "fail_streak": 0, "last_error_class": "",
            "last_guard_failure": None, "state_history": [],
            "pending": {"prompt_file": self.pf if active else "",
                        "prompt_sha256": self.sha if active else "",
                        "approval_id": "ap-1"},
            "last_send": {}, "lessons_candidates": [], "lessons_merged": 0,
            "closed": False,
        }
        wg.atomic_write_json(os.path.join(d, "state.json"), st)
        # state/current 存**裸 run_id**（真实仓库里就是这个形态：
        # run_dir_of(root, rid) 自己会拼 runs/<rid>，写 "runs/xxx" 会拼成双层路径）
        with open(os.path.join(self.root, "state", "current"), "w",
                  encoding="utf-8") as f:
            f.write(run_id)
        return st

    def args(self, **kw):
        base = dict(workspace=self.root, state="", run="", file="", text="",
                    platform="kimi", once=True, round=False, resume=False,
                    max_recover=1, reason="", approval="ap-1", ttl=600,
                    scope="single", max_writes=0, by="human", dry_run=False,
                    probe=False)
        base.update(kw)
        return _Args(**base)

    # ---------- 替身：只换外部边界 ----------
    def stub_gate(self, results):
        """results: {(root, args[0]) → (rc, dict)}，按调用顺序弹出。"""
        seq = list(results)

        def fake(root, args, env_extra=None, state=""):
            r = seq.pop(0) if seq else (1, {"error_code": "NO_MORE_STUBS"})
            return r[0], r[1]
        self.patch(orch, "run_gate", fake)

    def patch(self, obj, name, val):
        orig = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(setattr, obj, name, orig)

    def no_window_guard(self):
        self.patch(orch, "_window_guard", lambda root, st, a, ck: (True, ""))

    def read_state(self):
        return orch.load_run_state(self.root)

    def journal_events(self):
        d = os.path.join(self.root, "runs", self.read_state()["run_id"])
        return wg.read_journal(d)

    def step_from(self, st):
        return orch.one_step(self.root, st, self.p, self.args())


class TestSentRetiresActivePrompt(PromptWorkspace):
    def test_confirmed_sent_clears_pointer_keeps_file_and_sha(self):
        st = self.make_run("APPROVED")
        self.no_window_guard()
        self.stub_gate([(0, {"ok": True, "verdict": "SENT", "reply_count_delta": 1,
                             "reply_count_before": 2, "reply_count_after": 3,
                             "before": {"asst_fps": [], "asst_keys": [], "user_fps": []},
                             "signals": ["msg_count"]})])
        step = self.step_from(st)
        self.assertEqual(step["action"], "sent", step)
        after = self.read_state()
        # ① 活动指针已失效
        self.assertEqual(after["pending"]["prompt_file"], "")
        self.assertEqual(after["pending"]["prompt_sha256"], "")
        # ② 历史可追溯：文件还在、正文没动、sha 记在历史里
        self.assertTrue(os.path.exists(self.pf), "归档不许删原始文件")
        hist = after.get("prompt_history") or []
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["prompt_sha256"], self.sha)
        self.assertEqual(hist[0]["retired_reason"], "sent")
        self.assertEqual(hist[0]["verdict"], "SENT")
        # ③ journal 留痕，且不破坏原有 send 事件
        acts = [e["action"] for e in self.journal_events()]
        self.assertIn("prompt_retired", acts)
        self.assertIn("transition", acts)

    def test_maybe_sent_inconclusive_does_not_clear(self):
        """输入框空了但没主信号 = 不确定。抹掉指针会把「没确认」当成「已了结」。"""
        st = self.make_run("APPROVED")
        self.no_window_guard()
        self.stub_gate([(4, {"ok": False, "verdict": "INCONCLUSIVE_NOT_SUCCESS",
                             "error_class": "send_unconfirmed",
                             "detail": "主信号不满足，输入框已清空"})])
        step = self.step_from(st)
        self.assertTrue(step.get("stop"))
        after = self.read_state()
        self.assertEqual(after["pending"]["prompt_file"], self.pf,
                         "不确定态必须保留活动 prompt 供诊断")
        self.assertEqual(after.get("prompt_history") or [], [])

    def test_explicit_failure_does_not_clear(self):
        st = self.make_run("APPROVED")
        self.no_window_guard()
        self.stub_gate([(4, {"ok": False, "error_class": "fill_failed",
                             "detail": "编辑器拒绝该写入路径"})])
        self.step_from(st)
        after = self.read_state()
        self.assertEqual(after["pending"]["prompt_file"], self.pf,
                         "明确失败时 prompt 是待重试的任务信息，不能抹掉")
        self.assertEqual(after.get("prompt_history") or [], [])

    def test_duplicate_rejection_does_not_clear(self):
        """幂等拒绝 ≠ 发送成功。清掉指针会把「被拦下」伪装成「已了结」。"""
        st = self.make_run("APPROVED")
        self.no_window_guard()
        self.stub_gate([(2, {"ok": False, "error_code": "WRITE_BLOCKED_DUPLICATE",
                             "error_class": "duplicate", "detail": "同 prompt+同目标已发过"})])
        self.step_from(st)
        after = self.read_state()
        self.assertEqual(after["pending"]["prompt_file"], self.pf)
        self.assertEqual(after.get("prompt_history") or [], [])


class TestReplanRetiresOldPrompt(PromptWorkspace):
    def test_replan_clears_active_pointer_without_verdict(self):
        st = self.make_run("PAUSED_HUMAN")
        st["last_error_class"] = "reply_timeout"
        orch.save_run_state(self.root, st)
        rc = orch.cmd_replan(self.args(reason="同目标连续失败"))
        self.assertEqual(rc, 0, rc)
        after = self.read_state()
        self.assertEqual(after["pending"]["prompt_file"], "")
        hist = after.get("prompt_history") or []
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["retired_reason"], "replan")
        self.assertNotIn("verdict", hist[0], "replan 归档不得伪装成已发送")
        self.assertTrue(os.path.exists(self.pf))

    def test_loop_without_file_after_replan_returns_need_prompt(self):
        st = self.make_run("PAUSED_HUMAN")
        orch.save_run_state(self.root, st)
        self.assertEqual(orch.cmd_replan(self.args(reason="换方案")), 0)
        # replan 已把状态推到 READY；此时不带 --file 跑一轮
        st2 = self.read_state()
        self.assertEqual(st2["state"], "READY")
        self.stub_gate([(0, {"approval_id": "should-not-be-called"})])
        step = self.step_from(st2)
        self.assertEqual(step["action"], "need_prompt",
                         "replan 后没有新 prompt 就必须明确停下，而不是复用旧指令")

    def test_no_livelock_same_sha_never_sent_twice(self):
        """连续跑 loop：旧 prompt 被归档后，第二轮不得再把它提交给闸门。"""
        st = self.make_run("APPROVED")
        self.no_window_guard()
        calls = []
        orig = None

        def fake(root, args, env_extra=None, state=""):
            calls.append(list(args))
            if args[0] == "send":
                return 0, {"ok": True, "verdict": "SENT", "reply_count_delta": 1,
                           "before": {"asst_fps": [], "asst_keys": [], "user_fps": []},
                           "signals": ["msg_count"]}
            return 1, {"error_code": "UNEXPECTED", "detail": str(args)[:120]}
        self.patch(orch, "run_gate", fake)
        self.step_from(st)                      # 第一轮：真发
        sends_after_first = [c for c in calls if c[0] == "send"]
        self.assertEqual(len(sends_after_first), 1)
        # 第二轮：不带 --file，从 READY 起
        st2 = self.read_state()
        st2["state"] = "READY"
        orch.save_run_state(self.root, st2)
        step2 = self.step_from(st2)
        self.assertEqual(step2["action"], "need_prompt")
        self.assertEqual([c for c in calls if c[0] == "send"], sends_after_first,
                         "旧 prompt 不能被第二次提交给闸门")

    def test_new_prompt_enters_flow_normally(self):
        st = self.make_run("PAUSED_HUMAN")
        orch.save_run_state(self.root, st)
        orch.cmd_replan(self.args(reason="换方案"))
        pf2 = os.path.join(self.root, "runtime", "wa", "feedback-2.md")
        with open(pf2, "w", encoding="utf-8") as f:
            f.write("第二轮：按新判据 Z 执行\n")
        rc = orch.cmd_loop(self.args(file=pf2, once=True))
        self.assertEqual(rc, 0, rc)
        after = self.read_state()
        self.assertEqual(after["pending"]["prompt_file"], os.path.abspath(pf2))
        self.assertNotEqual(after["pending"]["prompt_sha256"], self.sha)


class TestWriteGateIdempotencyUntouched(PromptWorkspace):
    def test_already_sent_still_detects_same_prompt_same_target(self):
        """本项修复不许削弱 write_gate 的幂等：它仍是最后一道重复防护。"""
        st = self.make_run("APPROVED")
        orch.save_run_state(self.root, st)
        d = os.path.join(self.root, "runs", st["run_id"])
        wg.append_journal(d, {"action": "write_send", "result": "ok",
                              "prompt_sha256": self.sha, "target_url_sha256": "abc123"})
        dry = {"prompt_sha256": self.sha, "target_url_sha256": "abc123"}
        self.assertTrue(wg._already_sent(d, dry), "幂等保护不许退化")
        other = {"prompt_sha256": "deadbeef", "target_url_sha256": "abc123"}
        self.assertFalse(wg._already_sent(d, other))


if __name__ == "__main__":
    unittest.main()
