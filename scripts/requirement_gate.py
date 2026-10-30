#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""requirement_gate.py —— 需求卡片的**否定式**校验器（架构评审 S6）。

存在的理由：SKILL.md §2 的「需求三列表」以前只有方法论、没有机器校验，
「未明确项是否已清空」「冲突是否已裁定」全靠人眼看——而人眼会漏。

**红线（硬规则 2「不预估方向」）**：本工具**只拒绝，不生成、不建议、不补全**。
它不会说「你大概想要…」，也不会替你填任何一格。它只会说：
「这一节还有 N 条没清空，所以不许进 LOOP。」

用法：
    python scripts/requirement_gate.py --card docs/requirements/x.md
    python scripts/requirement_gate.py --card ... --json

退出码：0 = 放行；1 = 拒绝（附具体字段）；2 = 文件读不到/格式不可解析。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

# 允许出现在表格里的占位符（模板原样带来的空值）
PLACEHOLDERS = {"", "-", "—", "（不超过 30 字，说清「谁、在什么场景下、要什么结果」）",
                "待确认 / 已确认 / 部分确认（不算通过）",
                "（transcript 相对路径，如 `docs/requirements/<需求名>-transcript.md`）",
                "（逐步确认 / 直接执行——记用户原话，二选一；「首次人工确认门」的裁定结果，未确认不许进 LOOP）",
                "（整个任务 / 本阶段 / 接下来 N 条；直接执行模式必填，逐条批准就写「逐条」）",
                "（直接执行模式必填：无人看着时遇到岔路怎么选。见下方「无人值守三问」，**答这三问，不要写作文**）"}

SECTION_HEADINGS = {
    "已明确": r"^##+\s*一、已明确",
    "未明确": r"^##+\s*二、未明确",
    "有冲突": r"^##+\s*三、有冲突",
    "不做": r"^##+\s*四、明确不做的事",
    "确认记录": r"^##+\s*五、确认记录",
}


def _cells(line: str) -> list:
    s = line.strip()
    if not s.startswith("|"):
        return []
    parts = [p.strip() for p in s.strip("|").split("|")]
    return parts


def _is_sep(cells: list) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c or "") for c in cells if c != "") \
        and any(c for c in cells)


def _is_real_row(cells: list) -> bool:
    """去掉表头、分隔行、以及「只有编号没有内容」的空行。"""
    if not cells or _is_sep(cells):
        return False
    body = [c for c in cells[1:] if c not in PLACEHOLDERS]
    return any(c.strip() for c in body)


def parse_sections(text: str) -> dict:
    """把 markdown 按小节切开，返回 {节名: [[单元格...], ...]}。

    只收**数据行**：表头（紧跟分隔行的那一行）与分隔行本身都被剔除——
    否则模板自带的表头「| # | 要求（原话） |…」会被当成一条真实需求。
    """
    out = {k: [] for k in SECTION_HEADINGS}
    cur = None
    raw = {k: [] for k in SECTION_HEADINGS}
    for line in text.splitlines():
        hit = None
        for name, pat in SECTION_HEADINGS.items():
            if re.match(pat, line.strip()):
                hit = name
                break
        if hit:
            cur = hit
            continue
        if line.strip().startswith("## "):
            cur = None
            continue
        if cur:
            cells = _cells(line)
            if cells:
                raw[cur].append(cells)
    for name, rows in raw.items():
        for i, cells in enumerate(rows):
            if _is_sep(cells):
                continue
            nxt = rows[i + 1] if i + 1 < len(rows) else None
            if nxt is not None and _is_sep(nxt):
                continue                      # 表头
            if _is_real_row(cells):
                out[name].append(cells)
    return out


def _basic_fields(text: str) -> dict:
    """基本信息表的键值对（项目名 / 一句话目标 / 执行模式）。"""
    out = {}
    for line in text.splitlines():
        cells = _cells(line)
        if len(cells) >= 2 and cells[0] in ("项目名", "提出人", "提出日期", "一句话目标",
                                            "原始往来记录", "执行模式",
                                            "授权范围", "无人值守方案"):
            out[cells[0]] = cells[1]
    return out


