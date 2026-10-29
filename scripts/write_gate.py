#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""write_gate.py —— 写闸门（O3）+ 编排层/闸门层共用的落盘原语。

铁律（写闸门存在的全部意义）：

1. **缺省就是 dry-run**。真实写发生当且仅当同时满足：
   `STARPILOT_WRITE_MODE=single_write` **且** 存在**未过期**的 approval。
2. 前置校验失败一律返回 `WRITE_BLOCKED_*`，**且不产生** result 文件——
   「没批就不许写」必须能被审计证明，而不只是报错。
3. 写入路径必须是该平台**已实测**的那一条（见 `platforms.py` 的 `write_allowed`）。
   没实测的平台（`verified=False`）一律禁止真实写。
4. 真实写只发**一条**，发完立刻记 `reply_count_before/after`；
   **delta 必须正好等于 1**——口径是「助手回复条数」（一条提问对一条回复），
   不是消息总数（总数会把「我这条 + 平台那条」算成 2）。

本文件同时提供编排层与闸门层共用的落盘原语（原子写、journal、run 目录定位），
避免第三个文件和循环依赖：`orchestrator.py` 导入本文件，**本文件不导入 orchestrator**。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import confirm  # noqa: E402
import platforms  # noqa: E402
import wa  # noqa: E402

# ---------------------------------------------------------------- 落盘原语


def sha16(s: str) -> str:
    """审计用短哈希（16 位十六进制）。"""
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()[:16]


def atomic_write_text(path: str, text: str) -> None:
    """原子写：tmp → flush → fsync → os.replace。

    断电/崩溃时要么是旧内容、要么是新内容，**不会出现半截文件**。
    """
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, ".%s.tmp" % os.path.basename(path))
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def atomic_write_json(path: str, obj) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))


