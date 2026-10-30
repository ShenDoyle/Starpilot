#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""编排器补充验收单测（架构评审漏项 + S7 + S8）。

覆盖四件事，都是评审点出的「以前靠自觉、现在必须有断言」：
1. **迁移合法性矩阵**：修补型迁移（把状态往回推）必须声明 actor；
   已转交（HANDOFF）不许回到 READY —— 答案写在代码里，不只写在文档里。
2. **预算口径**：`max_rounds` 数实际发送轮次，失败也消耗；恢复不返还。
3. **恢复不改变 fail_streak**：崩溃既不算失败也不算成功（清零会绕过硬规则 4）。
4. **因果链**：send_id → verify 判定 → 回复文件，一条链能追到底。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import orchestrator as orch  # noqa: E402
import platforms  # noqa: E402
import write_gate as wg  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="sp-orch-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.makedirs(os.path.join(self.root, "state"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "references"), exist_ok=True)
        self.run_id = "20260101T000000-test"
        self.d = os.path.join(self.root, "runs", self.run_id)
        os.makedirs(os.path.join(self.d, "evidence"), exist_ok=True)
        os.makedirs(os.path.join(self.d, "writes"), exist_ok=True)
        os.makedirs(os.path.join(self.d, "replies"), exist_ok=True)
        wg.set_pointer(self.root, self.run_id)

    def make_state(self, state="PAUSED_HUMAN", **extra):
        st = {
            "run_id": self.run_id, "stage_id": "O5", "state": state,
            "created_at": "2026-01-01 00:00:00",
            "goal": dict(orch.DEFAULT_GOAL, text="t"),
            "target_url_sha256": "abc", "platform": "deepseek",
            "round": 0, "rounds_used": 0, "fail_streak": 0, "last_error_class": "",
            "last_guard_failure": None, "state_history": [],
            "pending": {"prompt_file": "", "prompt_sha256": "", "approval_id": ""},
            "last_send": {}, "lessons_candidates": [], "lessons_merged": 0,
            "closed": False, "recovery_count": 0,
        }
        st.update(extra)
        wg.atomic_write_json(os.path.join(self.d, "state.json"), st)
        return st


