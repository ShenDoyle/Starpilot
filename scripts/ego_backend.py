#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ego_backend.py —— Ego 原生浏览器后端适配器（PROJECT-03-R2）。

边界纪律（任务书 §3）：
- Ego SDK 只经 `scripts/ego/ego_bridge.mjs`（Node 侧）接触，Python 侧只发
  op 规范、收纯 JSON Result —— SDK 原生对象永不泄漏到业务层；
- 业务层不得 import 本模块（gate_lint 的 BACKEND_MODULES 已覆盖 `ego_backend`），
  所有 side_effect_possible=true 的操作必须经 ExecutionGate；
- 能力先声明后调用：CAPABILITIES 是唯一能力表，未声明 → SP-BE-002；
  SDK 里存在但未声明的能力**不暴露**（SP-BE-001 UNSUPPORTED）。

执行权纪律（任务书 §5，01-R1 F7 证据）：
- ownership 一律读真实 listTaskSpaces() 全局列表（ego_bridge space_probe）；
  本地 task.ownership 是陈旧快照，**绝不作为执行权判据**；
- ownership 未知 / 空间不存在 / 非 agent / 查询异常 → fail-closed
  （SP-GATE-008 / SP-BE-003）；
- user-control hard stop（agentDelegatedToUser / probeUserControlReason）→
  SP-BE-005：停止执行链、失效 Observation、不 reclaim、不自动重试；
- 动作已派发而结果不明（超时/rejection）→ SP-BE-004 → 账本 SEND_UNKNOWN。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time

import sp_errors as SP

EGO_EXE = (r"C:\Program Files\Citro Labs\ego lite\Application\0.6.2.10"
           r"\ego-browser.exe")
BRIDGE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "ego", "ego_bridge.mjs")
SDK_VERSION = "0.6.2.10"

# ------------------------------------------------------------- 能力表
# （任务书 §3.1：每项 op 必须声明以下字段；SDK 有但这里没有的能力不暴露）

# R3-C 输入能力边界固化（2026-11-13，R3-B 实测后）：
# 四种输入模式独立声明、绝不互相冒充。is_trusted 语义区分：
# - dom_synthesized：evaluate DOM 合成（R2 实现，保留）。isTrusted=false，
#   只证明「DOM 状态可被脚本改变」，不证明任何真实用户交互语义。
# - trusted_text_input：SDK page.fill() 真实文本输入管线（guide v2.2.0 一级 API）。
# - trusted_keyboard_input：SDK keyboard.type/press/paste + page.press()。
# - trusted_mouse_input：SDK page.click() / mouse.click(x,y)。
# trusted_* 证据 = project-03-r3-report/INPUT-CAPABILITY-MATRIX.md（localhost
# 夹具实测 isTrusted=true，ego lite 0.6.2.10 + guide v2.2.0，2026-11-13）。
# 边界：夹具结论不外推到具体真实站点（受控组件/反自动化/敏感流程必须逐站验证）。
INPUT_MODES = {
    "dom_synthesized": dict(
        supported=True, is_trusted=False, environment_dependent=False,
        verified="R2 localhost fixture (03-R2 DIAGNOSTICS)",
        note="evaluate DOM 合成：focus+value+input/change / el.click() / KeyboardEvent"),
    "trusted_text_input": dict(
        supported=True, is_trusted=True, environment_dependent=True,
        availability_probe="page_viewport", unavailable_error="SP-BE-007",
        verified="R3-B localhost fixture 2026-11-13 (matrix1/sdk_fill_plain/sdk_fill_controlled)",
        note="SDK page.fill() 真实管线；受控组件实测同步；依赖窗口渲染状态"),
    "trusted_keyboard_input": dict(
        supported=True, is_trusted=True, environment_dependent=True,
        availability_probe="page_viewport", unavailable_error="SP-BE-007",
        verified="R3-B localhost fixture 2026-11-13 (matrix1/sdk_keyboard_type/sdk_press/page_cdp_input_keyEvent)",
        note="SDK keyboard.* / page.press()；keydown 事件 isTrusted=true；依赖窗口渲染状态"),
    "trusted_mouse_input": dict(
        supported=True, is_trusted=True, environment_dependent=True,
        availability_probe="page_viewport", unavailable_error="SP-BE-007",
        verified="R3-B localhost fixture 2026-11-13 (matrix1/sdk_click/sdk_mouse_click)",
        note="SDK page.click() / mouse.click(x,y)；click 事件 isTrusted=true；依赖窗口渲染状态"),
}
# R3-S（GPT 终审 §4.1）：trusted 能力语义四层拆分——
#   supported             API 是否受官方支持（静态声明，本表）
#   available             当前运行环境是否可用（运行时探针 page_viewport 动态判定）
#   环境限制原因           不可用时显式返回（VIEWPORT_UNAVAILABLE / PROBE_FAILED）
#   本次调用实际模式       结果 input_mode 字段（既有 R3-C 语义不变）

