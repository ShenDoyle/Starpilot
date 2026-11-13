#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PROJECT-03-R2 真实 Ego 测试（组 A–F，全部走 ego-browser nodejs 原生通道）。

纪律：禁 BSK 驱动 Ego；handOff/finish 类用例一律一次性隔离 TaskSpace；
不自主 reclaim；hard stop 不自动重试；优先 localhost 隔离测试页。
"""
from __future__ import annotations

import json
import os
import re
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

import ego_backend as eb  # noqa: E402
import ego_r2_harness as H  # noqa: E402
import pageidentity  # noqa: E402
import actionledger  # noqa: E402
import procidentity  # noqa: E402
import statelock  # noqa: E402
import gate_lint  # noqa: E402
import sp_errors as SP  # noqa: E402
import p2_plane  # noqa: E402


class EgoR2Case(unittest.TestCase):
    """公共夹具：localhost 页面 + Gate 全栈（真实 ownership / observation）。"""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="sp-ego-r2-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.server = H.TestPageServer()
        self.addCleanup(self.server.close)
        self.lock = os.path.join(self.d, "gate.lock")
        self.ledger = actionledger.ActionLedger(os.path.join(self.d, "ledger.sqlite"))
        self.addCleanup(self.ledger.close)
        self.pi = pageidentity.PageIdentityManager(os.path.join(self.d, "pages.json"))
        # P2-2-4：副作用派发必须带 SecurityPlane（Envelope/Marker/DELEGATED）。
        # 测试用临时密钥与临时 ops 目录，零入库。
        self.plane = p2_plane.make_plane(self.d, worker_id="w-r2")
        self.gate = eb.make_gate(self.lock, self.ledger, self.pi,
                                 resource="r2-gate", plane=self.plane)
        self.executor = procidentity.executor_id(run_id="r2test")
        self.bridge_calls = 0

    def tearDown(self):
        pass

    # ---- Gate 全栈辅助 ----
    def new_space(self, prefix="r2test"):
        sp = H.EgoSpace(prefix)
        self.addCleanup(self._disposable_cleanup, sp)
        return sp

    def _disposable_cleanup(self, sp):
        # R3-E 资源回收铁律（2026-11-13 用户拍板）：用例结束必须自清理，
        # 不给桌面留「等你接管」的窗口——包括 handOff/keep-finish 造成的
        # 委托态空间与非托管标签。仅限本夹具自建的一次性空间；
        # 用户自己的空间（id 0 主等）绝不触碰。
        if getattr(sp, "finished", False):
            return
        if (not sp.handed_off
                and eb.probe_ownership(sp.space_id, retries=0) == "agent"):
            sp.finish_disposable(keep=[])
        else:
            sp.reclaim_and_destroy()

    def gate_flow(self, space, label, op, args, task_id="t-r2",
                  intent=None, expect_page_ready=True, exec_id=None):
        """标准受控流：reserve → acquire → bind → dispatch（真实 Ego）。

        P2-2-4：副作用操作必须携带 business_target（SP-ACT-010 可信分类表），
        否则 dispatch 在 ⓪ 步 fail-closed。这里以「空间|页面」作为业务对象，
        语义等同于"该隔离页面"。
        """
        exec_id = exec_id or self.executor
        url = self.server.url
        ident = H.wait_page_ready(space.space_id, label)
        ac = self.gate.acquire({"executor_id": exec_id, "run_id": "r2test"})
        assert ac["ok"], ac
        gen = ac["data"]["fencing_generation"]
        b = self.pi.bind("sp%s|%s" % (space.space_id, label), ident,
                         exec_id, gen, "bind")
        pg = b["page_generation"]
        obs = eb.build_observation(space.space_id, label, pg, exec_id, gen, ident)
        r = self.ledger.reserve(task_id, intent or {"op": op, "url": url},
                                budget_units=10, executor_id=exec_id,
                                business_target="space:%s|%s|%s|%s"
                                % (space.space_id, label, task_id, op))
        assert r["ok"], r
        aid = r["data"]["action_id"]
        ctx = {"executor_id": exec_id, "run_id": "r2test",
               "fencing_generation": gen, "page_generation": pg}
        backend = lambda o, a, c: eb.page_op(o, space.space_id, label, **a)
        out = self.gate.dispatch(ctx, op, backend, args, taskspace_id=space.space_id,
                                 page_key="sp%s|%s" % (space.space_id, label),
                                 identity_tuple=ident, observation=obs,
                                 action_id=aid)
        return {"ac": ac, "gen": gen, "pg": pg, "obs": obs, "aid": aid,
                "ctx": ctx, "out": out, "ident": ident}

    # ============================================================ 组 A
    def test_a1_capability_discovery(self):
        caps = eb.capabilities()
        self.assertIn("goto", caps)
        self.assertTrue(caps["goto"]["supported"])
        self.assertTrue(caps["goto"]["requires_execution_gate"])
        self.assertFalse(caps["cdp"]["supported"],
                         "SDK 存在但未声明 → 不暴露（SP-BE-001/002）")
        # 未声明能力拒绝
        r = eb.page_op("cdp", 0, "p1")
        self.assertEqual(r["code"], SP.SP_BE_002, r)
        r2 = eb.page_op("nonexistent_op", 0, "p1")
        self.assertEqual(r2["code"], SP.SP_BE_001)

    def test_a2_create_isolated_space(self):
        sp = self.new_space("a2")
        self.assertEqual(sp.ownership, "agent", sp.info())
        r = eb.probe_ownership(sp.space_id)
        self.assertEqual(r, "agent", "真实全局 ownership 必须是 agent")

    def test_a3_list_spaces_real_ownership(self):
        r = eb.list_spaces()
        self.assertTrue(r["ok"])
        spaces = {s["spaceId"]: s for s in r["data"]["spaces"]}
        # 用户主空间（space 0）ownership=user —— 真实全局态
        self.assertIn(0, spaces)
        self.assertEqual(spaces[0]["ownership"], "user")
        # probe 与全局列表一致
        self.assertEqual(eb.probe_ownership(0), "user")

    def test_a4_explicit_inherit_same_space(self):
        sp = self.new_space("a4")
        eb.page_op("goto", sp.space_id, "p1", url=self.server.url)
        i1 = H.wait_page_ready(sp.space_id, "p1")
        i2 = H.wait_page_ready(sp.space_id, "p1")  # 新进程再次显式继承
        self.assertEqual(i1["timeOrigin"], i2["timeOrigin"],
                         "显式继承同一 TaskSpace：页面身份一致")
        self.assertEqual(i1["marker"], i2["marker"])

    def test_a5_undeclared_capability_refused(self):
        # 伪造桥接返回未知 op → bridge 层就拒绝
        r = eb._run_bridge("totally_unknown", {})
        self.assertFalse(r["ok"])
        self.assertIn("unknown op", r.get("error", ""))

    # ============================================================ 组 B
    def test_b1_page_lookup_and_identity(self):
        sp = self.new_space("b1")
        eb.page_op("goto", sp.space_id, "p1", url=self.server.url)
        r = eb.page_exists(sp.space_id, "p1")
        self.assertTrue(r["ok"] and r["data"]["exists"])
        i = H.wait_page_ready(sp.space_id, "p1")
        # harness 页面 #m 的文本是 "GEN-{n}"（页面代次标记）
        self.assertRegex(str(i["marker"]), r"^GEN-\d+$")
        self.assertTrue(str(i["url"]).startswith("http://127.0.0.1:"))

    def test_b2_first_bind_generation(self):
        sp = self.new_space("b2")
        eb.page_op("goto", sp.space_id, "p1", url=self.server.url)
        ident = H.wait_page_ready(sp.space_id, "p1")
        b = self.pi.bind("sp%s|p1" % sp.space_id, ident, self.executor, 1, "bind")
        self.assertEqual(b["generation"], 1)
        v = self.pi.verify("sp%s|p1" % sp.space_id, ident, b["page_generation"],
                           self.executor, 1)
        self.assertTrue(v["ok"])

    def test_b3_navigate_updates_generation_rejects_old(self):
        sp = self.new_space("b3")
        eb.page_op("goto", sp.space_id, "p1", url=self.server.url)
        i1 = H.wait_page_ready(sp.space_id, "p1")
        k = "sp%s|p1" % sp.space_id
        b1 = self.pi.bind(k, i1, self.executor, 1, "bind")
        # 导航（真实 Ego 页面操作）
        self.server.bump_gen()
        eb.page_op("goto", sp.space_id, "p1", url=self.server.url)
        i2 = H.wait_page_ready(sp.space_id, "p1")
        b2 = self.pi.bind(k, i2, self.executor, 1, "navigate")
        self.assertEqual(b2["generation"], 2)
        self.assertNotEqual(b1["fingerprint"], b2["fingerprint"])
        # 旧 generation / 旧 observation 拒绝
        v = self.pi.verify(k, i1, b1["page_generation"], self.executor, 1)
        self.assertFalse(v["ok"])
        obs1 = eb.build_observation(sp.space_id, "p1", b1["page_generation"],
                                    self.executor, 1, i1)
        rv = eb.verify_observation(sp.space_id, "p1", obs1)
        self.assertEqual(rv["code"], SP.SP_GATE_009, rv)

    def test_b4_same_fingerprint_rebind_new_generation(self):
        sp = self.new_space("b4")
        eb.page_op("goto", sp.space_id, "p1", url=self.server.url)
        i = H.wait_page_ready(sp.space_id, "p1")
        k = "sp%s|p1" % sp.space_id
        b1 = self.pi.bind(k, i, self.executor, 1, "bind")
        self.pi.invalidate(k, "test")
        b2 = self.pi.bind(k, i, self.executor, 1, "rebind_after_invalidate")
        self.assertEqual(b2["generation"], 2)
        self.assertEqual(b1["fingerprint"], b2["fingerprint"],
                         "指纹相同（真实同一页面）")
        self.assertTrue(b2["same_fingerprint_as_prev"])
        v = self.pi.verify(k, i, b1["page_generation"], self.executor, 1)
        self.assertFalse(v["ok"], "旧 generation 不得复活")

    def test_b5_old_observation_rejected_after_navigation(self):
        sp = self.new_space("b5")
        eb.page_op("goto", sp.space_id, "p1", url=self.server.url)
        i1 = H.wait_page_ready(sp.space_id, "p1")
        obs1 = eb.build_observation(sp.space_id, "p1", "pg-1", self.executor, 1, i1)
        self.server.bump_gen()
        eb.page_op("goto", sp.space_id, "p1", url=self.server.url)
        rv = eb.verify_observation(sp.space_id, "p1", obs1)
        self.assertEqual(rv["code"], SP.SP_GATE_009, "真实页面变化 → 旧 obs 失效")

    def test_b6_new_process_rereads_live_page(self):
        sp = self.new_space("b6")
        eb.page_op("goto", sp.space_id, "p1", url=self.server.url)
        i1 = H.wait_page_ready(sp.space_id, "p1")
        self.server.bump_gen()
        eb.page_op("goto", sp.space_id, "p1", url=self.server.url)
        i2 = H.wait_page_ready(sp.space_id, "p1")  # 又是全新进程
        self.assertNotEqual(i1["marker"], i2["marker"],
                            "每次桥接都是新进程，身份必须重新实采")
        self.assertNotEqual(i1["timeOrigin"], i2["timeOrigin"])

    # ============================================================ 组 C
    def test_c1_agent_owned_allows_safe_action(self):
        sp = self.new_space("c1")
        eb.page_op("goto", sp.space_id, "p1", url=self.server.url)
        flow = self.gate_flow(sp, "p1", "click", {"selector": "#btn"},
                              task_id="t-c1")
        self.assertTrue(flow["out"]["ok"], flow["out"])
        # 实现如实性：input_mode 必须标注 dom_synthesized（不冒充真实输入）。
        # R3-C 规范化：结果字段携带 CAPABILITIES 声明的规范形式（下划线；
        # 旧断言的桥接原始标签是连字符 "dom-synthesized"，语义不变），
        # 并加严：synth 路径必须 trusted_pipeline=False。
        outcome = flow["out"]["data"]["outcome"]
        self.assertEqual(outcome["data"].get("input_mode"), "dom_synthesized",
                         outcome)
        self.assertFalse(outcome["data"]["verification"]["trusted_pipeline"],
                         "合成路径不得宣称真实管线")
        # commit tag 由 outcome 决定：outcome.ok 才允许 SENT
        self.assertTrue(outcome["ok"], "click 必须真实生效于页面")
        # dispatch/commit 分离：dispatch 成功后账本 DISPATCHED，锁仍持有
        row0 = self.ledger.get(flow["aid"])
        self.assertEqual(row0["state"], "DISPATCHED", row0)
        # commit 入口（fencing 复核）→ COMPLETED；锁覆盖到落盘后才 release
        lease = flow["ac"]["data"]["lease"]
        c = self.gate.commit(flow["ctx"], flow["aid"], "SENT",
                             evidence="c1 click delivered on isolated page")
        self.assertTrue(c["ok"], c)
        self.gate.release(lease)
        row = self.ledger.get(flow["aid"])
        self.assertEqual(row["state"], "COMPLETED")
        self.assertEqual(row["result_tag"], "SENT")
        self.assertEqual(row["dispatch_count"], 1)
        text = eb.page_op("read_text", sp.space_id, "p1")
        self.assertIn("CLICKED-", text["data"]["text"], "真实点击生效")

    def test_c2_user_owned_space_rejects(self):
        # 用户主空间（space 0，ownership=user）→ Gate 拒绝
        flow = self.gate_flow_space0()
        self.assertFalse(flow["out"]["ok"])
        self.assertEqual(flow["out"]["code"], SP.SP_GATE_008, flow["out"])

    def gate_flow_space0(self):
        """对用户空间 0 构造完整 ctx 并 dispatch —— 必须被门 2 拦下。"""
        ident = {"url": "about:blank", "title": "x", "marker": "",
                 "timeOrigin": 1.0}
        ac = self.gate.acquire({"executor_id": self.executor, "run_id": "r2test"})
        gen = ac["data"]["fencing_generation"]
        r = self.ledger.reserve("t-c2", {"op": "fill"}, budget_units=10,
                                business_target="space:0|p1|t-c2|fill")
        aid = r["data"]["action_id"]
        ctx = {"executor_id": self.executor, "run_id": "r2test",
               "fencing_generation": gen, "page_generation": "pg-1"}
        out = self.gate.dispatch(ctx, "fill", lambda o, a, c: eb.page_op(o, 0, "p1", **a),
                                 {"selector": "#x", "value": "v"},
                                 taskspace_id=0, page_key="sp0|p1",
                                 identity_tuple=ident, observation=None,
                                 action_id=aid)
        return {"out": out, "aid": aid, "ac": ac, "gen": gen}

    def test_c3_agent_delegated_to_user_rejects_and_no_reclaim(self):
        sp = self.new_space("c3")           # 一次性隔离空间
        eb.page_op("goto", sp.space_id, "p1", url=self.server.url)
        r = eb.hand_off(sp.space_id)
        self.assertTrue(r["ok"], r)
        sp.handed_off = True
        self.assertEqual((r["data"].get("globalAfter") or {}).get("ownership"),
                         "agentDelegatedToUser", r["data"])
        self.assertEqual(eb.probe_ownership(sp.space_id), "agentDelegatedToUser")
        # 副作用拒绝（C3）+ 不自主 reclaim（C7）+ 不自动重试（C8）
        calls = {"n": 0}
        orig = eb._run_bridge
        def counting(op, args, timeout_s=60, env_extra=None):
            calls["n"] += 1
            return orig(op, args, timeout_s)
        eb._run_bridge = counting
        try:
            ident = {"url": "about:blank", "title": "x", "marker": "",
                     "timeOrigin": 1.0}
            ac = self.gate.acquire({"executor_id": self.executor, "run_id": "r2"})
            r2 = self.ledger.reserve("t-c3", {"op": "click"}, budget_units=10,
                                     business_target="space:%s|p1|t-c3|click"
                                     % sp.space_id)
            ctx = {"executor_id": self.executor, "run_id": "r2",
                   "fencing_generation": ac["data"]["fencing_generation"],
                   "page_generation": "pg-1"}
            out = self.gate.dispatch(ctx, "click",
                                     lambda o, a, c: eb.page_op(o, sp.space_id, "p1", **a),
                                     {"selector": "#btn"}, taskspace_id=sp.space_id,
                                     page_key="sp%s|p1" % sp.space_id,
                                     identity_tuple=ident, observation=None,
                                     action_id=r2["data"]["action_id"])
            self.assertFalse(out["ok"])
            self.assertIn(out["code"], (SP.SP_GATE_008, SP.SP_BE_005), out)
            n_after_first = calls["n"]
            self.assertLessEqual(n_after_first, 3,
                                 "probe 短重试有限；绝无 reclaim/重试轰炸")
        finally:
            eb._run_bridge = orig

    def test_c4_list_failure_fail_closed(self):
        orig = eb._run_bridge
        eb._run_bridge = lambda *a, **k: {"ok": False, "error": "simulated outage"}
        try:
            self.assertEqual(eb.probe_ownership(63), "unknown",
                             "查询失败必须 fail-closed")
        finally:
            eb._run_bridge = orig
        flow = self.gate_flow_space0()  # Gate 用同一 probe 逻辑
        # 恢复后 gate 正常判定（user-owned 拒绝），证明不是缓存
        self.assertEqual(flow["out"]["code"], SP.SP_GATE_008)

    def test_c5_global_not_local_cache(self):
        # 全局列表返回 user，即使“本地 task”自称 agent —— 必须用全局
        sp = self.new_space("c5")
        orig = eb._run_bridge
        def forged(op, args, timeout_s=60, env_extra=None):
            r = orig(op, args, timeout_s)
            if op == "space_probe" and r.get("ok"):
                for hit in (r["data"].get("global"), r["data"].get("space")):
                    if hit:
                        hit["ownership"] = "user"   # 模拟全局态= user
            return r
        eb._run_bridge = forged
        try:
            self.assertEqual(eb.probe_ownership(sp.space_id), "user")
        finally:
            eb._run_bridge = orig
        self.assertEqual(eb.probe_ownership(sp.space_id), "agent",
                         "mock 移除后回到真实全局态")

    def test_c6_hand_off_invalidates_observation(self):
        sp = self.new_space("c6")
        eb.page_op("goto", sp.space_id, "p1", url=self.server.url)
        i = H.wait_page_ready(sp.space_id, "p1")
        obs = eb.build_observation(sp.space_id, "p1", "pg-1", self.executor, 1, i)
        r = eb.hand_off(sp.space_id)
        sp.handed_off = True
        self.assertTrue(r["ok"])
        rv = eb.verify_observation(sp.space_id, "p1", obs)
        self.assertFalse(rv["ok"], "handOff 后旧 observation 必须失效")
        self.assertIn(rv["code"], (SP.SP_GATE_010, SP.SP_BE_005, SP.SP_GATE_009,
                                   SP.SP_BE_003), rv)

    # C7/C8 已并入 C3（不 reclaim / 不重试断言）

    # ============================================================ 组 D
    def test_d1_d2_real_read_and_interaction_via_gate(self):
        sp = self.new_space("d12")
        eb.page_op("goto", sp.space_id, "p1", url=self.server.url)
        f1 = self.gate_flow(sp, "p1", "fill",
                            {"selector": "#inp", "value": "hello-r2"},
                            task_id="t-d1")
        self.assertTrue(f1["out"]["ok"], f1["out"])
        # commit tag 由 outcome 决定：outcome.ok 才 SENT
        self.assertTrue(f1["out"]["data"]["outcome"]["ok"], f1["out"])
        # dispatch 后 DISPATCHED；commit → COMPLETED（完整状态机）
        self.assertEqual(self.ledger.get(f1["aid"])["state"], "DISPATCHED")
        self.gate.commit(f1["ctx"], f1["aid"], "SENT", evidence="d1 fill")
        self.gate.release(f1["ac"]["data"]["lease"])
        row = self.ledger.get(f1["aid"])
        self.assertEqual(row["state"], "COMPLETED")
        # 真实安全交互：点击按钮（副作用发生在隔离测试页上）
        f2 = self.gate_flow(sp, "p1", "click", {"selector": "#btn"},
                            task_id="t-d2")
        self.assertTrue(f2["out"]["ok"], f2["out"])
        self.assertTrue(f2["out"]["data"]["outcome"]["ok"], f2["out"])
        self.gate.commit(f2["ctx"], f2["aid"], "SENT", evidence="d2 click")
        self.gate.release(f2["ac"]["data"]["lease"])
        self.assertEqual(self.ledger.get(f2["aid"])["state"], "COMPLETED")
        text = eb.page_op("read_text", sp.space_id, "p1")
        self.assertIn("CLICKED-", text["data"]["text"])

    def test_d3_lint_blocks_backend_import(self):
        import tempfile as tf
        biz = os.path.join(self.d, "biz_mod.py")
        with open(biz, "w", encoding="utf-8") as f:
            f.write("import ego_backend\nx = ego_backend.capabilities()\n")
        v = gate_lint.lint_files([biz])
        self.assertTrue(any(x["module"] == "ego_backend" for x in v))

    def test_d4_no_lock_no_dispatch(self):
        sp = self.new_space("d4")
        eb.page_op("goto", sp.space_id, "p1", url=self.server.url)
        ident = H.wait_page_ready(sp.space_id, "p1")
        # 不 acquire，伪造 ctx
        ctx = {"executor_id": self.executor, "run_id": "r2",
               "fencing_generation": 999, "page_generation": "pg-1"}
        r = self.ledger.reserve("t-d4", {"op": "click"}, budget_units=10,
                                business_target="space:%s|p1|t-d4|click"
                                % sp.space_id)
        out = self.gate.dispatch(ctx, "click",
                                 lambda o, a, c: eb.page_op(o, sp.space_id, "p1", **a),
                                 {"selector": "#btn"}, taskspace_id=sp.space_id,
                                 page_key="sp%s|p1" % sp.space_id,
                                 identity_tuple=ident, observation=None,
                                 action_id=r["data"]["action_id"])
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], SP.SP_GATE_007)

    def test_d5_stale_fencing_rejected(self):
        sp = self.new_space("d5")
        eb.page_op("goto", sp.space_id, "p1", url=self.server.url)
        flow = self.gate_flow(sp, "p1", "read_text", {}, task_id="t-d5a")
        self.assertTrue(flow["out"]["ok"])
        # 旧 gen ctx 再派发 → FENCED_OUT
        old_ctx = dict(flow["ctx"], fencing_generation=flow["gen"] - 1)
        r2 = self.ledger.reserve("t-d5b", {"op": "read", "n": 2}, budget_units=10)
        ident = H.wait_page_ready(sp.space_id, "p1")
        out = self.gate.dispatch(old_ctx, "read_text",
                                 lambda o, a, c: eb.page_op(o, sp.space_id, "p1", **a),
                                 {}, taskspace_id=sp.space_id,
                                 page_key="sp%s|p1" % sp.space_id,
                                 identity_tuple=ident, observation=None,
                                 action_id=r2["data"]["action_id"])
        self.assertEqual(out["code"], SP.SP_GATE_007, out)

    def test_d6_identity_change_blocks_action(self):
        sp = self.new_space("d6")
        eb.page_op("goto", sp.space_id, "p1", url=self.server.url)
        flow = self.gate_flow(sp, "p1", "read_text", {}, task_id="t-d6a")
        self.assertTrue(flow["out"]["ok"])
        # 真实导航改变页面
        self.server.bump_gen()
        eb.page_op("goto", sp.space_id, "p1", url=self.server.url)
        # 绑定记录仍是旧的（flow.ctx.page_generation 未更新）
        r2 = self.ledger.reserve("t-d6b", {"op": "click", "n": 2}, budget_units=10,
                                 business_target="space:%s|p1|t-d6b|click"
                                 % sp.space_id)
        ident_new = H.wait_page_ready(sp.space_id, "p1")
        out = self.gate.dispatch(flow["ctx"], "click",
                                 lambda o, a, c: eb.page_op(o, sp.space_id, "p1", **a),
                                 {"selector": "#btn"}, taskspace_id=sp.space_id,
                                 page_key="sp%s|p1" % sp.space_id,
                                 identity_tuple=ident_new, observation=None,
                                 action_id=r2["data"]["action_id"])
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], SP.SP_GATE_009, out)

    def test_d7_observation_invalid_rejected(self):
        sp = self.new_space("d7")
        eb.page_op("goto", sp.space_id, "p1", url=self.server.url)
        flow = self.gate_flow(sp, "p1", "read_text", {}, task_id="t-d7a")
        self.server.bump_gen()
        eb.page_op("goto", sp.space_id, "p1", url=self.server.url)
        r2 = self.ledger.reserve("t-d7b", {"op": "click", "n": 2}, budget_units=10,
                                 business_target="space:%s|p1|t-d7b|click"
                                 % sp.space_id)
        ident = H.wait_page_ready(sp.space_id, "p1")
        out = self.gate.dispatch(flow["ctx"], "click",
                                 lambda o, a, c: eb.page_op(o, sp.space_id, "p1", **a),
                                 {"selector": "#btn"}, taskspace_id=sp.space_id,
                                 page_key="sp%s|p1" % sp.space_id,
                                 identity_tuple=ident,
                                 observation=flow["obs"],   # 旧 observation
                                 action_id=r2["data"]["action_id"])
        self.assertEqual(out["code"], SP.SP_GATE_009, out)

    def test_d8_lock_held_until_outcome(self):
        sp = self.new_space("d8")
        eb.page_op("goto", sp.space_id, "p1", url=self.server.url)
        flow = self.gate_flow(sp, "p1", "read_text", {}, task_id="t-d8")
        self.assertTrue(flow["out"]["ok"])
        cur = statelock.read_holder_named(self.lock)
        self.assertIsNotNone(cur.get("lock_id"), "outcome 落盘后、release 前锁必须在")
        lease = flow["ac"]["data"]["lease"]
        self.gate.commit(flow["ctx"], flow["aid"], "SENT", evidence="d8")
        self.gate.release(lease)
        self.assertEqual(statelock.read_holder_named(self.lock), {})

    # ============================================================ 组 E
    def test_e3_timeout_is_unknown_not_failure(self):
        sp = self.new_space("e3")
        eb.page_op("goto", sp.space_id, "p1", url=self.server.url)
        ident = H.wait_page_ready(sp.space_id, "p1")
        ac = self.gate.acquire({"executor_id": self.executor, "run_id": "r2"})
        gen = ac["data"]["fencing_generation"]
        b = self.pi.bind("sp%s|p1" % sp.space_id, ident, self.executor, gen, "bind")
        obs = eb.build_observation(sp.space_id, "p1", b["page_generation"],
                                   self.executor, gen, ident)
        r = self.ledger.reserve("t-e3", {"op": "goto", "slow": True},
                                budget_units=10,
                                business_target="space:%s|p1|t-e3|goto"
                                % sp.space_id)
        ctx = {"executor_id": self.executor, "run_id": "r2",
               "fencing_generation": gen, "page_generation": b["page_generation"]}
        # 极小超时 → 桥接超时；动作可能已派发 → SP-BE-004 → UNKNOWN
        out = self.gate.dispatch(ctx, "goto",
                                 lambda o, a, c: eb.page_op(o, sp.space_id, "p1",
                                                            timeout_s=0.5, **a),
                                 {"url": self.server.url}, taskspace_id=sp.space_id,
                                 page_key="sp%s|p1" % sp.space_id,
                                 identity_tuple=ident, observation=obs,
                                 action_id=r["data"]["action_id"])
        self.assertEqual(out["code"], SP.SP_BE_004, out)
        # UNKNOWN 由 Gate.dispatch 内部自动恢复（不自动重放，resend 恒 0）
        self.assertEqual([x["action_id"] for x in out["data"]["recovered"]],
                         [r["data"]["action_id"]], out)
        # 二次恢复幂等：已恢复的不得重复
        rec = self.ledger.recover_unknown()
        self.assertEqual(rec["data"]["count"], 0, "恢复必须幂等，不得重复")
        row = self.ledger.get(r["data"]["action_id"])
        self.assertEqual(row["state"], "SEND_UNKNOWN")
        self.assertEqual(row["anti_replay_locked"], 1)
        self.assertEqual(row["resend_count"], 0)

    def test_e4_promise_rejection_maps_to_error(self):
        sp = self.new_space("e4")
        # 无效地址 → Ego Promise rejection → 明确失败（非 UNKNOWN）
        r = eb.page_op("goto", sp.space_id, "p1",
                       url="http://127.0.0.1:1/unreachable")
        self.assertFalse(r["ok"])
        self.assertIn(r["code"], (SP.SP_BE_003, SP.SP_BE_004), r)

    def test_e5_e6_e7_kill_between_dispatch_and_outcome(self):
        sp = self.new_space("e567")
        eb.page_op("goto", sp.space_id, "p1", url=self.server.url)
        ident = H.wait_page_ready(sp.space_id, "p1")
        # worker：真实 fill 经 Gate，桥接完成后、commit 前 os._exit(9)
        worker = os.path.join(HERE, "fixtures", "ego_r2_worker.py")
        marker = os.path.join(self.d, "bridge_done.json")
        env = dict(os.environ, PYTHONUTF8="1",
                   R2_LOCK=self.lock, R2_LEDGER=os.path.join(self.d, "w-ledger.sqlite"),
                   R2_PAGES=os.path.join(self.d, "w-pages.json"),
                   R2_SPACE=str(sp.space_id), R2_URL=self.server.url,
                   R2_MARKER=marker,
                   R2_KEYS=os.path.join(self.d, "w-keys"),
                   R2_OPS=os.path.join(self.d, "w-ops"))
        p = subprocess.Popen([sys.executable, worker], env=env, cwd=ROOT,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True)
        # 等 worker 落「桥接已完成」标记（动作已真实派发）
        import time as _t
        for _ in range(60):
            if os.path.exists(marker):
                break
            _t.sleep(0.5)
        self.assertTrue(os.path.exists(marker), "worker 必须先真实派发 fill")
        with open(marker, encoding="utf-8") as fh:
            out_w = json.load(fh)
        aid = out_w["action_id"]
        p.wait(timeout=30)
        h = getattr(p, "_handle", None)
        if h is not None and p.returncode is not None:
            try:
                h.Close()
                p._handle = None
            except Exception:
                pass
        self.assertEqual(p.returncode, 9)
        # 新进程恢复：UNKNOWN，不自动重放，重新核验 ownership/身份
        led = actionledger.ActionLedger(env["R2_LEDGER"])
        rec = led.recover_unknown()
        self.assertEqual([x["action_id"] for x in rec["data"]["recovered"]], [aid])
        row = led.get(aid)
        self.assertEqual(row["state"], "SEND_UNKNOWN")
        self.assertEqual(row["resend_count"], 0)
        # 防重：同 intent 任何再 reserve 全拒
        r2 = led.reserve("t-e567", json.loads(row["intent_payload"]),
                         seq=99, budget_units=10)
        self.assertFalse(r2["ok"])
        self.assertIn(r2["code"], (SP.SP_ACT_002, SP.SP_ACT_003))
        # 真实证据：页面上的 fill 确实发生了（不能推断 NOT_SENT）
        cur = eb.page_identity(sp.space_id, "p1")
        if cur["ok"]:
            pass  # 身份仍可读；fill 的 outcome 依旧 UNKNOWN（无页面级回执）
        # 锁恢复后重新核验 ownership 与身份（E8）
        self.assertEqual(eb.probe_ownership(sp.space_id), "agent")
        led.close()

    # ============================================================ 组 F
    def test_f1_finish_destroy_on_isolated_space(self):
        sp = self.new_space("f1")
        sid = sp.space_id
        self.assertEqual(eb.probe_ownership(sid), "agent")
        r = sp.finish_disposable(keep=[])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["code"], SP.SP_LIFE_002, "finish({keep:[]}) 必须显式标记销毁语义")
        # 销毁后操作 → 明确失败（非断言成功）
        r2 = eb.page_identity(sid, "p1")
        self.assertFalse(r2["ok"], r2)
        self.assertIn(eb.probe_ownership(sid), ("not_found", "unknown"))

    def test_f2_finish_keep_transfers_to_user(self):
        sp = self.new_space("f2")
        r = sp.finish_disposable(keep=["p1"])
        sp.finished = True
        self.assertTrue(r["ok"], r)
        own = eb.probe_ownership(sp.space_id)
        self.assertIn(own, ("user", "agentDelegatedToUser", "unknown"),
                      "keep-finish 后空间移交/离开 agent 可控范围：%s" % own)
        self.assertNotEqual(own, "agent", "移交后 agent 不得再拥有执行权")

    def test_f3_no_finish_semantic_confusion(self):
        # 静态断言：默认业务流没有 finish 调用面（仅测试夹具显式调用）
        with open(os.path.join(ROOT, "scripts", "ego_backend.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("finish_space", src)
        # gate/ledger/lock 模块无 finish **调用面**。
        # P2-2-4 修订：execution_gate 的可信分类表（SIDE_EFFECT_OPS）以数据
        # 形式列出 "finish_space"（用于判定该操作需要 business_target），
        # 这是分类数据而非调用；因此断言改为「无调用、无特判分支」，
        # 语义与原断言一致——默认业务流绝不含 finish 执行路径。
        for mod in ("execution_gate.py", "actionledger.py", "statelock.py"):
            with open(os.path.join(ROOT, "scripts", mod),
                      encoding="utf-8") as fh:
                msrc = fh.read()
            self.assertEqual(re.findall(r"finish_space\s*\(", msrc), [], mod)
            self.assertNotIn('op == "finish_space"', msrc, mod)
            self.assertNotIn("op == 'finish_space'", msrc, mod)
        # 结构性断言（GPT P2-2-4 终审 ①补充）：finish_space 必须继续走正常
        # Gate / 安全分类 / 授权路径 —— 既被分类为 side-effect（→ 强走五道屏障、
        # business_target 必填），又不得在 gate 模块内以别名或转发方式绕开检查。
        import execution_gate
        self.assertIn("finish_space", execution_gate.SIDE_EFFECT_OPS)
        self.assertEqual(execution_gate.operation_target_policy("finish_space"),
                         "require")
        with open(os.path.join(ROOT, "scripts", "execution_gate.py"),
                  encoding="utf-8") as fh:
            gsrc = fh.read()
        self.assertEqual(gsrc.count('"finish_space"'), 1,
                         "gate 模块内该操作名只允许出现在可信分类表一处（无别名转发）")
        for alias in ("FS = ", "FINISH = ", "finish = "):
            self.assertNotIn(alias, gsrc)
        # handOff ≠ finish ≠ close：夹具状态独立跟踪
        sp = self.new_space("f3")
        self.assertFalse(sp.finished and sp.handed_off)
        eb.hand_off(sp.space_id)
        sp.handed_off = True
        self.assertFalse(sp.finished, "handOff 绝不等于 finish")

    def test_f4_no_reclaim_anywhere(self):
        # 后端模块不提供 reclaim 能力；测试夹具清理只 finish 自己的一次性空间
        for mod in ("ego_backend.py",):
            with open(os.path.join(ROOT, "scripts", mod),
                      encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotIn("reclaim", src.lower().replace("不自主 reclaim", "")
                             .replace("no reclaim", "")
                             .replace("自主 reclaim", "")
                             .replace("不 reclaim", ""),   # 中文注释合法提及
                             "后端不得实现 reclaim")


if __name__ == "__main__":
    unittest.main()
