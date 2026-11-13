#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ego_paths.py —— ego-browser 可执行文件路径解析（中立工具层，无后端依赖）。

为什么要独立成模块：路径解析被两个层共用——ego_backend（后端层）与
envelope_node（业务层）。gate_lint 禁止业务模块 import 后端，所以解析逻辑
不能只住在 *_backend 里；放进中立模块让两层各自 import（2026-11-14）。

为什么不能写死版本目录：ego lite 安装在 `Application\\<版本>\\` 下，用户升级
（如 0.6.2.10 → 0.6.3.14）后旧目录即被移除，写死的路径当场失效（实测撞到：
fail-closed 连累全部 dispatch 测试）。

解析顺序：EGO_BROWSER_BIN env > Application\\ 下版本号最高的目录 > PATH
> 已知的旧固定路径（兜底，仅报错信息可用）。
"""
from __future__ import annotations

import os
import shutil

LEGACY_PINNED = (r"C:\Program Files\Citro Labs\ego lite\Application\0.6.2.10"
                 r"\ego-browser.exe")


def resolve_ego_exe(env=None) -> str:
    env = env if env is not None else os.environ
    env_bin = env.get("EGO_BROWSER_BIN")
    if env_bin and os.path.exists(env_bin):
        return env_bin
    base = os.path.join(env.get("ProgramFiles") or r"C:\Program Files",
                        "Citro Labs", "ego lite", "Application")
    try:
        cands = []
        for name in os.listdir(base):
            parts = name.split(".")
            if os.path.exists(os.path.join(base, name, "ego-browser.exe")) \
                    and all(x.isdigit() for x in parts):
                cands.append((tuple(int(x) for x in parts), name))
        if cands:
            return os.path.join(base, max(cands)[1], "ego-browser.exe")
    except OSError:
        pass
    return shutil.which("ego-browser") or LEGACY_PINNED