class TestTransitionMatrix(Base):
    def test_repair_edge_rejects_agent(self):
        st = self.make_state("PAUSED_HUMAN")
        ok = orch.transition(self.root, st, "READY", "agent 想自己恢复", by="agent")
        self.assertFalse(ok, "修补型迁移 PAUSED_HUMAN→READY 不许 agent 自走")
        self.assertEqual(st["state"], "PAUSED_HUMAN")
        evs = [e for e in wg.read_journal(self.d) if e.get("result") == "transition_rejected"]
        self.assertEqual(evs[-1]["error_class"], "actor_not_allowed")

    def test_repair_edge_allows_human(self):
        st = self.make_state("PAUSED_HUMAN")
        self.assertTrue(orch.transition(self.root, st, "READY", "人确认继续", by="human"))
        self.assertEqual(st["state"], "READY")

    def test_repair_edge_allows_recovery(self):
        st = self.make_state("RECOVERING")
        self.assertTrue(orch.transition(self.root, st, "AWAITING_REPLY", "恢复复核",
                                       by="recovery"))

    def test_handoff_cannot_go_back(self):
        st = self.make_state("HANDOFF")
        for to in ("READY", "SENDING", "AWAITING_REPLY", "PAUSED_HUMAN"):
            self.assertFalse(orch.transition(self.root, st, to, "想回去", by="human"),
                             "HANDOFF 之后只允许 CLOSED，%s 必须被拒" % to)
        self.assertTrue(orch.transition(self.root, st, "CLOSED", "收口", by="human"))

    def test_closed_is_terminal(self):
        st = self.make_state("CLOSED")
        self.assertFalse(orch.transition(self.root, st, "READY", "想复活", by="human"))

    def test_all_transitions_are_declared(self):
        """每个合法迁移要么是正向链，要么在 REPAIR_EDGES 里显式声明 actor。"""
        forward = {"INIT": {"BOUND"}, "BOUND": {"PLAN_OK"}, "PLAN_OK": {"READY", "PAUSED_HUMAN"},
                   "READY": {"DRY_RUN_OK", "PAUSED_HUMAN"},
                   "DRY_RUN_OK": {"APPROVED", "PAUSED_HUMAN"},
                   "APPROVED": {"SENDING", "PAUSED_HUMAN"},
                   "SENDING": {"AWAITING_REPLY", "PAUSED_HUMAN", "HANDOFF"},
                   "AWAITING_REPLY": {"REPLY_VERIFIED", "ROUND_DONE", "PAUSED_HUMAN", "HANDOFF"},
                   "REPLY_VERIFIED": {"ROUND_DONE"},
                   "ROUND_DONE": {"READY", "HANDOFF", "CLOSED", "PAUSED_HUMAN"},
                   "PAUSED_HUMAN": {"READY", "AWAITING_REPLY", "CLOSED", "HANDOFF"},
                   "RECOVERING": {"READY", "AWAITING_REPLY", "REPLY_VERIFIED",
                                  "PAUSED_HUMAN", "HANDOFF"},
                   "HANDOFF": {"CLOSED"}, "CLOSED": set()}
        for src, targets in orch.TRANSITIONS.items():
            for t in targets:
                self.assertIn(t, forward[src], "状态表里出现了未在测试里声明的迁移 %s→%s" % (src, t))
        self.assertEqual(set(orch.REPAIR_EDGES),
                         {("PAUSED_HUMAN", "READY"), ("PAUSED_HUMAN", "AWAITING_REPLY"),
                          ("PAUSED_HUMAN", "CLOSED"),
                          ("PAUSED_HUMAN", "HANDOFF"), ("RECOVERING", "READY"),
                          ("RECOVERING", "AWAITING_REPLY"), ("RECOVERING", "REPLY_VERIFIED"),
                          ("RECOVERING", "PAUSED_HUMAN"), ("RECOVERING", "HANDOFF")},
                         "新增/删除修补型迁移必须同步本测试与契约 §2.2")


class TestBudgetLongRun(Base):
    """S8：用纯函数把 12 轮预算跑到边界，不需要浏览器。"""

    def test_twelve_rounds_then_exhausted(self):
        st = self.make_state("ROUND_DONE", rounds_used=0)
        seen = []
        for i in range(12):
            d, info = orch.budget_gate(st)
            seen.append(d)
            self.assertEqual(d, "next", "第 %d 轮不该触顶" % (i + 1))
            st["rounds_used"] = info["used"] + 1
        d, info = orch.budget_gate(st)
        self.assertEqual(d, "exhausted", "第 13 次判定必须触顶")
        self.assertEqual(info["used"], 12)
        self.assertEqual(len(seen), 12)

    def test_failed_sends_consume_budget(self):
        """失败的发送也占预算：预算防「烧轮次」，不是防「烧成功」。"""
        st = self.make_state(rounds_used=11, fail_streak=1)
        self.assertEqual(orch.budget_gate(st)[0], "next")
        st["rounds_used"] = 12
        self.assertEqual(orch.budget_gate(st)[0], "exhausted")

    def test_max_rounds_override(self):
        st = self.make_state(rounds_used=3, goal={"max_rounds": 4, "max_same_error": 2,
                                                  "round_timeout": 60, "send_timeout": 30})
        self.assertEqual(orch.budget_gate(st)[0], "next")
        st["rounds_used"] = 4
        self.assertEqual(orch.budget_gate(st)[0], "exhausted")


