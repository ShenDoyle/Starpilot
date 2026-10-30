#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""环境自检与自举：Python 版本、bsk 是否可用，缺 bsk 就用官方脚本自动装。

给 agent 用的入口——**别猜、别让用户先去装**，跑一遍这个脚本看现状：
  python scripts/env.py --json      # 机器读：ok / python / bsk / actions
  python scripts/env.py             # 人读报告
  python scripts/env.py --install   # 缺 bsk 时自动安装（官方脚本，用户级，不要 root）
  python scripts/env.py --no-doctor # 不探测浏览器扩展（更快，但不知道扩展装没装）

退出码：0 = 一切就绪；4 = 缺东西但可按 actions 修；5 = 尝试安装后仍不可用。
"""

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys

MIN_PY = (3, 9)
INSTALL_SH = "https://raw.githubusercontent.com/Tencent/BrowserSkill/main/install.sh"
INSTALL_PS1 = "https://raw.githubusercontent.com/Tencent/BrowserSkill/main/install.ps1"
EXT_CHROME = "https://chromewebstore.google.com/detail/hhcmgoofomhgciiibhipgmgkgnoenaoi"
EXT_EDGE = "https://microsoftedge.microsoft.com/addons/detail/browserskill/emacgiaaaiojkkpkddmmdfhmokgmnikg"

PY_HINT = {
    "windows": "winget install --id Python.Python.3.12 -e   （或 https://www.python.org/downloads/windows/）",
    "darwin": "brew install python@3.12   （或 https://www.python.org/downloads/macos/）",
    "linux": "sudo apt install -y python3   （或发行版对应的包管理器）",
}


def _plat() -> str:
    p = sys.platform
    if p.startswith("win"):
        return "windows"
    if p == "darwin":
        return "darwin"
    return "linux"


def find_bsk(env=None, which=None) -> str:
    """定位顺序与 wa.py 完全一致：BSK_BIN → PATH → ~/.local/bin。"""
    env = os.environ if env is None else env
    which = shutil.which if which is None else which
    for c in [
        env.get("BSK_BIN") or "",
        which("bsk") or "",
        os.path.expanduser("~/.local/bin/bsk.exe"),
        os.path.expanduser("~/.local/bin/bsk"),
    ]:
        if c and os.path.exists(c):
            return c
    return None


def python_info() -> dict:
    v = sys.version_info
    alt = []
    for name in ("python3", "py", "python"):
        p = shutil.which(name)
        if p and os.path.abspath(p) != os.path.abspath(sys.executable):
            alt.append(p)
    return {
        "ok": (v.major, v.minor) >= MIN_PY,
        "version": "%d.%d.%d" % (v.major, v.minor, v.micro),
        "exe": sys.executable,
        "others": alt,
        "required": ".".join(str(x) for x in MIN_PY),
    }


def install_command(plat: str = None):
    """返回自动安装 bsk 的 argv；不支持的平台返回 None。"""
    plat = plat or _plat()
    if plat == "windows":
        return ["powershell", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", "irm %s | iex" % INSTALL_PS1]
    if plat in ("darwin", "linux"):
        return ["sh", "-c", "curl -fsSL %s | sh" % INSTALL_SH]
    return None


def _run(argv, timeout, runner=None) -> dict:
    runner = runner or subprocess.run
    try:
        p = runner(argv, capture_output=True, text=True,
                   encoding="utf-8", errors="replace", timeout=timeout)
    except Exception as e:  # 超时、找不到可执行文件等
        return {"rc": -1, "out": "", "err": "%s: %s" % (type(e).__name__, e)}
    return {"rc": p.returncode, "out": p.stdout or "", "err": p.stderr or ""}


def bsk_version(path, runner=None, timeout=20):
    if not path:
        return None
    r = _run([path, "--version"], timeout, runner)
    text = (r["out"] or "") + " " + (r["err"] or "")
    m = re.search(r"(\d+\.\d+(?:\.\d+)?)", text)
    return m.group(1) if m else None


def doctor(path, runner=None, timeout=25) -> dict:
    """bsk doctor：看浏览器扩展是否连上。拿不到就返回 unknown，不编。"""
    if not path:
        return {"rc": None, "connected": None}
    r = _run([path, "doctor"], timeout, runner)
    text = ((r["out"] or "") + "\n" + (r["err"] or "")).lower()
    if not text.strip():
        return {"rc": r["rc"], "connected": None}
    if "not connected" in text or "disconnected" in text:
        return {"rc": r["rc"], "connected": False}
    return {"rc": r["rc"], "connected": "connected" in text}


def check(with_doctor=True, runner=None, timeout=25) -> dict:
    py = python_info()
    bsk = find_bsk()
    report = {
        "ok": bool(py["ok"] and bsk),
        "platform": _plat(),
        "python": py,
        "bsk": {"ok": bool(bsk), "path": bsk, "version": bsk_version(bsk, runner, timeout),
                "doctor": None},
        "install_command": install_command(),
        "actions": [],
    }
    if bsk and with_doctor:
        report["bsk"]["doctor"] = doctor(bsk, runner, timeout)
    acts = report["actions"]
    if not py["ok"]:
        acts.append("Python %s+ 不满足，换成更高版本的解释器：%s" % (py["required"], PY_HINT[_plat()]))
    if not bsk:
        acts.append("bsk 未安装：跑 python scripts/env.py --install（官方脚本，用户级）")
    elif report["bsk"]["doctor"] and report["bsk"]["doctor"].get("connected") is False:
        acts.append("bsk 已装但扩展没连上：在浏览器启用 BrowserSkill 扩展"
                    "（Chrome %s / Edge %s），再跑 bsk doctor" % (EXT_CHROME, EXT_EDGE))
    report["ok"] = not acts and bool(py["ok"] and bsk)
    return report


def install(timeout=300, runner=None, rep=None) -> dict:
    """用官方脚本装 bsk，装完立刻复检。不碰 Python——那是解释器，得用对的那个跑我。"""
    cmd = install_command()
    if not cmd:
        return {"ok": False, "rc": None, "detail": "不支持的平台：%s" % sys.platform}
    r = _run(cmd, timeout, runner)
    after = rep or check(with_doctor=False, runner=runner, timeout=25)
    ok = bool(after["bsk"]["ok"])
    return {"ok": ok, "rc": r["rc"], "path": after["bsk"]["path"],
            "version": after["bsk"]["version"],
            "tail": (r["out"] or r["err"] or "").strip().splitlines()[-6:],
            "doctor": (after["bsk"].get("doctor") or {}).get("connected")}


def human(rep: dict) -> str:
    py, b = rep["python"], rep["bsk"]
    mark = lambda ok: "✓" if ok else "✗"  # noqa: E731
    lines = [
        "%s Python %s（需 %s+）  %s" % (mark(py["ok"]), py["version"], py["required"], py["exe"]),
        "%s bsk %s  %s" % (mark(b["ok"]), b["version"] or "-", b["path"] or "未找到"),
    ]
    if b["doctor"] is not None:
        c = b["doctor"].get("connected")
        lines.append("%s 浏览器扩展  %s" % (mark(c is True),
                                            {True: "已连接", False: "未连接", None: "未知"}[c]))
    if py["others"]:
        lines.append("  其它解释器：%s" % "  ".join(py["others"]))
    if rep["actions"]:
        lines.append("")
        lines += ["→ " + a for a in rep["actions"]]
    else:
        lines.append("")
        lines.append("环境就绪，可以直接 wa.py bind <地址> 开工。")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="星舵环境自检 / bsk 自动安装")
    ap.add_argument("--json", action="store_true", help="输出 JSON（agent 用）")
    ap.add_argument("--install", action="store_true", help="缺 bsk 时自动安装")
    ap.add_argument("--no-doctor", action="store_true", help="跳过扩展连接探测")
    ap.add_argument("--timeout", type=int, default=300, help="安装超时秒数")
    a = ap.parse_args(argv)

    rep = check(with_doctor=not a.no_doctor)
    inst = None
    if a.install and not rep["bsk"]["ok"]:
        inst = install(timeout=a.timeout, rep=None)
        rep = check(with_doctor=not a.no_doctor)

    if a.json:
        out = dict(rep)
        if inst is not None:
            out["install"] = inst
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(human(rep))
        if inst is not None:
            print("\n安装结果：%s  rc=%s" % ("成功" if inst["ok"] else "失败", inst["rc"]))
            for line in inst.get("tail") or []:
                print("  " + line)

    if rep["ok"]:
        return 0
    if inst is not None and not rep["bsk"]["ok"]:
        return 5
    return 4


if __name__ == "__main__":
    sys.exit(main())
