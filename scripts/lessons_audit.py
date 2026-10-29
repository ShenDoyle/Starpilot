#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lessons_audit.py —— 知识库体检（架构评审：lessons 会腐化成散文集）。

问题：`references/lessons.md` 只靠 `close --require-lessons` 追加，没人管它会变成
「一本没人读的散文集」——同一条坑换句话写三遍、失效的坑没人删、
`status=open` 挂半年。所以给它一套**可机器判读的协议**：

1. **格式**：附录条目是一行定长字段
   `[lesson_id][date][stage][run][platform][symptom][dom_signal][fix][verify_cmd]
    [trigger][rollback][blocking][status]`
   （老条目 10 字段 / 中间版本 12 字段 / 现在 13 字段，本工具三种都兼容）
2. **去重**：`lesson_id` 相同 → 只更新日期与状态；`(platform, symptom 前 40 字)` 相同
   → 视为重复，报 `duplicates`。
3. **失效标记**：`status` 取 `open | merged | retired`。`open` 只能因为「缺证据」或
   「缺复现」而存在；超过 `--max-age-days`（默认 14）未动静 → 报 `stale`（WARN，
   `--strict` 下失败）。长期 open 不是待办，是知识回流腐化的病灶。
   `open` 条目必须带 `blocking: true|false`：`true` 表示这条不闭合就不许阶段收口，
   `audit` 直接判 FAIL，`orchestrator close` 拒绝 CLOSE。
4. **平台事实对齐**：条目里的平台如果不在 `platforms.PLATFORMS` 里（拼错）→ `unknown_platform`。

用法：
    python scripts/lessons_audit.py                    # 体检
    python scripts/lessons_audit.py --strict           # 有问题就非 0 退出（收口用）
    python scripts/lessons_audit.py --json
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import platforms  # noqa: E402

ENTRY_RE = re.compile(r"^\s*-\s*(\[L-[^\]]*\].*)$")
FIELD_RE = re.compile(r"\[([^\]]*)\]")
VALID_STATUS = ("open", "merged", "retired")
# 平台无关的 lesson（口径 / 闸门 / 文档类）用中性标记，不算「平台名写错」
NEUTRAL_PLATFORMS = ("", "-", "—", "na", "any", "all", "*")


def parse_line(line: str):
    m = ENTRY_RE.match(line)
    if not m:
        return None
    fields = FIELD_RE.findall(m.group(1))
    if len(fields) < 10:
        return {"malformed": True, "raw": line.strip()[:120], "fields": fields}
    # 三种格式都见过：10 段（老，status 在最后）/ 12 段（+trigger/rollback）/ 13 段（+blocking）
    blocking = ""
    if len(fields) >= 13:
        status, blocking = fields[12], fields[11]
        trigger, rollback = fields[9], fields[10]
    elif len(fields) == 12:
        status = fields[11]
        trigger, rollback = fields[9], fields[10]
    else:
        status = fields[9]
        trigger, rollback = "", ""
    return {
        "malformed": False,
        "lesson_id": fields[0],
        "date": fields[1],
        "stage": fields[2],
        "run": fields[3],
        "platform": fields[4],
        "symptom": fields[5],
        "dom_signal": fields[6],
        "fix": fields[7],
        "verify_cmd": fields[8],
        "trigger": trigger,
        "rollback": rollback,
        "blocking": blocking,
        "status": status if status in VALID_STATUS else "unknown:%s" % status,
        "raw": line.strip()[:160],
    }


def _norm(s: str) -> str:
    return re.sub(r"[\s，,。.;；:：\"'`（）()]+", "", (s or ""))[:40]


