#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendored bsk（钉住版）解析——2026-11-11 用户裁定。

背景：bsk 0.4.2+ 已知劣化（浏览器任务被频繁打断），且 bsk 自带
BSK_AUTO_UPDATE 会绕过一切提示直接换掉机器级二进制（2026-11-09 实锤）。
因此星舵把匹配版本的 bsk 二进制**随仓库分发**：

    vendor/bsk/<ver>/<triple>/bsk[.exe]
    vendor/bsk/manifest.json     # 版本、来源、各 triple 的 sha256

解析顺序（env.find_bsk / wa.py 共用本模块）：

    BSK_BIN 环境变量（显式人工覆盖，永远最高）
    → vendor 钉住版（sha256 校验通过才返回，不符 = 文件损坏，fail-closed 跳过）
    → shim 真身 bsk.bin.exe → PATH → ~/.local/bin（机器级兜底）

机器级 bsk 升不升级、坏不坏，都不再影响星舵自己的调用；
PATH 上的 L2 shim 闸门对「裸调 bsk」的拦截依旧有效。
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def triple(sysp: str = None, machine: str = None):
    """当前平台的官方 release triple；无产物的平台返回 None。"""
    sysp = sysp or sys.platform
    machine = (machine or platform.machine() or "").lower()
    if sysp == "win32":
        # 官方只发 x64 Windows 产物；arm64 Windows 走模拟也能跑 x64
        return "x86_64-pc-windows-msvc"
    if sysp == "darwin":
        return "aarch64-apple-darwin" if machine in ("arm64", "aarch64") \
            else "x86_64-apple-darwin"
    if sysp.startswith("linux"):
        return "aarch64-unknown-linux-musl" if machine in ("arm64", "aarch64") \
            else "x86_64-unknown-linux-musl"
    return None


def manifest(skill_root: str = None) -> dict:
    """读 vendor/bsk/manifest.json；缺失/损坏返回 None（不猜）。"""
    p = os.path.join(skill_root or _ROOT, "vendor", "bsk", "manifest.json")
    try:
        with open(p, encoding="utf-8") as f:
            m = json.load(f)
        return m if isinstance(m, dict) and m.get("version") and m.get("assets") else None
    except Exception:
        return None


def pinned_version(skill_root: str = None) -> str:
    """钉住版本号，如 '0.4.1'；没有 vendor 时 None。"""
    return (manifest(skill_root) or {}).get("version")


def vendor_bsk(skill_root: str = None, sysp: str = None,
               machine: str = None, verify: bool = True) -> str:
    """钉住版二进制的绝对路径。

    fail-closed：平台无产物、文件缺失、sha256 不符一律返回 None，
    由调用方落到机器级候选——绝不返回一个校验不过的二进制。
    verify=False 仅用于测试（跳过哈希，省 6MB 读一遍的开销）。
    """
    m = manifest(skill_root)
    if not m:
        return None
    t = triple(sysp, machine)
    a = (m.get("assets") or {}).get(t) if t else None
    if not a:
        return None
    root = skill_root or _ROOT
    p = os.path.join(root, "vendor", "bsk", str(m["version"]), t, a["file"])
    if not os.path.exists(p):
        return None
    if verify:
        with open(p, "rb") as f:
            h = hashlib.sha256(f.read()).hexdigest()
        if h != a.get("sha256"):
            return None
    return p


if __name__ == "__main__":  # 手工自检：python scripts/bsk_vendor.py
    m = manifest()
    print("pinned:", pinned_version())
    print("triple:", triple())
    print("path  :", vendor_bsk() or "(本平台无 vendor 产物 / 校验未过)")