CAPABILITIES = {
    "list_spaces":       dict(supported=True,  side_effect_possible=False, requires_execution_gate=False, requires_ownership=False, requires_page_identity=False, idempotent=True,  timeout_s=60,  unknown_behavior="n/a", error_map="SP-BE-003"),
    "create_space":      dict(supported=True,  side_effect_possible=True,  requires_execution_gate=True,  requires_ownership=False, requires_page_identity=False, idempotent=False, timeout_s=90,  unknown_behavior="UNKNOWN_on_unclear", error_map="SP-BE-003"),
    "space_probe":       dict(supported=True,  side_effect_possible=False, requires_execution_gate=False, requires_ownership=False, requires_page_identity=False, idempotent=True,  timeout_s=60,  unknown_behavior="n/a", error_map="SP-BE-003"),
    "page_exists":       dict(supported=True,  side_effect_possible=False, requires_execution_gate=False, requires_ownership=True,  requires_page_identity=False, idempotent=True,  timeout_s=60,  unknown_behavior="n/a", error_map="SP-BE-003"),
    "page_identity":     dict(supported=True,  side_effect_possible=False, requires_execution_gate=False, requires_ownership=True,  requires_page_identity=True,  idempotent=True,  timeout_s=60,  unknown_behavior="n/a", error_map="SP-GATE-010"),
    # R3-B：viewport 渲染状态只读探针（0×0 = 窗口未在屏幕渲染，真实输入受阻）
    "page_viewport":     dict(supported=True,  side_effect_possible=False, requires_execution_gate=False, requires_ownership=True,  requires_page_identity=False, idempotent=True,  timeout_s=60,  unknown_behavior="n/a", error_map="SP-BE-003"),
    "goto":              dict(supported=True,  side_effect_possible=True,  requires_execution_gate=True,  requires_ownership=True,  requires_page_identity=True,  idempotent=False, timeout_s=90,  unknown_behavior="UNKNOWN_on_unclear", error_map="SP-BE-004"),
    # fill/click/press（03-R2 → 03-R3 演进）：
    # R2：TaskSpace 页面曾实测 viewport 0×0（page.info() w=0,h=0），CDP Input
    #     域恒超时 → 改 evaluate DOM 合成（input_mode="dom_synthesized"）。
    # R3-B（2026-11-13）：同版本 ego lite 0.6.2.10 复测，TaskSpace 页面
    #     viewport 真实存在（2016×987 / 2005×974，blank/多页均如此），0×0
    #     未复现；SDK 一级输入 API（page.fill/click/press、keyboard.*、
    #     mouse.*）与 page.cdp Input 域全部可用且事件 isTrusted=true ——
    #     结论 B-PASS。据此 R3-C 新增独立 trusted 输入模式（input_mode=
    #     "trusted"，映射 INPUT_MODES 的 trusted_* 三类），DOM 合成模式
    #     保留不删。两种模式的调用结果以 verification 分层字段区分。
    # 默认仍 dom_synthesized（保守：既有调用方语义不变），trusted 必须显式
    # 声明 input_mode="trusted" 才走真实管线；未声明的模式 → SP-BE-002。
    "fill":              dict(supported=True,  side_effect_possible=True,  requires_execution_gate=True,  requires_ownership=True,  requires_page_identity=True,  idempotent=False, timeout_s=90,  unknown_behavior="UNKNOWN_on_unclear", error_map="SP-BE-004", input_mode="dom_synthesized", accepted_input_modes=("dom_synthesized", "trusted")),
    "click":             dict(supported=True,  side_effect_possible=True,  requires_execution_gate=True,  requires_ownership=True,  requires_page_identity=True,  idempotent=False, timeout_s=90,  unknown_behavior="UNKNOWN_on_unclear", error_map="SP-BE-004", input_mode="dom_synthesized", accepted_input_modes=("dom_synthesized", "trusted")),
    "press":             dict(supported=True,  side_effect_possible=True,  requires_execution_gate=True,  requires_ownership=True,  requires_page_identity=True,  idempotent=False, timeout_s=90,  unknown_behavior="UNKNOWN_on_unclear", error_map="SP-BE-004", input_mode="dom_synthesized", accepted_input_modes=("dom_synthesized", "trusted")),
    "read_text":         dict(supported=True,  side_effect_possible=False, requires_execution_gate=False, requires_ownership=True,  requires_page_identity=True,  idempotent=True,  timeout_s=60,  unknown_behavior="n/a", error_map="SP-GATE-010"),
    "snapshot":          dict(supported=True,  side_effect_possible=False, requires_execution_gate=False, requires_ownership=True,  requires_page_identity=True,  idempotent=True,  timeout_s=60,  unknown_behavior="n/a", error_map="SP-GATE-010"),
    "hand_off":          dict(supported=True,  side_effect_possible=True,  requires_execution_gate=True,  requires_ownership=False, requires_page_identity=False, idempotent=False, timeout_s=90,  unknown_behavior="UNKNOWN_on_unclear", error_map="SP-BE-005"),
    "wait_control":      dict(supported=True,  side_effect_possible=False, requires_execution_gate=False, requires_ownership=True,  requires_page_identity=False, idempotent=True,  timeout_s=60,  unknown_behavior="n/a", error_map="SP-BE-003"),
    "finish_space":      dict(supported=True,  side_effect_possible=True,  requires_execution_gate=True,  requires_ownership=False, requires_page_identity=False, idempotent=False, timeout_s=90,  unknown_behavior="UNKNOWN_on_unclear", error_map="SP-LIFE-002"),
    # SDK 支持但本阶段**不暴露**给业务层的能力（不得冒充等价语义）
    "cdp":               dict(supported=False, side_effect_possible=True,  reason="low-level escape hatch — deliberately not exposed (R2 §3)"),
}

