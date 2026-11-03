#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L2 PATH shim：拦截 PATH 上裸调的 `bsk session start`（2026-11-02 用户指令：
「只要使用 bsk，必须遵守这个规则」）。

由 bsk.cmd 调用——.cmd 只把 `session start` 路由到这里，其余子命令零开销直通
真身 bsk.bin.exe。规则（与 wa.py 同一套账本）：

· 带**有效一次性票据**（`STARPILOT_WINDOW_TICKET`，由 wa.py 用 HMAC 签发、
  兑换即焚）→ wa.py 正路（已过租约 + 全局计数），直接放行；
· 无票据（其他 agent / 手工）→ 过全局开窗计数（锁目录 session-starts.json，
  与 wa.py 同一份账本）：未超限记账后放行；超限拒绝（rc=9）。

**fail-closed（2026-11-02 Web AI 复审裁定）**：旧版在 shim 自身故障时
fail-open 放行，被明确「不追认」——放行等于门不存在。现在只要有以下任一情况，
一律拒绝并给 rc=9：
  · 真身缺失 / 账本（winlease）不可导入 / 计数读不出 / 记账写不进 /
    票据校验过程抛异常 / 任何未预期异常。
唯一例外：票据校验**明确通过**（redeemed）→ 放行。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

REAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bsk.bin.exe")
TICKET_ENV = "STARPILOT_WINDOW_TICKET"

# winlease（全局计数账本 + 票据校验）从星舵安装位找；两处都试
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
    last = ""
    for d in _SKILL_SCRIPT_DIRS:
        if os.path.isdir(d):
            if d not in sys.path:
                sys.path.insert(0, d)
            try:
                import winlease  # noqa: F401
                return winlease
            except Exception as e:      # 导入失败要继续试下一个安装位
                last = "%s: %s" % (d, e)
                continue
    raise ImportError(last or "未找到星舵安装位（%s）" % ", ".join(_SKILL_SCRIPT_DIRS))


def _deny(code: str, message: str) -> int:
    print(json.dumps({"code": code, "message": message}, ensure_ascii=False,
                     indent=2), file=sys.stderr)
    return 9


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
    try:
        wl = _winlease()
    except Exception as e:
        # 账本不可用 = 无法判断「是否允许开窗」→ 拒绝（fail-closed）
        return _deny("BSK_SHIM_NO_LEDGER",
                     "L2 shim 无法加载星舵账本（winlease）：%s。按 fail-closed "
                     "拒绝开窗。请确认星舵安装在 ~/.workbuddy/skills/starpilot，"
                     "或重跑 tools/install_l2_shim.py 后由用户授权重试。" % e)

    ticket = (os.environ.get(TICKET_ENV) or "").strip()
    try:
        if ticket:
            res = wl.gate_redeem(ticket)
            if res.get("ok"):
                return _run(args)          # wa.py 正路：已过租约 + 计数
            return _deny("BSK_SHIM_BAD_TICKET",
                         "通行票据无效（%s）。票据一次性且 %0.f 秒过期：只有 wa.py "
                         "能签发。正路：python ~/.workbuddy/skills/starpilot/scripts/"
                         "wa.py bind/send/wait。"
                         % (res.get("why"), wl.GATE_TICKET_TTL))
        inst = _instance(args)
        many, n, mx = wl.too_many_starts(inst)
        if many:
            return _deny("BSK_START_BLOCKED_BY_SHIM",
                         "非 wa.py 来源的 bsk session start 已达全局上限"
                         "（10 分钟/实例 %d 次，当前 %d）。这不是正常的重试节奏——"
                         "会话在反复失效或调用方在无脑重开。正路：python "
                         "~/.workbuddy/skills/starpilot/scripts/wa.py。确属环境正常、"
                         "需要强行继续：仅限用户明示授权，删除锁目录 "
                         "session-starts.json 里该实例的计数后重试。" % (mx, n))
        wl.note_start(inst)
        sys.stderr.write("BSK-SHIM: 未走 wa.py 的 session start 已记账"
                         "（实例 %s，窗口内第 %d 次）。开窗正路是 starpilot 的 "
                         "wa.py：自带窗口租约、组窗口复用与 tab 回收。\n"
                         % (inst, n + 1))
    except SystemExit:
        raise
    except Exception as e:
        # 旧版在这里 fail-open（可用性优先），复审明确不追认 → 现在一律拒绝
        return _deny("BSK_SHIM_LEDGER_ERROR",
                     "L2 shim 计数/票据校验失败（%s：%s）。按 fail-closed 拒绝开窗；"
                     "先修环境（~/.starpilot/locks 可写、winlease 可导入）再重试。"
                     % (type(e).__name__, str(e)[:160]))
    return _run(args)


if __name__ == "__main__":
    sys.exit(main())
