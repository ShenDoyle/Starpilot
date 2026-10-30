#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""orchestrator.py —— 星舵循环编排器（O1 编排核心 / O2 恢复与预算 / O4 收口回流）。

设计前提（来自 `docs/orchestrator-contract.md`）：

- **按需拉起、单次循环**，不是常驻 daemon。`loop --once` 推进一个状态步，`loop --round` 推完一轮。
- **平台会忘**：每轮重读 `state/current_goal.md`、`state/contract.md`、`stage/<id>/handoff.md`，
  绝不假设上一轮还在平台上下文里。
- **真实写只能走写闸门**（`scripts/write_gate.py`）。本文件不自己实现发送——
  它只调闸门 CLI，这样「没批就不许写」是进程级的硬约束，不是口头约定。
- **状态机守卫不过就拒绝迁移**，并把失败原因记进 journal，绝不硬闯。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import confirm  # noqa: E402
import platforms  # noqa: E402
import wa  # noqa: E402
import write_gate as wg  # noqa: E402

CONTRACT = "docs/orchestrator-contract.md"

# ---------------------------------------------------------------- 状态机

STATES = [
    "INIT", "BOUND", "PLAN_OK", "READY", "DRY_RUN_OK", "APPROVED",
    "SENDING", "AWAITING_REPLY", "REPLY_VERIFIED", "ROUND_DONE",
    "PAUSED_HUMAN", "RECOVERING", "HANDOFF", "CLOSED",
]

TRANSITIONS = {
    "INIT": ["BOUND"],
    "BOUND": ["PLAN_OK"],
    "PLAN_OK": ["READY", "PAUSED_HUMAN"],
    "READY": ["DRY_RUN_OK", "PAUSED_HUMAN"],
    "DRY_RUN_OK": ["APPROVED", "PAUSED_HUMAN"],
    "APPROVED": ["SENDING", "PAUSED_HUMAN"],
    "SENDING": ["AWAITING_REPLY", "PAUSED_HUMAN", "HANDOFF"],
    "AWAITING_REPLY": ["REPLY_VERIFIED", "ROUND_DONE", "PAUSED_HUMAN", "HANDOFF"],
    "REPLY_VERIFIED": ["ROUND_DONE"],
    "ROUND_DONE": ["READY", "HANDOFF", "CLOSED", "PAUSED_HUMAN"],
    "PAUSED_HUMAN": ["READY", "AWAITING_REPLY", "CLOSED", "HANDOFF"],
    "RECOVERING": ["READY", "AWAITING_REPLY", "REPLY_VERIFIED", "PAUSED_HUMAN", "HANDOFF"],
    "HANDOFF": ["CLOSED"],
    "CLOSED": [],
}

DEFAULT_GOAL = {
    "max_rounds": 12,
    "max_same_error": 2,
    "round_timeout": 300,
    "send_timeout": 60,
}

# ---------------------------------------------------------------- 迁移合法性矩阵
# 契约 §2.2 的落地：正向迁移 agent 可自走，**修补型迁移必须声明 actor**。
# 评审指出的漏洞：以前守卫只写在 if-else 里，崩溃恢复时可能「恢复到一个不该回去的状态」，
# 而且没人能回答「已转交（HANDOFF）之后能不能回本地重试」。
# 现在这条答案写在代码里：HANDOFF 只能去 CLOSED（收口），回不去 READY。
REPAIR_EDGES = {
    ("PAUSED_HUMAN", "READY"): {"human", "recovery"},
    # 「已经发过了、只是没等到判定」——回到等待态继续等，**不重发**。
    # 只给 human/recovery，不给 agent：agent 不能自己绕过写闸门把状态往前推。
    ("PAUSED_HUMAN", "AWAITING_REPLY"): {"human", "recovery"},
    ("PAUSED_HUMAN", "CLOSED"): {"human"},
    ("PAUSED_HUMAN", "HANDOFF"): {"human", "agent"},
    ("RECOVERING", "READY"): {"recovery"},
    ("RECOVERING", "AWAITING_REPLY"): {"recovery"},
    ("RECOVERING", "REPLY_VERIFIED"): {"recovery"},
    ("RECOVERING", "PAUSED_HUMAN"): {"recovery"},
    ("RECOVERING", "HANDOFF"): {"recovery"},
}
IRREVERSIBLE_STATES = ("CLOSED",)
ACTORS = ("agent", "human", "recovery")

# 失败类型 → 处置建议（写进 lesson 候选的 fix 字段）
FIX_HINTS = {
    "send_unconfirmed": "换用更强的发送确认信号（消息数/地址/正文指纹三选一），别把「输入框清空」当成功",
    "write_len_mismatch": "只重写一次，别分段续写（会造重复段）；确认写入路径是该平台已实测的那条",
    "fill_failed": "先确认写入路径是该平台**已实测**的那条：受控编辑器（Quill 一类）可能拒绝 "
                   "execCommand('insertText')——字进了 DOM 不进 model。这类平台必须走 native"
                   "（真实键盘），判据看空态类与发送键，不看 innerText",
    "fill_not_accepted": "改走平台**已实测**的写入路径（受控编辑器如 Quill 只认真实键盘 native）；"
                         "判据看空态类与发送键，不看 innerText",
    "click_send_failed": "确认发送按钮不是 disabled 变体；用通用 isDisabled 判定",
    "probe_failed": "先 wa.py probe 看页面是否改版/跳转；probe 报 editor:false 是 DOM 变了，不是平台坏了",
    "reply_timeout": "轮次超时：不重发，先 probe 看 generating / last_len，再决定是否转人工",
    "guard_failed": "把守卫原因写进 journal，人工判断是页面状态问题还是 DOM 改版",
    "unknown": "按证据判断，不猜",
}

# 触发条件 / 回退方式（评审：lessons 缺「症状/触发条件/判据/回退」四要素）
TRIGGER_HINTS = {
    "send_unconfirmed": "发送后三选一主信号都不满足（消息数/地址/正文指纹）",
    "write_len_mismatch": "写入后编辑器字数 < 正文 95%",
    "fill_failed": "js_fill 返回 no-editor 或抛错",
    "fill_not_accepted": "字写进 DOM 但编辑器 model 是空的（空态类还在 / 发送键不出现）",
    "click_send_failed": "发送键命中但被 isDisabled 判为不可点",
    "probe_failed": "read_state 连续三次读不到消息或 editor=false",
    "reply_timeout": "wait_for_reply 超过 round_timeout 仍未稳定",
    "guard_failed": "DOM 守卫或 URL hash 守卫不通过",
    "unknown": "任何未归类的失败",
}
ROLLBACK_HINTS = {
    "send_unconfirmed": "退回只记录不判失败（INCONCLUSIVE），人工看页面后决定，禁止自动重发",
    "write_len_mismatch": "清空编辑器重写一次；仍不符则停，不续写",
    "fill_not_accepted": "停，不点发送（点了也是发空气）；换实测写入路径后重跑 dry-run",
    "probe_failed": "回退到上一次成功的 DOM 快照判定，并转 PAUSED_HUMAN",
    "reply_timeout": "不重发；再跑一次 wait 或 probe 看 generating",
    "guard_failed": "转 PAUSED_HUMAN，等人确认页面状态；不换选择器硬试",
    "unknown": "转 PAUSED_HUMAN，把原文报给网页 AI 裁定",
}

STEP_HINT = {
    "INIT": "需要先 bind 一个对话（orchestrator.py bind --url ... 或 --new）",
    "BOUND": "需要落盘规划产物（orchestrator.py plan --file ...）",
    "PLAN_OK": "下一步：DOM 守卫检查",
    "READY": "下一步：写闸门出计划（loop --once --file <正文>）",
    "DRY_RUN_OK": "人工卡点：需要批准（orchestrator.py approve --approval ...）",
    "APPROVED": "下一步：走闸门真发（需要 STARPILOT_WRITE_MODE=single_write）",
    "SENDING": "下一步：等回复确认",
    "AWAITING_REPLY": "下一步：等回复完成",
    "REPLY_VERIFIED": "下一步：落盘回复",
    "ROUND_DONE": "下一轮：loop --once --file <下一轮正文>",
    "PAUSED_HUMAN": "人工卡点：等指令",
    "HANDOFF": "已转交：等人工接手",
    "CLOSED": "阶段已收口",
}

# ---------------------------------------------------------------- 落盘


def run_dir(root: str, run_id: str = "") -> str:
    return wg.run_dir_of(root, run_id)


def load_run_state(root: str, run_id: str = "") -> dict:
    d = run_dir(root, run_id)
    p = os.path.join(d, "state.json")
    if not os.path.exists(p):
        raise SystemExit("这个 run 还没有 state.json：%s" % d)
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_run_state(root: str, st: dict, run_id: str = "") -> None:
    st["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    d = run_dir(root, st.get("run_id", run_id or ""))
    wg.atomic_write_json(os.path.join(d, "state.json"), st)


def journal(root: str, st: dict, event: dict) -> dict:
    """记一条 journal 事件（带完整审计键）。"""
    ev = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "run_id": st.get("run_id", ""),
        "round_id": st.get("round", 0),
        "target_url_sha256": st.get("target_url_sha256", ""),
        "prompt_sha256": (st.get("pending") or {}).get("prompt_sha256", ""),
        "action": event.get("action", ""),
        "before": event.get("before"),
        "after": event.get("after"),
        "result": event.get("result", "ok"),
        "error_class": event.get("error_class", ""),
    }
    skip = ("ts", "run_id", "round_id", "target_url_sha256", "prompt_sha256",
            "action", "before", "after", "result", "error_class")
    ev.update({k: v for k, v in event.items() if k not in skip})
    wg.append_journal(run_dir(root, st.get("run_id", "")), ev)
    return ev


def write_evidence(root: str, st: dict, name: str, obj: dict) -> str:
    d = run_dir(root, st.get("run_id", ""))
    path = os.path.join(d, "evidence", name)
    wg.atomic_write_json(path, obj)
    return path


def transition(root: str, st: dict, to: str, reason: str, event: dict = None,
               by: str = "agent") -> bool:
    """守卫式状态迁移。非法迁移一律拒绝并记 journal。

    `by` 是发起者（agent / human / recovery）：修补型迁移（见 REPAIR_EDGES）
    必须由被允许的 actor 发起，否则拒绝——这条把「谁能把状态往回推」写死，
    不再靠调用方自觉。
    """
    cur = st.get("state", "INIT")
    need = REPAIR_EDGES.get((cur, to))
    if need and by not in need:
        st["last_guard_failure"] = {
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "from": cur, "to": to, "by": by,
            "reason": "修补型迁移 %s→%s 只允许 %s 发起（本次 %s）"
                      % (cur, to, "/".join(sorted(need)), by),
            "legal": TRANSITIONS.get(cur, []),
        }
        save_run_state(root, st)
        journal(root, st, {"action": "transition", "result": "transition_rejected",
                           "error_class": "actor_not_allowed", "from": cur, "to": to,
                           "by": by, "guard_reason": reason})
        return False
    if to not in TRANSITIONS.get(cur, []):
        st["last_guard_failure"] = {
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "from": cur, "to": to, "by": by, "reason": reason,
            "legal": TRANSITIONS.get(cur, []),
        }
        save_run_state(root, st)
        journal(root, st, {"action": "transition", "result": "transition_rejected",
                           "error_class": "guard_failed", "from": cur, "to": to,
                           "by": by, "guard_reason": reason})
        return False
    # 修补型迁移还要**证据**，不只身份：光「人是被允许的 actor」挡不住诱导，
    # 「上一次发送没判清」才挡得住（网页 AI 第 2 轮裁定 1）。
    pok, pwhy = repair_precondition(st, to)
    if not pok:
        st["last_guard_failure"] = {
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "from": cur, "to": to, "by": by, "reason": pwhy,
            "checkpoint": "repair_precondition",
        }
        save_run_state(root, st)
        journal(root, st, {"action": "transition", "result": "transition_rejected",
                           "error_class": "precondition_failed", "from": cur, "to": to,
                           "by": by, "guard_reason": pwhy})
        return False
    st.setdefault("state_history", []).append({
        "from": cur, "to": to, "by": by,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"), "reason": reason,
    })
    st["state"] = to
    st["last_guard_failure"] = None
    save_run_state(root, st)
    journal(root, st, {"action": "transition", "result": "ok", "from": cur, "to": to,
                       "by": by, "guard_reason": reason})
    return True


