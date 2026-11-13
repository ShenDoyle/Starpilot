#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""procidentity.py —— Windows 可验证的进程身份（process_start_token / executor_id）。

为什么需要（PROJECT-03-R1 任务书 §3.2 / 终审补充 C）：只看 PID 会把「PID 被
复用」误判成「原持有者还活着」；只看 PID + 时间戳又会让接管逻辑滥用（C3-c：
活进程仅因锁年龄被抢）。本模块提供**每个进程生命周期唯一、可复核的启动身份**：

- Windows：`OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) + GetProcessTimes`
  取进程创建时间（FILETIME 64 位）。
- POSIX：`/proc/<pid>/stat` 第 22 字段 starttime + boot_id（boot 相对计时必须
  绑定本次开机，否则跨重启误判）。

token 形如 `win-ft:1337...` / `posix:<boot_id>:<ticks>`。同一 PID 在不同生命
周期里 token 必然不同 —— 这就是防 PID 复用的依据。

失败语义（fail-closed 纪律）：
- 进程确认不存在        → `None`（上层判 dead）
- 存在但身份不可得      → `"unknown"`（上层 fail-closed，禁止接管）
- 查询异常              → `"unknown"`（绝不把异常当 dead）
- 存在且身份不一致      → 上层判 mismatch（PID 复用，按任务书禁止接管）
"""
from __future__ import annotations

import os
import socket


def self_token() -> str:
    """当前进程的 process_start_token。"""
    return _token_of(os.getpid()) or "unknown"


def executor_id(run_id: str = "", owner: str = "") -> str:
    """单执行进程身份：ex-<pid>-<token>-<host>[-<owner>[-<run_id>]]。

    进程退出即失效；PID 复用后 token 必变；主机名参与，防止把另一台机器
    的锁文件误认成同一执行者（fencing 作用域边界：单机多进程）。
    """
    parts = ["ex", str(os.getpid()), self_token(),
             socket.gethostname() or "unknown-host"]
    if owner:
        parts.append(str(owner))
    if run_id:
        parts.append(str(run_id))
    return "-".join(parts)


def token_status(pid: int, token: str) -> str:
    """核验 (pid, token) 指向的进程身份，返回四态：

    - "dead"     进程确认不存在（原持有者已死亡 → 允许受控接管）
    - "match"    进程存在且启动身份与 token 一致（原持有者存活）
    - "mismatch" 进程存在但身份不一致（PID 已复用；按任务书**禁止接管**）
    - "unknown"  核验不可用（fail-closed，禁止接管）
    """
    try:
        pid = int(pid)
    except Exception:
        return "dead"  # 记录本身就是坏的，pid 缺失/非法视同无持有者
    if pid <= 0:
        return "dead"
    cur = _token_of(pid)
    if cur is None:
        return "dead"
    if not token or not isinstance(token, str) or token == "unknown":
        return "unknown"
    return "match" if cur == token else "mismatch"


# ---------------------------------------------------------------- 内部

def _token_of(pid: int):
    """取 pid 的启动身份；进程不存在 → None；不可得 → "unknown"。"""
    if os.name == "nt":
        return _win_token(pid)
    return _posix_token(pid)


def _win_token(pid: int):
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        ERROR_ACCESS_DENIED = 5
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            err = k32.GetLastError()
            if err == ERROR_ACCESS_DENIED:
                # 进程大概率存在（无权查身份）：fail-closed，不当 dead
                return "unknown"
            return None  # 进程不存在（无效参数/找不到）
        try:
            ft_create = wintypes.FILETIME()
            ft_exit = wintypes.FILETIME()
            ft_kernel = wintypes.FILETIME()
            ft_user = wintypes.FILETIME()
            if not k32.GetProcessTimes(h, ctypes.byref(ft_create),
                                       ctypes.byref(ft_exit),
                                       ctypes.byref(ft_kernel),
                                       ctypes.byref(ft_user)):
                return "unknown"
            return "win-ft:%d" % ((ft_create.dwHighDateTime << 32)
                                  | ft_create.dwLowDateTime)
        finally:
            k32.CloseHandle(h)
    except Exception:
        return "unknown"


def _posix_token(pid: int):
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            data = f.read()
        # stat 的 comm 字段可含空格与括号，取最后一个 ')' 之后按空白切
        rest = data[data.rfind(b")") + 1:].split()
        starttime = rest[19]  # 字段 22（1 基），去掉 pid 与 comm 后第 20 项
        boot_id = _boot_id()
        if boot_id is None:
            return "unknown"
        return "posix:%s:%s" % (boot_id, starttime.decode("ascii", "replace"))
    except FileNotFoundError:
        return None  # 进程不存在
    except Exception:
        return "unknown"


def _boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as f:
            return f.read().strip()
    except Exception:
        return None
