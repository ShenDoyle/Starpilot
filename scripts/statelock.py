#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""statelock.py —— `.wa-state.json` 的单写锁（纯 stdlib）。

为什么需要它（架构评审 F3）：`.wa-state.json` 是**全局单文件**，里面存着
`url / session / turn / awaiting_reply`。两个 run 同时跑（人或忘了自己开过一个、
或崩溃恢复的 run 与新 run 并存）会静默互相覆盖：

- `url` 被覆盖 = 绑定的对话地址换了 → 硬规则 1「同一对话」事实上被破坏；
- 而且破坏是**无声的**：你只会看到「网页 AI 好像忘了上下文」。

所以：**读 state 不加锁，写 state 必须加锁**。锁是文件级的
（`os.open(O_CREAT|O_EXCL)`），锁文件里记 pid / run_id / 时间戳，
过期的锁（进程已死或超过 stale_after）可被接管——否则一次 kill -9 会让项目永久卡死。

刻意**不**做：不引第三方锁库、不换 SQLite、不做常驻守护进程
（约束：纯标准库 + 按需拉起 + 单次循环；见 docs/orchestrator-contract.md §0）。
"""
from __future__ import annotations

import errno
import json
import os
import time


class LockBusy(RuntimeError):
    """锁被别的进程持有。异常里带持有者信息，方便直接报给人看。"""

    def __init__(self, holder: dict, path: str):
        self.holder = holder or {}
        self.path = path
        super().__init__(
            "state 文件被占用：%s（pid=%s run=%s，持有 %ss）"
            % (path, self.holder.get("pid"), self.holder.get("run_id"),
               self.holder.get("age_s")))


def lock_path_for(state_path: str) -> str:
    return state_path + ".lock"


def read_holder_named(lock_path: str) -> dict:
    """读任意锁文件内容（不存在则返回 {}）。"""
    if not os.path.exists(lock_path):
        return {}
    try:
        with open(lock_path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {"broken": True}
    try:
        d["age_s"] = int(time.time() - float(d.get("ts") or 0))
    except Exception:
        d["age_s"] = -1
    return d


def read_holder(state_path: str) -> dict:
    """读锁文件内容（不存在则返回 {}）。给 `wa.py state` 报告用。"""
    return read_holder_named(lock_path_for(state_path))


def _pid_alive(pid: int) -> bool:
    """尽量判断 pid 是否还活着；判断不了就当作活着（宁可保守，不误抢锁）。

    不用 os.kill(pid, 0)：Windows 上 signal 0 会走到 TerminateProcess，风险太大。
    """
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        # Windows：用 OpenProcess 查询（ctypes 是标准库）
        try:
            import ctypes
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not h:
                return False
            try:
                code = wintypes.DWORD()
                if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return code.value == 259  # STILL_ACTIVE
                return True
            finally:
                k32.CloseHandle(h)
        except Exception:
            return True
    try:
        os.kill(int(pid), 0)
        return True
    except OSError as e:
        return e.errno == errno.EPERM
    except Exception:
        return True


def _try_create(path: str, payload: dict) -> bool:
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except OSError:
        return False
    try:
        os.write(fd, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    finally:
        os.close(fd)
    return True


def acquire_named(lock_path: str, owner: str = "", run_id: str = "",
                  timeout: float = 10.0, stale_after: float = 300.0,
                  resource: str = "", allow_reentry: bool = False) -> dict:
    """对任意锁路径拿锁。返回持有者记录；拿不到抛 LockBusy。

    这是 acquire() 的泛化底座：state 单写锁、跨项目平台占用锁共用同一套
    O_EXCL + 死进程/超时接管逻辑，不维护两份。

    allow_reentry 默认 False：state 单写锁要挡住**同进程内的嵌套写**（那是 bug，
    不是需求）。平台占用锁显式打开它，因为 do_send 会被外层 cmd_send 装饰器
    套着调用，同进程重入是正常路径。
    """
    p = lock_path
    os.makedirs(os.path.dirname(os.path.abspath(p)) or ".", exist_ok=True)
    payload = {
        "pid": os.getpid(), "owner": owner or "wa", "run_id": run_id,
        "ts": time.time(), "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "resource": resource or os.path.abspath(p),
    }
    deadline = time.time() + max(0.0, timeout)
    while True:
        if _try_create(p, payload):
            return payload
        holder = read_holder_named(p)
        # 同进程重入（仅 allow_reentry=True 的锁）：外层已持有（如 wa.cmd_send
        # 装饰器 → wa.do_send 再取锁）直接放行，release 时不动锁文件——
        # 否则内层一放，外层就裸奔了。
        if (allow_reentry and int(holder.get("pid") or 0) == os.getpid()
                and not holder.get("broken")):
            return {"pid": os.getpid(), "owner": payload["owner"], "reentrant": True,
                    "resource": payload.get("resource") or os.path.abspath(p),
                    "at": payload["at"]}
        age = holder.get("age_s") or 0
        dead = not _pid_alive(int(holder.get("pid") or 0))
        stale_flag = holder.get("stale")
        # 接管条件：进程已死 / 超时过久 / 锁文件坏了 —— 三者任一即可
        if dead or age > stale_after or holder.get("broken"):
            payload["stale"] = "took_over:%s" % ("dead_pid" if dead else
                                                 ("broken" if holder.get("broken") else "age"))
            try:
                os.remove(p)          # 只删锁文件，不碰用户数据
            except OSError:
                pass
            if _try_create(p, payload):
                return payload
            continue
        if time.time() >= deadline:
            raise LockBusy(holder, p)
        time.sleep(0.15)


def acquire(state_path: str, owner: str = "", run_id: str = "",
            timeout: float = 10.0, stale_after: float = 300.0) -> dict:
    """拿锁。返回持有者记录；拿不到抛 LockBusy。"""
    return acquire_named(lock_path_for(state_path), owner, run_id,
                         timeout, stale_after, resource=os.path.abspath(state_path))


def release_named(lock_path: str, payload: dict = None) -> None:
    p = lock_path
    # 重入层不释放锁文件：真正的持锁者（外层）才负责放锁
    if payload and payload.get("reentrant"):
        return
    if not os.path.exists(p):
        return
    # 只释放自己的锁：pid 对不上就不动（防误删别人的锁）
    try:
        with open(p, encoding="utf-8") as f:
            cur = json.load(f)
        if payload and int(cur.get("pid") or 0) not in (0, os.getpid()):
            return
    except Exception:
        pass
    try:
        os.remove(p)
    except OSError:
        pass


def release(state_path: str, payload: dict = None) -> None:
    release_named(lock_path_for(state_path), payload)


class state_lock(object):
    """`with state_lock(path, run_id=...)`：写 state 时用。"""

    def __init__(self, state_path: str, owner: str = "", run_id: str = "",
                 timeout: float = 10.0, stale_after: float = 300.0):
        self.state_path = state_path
        self.owner = owner
        self.run_id = run_id
        self.timeout = timeout
        self.stale_after = stale_after
        self.payload = None

    def __enter__(self):
        self.payload = acquire(self.state_path, self.owner, self.run_id,
                               self.timeout, self.stale_after)
        return self.payload

    def __exit__(self, *exc):
        release(self.state_path, self.payload)
        return False
