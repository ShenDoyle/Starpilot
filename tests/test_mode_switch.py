"""执行模式：step（逐步确认）/ auto（无人值守），以及 mid-loop 切换。

用户裁定（2026-10-30）：
  · 两条线（逐步确认 / 直接执行）都要有，中间有情况可以打断重新选；
  · 模式必须能 mid-loop 切换（不是一锤定音）；
  · 缺省保守：没人明确说「直接执行」，就不许替人决定不打扰。

切换必须留痕：事后要能回答「这一轮为什么没停」，否则等于没有纪律。
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import orchestrator as orch  # noqa: E402
import platforms  # noqa: E402
import write_gate as wg  # noqa: E402


def _ns(**kw):
    base = dict(workspace="", run="", goal="g", stage="s1", slug="t",
                max_rounds=0, max_same_error=0, round_timeout=0, send_timeout=0,
                card="", exec_mode="", state="", platform="", by="human", reason="",
                force=False)
    base.update(kw)
    return SimpleNamespace(**base)


def _capture(fn, *a):
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        rc = fn(*a)
    finally:
        sys.stdout = old
    txt = buf.getvalue().strip()
    try:
        return rc, json.loads(txt) if txt else {}
    except Exception:
        return rc, {"raw": txt}


class TestResolveExecMode(unittest.TestCase):
    def test_default_is_step(self):
        self.assertEqual(orch._resolve_exec_mode("", None), "step")

    def test_cli_wins(self):
        card = {"stats": {"exec_mode_code": "step"}}
        self.assertEqual(orch._resolve_exec_mode("auto", card), "auto")

    def test_card_used_when_cli_empty(self):
        self.assertEqual(orch._resolve_exec_mode("", {"stats": {"exec_mode_code": "auto"}}),
                         "auto")

    def test_garbage_cli_falls_back_to_card(self):
        self.assertEqual(orch._resolve_exec_mode("whatever", {"stats": {"exec_mode_code": "auto"}}),
                         "auto")


class TestModeCommand(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="sp-mode-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        rc, out = _capture(orch.cmd_init, _ns(workspace=self.root, exec_mode="auto"))
        self.assertEqual(rc, 0, out)
        self.run_id = wg.read_pointer(self.root)

    def _mode(self, **kw):
        return _capture(orch.cmd_mode, _ns(workspace=self.root, run=self.run_id, **kw))

    def test_init_writes_exec_mode(self):
        st = orch.load_run_state(self.root, self.run_id)
        self.assertEqual(st["exec_mode"], "auto")

    def test_show_returns_current_and_explains(self):
        rc, out = self._mode(set="")
        self.assertEqual(rc, 0)
        self.assertEqual(out["exec_mode"], "auto")
        self.assertIn("无人值守", out["explain"])

    def test_switch_mid_loop_is_recorded(self):
        rc, out = self._mode(set="step", reason="想每轮看一眼")
        self.assertEqual(rc, 0)
        self.assertEqual(out["exec_mode"], "step")
        st = orch.load_run_state(self.root, self.run_id)
        self.assertEqual(len(st["exec_mode_switches"]), 1)
        rec = st["exec_mode_switches"][0]
        self.assertEqual((rec["from"], rec["to"]), ("auto", "step"))
        self.assertEqual(rec["reason"], "想每轮看一眼")

    def test_switch_is_journaled(self):
        self._mode(set="step", reason="r")
        evs = [e for e in wg.read_journal(os.path.join(self.root, "runs", self.run_id))
               if e.get("action") == "exec_mode_switch"]
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["to"], "step")

    def test_switch_back_is_allowed(self):
        self._mode(set="step")
        rc, out = self._mode(set="auto")
        self.assertEqual(rc, 0)
        self.assertEqual(out["exec_mode"], "auto")
        st = orch.load_run_state(self.root, self.run_id)
        self.assertEqual([r["to"] for r in st["exec_mode_switches"]], ["step", "auto"])

    def test_same_mode_is_noop_without_force(self):
        rc, out = self._mode(set="auto")
        self.assertEqual(rc, 0)
        self.assertTrue(out.get("noop"))
        st = orch.load_run_state(self.root, self.run_id)
        self.assertEqual(st["exec_mode_switches"], [])


class TestRoundDoneGatesOnMode(unittest.TestCase):
    """step 每轮停给人看结果；auto 直接进下一轮。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="sp-round-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        _capture(orch.cmd_init, _ns(workspace=self.root))
        self.run_id = wg.read_pointer(self.root)
        self.p = platforms.get("kimi")

    def _run_round_done(self, mode):
        st = orch.load_run_state(self.root, self.run_id)
        st["state"] = "ROUND_DONE"
        st["rounds_used"] = 1
        st["exec_mode"] = mode
        orch.save_run_state(self.root, st, self.run_id)
        # one_step 是返回值而非打印，别用 _capture 解析 stdout
        buf, old = sys.stdout, sys.stdout
        sys.stdout = io.StringIO()
        try:
            step = orch.one_step(self.root, st, self.p, _ns(workspace=self.root))
        finally:
            sys.stdout = buf
        return step, orch.load_run_state(self.root, self.run_id)

    def test_step_mode_pauses_for_review(self):
        step, st = self._run_round_done("step")
        self.assertEqual(step["action"], "round_review_pause")
        self.assertTrue(step["stop"])
        self.assertEqual(st["state"], "PAUSED_HUMAN")
        self.assertEqual(st["last_guard_failure"]["checkpoint"], "round_review")

    def test_auto_mode_continues(self):
        step, st = self._run_round_done("auto")
        self.assertEqual(step["action"], "next_round")
        self.assertEqual(st["state"], "READY")

    def test_auto_still_respects_budget(self):
        st = orch.load_run_state(self.root, self.run_id)
        st["state"] = "ROUND_DONE"
        st["rounds_used"] = int((st["goal"] or {}).get("max_rounds") or 12)
        st["exec_mode"] = "auto"
        orch.save_run_state(self.root, st, self.run_id)
        old = sys.stdout
        sys.stdout = io.StringIO()
        try:
            orch.one_step(self.root, st, self.p, _ns(workspace=self.root))
        finally:
            sys.stdout = old
        st2 = orch.load_run_state(self.root, self.run_id)
        # 无人值守不是无限跑：预算耗尽照样交接
        self.assertEqual(st2["state"], "HANDOFF")


if __name__ == "__main__":
    unittest.main()
