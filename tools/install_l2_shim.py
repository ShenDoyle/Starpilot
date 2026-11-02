#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L2 PATH shim 安装器（2026-11-02 用户指令：「只要使用 bsk，必须遵守这个规则」）。

做法：
  1. 定位 bsk.exe（~/.local/bin，与 wa.py 的候选顺序一致）；
  2. 真身改名 bsk.bin.exe（已在运行会 PermissionError——提示稍后重跑）；
  3. 原位写 bsk.cmd：仅 `session start` 路由到 bsk_shim.py，其余直通真身；
  4. 写 bsk_shim.py（与 bsk.cmd 同目录）。

回滚：把 bsk.bin.exe 改回 bsk.exe、删掉 bsk.cmd 与 bsk_shim.py 即可。
注意：`env.py --install` 重装 bsk 会还原 bsk.exe（绕过 shim），重装后重跑本安装器。
"""
from __future__ import annotations

import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = os.path.expanduser("~/.local/bin")

CMD_TEMPLATE = """@echo off
rem L2 PATH shim (2026-11-02): only "session start" is gated; all other
rem subcommands pass straight through to the real binary at zero cost.
setlocal
set "REAL=%~dp0bsk.bin.exe"
if /i not "%~1"=="session" goto real
if /i not "%~2"=="start" goto real
where python >nul 2>nul || goto real
python "%~dp0bsk_shim.py" %*
exit /b %errorlevel%
:real
"%REAL%" %*
exit /b %errorlevel%
"""


def main() -> int:
    exe = os.path.join(BIN_DIR, "bsk.exe")
    bin_real = os.path.join(BIN_DIR, "bsk.bin.exe")
    if not os.path.exists(exe) and not os.path.exists(bin_real):
        print("找不到 %s（bsk 未安装？先 env.py --install）" % exe)
        return 1
    if os.path.exists(exe):
        try:
            os.rename(exe, bin_real)
            print("真身已改名：%s -> %s" % (exe, bin_real))
        except PermissionError as e:
            print("改名失败（bsk.exe 正被使用，稍后重跑本安装器）：%s" % e,
                  file=sys.stderr)
            return 1
    else:
        print("真身已是 %s，跳过改名" % bin_real)
    shim_dst = os.path.join(BIN_DIR, "bsk_shim.py")
    shutil.copy2(os.path.join(HERE, "bsk_shim.py"), shim_dst)
    cmd_dst = os.path.join(BIN_DIR, "bsk.cmd")
    with open(cmd_dst, "w", encoding="ascii", newline="\r\n") as f:
        f.write(CMD_TEMPLATE)
    print("已写入：%s\n已写入：%s" % (shim_dst, cmd_dst))
    # 自检：--version 走直通路径（仅报告，不据此判安装成败）
    import subprocess
    try:
        p = subprocess.run([cmd_dst, "--version"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        print("自检 bsk --version（rc=%d）：%s"
              % (p.returncode, (p.stdout or p.stderr).strip()[:120]))
    except Exception as e:
        print("自检未跑通（不影响安装，手工验证 `bsk doctor` 即可）：%s" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
