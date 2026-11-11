#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L2 PATH shim 安装器（2026-11-02 用户指令：「只要使用 bsk，必须遵守这个规则」）。

做法（Windows）：
  1. 定位 bsk.exe（~/.local/bin，与 wa.py 的候选顺序一致）；
  2. 真身改名 bsk.bin.exe（已在运行会 PermissionError——提示稍后重跑）；
  3. 原位写 bsk.cmd：仅 `session start` 路由到 bsk_shim.py，其余直通真身；
  4. 写 bsk_shim.py（与 bsk.cmd 同目录）。

做法（macOS / Linux，2026-11-11 随 vendor 钉住版一并补齐）：
  1. 定位 bsk（~/.local/bin）；
  2. 真身改名 bsk.bin；
  3. 原位写可执行 shim 脚本 bsk（shebang bash，仅 `session start` 路由
     到 python3 bsk_shim.py，其余直通真身）；
  4. 写 bsk_shim.py。

真身解析：shim 同目录 bsk.bin[.exe] → vendor 钉住版（vendor/bsk/…，
随星舵仓库分发，sha256 校验）。机器级 bsk 被 BSK_AUTO_UPDATE 换成劣化版时，
闸门放行后执行的仍是钉住版。

回滚：把 bsk.bin[.exe] 改回 bsk[.exe]、删掉 shim 脚本与 bsk_shim.py 即可。
注意：`env.py --install` 重装 bsk 会还原真身（绕过 shim），重装后重跑本安装器。
"""
from __future__ import annotations

import os
import shutil
import subprocess
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

SH_TEMPLATE = """#!/bin/bash
# L2 PATH shim (2026-11-11, macOS/Linux): only "session start" is gated;
# all other subcommands pass straight through to the real binary.
DIR="$(cd "$(dirname "$0")" && pwd)"
REAL="$DIR/bsk.bin"
if [ "$1" = "session" ] && [ "$2" = "start" ] && command -v python3 >/dev/null 2>&1; then
  exec python3 "$DIR/bsk_shim.py" "$@"
fi
exec "$REAL" "$@"
"""


def main() -> int:
    posix = sys.platform != "win32"
    exe = os.path.join(BIN_DIR, "bsk" if posix else "bsk.exe")
    bin_real = os.path.join(BIN_DIR, "bsk.bin" if posix else "bsk.bin.exe")
    shim_dst = os.path.join(BIN_DIR, "bsk_shim.py")
    entry_dst = os.path.join(BIN_DIR, "bsk" if posix else "bsk.cmd")

    if not os.path.exists(exe) and not os.path.exists(bin_real):
        print("找不到 %s（bsk 未安装？先 env.py --install）" % exe)
        return 1
    if os.path.exists(exe):
        try:
            os.rename(exe, bin_real)
            print("真身已改名：%s -> %s" % (exe, bin_real))
        except PermissionError as e:
            print("改名失败（真身正被使用，稍后重跑本安装器）：%s" % e,
                  file=sys.stderr)
            return 1
    else:
        print("真身已是 %s，跳过改名" % bin_real)
    shutil.copy2(os.path.join(HERE, "bsk_shim.py"), shim_dst)
    print("已写入：%s" % shim_dst)
    if posix:
        with open(entry_dst, "w", encoding="utf-8", newline="\n") as f:
            f.write(SH_TEMPLATE)
        os.chmod(entry_dst, 0o755)
    else:
        with open(entry_dst, "w", encoding="ascii", newline="\r\n") as f:
            f.write(CMD_TEMPLATE)
    print("已写入：%s" % entry_dst)
    # 自检：--version 走直通路径（仅报告，不据此判安装成败）
    try:
        p = subprocess.run([entry_dst, "--version"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=30)
        print("自检 bsk --version（rc=%d）：%s"
              % (p.returncode, (p.stdout or p.stderr).strip()[:120]))
    except Exception as e:
        print("自检未跑通（不影响安装，手工验证 `bsk doctor` 即可）：%s" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
