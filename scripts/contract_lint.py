#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""契约自洽检查（SKILL §5.5 ⑤）。

存在的理由：执行颗粒度靠文档约束，文档与代码一旦各写一套，agent 就按错的那套做。
本脚本把「文档声称的」与「代码里真有的」对一遍，非 0 即契约不自洽。

查五件事：
  1. rc 覆盖：platforms 的每个 STALL_* 都在执行契约 §8 的 rc 表里有一行
  2. 动作卡四段齐全（前置断言 / 唯一通路 / 通过判据 / 失败分支）
  3. runbook 索引：SKILL 引用的 runbook 都真实存在，且目录里的 runbook 都被索引
  4. 硬边界表列出的 wa.py 命令真实存在（防文档写不存在的命令）
  5. verified 与 write_verified 分账未被混用

用法：
  python scripts/contract_lint.py [--json] [--root .]
退出码：0 自洽；1 不自洽（逐条列出）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

CARD_SECTIONS = ("**前置断言**", "**唯一通路**", "**通过判据**", "**失败分支**")
RUNBOOK_DIR = os.path.join("references", "runbooks")
CONTRACT = os.path.join("references", "execution-contract.md")
SKILL = "SKILL.md"


def _read(root: str, rel: str) -> str:
    p = os.path.join(root, rel)
    if not os.path.exists(p):
        return ""
    with open(p, encoding="utf-8") as f:
        return f.read()


def _cards(text: str) -> dict:
    """切出 {卡名: 块文本}。标题形如 `## 2. CARD-bind · 接入...`。"""
    out = {}
    parts = re.split(r"(?m)^##\s+", text)
    for part in parts:
        m = re.match(r"\d+\.\s+(CARD-[A-Za-z0-9_-]+)", part.strip())
        if m:
            out[m.group(1)] = part
    return out


def _rc_rows(text: str) -> list:
    """从 §8 的 rc 标记表里取出 rc 列（只认 2~8 列的纯数字行）。"""
    rcs = []
    for line in text.splitlines():
        m = re.match(r"\|\s*(\d{1,2})\s*\|", line.strip())
        if m:
            rcs.append(int(m.group(1)))
    return rcs


def _wa_commands(root: str) -> set:
    src = _read(root, os.path.join("scripts", "wa.py"))
    return set(re.findall(r'sub\.add_parser\(\s*"([a-z_]+)"', src))