class TestRecoverySemantics(Base):
    def test_recovery_does_not_touch_fail_streak(self):
        st = self.make_state("AWAITING_REPLY", fail_streak=1,
                             last_send={"reply_count_before": 0})
        wg.append_journal(self.d, {"action": "write_send", "result": "ok",
                                   "send_id": "sd-1", "approval_id": "ap-1",
                                   "target_url_sha256": "abc", "prompt_sha256": "p1",
                                   "before": {}, "after": {}})
        before = st["fail_streak"]
        orch.do_resume(self.root, st, platforms.get("deepseek"),
                       _Args(state="", workspace=self.root))
        st2 = orch.load_run_state(self.root, self.run_id)
        self.assertEqual(st2["fail_streak"], before,
                         "恢复既不清零也不加一（清零会绕过硬规则 4）")
        self.assertEqual(st2["recovery_count"], 1, "恢复次数要能被审计")


class TestTrace(Base):
    def test_trace_links_send_to_verify_and_reply(self):
        self.make_state("ROUND_DONE")
        wg.append_journal(self.d, {"action": "write_send", "result": "ok",
                                   "send_id": "sd-abc", "approval_id": "ap-1",
                                   "round_id": 1, "signals": ["msg_count"],
                                   "reply_count_delta": 1, "effective_delta": 1,
                                   "error_class": ""})
        wg.append_journal(self.d, {"action": "write_verify", "send_id": "sd-abc",
                                   "approval_id": "ap-1", "verdict": "VERIFIED",
                                   "result": "ok"})
        wg.atomic_write_text(os.path.join(self.d, "replies", "round-001.md"), "回复原文")
        out = json.loads(_capture(orch.cmd_audit, _Args(
            run=self.run_id, workspace=self.root, state="", trace=True)))
        self.assertIn("trace", out)
        row = out["trace"][0]
        self.assertEqual(row["send_id"], "sd-abc")
        self.assertEqual(row["verify_verdict"], "VERIFIED")
        self.assertEqual(row["reply_file"], os.path.join("replies", "round-001.md"))
        self.assertEqual(row["effective_delta"], 1)

    def test_trace_marks_old_events_without_send_id(self):
        self.make_state("ROUND_DONE")
        wg.append_journal(self.d, {"action": "write_send", "result": "ok",
                                   "approval_id": "ap-old",
                                   "target_url_sha256": "abc", "prompt_sha256": "p"})
        chain = orch.build_trace(self.d, wg.read_journal(self.d))
        self.assertIn("无 send_id", chain[0]["send_id"], "旧事件要标出来，不能假装有")
        self.assertEqual(chain[0]["verify_verdict"], "(未核验)")