# 执行模式是固定二选一词表（SKILL §2.5 首次人工确认门）：
# 只认这两个词根，其余写法一律不认（fail-closed）。
EXEC_MODE_WORDS = ("逐步确认", "直接执行")
# 人话 → 机器码：agent 与编排器只认机器码，避免「大概那个意思」被当成授权。
EXEC_MODE_CANON = {"逐步确认": "step", "直接执行": "auto"}


def _exec_mode_ok(val: str) -> bool:
    return any(w in (val or "") for w in EXEC_MODE_WORDS)


def exec_mode_of(val: str) -> str:
    """卡片上的执行模式原文 → "step" / "auto"；认不出返回 ""（不许猜）。"""
    for word, code in EXEC_MODE_CANON.items():
        if word in (val or ""):
            return code
    return ""


CONFIRM_WORDS = ("已确认", "待确认", "部分确认", "未确认", "已授权", "授权")


def _looks_like_confirmation(row) -> bool:
    """这一行像不像「确认记录」？

    判据只看最后两列是否出现确认状态词表里的词——确认状态是固定词表，
    技术裁定 / 证据链接之类不会命中。**命中不了就当不是**（fail-closed 的反面：
    宁可多报一条 NO_CONFIRMATION，也不把一张裁定表当成人已确认）。
    """
    if not row:
        return False
    return any(w in (cell or "") for cell in row[-2:] for w in CONFIRM_WORDS)


