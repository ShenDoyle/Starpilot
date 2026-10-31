# -*- coding: utf-8 -*-
"""bsk 0.4.0 兼容层：为所有页面动作自动补 cooperate 上下文。

## 背景（2026-10-31 实测）

bsk 从 0.3.x 升到 0.4.0 后，所有**触页面**的子命令（evaluate / navigate / fill /
press / click / step）都要求 `--context`，其值来自 `bsk cooperate sync` 返回的
`context_id`，且 **一个 context 只授权一次动作**。旧写法（直接 `bsk evaluate ...
--session sid`）会得到 rc!=0：

    error: shared page control or observation changed
    details: This action requires the context returned by the latest cooperate sync

页面内容在 sync 与动作之间变了（倒计时、流式输出等）还会得到：

    details: The action's observed content changed; synchronize and replan

星舵 0.1.2 全部按旧 API 调用，所以「一上页面就报错」——这是 v0.1.2 时期
「星舵问题太多」的最大单一来源。

## 本层做法

不改任何上层逻辑，只在最底层的 `bsk()` 调用上加自动重试：

1. 正常执行；若 rc!=0 且错误符合 cooperate 语义（见 `_NEEDS_CONTEXT`），进入补救。
2. `bsk cooperate wait` → `bsk cooperate sync` → 解析 `context_id`；
   解析不到就原样返回失败（不硬猜）。
3. 给原命令追加 `--context <id>` 重试；若再次报「observed content changed」，
   再 sync 一次重试（最多 2 轮补救）。
4. `cooperate wait` 返回 `stopped=true` 说明 Agent Window 被关/被停，
   这种失败不是本层能救的，原样上抛（上层按 session_lost 自愈）。

只对「触页面」的子命令启用补救；session / browsers / doctor / daemon 等
本地命令失败直接返回，不做无谓的 sync（每次 sync 约 1-2 秒）。
"""

import json
import re
import subprocess
import time

# 命中这些字样 = 缺 cooperate context 或 context 已过期，值得补救
_NEEDS_CONTEXT = (
    "requires the context returned by the latest cooperate sync",
    "observed content changed",
    "shared page control or observation changed",
    "requires the context",
)

# 这些子命令不触页面，补救没有意义
_LOCAL_CMDS = {
    "session", "browsers", "doctor", "daemon", "status", "logs", "update",
    "install-skill", "view", "debug",
}

_CTX_RE = re.compile(r'"context_id"\s*:\s*"([^"]+)"')
_STOPPED_RE = re.compile(r'"stopped"\s*:\s*true')


def bsk_bin() -> str:
    """由宿主（wa.py）注入 bsk 可执行路径，避免重复解析。"""
    global _BIN
    return _BIN


_BIN = ""

# 测试注入点：替换成假执行器后，_run 与 _sync_context 都走它
_RUN_FN = None


def set_run_fn(fn) -> None:
    global _RUN_FN
    _RUN_FN = fn


def set_bin(path: str) -> None:
    global _BIN
    _BIN = path


def _run(args, timeout):
    if _RUN_FN is not None:
        return _RUN_FN(args, timeout)
    try:
        p = subprocess.run([_BIN] + list(args), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", "bsk 超时（%ss）：%s" % (timeout, " ".join(args[:2]))
    return p.returncode, p.stdout or "", p.stderr or ""


def _sync_context(sid):
    """cooperate wait + sync，返回 context_id 或 None。"""
    _run(["cooperate", "wait", "--session", sid], timeout=30)
    rc, out, err = _run(["cooperate", "sync", "--session", sid], timeout=60)
    blob = (out or "") + (err or "")
    if _STOPPED_RE.search(blob):
        return None  # 窗口被关/被停，救不了
    m = _CTX_RE.search(blob)
    return m.group(1) if m else None


def run(bsk_fn, args, timeout):
    """bsk_fn(标准 subprocess 包装) 的上下文感知包装。

    bsk_fn: wa.py 的原 bsk()（保证超时/编码语义一致）。返回 (rc, out, err)。
    """
    rc, out, err = bsk_fn(args, timeout)
    if rc == 0:
        return rc, out, err
    blob = (err or "") + (out or "")
    cmd0 = args[0] if args else ""
    if cmd0 in _LOCAL_CMDS:
        return rc, out, err
    if not any(k in blob for k in _NEEDS_CONTEXT):
        return rc, out, err
    sid = _sid_of(args)
    if not sid:
        return rc, out, err
    for _ in range(2):  # 最多两轮补救：sync→重试；页面又变了再 sync→重试
        ctx = _sync_context(sid)
        if not ctx:
            return rc, out, err
        if "--context" in args:
            i = args.index("--context")
            args = args[:i] + args[i + 2:] if len(args) > i + 2 else args[:i]
        rc, out, err = bsk_fn(list(args) + ["--context", ctx], timeout)
        if rc == 0:
            return rc, out, err
        blob = (err or "") + (out or "")
        if not any(k in blob for k in _NEEDS_CONTEXT):
            return rc, out, err
        time.sleep(1)
    return rc, out, err


def _sid_of(args):
    a = list(args)
    for i, v in enumerate(a):
        if v == "--session" and i + 1 < len(a):
            return a[i + 1]
    return None
