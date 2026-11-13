#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PROJECT-03-R3 测试：输入能力边界固化 + Intent 幂等语义（组 A–F）。

覆盖（R3 任务书 §R3-E）：
- A 能力声明：INPUT_MODES 四模式独立声明，trusted_* 与 dom_synthesized
  的 is_trusted 语义分野；bridge trusted 路径静态存在；reclaim 不进后端。
- B input_mode 校验：未声明模式 SP-BE-002 拒绝（不静默降级、不绕 Gate）；
  trusted/dom_synthesized 双路透传与 verification 分层。
- C Gate 约束：ownership 失效 / 未配置 probe 时 trusted 输入同样被拒
  （能力不足不产生 Gate 旁路）。
- D Intent 语义：COMPLETED(SENT) 原样重放 SP-ACT-008；新业务身份放行；
  NOT_SENT 合法重试；UNKNOWN 任何新身份不可绕；unlock 不可改判 NOT_SENT；
  异业务对象隔离。
- E 多进程竞争相同 Intent：唯一索引裁决，恰好一胜。
- F 真实 Ego（一次性隔离空间，自清理）：trusted 输入真实管线 + 夹具页
  受控复验 + reclaim_and_destroy 生命周期（handOff 后终局销毁）。

纪律：不删 R2 测试、不放宽原断言；真实测试不用 mock 替代（mock 只用于
组 B 的桥接桩，真实路径由组 F 覆盖）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(HERE, "fixtures"))
sys.path.insert(0, HERE)

import actionledger  # noqa: E402
import ego_backend as eb  # noqa: E402
import ego_r2_harness as H  # noqa: E402
import execution_gate  # noqa: E402
import p2_plane  # noqa: E402
import procidentity  # noqa: E402
import sp_errors as SP  # noqa: E402