_HARDCODED_PAGE_CMDS = {"goto", "fill", "click", "press", "page_identity",
                        "read_text", "snapshot"}


def capabilities() -> dict:
    """capability discovery：返回能力表副本（业务层唯一合法入口）。"""
    return {k: dict(v) for k, v in CAPABILITIES.items()}


def input_modes() -> dict:
    """R3-C：输入模式能力表副本（dom_synthesized / trusted_* 独立声明）。"""
    return {k: dict(v) for k, v in INPUT_MODES.items()}


def declare(op: str) -> dict | None:
    return CAPABILITIES.get(op)


# ------------------------------------------------------------- 桥接层

def _run_bridge(op: str, args: dict, timeout_s: float = 60,
                env_extra: dict | None = None) -> dict:
    """拉起全新 ego-browser nodejs 进程执行一个 op；返回桥接 JSON。

    桥接失败（进程崩溃/无输出/超时）→ {ok:False, error:…}，由上层映射
    SP-BE-003 / SP-BE-004（结果不明不推断成功/失败）。
    """
    d = tempfile.mkdtemp(prefix="ego-op-")
    spec = os.path.join(d, "spec.json")
    out = os.path.join(d, "out.json")
    with open(spec, "w", encoding="utf-8") as f:
        json.dump({"op": op, "args": args}, f, ensure_ascii=False)
    # ego-browser nodejs 不透传自定义环境变量（实测），用 wrapper 注入路径
    wrapper = os.path.join(d, "wrapper.mjs")
    with open(wrapper, "w", encoding="utf-8") as f:
        f.write(
            "globalThis.EGO_BRIDGE_SPEC = %s;\n"
            "globalThis.EGO_BRIDGE_OUT = %s;\n"
            "await import('file:///%s');\n"
            % (json.dumps(spec), json.dumps(out), BRIDGE.replace("\\", "/")))
    env = dict(os.environ, PYTHONUTF8="1")
    try:
        p = subprocess.run(
            [EGO_EXE, "nodejs", "-e",
             "await import('file:///%s')" % wrapper.replace("\\", "/")],
            capture_output=True, text=True, timeout=timeout_s, env=env,
            encoding="utf-8", errors="backslashreplace")
    except subprocess.TimeoutExpired as e:
        # 超时 ≠ 未执行（任务书 §7.2）：结果不明，由调用方决定 UNKNOWN
        return {"ok": False, "error": "bridge timeout after %ss" % timeout_s,
                "unclear": True, "stdout_tail": (e.stdout or "")[-300:]}
    if os.path.exists(out):
        try:
            with open(out, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            return {"ok": False, "error": "bridge output unparsable: %s" % e,
                    "unclear": True}
    return {"ok": False,
            "error": "bridge died rc=%s stderr=%s" % (
                p.returncode, (p.stderr or "")[-400:]),
            "unclear": p.returncode == 1,
            "stdout_tail": (p.stdout or "")[-300:]}


def _r(ok, code, data=None, evidence="", fencing=None):
    return SP.result(ok, code, data, evidence, fencing)


def _hard_stop(hit: dict) -> bool:
    """识别 user-control hard stop（01-R1 b2 实测语义）。"""
    blob = json.dumps(hit, ensure_ascii=False).lower()
    return ("agentdelegatedtouser" in blob
            or "stopforusercontrol" in blob
            or "probeusercontrolreason" in blob
            or "no longer assigned to the agent" in blob)


# ------------------------------------------------------------- 后端 API
# （全部返回星舵 Result；SDK 对象不泄漏）

def list_spaces(timeout_s: float = 60) -> dict:
    cap = CAPABILITIES["list_spaces"]
    r = _run_bridge("list_spaces", {}, cap["timeout_s"])
    if not r.get("ok"):
        return _r(False, SP.SP_BE_003, {"raw": r.get("error")}, "bridge failed: %s" % r.get("error"))
    return _r(True, SP.SP_OK, r["data"], "real listTaskSpaces()")


def probe_ownership(taskspace_id, timeout_s: float = 60,
                    retries: int = 2, retry_delay_s: float = 1.2) -> str:
    """ExecutionGate 门 2 的 ownership probe（真实 listTaskSpaces 全局态）。

    返回 "agent" | "user" | "agentDelegatedToUser" | "unknown" | "not_found"。
    fail-closed 约定：任何失败/不完整都归 "unknown"（Gate 会拒绝）。
    实测：刚创建的空间在全局列表中有传播延迟，not_found 短重试 2 次
    （只重试查询，不重试任何副作用）。
    """
    for attempt in range(max(1, retries + 1)):
        r = _run_bridge("space_probe", {"spaceId": taskspace_id}, timeout_s)
        if r.get("ok"):
            data = r.get("data") or {}
            if data.get("found"):
                space = data.get("global") or data.get("space") or {}
                own = space.get("ownership")
                if own in ("agent", "user", "agentDelegatedToUser"):
                    return own
                return "unknown"
            if attempt < retries:
                time.sleep(retry_delay_s)
                continue
            return "not_found"
        if attempt < retries:
            time.sleep(retry_delay_s)
            continue
        return "unknown"


def create_space(name: str, timeout_s: float = 90) -> dict:
    r = _run_bridge("create_space", {"name": name}, timeout_s)
    if not r.get("ok"):
        if _hard_stop(r):
            return _r(False, SP.SP_BE_005, {"raw": r.get("error")},
                      "hard stop during create — no reclaim, no retry")
        return _r(False, SP.SP_BE_003, {"raw": r.get("error")},
                  "bridge failed: %s" % r.get("error"))
    sp = (r.get("data") or {}).get("space") or {}
    return _r(True, SP.SP_OK,
              {"taskspace_id": sp.get("spaceId"), "name": sp.get("name"),
               "ownership": sp.get("ownership"), "raw_keys": sp.get("rawKeys")},
              "TaskSpace created via ego nodejs", None)


def page_exists(taskspace_id, label: str) -> dict:
    r = _run_bridge("page_exists", {"spaceId": taskspace_id, "label": label}, 60)
    if not r.get("ok"):
        return _r(False, SP.SP_BE_003, {"raw": r.get("error")}, "bridge failed")
    return _r(True, SP.SP_OK, r.get("data"), "page lookup")


def page_identity(taskspace_id, label: str) -> dict:
    """真实页面 identity_tuple 采集（不读缓存，不用上次结果补齐）。"""
    r = _run_bridge("page_identity", {"spaceId": taskspace_id, "label": label}, 60)
    if not r.get("ok"):
        # 页面读取失败 → 不补齐，显式失败（任务书 §6.1）
        return _r(False, SP.SP_GATE_010, {"raw": r.get("error")},
                  "page identity probe failed: %s" % r.get("error"))
    ident = (r.get("data") or {}).get("identity")
    if not ident or ident.get("readyState") not in ("complete", "interactive"):
        return _r(False, SP.SP_GATE_010, {"raw": ident},
                  "identity incomplete or page not ready")
    return _r(True, SP.SP_OK, ident, "live page identity")


def page_viewport(taskspace_id, label: str) -> dict:
    """R3-B：页面渲染 viewport 只读探针（真实输入的环境前置条件）。"""
    r = _run_bridge("page_viewport", {"spaceId": taskspace_id,
                                      "label": label}, 60)
    if not r.get("ok"):
        return _r(False, SP.SP_BE_003, {"raw": r.get("error")},
                  "viewport probe failed: %s" % r.get("error"))
    return _r(True, SP.SP_OK, r.get("data"), "live page viewport")


def page_op(op: str, taskspace_id, label: str, timeout_s: float | None = None,
            **args) -> dict:
    """页面操作（goto/fill/click/press/read_text/snapshot）。

    side_effect_possible=true 的 op 必须由 ExecutionGate 调度后才允许进入
    本函数；本函数自身只做能力校验 + 桥接 + 错误映射。
    """
    cap = CAPABILITIES.get(op)
    if cap is None or not cap.get("supported"):
        return _r(False, SP.SP_BE_002 if cap else SP.SP_BE_001,
                  {"op": op}, "capability not declared / unsupported")
    t = timeout_s or cap["timeout_s"]
    # R3-C：input_mode 必须在能力声明范围内（未声明 → SP-BE-002 拒绝，
    # 不绕 Gate、不静默降级到默认模式）
    im = args.get("input_mode", cap.get("input_mode", "dom_synthesized"))
    accepted = cap.get("accepted_input_modes")
    if accepted is not None and im not in accepted:
        return _r(False, SP.SP_BE_002,
                  {"op": op, "input_mode": im,
                   "accepted": list(accepted)},
                  "input_mode not declared for this op — refusing "
                  "(no silent fallback, no Gate bypass)")
    payload = {"spaceId": taskspace_id, "label": label}
    payload.update(args)
    payload["input_mode"] = im  # R3-C：解析后的模式注入桥接（bridge 据此分流）
    if str(im).startswith("trusted"):
        # R3-S（GPT 终审 §4.1）：trusted 环境可用性探针——API supported ≠
        # 当前环境 available。窗口未渲染（viewport 0×0）时 CDP Input 管线
        # 阻塞（R3-B diag0 实测），必须显式拒绝（SP-BE-007）而非超时
        # UNKNOWN；探针失败同样 fail-closed。合成模式不做此探针。
        vr = _run_bridge("page_viewport", {"spaceId": taskspace_id,
                                           "label": label}, 60)
        vdata = vr.get("data") or {}
        vw, vh = vdata.get("innerW"), vdata.get("innerH")
        if not vr.get("ok") or (vw == 0 and vh == 0):
            return _r(False, SP.SP_BE_007,
                      {"input_mode": im, "availability": "unavailable",
                       "reason": ("VIEWPORT_UNAVAILABLE" if vr.get("ok")
                                  else "PROBE_FAILED"),
                       "viewport": ({"w": vw, "h": vh} if vr.get("ok")
                                    else None)},
                      "trusted input requires a rendered window viewport — "
                      "refused before dispatch (no UNKNOWN inferred)")
    r = _run_bridge(op, payload, t)
    if not r.get("ok"):
        if _hard_stop(r):
            return _r(False, SP.SP_BE_005, {"raw": r.get("error")},
                      "user-control hard stop — stop chain, no reclaim/retry")
        if r.get("unclear"):
            res = _r(False, SP.SP_BE_004, {"raw": r.get("error")},
                     "RESULT_UNKNOWN — dispatched outcome unclear", None)
            # dispatch 契约（execution_gate.dispatch docstring）：backend_fn
            # 返回 {"ok", "result", "unknown"}；unknown=True → 账本
            # SEND_UNKNOWN，绝不推断 SENT/NOT_SENT
            res["unknown"] = True
            return res
        return _r(False, SP.SP_BE_003, {"raw": r.get("error")},
                  "bridge failed: %s" % r.get("error"))
    data = r.get("data") or {}
    ident = data.get("identity")
    if _HARDCODED_PAGE_CMDS and op in _HARDCODED_PAGE_CMDS:
        data = dict(data, identity=ident)
    # R3-C：结果分层（四层语义分开，页面状态复验 ≠ 业务成功 ≠ 真实交互语义）
    #   call_ok              —— 调用成功
    #   dom_changed          —— 桥接侧实测 DOM 状态发生变化
    #   page_state_confirmed —— identity 重采集成功（页面可观测）
    #   trusted_pipeline     —— 是否经 SDK 真实输入管线（isTrusted 语义）
    #                           逐事件 isTrusted 证据只在有埋点的夹具页可证
    data = dict(data,
                input_mode=im,
                verification={
                    "call_ok": True,
                    "dom_changed": bool(data.get("dom_changed")),
                    "page_state_confirmed": bool(ident),
                    "trusted_pipeline": im == "trusted",
                })
    return _r(True, SP.SP_OK, data, "%s via ego nodejs" % op)


def hand_off(taskspace_id) -> dict:
    r = _run_bridge("hand_off", {"spaceId": taskspace_id}, 90)
    if not r.get("ok"):
        if _hard_stop(r):
            return _r(False, SP.SP_BE_005, {"raw": r.get("error")},
                      "hard stop during handOff")
        return _r(False, SP.SP_BE_003, {"raw": r.get("error")}, "bridge failed")
    return _r(True, SP.SP_OK, r.get("data"), "handOff resolved (cooperative; "
              "NOT proof of action success — 01-R1 F-b2)")


def wait_control(taskspace_id, timeout_ms: int = 10000) -> dict:
    r = _run_bridge("wait_control", {"spaceId": taskspace_id,
                                     "timeoutMs": timeout_ms}, 60)
    if not r.get("ok"):
        return _r(False, SP.SP_BE_003, {"raw": r.get("error")}, "bridge failed")
    return _r(True, SP.SP_OK, r.get("data"), "waitForControl resolved")


def finish_space(taskspace_id, keep=None) -> dict:
    """finish 生命周期（任务书 §8.2：默认业务流程禁止，仅一次性隔离空间）。"""
    cap = CAPABILITIES["finish_space"]
    r = _run_bridge("finish_space", {"spaceId": taskspace_id,
                                     "keep": keep or []}, cap["timeout_s"])
    if not r.get("ok"):
        if _hard_stop(r):
            return _r(False, SP.SP_BE_005, {"raw": r.get("error")},
                      "hard stop during finish")
        return _r(False, SP.SP_BE_003, {"raw": r.get("error")}, "bridge failed")
    code = SP.SP_OK if (keep or []) else SP.SP_LIFE_002
    return _r(True, code, r.get("data"),
              "finish({keep:%r}) executed on isolated space" % (keep or []))


# ------------------------------------------------------------- Observation

def build_observation(taskspace_id, label: str, page_generation: str,
                      executor_id: str, fencing_generation: int,
                      ident: dict) -> dict:
    """把真实 identity_tuple 组装成任务书 §6 的 Observation 结构。"""
    fp_src = json.dumps({k: ident.get(k) for k in
                         ("url", "title", "marker", "timeOrigin")},
                        sort_keys=True, ensure_ascii=False)
    fp = "pf-" + hashlib.sha256(fp_src.encode("utf-8")).hexdigest()[:16]
    oid = "obs-" + hashlib.sha256(
        ("%s|%s|%s|%s|%s|%s" % (taskspace_id, label, fp, page_generation,
                                executor_id, fencing_generation)
         ).encode("utf-8")).hexdigest()[:16]
    body_len = int(ident.get("bodyLen") or 0)
    if body_len <= 0:
        health = "unknown"            # 不可靠判断 → unknown，不猜（§6.2）
    elif body_len < 200:
        health = "degraded"
    else:
        health = "usable"
    return {
        "observation_id": oid,
        "taskspace_id": taskspace_id,
        "page_label": label,
        "page_generation": page_generation,
        "page_fingerprint": fp,
        "executor_id": executor_id,
        "fencing_generation": int(fencing_generation),
        "observed_at": time.time(),
        "url": ident.get("url"),
        "title": ident.get("title"),
        "marker": ident.get("marker"),
        "timeOrigin": ident.get("timeOrigin"),
        "ready_state": ident.get("readyState"),
        "page_health": health,
        "source": "ego_bridge.page_identity (real page evaluate)",
        "collection": "live evaluate; no cache fill",
    }


def verify_observation(taskspace_id, label: str, observation: dict) -> dict:
    """Gate 门 3 的 observation 校验：重新真实采集并逐字段比对。"""
    r = page_identity(taskspace_id, label)
    if not r["ok"]:
        return r
    ident = r["data"]
    fp_src = json.dumps({k: ident.get(k) for k in
                         ("url", "title", "marker", "timeOrigin")},
                        sort_keys=True, ensure_ascii=False)
    fp = "pf-" + hashlib.sha256(fp_src.encode("utf-8")).hexdigest()[:16]
    if fp != observation.get("page_fingerprint"):
        return _r(False, SP.SP_GATE_009,
                  {"live": fp, "bound": observation.get("page_fingerprint")},
                  "live fingerprint != observation fingerprint — old observation invalid")
    return _r(True, SP.SP_OK, {"observation_id": observation.get("observation_id")},
              "observation re-verified against live page")


# ------------------------------------------------------------- Gate 集成

def make_gate(lock_path: str, ledger, page_identity_mgr,
              resource: str = "", probe_timeout_s: float = 60, plane=None):
    """构建绑定真实 Ego ownership probe / observation 校验的 ExecutionGate。

    注意：page_identity_mgr（星舵代次账本）与真实页面采集结合 ——
    verify 时既查星舵绑定代次（pageidentity.verify）也查真实页面指纹。

    plane: P2-2-4 派发前屏障配置（SecurityPlane）。缺省 None 时**不构造**
    默认 plane —— 保持 fail-closed：副作用操作将在 dispatch 处被
    SP-OP-001 拒绝，而不是静默降级成无签名/无 Marker 派发。真实调用方
    必须显式传入。
    """
    import execution_gate

    def ownership_probe(taskspace_id):
        return probe_ownership(taskspace_id, probe_timeout_s)

    def observation_verify(observation):
        return verify_observation(observation.get("taskspace_id"),
                                  observation.get("page_label"), observation)

    return execution_gate.ExecutionGate(
        lock_path, ledger, page_identity_mgr,
        ownership_probe=ownership_probe, resource=resource or lock_path,
        observation_verify=observation_verify, plane=plane)