class TestCardGate(Base):
    def test_init_refuses_unready_card(self):
        card = os.path.join(self.root, "card.md")
        wg.atomic_write_text(card, (
            "# 需求卡片\n\n## 基本信息\n\n| 项 | 内容 |\n|---|---|\n"
            "| 项目名 | 星舵 |\n| 一句话目标 | 让 Agent 跑闭环 |\n"
            "| 原始往来记录 | docs/requirements/t-transcript.md |\n\n"
            "## 一、已明确\n\n| # | 要求（原话） | 落地方式 | 验收怎么看 |\n|---|---|---|---|\n"
            "| 1 | 要能审计 | journal | grep |\n\n"
            "## 二、未明确\n\n| # | 待确认点 | 方案 A | 方案 B | 不同答案的影响 |\n"
            "|---|---|---|---|---|\n| 1 | 用哪个平台 | Kimi | DeepSeek | 选择器不同 |\n\n"
            "## 五、确认记录\n\n| 轮次 | 日期 | 用户回复 | 确认状态 |\n|---|---|---|---|\n"
            "| 1 | 2026-10-29 | 先这样 | 部分确认（不算通过） |\n"))
        before = sorted(os.listdir(os.path.join(self.root, "runs")))
        rc = orch.cmd_init(_Args(workspace=self.root, goal="t", stage="O5", slug="card",
                                 max_rounds=0, max_same_error=0, round_timeout=0,
                                 send_timeout=0, run="", card=card))
        self.assertEqual(rc, 1, "未明确项没清空就不许建 run")
        after = sorted(os.listdir(os.path.join(self.root, "runs")))
        self.assertEqual(before, after, "被拒时不该留下半成品 run")

    def _ready_card(self, path, tail=""):
        wg.atomic_write_text(path, (
            "# 需求卡片\n\n## 基本信息\n\n| 项 | 内容 |\n|---|---|\n"
            "| 项目名 | 星舵 |\n| 一句话目标 | 让 Agent 跑闭环 |\n"
            "| 原始往来记录 | docs/requirements/t-transcript.md |\n"
            "| 执行模式 | 直接执行 |\n"
            "| 授权范围 | 整个任务 |\n"
            "| 无人值守方案 | 选改动最小的；不装依赖；外部挂了换备用；连败 2 次停 |\n\n"
            "## 一、已明确\n\n| # | 要求 | 落地 | 验收 |\n|---|---|---|---|\n"
            "| 1 | 要能审计 | journal | grep |\n\n"
            "## 二、未明确\n\n| # | 待确认 | A | B | 影响 |\n|---|---|---|---|---|\n\n"
            "## 三、有冲突\n\n| # | 冲突 | A | B | 谁优先 |\n|---|---|---|---|---|\n\n"
            "## 四、不做\n\n| # | 不做 | 原因 |\n|---|---|---|\n| 1 | 不做 X | 越界 |\n\n"
            "## 五、确认记录\n\n| 轮次 | 日期 | 用户回复 | 确认状态 |\n|---|---|---|---|\n"
            "| 1 | 2026-10-29 | 开干 | 已确认 |\n" + tail))

    def test_stray_table_in_confirmation_section_is_ignored_not_trusted(self):
        """确认节里混进一张「技术裁定表」时：用真正的确认行，裁定行记 stray_rows。

        这是个真踩到的坑：加裁定表后闸门报 NOT_CONFIRMED，detail 里是个文件路径。
        修的方向不是放宽，而是**按固定词表识别确认状态**：认不出就当不是确认行。
        """
        card = os.path.join(self.root, "card2.md")
        self._ready_card(card, tail=(
            "\n### 执行期裁定\n\n| 轮 | 来源 | 内容 | 原文 |\n|---|---|---|---|\n"
            "| 1 | DeepSeek | 四项致命 | `runtime/x.md` |\n"))
        out = json.loads(_capture(orch.cmd_card, _Args(file=card)))
        self.assertTrue(out["ok"], out.get("violations"))
        self.assertEqual(out["stats"]["confirmations"], 1)
        self.assertEqual(out["stats"]["stray_rows"], 1, "裁定行要被挑出来报，不许静默吞掉")

    def test_only_stray_rows_means_no_confirmation(self):
        card = os.path.join(self.root, "card3.md")
        wg.atomic_write_text(card, (
            "# 需求卡片\n\n## 一、已明确\n\n| # | 要求 | 落地 | 验收 |\n|---|---|---|---|\n"
            "| 1 | x | y | z |\n\n"
            "## 五、确认记录\n\n| 轮 | 来源 | 内容 | 原文 |\n|---|---|---|---|\n"
            "| 1 | DeepSeek | 裁定 | `a.md` |\n"))
        out = json.loads(_capture(orch.cmd_card, _Args(file=card)))
        self.assertFalse(out["ok"], "没有真正的确认行：不许当成已确认")
        self.assertIn("NO_CONFIRMATION", {v["code"] for v in out["violations"]})

    def test_card_command_reports_violations(self):
        card = os.path.join(self.root, "empty.md")
        wg.atomic_write_text(card, "# 需求卡片\n\n## 一、已明确\n")
        out = json.loads(_capture(orch.cmd_card, _Args(file=card)))
        self.assertFalse(out["ok"])
        codes = {v["code"] for v in out["violations"]}
        self.assertIn("CLARIFIED_EMPTY", codes)