def validate(text: str) -> dict:
    """返回 {"ok": bool, "violations": [{code, section, detail}], "stats": {...}}"""
    secs = parse_sections(text)
    basic = _basic_fields(text)
    v = []

    for key in ("项目名", "一句话目标"):
        val = (basic.get(key) or "").strip()
        if not val or val in PLACEHOLDERS:
            v.append({"code": "BASIC_EMPTY", "section": "基本信息",
                      "detail": "「%s」为空" % key})

    raw_brief = (basic.get("原始往来记录") or "").strip()
    if not raw_brief or raw_brief in PLACEHOLDERS:
        v.append({"code": "RAW_BRIEF_MISSING", "section": "基本信息",
                  "detail": "「原始往来记录」为空：需求分析由网页版 AI 主导，卡片必须是"
                            " agent↔网页版 AI↔用户 往来的**摘录**——先把 transcript 落盘再填卡"
                            "（硬规则 2 · 原始需求零加工转交）"})

    mode = (basic.get("执行模式") or "").strip()
    if not mode or mode in PLACEHOLDERS:
        v.append({"code": "EXEC_MODE_MISSING", "section": "基本信息",
                  "detail": "「执行模式」为空：首次人工确认门没走完——必须问清后续是"
                            "「逐步确认」（每轮网页 AI 出方案后等人工拍板）还是「直接执行」"
                            "（按既定规划连续执行到交付），并记用户原话（SKILL §2.5）"})
    elif not _exec_mode_ok(mode):
        v.append({"code": "EXEC_MODE_INVALID", "section": "基本信息",
                  "detail": "「执行模式」=%r 不在固定二选一（逐步确认 / 直接执行）词表里："
                            "不认其他写法" % mode})

    # 直接执行 = 人不在场。这时候「遇到岔路怎么选」和「授权覆盖到哪」必须**事先**写死，
    # 否则 agent 只能要么自己乱决定、要么中途停下来问——两种都是用户不要的。
    scope_val = (basic.get("授权范围") or "").strip()
    autonomy = (basic.get("无人值守方案") or "").strip()
    if exec_mode_of(mode) == "auto":
        if not scope_val or scope_val in PLACEHOLDERS:
            v.append({"code": "SCOPE_MISSING", "section": "基本信息",
                      "detail": "直接执行模式下「授权范围」为空：一次授权要覆盖到哪"
                                "（整个任务 / 本阶段 / 接下来 N 条）必须写明，否则等于无限放权"})
        if not autonomy or autonomy in PLACEHOLDERS:
            v.append({"code": "AUTONOMY_MISSING", "section": "基本信息",
                      "detail": "直接执行模式下「无人值守方案」为空：无人看着时遇到两条路都能走"
                                "该怎么选、要不要装东西、外部服务挂了怎么办——"
                                "先答 templates/confirm-gate.md 的「无人值守三问」再填这一格"})

    if not secs["已明确"]:
        v.append({"code": "CLARIFIED_EMPTY", "section": "一、已明确",
                  "detail": "没有任何一条「已明确」要求：这张卡片还没开始填"})

    # 核心：未明确必须清空
    for row in secs["未明确"]:
        v.append({"code": "UNRESOLVED_PENDING", "section": "二、未明确",
                  "detail": "未清空：%s" % "｜".join(row[:2])})

    # 冲突必须裁定（「谁优先」列非空）
    for row in secs["有冲突"]:
        winner = row[3] if len(row) > 3 else ""
        if not winner.strip():
            v.append({"code": "CONFLICT_UNDECIDED", "section": "三、有冲突",
                      "detail": "冲突未裁定（谁优先为空）：%s" % "｜".join(row[:2])})

    # 确认记录：最后一条必须是「已确认」，沉默/部分确认不算通过
    raw = secs["确认记录"]
    # 「确认状态」是固定词表。该节里出现的**别的表**（比如技术裁定表）
    # 不该被当成确认记录，但也不能静默丢掉——挑出来报 stray_rows。
    # 注意方向：词表之外的**不认**（fail-closed），不是「认下来当通过」。
    rows = [r for r in raw if _looks_like_confirmation(r)]
    stray = [r for r in raw if not _looks_like_confirmation(r)]
    if not rows:
        v.append({"code": "NO_CONFIRMATION", "section": "五、确认记录",
                  "detail": "没有确认记录：用户没说「就这样/开始/没问题」就不算通过"
                            + ("（该节有 %d 行不像确认记录，已忽略）" % len(stray)
                               if stray else "")})
    else:
        last = rows[-1]
        status = last[-1] if last else ""
        if "已确认" not in status or "待确认" in status or "部分" in status:
            v.append({"code": "NOT_CONFIRMED", "section": "五、确认记录",
                      "detail": "最新一轮确认状态是「%s」：不算通过" % status})

    return {
        "ok": not v,
        "violations": v,
        "stats": {
            "raw_brief": 1 if raw_brief and raw_brief not in PLACEHOLDERS else 0,
            "exec_mode": 1 if mode and mode not in PLACEHOLDERS and _exec_mode_ok(mode) else 0,
            # 机器码版本：编排器读这个（step=逐步确认 / auto=无人值守），
            # exec_mode 的 0/1 是给「卡片是否合格」用的，别混用。
            "exec_mode_code": exec_mode_of(mode),
            "clarified": len(secs["已明确"]),
            "pending": len(secs["未明确"]),
            "conflicts": len(secs["有冲突"]),
            "not_doing": len(secs["不做"]),
            "confirmations": len(rows),
            "stray_rows": len(stray),
        },
        "note": "否定式校验：只拒绝，不生成、不建议、不补全（硬规则 2）。",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="需求卡片否定式校验器")
    ap.add_argument("--card", required=True, help="需求卡片 md 路径")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    a = ap.parse_args()
    if not os.path.exists(a.card):
        print(json.dumps({"ok": False, "error": "文件不存在：%s" % a.card},
                         ensure_ascii=False, indent=2))
        return 2
    with open(a.card, encoding="utf-8") as f:
        text = f.read()
    res = validate(text)
    res["card"] = os.path.abspath(a.card)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        if res["ok"]:
            print("PASS  需求卡片可进规划（未明确=0，冲突已裁定，确认已通过）")
        else:
            print("FAIL  需求卡片不可进规划：")
            for x in res["violations"]:
                print("  [%s] %s — %s" % (x["code"], x["section"], x["detail"]))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
