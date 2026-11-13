#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""execution_gate.py —— 唯一受控执行入口（Lock / Ownership / Identity / Ledger）。

规范来源：PROJECT-02-R1 ARCHITECTURE §5–§7 + PROJECT-03-R1 任务书 §6 +
终审三项重点（TOCTOU、幂等键、锁覆盖）。

时序（ARCHITECTURE §6，锁覆盖完整窗口 = SP-GATE-011 约束）：

    business → ledger.reserve()            （意图登记 + 预占，先于锁）
             → gate.acquire()              （门 1 本地锁）
             → gate.dispatch(op)           （门 2 所有权 + 门 3 页面身份 +
                                            fencing 即时复核 → mark_dispatched
                                            → 真正调用 backend → outcome）
             → gate.commit(outcome)        （commit 入口复核 fencing →
                                            落账本 → 才 release 锁）

纪律：
- **dispatch 与 commit 分离**；锁覆盖到 outcome 落盘之后才释放（INV-2）。
- dispatch 内部在「mark_dispatched 之后、backend 调用之前」做最后一轮
  fencing + 所有权 + 页面身份复核，缩短 TOCTOU 窗口（终审重点 2）。
- backend 调用异常 / RESULT_UNKNOWN → 恢复 SEND_UNKNOWN，anti_replay
  锁定，自动重发恒 0（INV-8 / 终审补充 D）。
- fencing_generation 在 dispatch 与 commit 入口**重读锁文件校验**，
  旧执行者凭缓存对象继续执行 → SP-GATE-007 FENCED_OUT（INV-4）。
- TaskSpace 全局所有权经 ownership_probe 读取（本阶段为显式标注的
  测试桩 / 后端契约位，PROJECT-03-R2 接真实 Ego listTaskSpaces）。