class TestWorkspaceStateAndUrlGuard(Base):
    """2026-10-29 端到端实测抓到的两个缺陷的回归测试。

    1. 闸门是**子进程**：不显式传 `--state` 就会读子进程 cwd 的 state
       （= 另一个工作区/另一个对话的绑定）。
    2. 地址守卫必须比**活页面**，比 state 副本等于没比。
    """

    def test_gate_reads_workspace_state_not_cwd(self):
        other = os.path.join(self.root, "other-ws")
        os.makedirs(os.path.join(other, "runs", self.run_id, "writes"), exist_ok=True)
        # 另一个 workspace 绑的是「未实测平台」，cwd（本仓库根）绑的是 kimi
        wg.atomic_write_json(os.path.join(other, ".wa-state.json"),
                             {"url": "https://x/chat/1", "platform": "claude"})
        aid = "ap-x"
        wg.atomic_write_json(os.path.join(other, "runs", self.run_id, "writes",
                                          "%s.dry.json" % aid),
                             {"approval_id": aid, "platform": "claude",
                              "target_url_sha256": "h", "prompt_sha256": "p"})
        wg.atomic_write_json(os.path.join(other, "runs", self.run_id, "writes",
                                          "%s.approval.json" % aid),
                             {"approval_id": aid, "expires_at": 9e12,
                              "target_url_sha256": "h", "prompt_sha256": "p"})
        os.environ["STARPILOT_WRITE_MODE"] = "single_write"
        self.addCleanup(os.environ.pop, "STARPILOT_WRITE_MODE", None)
        out = _capture(wg.cmd_send, _Args(approval=aid, run=self.run_id,
                                          workspace=other, state="", platform="",
                                          round=1, force=False, send_id=""))
        res = json.loads(out)
        self.assertEqual(res["error_code"], "WRITE_BLOCKED_PLATFORM_UNVERIFIED",
                         "必须按 workspace 的 state 判定（claude 未实测），"
                         "不是按 cwd 的 kimi")

    def test_wa_state_path_is_workspace_relative(self):
        self.assertEqual(wg.wa_state_path("/tmp/ws"), os.path.join("/tmp/ws", ".wa-state.json"))
        self.assertEqual(wg.wa_state_path("/tmp/ws", "x.json"), "x.json")

    def test_url_drift_is_caught(self):
        snap = {"url": "https://www.kimi.com/chat/abc", "editor": True,
                "editor_len": 0, "modal": False, "raw": {"editor": True}}
        ok, why = wg.guard_ok(snap, "https://chat.deepseek.com/a/chat/s/xyz")
        self.assertFalse(ok, "页面在 kimi、绑定是 deepseek：必须拒绝")
        self.assertIn("地址漂移", why)

    def test_url_query_is_ignored(self):
        snap = {"url": "https://www.kimi.com/chat/abc?chat_enter_method=home",
                "editor": True, "editor_len": 0, "modal": False,
                "raw": {"editor": True}}
        ok, why = wg.guard_ok(snap, "https://www.kimi.com/chat/abc")
        self.assertTrue(ok, "query 差异不算漂移：%s" % why)


class TestLessonFormat(Base):
    """lesson 条目是「一条一行」——字段里夹换行会把格式悄悄撑破。

    2026-10-29 实测踩到：失败详情里带多行 JSON（bsk 的 cdp_failed 原文），
    原样写进 `- [..][..]` 后条目变成十几行，按行解析的体检工具当场把续行当垃圾。
    坏法非常安静：只有 `lessons_audit` 会喊，而它不在主链路上。
    """

    def test_multiline_field_is_collapsed_to_one_line(self):
        line = orch._lesson_line(
            lesson_id="L-T-1", date_utc="2026-10-29", stage="O5",
            source_run_ref="runs/x", platform="gemini",
            symptom='原生输入失败：{\n  "code": "cdp_failed",\n  "message": "x"\n}',
            dom_signal="", fix="f", verify_cmd="v", trigger="t", rollback="rb",
            blocking="false", status="merged")
        self.assertEqual(len(line.splitlines()), 1, "一条 lesson 必须只有一行")
        self.assertNotIn("\n", line)

    def test_generated_line_round_trips_through_the_auditor(self):
        """写出来的行必须能被体检工具原样读回——格式和解析器是一对，不能各走各的。"""
        import lessons_audit as la
        line = orch._lesson_line(
            lesson_id="L-T-2", date_utc="2026-10-29", stage="O5",
            source_run_ref="runs/x", platform="gemini",
            symptom="多行\n症状\n在这里", dom_signal="", fix="f", verify_cmd="v",
            trigger="t", rollback="rb", blocking="false", status="open")
        e = la.parse_line(line)
        self.assertIsNotNone(e, "生成的条目体检工具读不出来：%s" % line[:120])
        self.assertEqual(e["lesson_id"], "L-T-2")
        self.assertEqual(e["status"], "open")
        self.assertNotIn("\n", e["symptom"])