def append_journal(run_dir: str, event: dict) -> None:
    """journal.jsonl 追加一条事件（append-only + fsync）。"""
    os.makedirs(run_dir, exist_ok=True)
    path = os.path.join(run_dir, "journal.jsonl")
    line = json.dumps(event, ensure_ascii=False, sort_keys=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_journal(run_dir: str) -> list:
    path = os.path.join(run_dir, "journal.jsonl")
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
    return out


def workspace_root(explicit: str = "") -> str:
    return os.path.abspath(explicit or os.getcwd())


def pointer_path(root: str) -> str:
    return os.path.join(root, "state", "current")


def read_pointer(root: str) -> str:
    p = pointer_path(root)
    if not os.path.exists(p):
        return ""
    with open(p, encoding="utf-8") as f:
        return f.read().strip()


def set_pointer(root: str, run_id: str) -> None:
    atomic_write_text(pointer_path(root), run_id + "\n")


def run_dir_of(root: str, run_id: str = "") -> str:
    rid = run_id or read_pointer(root)
    if not rid:
        raise SystemExit("找不到当前 run：state/current 是空的。先跑 orchestrator.py init。")
    d = os.path.join(root, "runs", rid)
    if not os.path.isdir(d):
        raise SystemExit("run 目录不存在：%s" % d)
    return d


def new_run_id(slug: str = "") -> str:
    """run_id = <UTC 时间>-<slug>。"""
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    s = "".join(ch for ch in (slug or "run") if ch.isalnum() or ch in "-_")[:32] or "run"
    return "%s-%s" % (stamp, s)


# ---------------------------------------------------------------- 闸门

BLOCK_CODES = {
    "WRITE_BLOCKED_MODE_DRY_RUN": "当前是 dry-run 模式，真实写被禁止（缺省即 dry_run）",
    "WRITE_BLOCKED_NO_APPROVAL": "没有有效批准（approval 缺失或已过期）",
    "WRITE_BLOCKED_TARGET_MISMATCH": "目标与 dry-run 记录不一致（URL hash 或正文 hash 对不上）",
    "WRITE_BLOCKED_PLATFORM_UNVERIFIED": "该平台未实测（verified=False），禁止真实写",
    "WRITE_BLOCKED_WRITE_METHOD": "写入路径不在该平台已实测的白名单里",
    "WRITE_BLOCKED_GUARD": "DOM 守卫不过（编辑器不干净 / 有弹窗遮罩 / 不能发送 / 地址漂移）",
    "WRITE_BLOCKED_DUPLICATE": "同指纹已发过，禁止重复发送",
}


def blocked(code: str, detail: str = "", extra: dict = None) -> int:
    out = {"ok": False, "error_code": code,
           "message": BLOCK_CODES.get(code, code), "detail": detail}
    if extra:
        out.update(extra)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 2


def wa_state_path(root: str, explicit: str = "") -> str:
    """workspace 内的 wa state 路径。

    **必须相对 workspace 解析**，不能用 `wa.state_path()` 的 cwd 缺省：
    闸门是**子进程**，cwd 是调用方的目录。早先这里直接用 cwd 缺省，
    结果 `--workspace runtime/e2e` 的闸门读到了项目根目录的 `.wa-state.json`——
    也就是**另一个对话的绑定**（kimi），而编排器记的是 deepseek 的 hash。
    两边各自自洽，跨进程却对不上，差点把消息发到错的对话里
    （2026-10-29 端到端实测抓到的缺陷，谢谢窗口守卫先挡了一下）。
    """
    return explicit or os.path.join(root, ".wa-state.json")


def write_mode() -> str:
    """缺省必须是 dry_run。"""
    return (os.environ.get("STARPILOT_WRITE_MODE") or "dry_run").strip().lower()


def allowed_write_methods(p: dict) -> list:
    return list(p.get("write_allowed") or [])


# ---------------------------------------------------------------- DOM 快照


def dom_snapshot(p: dict, sid: str) -> dict:
    """取一份用于「写入前后对比」的 DOM 快照。"""
    ok, cur = wa.read_state(p, sid)
    if not ok:
        return {"ok": False, "error": str(cur)}
    dom_hash = sha16("|".join([
        str(cur.get("url") or ""),
        str(cur.get("msg_count")),
        str(cur.get("editor_len")),
        str(cur.get("last_len")),
        str(cur.get("generating")),
    ]))
    return {
        "ok": True,
        "url": cur.get("url"),
        "msg_count": cur.get("msg_count"),
        "reply_count": cur.get("reply_count"),
        "editor_len": cur.get("editor_len"),
        "last_len": cur.get("last_len"),
        "generating": cur.get("generating"),
        "modal": bool(cur.get("modal")),
        "modal_text": cur.get("modal_text") or "",
        "can_send": bool(cur.get("can_send")),
        "write_method": p.get("write_method"),
        "dom_hash": dom_hash,
        "raw": cur,
    }


def _url_key(u: str) -> str:
    """URL 比较键：去掉 query / fragment / 末尾斜杠。"""
    return (u or "").split("?")[0].split("#")[0].rstrip("/")


def guard_ok(snap: dict, bound_url: str = "") -> tuple:
    """DOM 守卫：地址没漂 + 编辑器存在 + 输入框干净 + 无弹窗遮罩。返回 (ok, 原因)。

    **地址守卫必须比「活页面」而不是比 state 里的副本**：
    state 里的 url 是绑定时记下的，页面可能早就被导航到别处（人手切了个标签页，
    或工具复用了同一个 session 去读了另一个平台）。只比副本 = 比了个寂寞，
    消息会写进错的对话，而记录里全是「正确」的 hash。
    （2026-10-29 端到端实测抓到：闸门把 kimi 的页面当成 deepseek 的目标。）

    注意**不**把 `can_send` 当守卫：输入框为空时发送按钮必然是 disabled，
    拿它当条件会让「空输入框」永远过不了守卫。
    """
    if bound_url and _url_key(snap.get("url")) != _url_key(bound_url):
        return False, ("地址漂移：绑定 %s，当前页面 %s —— 拒绝在任何非目标对话上操作"
                       % (_url_key(bound_url), _url_key(snap.get("url"))))
    if snap.get("error"):
        return False, "探测失败：%s" % snap.get("error")
    if snap.get("modal"):
        return False, "页面有弹窗/遮罩：%s" % (snap.get("modal_text") or "")
    if not (snap.get("raw") or {}).get("editor"):
        return False, "找不到输入框（DOM 改版了？）"
    if snap.get("editor_len") != 0:
        return False, "输入框里已有 %s 字，先清空" % snap.get("editor_len")
    return True, ""


# ---------------------------------------------------------------- plan


def cmd_plan(a) -> int:
    root = workspace_root(a.workspace)
    text = ""
    if a.file:
        with open(a.file, encoding="utf-8") as f:
            text = f.read()
    text = (a.text or text).strip()
    if not text:
        return blocked("WRITE_BLOCKED_GUARD", "正文为空，拒绝生成计划")

    st = wa.load_state(wa_state_path(root, a.state))
    p = platforms.get(a.platform or st.get("platform"))
    if not p.get("verified"):
        return blocked("WRITE_BLOCKED_PLATFORM_UNVERIFIED",
                       "平台 %s 未实测" % p.get("key"), {"platform": p.get("key")})
    if p.get("write_method") not in allowed_write_methods(p):
        return blocked("WRITE_BLOCKED_WRITE_METHOD",
                       "平台 %s 只允许 %s，实际配置 %s" % (
                           p.get("key"), allowed_write_methods(p), p.get("write_method")),
                       {"platform": p.get("key")})

    sid = wa.ensure_session(st, wa_state_path(root, a.state))
    url = st.get("url") or ""
    if not url:
        return blocked("WRITE_BLOCKED_TARGET_MISMATCH", "还没绑定对话地址（先 wa.py bind）")
    snap = dom_snapshot(p, sid)
    ok, why = guard_ok(snap, url)
    if not ok:
        return blocked("WRITE_BLOCKED_GUARD", why)

    url_sha = sha16(url)
    prompt_sha = sha16(text)
    approval_id = "ap-%s-%s" % (prompt_sha, url_sha)
    run_dir = run_dir_of(root, a.run)
    dry = {
        "approval_id": approval_id,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "stage_id": a.stage or "",
        "platform": p.get("key"),
        "write_method": p.get("write_method"),
        "write_allowed": allowed_write_methods(p),
        "target_url": url,
        "target_url_sha256": url_sha,
        "prompt_sha256": prompt_sha,
        "prompt_chars": len(text),
        "prompt_file": os.path.abspath(a.file) if a.file else "",
        "selectors": {
            "editor": p.get("sel", {}).get("editor") or [],
            "send": p.get("sel", {}).get("send") or [],
        },
        "dom_before": snap,
        "planned_action": "send_one_message",
        "mode_at_plan": write_mode(),
        "note": "本文件只是计划。真实写需要 STARPILOT_WRITE_MODE=single_write + 未过期的批准。",
    }
    atomic_write_json(os.path.join(run_dir, "writes", "%s.dry.json" % approval_id), dry)
    print(json.dumps({"ok": True, "approval_id": approval_id,
                      "run_dir": run_dir,
                      "dry": os.path.join("writes", "%s.dry.json" % approval_id),
                      "mode": write_mode(),
                      "next": "orchestrator.py approve --approval %s --ttl 600" % approval_id},
                     ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------- approve


def cmd_approve(a) -> int:
    root = workspace_root(a.workspace)
    run_dir = run_dir_of(root, a.run)
    dry_path = os.path.join(run_dir, "writes", "%s.dry.json" % a.approval)
    if not os.path.exists(dry_path):
        return blocked("WRITE_BLOCKED_NO_APPROVAL", "找不到 dry-run 计划：%s" % dry_path)
    with open(dry_path, encoding="utf-8") as f:
        dry = json.load(f)
    ttl = max(60, int(a.ttl))
    rec = {
        "approval_id": a.approval,
        "approved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "expires_at": time.time() + ttl,
        "ttl": ttl,
        "approver": a.by or "human",
        "target_url_sha256": dry.get("target_url_sha256"),
        "prompt_sha256": dry.get("prompt_sha256"),
        "note": "批准只对这一组 (URL hash, prompt hash) 生效。",
    }
    atomic_write_json(os.path.join(run_dir, "writes", "%s.approval.json" % a.approval), rec)
    print(json.dumps({"ok": True, "approval_id": a.approval, "ttl": ttl,
                      "expires_at": rec["expires_at"]},
                     ensure_ascii=False, indent=2))
    return 0


def load_approval(run_dir: str, approval_id: str) -> tuple:
    p = os.path.join(run_dir, "writes", "%s.approval.json" % approval_id)
    if not os.path.exists(p):
        return None, "没有批准记录：%s" % approval_id
    with open(p, encoding="utf-8") as f:
        rec = json.load(f)
    if time.time() > float(rec.get("expires_at") or 0):
        return None, "批准已过期（%s）" % rec.get("approved_at")
    return rec, ""


# ---------------------------------------------------------------- send


def _snap(cur: dict, dom_hash: str = "") -> dict:
    """快照裁剪的唯一实现在 confirm.trim（两层共用）。"""
    return confirm.trim(cur, dom_hash)


def execute_send(p: dict, text: str, sid: str, round_id: int = 0,
                 reply_timeout: int = 60) -> dict:
    """真实发一条。返回结构化结果（不自己写 journal，交给调用方记）。

    只做「确定性动作」：写入 → 点发送 → 等确认。
    确认判据：消息数增加 / 地址变了 / 正文指纹命中（**不是**「输入框清空」——
    实测 Kimi 限流时会清空输入框却拒绝建会话）。

    **两个计数必须分开**：
      · `msg_count`  —— 平台相关（Kimi 是消息总数：我这条 + 平台那条 = +2）
      · `reply_count` —— 助手回复条数（平台无关）：「一条提问对一条回复」= +1
    `reply_count_delta` 一律用后者，判定标准是 **正好 1**。
    """
    ok, cur = wa.read_state(p, sid)
    if not ok:
        return {"ok": False, "error_class": "probe_failed", "detail": str(cur)}
    before = _snap(cur, dom_snapshot(p, sid).get("dom_hash"))
    want = len(text)
    # 写入的**唯一实现**在 wa.do_fill（CLI 与闸门共用）：
    # 它按平台已实测的路径写（native / exec / paste），并做「编辑器到底收下了没有」的判定。
    # 历史上这里是第二份实现 —— 两份实现必然漂移，而且漂移时没人会去比对（评审 F2 的同类）。
    fill = wa.do_fill(p, text, sid)
    if not fill.get("ok"):
        code = fill.get("code") or ""
        err = ("write_len_mismatch" if code == platforms.FILL_SHORT else
               "fill_not_accepted" if code in (platforms.FILL_BLANK, platforms.FILL_NO_SEND) else
               "probe_failed_after_fill" if code == "probe_failed_after_fill" else
               "fill_failed")
        return {"ok": False, "error_class": err, "detail": fill.get("detail") or "",
                "fill": fill, "before": before}

    ok, res = wa.do_send(p, sid)
    if not ok:
        return {"ok": False, "error_class": "click_send_failed", "detail": str(res),
                "before": before}

    signals = []
    confirmed = False
    after = {"url": None, "msg_count": None, "reply_count": None,
             "editor_len": None, "dom_hash": None}
    for _ in range(20):
        time.sleep(1.5)
        ok, cur3 = wa.read_state(p, sid)
        if not ok:
            continue
        url_changed = bool(before.get("url")) and (cur3.get("url") or "") != before.get("url")
        msgs_up = (cur3.get("msg_count") or 0) > (before.get("msg_count") or 0)
        if msgs_up:
            signals.append("msg_count")
        if url_changed:
            signals.append("url")
        if msgs_up or url_changed:
            confirmed = True
            # 关键：**用确认这一刻的快照**当 after，不再多读一次。
            after = _snap(cur3, dom_snapshot(p, sid).get("dom_hash"))
            break
        ok2, nd = wa.evaluate(platforms.js_has_text(p, text), sid)
        if ok2 and isinstance(nd, dict) and nd.get("ok"):
            signals.append("needle")
            confirmed = True
            after = _snap(cur3, dom_snapshot(p, sid).get("dom_hash"))
            break
    if not confirmed and after.get("msg_count") is None:
        # 未确认：也要留下最后一次观测的快照，编排层据此判定弱信号
        after = {"url": None, "msg_count": None, "reply_count": None,
                 "editor_len": None, "dom_hash": None}
        try:
            ok5, cur5 = wa.read_state(p, sid)
            if ok5:
                after = _snap(cur5, dom_snapshot(p, sid).get("dom_hash"))
        except Exception:
            pass

    # ---- 送达 ≠ 已有回复：按「助手回复条数」口径把 delta 量准 ----
    # 平台会先落地我这条提问，助手消息稍后才建好。若在「送达」那一刻就取
    # after，reply_count 可能还是 0（口径不准则算出 2 —— 上一轮的根因）。
    # 这里最多再等 send_timeout 秒，等助手消息出现，delta 才是可判定的数。
    reply_pending = False
    if confirmed:
        deadline = time.time() + max(5, int(reply_timeout))
        while time.time() < deadline:
            if (after.get("reply_count") or 0) > (before.get("reply_count") or 0):
                break
            time.sleep(1.5)
            ok4, cur4 = wa.read_state(p, sid)
            if not ok4:
                continue
            after = _snap(cur4, dom_snapshot(p, sid).get("dom_hash"))
        reply_pending = (after.get("reply_count") or 0) <= (before.get("reply_count") or 0)

    # ---- 判定：口径唯一来源是 confirm.classify_send_result（F2）----
    # 这里只负责采快照、喂信号，不再自己写一套 if-else ——
    # 历史上 wa.py 与本文件各判一套，漂移出过「假重发」。
    clas = confirm.classify_send_result(before, after, signals)

    return {
        "ok": confirmed,
        "verdict": clas["verdict"],
        "reason": clas["reason"],
        "error_class": "" if confirmed else "send_unconfirmed",
        "signals": signals,
        "before": before,
        "after": after,
        # 口径：助手回复条数（一条提问对一条回复 ⇒ 正好 1）
        "reply_count_before": before.get("reply_count"),
        "reply_count_after": after.get("reply_count"),
        "reply_count_delta": ((after.get("reply_count") or 0)
                              - (before.get("reply_count") or 0)),
        # 判定实际采用的增量：锚点口径优先（对虚拟列表免疫），见 confirm.py
        "effective_delta": clas["effective_delta"],
        "delta_source": clas["delta_source"],
        "anchors": clas["anchors"],
        "count_anchor_disagree": clas["count_anchor_disagree"],
        # 平台相关的总消息数（仅作审计参考，不作判据）
        "msg_count_before": before.get("msg_count"),
        "msg_count_after": after.get("msg_count"),
        "msg_count_delta": ((after.get("msg_count") or 0)
                            - (before.get("msg_count") or 0)),
        # 用户消息条数（DOM 口径）：一条回复被拆成 N 个气泡 vs 真的重发 N 条
        "user_msg_count_before": before.get("user_msg_count"),
        "user_msg_count_after": after.get("user_msg_count"),
        "user_msg_delta": ((after.get("user_msg_count") or 0)
                           - (before.get("user_msg_count") or 0)),
        "reply_pending": reply_pending,
        "round_id": round_id,
    }


def _event(run_dir: str, round_id: int, dry: dict, res: dict,
           action: str = "write_send", send_id: str = "") -> dict:
    st = _load_run_state(run_dir)
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "run_id": st.get("run_id", ""),
        "round_id": round_id,
        "target_url_sha256": dry.get("target_url_sha256") or st.get("target_url_sha256", ""),
        "prompt_sha256": dry.get("prompt_sha256", ""),
        "action": action,
        "before": res.get("before"),
        "after": res.get("after"),
        "result": "ok" if res.get("ok") else "failed",
        "error_class": res.get("error_class", ""),
        "approval_id": dry.get("approval_id", ""),
        "send_id": send_id,
        # 口径必须跟着事件走：只记 reply_count_delta 而不记判定实际用的口径，
        # 事后复盘会以为「delta=0 = 没发出去」（2026-10-29 真实踩到）
        "verdict": res.get("verdict", ""),
        "delta_source": res.get("delta_source", ""),
        "effective_delta": res.get("effective_delta"),
        "write_mode": write_mode(),
        "signals": res.get("signals", []),
        "reply_count_delta": res.get("reply_count_delta"),
        "msg_count_delta": res.get("msg_count_delta"),
        "reply_pending": res.get("reply_pending", False),
    }


def _load_run_state(run_dir: str) -> dict:
    p = os.path.join(run_dir, "state.json")
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def cmd_send(a) -> int:
    root = workspace_root(a.workspace)
    run_dir = run_dir_of(root, a.run)
    dry_path = os.path.join(run_dir, "writes", "%s.dry.json" % a.approval)
    if not os.path.exists(dry_path):
        return blocked("WRITE_BLOCKED_NO_APPROVAL", "找不到 dry-run 计划：%s" % dry_path)
    with open(dry_path, encoding="utf-8") as f:
        dry = json.load(f)

    # ---- 前置校验（顺序即优先级）----
    if write_mode() != "single_write":
        return blocked("WRITE_BLOCKED_MODE_DRY_RUN",
                       "STARPILOT_WRITE_MODE=%s" % write_mode(),
                       {"approval_id": a.approval, "mode": write_mode()})
    rec, why = load_approval(run_dir, a.approval)
    if rec is None:
        return blocked("WRITE_BLOCKED_NO_APPROVAL", why, {"approval_id": a.approval})

    st = wa.load_state(wa_state_path(root, a.state))
    p = platforms.get(a.platform or st.get("platform"))
    if not p.get("verified"):
        return blocked("WRITE_BLOCKED_PLATFORM_UNVERIFIED",
                       "平台 %s 未实测" % p.get("key"), {"approval_id": a.approval})
    if p.get("write_method") not in allowed_write_methods(p):
        return blocked("WRITE_BLOCKED_WRITE_METHOD",
                       "平台 %s 只允许 %s" % (p.get("key"), allowed_write_methods(p)),
                       {"approval_id": a.approval})

    if dry.get("platform") and p.get("key") != dry.get("platform"):
        return blocked("WRITE_BLOCKED_TARGET_MISMATCH",
                       "平台不一致：计划 %s，当前 %s" % (dry.get("platform"), p.get("key")),
                       {"approval_id": a.approval})

    url = st.get("url") or ""
    if sha16(url) != dry.get("target_url_sha256"):
        return blocked("WRITE_BLOCKED_TARGET_MISMATCH",
                       "当前 URL hash %s != 计划 %s" % (sha16(url), dry.get("target_url_sha256")),
                       {"approval_id": a.approval})

    text = ""
    pf = dry.get("prompt_file") or ""
    if pf and os.path.exists(pf):
        with open(pf, encoding="utf-8") as f:
            text = f.read().strip()
    if not text:
        return blocked("WRITE_BLOCKED_TARGET_MISMATCH",
                       "计划里的正文文件读不到：%s" % pf,
                       {"approval_id": a.approval})
    if sha16(text) != dry.get("prompt_sha256"):
        return blocked("WRITE_BLOCKED_TARGET_MISMATCH",
                       "正文已变（hash %s != 计划 %s）" % (sha16(text), dry.get("prompt_sha256")),
                       {"approval_id": a.approval})

    sid = wa.ensure_session(st, wa_state_path(root, a.state))
    snap = dom_snapshot(p, sid)
    ok, why = guard_ok(snap, url)
    if not ok:
        return blocked("WRITE_BLOCKED_GUARD", why, {"approval_id": a.approval})

    # ---- 幂等：同指纹已发过就不许再发 ----
    if not a.force and _already_sent(run_dir, dry):
        return blocked("WRITE_BLOCKED_DUPLICATE",
                       "同 (URL hash, prompt hash) 已发过", {"approval_id": a.approval})

    # ---- 真发 ----
    run_state = _load_run_state(run_dir)
    reply_timeout = int((run_state.get("goal") or {}).get("send_timeout") or 60)
    res = execute_send(p, text, sid, round_id=a.round, reply_timeout=reply_timeout)
    res["approval_id"] = a.approval
    res["send_id"] = getattr(a, "send_id", "") or ""
    res["mode"] = write_mode()
    res["write_method"] = p.get("write_method")
    if not res.get("ok"):
        # 失败**不**产生 result.json（闸门语义：没成功就没产物）
        res["result_file"] = None
        append_journal(run_dir, _event(run_dir, a.round, dry, res,
                                       send_id=getattr(a, "send_id", "") or ""))
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 4
    result_path = os.path.join(run_dir, "writes", "%s.result.json" % a.approval)
    atomic_write_json(result_path, res)
    res["result_file"] = os.path.relpath(result_path, run_dir)
    append_journal(run_dir, _event(run_dir, a.round, dry, res,
                                   send_id=getattr(a, "send_id", "") or ""))
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def _already_sent(run_dir: str, dry: dict) -> bool:
    for ev in read_journal(run_dir):
        if ev.get("action") == "write_send" and ev.get("result") == "ok":
            if (ev.get("prompt_sha256") == dry.get("prompt_sha256")
                    and ev.get("target_url_sha256") == dry.get("target_url_sha256")):
                return True
    return False


# ---------------------------------------------------------------- verify


def cmd_verify(a) -> int:
    root = workspace_root(a.workspace)
    run_dir = run_dir_of(root, a.run)
    dry_path = os.path.join(run_dir, "writes", "%s.dry.json" % a.approval)
    res_path = os.path.join(run_dir, "writes", "%s.result.json" % a.approval)
    if not os.path.exists(dry_path):
        return blocked("WRITE_BLOCKED_NO_APPROVAL", "找不到 dry-run 计划")
    if not os.path.exists(res_path):
        return blocked("WRITE_BLOCKED_NO_APPROVAL",
                       "没有 result 文件 = 这一步没写成（被闸门挡住或发送失败）")
    with open(dry_path, encoding="utf-8") as f:
        dry = json.load(f)
    with open(res_path, encoding="utf-8") as f:
        res = json.load(f)

    count_delta = res.get("reply_count_delta")
    # 判定用「锚点口径」的增量；老 result 文件没有这个字段时退回计数口径
    delta = res.get("effective_delta")
    delta_source = res.get("delta_source") or "count"
    if delta is None:
        delta = count_delta
    user_delta = res.get("user_msg_delta")
    # reply_bubbles = 助手消息气泡数；logical_replies = 本轮逻辑提问数（我发了几条）
    bubbles, logical = count_delta, user_delta
    if delta == 1:
        checks = [{"name": "reply_count_delta", "expect": "==1",
                   "actual": delta, "ok": True,
                   "note": "口径=%s（计数口径=%s）" % (delta_source, count_delta)}]
        verdict = "VERIFIED"
    elif delta == 0:
        checks = [{"name": "reply_count_delta", "expect": "==1",
                   "actual": delta, "ok": False,
                   "note": "没有新的助手回复：判定失败（送达了但没回）"}]
        verdict = "MISMATCH -> PAUSED_HUMAN"
    else:
        if user_delta and user_delta >= 2:
            checks = [{"name": "reply_count_delta", "expect": "==1",
                       "actual": delta, "ok": False,
                       "note": "用户消息 +%s：这是重发（不是气泡拆分），判失败" % user_delta}]
            verdict = "MISMATCH -> PAUSED_HUMAN"
        elif user_delta == 1:
            # 关键：一条回复被拆成 N 个气泡时**不许直接 MISMATCH**——
            # 记下气泡数与逻辑回复数，判 INCONCLUSIVE 交人工复核。
            checks = [{"name": "reply_count_delta", "expect": "==1",
                       "actual": delta, "ok": False,
                       "note": "reply_bubbles=%s / logical_replies=%s：无法排除"
                               "「一条回复被拆成 N 个气泡」，判 INCONCLUSIVE，"
                               "不自动 MISMATCH" % (bubbles, logical)}]
            verdict = "INCONCLUSIVE -> 需人工复核（不自动判失败）"
        else:
            # user_msg_delta == 0 或缺失：用户消息选择器失效 / 消息消失，
            # 同样**不能**落进 MISMATCH——只有 user_msg_delta>=2 才够格实锤重发。
            checks = [{"name": "reply_count_delta", "expect": "==1",
                       "actual": delta, "ok": False,
                       "note": "reply_bubbles=%s / logical_replies=%s：用户消息数"
                               "不可信（选择器失效或消息消失），无法区分气泡拆分与"
                               "重发，判 INCONCLUSIVE，不自动 MISMATCH"
                               % (bubbles, logical)}]
            verdict = "INCONCLUSIVE -> 需人工复核（不自动判失败）"
    before_hash = ((dry.get("dom_before") or {}).get("dom_hash")) or ""
    after_hash = ((res.get("after") or {}).get("dom_hash")) or ""
    checks.append({
        "name": "dom_changed", "expect": "before != after",
        "actual": [before_hash, after_hash],
        "ok": bool(before_hash and after_hash and before_hash != after_hash),
    })
    checks.append({
        "name": "url_not_drifted", "expect": "URL 仍属于同一对话",
        "actual": (res.get("after") or {}).get("url"),
        "ok": True,
    })
    bad = [c for c in checks if not c["ok"]]
    if bad and verdict == "VERIFIED":
        verdict = "MISMATCH -> PAUSED_HUMAN"
    rc = 0 if not bad else (5 if verdict.startswith("MISMATCH") else 6)
    # 核验结论落 journal：audit 的 inconclusive 计数以它为准（可审计，不靠回忆）
    append_journal(run_dir, {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "action": "write_verify",
        "approval_id": a.approval,
        "send_id": getattr(a, "send_id", "") or "",
        "verdict": verdict,
        "result": ("ok" if not bad
                   else ("mismatch" if verdict.startswith("MISMATCH") else "inconclusive")),
        "reply_count_delta": delta,
        "reply_bubbles": bubbles,
        "logical_replies": logical,
        "msg_count_delta": res.get("msg_count_delta"),
        "user_msg_delta": user_delta,
    })
    out = {"ok": not bad, "approval_id": a.approval,
           "verdict_scope": delta_source,
           "effective_delta": delta,
           "count_anchor_disagree": bool(res.get("count_anchor_disagree")),
           "anchors": res.get("anchors") or {},
           "reply_count_delta": res.get("reply_count_delta"),
           "reply_count_before": res.get("reply_count_before"),
           "reply_count_after": res.get("reply_count_after"),
           "reply_bubbles": bubbles, "logical_replies": logical,
           "msg_count_delta": res.get("msg_count_delta"),
           "user_msg_delta": user_delta,
           "reply_pending": res.get("reply_pending", False),
           "result_file": os.path.relpath(res_path, run_dir),
           "checks": checks,
           "verdict": verdict}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return rc


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description="星舵写闸门（缺省 dry-run）")
    ap.add_argument("--workspace", default="", help="工作区根（默认当前目录）")
    ap.add_argument("--state", default="", help="wa 的 state 文件路径（默认 ./.wa-state.json）")
    ap.add_argument("--platform", default="", help="平台 key（默认读 state）")
    ap.add_argument("--run", default="", help="run_id（默认读 state/current）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_pl(subp):
        subp.add_argument("--workspace", default=argparse.SUPPRESS)
        subp.add_argument("--state", default=argparse.SUPPRESS)
        subp.add_argument("--platform", default=argparse.SUPPRESS)
        subp.add_argument("--run", default=argparse.SUPPRESS)
        return subp

    p = add_pl(sub.add_parser("plan", help="生成 dry-run 计划（不发送）"))
    p.add_argument("--file", default="")
    p.add_argument("--text", default="")
    p.add_argument("--stage", default="")
    p.set_defaults(func=cmd_plan)

    p = add_pl(sub.add_parser("approve", help="批准一份计划"))
    p.add_argument("--approval", required=True)
    p.add_argument("--ttl", type=int, default=600)
    p.add_argument("--by", default="human")
    p.set_defaults(func=cmd_approve)

    p = add_pl(sub.add_parser("send", help="按批准发一条（缺省被闸门挡住）"))
    p.add_argument("--approval", required=True)
    p.add_argument("--send-id", default="", help="因果链 id（编排器生成，写进 journal）")
    p.add_argument("--round", type=int, default=0)
    p.add_argument("--force", action="store_true", help="跳过重复发送检查（仅排障用）")
    p.set_defaults(func=cmd_send)

    p = add_pl(sub.add_parser("verify", help="复核一次真实写"))
    p.add_argument("--approval", required=True)
    p.add_argument("--send-id", default="", help="因果链 id（写进 write_verify 事件）")
    p.set_defaults(func=cmd_verify)

    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    raise SystemExit(main())
