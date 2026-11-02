#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L2 PATH shim：拦截 PATH 上裸调的 `bsk session start`（2026-11-02 用户指令：
「只要使用 bsk，必须遵守这个规则」）。

由 bsk.cmd 调用——.cmd 只把 `session start` 路由到这里，其余子命令零开销直通
真身 bsk.bin.exe。规则（与 wa.py 同一套账本）：

· 带 STARPILOT_WINDOW_GATE=1 令牌 → wa.py 正路（已过租约+计数），直接放行；
· 无令牌（其他 agent / 手工）→ 过全局开窗计数（锁目录 session-starts.json，
  与 wa.py 同一份账本）：
    - 未超限：记账后放行（ Warn：正路是 starpilot 的 wa.py）；
    - 超限：拒绝（rc=9），指引走 wa.py 或由用户明示授权清计数；
· shim 自身故障 → fail-open 放行并告警（可用性优先；正路侧仍有
  L3 唯一入口 + contract_lint 双保险）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

REAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bsk.bin.exe")
GATE_ENV = "STARPILOT_WINDOW_GATE"

# winlease（全局计数账本）从星舵安装位找；两处都试
_SKILL_SCRIPT_DIRS = [
    os.path.expanduser(r"~\.workbuddy\skills\starpilot\scripts"),
    os.path.expanduser(r"~\.trae-cn\skills\starpilot\scripts"),
]


def _instance(args: list) -> str:
    for i, a in enumerate(args):
        if a == "--browser" and i + 1 < len(args):
            return args[i + 1]
    return "default"


def _winlease():
    for d in _SKILL_SCRIPT_DIRS:
        if os.path.isdir(d):
            if d not in sys.path:
                sys.path.insert(0, d)
            try:
                import winlease  # noqa: F401
                return winlease
            except Exception:
                continue
    raise ImportError("winlease 不可用（星舵安装位缺失）")


def _run(args: list) -> int:
    p = subprocess.run([REAL] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    sys.stdout.write(p.stdout or "")
    sys.stderr.write(p.stderr or "")
    return p.returncode


def main() -> int:
    args = sys.argv[1:]
    if not os.path.exists(REAL):
        print("BSK-SHIM ERROR: 找不到真身 %s（shim 安装不完整，"
              "重跑 tools/install_l2_shim.py）" % REAL, file=sys.stderr)
        return 1
    if (os.environ.get(GATE_ENV) or "").strip() == "1":
        return _run(args)  # wa.py 正路
    try:
        wl = _winlease()
        inst = _instance(args)
        many, n, mx = wl.too_many_starts(inst)
        if many:
            print(json.dumps({
                "code": "BSK_START_BLOCKED_BY_SHIM",
                "instance": inst,
                "starts_in_window": n, "limit": mx,
                "message": ("L2 shim 拦截：非 wa.py 来源的 bsk session start 已达全局"
                            "上限（10 分钟/实例 %d 次）。这不是正常的重试节奏——会话在"
                            "反复失效或调用方在无脑重开。正路：python "
                            "~/.workbuddy/skills/starpilot/scripts/wa.py（bind/send/"
                            "wait 自带租约与计数）。确属环境正常、需要强行继续：仅限"
                            "用户明示授权，删除锁目录 session-starts.json 里该实例的"
                            "计数后重试。" % mx),
            }, ensure_ascii=False, indent=2), file=sys.stderr)
            return 9
        wl.note_start(inst)
        sys.stderr.write("BSK-SHIM: 未走 wa.py 的 session start 已记账"
                         "（实例 %s，窗口内第 %d 次）。开窗正路是 starpilot 的 "
                         "wa.py：自带窗口租约、组窗口复用与 tab 回收。\n"
                         % (inst, n + 1))
    except SystemExit:
        raise
    except Exception as e:
        sys.stderr.write("BSK-SHIM WARNING: 计数检查失败(%s)，fail-open 放行\n" % e)
    return _run(args)


if __name__ == "__main__":
    sys.exit(main())