class TestVerdictEffect(Base):
    """第 2 轮裁定 2：fail_streak 只统计「有结论的失败」。

    三值语义：失败 +1 / 成功清零 / 无结论不动。判据只许走 `apply_verdict_effect`，
    别处直接 `+= 1` 或 `= 0` 就是漂移的开始（契约 §2.4）。
    """

    def test_every_verdict_maps_to_three_valued_effect(self):
        for v, eff in orch.VERDICT_EFFECT.items():
            self.assertIn(eff, ("clear", 0, 1), "verdict %s 映射非法：%r" % (v, eff))
        self.assertEqual(orch.VERDICT_EFFECT["INCONCLUSIVE_NOT_SUCCESS"], 0)
        self.assertEqual(orch.VERDICT_EFFECT["INCONCLUSIVE_COLLISION"], 0)
        self.assertEqual(orch.VERDICT_EFFECT["SENT"], "clear")
        self.assertEqual(orch.VERDICT_EFFECT["NOT_SENT"], 1)

    def test_unknown_verdict_counts_as_failure_not_silence(self):
        self.assertEqual(orch.verdict_effect("我编的"), 1,
                         "查不到就是与契约脱节：宁可停，不可静默放过")

    def test_apply_moves_streak_per_three_valued_rule(self):
        st = {"fail_streak": 0}
        orch.apply_verdict_effect(st, "reply_timeout")
        self.assertEqual(st["fail_streak"], 1)
        orch.apply_verdict_effect(st, "INCONCLUSIVE_NOT_SUCCESS")
        self.assertEqual(st["fail_streak"], 1, "无结论不动 streak")
        orch.apply_verdict_effect(st, "SENT")
        self.assertEqual(st["fail_streak"], 0, "成功清零（打断连续失败）")
        self.assertEqual(st["last_verdict_effect"], "clear")

    def test_no_naked_fail_streak_writes(self):
        """源码守卫：fail_streak 的写入只许出现在唯一入口里。"""
        src = os.path.join(ROOT, "scripts", "orchestrator.py")
        with open(src, encoding="utf-8") as f:
            lines = f.readlines()
        offenders = []
        inside = False
        for i, ln in enumerate(lines, 1):
            if ln.startswith("def "):
                inside = ln.startswith("def apply_verdict_effect")
            if inside or "fail_streak" not in ln:
                continue
            if "=" in ln and ("+ 1" in ln or "= 0" in ln):
                offenders.append((i, ln.strip()))
        self.assertEqual(offenders, [], "fail_streak 只许经 apply_verdict_effect 修改：%s"
                         % offenders)


