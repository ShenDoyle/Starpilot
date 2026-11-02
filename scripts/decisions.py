#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""裁定台账：把「WebAI 的回复」变成「可校验的授权」（2026-11-02 GPT 审计 P0-2）。

GPT 裁定要点：
  · **倾向 ≠ 批准**：「我倾向于方案 A」不能转成执行授权；
  · 裁定必须绑定**具体动作 + 适用范围**，并记下裁定时的**事实指纹**；
  · 裁定后现场事实变化 → 原裁定自动失效，必须重新申请；
  · 未取得明确授权前，不得执行待裁定动作。

记录格式（`state/decisions.json`，append-only 列表）：

    {"id": "D-xxxx", "action": "合并阶段 S2+S3", "scope": "本项目本轮",
     "verdict": "approved|rejected|need_info|stopped|tentative",
     "raw": "<WebAI 原话摘要>", "facts_hash": "<裁定时的现场指纹>",
     "by": "web_ai", "ts": "..."}

用法（agent 在动手前自查）：

    python scripts/decisions.py record --ledger X --action "..." --verdict-text "<原话>" \
        --facts-hash abc123
    python scripts/decisions.py check --ledger X --action "..." --facts-hash abc123
      → 0 已授权 / 6 未授权（含 tentative / 已失效 / 无记录）

事实指纹由调用方计算（例如关键文件哈希、页面消息数、配置快照的 sha16）。
本模块只存与比，不猜「事实」是什么。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import time

VERDICTS = ("approved", "rejected", "need_info", "stopped", "tentative")

# 顺序有意义：先判否决/停止/需补充，再判批准，最后才是「倾向」。
# 「我倾向于批准 A」同时含「倾向」和「批准」——必须落到 tentative，
# 否则软话被当成硬授权（这正是 GPT 点名的绕过路径）。
_PATTERNS = (
    ("stopped", r"停止执行|中止|先停|停一下|不要继续|stop"),
    ("rejected", r"否决|不批准|不同意|不可以|不要这么做|禁止|拒绝|驳回|不可行"),
    ("need_info", r"补充信息|信息不足|需要更多|再确认|说清楚|先补充|不确定.*补充"),
    ("tentative", r"倾向|倾向于|建议|推荐|我觉得|感觉|可以考虑|或许|大概|看起来|应该可以|probably|I('| a)m leaning"),
    ("approved", r"批准|同意|按\s*[AB]|采用\s*[AB]|可以按|就这么做|通过|approved|go ahead"),
)


def classify_verdict(text: str) -> tuple:
    """把 WebAI 原话分到五态之一。返回 (verdict, 命中的片段)。

    规则：**任何含「倾向/建议/或许」的表述先归 tentative**——软话不构成授权，
    哪怕句子里同时出现「批准」二字（「我倾向于批准」仍是倾向）。
    """
    t = (text or "").strip()
    if not t:
        return "need_info", ""
    for name, pat in _PATTERNS:
        m = re.search(pat, t, re.I)
        if m:
            return name, m.group(0)
    return "tentative", ""  # 认不出的表述一律不当授权（fail-closed）


def ledger_path(root: str) -> str:
    return os.path.join(root, "state", "decisions.json")


def _load(path: str) -> list:
    if not os.path.exists(path):
        return []
    try:
        with io.open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(path: str, items: list) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def record(path: str, action: str, verdict_text: str, facts_hash: str,
           scope: str = "", by: str = "web_ai") -> dict:
    verdict, hit = classify_verdict(verdict_text)
    rid = "D-%s" % hashlib.sha256(
        ("%s|%s|%s" % (action, verdict_text, time.time())).encode("utf-8")
    ).hexdigest()[:8]
    rec = {"id": rid, "action": (action or "").strip(), "scope": (scope or "").strip(),
           "verdict": verdict, "matched": hit, "raw": (verdict_text or "").strip()[:300],
           "facts_hash": (facts_hash or "").strip(), "by": by,
           "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    items = _load(path)
    items.append(rec)
    _save(path, items)
    return rec


def check(path: str, action: str, facts_hash: str) -> tuple:
    """查该动作是否有**仍然有效**的明确批准。返回 (ok, 说明, 记录)。

    有效 = verdict==approved + 动作匹配 + 事实指纹一致（空指纹视为「未绑定事实」，
    也按未授权处理——没有事实绑定就无从判断裁定是否已过期）。
    """
    items = _load(path)
    act = (action or "").strip()
    if not act:
        return False, "未给出动作，无法校验授权", None
    hits = [r for r in items if r.get("action") == act]
    if not hits:
        return False, "无该动作的裁定记录：先申请裁定（templates/web-ai-decision.md）", None
    latest = hits[-1]
    v = latest.get("verdict")
    if v != "approved":
        return False, ("最近一次裁定是 %s（不是明确批准）：tentative/rejected/"
                       "need_info/stopped 都不构成执行授权" % v), latest
    if not latest.get("facts_hash"):
        return False, "该裁定未绑定事实指纹：无从判断裁定后事实是否变化，按未授权处理", latest
    if not facts_hash:
        return False, "未提供事实指纹：无法判断裁定后事实是否变化", latest
    if (latest.get("facts_hash") or "") != facts_hash:
        return False, ("裁定后现场事实已变化（裁定指纹 %s ≠ 当前 %s）：原裁定失效，"
                       "必须重新申请" % (latest.get("facts_hash"), facts_hash)), latest
    return True, "已授权（%s，%s）" % (latest.get("id"), latest.get("ts")), latest


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="decisions.py",
                                 description="裁定台账：倾向≠批准；事实变了裁定失效")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="登记一条 WebAI 裁定")
    r.add_argument("--ledger", required=True)
    r.add_argument("--action", required=True)
    r.add_argument("--verdict-text", required=True, help="WebAI 原话（不改写）")
    r.add_argument("--facts-hash", default="")
    r.add_argument("--scope", default="")
    r.add_argument("--by", default="web_ai")

    c = sub.add_parser("check", help="检查动作是否已获有效授权")
    c.add_argument("--ledger", required=True)
    c.add_argument("--action", required=True)
    c.add_argument("--facts-hash", default="")

    l = sub.add_parser("list", help="列出台账")
    l.add_argument("--ledger", required=True)

    a = ap.parse_args(argv)
    if a.cmd == "record":
        rec = record(a.ledger, a.action, a.verdict_text, a.facts_hash,
                     scope=a.scope, by=a.by)
        print(json.dumps(rec, ensure_ascii=False, indent=2))
        return 0 if rec["verdict"] == "approved" else 6
    if a.cmd == "list":
        print(json.dumps(_load(a.ledger), ensure_ascii=False, indent=2))
        return 0
    ok, why, rec = check(a.ledger, a.action, a.facts_hash)
    print(json.dumps({"ok": ok, "reason": why, "record": rec},
                     ensure_ascii=False, indent=2))
    return 0 if ok else 6


if __name__ == "__main__":
    sys.exit(_main())