# ---------------------------------------------------------------- 上下文重读


def reread_context(root: str, st: dict) -> dict:
    """每轮重读：目标、契约、阶段交接。**绝不假设平台还记得。**"""
    files = {
        "goal": os.path.join(root, "state", "current_goal.md"),
        "contract": os.path.join(root, "state", "contract.md"),
        "handoff": os.path.join(root, "stage", st.get("stage_id", ""), "handoff.md"),
    }
    out = {}
    for k, path in files.items():
        rel = os.path.relpath(path, root) if path else ""
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                txt = f.read()
            out[k] = {"path": rel, "chars": len(txt), "sha256": wg.sha16(txt)}
        else:
            out[k] = {"path": rel, "chars": 0, "sha256": ""}
    return out


# ---------------------------------------------------------------- 工具


def wa_state_path(root: str, explicit: str = "") -> str:
    return explicit or os.path.join(root, ".wa-state.json")


def platform_of(root: str, explicit: str = "") -> dict:
    st = wa.load_state(wa_state_path(root, explicit))
    return platforms.get(explicit or st.get("platform"))


def session_of(root: str, explicit: str = "") -> tuple:
    """返回 (sid, wa_state)。会话复用 wa 的 state。"""
    path = wa_state_path(root, explicit)
    st = wa.load_state(path)
    if not st.get("url"):
        raise SystemExit("还没绑定对话：先 wa.py bind（或者 orchestrator.py bind）")
    sid = wa.ensure_session(st, path)
    return sid, st


def run_gate(root: str, args: list, env_extra: dict = None, state: str = "") -> tuple:
    """调写闸门 CLI（子进程）。返回 (rc, 解析后的 JSON 或原始文本)。

    **显式传 `--state`**：闸门是子进程，cwd 不等于 workspace，
    不传它就会去读子进程 cwd 的 state（那是另一个对话的绑定）。
    """
    cmd = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "write_gate.py"),
           "--workspace", root]
    if state:
        cmd += ["--state", state]
    cmd += args
    env = dict(os.environ)
    env.update(env_extra or {})
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=900, env=env)
    out = (p.stdout or "").strip()
    try:
        return p.returncode, json.loads(out)
    except Exception:
        return p.returncode, {"raw": out, "stderr": (p.stderr or "")[:400]}


# ---------------------------------------------------------------- fail_streak 三值语义

# verdict / error_class → 对 fail_streak 的作用（契约 §2.4）。
# 三值语义：**有结论的失败 +1；有结论的成功清零；无结论（0）不动**。
# 「恢复」属于无结论，所以恢复不改 streak（§3.2）；等待成功属于有结论的成功，所以清零。
# 任何新增 verdict 必须在这里归类，否则本表查不到 —— 这是刻意的：
# 不许出现「新加了一个判定结果，但没人想清楚它算不算失败」。
VERDICT_EFFECT = {
    # 有结论的成功
    "SENT": "clear",
    "SENT_NO_REPLY": "clear",
    "SENT_MULTI": "clear",
    # 无结论（既不能报成功也不能报失败 → 人工复核）
    "INCONCLUSIVE_NOT_SUCCESS": 0,
    "INCONCLUSIVE_COLLISION": 0,
    # 有结论的失败
    "NOT_SENT": 1,
    "fill_failed": 1,
    "fill_not_accepted": 1,
    "write_len_mismatch": 1,
    "click_send_failed": 1,
    "probe_failed": 1,
    "probe_failed_after_fill": 1,
    "reply_timeout": 1,
    "guard_failed": 1,
    "rule6_violation": 1,
    "send_unconfirmed": 1,
}

INCONCLUSIVE_VERDICTS = ("INCONCLUSIVE_NOT_SUCCESS", "INCONCLUSIVE_COLLISION")


def verdict_effect(verdict: str):
    """查表：'clear'（成功，清零）/ 0（无结论，不动）/ 1（失败，+1）。

    查不到就是代码与契约脱节：**按失败处理**（宁可停，不可静默放过），
    并在返回值里显式暴露 unknown，让审计看得见。
    """
    v = (verdict or "").strip()
    if v in VERDICT_EFFECT:
        return VERDICT_EFFECT[v]
    return 1


def apply_verdict_effect(st: dict, verdict: str) -> dict:
    """fail_streak 的**唯一写入口**。别处不许直接 +=1 / =0。"""
    eff = verdict_effect(verdict)
    before = int(st.get("fail_streak") or 0)
    if eff == "clear":
        st["fail_streak"] = 0
    else:
        st["fail_streak"] = before + int(eff)
    st["last_verdict"] = verdict or "UNKNOWN"
    st["last_verdict_effect"] = eff
    return {"verdict": verdict or "UNKNOWN", "effect": eff,
            "fail_streak_before": before, "fail_streak_after": st["fail_streak"],
            "known": (verdict or "") in VERDICT_EFFECT}


def repair_precondition(st: dict, to: str) -> tuple:
    """修补型迁移的前置断言：光有 actor 不够，还得有**证据**。

    `PAUSED_HUMAN → AWAITING_REPLY` 的语义是「已经发过了，只是没判清，继续等」。
    要证明这个语义成立，必须满足两条：
      1. 有 `last_send.send_id`：能追到具体哪一次发送（没有 = 可能根本没发过）；
      2. 那次发送的 verdict 是 INCONCLUSIVE*：真的没判清。
    若 verdict 已经是 SENT（判清了却没等到回复），那是**真失败**——
    该走 HANDOFF 或按新轮重发，不是原地继续等。这条把「继续等」从
    「人点了就行」变成「有证据才行」，actor 门控挡不住「诱导人点」，证据可以。
    """
    if (st.get("state"), to) != ("PAUSED_HUMAN", "AWAITING_REPLY"):
        return True, ""
    ls = st.get("last_send") or {}
    sid_ = ls.get("send_id") or ""
    if not sid_:
        return False, ("拒绝修补：last_send 没有 send_id，无法证明「确实发过」。"
                       "先 probe 页面确认，再按新轮重发或 HANDOFF，不要原地继续等")
    v = ls.get("verdict") or ""
    if v not in INCONCLUSIVE_VERDICTS:
        return False, ("拒绝修补：上次判定是 %s（已判清）。已判清却没等到回复 = 真失败，"
                       "该走 HANDOFF 或重发，不是继续等" % (v or "UNKNOWN"))
    return True, ""


def reply_count_of(cur: dict) -> int:
    """助手回复条数（平台无关口径）。

    `reply_count` 是探测片段给的「助手消息数」；老状态文件没有这个字段时
    退回 `msg_count`（宁可粗一点，也不能把 0 当成「回复到了」）。
    """
    v = (cur or {}).get("reply_count")
    if v is None:
        v = (cur or {}).get("msg_count")
    try:
        return int(v or 0)
    except Exception:
        return 0


def wait_for_reply(p: dict, sid: str, before_count: int, timeout: int,
                    interval: float = 3.0, before: dict = None) -> dict:
    """等回复完成：**来了新回复** + 不在生成 + 末条正文长度连续两次一致。

    判「来了新回复」有三条见证，任一成立即可（记在返回值的 `witness` 里）：
      1. `anchor`   —— 锚点集合差 / 末条锚点变化（消息身份，最硬）
      2. `count`    —— 锚点不可信时退回计数（`reply_count` +1）
      3. `growth`   —— 同一容器内**流式补全**：锚点看不出新消息，但正文显著变长

    第 3 条是 Gemini 第 5 条裁定点名要的：SPA 把回复流式写进**已有**容器时，
    集合差恒为 0，只有长度能证明「回复真的在长」。代价是「旧回复被展开/重排」
    也会让长度变长——所以它必须同时满足「不在生成 + 连续两次长度不变」，
    并且把 `witness` 如实记下来，事后能分辨这一轮是凭什么判到的。

    为什么不能只数节点：虚拟列表会卸载旧气泡，`reply_count` 可能
    「发送前 2 → 回复后还是 2」，按计数判会白等到超时
    （2026-10-29 端到端实测：回复 2597 字已在页面上，却因为计数没涨被判 reply_timeout）。
    """
    deadline = time.time() + timeout
    prev_len = -1
    stable = 0
    last = {}
    base = dict(before or {})
    base.setdefault("reply_count", before_count or 0)
    while time.time() < deadline:
        ok, cur = wa.read_state(p, sid)
        if not ok:
            time.sleep(interval)
            continue
        last = cur
        anch = platforms.anchor_delta(base, cur)
        grew = confirm.reply_grew(base, cur)
        if anch.get("anchors_available"):
            got_new = bool(anch["new_asst"] >= 1 or anch["last_asst_changed"])
            witness = "anchor"
        else:
            got_new = reply_count_of(cur) >= (before_count or 0) + 1
            witness = "count"
        # 第 3 条见证：同容器流式补全（生成中不算，否则会把半截话当成品）
        got_growth = bool(grew["grew"]) and not cur.get("generating") and not got_new
        got_reply = got_new or got_growth
        if got_reply and not cur.get("generating") and (cur.get("last_len") or 0) > 0:
            if cur.get("last_len") == prev_len:
                stable += 1
                if stable >= 2:
                    return {"ok": True,
                            "signal": "text_stable" if got_new else "text_growth",
                            "witness": witness if got_new else "growth",
                            "chars": cur.get("last_len"),
                            "new_asst": anch.get("new_asst"),
                            "growth": grew,
                            "msg_count": cur.get("msg_count"),
                            "tail": cur.get("last_tail", "")}
            else:
                stable = 0
        else:
            stable = 0
        prev_len = cur.get("last_len", 0) if not cur.get("generating") else -1
        time.sleep(interval)
    return {"ok": False, "error_class": "reply_timeout", "last": last, "waited_s": timeout}


# ---------------------------------------------------------------- init / bind / plan

EXEC_MODES = ("step", "auto")
MODE_HUMAN = {
    "step": "逐步确认：每一轮在真实发送前停下来等你拍板",
    "auto": "无人值守：按既定规划连续跑，只在「连续失败 2 次 / 地址漂移 / 出现新歧义」时停",
}


def _resolve_exec_mode(cli_val: str, card_res: dict = None) -> str:
    """执行模式的三级来源：命令行 > 需求卡片 > 缺省 step。

    缺省必须是 step：拿不到人明确说过「直接执行」，就不许替他决定不打扰。
    """
    v = (cli_val or "").strip().lower()
    if v in EXEC_MODES:
        return v
    code = ((card_res or {}).get("stats") or {}).get("exec_mode_code") or ""
    if code in EXEC_MODES:
        return code
    return "step"


