#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""confirm.py —— 发送确认判定的**唯一实现**。

为什么单独一个文件：`wa.py`（桥接层）和 `write_gate.py`（闸门层）都需要判断
「这条消息到底发出去没有」。历史上这两处各写了一套，口径漂移过一次——
把「一条提问 + 一条回复」算成 2，看着像重发（见 `references/lessons.md`
L-KIMI-DELTA-METRIC-SCOPE）。漂移是无声的：没人会在改一处时同步检查另一处。

所以判定只留这一份，两层都调它；任何口径改动只改这里，
并由 `docs/orchestrator-contract.md` §2.4 冻结。

判定口径（契约 §2.3 / §2.4）：

| 信号 | 强度 |
|---|---|
| 消息数增加 / 地址变了 / 正文指纹命中 | **主**（三选一即可判送达） |
| 输入框变空 | **弱**（单独出现只能判 INCONCLUSIVE_NOT_SUCCESS） |

回复增量优先用**锚点指纹**（对虚拟列表免疫），锚点不可信时退回节点计数。
"""
from __future__ import annotations

import platforms

# 判定结果（字符串常量，写进 journal 后不许改名，否则旧审计对不上）
SENT = "SENT"                          # 送达且新增正好 1 条助手回复
SENT_NO_REPLY = "SENT_NO_REPLY"        # 送达但没有新回复（助手气泡还没建好 / 平台没回）
SENT_MULTI = "SENT_MULTI"              # 送达但新增 >1（气泡拆分或重发，由 verify 细分）
INCONCLUSIVE_NOT_SUCCESS = "INCONCLUSIVE_NOT_SUCCESS"  # 只有弱信号：既不报成功也不报失败
NOT_SENT = "NOT_SENT"                  # 主信号一个都不满足，输入框也没清空

PRIMARY_SIGNALS = ("msg_count", "url", "needle")


def reply_grew(before: dict, after: dict, min_abs: int = 40, min_ratio: float = 0.02) -> dict:
    """末条正文是否**显著变长**——「同容器内流式补全」的唯一证据。

    为什么需要：锚点（消息 id / 指纹）判的是「有没有**新的**消息」。
    如果平台是在已有容器里把回复流式填进去（Gemini 一类 SPA 常见），
    集合差会一直是 0，锚点口径会判「没收到回复」——这是锚点口径唯一的盲区。

    判据（两条都要）：
      · 绝对增量 ≥ min_abs（太小的是光标/空白/渲染误差，不是回复）
      · 相对增量 ≥ min_ratio（长回复里 40 字可能只是重排，按比例兜一层）
    """
    b = int((before or {}).get("last_len") or 0)
    a = int((after or {}).get("last_len") or 0)
    delta = a - b
    need = max(int(min_abs), int(b * float(min_ratio)))
    return {"delta": delta, "need": need, "grew": bool(a > 0 and delta >= need)}


def trim(cur: dict, dom_hash: str = "") -> dict:
    """把 probe 输出裁成「发送前后可对比」的快照（含锚点指纹）。

    两层（wa.py / write_gate.py）都用这一份，避免两边字段名各写各的。
    """
    cur = cur or {}
    return {
        "url": cur.get("url"),
        "msg_count": cur.get("msg_count"),
        "reply_count": cur.get("reply_count"),
        "user_msg_count": cur.get("user_msg_count"),
        "editor_len": cur.get("editor_len"),
        "asst_fps": cur.get("asst_fps") or [],
        "user_fps": cur.get("user_fps") or [],
        "msg_fps": cur.get("msg_fps") or [],
        # 平台消息 ID（有则优先，见 platforms.anchor_delta）：身份锚点，比正文指纹硬
        "asst_keys": cur.get("asst_keys") or [],
        "msg_keys": cur.get("msg_keys") or [],
        "last_asst_fp": cur.get("last_asst_fp") or "",
        # 末条正文长度：同一容器内**流式补全**时，锚点集合差为 0，
        # 只有长度增长能证明「回复真的在长」（Gemini 第 5 条裁定）
        "last_len": cur.get("last_len") or 0,
        "last_user_fp": cur.get("last_user_fp") or "",
        "editor_kind": cur.get("editor_kind") or "",
        "dom_hash": dom_hash,
    }


def classify_send_result(before: dict, after: dict, signals=()) -> dict:
    """纯函数：给两份快照，出结论。不碰浏览器、不写盘、不抛异常。

    `before` / `after` 是 `platforms.js_probe` 的输出（至少含 reply_count、
    editor_len、asst_fps、last_asst_fp）。`signals` 是采集到的送达信号列表。
    """
    b, a = (before or {}), (after or {})
    sig = [s for s in (signals or []) if s in PRIMARY_SIGNALS]
    delivered = bool(sig)

    def _i(v):
        try:
            return int(v or 0)
        except Exception:
            return 0

    count_delta = _i(a.get("reply_count")) - _i(b.get("reply_count"))
    anch = platforms.anchor_delta(b, a)
    effective = anch["new_asst"] if anch.get("anchors_available") else count_delta
    editor_cleared = _i(a.get("editor_len")) == 0 and a.get("editor_len") is not None

    out = {
        "delivered": delivered,
        "signals": sig,
        "count_delta": count_delta,
        "effective_delta": effective,
        "delta_source": "anchor" if anch.get("anchors_available") else "count",
        "anchors": anch,
        "editor_cleared": editor_cleared,
        "count_anchor_disagree": bool(anch.get("anchors_available")
                                      and anch["new_asst"] != count_delta),
    }

    if not delivered:
        out["verdict"] = INCONCLUSIVE_NOT_SUCCESS if editor_cleared else NOT_SENT
        out["error_class"] = "send_unconfirmed"
        out["reason"] = ("三选一主信号（消息数增加/地址变了/正文指纹命中）都不满足；"
                         + ("输入框已清空 → 弱信号不足以下结论，转人工复核页面"
                            if editor_cleared else "输入框仍有内容 → 没发出去"))
        return out

    if effective == 1:
        out["verdict"] = SENT
        out["error_class"] = ""
        out["reason"] = "送达，且新增正好 1 条助手回复（口径：%s）" % out["delta_source"]
    elif effective == 0:
        out["verdict"] = SENT_NO_REPLY
        out["error_class"] = ""
        out["reason"] = "送达，但还没有新的助手回复（reply_pending）"
    else:
        out["verdict"] = SENT_MULTI
        out["error_class"] = ""
        out["reason"] = ("送达，但新增 %d 条助手回复：可能是气泡拆分，也可能是重发，"
                         "由 write_gate verify 按 user_msg_delta 细分" % effective)
    return out


def count_delta_of(before: dict, after: dict) -> int:
    """仅计数口径的增量（审计用，不作判据）。"""
    def _i(v):
        try:
            return int(v or 0)
        except Exception:
            return 0
    return _i((after or {}).get("reply_count")) - _i((before or {}).get("reply_count"))