"""
from __future__ import annotations

import os

import dispatch_marker
import envelope
import envelope_node
import statelock
import sp_errors as SP

# ---------------------------------------------------------------- P2-2-4
# 操作类型 × 业务对象要求：**可信分类表**（不由调用方 decide）。
# 与 ego_backend.CAPABILITIES 的 side_effect_possible 对齐（tests 断言一致）；
# 未识别操作 → 一律按「需业务对象」处理并拒绝（fail-closed）。
SIDE_EFFECT_OPS = frozenset({
    "create_space", "goto", "fill", "click", "press", "hand_off",
    "finish_space", "send", "send_reply", "submit",
})
READ_ONLY_OPS = frozenset({
    "list_spaces", "space_probe", "page_exists", "page_identity",
    "page_viewport", "read_text", "snapshot", "wait_control",
    "probe_page", "probe_evaluate", "noop",
})


def operation_target_policy(op: str) -> str:
    """返回 "require"（需业务对象）/ "allow_empty" / "unknown"。"""
    if op in SIDE_EFFECT_OPS:
        return "require"
    if op in READ_ONLY_OPS:
        return "allow_empty"
    return "unknown"


class SecurityPlane:
    """P2-2-4 派发前屏障配置（Envelope 密钥 / worker 身份 / Marker 目录）。"""

    def __init__(self, key_dir: str, worker_id: str, ops_root: str,
                 key_version: str | None = None):
        if not key_dir or not worker_id or not ops_root:
            raise ValueError("security plane requires key_dir/worker_id/ops_root")
        self.key_dir = key_dir
        self.worker_id = worker_id
        self.ops_root = ops_root
        self.key_version = key_version
        # 启动自检：Marker 目录可用 + 注册表信任边界（缺失/损坏/冲突即 fail-closed）
        self.dispatching = dispatch_marker.dispatching_dir(ops_root)
        envelope.check_registry_trust(key_dir)


class ExecutionGate:
    """resource 上的唯一副作用出口。所有 side_effect_possible=true 的操作
    必须经 dispatch()；业务模块不得直接 import 后端（gate_lint 强制）。"""

    def __init__(self, lock_path: str, ledger, page_identity,
                 ownership_probe=None, resource: str = "",
                 observation_verify=None, plane: SecurityPlane | None = None):
        # ownership_probe: callable(taskspace_id) -> "agent" | 其它。
        #   本阶段测试桩返回常量；R2 接 Ego listTaskSpaces()。None = 未配置，
        #   dispatch 直接 SP-GATE-008 OWNERSHIP_UNVERIFIED 拒绝（fail-closed）。
        # plane: P2-2-4 派发前屏障（Envelope/Marker/DELEGATED）。side-effect
        #   操作未配置 plane → dispatch SP-OP-001 拒绝（唯一合法副作用出口
        #   不存在绕过路径；DP15）。
        self.lock_path = lock_path
        self.ledger = ledger
        self.pi = page_identity
        self.ownership_probe = ownership_probe
        self.resource = resource or lock_path
        self.observation_verify = observation_verify  # callable(observation) -> Result
        self.plane = plane

    # ------------------------------------------------------------ 门 1
    def acquire(self, ctx: dict, timeout: float = 10.0) -> dict:
        """拿本地锁。ctx 需含 executor_id（携带 process_start_token 信息）。"""
        try:
            lock = statelock.acquire_named(
                self.lock_path, owner=ctx.get("owner") or "gate",
                run_id=ctx.get("run_id") or "", timeout=timeout,
                stale_after=float(ctx.get("stale_after") or 300.0),
                resource=self.resource,
                executor_id=ctx.get("executor_id") or "")
        except statelock.LockStateUnknown as e:
            return SP.result(False, SP.SP_GATE_004,
                             {"holder": e.holder}, str(e))
        except statelock.LockCorrupt as e:
            return SP.result(False, SP.SP_GATE_005,
                             {"holder": e.holder}, str(e))
        except statelock.LockBusy as e:
            return SP.result(False, SP.SP_GATE_001,
                             {"holder": e.holder}, str(e))
        gen = int(lock.get("fencing_generation") or 0)
        if lock.get("stale"):
            code = SP.SP_GATE_006  # LOCK_RECOVERED：接管成功，必须重建 observation
        else:
            code = SP.SP_OK
        return SP.result(True, code,
                         {"lease": lock, "fencing_generation": gen,
                          "recovered": bool(lock.get("stale"))},
                         "lease acquired; stale=%r" % lock.get("stale"), gen)

    # ------------------------------------------------------------ fencing
    def _current_lease(self) -> dict | None:
        """重读锁文件，返回当前权威持有者（fencing 校验的唯一事实来源）。"""
        h = statelock.read_holder_named(self.lock_path)
        if not h or h.get("broken") or not h.get("lock_id"):
            return None
        return h

    def _fencing_ok(self, ctx: dict) -> dict:
        cur = self._current_lease()
        if cur is None:
            return SP.result(False, SP.SP_GATE_007, {}, "no live lease on resource")
        if int(cur.get("fencing_generation") or -1) != int(ctx["fencing_generation"]):
            return SP.result(False, SP.SP_GATE_007,
                             {"current": cur.get("fencing_generation"),
                              "claimed": ctx["fencing_generation"]},
                             "stale fencing generation — executor fenced out")
        if cur.get("executor_id") != ctx.get("executor_id"):
            return SP.result(False, SP.SP_GATE_007,
                             {"current": cur.get("executor_id"),
                              "claimed": ctx.get("executor_id")},
                             "executor_id mismatch — second executor or lost lease")
        return SP.result(True, SP.SP_OK, {"lock_id": cur.get("lock_id")}, "fencing verified")

    # ------------------------------------------------------------ 门 2/3
    def verify(self, ctx: dict, taskspace_id=None, page_key: str = "",
               identity_tuple: dict | None = None,
               observation: dict | None = None) -> dict:
        """三重门全量核验（acquire 之后、dispatch 之前；dispatch 内部还会
        再做一轮即时复核，见 _pre_dispatch_verify）。"""
        return self._verify_all(ctx, taskspace_id, page_key, identity_tuple,
                                observation, check_fencing=True)

    def _verify_all(self, ctx: dict, taskspace_id, page_key: str,
                    identity_tuple, observation, check_fencing: bool) -> dict:
        if check_fencing:
            f = self._fencing_ok(ctx)
            if not f["ok"]:
                return f
        # 门 2：全局所有权（fail-closed：探针未配置即拒）
        if self.ownership_probe is None:
            return SP.result(False, SP.SP_GATE_008, {},
                             "ownership probe not configured (backend stub not wired)")
        own = self.ownership_probe(taskspace_id)
        if own != "agent":
            return SP.result(False, SP.SP_GATE_008,
                             {"ownership": own},
                             "taskspace ownership is %r — agent blocked" % own)
        # 门 3：页面身份 + observation（fail-closed：配置了校验器却不带
        # observation 一律拒绝，防止「跳过采集直接派发」）
        if page_key:
            v = self.pi.verify(page_key, identity_tuple or {},
                               ctx.get("page_generation") or "",
                               ctx.get("executor_id") or "",
                               ctx.get("fencing_generation") or 0)
            if not v["ok"]:
                return v
        if self.observation_verify is not None:
            if observation is None:
                return SP.result(False, SP.SP_GATE_010, {},
                                 "observation required but missing — fail-closed")
            ov = self.observation_verify(observation)
            if not ov["ok"]:
                return SP.result(False, SP.SP_GATE_010, ov.get("data", {}),
                                 ov.get("evidence", "observation invalid"))
        return SP.result(True, SP.SP_OK, {}, "gates verified", ctx["fencing_generation"])

    # ------------------------------------------------------------ 派发
    def dispatch(self, ctx: dict, op: str, backend_fn, args: dict,
                 taskspace_id=None, page_key: str = "", identity_tuple=None,
                 observation=None, action_id: str = "",
                 require_business_target: bool = False) -> dict:
        """唯一副作用出口（P2-2-4 五道派发前屏障 + 唯一浏览器调用点）。

        合法顺序（任何路径不得跳过其中一步）：
          ⓪ 操作类型策略（可信分类，未知类型拒绝）
          ① 四门复核（业务对象锁/操作幂等锁/意图身份锁/fencing/所有权/页面身份）
          ② Envelope 构造+签名+验签（Python 镜像 + **Ego Node v24 原生验签**，
             签名内容与实际执行对象一致；Node 失败即止步且不得回退镜像）
          ③ Marker 持久化（O_EXCL→写入→fsync→平台目录持久化）
          ④ DISPATCH_DELEGATED 独立事务提交 + 重新确认
          ⑤ mark_dispatched
          ⑥ backend_fn —— 唯一浏览器调用边界，**绝不自动重试**（无 _infra_retry）
          ⑦ 结果提交或 UNKNOWN 恢复（异常/超时/不确定一律保守）

        backend_fn(op, args, ctx) -> dict；unknown=True → SEND_UNKNOWN。
        """
        # ⓪ 操作类型 × 业务对象（可信分类表，不由调用方决定）
        policy = operation_target_policy(op)
        if policy == "unknown":
            return SP.result(False, SP.SP_ACT_010, {"op": op},
                             "dispatch refused: unknown operation type — "
                             "fail-closed (operation allowlist)")
        act = self.ledger.get(action_id) if action_id else None
        target_norm = (act or {}).get("business_target_norm") or ""
        if policy == "require" and not (act and act.get("business_target")):
            return SP.result(False, SP.SP_ACT_010,
                             {"action_id": action_id,
                              "has_business_target": bool(act and act.get("business_target"))},
                             "dispatch refused: side-effect operation requires "
                             "business_target — fail-closed (SP-ACT-010)")
        if require_business_target and not (act and act.get("business_target")):
            return SP.result(False, SP.SP_ACT_010,
                             {"action_id": action_id},
                             "dispatch refused: caller requires business_target — "
                             "fail-closed (SP-ACT-010)")
        # ① 派发前最后一轮全量门核验（最小化 TOCTOU 窗口）
        pre = self._verify_all(ctx, taskspace_id, page_key, identity_tuple,
                               observation, check_fencing=True)
        if not pre["ok"]:
            return pre
        # ②-④ 派发前屏障（仅 side-effect；读操作无副作用不需要）
        backend_ctx = dict(ctx)
        if policy == "require":
            b = self._pre_dispatch_barriers(ctx, act, action_id, target_norm)
            if not b["ok"]:
                return b
            # P2-2-4R（NR12）：把 **Node 已验签的那一份 envelope** 原样交给
            # backend，使“验签对象 == 执行对象”可核验（同一 dict / 同一 digest）。
            backend_ctx["envelope"] = b["data"]["envelope"]
            backend_ctx["envelope_digest"] = b["data"]["envelope_digest"]
            backend_ctx["node_runtime"] = b["data"]["node_runtime"]
        # ⑤ 账本：RESERVED→DISPATCHED（副作用之前；之后崩溃 → UNKNOWN）
        d = self.ledger.mark_dispatched(action_id, ctx["fencing_generation"],
                                        ctx.get("executor_id") or "")
        if not d["ok"]:
            return d
        # ⑥ 唯一浏览器调用边界：绝不自动重试（无 _infra_retry、无循环）
        try:
            out = backend_fn(op, dict(args or {}), backend_ctx)
        except Exception as e:  # 本地异常 ≠ 未发出（禁止推断 NOT_SENT）
            rec = self.ledger.recover_unknown()
            return SP.result(False, SP.SP_BE_004,
                             {"action_id": action_id,
                              "recovered": rec.get("data", {}).get("recovered", [])},
                             "backend call raised: %s — action locked UNKNOWN" % e,
                             ctx["fencing_generation"])
        if not isinstance(out, dict) or out.get("unknown"):
            rec = self.ledger.recover_unknown()
            return SP.result(False, SP.SP_BE_004,
                             {"action_id": action_id,
                              "backend_raw": out,
                              "recovered": rec.get("data", {}).get("recovered", [])},
                             "RESULT_UNKNOWN — SEND_UNKNOWN, auto-resend stays 0",
                             ctx["fencing_generation"])
        return SP.result(True, SP.SP_OK,
                         {"action_id": action_id, "outcome": out},
                         "dispatched and outcome present", ctx["fencing_generation"])

    # ------------------------------------------------ P2-2-4 五道屏障实现
    def _pre_dispatch_barriers(self, ctx: dict, act: dict, action_id: str,
                               target_norm: str) -> dict:
        """② Envelope(Python 镜像 + Node 原生验签) → ③ Marker → ④ DELEGATED。
        任一失败即拒。"""
        if self.plane is None:
            return SP.result(False, SP.SP_OP_001, {"action_id": action_id},
                             "security plane unconfigured — dispatch refused "
                             "(fail-closed, DP15)")
        fencing = str(int(ctx["fencing_generation"]))
        op_id = action_id
        # ② Envelope 构造 + 签名 + 验签（签名内容与实际执行对象一致）
        try:
            env = envelope.build_and_sign(
                op_id=op_id, task_id=(act or {}).get("task_id", ""),
                intent_key=(act or {}).get("intent_key", ""),
                fencing=int(fencing), worker_id=self.plane.worker_id,
                business_target_norm=target_norm,
                key_dir=self.plane.key_dir, key_version=self.plane.key_version)
            pem = envelope.public_key_pem(
                self.plane.key_dir, env["key_version"])
        except (envelope.EnvelopeUnavailable, ValueError) as e:
            return SP.result(False, SP.SP_OP_001, {"action_id": action_id,
                                                   "reason": str(e)[:200]},
                             "envelope unavailable/invalid — dispatch refused")
        v = envelope.verify_envelope(env, pem)
        if not v["ok"]:
            return SP.result(False, SP.SP_OP_001,
                             {"action_id": action_id, "reason": v["reason"]},
                             "envelope self-verification failed — dispatch refused")
        # ②' P2-2-4R：**Ego Node 原生 Ed25519 验签**（权威屏障，强制前置）。
        # 必须在 Marker/DELEGATED/mark 之前完成；失败即止步（不写任何证据、
        # 不调 backend、不自动重试，也不允许回退到 Python 镜像验签继续）。
        nv = envelope_node.verify_via_node(env, self.plane.key_dir)
        if not nv["ok"]:
            return SP.result(False, SP.SP_OP_001,
                             {"action_id": action_id, "stage": "node_verify",
                              "node_stage": nv.get("stage"),
                              "runtime": nv.get("runtime", ""),
                              "reason": nv.get("reason", "")[:200]},
                             "ego node native verification failed/unavailable — "
                             "dispatch refused (fail-closed, NR01-NR11)")
        # ③ Marker 持久化（失败禁止继续；已存在=可能已派发 → 保守拒绝）
        try:
            mk = dispatch_marker.write_marker(
                self.plane.dispatching, op_id, env["envelope_digest"],
                fencing, self.plane.worker_id, env["key_version"])
        except dispatch_marker.MarkerExists:
            return SP.result(False, SP.SP_OP_001,
                             {"action_id": action_id, "op_id": op_id,
                              "marker_state": "PRESENT"},
                             "marker already exists — operation may have been "
                             "dispatched; conservative refusal (no auto re-dispatch)")
        except dispatch_marker.MarkerError as e:
            return SP.result(False, SP.SP_OP_001,
                             {"action_id": action_id, "reason": str(e)[:200]},
                             "marker persistence failed — dispatch forbidden")
        # ④ DELEGATED 独立事务提交 + 重新确认（提交后才允许 mark）
        d = self.ledger.write_delegated(
            action_id, op_id, env["envelope_digest"], fencing,
            self.plane.worker_id, env["key_version"], target_norm)
        if not d["ok"]:
            return d
        chk = self.ledger.check_delegated(action_id, {
            "op_id": op_id, "envelope_digest": env["envelope_digest"],
            "fencing": fencing, "worker_id": self.plane.worker_id,
            "key_version": env["key_version"]})
        if not chk["ok"] or chk["state"] != "PRESENT":
            return SP.result(False, SP.SP_OP_002,
                             {"action_id": action_id,
                              "state": chk.get("state"),
                              "reason": chk.get("reason")},
                             "delegated not confirmed after commit — dispatch forbidden")
        return SP.result(True, SP.SP_OK,
                         {"action_id": action_id, "envelope_digest":
                          env["envelope_digest"], "marker": mk["path"],
                          "envelope": env, "node_runtime": nv["runtime"]},
                         "pre-dispatch barriers satisfied (ego node %s verified)"
                         % nv["runtime"])

    def _marker_state(self, op_id: str) -> str:
        if self.plane is None:
            return "unknown"
        try:
            path = dispatch_marker.marker_path(self.plane.dispatching, op_id)
        except dispatch_marker.MarkerInvalid:
            return "invalid"
        if not os.path.exists(path):
            return "absent"
        try:
            dispatch_marker.read_marker(path)
            return "present"
        except dispatch_marker.MarkerInvalid:
            return "invalid"

    def recovery_verdict(self, action_id: str,
                         authority_proven: bool = False) -> dict:
        """恢复合并（三类证据：Ledger 状态 + Marker + DELEGATED）。

        任何不确定 → UNKNOWN（禁止自动重派）；唯一快路径要求
        authority_proven=True 且双证据缺失、状态机处于未派发前态。
        """
        act = self.ledger.get(action_id)
        if not act:
            return {"verdict": "UNKNOWN", "reason": "action not found"}
        state = act.get("state")
        marker = self._marker_state(action_id)
        dg = self.ledger.check_delegated(action_id, {"op_id": action_id})
        if marker == "invalid":
            return {"verdict": "UNKNOWN", "reason": "marker corrupted",
                    "marker": marker}
        if not dg["ok"]:
            return {"verdict": "UNKNOWN", "reason": "delegated query incomplete",
                    "delegated_state": dg.get("state")}
        dstate = dg["state"]
        if dstate in ("CONFLICT", "STALE_FENCING"):
            return {"verdict": "UNKNOWN", "reason": "delegated " + dstate,
                    "marker": marker}
        m_present, d_present = marker == "present", dstate == "PRESENT"
        if not m_present and not d_present:
            if authority_proven and state in ("CREATED", "RESERVED"):
                return {"verdict": "NOT_DISPATCHED_FASTPATH",
                        "reason": "both evidence absent + authority integrity proven",
                        "state": state}
            return {"verdict": "UNKNOWN",
                    "reason": "recovery gap: both evidence absent but authority "
                              "integrity not proven (no auto re-dispatch)",
                    "state": state}
        if m_present and d_present:
            try:
                mk = dispatch_marker.read_marker(
                    dispatch_marker.marker_path(self.plane.dispatching, action_id))
            except dispatch_marker.MarkerError as e:
                return {"verdict": "UNKNOWN", "reason": "marker re-read: %s" % e}
            if mk["envelope_digest"] != dg["event"]["envelope_digest"]:
                return {"verdict": "UNKNOWN",
                        "reason": "marker/delegated digest mismatch"}
            return {"verdict": "DISPATCH_POSSIBLE",
                    "reason": "both evidence present and consistent; backend "
                              "execution unproven — state machine decides",
                    "state": state}
        return {"verdict": "UNKNOWN",
                "reason": "single evidence present (marker=%s delegated=%s) — "
                          "conservative, no auto re-dispatch"
                          % (marker, dstate), "state": state}

    # ------------------------------------------------------------ 提交
    def commit(self, ctx: dict, action_id: str, result_tag: str,
               evidence: str = "", unlock: dict | None = None) -> dict:
        """commit 入口：**先复核 fencing**，再落账本。落盘成功才允许 release。

        SP1-A：fencing 同时透传给账本（`expected_fencing`），使所有权校验在
        账本层再走一遍——应用层复核不再是唯一防线。
        """
        f = self._fencing_ok(ctx)
        if not f["ok"]:
            return f
        c = self.ledger.commit_outcome(
            action_id, result_tag, evidence, unlock=unlock,
            expected_fencing=ctx.get("fencing_generation"))
        if not c["ok"]:
            return c
        return SP.result(True, SP.SP_OK, c["data"],
                         "outcome committed (lock still held until release)",
                         ctx["fencing_generation"])

    # ------------------------------------------------------------ 释放
    def release(self, lease: dict) -> None:
        """锁覆盖终点：只在 outcome 落盘（或明确放弃）之后调用。"""
        statelock.release_named(self.lock_path, lease)