# ============================================================ 组 A：能力声明
class TestR3CapabilityDeclarations(unittest.TestCase):
    """R3-C：四种输入模式独立声明，绝不互相冒充。"""

    def test_a1_input_modes_declared(self):
        ims = eb.input_modes()
        self.assertEqual(
            set(ims.keys()),
            {"dom_synthesized", "trusted_text_input",
             "trusted_keyboard_input", "trusted_mouse_input"})
        self.assertFalse(ims["dom_synthesized"]["is_trusted"])
        for k in ("trusted_text_input", "trusted_keyboard_input",
                  "trusted_mouse_input"):
            self.assertTrue(ims[k]["is_trusted"], k)
            self.assertTrue(ims[k]["supported"], k)
            # 实测证据必须留痕（R3-B localhost 夹具，2026-11-13）
            self.assertIn("R3-B", ims[k]["verified"], k)

    def test_a2_ops_accept_only_declared_modes(self):
        for op in ("fill", "click", "press"):
            cap = eb.CAPABILITIES[op]
            self.assertEqual(cap["input_mode"], "dom_synthesized", op)
            self.assertEqual(cap["accepted_input_modes"],
                             ("dom_synthesized", "trusted"), op)
            self.assertTrue(cap["requires_execution_gate"], op)

    def test_a3_bridge_trusted_path_static(self):
        # bridge 必须实现 trusted 真实管线（SDK page.* 一级 API）且保留合成路径
        with open(os.path.join(ROOT, "scripts", "ego", "ego_bridge.mjs"),
                  encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('im === "trusted"', src)
        self.assertIn("page.fill(a.selector, a.value)", src)
        self.assertIn("page.click(a.selector)", src)
        self.assertIn('input_mode: "dom-synthesized"', src)
        # closedSpace 校验（2026-11-13 非托管标签签名）
        self.assertIn("closed_space", src)

    def test_a4_backend_has_no_reclaim(self):
        # R2 f4 铁律的 R3 复核：reclaim 只允许存在于测试夹具层
        with open(os.path.join(ROOT, "scripts", "ego_backend.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        filtered = (src.lower().replace("不自主 reclaim", "")
                    .replace("no reclaim", "")
                    .replace("自主 reclaim", "")
                    .replace("不 reclaim", ""))
        self.assertNotIn("reclaim", filtered,
                         "backend must not implement reclaim capability")
        with open(os.path.join(HERE, "fixtures", "ego_r2_harness.py"),
                  encoding="utf-8") as fh:
            hsrc = fh.read()
        self.assertIn("reclaim_and_destroy", hsrc)

    def test_a5_accessors_are_copies(self):
        c1 = eb.capabilities()
        c1["fill"]["input_mode"] = "tampered"
        self.assertEqual(eb.CAPABILITIES["fill"]["input_mode"],
                         "dom_synthesized")
        m1 = eb.input_modes()
        m1["dom_synthesized"]["is_trusted"] = True
        self.assertFalse(eb.INPUT_MODES["dom_synthesized"]["is_trusted"])


# ==================================================== 组 B：input_mode 校验
class TestR3InputModeValidation(unittest.TestCase):
    """未声明模式拒绝（SP-BE-002）；双模式透传；verification 四层分层。

    本组用桥接桩（monkeypatch _run_bridge）隔离校验逻辑本身；
    真实管线行为由组 F 真实 Ego 覆盖。
    """

    def setUp(self):
        self.calls = []

        def fake_bridge(op, args, timeout_s=60, env_extra=None):
            self.calls.append({"op": op, "args": dict(args)})
            if op == "page_viewport":
                # R3-S：trusted 环境探针在桩内返回「窗口已渲染」
                return {"ok": True, "data": {"innerW": 1009, "innerH": 974,
                                             "vis": "visible"}}
            return {"ok": True, "data": {
                "identity": {"url": "http://fixture/", "title": "t",
                             "readyState": "complete", "bodyLen": 50},
                "applied": args.get("value"),
                "filled": args.get("value"),
                "dom_changed": True,
            }}
        self._orig = eb._run_bridge
        eb._run_bridge = fake_bridge
        self.addCleanup(setattr, eb, "_run_bridge", self._orig)

    def test_b1_undeclared_mode_refused(self):
        r = eb.page_op("fill", "sid", "p1", selector="#i", value="x",
                       input_mode="bogus")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_BE_002)
        self.assertEqual(r["data"]["accepted"],
                         ["dom_synthesized", "trusted"])
        self.assertEqual(self.calls, [], "bridge must not be called")

    def test_b2_undeclared_op_refused(self):
        r = eb.page_op("no_such_op", "sid", "p1")
        self.assertEqual(r["code"], SP.SP_BE_001)
        r2 = eb.page_op("cdp", "sid", "p1")
        self.assertEqual(r2["code"], SP.SP_BE_002,
                         "SDK 支持但未声明暴露的能力不开放（SP-BE-001/002）")

    def test_b3_trusted_passthrough_and_layers(self):
        r = eb.page_op("fill", "sid", "p1", selector="#i",
                       value="trusted-v", input_mode="trusted")
        self.assertTrue(r["ok"], r)
        d = r["data"]
        self.assertEqual(d["input_mode"], "trusted")
        self.assertEqual(d["verification"],
                         {"call_ok": True, "dom_changed": True,
                          "page_state_confirmed": True,
                          "trusted_pipeline": True})
        fill_calls = [c for c in self.calls if c["op"] == "fill"]
        self.assertTrue(fill_calls, "trusted fill 必须实际派发到桥接")
        self.assertEqual(fill_calls[0]["args"]["input_mode"], "trusted")

    def test_b4_default_is_dom_synthesized(self):
        r = eb.page_op("fill", "sid", "p1", selector="#i", value="s")
        d = r["data"]
        self.assertEqual(d["input_mode"], "dom_synthesized")
        self.assertFalse(d["verification"]["trusted_pipeline"])
        self.assertEqual(self.calls[0]["args"]["input_mode"],
                         "dom_synthesized")

    def test_b5_click_press_modes(self):
        r = eb.page_op("click", "sid", "p1", selector="#b",
                       input_mode="trusted")
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["input_mode"], "trusted")
        r2 = eb.page_op("press", "sid", "p1", key="Enter",
                        input_mode="trusted")
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["data"]["input_mode"], "trusted")