def cmd_mode(a) -> int:
    """查看 / 切换执行模式（mid-loop 可切，含切换留痕）。

    用户裁定：两条线之间随时能打断重选——不是一锤定音。
    但每次切换都要留一句为什么，否则事后没人能解释「这轮为什么没停」。
    """
    root = wg.workspace_root(a.workspace)
    run_id = a.run or wg.read_pointer(root)
    st = load_run_state(root, run_id)
    cur = st.get("exec_mode") or "step"

    if not a.set:
        print(json.dumps({"ok": True, "run_id": run_id, "exec_mode": cur,
                          "explain": MODE_HUMAN.get(cur, cur),
                          "switches": st.get("exec_mode_switches") or [],
                          "state": st.get("state")}, ensure_ascii=False, indent=2))
        return 0

    to = a.set.strip().lower()
    if to not in EXEC_MODES:
        print(json.dumps({"ok": False, "error": "exec_mode 只能是 step / auto，拿到 %r" % to,
                          "hint": "step=逐步确认；auto=无人值守"}, ensure_ascii=False))
        return 2
    if to == cur and not a.force:
        print(json.dumps({"ok": True, "run_id": run_id, "exec_mode": cur,
                          "noop": True, "explain": "已经是这个模式了"}, ensure_ascii=False))
        return 0
    rec = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "from": cur, "to": to,
           "by": a.by or "human", "reason": a.reason or ""}
    st["exec_mode"] = to
    st.setdefault("exec_mode_switches", []).append(rec)
    save_run_state(root, st)
    journal(root, st, {"action": "exec_mode_switch", "result": "ok",
                       "from": cur, "to": to, "reason": a.reason or "",
                       "by": a.by or "human"})
    print(json.dumps({"ok": True, "run_id": run_id, "exec_mode": to,
                      "explain": MODE_HUMAN[to], "switch": rec},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_init(a) -> int:
    root = wg.workspace_root(a.workspace)
    card_res = None
    if getattr(a, "card", ""):
        card_res = _run_card_gate(a.card)
        if not card_res.get("ok"):
            print(json.dumps({"ok": False, "stage": "requirement_gate",
                              "card": os.path.abspath(a.card),
                              "violations": card_res.get("violations"),
                              "hint": "需求没定型不许进规划（硬规则 2）：先把「未明确」清空、"
                                      "冲突裁定、拿到明确确认。本工具只拒绝，不会替你补。",
                              "stats": card_res.get("stats")},
                             ensure_ascii=False, indent=2))
            return 1
    run_id = a.run or wg.new_run_id(a.slug or "stage")
    d = os.path.join(root, "runs", run_id)
    os.makedirs(os.path.join(d, "evidence"), exist_ok=True)
    os.makedirs(os.path.join(d, "writes"), exist_ok=True)
    goal = dict(DEFAULT_GOAL)
    if a.max_rounds:
        goal["max_rounds"] = int(a.max_rounds)
    if a.max_same_error:
        goal["max_same_error"] = int(a.max_same_error)
    if a.round_timeout:
        goal["round_timeout"] = int(a.round_timeout)
    if a.send_timeout:
        goal["send_timeout"] = int(a.send_timeout)
    st = {
        "run_id": run_id, "stage_id": a.stage or "", "state": "INIT",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        # 执行模式：step=逐步确认（每轮停在 APPROVED 前等人批）；auto=无人值守。
        # 来源优先级：命令行 --exec-mode > 卡片「执行模式」> 缺省 step（保守）。
        "exec_mode": _resolve_exec_mode(getattr(a, "exec_mode", "") or "", card_res),
        "exec_mode_switches": [],
        "goal": dict(goal, text=a.goal or ""),
        "target_url_sha256": "", "platform": "", "round": 0, "rounds_used": 0,
        "fail_streak": 0, "last_error_class": "", "last_guard_failure": None,
        "state_history": [],
        "pending": {"prompt_file": "", "prompt_sha256": "", "approval_id": ""},
        "last_send": {}, "lessons_candidates": [], "lessons_merged": 0, "closed": False,
        "recovery_count": 0,
        "requirement_card": os.path.abspath(a.card) if getattr(a, "card", "") else "",
        "requirement_gate": (card_res or {}).get("stats") if card_res else None,
    }
    wg.atomic_write_json(os.path.join(d, "state.json"), st)
    wg.set_pointer(root, run_id)
    goal_md = "# 当前目标\n\n%s\n\n## 预算\n\n%s\n" % (
        a.goal or "", json.dumps(goal, ensure_ascii=False, indent=2))
    wg.atomic_write_text(os.path.join(root, "state", "current_goal.md"), goal_md)
    contract_path = os.path.join(root, "state", "contract.md")
    if not os.path.exists(contract_path):
        src = os.path.join(root, CONTRACT)
        body = ""
        if os.path.exists(src):
            with open(src, encoding="utf-8") as f:
                body = f.read()
        wg.atomic_write_text(contract_path, body or "# 契约\n\n（尚未冻结）\n")
    if a.stage:
        hp = os.path.join(root, "stage", a.stage, "handoff.md")
        if not os.path.exists(hp):
            wg.atomic_write_text(hp, "# 阶段 %s 交接\n\n（待填）\n" % a.stage)
    journal(root, st, {"action": "init", "result": "ok", "run_id": run_id,
                       "stage_id": st["stage_id"]})
    print(json.dumps({"ok": True, "run_id": run_id, "run_dir": os.path.relpath(d, root),
                      "state": st["state"], "goal_budget": goal},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_bind(a) -> int:
    root = wg.workspace_root(a.workspace)
    run_id = a.run or wg.read_pointer(root)
    if not run_id:
        raise SystemExit("还没有 run：先 orchestrator.py init")
    st = load_run_state(root, run_id)
    wa_st = wa.load_state(wa_state_path(root, a.state))
    if a.reuse:
        # 复用已绑定的对话：同一对话纪律的硬约束——不重新 bind、不新增地址。
        # 但「复用」只能复用**有身份**的对话：conv_id 为空说明这份 state 是
        # 归属门禁上线之前写的，无法证明它是本项目新建的那条（2026-10-29 事故现场）。
        url = wa_st.get("url") or ""
        if not url:
            raise SystemExit("--reuse 需要已经绑定的对话（wa state 里没有 url）")
        if not (wa_st.get("conv_id") or "") or not (wa_st.get("conv_origin") or ""):
            raise SystemExit(
                "拒绑：state 里的对话身份不完整（缺 conv_id 或 conv_origin）。\n"
                "这多半是归属门禁上线前绑的对话，无法证明是本项目新建的那条。\n"
                "→ 要干净上下文：orchestrator.py bind --new --platform <key>\n"
                "→ 确认沿用这条：先在 wa state 上重跑一次 wa.py bind <url> --reuse-existing")
        st["target_url_sha256"] = wg.sha16(url)
        st["platform"] = wa_st.get("platform") or ""
        st["bound_url"] = url
        transition(root, st, "BOUND", "复用已绑定对话：%s" % url,
                   {"action": "bind", "result": "ok", "bound_url": url, "reused": True})
        print(json.dumps({"ok": True, "reused": True, "url": url,
                          "target_url_sha256": st["target_url_sha256"],
                          "platform": st["platform"], "state": st["state"]},
                         ensure_ascii=False, indent=2))
        return 0
    argv = ["bind"]
    if a.new:
        argv.append("--new")
    elif a.url:
        argv.append(a.url)
    if getattr(a, "reuse_existing", False):
        argv.append("--reuse-existing")
    argv += ["--state", wa_state_path(root, a.state)]
    if a.platform:
        argv += ["--platform", a.platform]
    p = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                     "wa.py")] + argv,
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=300)
    out = (p.stdout or "").strip()
    if p.returncode != 0:
        print(out or (p.stderr or ""), file=sys.stderr)
        return p.returncode
    try:
        info = json.loads(out)
    except Exception:
        info = {"raw": out}
    wa_st = wa.load_state(wa_state_path(root, a.state))
    url = wa_st.get("url") or ""
    st["target_url_sha256"] = wg.sha16(url)
    st["platform"] = wa_st.get("platform") or ""
    st["bound_url"] = url
    transition(root, st, "BOUND", "绑定成功：%s" % url,
               {"action": "bind", "result": "ok", "bound_url": url})
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def cmd_plan(a) -> int:
    root = wg.workspace_root(a.workspace)
    run_id = a.run or wg.read_pointer(root)
    st = load_run_state(root, run_id)
    if not a.file or not os.path.exists(a.file):
        raise SystemExit("规划产物文件不存在：%s" % a.file)
    with open(a.file, encoding="utf-8") as f:
        body = f.read().strip()
    if not body:
        raise SystemExit("规划产物是空的")
    dest = os.path.join(run_dir(root, run_id), "plan.md")
    wg.atomic_write_text(dest, body)
    st["plan_file"] = os.path.relpath(dest, root)
    st["plan_chars"] = len(body)
    transition(root, st, "PLAN_OK", "规划产物已落盘（%d 字）" % len(body),
               {"action": "plan", "result": "ok", "plan_chars": len(body)})
    print(json.dumps({"ok": True, "plan_file": st["plan_file"], "chars": len(body),
                      "state": st["state"]}, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------- loop


def cmd_loop(a) -> int:
    root = wg.workspace_root(a.workspace)
    run_id = a.run or wg.read_pointer(root)
    st = load_run_state(root, run_id)
    p = platform_of(root, a.platform)

    if a.resume:
        return do_resume(root, st, p, a)

    if a.file:
        with open(a.file, encoding="utf-8") as f:
            txt = f.read().strip()
        st.setdefault("pending", {})["prompt_file"] = os.path.abspath(a.file)
        st["pending"]["prompt_sha256"] = wg.sha16(txt)
        st["pending"]["prompt_chars"] = len(txt)
        save_run_state(root, st)
        journal(root, st, {"action": "queue_prompt", "result": "ok",
                           "prompt_file": st["pending"]["prompt_file"],
                           "prompt_chars": len(txt)})

    steps = 40 if a.round else 1
    out = []
    for _ in range(steps):
        r = one_step(root, st, p, a)
        out.append(r)
        if r.get("stop"):
            break
    print(json.dumps({"ok": True, "run_id": st.get("run_id"), "state": st.get("state"),
                      "steps": out}, ensure_ascii=False, indent=2))
    return 0


def one_step(root: str, st: dict, p: dict, a) -> dict:
    """推进一个状态步。返回本次动作摘要。"""
    ctx = reread_context(root, st)
    cur = st.get("state", "INIT")
    step = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "from": cur, "context": ctx}

    if cur == "INIT":
        step["action"] = "need_bind"
        step["stop"] = True
        return step
    if cur == "BOUND":
        step["action"] = "need_plan"
        step["stop"] = True
        return step

    # ---- PLAN_OK → READY：窗口守卫 + DOM 守卫 ----
    if cur == "PLAN_OK":
        wok, wwhy = _window_guard(root, st, a, "dom_guard")
        if not wok:
            st["last_guard_failure"] = {"reason": wwhy, "checkpoint": "window_guard",
                                        "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            save_run_state(root, st)
            transition(root, st, "PAUSED_HUMAN", wwhy,
                       {"action": "guard", "result": "failed", "error_class": "rule6_violation"})
            step["action"] = "window_guard_failed"
            step["detail"] = wwhy
            step["stop"] = True
            return step
        sid, wa_st = session_of(root, a.state)
        url = wa_st.get("url") or ""
        if st.get("target_url_sha256") and wg.sha16(url) != st["target_url_sha256"]:
            st["last_guard_failure"] = {"reason": "URL 漂移：绑定 %s，现在 %s"
                                        % (st.get("target_url_sha256"), wg.sha16(url)),
                                        "checkpoint": "url_drift"}
            save_run_state(root, st)
            transition(root, st, "PAUSED_HUMAN", "URL 漂移",
                       {"action": "guard", "result": "failed", "error_class": "guard_failed"})
            step["action"] = "guard_failed"
            step["stop"] = True
            return step
        snap = wg.dom_snapshot(p, sid)
        # 比「活页面」的地址，不是 state 里的副本（见 wg.guard_ok 注释）
        ok, why = wg.guard_ok(snap, url)
        if not ok:
            st["last_guard_failure"] = {"reason": why, "checkpoint": "dom_guard",
                                        "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            save_run_state(root, st)
            transition(root, st, "PAUSED_HUMAN", why,
                       {"action": "guard", "result": "failed", "error_class": "guard_failed"})
            step["action"] = "guard_failed"
            step["detail"] = why
            step["stop"] = True
            return step
        transition(root, st, "READY", "DOM 守卫通过",
                   {"action": "guard", "result": "ok", "dom_hash": snap.get("dom_hash")})
        step["action"] = "guard_ok"
        step["to"] = "READY"
        return step

    # ---- READY → DRY_RUN_OK：写闸门出计划 ----
    if cur == "READY":
        pf = (st.get("pending") or {}).get("prompt_file") or ""
        if not pf or not os.path.exists(pf):
            step["action"] = "need_prompt"
            step["stop"] = True
            return step
        rc, res = run_gate(root, ["plan", "--file", pf, "--stage", st.get("stage_id", "")],
                           {"STARPILOT_WRITE_MODE": "dry_run"},
                           state=wa_state_path(root, a.state))
        if rc != 0 or not isinstance(res, dict) or not res.get("approval_id"):
            step["action"] = "plan_failed"
            step["detail"] = res
            step["stop"] = True
            return step
        st.setdefault("pending", {})["approval_id"] = res["approval_id"]
        save_run_state(root, st)
        transition(root, st, "DRY_RUN_OK", "写闸门已出计划 %s" % res["approval_id"],
                   {"action": "dry_run", "result": "ok", "approval_id": res["approval_id"]})
        step["action"] = "dry_run_ok"
        step["approval_id"] = res["approval_id"]
        step["to"] = "DRY_RUN_OK"
        return step

    # ---- DRY_RUN_OK → APPROVED：唯一的「人」的卡点 ----
    # 两种放行方式：
    #   ① 单条批准（single）：只对这一组 hash 有效；
    #   ② 授权单（count/stage/task）：一次授权覆盖后续多条 —— 用户裁定「一次授权覆盖」，
    #      也是「无人值守不反复打扰」的唯一实现方式。
    # 两者都没有 = 没被授权，一律 fail-closed 停下，并**说清怎么一次授权**。
    if cur == "DRY_RUN_OK":
        aid = (st.get("pending") or {}).get("approval_id") or ""
        d = run_dir(root, st.get("run_id"))
        dry_path = os.path.join(d, "writes", "%s.dry.json" % aid)
        dry = {}
        if os.path.exists(dry_path):
            with open(dry_path, encoding="utf-8") as f:
                dry = json.load(f)
        rec, why = wg.load_approval(d, aid)
        grant, grant_path = None, ""
        if rec is None:
            grant, grant_path, gwhy = wg.find_grant(
                d, dry.get("target_url_sha256") or "", p.get("key") or "",
                st.get("stage_id") or "")
        if rec is None and grant is None:
            hint = ("一次性授权可免去后面每轮都停：write_gate.py approve --approval %s "
                    "--scope task --ttl 3600 --max-writes 200" % aid)
            st["last_guard_failure"] = {"reason": "人工卡点：这次写没有批准或授权覆盖（%s）"
                                        % ("%s；%s" % (why, gwhy) if rec is None else why),
                                        "checkpoint": "write_approval",
                                        "hint": hint,
                                        "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            save_run_state(root, st)
            transition(root, st, "PAUSED_HUMAN", "等待人工批准真实写",
                       {"action": "checkpoint", "result": "paused",
                        "checkpoint": "write_approval", "approval_id": aid,
                        "hint": hint})
            step["action"] = "checkpoint_write_approval"
            step["approval_id"] = aid
            step["hint"] = hint
            step["stop"] = True
            return step
        if rec is not None:
            if (rec.get("target_url_sha256") != dry.get("target_url_sha256")
                    or rec.get("prompt_sha256") != dry.get("prompt_sha256")):
                st["last_guard_failure"] = {"reason": "批准与计划不一致（URL/prompt hash 对不上）",
                                            "checkpoint": "write_approval"}
                save_run_state(root, st)
                transition(root, st, "PAUSED_HUMAN", "批准与计划不一致",
                           {"action": "checkpoint", "result": "paused",
                            "checkpoint": "write_approval"})
                step["action"] = "approval_mismatch"
                step["stop"] = True
                return step
            how = "单条批准"
            gid = ""
        else:
            how = "授权单（%s，剩余 %d 条）" % (
                grant.get("scope"),
                int(grant.get("max_writes") or 0) - int(grant.get("used") or 0))
            gid = grant.get("grant_id") or ""
        transition(root, st, "APPROVED", "放行依据：%s %s" % (how, aid),
                   {"action": "approve", "result": "ok", "approval_id": aid,
                    "grant_id": gid, "basis": "grant" if gid else "approval"})
        step["action"] = "approved"
        step["to"] = "APPROVED"
        step["basis"] = "grant" if gid else "approval"
        return step

    # ---- APPROVED → SENDING：真发（必须经闸门）----
    if cur == "APPROVED":
        wok, wwhy = _window_guard(root, st, a, "before_send")
        if not wok:
            st["last_guard_failure"] = {"reason": wwhy, "checkpoint": "window_guard",
                                        "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            save_run_state(root, st)
            transition(root, st, "PAUSED_HUMAN", wwhy,
                       {"action": "guard", "result": "failed", "error_class": "rule6_violation"})
            step["action"] = "window_guard_failed"
            step["detail"] = wwhy
            step["stop"] = True
            return step
        aid = (st.get("pending") or {}).get("approval_id") or ""
        st["round"] = int(st.get("round") or 0) + 1
        st["rounds_used"] = int(st.get("rounds_used") or 0) + 1
        # send_id：把「这次发送」的因果链钉死（评审 S7）。
        # 一条 send 会牵出：write_send 事件 → write_verify 判定 → 本轮 reply 文件。
        # 没有它，事后复盘只能靠时间戳猜「这条 delta 是哪次发的」。
        send_id = "sd-%s" % wg.sha16("%s|%s|%s" % (
            st["round"], (st.get("pending") or {}).get("prompt_sha256", ""),
            time.time()))
        st.setdefault("pending", {})["send_id"] = send_id
        save_run_state(root, st)
        transition(root, st, "SENDING", "开始发送（闸门执行）",
                   {"action": "send_begin", "result": "ok", "approval_id": aid,
                    "send_id": send_id})
        rc, res = run_gate(root, ["send", "--approval", aid, "--round", str(st["round"]),
                                  "--send-id", send_id],
                           {"STARPILOT_WRITE_MODE": "single_write"},
                           state=wa_state_path(root, a.state))
        ok = (rc == 0 and isinstance(res, dict) and res.get("ok"))
        if not ok:
            err = ((res or {}).get("error_class") or (res or {}).get("error_code")
                   or "send_failed")
            detail = ((res or {}).get("detail") or (res or {}).get("message")
                      or json.dumps(res, ensure_ascii=False)[:200])
            # 弱信号处置：三选一主信号都不满足、但输入框已清空 → **不能报成功也不能报失败**，
            # 必须报 INCONCLUSIVE_NOT_SUCCESS（网页 AI 第 2 轮裁定）。
            if (res or {}).get("verdict") == "INCONCLUSIVE_NOT_SUCCESS":
                err = "INCONCLUSIVE_NOT_SUCCESS"
                detail = ("三选一主信号（消息数增加/地址变了/正文指纹命中）都不满足，"
                          "但输入框已清空：既不能判定成功、也不能判定失败 —— 需人工复核页面")
            apply_verdict_effect(st, err)   # 唯一写入口（§2.4 三值语义）
            st["last_error_class"] = err
            save_run_state(root, st)
            lesson_candidate(root, st, err, detail)
            if st["fail_streak"] >= int((st.get("goal") or {}).get("max_same_error", 2)):
                transition(root, st, "HANDOFF", "同目标连续失败 %d 次" % st["fail_streak"],
                           {"action": "send", "result": "failed", "error_class": err})
            else:
                st["state"] = "PAUSED_HUMAN"
                st["last_guard_failure"] = {"reason": "发送失败：%s（%s）" % (err, detail),
                                            "checkpoint": "send_failed",
                                            "at": time.strftime("%Y-%m-%d %H:%M:%S")}
                save_run_state(root, st)
                journal(root, st, {"action": "send", "result": "failed",
                                   "error_class": err})
            step["action"] = "send_failed"
            step["detail"] = res
            step["stop"] = True
            return step
        st["last_send"] = {
            "url_sha256": st.get("target_url_sha256", ""),
            "prompt_sha256": (st.get("pending") or {}).get("prompt_sha256", ""),
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            # 判据口径：助手回复条数（一条提问对一条回复 ⇒ 正好 1）
            "reply_count_before": res.get("reply_count_before"),
            "reply_count_after": res.get("reply_count_after"),
            "reply_count_delta": res.get("reply_count_delta"),
            # 平台相关的总消息数（仅审计参考）
            "msg_count_before": res.get("msg_count_before"),
            "msg_count_after": res.get("msg_count_after"),
            "msg_count_delta": res.get("msg_count_delta"),
            "reply_pending": res.get("reply_pending", False),
            "signals": res.get("signals", []),
            # 发送前的锚点快照：下一状态 wait_for_reply 靠它判「来了新回复」，
            # 只存计数是不够的（虚拟列表会让计数原地不动）
            "asst_fps_before": (res.get("before") or {}).get("asst_fps") or [],
            "asst_keys_before": (res.get("before") or {}).get("asst_keys") or [],
            "user_fps_before": (res.get("before") or {}).get("user_fps") or [],
            "last_asst_fp_before": (res.get("before") or {}).get("last_asst_fp") or "",
            "last_len_before": (res.get("before") or {}).get("last_len") or 0,
            "approval_id": aid,
            "send_id": send_id,
            "verdict": res.get("verdict", ""),
            "delta_source": res.get("delta_source", ""),
            "effective_delta": res.get("effective_delta"),
        }
        apply_verdict_effect(st, res.get("verdict") or "SENT")  # 送达=有结论的成功
        st["last_error_class"] = ""
        save_run_state(root, st)
        transition(root, st, "AWAITING_REPLY",
                   "已确认送达（reply_count_delta=%s）" % res.get("reply_count_delta"),
                   {"action": "send", "result": "ok",
                    "reply_count_delta": res.get("reply_count_delta"),
                    "signals": res.get("signals", [])})
        step["action"] = "sent"
        step["reply_count_delta"] = res.get("reply_count_delta")
        step["to"] = "AWAITING_REPLY"
        return step

    # ---- AWAITING_REPLY → REPLY_VERIFIED ----
    if cur == "AWAITING_REPLY":
        sid, wa_st = session_of(root, a.state)
        last_send = st.get("last_send") or {}
        before = {
            "reply_count": last_send.get("reply_count_before") or 0,
            "asst_fps": last_send.get("asst_fps_before") or [],
            "user_fps": last_send.get("user_fps_before") or [],
            "asst_keys": last_send.get("asst_keys_before") or [],
            "last_asst_fp": last_send.get("last_asst_fp_before") or "",
            "last_len": last_send.get("last_len_before") or 0,
        }
        timeout = int((st.get("goal") or {}).get("round_timeout", 300))
        res = wait_for_reply(p, sid, before["reply_count"], timeout, before=before)
        if not res.get("ok"):
            apply_verdict_effect(st, "reply_timeout")
            st["last_error_class"] = "reply_timeout"
            save_run_state(root, st)
            lesson_candidate(root, st, "reply_timeout", "等回复 %ds 没等到" % timeout)
            if st["fail_streak"] >= int((st.get("goal") or {}).get("max_same_error", 2)):
                transition(root, st, "HANDOFF", "等回复连续超时 %d 次" % st["fail_streak"],
                           {"action": "wait", "result": "failed", "error_class": "reply_timeout"})
            else:
                st["state"] = "PAUSED_HUMAN"
                st["last_guard_failure"] = {"reason": "等回复超时（%ds）" % timeout,
                                            "checkpoint": "reply_timeout",
                                            "at": time.strftime("%Y-%m-%d %H:%M:%S")}
                save_run_state(root, st)
                journal(root, st, {"action": "wait", "result": "failed",
                                   "error_class": "reply_timeout"})
            step["action"] = "reply_timeout"
            step["stop"] = True
            return step
        # 等到了 = 有结论的成功 → 清零（三值语义里「成功」打断「连续失败」）。
        # 恢复不清零（无结论），两件事不矛盾：都不是「有结论的失败」，但只有成功打断链条。
        apply_verdict_effect(st, "SENT")
        save_run_state(root, st)
        transition(root, st, "REPLY_VERIFIED", "回复已就绪（%d 字）" % res.get("chars", 0),
                   {"action": "wait", "result": "ok", "chars": res.get("chars"),
                    "signal": res.get("signal", "")})
        step["action"] = "reply_verified"
        step["chars"] = res.get("chars")
        step["to"] = "REPLY_VERIFIED"
        return step

    # ---- REPLY_VERIFIED → ROUND_DONE（落盘回复原文）----
    if cur == "REPLY_VERIFIED":
        sid, wa_st = session_of(root, a.state)
        ok, data = wa.evaluate(platforms.js_read_last(p), sid)
        text = (data or {}).get("text") or ""
        if not text:
            step["action"] = "read_failed"
            step["stop"] = True
            return step
        d = run_dir(root, st.get("run_id"))
        out_path = os.path.join(d, "replies", "round-%03d.md" % st.get("round", 0))
        wg.atomic_write_text(out_path, text)
        # 因果链第三环：回复落盘后立刻让闸门**核验**这一轮，否则 journal 里只有
        # write_send 没有 write_verify，事后审计只能看到「(未核验)」——
        # 链断了就等于没有链（评审 S7）。核验失败不拦落盘，但要如实记进 step。
        aid = (st.get("pending") or {}).get("approval_id") or ""
        send_id = (st.get("pending") or {}).get("send_id") or ""
        verify = {}
        if aid:
            vrc, verify = run_gate(root, ["verify", "--approval", aid,
                                          "--send-id", send_id],
                                   {"STARPILOT_WRITE_MODE": "single_write"},
                                   state=wa_state_path(root, a.state))
            verify["rc"] = vrc
        transition(root, st, "ROUND_DONE", "回复已落盘（%d 字）" % len(text),
                   {"action": "read_reply", "result": "ok", "chars": len(text),
                    "reply_file": os.path.relpath(out_path, root),
                    "verify_verdict": (verify or {}).get("verdict", "")})
        step["action"] = "round_done"
        step["reply_file"] = os.path.relpath(out_path, root)
        step["chars"] = len(text)
        step["to"] = "ROUND_DONE"
        return step

    if cur == "ROUND_DONE":
        decision, info = budget_gate(st)
        used, cap = info["used"], info["cap"]
        if decision == "exhausted":
            transition(root, st, "HANDOFF", "轮次预算耗尽（%d/%d）" % (used, cap),
                       {"action": "budget", "result": "failed", "error_class": "budget_exhausted"})
            step["action"] = "budget_exhausted"
            step["stop"] = True
            return step
        # 执行模式决定「这一轮结束后停不停」——这是两条线唯一的行为差异：
        #   step（逐步确认）：每轮停下，让人看结果、决定下一轮怎么写；
        #   auto（无人值守）：直接进下一轮，只在三条例外（连续失败 2 次 / 地址漂移 /
        #                     出现新歧义）才停——那三条例外由各自的守卫负责，不在这里。
        mode = st.get("exec_mode") or "step"
        if mode == "step":
            # 顺序要紧：transition() 成功后会清空 last_guard_failure，
            # 所以 checkpoint 必须**在迁移之后**写，否则 PAUSED_HUMAN 读出来是空的。
            transition(root, st, "PAUSED_HUMAN", "逐步确认：本轮结束，等待人工确认下一轮",
                       {"action": "checkpoint", "result": "paused",
                        "checkpoint": "round_review", "round": st.get("round")})
            st["last_guard_failure"] = {
                "reason": "逐步确认：本轮已结束，等你看完结果再决定下一轮",
                "checkpoint": "round_review",
                "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            save_run_state(root, st)
            step["action"] = "round_review_pause"
            step["stop"] = True
            return step
        transition(root, st, "READY", "进入下一轮（无人值守）",
                   {"action": "next_round", "result": "ok", "exec_mode": mode})
        step["action"] = "next_round"
        step["to"] = "READY"
        return step

    if cur == "PAUSED_HUMAN":
        step["action"] = "paused"
        step["checkpoint"] = (st.get("last_guard_failure") or {}).get("checkpoint", "")
        step["stop"] = True
        return step
    if cur == "HANDOFF":
        step["action"] = "handoff"
        step["stop"] = True
        return step
    if cur == "CLOSED":
        step["action"] = "closed"
        step["stop"] = True
        return step
    step["action"] = "noop"
    step["stop"] = True
    return step


# ---------------------------------------------------------------- lesson 候选


def lesson_candidate(root: str, st: dict, error_class: str, detail: str) -> dict:
    """失败/恢复事件 → lesson 候选（O4 的强制点之一）。

    字段口径由网页 AI 第 2 轮裁定：`lesson_id, date_utc, stage, source_run_ref,
    platform, symptom, dom_signal, fix, verify_cmd, status=open|merged`。
    `lesson_id` 必须**稳定且可检索**（同一条坑在任何地方都用同一个 id）。
    """
    symptom = "%s：%s" % (error_class, (detail or "")[:200])
    cands = st.setdefault("lessons_candidates", [])
    for c in cands:
        if c.get("symptom", "")[:120] == symptom[:120]:
            return c
    # lesson_id = L-<平台>-<错误类>-<症状摘要哈希前 6 位>：稳定、可 grep
    lid = "L-%s-%s-%s" % (
        (st.get("platform") or "na").upper()[:6],
        re.sub(r"[^A-Za-z0-9]+", "-", error_class).strip("-").upper()[:24] or "ERR",
        wg.sha16(symptom)[:6])
    cand = {
        "lesson_id": lid,
        "date_utc": time.strftime("%Y-%m-%d", time.gmtime()),
        "stage": st.get("stage_id", ""),
        "source_run_ref": "runs/%s" % st.get("run_id", ""),
        "platform": st.get("platform", ""),
        "symptom": symptom,
        "dom_signal": "",
        "fix": FIX_HINTS.get(error_class, FIX_HINTS["unknown"]),
        "trigger": TRIGGER_HINTS.get(error_class, TRIGGER_HINTS["unknown"]),
        "rollback": ROLLBACK_HINTS.get(error_class, ROLLBACK_HINTS["unknown"]),
        "verify_cmd": "python scripts/orchestrator.py audit --run %s" % st.get("run_id", ""),
        "status": "open",
    }
    cands.append(cand)
    save_run_state(root, st)
    journal(root, st, {"action": "lesson_candidate", "result": "ok",
                       "error_class": error_class, "lesson": cand})
    return cand


# ---------------------------------------------------------------- 恢复


def do_resume(root: str, st: dict, p: dict, a) -> int:
    """崩溃恢复：只复核 DOM，**绝不重发**。

    验收口径（网页 AI 第 3 轮裁定）：
      · resumed_from —— 崩溃时所处的状态（AWAITING_REPLY / SENDING …）
      · resend_attempts —— 恢复标记之后出现的发送动作数（**必须为 0**）
      · journal 里 resume 标记之后不得出现任何 action=send
      · 终态 REPLY_VERIFIED（看到回复）或 PAUSED_HUMAN（证据不足）均可接受
    """
    d = run_dir(root, st.get("run_id"))
    evs = wg.read_journal(d)
    sends = [e for e in evs if e.get("action") == "write_send"]
    # 崩溃时的状态：取 journal 里**第一次恢复标记之前**最后一次迁移的目标状态。
    # 不能用 state_history 的最后一条——恢复之后它会继续往前走，
    # 复跑 resume 就会把「崩溃时的状态」写成恢复后的状态（口径错）。
    first_marker = -1
    for i, e in enumerate(evs):
        if e.get("action") == "resume_marker":
            first_marker = i
            break
    prev_state = "SENDING"
    for i, e in enumerate(evs):
        if first_marker >= 0 and i >= first_marker:
            break
        if e.get("action") == "transition" and e.get("result") == "ok":
            prev_state = e.get("to", prev_state)
    if prev_state == "SENDING":
        hist = st.get("state_history") or []
        if hist:
            prev_state = hist[-1].get("to", "SENDING")

    st["state"] = "RECOVERING"
    st["recovery_count"] = int(st.get("recovery_count") or 0) + 1
    # 契约 §3.2：恢复**不改变** fail_streak —— 崩溃既不算失败也不算成功。
    # 清零会绕过硬规则 4（失败两次停），加一等于把「机器断电」记成「指令错」。
    save_run_state(root, st)
    # 恢复标记先落 journal：此后任何 send 动作都会落在标记之后，审计可查。
    journal(root, st, {"action": "resume_marker", "result": "ok",
                       "resumed_from": prev_state,
                       "rule": "恢复只复核 DOM，绝不重发"})
    evs2 = wg.read_journal(d)
    last_marker = max([i for i, e in enumerate(evs2)
                       if e.get("action") == "resume_marker"] or [-1])
    # 口径：**本次恢复标记之后**的发送动作数 —— 恢复路径不该有任何发送。
    # （kill 之前那次合法发送不算，它在标记之前。）
    resend_attempts = sum(1 for e in evs2[last_marker + 1:]
                          if e.get("action") in ("write_send", "send"))
    sends_before_marker = sum(1 for e in evs2[:last_marker]
                               if e.get("action") in ("write_send", "send"))
    decision = {"resumed_from": prev_state, "resend_attempts": resend_attempts,
                "sends_before_marker": sends_before_marker,
                "decision": "", "evidence": ""}

    last_send_ev = sends[-1] if sends else None
    if last_send_ev and last_send_ev.get("result") == "ok":
        before = (st.get("last_send") or {}).get("reply_count_before")
        if before is None:
            before = (st.get("last_send") or {}).get("msg_count_before") or 0
        try:
            sid, wa_st = session_of(root, a.state)
            ok, cur_probe = wa.read_state(p, sid)
        except SystemExit:
            ok, cur_probe = False, {}
        now = reply_count_of(cur_probe)
        decision["reply_count_before"] = before
        decision["reply_count_now"] = now
        decision["reply_found"] = bool(ok and now >= (before or 0) + 1)
        if ok and now >= (before or 0) + 1:
            decision["decision"] = ("已送达且已有回复：只复核 DOM，确认到了，不重发"
                                    "（resend_attempts=0）")
            if st.get("state") == "REPLY_VERIFIED":
                # 已经是这个状态（复跑 resume）：不再做非法迁移，只记账
                st["state"] = "REPLY_VERIFIED"
                save_run_state(root, st)
                journal(root, st, {"action": "recover", "result": "ok",
                                   "note": "状态已是 REPLY_VERIFIED，复跑不重复迁移",
                                   "resend_attempts": resend_attempts,
                                   "reply_count_before": before, "reply_count_now": now})
            else:
                transition(root, st, "REPLY_VERIFIED", "恢复：DOM 复核到回复，不重发",
                           {"action": "recover", "result": "ok",
                            "resend_attempts": resend_attempts,
                            "reply_count_before": before, "reply_count_now": now},
                           by="recovery")
        else:
            decision["decision"] = "证据不足（DOM 复核不出已送达）：转人工，不重发"
            st["last_guard_failure"] = {"reason": decision["decision"],
                                        "checkpoint": "unconfirmed_send",
                                        "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            st["state"] = "PAUSED_HUMAN"
            save_run_state(root, st)
            journal(root, st, {"action": "recover", "result": "paused",
                               "error_class": "send_unconfirmed",
                               "resend_attempts": resend_attempts})
    elif last_send_ev and last_send_ev.get("error_class") == "send_unconfirmed":
        decision["decision"] = "上次发送未确认：只复核 DOM，不重发；证据不足转人工"
        st["last_guard_failure"] = {"reason": decision["decision"],
                                    "checkpoint": "unconfirmed_send",
                                    "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        st["state"] = "PAUSED_HUMAN"
        save_run_state(root, st)
        journal(root, st, {"action": "recover", "result": "paused",
                           "error_class": "send_unconfirmed",
                           "resend_attempts": resend_attempts})
    else:
        decision["resumed_from"] = st.get("state", "INIT")
        decision["decision"] = "没有未决发送：从当前状态继续（不重发）"
        target = st.get("state") if st.get("state") in STATES else "READY"
        st["state"] = "RECOVERING"
        save_run_state(root, st)
        transition(root, st, target, decision["decision"],
                   {"action": "recover", "result": "ok",
                    "resend_attempts": resend_attempts},
                   by="recovery")

    ed = os.path.join(d, "evidence")
    n = len([f for f in os.listdir(ed) if f.startswith("recovery-")]) + 1 if os.path.isdir(ed) else 1
    ev_path = write_evidence(root, st, "recovery-%03d.json" % n, {
        "recovered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "resumed_from": decision.get("resumed_from"),
        "resend_attempts": resend_attempts,
        "reply_found": decision.get("reply_found"),
        "final_state": st.get("state"),
        "decision": decision,
        "journal_events": len(evs),
        "rule": "恢复只复核 DOM，绝不重发；证据不足一律 PAUSED_HUMAN。",
    })
    decision["evidence"] = os.path.relpath(ev_path, root)
    decision["final_state"] = st.get("state")
    print(json.dumps({"ok": True, "state": st.get("state"), "decision": decision},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_recover(a) -> int:
    """只解释恢复决策，不执行。"""
    root = wg.workspace_root(a.workspace)
    run_id = a.run or wg.read_pointer(root)
    st = load_run_state(root, run_id)
    d = run_dir(root, run_id)
    evs = wg.read_journal(d)
    sends = [e for e in evs if e.get("action") == "write_send"]
    out = {
        "run_id": st.get("run_id"),
        "state": st.get("state"),
        "last_guard_failure": st.get("last_guard_failure"),
        "budget": budget_of(st),
        "sends": [{"ts": e.get("ts"), "result": e.get("result"),
                   "error_class": e.get("error_class"),
                   "reply_count_delta": e.get("reply_count_delta"),
                   "signals": e.get("signals")} for e in sends],
        "explain": [],
    }
    if not sends:
        out["explain"].append("没有未决发送：可安全继续。")
    for e in sends[-1:]:
        if e.get("result") == "ok":
            out["explain"].append(
                "上次发送已确认成功（reply_count_delta=%s）：恢复时**不重发**，"
                "只把状态推回 AWAITING_REPLY。" % e.get("reply_count_delta"))
        elif e.get("error_class") == "send_unconfirmed":
            out["explain"].append(
                "上次发送未确认：恢复时**只复核 DOM**；msg_count 没涨就是证据不足"
                " → PAUSED_HUMAN，不猜、不重发。")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------- status / audit / approve


def budget_gate(st: dict) -> tuple:
    """轮次预算的**唯一判定点**（契约 §3.1）。

    口径（写死，不许在别处另算）：
      · `max_rounds` 数的是**实际发送轮次**（`rounds_used`），不是「成功轮次」；
      · 失败的发送也消耗预算——预算防的是「烧轮次」，不是「烧成功」；
      · 崩溃恢复不返还预算（恢复是复核，不是重来）；`fail_streak` 才是「失败两次停」的口径。
    """
    used = int(st.get("rounds_used") or 0)
    cap = int((st.get("goal") or {}).get("max_rounds", 12))
    return ("exhausted" if used >= cap else "next", {"used": used, "cap": cap})


def budget_of(st: dict) -> dict:
    g = st.get("goal") or {}
    used = int(st.get("rounds_used") or 0)
    cap = int(g.get("max_rounds", 12))
    return {
        "rounds_used": used,
        "rounds_left": max(0, cap - used),
        "max_rounds": cap,
        "max_same_error": int(g.get("max_same_error", 2)),
        "fail_streak": int(st.get("fail_streak") or 0),
        "round_timeout": int(g.get("round_timeout", 300)),
        "send_timeout": int(g.get("send_timeout", 60)),
    }


def cmd_status(a) -> int:
    root = wg.workspace_root(a.workspace)
    run_id = a.run or wg.read_pointer(root)
    if not run_id:
        print(json.dumps({"ok": False, "hint": "还没有 run：先 orchestrator.py init"},
                         ensure_ascii=False, indent=2))
        return 2
    st = load_run_state(root, run_id)
    out = {
        "run_id": st.get("run_id"),
        "stage_id": st.get("stage_id"),
        "state": st.get("state"),
        "round": st.get("round"),
        "platform": st.get("platform"),
        "target_url_sha256": st.get("target_url_sha256"),
        "last_guard_failure": st.get("last_guard_failure"),
        "last_error_class": st.get("last_error_class"),
        "budget": budget_of(st),
        "lessons_candidates": len(st.get("lessons_candidates") or []),
        "lessons_merged": st.get("lessons_merged", 0),
        "recovery_count": st.get("recovery_count", 0),
        "requirement_gate": st.get("requirement_gate"),
        "windows": (window_report(root, a.state) if getattr(a, "state", None) is not None
                    else {}),
        "next_hint": STEP_HINT.get(st.get("state", "INIT"), ""),
        "state_history": (st.get("state_history") or [])[-5:],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_audit(a) -> int:
    root = wg.workspace_root(a.workspace)
    run_id = a.run or wg.read_pointer(root)
    d = run_dir(root, run_id)
    st = load_run_state(root, run_id)
    evs = wg.read_journal(d)

    sends = [e for e in evs if e.get("action") == "write_send"]
    groups = {}
    for e in sends:
        if e.get("result") != "ok":
            continue
        k = (e.get("target_url_sha256", ""), e.get("prompt_sha256", ""))
        groups[k] = groups.get(k, 0) + 1
    duplicate_sends = sum(1 for k, n in groups.items() if n > 1)

    unconfirmed_sends = sum(1 for e in sends if e.get("error_class") == "send_unconfirmed")

    unexpected_write = 0
    for e in sends:
        if e.get("result") != "ok":
            continue
        aid = e.get("approval_id") or ""
        if not aid or not os.path.exists(os.path.join(d, "writes", "%s.approval.json" % aid)):
            unexpected_write += 1

    cands = st.get("lessons_candidates") or []
    lessons_pending = max(0, len(cands) - int(st.get("lessons_merged") or 0))

    # 恢复后不许再有发送动作：resume 标记之后的 write_send/send 计数必须为 0。
    # 口径：**没有 resume 标记就没有「恢复后」可言**，计数为 0
    # （否则会把恢复之前的合法发送也算进去，把没恢复过的 run 判成 FAIL）。
    markers = [i for i, e in enumerate(evs) if e.get("action") == "resume_marker"]
    sends_after_resume = 0
    if markers:
        sends_after_resume = sum(1 for e in evs[markers[-1] + 1:]
                                  if e.get("action") in ("write_send", "send"))
    resume_events = len(markers)
    write_mode_at_send = ""
    for e in sends:
        if e.get("write_mode"):
            write_mode_at_send = e.get("write_mode")
    # inconclusive 计数：核验结论为「不自动判失败」的那些（气泡拆分嫌疑等）
    inconclusive = sum(1 for e in evs
                       if e.get("action") == "write_verify"
                       and e.get("result") == "inconclusive")

    out = {
        "run_id": st.get("run_id"),
        "duplicate_sends": duplicate_sends,
        "unconfirmed_sends": unconfirmed_sends,
        "unexpected_write": unexpected_write,
        "lessons_pending": lessons_pending,
        "inconclusive": inconclusive,
        "sends_total": len(sends),
        "resume_events": resume_events,
        "sends_after_resume": sends_after_resume,
        "last_reply_count_delta": (st.get("last_send") or {}).get("reply_count_delta"),
        "write_mode_at_send": write_mode_at_send,
        "write_mode_now": wg.write_mode(),
        "verdict": "PASS" if (duplicate_sends == 0 and unconfirmed_sends == 0
                              and unexpected_write == 0 and lessons_pending == 0
                              and sends_after_resume == 0 and inconclusive == 0) else "FAIL",
    }
    if getattr(a, "trace", False):
        out["trace"] = build_trace(d, evs)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["verdict"] == "PASS" else 5


def cmd_approve(a) -> int:
    root = wg.workspace_root(a.workspace)
    run_id = a.run or wg.read_pointer(root)
    rc, res = run_gate(root, ["approve", "--approval", a.approval, "--ttl", str(a.ttl),
                              "--by", a.by], state=wa_state_path(root, a.state))
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return rc


# ---------------------------------------------------------------- close（O4）


# 知识回流条目格式（v1.1：新增 trigger / rollback 两段）。
# 老条目是 10 段（status 在最后），lessons_audit.py 两种都认——格式只许**追加**，不许重排。
# 13 段：第 12 段 blocking 是「这条不闭合就不许收口」的显式声明，
# 不是备注。老条目 10/12 段仍可读（lessons_audit 三种格式都认）。
LESSON_LINE = ("[{lesson_id}][{date_utc}][{stage}][{source_run_ref}][{platform}]"
               "[{symptom}][{dom_signal}][{fix}][{verify_cmd}][{trigger}][{rollback}]"
               "[{blocking}][{status}]")


def _oneline(v) -> str:
    """把字段压成一行：`- [..][..]` 的格式是「一条 lesson 一行」。

    2026-10-29 实测踩到：失败的 detail 里带多行 JSON，原样写进去会把条目撑成十几行，
    后面的解析（按行切）当场把 continuation 当垃圾——格式坏掉的方式非常安静，
    只有 `lessons_audit` 才会喊 malformed。
    """
    return re.sub(r"\s+", " ", str(v if v is not None else "")).strip()


def _lesson_line(**kw) -> str:
    return "- " + LESSON_LINE.format(**{k: _oneline(v) for k, v in kw.items()})


def cmd_close(a) -> int:
    """阶段收口：强制合并 lesson 候选进 references/lessons.md（O4）。

    验收口径（网页 AI 第 2/3 轮裁定）：
      · candidates = merged = appendix_added（同一组数字：这一轮回流了几条）
      · audit 的 lessons_pending = 0
      · 每条可检索 lesson_id（grep 得到）
      · references/lessons.md 哈希必须变化
      · 故意搁置的条目（纪律搁置、未实测平台等）以 **status=open** 进附录，
        不许被「回流完成」的措辞吞掉——沉默缺口正是要消灭的东西
    """
    root = wg.workspace_root(a.workspace)
    run_id = a.run or wg.read_pointer(root)
    st = load_run_state(root, run_id)
    d = run_dir(root, run_id)
    cands = st.get("lessons_candidates") or []
    merged = int(st.get("lessons_merged") or 0)
    pending = cands[merged:]

    lp = os.path.join(root, "references", "lessons.md")
    # 收口前先体检：**blocking 的 open 不许带进下一轮**（第 2 轮裁定 3）。
    # 纪律漏洞类 open 挂着不闭合，等于把已知缺口写进新阶段。
    if os.path.exists(lp):
        try:
            import lessons_audit
            la = lessons_audit.audit(lp)
        except Exception as e:  # 体检本身坏了不许静默放行
            print(json.dumps({"ok": False, "reason": "lessons 体检失败：%s" % e},
                             ensure_ascii=False, indent=2))
            return 5
        if la.get("blocking_open"):
            print(json.dumps({"ok": False, "blocking_open": la["blocking_open"],
                              "verdict": la.get("verdict"),
                              "reason": "这些 lesson 标了 blocking=true 还开着，"
                                        "不许阶段收口：先补完或改成 blocking=false 并写理由"},
                             ensure_ascii=False, indent=2))
            return 5
    body = ""
    before_hash = ""
    if os.path.exists(lp):
        with open(lp, encoding="utf-8") as f:
            body = f.read()
        before_hash = wg.sha16(body)

    appendix_count_before = body.count("[L-")
    added = 0
    updated = 0
    if pending:
        lines = body.splitlines()
        new_lines = []
        for c in pending:
            lid = c.get("lesson_id", "")
            target_status = c.get("appendix_status") or "merged"
            hit = False
            for i, ln in enumerate(lines):
                if "[%s]" % lid in ln:
                    # 已有同一个 lesson_id：**只更新日期与状态**，不追加重复条目
                    parts = re.findall(r"\[([^\]]*)\]", ln)
                    if len(parts) >= 3:
                        parts[1] = c.get("date_utc", parts[1])
                        parts[-1] = target_status
                        if len(parts) >= 13:
                            parts[-2] = "true" if c.get("blocking") else "false"
                        head = ln[:ln.index("[")]
                        lines[i] = head + "[" + "][".join(parts) + "]"
                    updated += 1
                    hit = True
                    break
            if not hit:
                new_lines.append(_lesson_line(
                    lesson_id=lid, date_utc=c.get("date_utc", ""),
                    stage=c.get("stage", a.stage or ""),
                    source_run_ref=c.get("source_run_ref", ""),
                    platform=c.get("platform", ""), symptom=c.get("symptom", ""),
                    dom_signal=c.get("dom_signal", ""), fix=c.get("fix", ""),
                    verify_cmd=c.get("verify_cmd", ""),
                    trigger=c.get("trigger", ""), rollback=c.get("rollback", ""),
                    blocking="true" if c.get("blocking") else "false",
                    status=target_status))
                added += 1
        if new_lines:
            if body and not body.endswith("\n"):
                body += "\n"
            body += "\n---\n\n## 附录：%s 阶段自动回流（%s）\n\n" % (
                a.stage or "", time.strftime("%Y-%m-%d"))
            body += "\n".join(new_lines) + "\n"
        else:
            body = "\n".join(lines) + "\n"
        wg.atomic_write_text(lp, body)

    after_hash = wg.sha16(body)
    appendix_count_after = body.count("[L-")
    if pending and before_hash == after_hash:
        print(json.dumps({"ok": False, "lessons_candidates": len(cands),
                          "lessons_merged": merged, "appendix_added": added,
                          "reason": "lessons.md 哈希没变 = 没真正写进去，CLOSED 失败"},
                         ensure_ascii=False, indent=2))
        return 5
    if pending and (added + updated) != len(pending):
        print(json.dumps({"ok": False, "lessons_candidates": len(cands),
                          "lessons_merged": merged, "appendix_added": added,
                          "appendix_updated": updated,
                          "reason": "附录新增数 %d + 更新数 %d != 候选合并数 %d，CLOSED 失败"
                                    % (added, updated, len(pending))},
                         ensure_ascii=False, indent=2))
        return 5
    if a.require_lessons and pending:
        missing = [c.get("lesson_id", "") for c in pending
                   if ("[%s]" % c.get("lesson_id", "")) not in body]
        if missing:
            print(json.dumps({"ok": False, "candidates": len(pending),
                              "merged": len(pending), "appendix_added": added,
                              "reason": "这些 lesson_id 在附录里 grep 不到：%s"
                                        % ", ".join(missing)},
                             ensure_ascii=False, indent=2))
            return 5
    # 收口 PASS 线：核验结论里不许有「不自动判失败」的条目（inconclusive 必须为 0）
    evs = wg.read_journal(d)
    inconclusive = sum(1 for e in evs
                       if e.get("action") == "write_verify"
                       and e.get("result") == "inconclusive")
    if a.require_lessons and inconclusive:
        print(json.dumps({"ok": False, "candidates": len(pending),
                          "merged": len(pending), "appendix_added": added,
                          "inconclusive": inconclusive,
                          "reason": "有 %d 条核验结论是 INCONCLUSIVE（不自动判失败），"
                                    "不许收口：先人工复核" % inconclusive},
                         ensure_ascii=False, indent=2))
        return 5

    for c in pending:
        if not c.get("appendix_status"):
            c["status"] = "merged"
    st["lessons_merged"] = len(cands)
    cur = st.get("state")
    if "CLOSED" in TRANSITIONS.get(cur, []):
        transition(root, st, "CLOSED", "阶段收口：%s" % a.stage,
                   {"action": "close", "result": "ok", "stage": a.stage},
                   by="human")
    else:
        st["state"] = "CLOSED"
        st["closed"] = True
        save_run_state(root, st)
        journal(root, st, {"action": "close", "result": "ok", "stage": a.stage,
                           "note": "当前状态 %s 不在 CLOSED 的合法迁移表里，"
                                   "直接收口并记账（不静默）" % cur})
    journal(root, st, {"action": "close_summary", "result": "ok", "stage": a.stage,
                       "lessons_candidates": len(cands), "lessons_merged": len(cands),
                       "candidates": len(pending), "merged": len(pending),
                       "appendix_added": added,
                       "lessons_md_sha256_before": before_hash,
                       "lessons_md_sha256_after": after_hash})
    out = {"ok": True,
           "candidates": len(pending), "merged": len(pending),
           "appendix_added": added, "appendix_updated": updated,
           "lessons_candidates": len(cands), "lessons_merged": len(cands),
           "lessons_pending": 0, "inconclusive": inconclusive, "stage": a.stage,
           "verdict": "PASS",
           "evidence": {
               "journal": os.path.relpath(os.path.join(d, "journal.jsonl"), root),
               "writes": os.path.relpath(os.path.join(d, "writes"), root),
               "evidence_dir": os.path.relpath(os.path.join(d, "evidence"), root),
               "run_id": st.get("run_id"),
           },
           "lesson_ids": [c.get("lesson_id", "") for c in cands],
           "open_lessons": [c.get("lesson_id", "") for c in cands
                            if c.get("appendix_status") == "open"],
           "lessons_md_sha256_before": before_hash,
           "lessons_md_sha256_after": after_hash}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_lesson(a) -> int:
    """手工登记一条 lesson 候选（O4 回流入口）。

    用途：把**故意搁置**的事项也钉进知识回流，而不是让它在对话里蒸发。
    `--status open` 的条目进附录时仍写 status=open（还没做完的事不许写成做完了）。
    """
    root = wg.workspace_root(a.workspace)
    run_id = a.run or wg.read_pointer(root)
    st = load_run_state(root, run_id)
    symptom = (a.symptom or "").strip()
    if not symptom:
        raise SystemExit("lesson 需要 --symptom")
    cands = st.setdefault("lessons_candidates", [])
    for c in cands:
        if c.get("symptom", "")[:120] == symptom[:120]:
            print(json.dumps({"ok": True, "dup": True, "lesson": c},
                             ensure_ascii=False, indent=2))
            return 0
    lid = "L-%s-%s-%s" % (
        (getattr(a, "platform", "") or st.get("platform") or "na").upper()[:6],
        re.sub(r"[^A-Za-z0-9]+", "-", a.error_class or "deferred").strip("-").upper()[:24]
        or "DEFERRED",
        wg.sha16(symptom)[:6])
    cand = {
        "lesson_id": lid,
        "date_utc": time.strftime("%Y-%m-%d", time.gmtime()),
        "stage": st.get("stage_id", ""),
        "source_run_ref": "runs/%s" % st.get("run_id", ""),
        "platform": getattr(a, "platform", "") or st.get("platform", ""),
        "symptom": symptom,
        "dom_signal": a.dom_signal or "",
        "fix": a.fix or "",
        "trigger": getattr(a, "trigger", "") or "",
        "rollback": getattr(a, "rollback", "") or "",
        "verify_cmd": a.verify_cmd or "",
        "blocking": bool(getattr(a, "blocking", False)),
        "status": a.status or "open",
    }
    if (a.status or "open") == "open":
        cand["appendix_status"] = "open"
    cands.append(cand)
    save_run_state(root, st)
    journal(root, st, {"action": "lesson_candidate", "result": "ok",
                       "error_class": a.error_class or "deferred", "lesson": cand})
    print(json.dumps({"ok": True, "lesson": cand}, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------- 平台矩阵


def cmd_matrix(a) -> int:
    """平台矩阵：platform / verified / read_probe / send_allowed。

    `read_probe` 只做**只读**探测（不写、不发、不新开窗口）。
    未实测平台一律 send_allowed=false——**不因为 Kimi 跑通就外推**。

    口径纪律（网页 AI 第 3 轮裁定）：
      · 已绑定且已实测 → 真探测，read_probe=ok/failed
      · 已实测但未绑定（chatgpt）→ `deferred_rule1`：
        **纪律搁置**，不是技术失败。硬规则 1「绑定后永不新开」不接受例外，
        临时绑定 / 侧标签页同样被拒；只有人明确把项目改绑到 ChatGPT 地址后才可探测。
      · 未实测平台 → `skipped_not_bound`：**skipped ≠ probed**，
        本轮没探测过就如实写没探测，绝不假装探测过。
    """
    root = wg.workspace_root(a.workspace)
    wa_st = wa.load_state(wa_state_path(root, a.state))
    bound_platform = wa_st.get("platform") or ""
    rows = []
    for key in sorted(platforms.PLATFORMS):
        p = platforms.get(key)
        verified = bool(p.get("verified"))
        allowed = bool(p.get("write_allowed"))
        read_probe = "skipped"
        detail = ""
        if key == bound_platform and verified and a.probe:
            try:
                sid, _ = session_of(root, a.state)
                ok, data = wa.read_state(p, sid)
                read_probe = "ok" if ok and data.get("editor") else "failed"
                detail = ("editor=%s msg_count=%s reply_count=%s last_len=%s"
                          % (data.get("editor"), data.get("msg_count"),
                             data.get("reply_count"), data.get("last_len"))
                          if ok else str(data))
            except Exception as e:
                read_probe = "failed"
                detail = str(e)[:120]
        elif key == bound_platform and verified:
            read_probe = "not_probed"
            detail = "已绑定，但本轮未开只读探测（--probe 才探）"
        elif verified:
            # chatgpt：历史实测为真，但本轮按硬规则 1 纪律搁置
            read_probe = "deferred_rule1"
            detail = ("纪律搁置，不是技术失败：硬规则 1「绑定后永不新开」——"
                      "临时绑定与侧标签页都不接受例外；"
                      "待人明确把项目改绑到 ChatGPT 地址后再探测")
        else:
            read_probe = "skipped_not_bound"
            detail = ("skipped ≠ probed：未实测平台本轮不探测，"
                      "也不允许临时绑定抢窗口")
        rows.append({"platform": key, "verified": verified,
                     "read_probe": read_probe, "send_allowed": allowed,
                     "write_method": p.get("write_method"), "detail": detail})
    out = {"rows": rows, "row_count": len(rows),
           "bound_platform": bound_platform,
           "rule": "未实测平台 send_allowed=false；read_probe 只读，不写不发；"
                   "未绑定不探测（skipped ≠ probed）；纪律搁置记 deferred_rule1。"}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0



# ---------------------------------------------------------------- 窗口断言（硬规则 6）

def window_report(root: str, state_path: str = "") -> dict:
    """报告「同一浏览器实例下有几个会话」，并给出是否违反硬规则 6。

    硬规则 6 以前只写在 SKILL.md 里靠人守。评审指出：这该是**运行时断言**——
    窗口堆积的直接后果是「state 里的 session 指向一个已经被顶掉的窗口」，
    表现为 `msg_count=0`、`editor=false`，然后被误判成「平台改版了」。
    """
    wa_st = wa.load_state(wa_state_path(root, state_path))
    bound = wa_st.get("browser") or ""
    by = {}
    for s in wa.list_sessions():
        b = s.get("browser") or s.get("browser_instance_id") or "unknown"
        by.setdefault(b, []).append({"session": s.get("id") or "",
                                     "window": s.get("window") or ""})
    violations = [b for b, lst in by.items() if len(lst) > 1]
    return {
        "ok": not violations,
        "bound_browser": bound,
        "instances": [{"browser": b, "sessions": lst, "count": len(lst)}
                      for b, lst in sorted(by.items())],
        "violations": violations,
        "bound_instance_count": len(by.get(bound, [])) if bound else None,
        "rule": "硬规则 6：同一浏览器实例内只允许一个会话/Agent Window",
    }


def cmd_unblock(a) -> int:
    """人工解除卡点：PAUSED_HUMAN → <to>，actor=human。

    为什么必须有这个命令：`PAUSED_HUMAN` 的出口是修补型迁移，
    actor 门控不允许 agent 自走（否则「两次失败停」形同虚设）。
    人给出理由后走这里，迁移与理由一起进 journal。
    """
    root = wg.workspace_root(a.workspace)
    run_id = a.run or wg.read_pointer(root)
    st = load_run_state(root, run_id)
    ok = transition(root, st, a.to, "人工解除卡点：%s" % (a.reason or ""),
                    {"action": "unblock", "result": "ok", "to": a.to,
                     "reason": a.reason or ""}, by="human")
    print(json.dumps({"ok": ok, "state": st.get("state"),
                      "from": (st.get("state_history") or [{}])[-1].get("from"),
                      "last_guard_failure": st.get("last_guard_failure")},
                     ensure_ascii=False, indent=2))
    return 0 if ok else 5


def cmd_windows(a) -> int:
    root = wg.workspace_root(a.workspace)
    rep = window_report(root, a.state)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0 if rep["ok"] else 5


def _window_guard(root: str, st: dict, a, checkpoint: str) -> tuple:
    """守卫：窗口数 >1 就拒绝继续（返回 (ok, reason)）。"""
    try:
        rep = window_report(root, a.state)
    except Exception as e:
        return True, "窗口断言跳过（读不到 bsk：%s）" % str(e)[:80]
    if rep["ok"]:
        return True, ""
    return False, ("同一浏览器实例下存在 %d 个会话（实例 %s）：违反硬规则 6。"
                   "先收掉多余会话：orchestrator.py windows 看清单"
                   % (max([i["count"] for i in rep["instances"]] or [0]),
                      ",".join(rep["violations"])))


# ---------------------------------------------------------------- 因果链（S7）

def build_trace(d: str, evs: list) -> list:
    """把 journal 拼成 send_id → 判定 → 回复文件 的因果链。

    没有这条链，事后只能靠时间戳猜「这条 delta 是哪次发出来的」——
    而那正是历史上「假重发」难查的深层原因。
    """
    verifies = {}
    for e in evs:
        if e.get("action") == "write_verify":
            key = e.get("send_id") or e.get("approval_id") or ""
            verifies[key] = e
    chain = []
    for e in evs:
        if e.get("action") != "write_send":
            continue
        sid = e.get("send_id") or ""
        aid = e.get("approval_id") or ""
        ver = verifies.get(sid) or verifies.get(aid) or {}
        rnd = e.get("round_id") or 0
        reply = os.path.join(d, "replies", "round-%03d.md" % int(rnd))
        chain.append({
            "send_id": sid or "(无 send_id：旧事件)",
            "approval_id": aid,
            "round": rnd,
            "send_result": e.get("result"),
            "signals": e.get("signals") or [],
            "count_delta": e.get("reply_count_delta"),
            "effective_delta": e.get("effective_delta"),
            "verify_verdict": ver.get("verdict") or "(未核验)",
            "reply_file": os.path.relpath(reply, d) if os.path.exists(reply) else None,
            "error_class": e.get("error_class") or "",
        })
    return chain


# ---------------------------------------------------------------- 需求闸门（S6）

def _run_card_gate(card_path: str) -> dict:
    """调用 scripts/requirement_gate.py（子进程，保持「否定式校验」边界）。"""
    cmd = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "requirement_gate.py"),
           "--card", card_path, "--json"]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=60)
    out = (p.stdout or "").strip()
    try:
        return json.loads(out)
    except Exception:
        return {"ok": False, "violations": [{"code": "GATE_CRASH",
                                             "section": "", "detail": out[:200]}]}


def cmd_card(a) -> int:
    res = _run_card_gate(a.file or getattr(a, "file_opt", "") or "")
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description="星舵循环编排器")
    ap.add_argument("--workspace", default="", help="工作区根（默认当前目录）")
    ap.add_argument("--state", default="", help="wa 的 state 文件路径")
    ap.add_argument("--platform", default="", help="平台 key")
    ap.add_argument("--run", default="", help="run_id（默认读 state/current）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_pl(subp):
        subp.add_argument("--workspace", default=argparse.SUPPRESS)
        subp.add_argument("--state", default=argparse.SUPPRESS)
        subp.add_argument("--platform", default=argparse.SUPPRESS)
        subp.add_argument("--run", default=argparse.SUPPRESS)
        return subp

    p = add_pl(sub.add_parser("init", help="建一个 run（落盘目标与契约）"))
    p.add_argument("--goal", default="")
    p.add_argument("--stage", default="")
    p.add_argument("--slug", default="")
    p.add_argument("--max-rounds", type=int, default=0)
    p.add_argument("--max-same-error", type=int, default=0)
    p.add_argument("--round-timeout", type=int, default=0)
    p.add_argument("--send-timeout", type=int, default=0)
    p.add_argument("--card", default="", help="需求卡片路径：先过否定式校验才建 run")
    p.add_argument("--exec-mode", default="", choices=["", "step", "auto"],
                   help="执行模式：step=逐步确认（缺省）；auto=无人值守。"
                        "缺省时读需求卡片的「执行模式」字段")
    p.set_defaults(func=cmd_init)

    p = add_pl(sub.add_parser("mode", help="查看/切换执行模式（mid-loop 可切）"))
    p.add_argument("--set", default="", choices=["", "step", "auto"])
    p.add_argument("--reason", default="", help="为什么切：写进切换记录，事后要能解释")
    p.add_argument("--by", default="human")
    p.add_argument("--force", action="store_true", help="已是该模式也照样记一条")
    p.set_defaults(func=cmd_mode)

    p = add_pl(sub.add_parser("bind", help="绑定对话（沿用 wa.py 的绑定逻辑）"))
    p.add_argument("url", nargs="?", default="")
    p.add_argument("--new", action="store_true")
    p.add_argument("--reuse", action="store_true",
                   help="复用 wa state 里已绑定的对话（不重新 bind、不新增地址）")
    p.add_argument("--reuse-existing", action="store_true", dest="reuse_existing",
                   help="显式接管一条已有消息的对话（默认拒绝非本项目新建的对话）")
    p.set_defaults(func=cmd_bind)

    p = add_pl(sub.add_parser("plan", help="落盘规划产物 → PLAN_OK"))
    p.add_argument("--file", required=True)
    p.set_defaults(func=cmd_plan)

    p = add_pl(sub.add_parser("loop", help="推进循环"))
    p.add_argument("--once", action="store_true", help="推进一个状态步")
    p.add_argument("--round", action="store_true", help="推完一轮")
    p.add_argument("--resume", action="store_true", help="崩溃恢复（只复核，不重发）")
    p.add_argument("--file", default="", help="本轮要发的正文文件")
    p.set_defaults(func=cmd_loop)

    p = add_pl(sub.add_parser("approve", help="人工卡点：批准一次真实写"))
    p.add_argument("--approval", required=True)
    p.add_argument("--ttl", type=int, default=600)
    p.add_argument("--by", default="human")
    p.set_defaults(func=cmd_approve)

    p = add_pl(sub.add_parser("status", help="打印状态、守卫失败原因、剩余预算"))
    p.set_defaults(func=cmd_status)

    p = add_pl(sub.add_parser("audit", help="审计：重复发送/未确认/越权写/待回流"))
    p.add_argument("--trace", action="store_true",
                   help="输出 send_id → 判定 → 回复文件 的因果链")
    p.set_defaults(func=cmd_audit)

    p = add_pl(sub.add_parser("recover", help="解释恢复决策（不执行）"))
    p.set_defaults(func=cmd_recover)

    p = add_pl(sub.add_parser("close", help="阶段收口：强制合并 lesson 候选"))
    p.add_argument("--stage", required=True)
    p.add_argument("--require-lessons", action="store_true")
    p.set_defaults(func=cmd_close)

    p = add_pl(sub.add_parser("lesson", help="手工登记一条 lesson 候选（O4 回流入口）"))
    p.add_argument("--symptom", required=True)
    p.add_argument("--fix", default="")
    p.add_argument("--verify-cmd", default="")
    p.add_argument("--dom-signal", default="")
    p.add_argument("--trigger", default="", help="触发条件（什么情况下会踩到）")
    p.add_argument("--rollback", default="", help="回退方式（踩到了怎么退回来）")
    p.add_argument("--error-class", default="deferred")
    p.add_argument("--blocking", action="store_true",
                   help="这条不闭合就不许阶段收口（纪律类漏洞才用；平台/体验类留 false）")
    p.add_argument("--status", default="open", choices=["open", "merged"])
    p.set_defaults(func=cmd_lesson)

    p = add_pl(sub.add_parser("unblock", help="人工解除卡点（PAUSED_HUMAN → READY）"))
    p.add_argument("--to", default="READY", help="目标状态（默认 READY）")
    p.add_argument("--reason", default="", help="为什么可以继续（进 journal）")
    p.set_defaults(func=cmd_unblock)

    p = add_pl(sub.add_parser("windows", help="硬规则 6 断言：同一实例下的会话数"))
    p.set_defaults(func=cmd_windows)

    p = add_pl(sub.add_parser("card", help="需求卡片否定式校验（只拒绝，不生成）"))
    p.add_argument("file", nargs="?", default="", help="需求卡片路径（也可用 --file）")
    p.add_argument("--file", dest="file_opt", default="",
                   help="等价于位置参数；文档里更常见，保留两种写法")
    p.set_defaults(func=cmd_card)

    p = add_pl(sub.add_parser("matrix", help="平台矩阵：verified / read_probe / send_allowed"))
    p.add_argument("--probe", action="store_true", help="对已实测平台跑只读探测")
    p.add_argument("--session", default="", help="已实测平台探测用的 bsk session id")
    p.set_defaults(func=cmd_matrix)

    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
