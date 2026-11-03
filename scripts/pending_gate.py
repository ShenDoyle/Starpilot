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
    python scripts/pending_gate.py settle     <pending.md> --file <feedback.md> \
                                        --round 第3轮
    （settle 只核销「本轮回启确实报备过」的违例/豁免条目，回执没引用的一律不核销；
      核销后条目仍在台账上，只是不再逐轮重复占用回执。）
退出码：0 通过；1 不通过（stdout JSON 带缺失清单）。
"""

import json
import os
import re
import sys
import time

import safeprint

# 门禁的拒绝理由全是中文，且是 agent 最需要看清的那句话。
# cp936 控制台上打印时崩掉 = 门禁把「为什么拦你」弄丢了。
safeprint.harden_streams()

ITEM_RE = re.compile(
    r"^-\s*\[(?P<pri>P[012])\]\s*\[(?P<id>[A-Za-z0-9-]+)\]\s*"
    r"(?P<text>.+?)\s*\|\s*(?P<status>\w+)[（(]?(?P<detail>[^)）]*)[)）]?\s*(?:\|\s*(?P<src>.*))?$"
)

# 回执里允许的状态标记：诚实报 ❌/未做 合格，隐身不合格
STATUS_MARKS = ("✅", "⚠️", "❌", "未做", "处理中", "done", "deferred", "settled")

# 一次性核销（2026-11-03 加）：违例条目**报够一次就该结账**，
# 否则台账变成永久税负：32 条 P0-ESC 每一条都要在之后**每一轮**回执里出现，
# 回执越长越容易漏项，漏项又被判回执不合格，agent 只能再走逃生门——循环自激。
# 核销不等于隐身：条目留在台账上（status=settled(第N轮已报备)），
# 计数与时间戳都在 detail 里，随时可查；只是不再逐轮重复占用回执与上下文。
REPORTED_STATUSES = ("open", "deferred", "blocked")
SETTLED_STATUS = "settled"

# 聚合条目的 detail 字段格式（只认自己写的这套，不做宽松猜测）：
#   违例 N 次；最近:TS；理由：r1 ‖ r2
COUNT_RE = re.compile(r"违例\s*(\d+)\s*次")
REASON_HEAD = "理由："
REASON_SEP = " ‖ "
# ITEM_RE 的 detail 段用 `[^)）]*` 定界，所以理由原文里的括号必须换掉：
# 留着会把 detail 截断 → 下一轮读不到旧计数 → 违例次数被越记越少。
_PAREN_SWAP = str.maketrans({"(": "（", ")": "）", "（": "（", "）": "）"})


def _safe_reason(text: str) -> str:
    return (text or "").replace("\n", " ").replace("|", "｜").strip()[:160] \
        .translate(_PAREN_SWAP)


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

    追加（2026-11-03）：`settled(...)` 是唯一例外。已核销的条目不再占用每一轮
    回执，但它**仍在台账上、仍带计数与时间戳**——核销的是重复报备的义务，
    不是记录本身。不这么改的话，违例越多回执越长，回执越长越容易不合格，
    不合格就更多违例：门会变成自激振荡器。
    """
    items = parse_pending(pending_path)
    if not items:
        return True, "ok", []
    missing = [i["id"] for i in open_items(items)
               if not referenced_with_status(feedback_text, i)]
    must_report = [i["id"] for i in items
                   if (i["id"].startswith("P0-ESC") or i["status"] == "blocked")
                   and i["status"] != SETTLED_STATUS
                   and not referenced_with_status(feedback_text, i)]
    missing = missing + must_report
    if missing:
        return False, ("回执缺项（没做的项必须显式出现；逃生门违例 P0-ESC 与 "
                       "BLOCKED 豁免不许隐身；已核销项用 settle 结账后不再重复报备）"), missing
    return True, "ok", []


def settle_candidates(pending_path: str, feedback_text: str) -> list:
    """本轮回执**已经**报备过的违例/豁免条目 = 可以核销的候选。"""
    items = parse_pending(pending_path)
    return [i["id"] for i in items
            if (i["id"].startswith("P0-ESC") or i["status"] == "blocked")
            and i["status"] != SETTLED_STATUS
            and referenced_with_status(feedback_text, i)]


def settle(pending_path: str, feedback_text: str, round_tag: str) -> dict:
    """把候选条目改写成 `settled(<round_tag>)`，其余行原样保留。

    先校验再改写：回执没引用的条目**不许**被核销（否则这条命令就成了
    「让违例消失」的后门，正是裁定要堵的东西）。
    """
    ids = set(settle_candidates(pending_path, feedback_text))
    if not ids:
        return {"ok": True, "settled": [], "skipped": [],
                "reason": "no_candidates"}
    with open(pending_path, encoding="utf-8") as f:
        lines = f.readlines()
    out, done = [], set()
    for ln in lines:
        m = ITEM_RE.match(ln.strip())
        if m and m.group("id") in ids:
            detail = (m.group("detail") or "").strip()
            tail = " | %s" % m.group("src") if m.group("src") else ""
            mark = "settled(%s%s)" % (round_tag, ("；" + detail) if detail else "")
            out.append("- [%s][%s] %s | %s%s\n" % (
                m.group("pri"), m.group("id"), m.group("text"), mark, tail))
            done.add(m.group("id"))
        else:
            out.append(ln)
    with open(pending_path, "w", encoding="utf-8", newline="") as f:
        f.writelines(out)
    return {"ok": True, "settled": sorted(done),
            "skipped": sorted(ids - done),
            "reason": ""}