def check(root: str) -> list:
    results = []
    contract = _read(root, CONTRACT)
    skill = _read(root, SKILL)

    # 1. rc 覆盖
    try:
        import platforms  # noqa: E402
        import wa  # noqa: E402
        stall_rc = dict(wa.STALL_RC)
        stall_vals = sorted(set(stall_rc.values()))
        states = [getattr(platforms, n) for n in dir(platforms) if n.startswith("STALL_")]
    except Exception as e:  # pragma: no cover - 环境异常单独报
        results.append({"name": "rc_cover", "ok": False,
                        "detail": "无法导入 platforms/wa：%s" % e})
        stall_vals, states = [], []
    else:
        have = set(_rc_rows(contract))
        missing = [v for v in stall_vals if v not in have]
        results.append({
            "name": "rc_cover",
            "ok": not missing,
            "detail": ("rc 表覆盖 %d 个分诊码（%s）" % (len(stall_vals), stall_vals))
            if not missing else ("rc 表缺这些码：%s" % missing),
            "extra": {"stall_states": len(states)},
        })

    # 2. 动作卡四段
    cards = _cards(contract)
    bad = {name: [s for s in CARD_SECTIONS if s not in body] for name, body in cards.items()}
    bad = {k: v for k, v in bad.items() if v}
    results.append({
        "name": "card_sections",
        "ok": bool(cards) and not bad,
        "detail": ("%d 张卡四段齐全" % len(cards)) if cards and not bad
        else ("缺段：%s" % bad if bad else "没找到任何 CARD-* 动作卡"),
        "extra": {"cards": sorted(cards)},
    })

    # 3. runbook 索引
    referenced = set(re.findall(r"references/runbooks/([A-Za-z0-9_.-]+\.md)", skill))
    on_disk = set()
    rb_dir = os.path.join(root, RUNBOOK_DIR)
    if os.path.isdir(rb_dir):
        on_disk = {f for f in os.listdir(rb_dir) if f.endswith(".md")}
    missing_files = sorted(referenced - on_disk)
    unindexed = sorted(on_disk - referenced)
    results.append({
        "name": "runbook_index",
        "ok": not missing_files and not unindexed,
        "detail": ("%d 份 runbook 已索引且存在" % len(on_disk))
        if not missing_files and not unindexed
        else ("引用但不存在：%s；存在但未索引：%s" % (missing_files, unindexed)),
    })

    # 4. 硬边界表里的命令真实存在
    cmds = _wa_commands(root)
    # 只校验硬边界表内出现的 `wa.py <cmd>` 与 `<cmd>` 命令名
    section = ""
    m = re.search(r"(?ms)^###\s*5\.5\s.*?(?=^###\s|\Z)", skill)
    if m:
        section = m.group(0)
    used = set(re.findall(r"wa\.py\s+([a-z_]+)", section))
    unknown = sorted(c for c in used if c not in cmds)
    results.append({
        "name": "command_exists",
        "ok": not unknown,
        "detail": ("硬边界表引用的 %d 个命令都存在" % len(used)) if not unknown
        else ("硬边界表写了不存在的命令：%s（wa.py 现有：%s）" % (unknown, sorted(cmds))),
    })

    # 5. verified / write_verified 分账（且必须带时间戳，防「历史结论当永远有效」）
    leak, no_ts = [], []
    try:
        import platforms as _p
        for key, spec in getattr(_p, "PLATFORMS", {}).items():
            if spec.get("verified") and "write_verified" not in spec:
                leak.append(key)
            if spec.get("write_verified") and not spec.get("write_verified_at"):
                no_ts.append(key)
    except Exception:
        pass
    ok5 = not leak and not no_ts
    results.append({
        "name": "verify_split",
        "ok": ok5,
        "detail": "verified 平台都带 write_verified + 时间戳" if ok5
        else ("标了 verified 却没 write_verified：%s；write_verified 没时间戳：%s"
              % (leak or "无", no_ts or "无")),
    })

    # 6. 禁止裸调开窗（2026-11-02 用户指令「禁止无脑重复打开新窗口」）
    #    开窗唯一入口 = scripts/wa.py 的 ensure_session（那里有全局跨任务频率计数、
    #    组窗口复用、窗口租约）。任何其他 .py 直接 bsk session start = 绕过所有限制
    #    = 2026-11-02 现场 13 个标签堆积的直接根因，FAIL。
    offenders = []
    py_roots = [os.path.join(root, "scripts"), os.path.join(root, "runtime")]
    pat = re.compile(r"\[?\(?[\"']session[\"']\s*,\s*[\"']start[\"']")
    for base in py_roots:
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__",)]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, fn), root)
                if rel.replace("\\", "/") in ("scripts/wa.py",):
                    continue
                try:
                    src = open(os.path.join(dirpath, fn), encoding="utf-8").read()
                except Exception:
                    continue
                if pat.search(src):
                    offenders.append(rel.replace("\\", "/"))
    results.append({
        "name": "no_raw_session_start",
        "ok": not offenders,
        "detail": "开窗唯一入口=wa.py ensure_session，无裸调" if not offenders
        else ("这些文件绕过 wa.py 直接 bsk session start（删掉或改走 wa.py）：%s"
              % offenders),
    })

    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="契约自洽检查")
    ap.add_argument("--root", default=os.path.dirname(HERE))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    root = os.path.abspath(a.root)
    res = check(root)
    ok = all(r["ok"] for r in res)
    if a.json:
        print(json.dumps({"ok": ok, "checks": res}, ensure_ascii=False, indent=2))
    else:
        for r in res:
            print("%s %-16s %s" % ("PASS" if r["ok"] else "FAIL", r["name"], r["detail"]))
        print("verdict:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