def audit(path: str, max_age_days: int = 14, today: datetime.date = None) -> dict:
    today = today or datetime.date.today()
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()

    entries, malformed = [], []
    for ln in lines:
        if not ln.strip().startswith("- [L-"):
            continue
        rec = parse_line(ln)
        if rec is None:
            continue
        if rec.get("malformed"):
            malformed.append(rec["raw"])
            continue
        entries.append(rec)

    by_status, by_platform = {}, {}
    stale, unknown_platform, unknown_status = [], [], []
    blocking_open, opens = [], []
    for e in entries:
        by_status[e["status"]] = by_status.get(e["status"], 0) + 1
        by_platform[e["platform"]] = by_platform.get(e["platform"], 0) + 1
        plat = (e["platform"] or "").strip()
        if plat.lower() not in NEUTRAL_PLATFORMS and plat.lower() not in platforms.PLATFORMS:
            unknown_platform.append(e["lesson_id"])
        if e["status"].startswith("unknown"):
            unknown_status.append(e["lesson_id"])
        if e["status"] == "open":
            blocking = (e.get("blocking") or "").strip().lower() in ("true", "yes", "1")
            try:
                d = datetime.date.fromisoformat(e["date"])
            except Exception:
                stale.append({"lesson_id": e["lesson_id"], "reason": "date 不可解析：%s" % e["date"]})
                if blocking:
                    blocking_open.append(e["lesson_id"])
                continue
            age = (today - d).days
            opens.append({"lesson_id": e["lesson_id"], "open_since": e["date"],
                          "age_days": age, "blocking": blocking,
                          "has_criteria": bool(e.get("fix") and e.get("verify_cmd"))})
            if blocking:
                blocking_open.append(e["lesson_id"])
            if age > max_age_days:
                stale.append({"lesson_id": e["lesson_id"], "age_days": age,
                              "blocking": blocking,
                              "reason": "open 超过 %d 天未动静：补完或显式 retired" % max_age_days})

    # 去重：同平台 + 同症状前缀
    seen, dups = {}, []
    for e in entries:
        k = (e["platform"], _norm(e["symptom"]))
        if k in seen:
            dups.append({"platform": e["platform"], "lesson_id": e["lesson_id"],
                         "duplicate_of": seen[k]})
        else:
            seen[k] = e["lesson_id"]

    # blocking 的 open = 纪律漏洞没闭合：直接 FAIL，不给「先收口回头再说」留口子
    ok = not (malformed or dups or unknown_platform or unknown_status or blocking_open)
    return {
        "ok": ok,
        "path": os.path.abspath(path),
        "total": len(entries),
        "by_status": by_status,
        "by_platform": by_platform,
        "malformed": malformed,
        "duplicates": dups,
        "stale_open": stale,
        "open_items": opens,
        "blocking_open": blocking_open,
        "unknown_platform": unknown_platform,
        "unknown_status": unknown_status,
        "max_age_days": max_age_days,
        "verdict": ("FAIL" if not ok else ("WARN" if stale else "PASS")),
        "rule": "同 lesson_id 只更新不追加；open 只能因缺证据/缺复现存在且必须标 blocking；"
                "超过 %d 天未动静要补完或显式 retired；平台名必须在 platforms.py 里" % max_age_days,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="lessons.md 体检")
    ap.add_argument("--file", default=os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "references", "lessons.md"))
    ap.add_argument("--max-age-days", type=int, default=14,
                    help="open 条目超过这个天数未动静就报警（默认 14）")
    ap.add_argument("--strict", action="store_true",
                    help="malformed/duplicates/unknown/blocking_open/超龄 open 任一存在即非 0")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if not os.path.exists(a.file):
        print(json.dumps({"ok": False, "error": "文件不存在：%s" % a.file},
                         ensure_ascii=False, indent=2))
        return 2
    res = audit(a.file, a.max_age_days)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print("lessons: %d 条  状态=%s" % (res["total"], res["by_status"]))
        print("平台分布: %s" % res["by_platform"])
        if res["malformed"]:
            print("!! 格式不合规 %d 条" % len(res["malformed"]))
        if res["duplicates"]:
            print("!! 疑似重复 %d 条：%s" % (len(res["duplicates"]), res["duplicates"][:3]))
        if res["unknown_platform"]:
            print("!! 平台名不在 platforms.py：%s" % res["unknown_platform"])
        if res["open_items"]:
            for it in res["open_items"]:
                print(".. open %s：于 %s 打开（%d 天）blocking=%s 判据=%s"
                      % (it["lesson_id"], it["open_since"], it["age_days"],
                         "true" if it["blocking"] else "false",
                         "有" if it["has_criteria"] else "缺"))
        if res["blocking_open"]:
            print("!! blocking 的 open 未闭合（阻塞收口）：%s" % res["blocking_open"])
        if res["stale_open"]:
            print("!! 超期 open %d 条：%s" % (len(res["stale_open"]), res["stale_open"][:3]))
        print("verdict: %s" % res["verdict"])
    if a.strict and (not res["ok"] or res["stale_open"]):
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main())