def record_escape(pending_path: str, target_sha: str, reason: str,
                  max_reasons: int = 5) -> dict:
    """逃生门落账（聚合式）：同一目标的违例**合成一条**，带次数与最近理由。

    为什么不再一条一发一个 id（2026-11-03）：一发一条会让台账按发送次数增长，
    而 check_receipt 要求每条都在回执里出现——24 小时内 32 条，回执正文比
    交付物还长，弱一点的 agent 直接被判不合格。违例必须留痕，但**留痕不该
    靠复读**：条目稳定、计数精确、理由留最近几条，审计信息一条不少。
    """
    eid = "P0-ESC-%s" % target_sha
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    os.makedirs(os.path.dirname(pending_path) or ".", exist_ok=True)
    items = parse_pending(pending_path) if os.path.exists(pending_path) else []
    hit = next((i for i in items if i["id"] == eid), None)
    n, reasons = 1, []
    if hit:
        prev = hit["detail"] or ""
        # 只认自己写出来的格式（「违例 N 次」），别拿宽松的 ×N 猜：
        # 上一版就是在这里读不到旧计数，每次都退回 2，把 32 次违例压成「2 次」，
        # 聚合反而把事实说小了——留痕机制最不能接受的就是少记。
        m = COUNT_RE.search(prev)
        n = (int(m.group(1)) if m else 1) + 1
        seg = prev.split(REASON_HEAD, 1)
        reasons = [x.strip() for x in seg[1].split(REASON_SEP)
                   if x.strip()] if len(seg) > 1 else []
    reasons = (reasons + [_safe_reason(reason)])[-max_reasons:]
    detail = "违例 %d 次；最近:%s；%s%s" % (
        n, ts, REASON_HEAD, REASON_SEP.join(reasons))
    line = ("- [P0][%s] direct_send 绕过编排器直发 | deferred(%s) "
            "| 来源:escape_hatch\n" % (eid, detail))
    if hit:
        with open(pending_path, encoding="utf-8") as f:
            lines = f.readlines()
        out = [line if (mm := ITEM_RE.match(l.strip())) and mm.group("id") == eid
               else l for l in lines]
        with open(pending_path, "w", encoding="utf-8", newline="") as f:
            f.writelines(out)
    else:
        head = "" if os.path.exists(pending_path) else "# 未清项清单\n"
        with open(pending_path, "a", encoding="utf-8", newline="") as f:
            if head:
                f.write(head)
            f.write(line)
    return {"id": eid, "count": n, "ledger": pending_path}


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
    if len(argv) < 2 or argv[0] not in ("check-pending", "check-receipt", "settle"):
        print(__doc__)
        return 2
    cmd, pending = argv[0], argv[1]
    prompt_file = ""
    for i, a in enumerate(argv):
        if a in ("--prompt", "--file") and i + 1 < len(argv):
            prompt_file = argv[i + 1]
    prompt_text = _read(prompt_file)
    if cmd == "check-pending":
        ok, reason = check_p0_gate(pending, prompt_text)
        out = {"ok": ok, "reason": reason, "gate": "p0",
               "open_items": [i["id"] for i in open_items(parse_pending(pending))]}
    elif cmd == "settle":
        round_tag = ""
        for i, a in enumerate(argv):
            if a == "--round" and i + 1 < len(argv):
                round_tag = argv[i + 1]
        if not prompt_text:
            # 没有回执原文就不许核销：否则 settle 会变成「让违例消失」的后门。
            out = {"ok": False, "gate": "settle", "settled": [],
                   "reason": "SETTLE_NEEDS_RECEIPT：settle 必须带 --file <本轮回执>，"
                             "只核销回执里确实报备过的条目"}
            print(json.dumps(out, ensure_ascii=False))
            return 1
        res = settle(pending, prompt_text, round_tag or "本轮")
        out = {"ok": True, "gate": "settle", **res}
    else:
        ok, reason, missing = check_receipt(pending, prompt_text)
        settled = [i["id"] for i in parse_pending(pending)
                   if i["status"] == SETTLED_STATUS]
        out = {"ok": ok, "reason": reason, "gate": "receipt", "missing": missing,
               # 已核销数一并报出来：让「税已经交过」这件事在机器输出里看得见
               "settled_not_reported": len(settled)}
    safeprint.jprint(out)
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    # 注意是 sys.argv[1:]：main() 的第 0 位约定是**子命令名**。
    # 以前这里传整个 sys.argv，`python scripts/pending_gate.py check-pending x.md`
    # 永远走 usage 分支返回 2 —— 而 2 在本模块契约里是「不通过」，
    # agent 读到的是「门禁拦住了我」，真相是「命令行根本没能启动」。
    # （2026-11-03 实测：本文件的 CLI 一直不可用，只有单测按 argv[1:] 直接调 main 才通。）
    sys.exit(main(sys.argv[1:]))