# ==================================================== 组 C：Gate 输入模式约束
class TestR3GateConstraints(unittest.TestCase):
    """trusted 输入同样必须过 Gate；ownership 不足时拒绝且不绕行。"""

    def _gate(self, ownership):
        d = tempfile.mkdtemp(prefix="sp-r3-gate-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        ledger = actionledger.ActionLedger(os.path.join(d, "l.sqlite"))
        self.addCleanup(ledger.close)
        gate = execution_gate.ExecutionGate(
            os.path.join(d, "g.lock"), ledger, None,
            ownership_probe=(lambda sid: ownership) if ownership else None,
            resource="r3-gate", plane=p2_plane.make_plane(d))
        return gate, ledger

    def _reserve(self, ledger, task_id):
        # P2-2-4：fill 属副作用（SIDE_EFFECT_OPS），reserve 必须带
        # business_target（SP-ACT-010）；这里以「测试页面|任务」为业务对象。
        r = ledger.reserve(task_id, {"op": "fill", "mode": "trusted"},
                           budget_units=5, executor_id="r3-exec",
                           business_target="r3|sid|%s" % task_id)
        assert r["ok"], r
        return r["data"]["action_id"]

    def test_c1_ownership_user_blocks_trusted_dispatch(self):
        gate, ledger = self._gate("user")
        ctx = {"executor_id": "r3-exec", "run_id": "r3"}
        ac = gate.acquire(ctx)
        assert ac["ok"], ac
        ctx["fencing_generation"] = ac["data"]["fencing_generation"]
        aid = self._reserve(ledger, "t-c1")
        r = gate.dispatch(ctx, "fill",
                          lambda op, args, c: {"ok": True, "unknown": False},
                          {"input_mode": "trusted"},
                          taskspace_id="sid", action_id=aid)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_GATE_008)
        # 副作用入口未打开：dispatch_count 仍为 0
        self.assertEqual(ledger.get(aid)["dispatch_count"], 0)

    def test_c2_probe_unconfigured_fails_closed(self):
        gate, ledger = self._gate(None)
        ctx = {"executor_id": "r3-exec", "run_id": "r3"}
        ac = gate.acquire(ctx)
        assert ac["ok"], ac
        ctx["fencing_generation"] = ac["data"]["fencing_generation"]
        aid = self._reserve(ledger, "t-c2")
        r = gate.dispatch(ctx, "fill",
                          lambda op, args, c: {"ok": True, "unknown": False},
                          {"input_mode": "trusted"},
                          taskspace_id="sid", action_id=aid)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_GATE_008)

    def test_c3_trusted_mode_dispatches_through_gate_normally(self):
        gate, ledger = self._gate("agent")
        ctx = {"executor_id": "r3-exec", "run_id": "r3"}
        ac = gate.acquire(ctx)
        assert ac["ok"], ac
        gen = ac["data"]["fencing_generation"]
        ctx["fencing_generation"] = gen
        aid = self._reserve(ledger, "t-c3")
        seen = {}

        def backend(op, args, c):
            seen["op"], seen["args"], seen["gen"] = op, args, c.get(
                "fencing_generation")
            return {"ok": True, "unknown": False,
                    "result": {"input_mode": args.get("input_mode")}}
        r = gate.dispatch(ctx, "fill", backend, {"input_mode": "trusted"},
                          taskspace_id="sid", action_id=aid)
        self.assertTrue(r["ok"], r)
        self.assertEqual(seen["args"]["input_mode"], "trusted")
        self.assertEqual(seen["gen"], gen)
        self.assertEqual(ledger.get(aid)["state"], "DISPATCHED")


