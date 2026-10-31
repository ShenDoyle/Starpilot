# -*- coding: utf-8 -*-
"""pending_gate：未清项清单 + 回执逐项对账的机器校验。

## 背景（2026-10-31，Benchyard 星舵循环实证）

DeepSeek 裁决连续两轮点名 agent「最高优先级阻断项完全没碰」「已发≠已达标」。
根因不是抗命，是结构性的：

1. 每轮都是新 agent 实例，上一轮裁决的未清项不会自动进入下一轮上下文——
   「欠账」从记忆里蒸发（lessons L-PENDING-EVAPORATION）。
2. 反馈按 agent 自己的叙事组织，**没做的项在汇报里根本不出现**，
   架构师只能靠自己记忆反查。

本模块把两道口子变成断言（只校验，不生成——硬规则 2）：

- **未清项清单** `stage/<id>/pending.md`：agent 每轮从架构师裁决提取，
  格式一行一项：`- [P0][P0-1] 项目描述 | open | 来源:第N轮裁决`
  状态：open / done(第N轮) / deferred(理由必填)。
- **P0 门**：开工前检查——存在 open 的 P0 项且本轮 prompt（反馈正文）
  没有引用它并给出状态 → 拒绝进入本轮（P0 不清不得先做别的）。
- **回执完整性门**：发送前检查——每条 open 项都必须在本轮 prompt 里被引用
  且带显式状态标记（✅ / ⚠️ / ❌ / 未做 / 处理中）。**只要求「出现」，
  不要求「成功」**——如实报 ❌ 是合格的，隐身不报才不合格。

CLI：
    python scripts/pending_gate.py check-pending <pending.md> [--prompt <file>]
    python scripts/pending_gate.py check-receipt <pending.md> --file <feedback.md>
退出码：0 通过；1 不通过（stdout JSON 带缺失清单）。
"""

import json
import os
import re
import sys

ITEM_RE = re.compile(
    r"^-\s*\[(?P<pri>P[012])\]\s*\[(?P<id>[A-Za-z0-9-]+)\]\s*"
    r"(?P<text>.+?)\s*\|\s*(?P<status>\w+)[（(]?(?P<detail>[^)）]*)[)）]?\s*(?:\|\s*(?P<src>.*))?$"
)

# 回执里允许的状态标记：诚实报 ❌/未做 合格，隐身不合格
STATUS_MARKS = ("✅", "⚠️", "❌", "未做", "处理中", "done", "deferred")


def parse_pending(path: str) -> list:
    """解析 pending.md，返回 [{id, priority, text, status, detail, source}]。"""
    if not os.path.exists(path):
        return []
    items = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = ITEM_RE.match(line.strip())
            if not m:
                continue
            items.append({
                "id": m.group("id"),
                "priority": m.group("pri"),
                "text": m.group("text").strip(),
                "status": m.group("status").lower(),
                "detail": (m.group("detail") or "").strip(),
                "source": (m.group("src") or "").strip(),
            })
    return items


def open_items(items: list) -> list:
    return [i for i in items if i["status"] == "open"]


def blocked_items(items: list) -> list:
    """BLOCKED(reason) 豁免项（0.1.5，DeepSeek 裁定）：门禁放行但**不许隐身**——
    必须在回执三栏「违例/异常」栏出现，否则豁免即隐身。"""
    return [i for i in items if i["status"] == "blocked"]


def referenced_with_status(prompt_text: str, item: dict) -> bool:
    """prompt 里是否引用了该条 id 且同行（或附近 3 行内）带状态标记。"""
    if not prompt_text:
        return False
    for ln in prompt_text.splitlines():
        if item["id"] in ln and any(k in ln for k in STATUS_MARKS):
            return True
    lines = prompt_text.splitlines()
    for idx, ln in enumerate(lines):
        if item["id"] in ln:
            window = "\n".join(lines[max(0, idx - 1): idx + 3])
            if any(k in window for k in STATUS_MARKS):
                return True
    return False


def check_p0_gate(pending_path: str, prompt_text: str) -> tuple:
    """P0 门：open 的 P0 项必须在本轮 prompt 里被引用并给出状态。

    返回 (ok, reason)。pending.md 不存在 = 门未启用，放行（不强迫所有项目建清单，
    但一旦建了就必须遵守）。
    """
    items = parse_pending(pending_path)
    if not items:
        return True, "no_pending_list"
    p0 = [i for i in open_items(items) if i["priority"] == "P0"]
    missing = [i["id"] for i in p0 if not referenced_with_status(prompt_text, i)]
    if missing:
        return False, "P0 未清且本轮未处理：%s（P0 不清不得先做别的；确要顺延用 deferred(理由) 落清单）" % ", ".join(missing)
    return True, "ok"


def check_receipt(pending_path: str, feedback_text: str) -> tuple:
    """回执完整性门：每条 open 项必须在本轮 prompt 里被引用且带显式状态。

    追加（2026-10-31 DeepSeek 裁定）：P0-ESC-*（逃生门落账的违例条目）与
    BLOCKED 豁免项**无论状态如何**都必须在回执中出现并带状态标记——
    违例与豁免都不允许隐身（隐身即隐身不合格）。
    """
    items = parse_pending(pending_path)
    if not items:
        return True, "ok", []
    missing = [i["id"] for i in open_items(items)
               if not referenced_with_status(feedback_text, i)]
    must_report = [i["id"] for i in items
                   if (i["id"].startswith("P0-ESC") or i["status"] == "blocked")
                   and not referenced_with_status(feedback_text, i)]
    missing = missing + must_report
    if missing:
        return False, ("回执缺项（没做的项必须显式出现；逃生门违例 P0-ESC 与 "
                       "BLOCKED 豁免不许隐身）"), missing
    return True, "ok", []


def pending_path_for(root: str, stage_id: str = "", run_id: str = "") -> str:
    """pending.md 的查找顺序：stage/<id>/pending.md → runs/<id>/pending.md → 项目根。"""
    for cand in (
        os.path.join(root, "stage", stage_id or "_", "pending.md") if stage_id else "",
        os.path.join(root, "runs", run_id or "_", "pending.md") if run_id else "",
        os.path.join(root, "pending.md"),
    ):
        if cand and os.path.exists(cand):
            return cand
    # 都不存在时返回约定位置（有 stage 用 stage，否则用项目根），供写入
    if stage_id:
        return os.path.join(root, "stage", stage_id, "pending.md")
    return os.path.join(root, "pending.md")


def _read(path: str) -> str:
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return ""


def main(argv: list) -> int:
    if len(argv) < 2 or argv[0] not in ("check-pending", "check-receipt"):
        print(__doc__)
        return 2
    cmd, pending = argv[0], argv[1]
    prompt_file = ""
    if cmd == "check-pending":
        for i, a in enumerate(argv):
            if a == "--prompt" and i + 1 < len(argv):
                prompt_file = argv[i + 1]
    else:
        for i, a in enumerate(argv):
            if a == "--file" and i + 1 < len(argv):
                prompt_file = argv[i + 1]
    prompt_text = _read(prompt_file)
    if cmd == "check-pending":
        ok, reason = check_p0_gate(pending, prompt_text)
        out = {"ok": ok, "reason": reason, "gate": "p0",
               "open_items": [i["id"] for i in open_items(parse_pending(pending))]}
    else:
        ok, reason, missing = check_receipt(pending, prompt_text)
        out = {"ok": ok, "reason": reason, "gate": "receipt", "missing": missing}
    print(json.dumps(out, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