class TestWaitReplyAnchor(Base):
    """2026-10-29 端到端实测缺陷：回复已经渲染在页面上，但**助手气泡计数没涨**
    （虚拟列表把旧气泡卸载了），按计数判「回复到了没」会白等到超时。

    实测现场：发送前 reply_count=2，回复 2597 字落到页面后 reply_count 仍是 2，
    老逻辑判 reply_timeout；锚点口径（新指纹出现 / 末条指纹变化）当场判成功。
    """

    def _patch_read_state(self, seq):
        calls = {"i": 0}

        def fake(_p, _sid):
            i = min(calls["i"], len(seq) - 1)
            calls["i"] += 1
            return True, seq[i]

        orig = orch.wa.read_state
        orch.wa.read_state = fake
        self.addCleanup(setattr, orch.wa, "read_state", orig)
        return calls

    def test_reply_detected_when_count_is_flat(self):
        before = {"reply_count": 2,
                  "asst_fps": ["架构评审：Starpilot", "连通性测试回复"],
                  "last_asst_fp": "连通性测试回复"}
        cur = {"reply_count": 2, "generating": False, "last_len": 2597,
               "asst_fps": ["架构评审：Starpilot", "第1轮裁定：先说结论"],
               "last_asst_fp": "第1轮裁定：先说结论"}
        self._patch_read_state([cur])
        res = orch.wait_for_reply({}, "sid", 2, timeout=10, interval=0, before=before)
        self.assertTrue(res["ok"], "计数持平但有新指纹：必须判回复到了，不能判超时")

    def test_count_fallback_when_anchors_unavailable(self):
        # 老状态文件没有 asst_fps → 锚点不可信 → 退回计数口径（宁可粗，不能不判）
        cur = {"reply_count": 3, "generating": False, "last_len": 100, "asst_fps": []}
        self._patch_read_state([cur])
        res = orch.wait_for_reply({}, "sid", 2, timeout=10, interval=0, before={})
        self.assertTrue(res["ok"], "无指纹时按计数 +1 判定")

    def test_resume_requires_send_id_and_inconclusive_verdict(self):
        """修补边要证据，不只是「人是被允许的 actor」（第 2 轮裁定 1）。

        三条验收：
          · 没有 send_id → 拒（证不了「确实发过」）
          · verdict=SENT  → 拒（已判清却没等到回复 = 真失败，该 HANDOFF/重发）
          · verdict=INCONCLUSIVE_NOT_SUCCESS → 放行（确实发过、确实没判清 → 继续等）
        """
        # 1) 无 send_id
        st = self.make_state("PAUSED_HUMAN")
        ok = orch.transition(self.root, st, "AWAITING_REPLY", "r", {"action": "unblock"},
                             by="human")
        self.assertFalse(ok, "没有 send_id 不许继续等")
        self.assertEqual(st["state"], "PAUSED_HUMAN")
        self.assertEqual((st["last_guard_failure"] or {}).get("checkpoint"),
                         "repair_precondition")

        # 2) send_id 在，但 verdict 已判清（SENT）
        st2 = self.make_state("PAUSED_HUMAN",
                              last_send={"send_id": "sd-1", "verdict": "SENT"})
        ok2 = orch.transition(self.root, st2, "AWAITING_REPLY", "r", {"action": "unblock"},
                              by="human")
        self.assertFalse(ok2, "已判清却没等到回复 = 真失败，不许原地继续等")

        # 3) send_id + INCONCLUSIVE → 放行
        st3 = self.make_state("PAUSED_HUMAN",
                              last_send={"send_id": "sd-2",
                                         "verdict": "INCONCLUSIVE_NOT_SUCCESS"})
        ok3 = orch.transition(self.root, st3, "AWAITING_REPLY", "r", {"action": "unblock"},
                              by="human")
        self.assertTrue(ok3, "确实发过 + 确实没判清：允许继续等")
        self.assertEqual(st3["state"], "AWAITING_REPLY")

    def test_agent_still_cannot_walk_this_edge(self):
        # 就算证据齐全，agent 也不能自己往前推（不许绕过写闸门）
        st = self.make_state("PAUSED_HUMAN",
                             last_send={"send_id": "sd-3",
                                        "verdict": "INCONCLUSIVE_NOT_SUCCESS"})
        ok = orch.transition(self.root, st, "AWAITING_REPLY", "r", {"action": "x"},
                             by="agent")
        self.assertFalse(ok)
        self.assertEqual(st["state"], "PAUSED_HUMAN")


def _capture(fn, args):
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(args)
    return buf.getvalue()


class _Args(object):
    def __init__(self, **kw):
        self.__dict__.update(kw)


if __name__ == "__main__":
    unittest.main()