# ================================================ 组 D：Intent 幂等语义（D1~D6）
class TestR3IntentSemantics(unittest.TestCase):
    """R3-D 状态与业务语义矩阵的接口级强制。"""

    def setUp(self):
        d = tempfile.mkdtemp(prefix="sp-r3-led-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        self.L = actionledger.ActionLedger(os.path.join(d, "l.sqlite"))
        self.addCleanup(self.L.close)

    INTENT = {"action": "send_reply", "target": "conv-42", "body": "hi"}

    def test_d2_sent_intent_replay_needs_new_identity(self):
        r1 = self.L.reserve("t1", self.INTENT, budget_units=5)
        assert r1["ok"], r1
        aid = r1["data"]["action_id"]
        assert self.L.mark_dispatched(aid, 1, "exec")["ok"]
        assert self.L.commit_outcome(aid, "SENT", "needle seen",
                                     expected_fencing=1)["ok"]
        # 换 task_id / 换 seq 原样重放 → SP-ACT-008（新身份 != 新 task_id）
        r2 = self.L.reserve("t2-different-task", self.INTENT, seq=7,
                            budget_units=5)
        self.assertFalse(r2["ok"])
        self.assertEqual(r2["code"], SP.SP_ACT_008)
        # 合法路径：payload 带业务区分字段（round）→ 新 intent_key → 放行
        intent2 = dict(self.INTENT, round=2)
        r3 = self.L.reserve("t3", intent2, budget_units=5)
        self.assertTrue(r3["ok"], r3)
        self.assertNotEqual(r2["data"]["intent_key"], r3["data"]["intent_key"])

    def test_d3_not_sent_proven_replay_allowed(self):
        r1 = self.L.reserve("t1", self.INTENT, budget_units=5)
        aid = r1["data"]["action_id"]
        # RESERVED（未派发）→ NOT_SENT：唯一合法路径
        c = self.L.commit_outcome(aid, "NOT_SENT",
                                  "gate refused before dispatch")
        self.assertTrue(c["ok"], c)
        # 已证明无副作用 → 同一业务意图允许重新预占（新 task）
        r2 = self.L.reserve("t2", self.INTENT, budget_units=5)
        self.assertTrue(r2["ok"], r2)
        kinds = [e["kind"] for e in self.L.events(r2["data"]["action_id"])]
        self.assertIn("replay_after_not_sent", kinds)

    def test_d4_unknown_cannot_be_bypassed_by_any_identity(self):
        r1 = self.L.reserve("t1", self.INTENT, budget_units=5)
        aid = r1["data"]["action_id"]
        assert self.L.mark_dispatched(aid, 1, "exec")["ok"]
        # 模拟崩溃：DISPATCHED 无 outcome → SEND_UNKNOWN + anti_replay_locked
        rec = self.L.recover_unknown()
        self.assertEqual(rec["data"]["count"], 1)
        # 新 run / 新 task_id / 新 seq 全部绕不过
        for tid, seq in (("t2", 0), ("t3", 9), ("t4", 0)):
            r = self.L.reserve(tid, self.INTENT, seq=seq, run_id="other-run",
                               budget_units=5)
            self.assertFalse(r["ok"], (tid, seq))
            self.assertEqual(r["code"], SP.SP_ACT_002)
        # UNKNOWN 不能直接改判 NOT_SENT（unlock 也救不了）
        self.assertFalse(
            self.L.commit_outcome(aid, "NOT_SENT", "wishful thinking",
                                  unlock={"kind": "manual",
                                          "operator": "x"})["ok"])
        # 无 unlock 的 UNKNOWN → COMPLETED 拒绝
        self.assertEqual(
            self.L.commit_outcome(aid, "SENT", "no unlock")["code"],
            SP.SP_ACT_003)
        # G6-HOTFIX（2026-11-13）：即便"看起来合法"的 unlock 形态也一律拒绝。
        # 旧实现会放行 {"kind":"page_evidence",...} 并释放 L-biz；该分支已移除。
        for fake in (
            {"kind": "page_evidence", "page_generation": "pg1",
             "observation_id": "obs1"},
            {"kind": "manual", "operator": "operator-fan", "reason": "confirmed"},
            {"adjudication_id": "0" * 64},          # 伪造裁决引用（无数据源）
            {"kind": "x"},                           # 最简绕过形态
        ):
            u = self.L.commit_outcome(aid, "SENT", "page evidence", unlock=fake)
            self.assertFalse(u["ok"], fake)
            self.assertEqual(u["code"], SP.SP_ACT_003, fake)
            self.assertEqual(u["data"]["reason"], "unknown_unlock_disabled", fake)
        # 状态与锁均未变（H6）
        row = self.L.get(aid)
        self.assertEqual(row["state"], "SEND_UNKNOWN")
        self.assertEqual(row["anti_replay_locked"], 1)
        self.assertEqual(row["result_tag"], None)

    def test_d5_same_action_different_targets_isolated(self):
        # 不同业务对象的相同动作名：不误判重复（作用域 = 业务字段）
        a = {"action": "click", "target": "obj-A"}
        b = {"action": "click", "target": "obj-B"}
        r1 = self.L.reserve("t1", a, budget_units=5)
        r2 = self.L.reserve("t2", b, budget_units=5)
        self.assertTrue(r1["ok"] and r2["ok"])
        self.assertNotEqual(r1["data"]["intent_key"], r2["data"]["intent_key"])

    def test_d6_active_intent_collision_unchanged(self):
        # D6 兼容性：R2 的活跃意图防重语义不变
        r1 = self.L.reserve("t1", self.INTENT, budget_units=5)
        r2 = self.L.reserve("t2", self.INTENT, budget_units=5)
        self.assertFalse(r2["ok"])
        self.assertEqual(r2["code"], SP.SP_ACT_002)


# ================================================ 组 E：多进程竞争相同 Intent
class TestR3IntentMultiprocess(unittest.TestCase):
    """两个独立 OS 进程同时预占同一 intent：唯一索引裁决恰好一胜。"""

    def test_e1_mp_same_intent_race(self):
        d = tempfile.mkdtemp(prefix="sp-r3-mp-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        db = os.path.join(d, "l.sqlite")
        worker = os.path.join(d, "worker.py")
        with open(worker, "w", encoding="utf-8") as f:
            f.write(
                "import json,sys,os,time,random\n"
                "sys.path.insert(0, os.path.join(r'%s', 'scripts'))\n"
                "import actionledger\n"
                "db, out = sys.argv[1], sys.argv[2]\n"
                "time.sleep(random.random() * 0.3)\n"
                "L = actionledger.ActionLedger(db)\n"
                "r = L.reserve('task-' + str(os.getpid()), "
                "{'action': 'send', 'target': 'same-object'}, "
                "budget_units=5)\n"
                "json.dump({'ok': r['ok'], 'code': r['code'], "
                "'pid': os.getpid()}, open(out, 'w'))\n"
                % ROOT.replace("\\", "/"))
        env = dict(os.environ, PYTHONUTF8="1")
        procs, outs = [], []
        for i in range(2):
            o = os.path.join(d, "out%d.json" % i)
            outs.append(o)
            procs.append(subprocess.Popen(
                [sys.executable, worker, db, o],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, cwd=d))
        for p, o in zip(procs, outs):
            pass
        for p in procs:
            p.wait(timeout=60)
        err = [p.stderr.read().decode("utf-8", "replace")[-400:] for p in procs]
        self.assertTrue(all(os.path.exists(o) for o in outs),
                        "worker must leave result JSON; stderr=%s" % err)
        results = [json.load(open(o, encoding="utf-8")) for o in outs]
        ok_count = sum(1 for r in results if r["ok"])
        self.assertEqual(ok_count, 1, results)
        loser = [r for r in results if not r["ok"]][0]
        self.assertEqual(loser["code"], SP.SP_ACT_002)
        self.assertEqual(len({r["pid"] for r in results}), 2,
                         "must be two distinct OS processes")


# ==================================== 组 F：真实 Ego（一次性隔离空间，自清理）
class TestR3LiveTrustedInput(unittest.TestCase):
    """trusted 真实管线（localhost 夹具）+ reclaim 生命周期。真实 Ego。"""

    def setUp(self):
        self.server = H.TestPageServer()
        self.addCleanup(self.server.close)
        self.sp = H.EgoSpace("r3t")
        self.addCleanup(self._cleanup_space)

    def _cleanup_space(self):
        # R3-E 资源回收铁律：真实 Ego 用例结束必须自清理（含委托态）
        if getattr(self.sp, "finished", False):
            return
        try:
            if (not self.sp.handed_off
                    and eb.probe_ownership(self.sp.space_id,
                                           retries=0) == "agent"):
                self.sp.finish_disposable(keep=[])
            else:
                self.sp.reclaim_and_destroy()
        except Exception:
            pass  # 清理失败不掩盖用例结果，但绝不留窗口不报——由
                  # reclaim 内部的 closedSpace 断言兜底

    def _ready(self, label="p1"):
        # 先导航到夹具页（R2 gate_flow 同款），再等 identity 就绪
        g = eb.page_op("goto", self.sp.space_id, label, url=self.server.url)
        assert g["ok"], g
        return H.wait_page_ready(self.sp.space_id, label)

    def _viewport_ready(self) -> None:
        """真实输入的环境前置：页面必须有真实渲染 viewport。

        2026-11-13 实测根因：viewport 0×0 = Ego 窗口不在屏幕上渲染
        （窗口级状态，非页面模式/SDK 限制），此时 SDK 真实输入 API
        （CDP Input 驱动）超时 —— 对应 R3-B 输入能力矩阵「viewport 0×0」
        行结论：真实输入受阻，DOM 合成不受影响。环境不满足 → 跳过并
        标注原因（这是环境前置，不是放宽断言；真实能力结论见 R3-B
        localhost 夹具实测，当时窗口在屏，isTrusted=true 全链路取证）。
        """
        r = eb.page_viewport(self.sp.space_id, "p1")
        if not r["ok"]:
            self.skipTest("viewport probe failed: %s" % r.get("evidence"))
        d = r.get("data") or {}
        if not (d.get("innerW") or 0) > 0:
            self.skipTest(
                "SKIPPED_env_window_hidden: viewport %sx%s — Ego window not "
                "rendered on screen; real-input APIs blocked (R3-B matrix)"
                % (d.get("innerW"), d.get("innerH")))

    def test_f1_trusted_fill_real_pipeline(self):
        self._viewport_ready()
        ident = self._ready()
        r = eb.page_op("fill", self.sp.space_id, "p1", selector="#inp",
                       value="r3-trusted-fill", input_mode="trusted")
        self.assertTrue(r["ok"], r)
        d = r["data"]
        self.assertEqual(d["input_mode"], "trusted")
        v = d["verification"]
        self.assertTrue(v["call_ok"] and v["dom_changed"]
                        and v["page_state_confirmed"]
                        and v["trusted_pipeline"])
        self.assertEqual(d["applied"], "r3-trusted-fill")
        # DOM 真实更新（夹具页镜像读回：oninput → #mirror，innerText 可观测）
        chk = eb.page_op("read_text", self.sp.space_id, "p1")
        self.assertIn("VAL:r3-trusted-fill",
                      (chk.get("data") or {}).get("text", ""))

    def test_f2_trusted_click_real_pipeline(self):
        self._viewport_ready()
        self._ready()
        r = eb.page_op("click", self.sp.space_id, "p1", selector="#btn",
                       input_mode="trusted")
        self.assertTrue(r["ok"], r)
        d = r["data"]
        self.assertEqual(d["input_mode"], "trusted")
        self.assertTrue(d["dom_changed"], "真实点击必须产生页面状态变化")
        chk = eb.page_op("read_text", self.sp.space_id, "p1")
        self.assertIn("CLICKED", (chk.get("data") or {}).get("text", ""))

    def test_f3_default_remains_dom_synthesized(self):
        self._ready()
        r = eb.page_op("fill", self.sp.space_id, "p1", selector="#inp",
                       value="r3-synth")
        d = r["data"]
        self.assertEqual(d["input_mode"], "dom_synthesized")
        self.assertFalse(d["verification"]["trusted_pipeline"])
        self.assertEqual(d["applied"], "r3-synth")

    def test_f4_reclaim_after_handoff_closes_space(self):
        self._ready()
        r = eb.hand_off(self.sp.space_id)
        self.assertTrue(r["ok"], r)
        self.sp.handed_off = True
        # 终局自清理：委托态 → takeOver → 收编标签 → finish，closedSpace=true
        rr = self.sp.reclaim_and_destroy()
        self.assertTrue(rr["ok"], rr)
        self.assertTrue((rr.get("data") or {}).get("closed_space"), rr)
        self.sp.finished = True
        # 传播延迟后全局列表不再包含该空间（容忍 30s）
        import time as _t
        gone = False
        for _ in range(6):
            _t.sleep(5)
            ls = eb.list_spaces()
            ids = {s.get("spaceId") for s in
                   (ls.get("data") or {}).get("spaces", [])}
            if self.sp.space_id not in ids:
                gone = True
                break
        self.assertTrue(gone, "handOff 后 reclaim 空间必须从全局列表消失")


# ================================================ 组 U：UNKNOWN × 新业务身份（R3-S GPT 终审 §3.4）
class TestR3BusinessTargetRisk(unittest.TestCase):
    """业务对象风险约束 U1~U5：Intent 防重 ≠ 业务对象授权。

    GPT 终审裁定：同一业务对象的 UNKNOWN 未决时，换 intent_key 的新意图
    不得自动获得执行授权；任何后续执行必须有独立、可审计的授权依据。
    """

    def setUp(self):
        d = tempfile.mkdtemp(prefix="sp-r3-bt-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        self.L = actionledger.ActionLedger(os.path.join(d, "l.sqlite"))
        self.addCleanup(self.L.close)

    def _reserve(self, payload, task_id="t1", **kw):
        return self.L.reserve(task_id, payload, budget_units=5, **kw)

    def _to_unknown(self, aid):
        r = self.L.mark_dispatched(aid, 1, "u-exec")
        assert r["ok"], r
        r = self.L.recover_unknown()
        assert r["ok"], r

    def test_u1_unknown_same_target_new_round_refused(self):
        # U1：同 target，round-1 UNKNOWN，round-2 换 round → 不得自动授权
        p1 = {"action": "send_reply", "target": "conv-123", "scope": "round-1"}
        r1 = self._reserve(p1, business_target="conv-123")
        self.assertTrue(r1["ok"], r1)
        self._to_unknown(r1["data"]["action_id"])
        p2 = {"action": "send_reply", "target": "conv-123", "scope": "round-2"}
        r2 = self._reserve(p2, business_target="conv-123")
        self.assertFalse(r2["ok"], r2)
        self.assertEqual(r2["code"], SP.SP_ACT_009, r2)
        self.assertEqual(r2["data"]["holder_state"], "SEND_UNKNOWN")

    def test_u2_new_task_and_scope_needs_business_check(self):
        # U2（P2-0 终审 T09 语义修订）：同 target，换 task_id 和 scope →
        # 必须经过业务风险检查；authorization_ref 只证明授权关系，
        # **不构成 L-biz 绕过**——授权/风险裁定/锁释放三动作分离（P2-1-A），
        # 释放只能走 unlock/裁决流程
        p1 = {"action": "send_reply", "target": "conv-123", "scope": "s1"}
        r1 = self._reserve(p1, task_id="t-a", business_target="conv-123")
        self._to_unknown(r1["data"]["action_id"])
        p2 = {"action": "send_reply", "target": "conv-123", "scope": "s2"}
        r2 = self.L.reserve("t-b-different-task", p2, budget_units=5,
                            business_target="conv-123")
        self.assertFalse(r2["ok"])
        self.assertEqual(r2["code"], SP.SP_ACT_009)
        r3 = self.L.reserve("t-b-different-task", p2, budget_units=5,
                            business_target="conv-123",
                            authorization_ref="audit-20261113-001")
        self.assertFalse(r3["ok"], r3)
        self.assertEqual(r3["code"], SP.SP_ACT_009, r3)
        # 授权引用仅作审计记录随拒绝证据返回；原 UNKNOWN action 原样保留
        self.assertTrue(r3["data"].get("authorization_ref_recorded") is True)
        self.assertEqual(r3["data"]["holder_task_id"], "t-a")
        self.assertIsNone(self.L.get(r3.get("data", {}).get("action_id")))

    def test_u3_other_target_not_globally_frozen(self):
        # U3：不同 target 的第二个业务对象正常执行（不被无差别全局冻结）
        p1 = {"action": "send_reply", "target": "conv-123", "scope": "s1"}
        r1 = self._reserve(p1, business_target="conv-123")
        self._to_unknown(r1["data"]["action_id"])
        p2 = {"action": "send_reply", "target": "conv-999", "scope": "s1"}
        r2 = self._reserve(p2, task_id="t-other", business_target="conv-999")
        self.assertTrue(r2["ok"], r2)

    def test_u4_unknown_not_releasable_new_intent_still_blocked(self):
        # U4（G6-HOTFIX 2026-11-13 改写）：UNKNOWN 不再可被调用方终结，
        # 因此 L-biz 持续占用，同对象新业务意图被拦。
        # 语义变更原因：原用例验证"manual unlock → SENT 后新意图独立判断"，
        # 该路径已按 GPT 裁定（A4 / H1-H3）关闭——无可信裁决源时一律 fail-closed。
        p1 = {"action": "send_reply", "target": "conv-123", "scope": "s1"}
        r1 = self._reserve(p1, business_target="conv-123")
        aid1 = r1["data"]["action_id"]
        self._to_unknown(aid1)
        self.assertEqual(self.L.business_risk("conv-123")["count"], 1)
        r = self.L.commit_outcome(
            aid1, "SENT",
            unlock={"kind": "manual", "operator": "operator-fan",
                    "reason": "page evidence confirmed delivered"})
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["code"], SP.SP_ACT_003)
        # H6：解锁失败不得释放 L-biz
        self.assertEqual(self.L.business_risk("conv-123")["count"], 1)
        p2 = {"action": "send_reply", "target": "conv-123", "scope": "s2"}
        r2 = self._reserve(p2, task_id="t-after", business_target="conv-123")
        self.assertFalse(r2["ok"], r2)
        self.assertEqual(r2["code"], SP.SP_ACT_009, r2)

    def test_u5_replay_after_blocked_unlock_still_sp_act_008(self):
        # U5（G6-HOTFIX 改写）：解锁被拒后，原 intent 原样重放仍被拒。
        p1 = {"action": "send_reply", "target": "conv-123", "scope": "s1"}
        r1 = self._reserve(p1, task_id="t-orig", business_target="conv-123")
        aid1 = r1["data"]["action_id"]
        self._to_unknown(aid1)
        r = self.L.commit_outcome(
            aid1, "SENT",
            unlock={"kind": "manual", "operator": "operator-fan",
                    "reason": "confirmed"})
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["code"], SP.SP_ACT_003)
        p_same = {"action": "send_reply", "target": "conv-123", "scope": "s1"}
        r2 = self.L.reserve("t-new-task", p_same, budget_units=5)
        self.assertFalse(r2["ok"])
        # UNKNOWN 仍活跃 → L-int 先命中（SP-ACT-002）。原用例断言 008 是因为
        # 旧实现解锁后状态变 COMPLETED、intent 不再活跃；该路径已按 G6-HOTFIX 关闭。
        self.assertEqual(r2["code"], SP.SP_ACT_002, r2)
        self.assertEqual(r2["data"]["holder_state"], "SEND_UNKNOWN")


# ================================================ 组 S：R3-S 补充固化（GPT 终审 §4.1/§4.4）
class TestR3TrustedEnvAvailability(unittest.TestCase):
    """trusted 输入运行环境可用性：supported ≠ available，0×0 显式拒绝。"""

    def test_s1_capability_availability_fields(self):
        modes = eb.input_modes()
        for m in ("trusted_text_input", "trusted_keyboard_input",
                  "trusted_mouse_input"):
            self.assertTrue(modes[m]["environment_dependent"], m)
            self.assertEqual(modes[m]["availability_probe"], "page_viewport")
            self.assertEqual(modes[m]["unavailable_error"], SP.SP_BE_007)
        self.assertFalse(modes["dom_synthesized"]["environment_dependent"])

    def test_s2_trusted_refused_on_hidden_window(self):
        calls = []

        def fake_bridge(op, args, timeout_s=60, env_extra=None):
            calls.append(op)
            if op == "page_viewport":
                return {"ok": True, "data": {"innerW": 0, "innerH": 0,
                                             "vis": "visible"}}
            raise AssertionError("input op must not be dispatched after "
                                 "env probe refusal")

        orig = eb._run_bridge
        eb._run_bridge = fake_bridge
        self.addCleanup(setattr, eb, "_run_bridge", orig)
        r = eb.page_op("fill", "sid", "p1", selector="#i", value="x",
                       input_mode="trusted")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_BE_007, r)
        self.assertEqual(r["data"]["reason"], "VIEWPORT_UNAVAILABLE")
        self.assertEqual(r["data"]["availability"], "unavailable")
        self.assertEqual(calls, ["page_viewport"],
                         "探针拒绝后不得派发输入 op")

    def test_s3_trusted_refused_when_probe_fails(self):
        calls = []

        def fake_bridge(op, args, timeout_s=60, env_extra=None):
            calls.append(op)
            if op == "page_viewport":
                return {"ok": False, "error": "probe died"}
            raise AssertionError("unreachable")

        orig = eb._run_bridge
        eb._run_bridge = fake_bridge
        self.addCleanup(setattr, eb, "_run_bridge", orig)
        r = eb.page_op("click", "sid", "p1", selector="#b",
                       input_mode="trusted")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_BE_007, r)
        self.assertEqual(r["data"]["reason"], "PROBE_FAILED")

    def test_s4_dom_synthesized_skips_probe(self):
        calls = []

        def fake_bridge(op, args, timeout_s=60, env_extra=None):
            calls.append(op)
            return {"ok": True, "data": {
                "identity": {"readyState": "complete"},
                "applied": args.get("value"), "dom_changed": True,
                "input_mode": "dom_synthesized"}}

        orig = eb._run_bridge
        eb._run_bridge = fake_bridge
        self.addCleanup(setattr, eb, "_run_bridge", orig)
        r = eb.page_op("fill", "sid", "p1", selector="#i", value="x")
        self.assertTrue(r["ok"], r)
        self.assertNotIn("page_viewport", calls,
                         "合成模式不做环境探针（行为不变）")


