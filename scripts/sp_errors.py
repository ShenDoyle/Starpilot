#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sp_errors.py —— 星舵统一错误码锚点（PROJECT-02-R1 ARCHITECTURE §3 的代码层唯一定义）。

规范来源：project-02-r1-report/PROJECT-02-R1-ARCHITECTURE.md §3（31 个 SP-*-NNN）。
任何模块需要错误码时 import 本文件，**不得在别处重新定义字符串**。
 gate_lint.py 会校验错误码只从这里流出。

Result<T> 统一返回形状（风格锚点，ARCHITECTURE §5）：

    Result = {ok: bool, code: str, data: dict|None, evidence: str, fencing_generation: int}

`ok=True` 时 code 为 "SP-OK"；失败时 code 为具体 SP 错误码，evidence 是
人类可读的一行事实描述（不吞栈、不改写）。
"""
from __future__ import annotations


def result(ok: bool, code: str, data: dict | None = None, evidence: str = "",
           fencing_generation: int | None = None) -> dict:
    """构造统一 Result。fencing_generation 缺省时不在返回里伪造（键缺省）。"""
    r = {"ok": bool(ok), "code": code, "data": data or {},
         "evidence": evidence}
    if fencing_generation is not None:
        r["fencing_generation"] = int(fencing_generation)
    return r


def ok_result(data: dict | None = None, evidence: str = "",
              fencing_generation: int | None = None) -> dict:
    return result(True, SP_OK, data, evidence, fencing_generation)


# ---------------------------------------------------------------- 成功
SP_OK = "SP-OK"

# --------------------------------------------------- 3.1 执行权 / 锁域
SP_GATE_001 = "SP-GATE-001"  # LOCK_BUSY           锁被其它进程持有
SP_GATE_002 = "SP-GATE-002"  # LOCK_OWNER_ALIVE    持有者存活（禁止接管）
SP_GATE_003 = "SP-GATE-003"  # LOCK_OWNER_DEAD     持有者确认死亡（允许接管）
SP_GATE_004 = "SP-GATE-004"  # LOCK_STATE_UNKNOWN  状态不明（fail-closed）
SP_GATE_005 = "SP-GATE-005"  # LOCK_CORRUPT        锁文件损坏（fail-closed）
SP_GATE_006 = "SP-GATE-006"  # LOCK_RECOVERED      已合法接管
SP_GATE_007 = "SP-GATE-007"  # FENCED_OUT          generation 已失效
SP_GATE_008 = "SP-GATE-008"  # OWNERSHIP_LOST      全局所有权非 agent
SP_GATE_009 = "SP-GATE-009"  # PAGE_IDENTITY_CHANGED 页面身份变化
SP_GATE_010 = "SP-GATE-010"  # OBSERVATION_INVALID observation 失效
SP_GATE_011 = "SP-GATE-011"  # GATE_WINDOW_VIOLATION 锁覆盖不完整
SP_GATE_012 = "SP-GATE-012"  # EXECUTOR_NOT_UNIQUE 检测到第二执行者

# --------------------------------------------------- 3.2 账本 / 动作域
SP_ACT_001 = "SP-ACT-001"  # ACTION_DUPLICATE          同 action_id 重复提交
SP_ACT_002 = "SP-ACT-002"  # INTENT_COLLISION          同 intent_key 冲突
SP_ACT_003 = "SP-ACT-003"  # ANTI_REPLAY_LOCKED        UNKNOWN 锁定中
SP_ACT_004 = "SP-ACT-004"  # RESERVATION_EXHAUSTED     动作额度耗尽
SP_ACT_005 = "SP-ACT-005"  # OUTCOME_MISSING           崩溃后 outcome 缺失
SP_ACT_006 = "SP-ACT-006"  # TASK_ID_MISMATCH          跨 run 继承校验失败
SP_ACT_007 = "SP-ACT-007"  # BUDGET_INVARIANT_BROKEN   预占/实际次数不守恒
SP_ACT_008 = "SP-ACT-008"  # INTENT_REPLAY_NEEDS_NEW_IDENTITY  同一业务意图已 SENT 终结，禁止原样重放
SP_ACT_009 = "SP-ACT-009"  # BUSINESS_TARGET_PENDING_UNKNOWN  业务对象存在未决 UNKNOWN，新意图身份不构成执行授权（R3-S GPT 终审 §3.3；P2-0 终审 T09：authorization_ref 不构成绕过）
SP_OP_002 = "SP-OP-002"  # ENVELOPE_IDENTITY_CONFLICT  同一 op_id 身份冲突（DELEGATED 不覆盖，P2-2-3）
SP_OP_001 = "SP-OP-001"  # ENVELOPE_INVALID  envelope 校验失败/验签拒绝（fail-closed，P2-2-1）
SP_ACT_010 = "SP-ACT-010"  # BUSINESS_TARGET_REQUIRED  side-effect 动作 business_target 缺失/空/非法（fail-closed，P2-0 终审 T07 / P1-A §68）
SP_ACT_011 = "SP-ACT-011"  # FENCING_OWNER_UNPROVEN  账本记录该动作有 fencing 所有权，但调用方未声明 expected_fencing（SP1-A 下沉校验）
SP_ACT_012 = "SP-ACT-012"  # FENCING_MISMATCH        调用方声称的 fencing 与账本不一致（旧持有者/伪造，SP1-A）
SP_ACT_013 = "SP-ACT-013"  # FENCING_TAKEOVER_DENIED 接管前置不满足（状态不可接管 / CAS 失败 / 执行者非法，SP1-A）
SP_ACT_014 = "SP-ACT-014"  # ADJUDICATION_CONSUMED  裁决已消费或无法再次消费（SP3-1）
SP_ACT_015 = "SP-ACT-015"  # ADJUDICATION_DECISION_INVALID  裁决决定/证据/复核条件不合法（SP3-1）
SP_ACT_016 = "SP-ACT-016"  # ADJUDICATION_STATE_INVALID  action 当前状态不允许执行该裁决（SP3-1）

# --------------------------------------------------- 3.3 后端 / 能力域
SP_BE_001 = "SP-BE-001"  # UNSUPPORTED
SP_BE_002 = "SP-BE-002"  # CAPABILITY_NOT_DECLARED
SP_BE_003 = "SP-BE-003"  # BACKEND_DISCONNECTED
SP_BE_004 = "SP-BE-004"  # RESULT_UNKNOWN（不得推断成功/失败）
SP_BE_005 = "SP-BE-005"  # HARD_STOP（如 Ego agentDelegatedToUser）
SP_BE_006 = "SP-BE-006"  # BACKEND_SEMANTIC_MISMATCH
SP_BE_007 = "SP-BE-007"  # TRUSTED_INPUT_ENV_UNAVAILABLE  trusted 输入当前运行环境不可用（如 viewport 0×0，R3-S GPT 终审 §4.1）

# --------------------------------------------------- 3.4 生命周期域
SP_LIFE_001 = "SP-LIFE-001"  # FINISH_FORBIDDEN
SP_LIFE_002 = "SP-LIFE-002"  # FINISH_DESTROYS_SPACE
SP_LIFE_003 = "SP-LIFE-003"  # FINISH_TRANSFERS_TO_USER
SP_LIFE_004 = "SP-LIFE-004"  # LABEL_GONE
SP_LIFE_005 = "SP-LIFE-005"  # RECLAIM_REQUIRES_AUTHORIZATION
SP_LIFE_006 = "SP-LIFE-006"  # BROWSER_RESTART_UNVERIFIED

# --------------------------------------------------- 错误码 → 名称映射
# 名称与 ARCHITECTURE §3 表格一一对应；报告与测试用名称，代码用常量。
CODE_NAMES = {
    SP_GATE_001: "LOCK_BUSY",
    SP_GATE_002: "LOCK_OWNER_ALIVE",
    SP_GATE_003: "LOCK_OWNER_DEAD",
    SP_GATE_004: "LOCK_STATE_UNKNOWN",
    SP_GATE_005: "LOCK_CORRUPT",
    SP_GATE_006: "LOCK_RECOVERED",
    SP_GATE_007: "FENCED_OUT",
    SP_GATE_008: "OWNERSHIP_LOST",
    SP_GATE_009: "PAGE_IDENTITY_CHANGED",
    SP_GATE_010: "OBSERVATION_INVALID",
    SP_GATE_011: "GATE_WINDOW_VIOLATION",
    SP_GATE_012: "EXECUTOR_NOT_UNIQUE",
    SP_ACT_001: "ACTION_DUPLICATE",
    SP_ACT_002: "INTENT_COLLISION",
    SP_ACT_003: "ANTI_REPLAY_LOCKED",
    SP_ACT_004: "RESERVATION_EXHAUSTED",
    SP_ACT_005: "OUTCOME_MISSING",
    SP_ACT_006: "TASK_ID_MISMATCH",
    SP_ACT_007: "BUDGET_INVARIANT_BROKEN",
    SP_ACT_008: "INTENT_REPLAY_NEEDS_NEW_IDENTITY",
    SP_ACT_009: "BUSINESS_TARGET_PENDING_UNKNOWN",
    SP_ACT_010: "BUSINESS_TARGET_REQUIRED",
    SP_ACT_011: "FENCING_OWNER_UNPROVEN",
    SP_ACT_012: "FENCING_MISMATCH",
    SP_ACT_013: "FENCING_TAKEOVER_DENIED",
    SP_ACT_014: "ADJUDICATION_CONSUMED",
    SP_ACT_015: "ADJUDICATION_DECISION_INVALID",
    SP_ACT_016: "ADJUDICATION_STATE_INVALID",
    SP_OP_001: "ENVELOPE_INVALID",
    SP_OP_002: "ENVELOPE_IDENTITY_CONFLICT",
    SP_BE_001: "UNSUPPORTED",
    SP_BE_002: "CAPABILITY_NOT_DECLARED",
    SP_BE_003: "BACKEND_DISCONNECTED",
    SP_BE_004: "RESULT_UNKNOWN",
    SP_BE_005: "HARD_STOP",
    SP_BE_006: "BACKEND_SEMANTIC_MISMATCH",
    SP_BE_007: "TRUSTED_INPUT_ENV_UNAVAILABLE",
    SP_LIFE_001: "FINISH_FORBIDDEN",
    SP_LIFE_002: "FINISH_DESTROYS_SPACE",
    SP_LIFE_003: "FINISH_TRANSFERS_TO_USER",
    SP_LIFE_004: "LABEL_GONE",
    SP_LIFE_005: "RECLAIM_REQUIRES_AUTHORIZATION",
    SP_LIFE_006: "BROWSER_RESTART_UNVERIFIED",
}