class TestR3ReclaimIsolation(unittest.TestCase):
    """R3-S §4.4：reclaim 隔离性静态核验——后端/Gate/业务层零 reclaim。"""

    def test_backend_and_gate_have_no_reclaim(self):
        # 与 R2 f4 同一过滤口径：中文注释里「不 reclaim / no reclaim」属合法提及
        def filtered(src):
            return (src.lower().replace("不自主 reclaim", "")
                    .replace("no reclaim", "")
                    .replace("自主 reclaim", "")
                    .replace("不 reclaim", ""))
        for name in ("ego_backend.py", "execution_gate.py"):
            src = open(os.path.join(ROOT, "scripts", name),
                       encoding="utf-8").read()
            self.assertNotIn("reclaim", filtered(src),
                             "%s 不得出现 reclaim 能力" % name)

    def test_bridge_reclaim_only_reachable_from_fixtures(self):
        # bridge 有 reclaim_space op，但其后端能力面不可达：
        # scripts 下除 bridge 自身（op 实现层）外不得有 reclaim（含注释过滤）
        def filtered(src):
            return (src.lower().replace("不自主 reclaim", "")
                    .replace("no reclaim", "")
                    .replace("自主 reclaim", "")
                    .replace("不 reclaim", ""))
        hit = []
        for base, _dirs, files in os.walk(os.path.join(ROOT, "scripts")):
            for f in files:
                if f.endswith((".py", ".mjs")):
                    p = os.path.join(base, f)
                    s = open(p, encoding="utf-8").read()
                    if "reclaim" in filtered(s):
                        hit.append(os.path.relpath(p, ROOT).replace("\\", "/"))
        allowed = {"scripts/ego/ego_bridge.mjs",   # op 实现层（能力面不可达）
                   "scripts/sp_errors.py",          # SP-LIFE-005 错误码锚点
                   "scripts/winlease.py"}           # 无关域（实例回收注释）
        unexpected = [h for h in hit if h not in allowed]
        self.assertEqual(unexpected, [],
                         "scripts 下 reclaim 仅允许 bridge op + 错误码锚点")


if __name__ == "__main__":
    unittest.main()
