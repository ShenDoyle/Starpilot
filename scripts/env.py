#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""环境自检与自举：Python 版本、浏览器后端（bsk 或 agent 内置浏览器）。

给 agent 用的入口——**别猜、别让用户先去装**，跑一遍这个脚本看现状：
  python scripts/env.py --json      # 机器读：ok / python / bsk / backend / needs_decision / actions
  python scripts/env.py             # 人读报告
  python scripts/env.py --install   # 缺 bsk 时自动安装（官方脚本，用户级，不要 root）
  python scripts/env.py --no-doctor # 不探测浏览器扩展（更快，但不知道扩展装没装）

浏览器后端（0.1.3 起）——很多宿主 agent 自带浏览器，bsk 不是唯一路径：
  python scripts/env.py --set-backend bsk|builtin   # 首次激活时问过用户后记录（~/.starpilot/config.json）
                                                    # 只记录，不安装；选 bsk 还要 --install
  backend 取值顺序：--backend 参数 > config > 自动（找到 bsk 就算 bsk，都没有 = unconfigured）
  needs_decision=true 表示「bsk 没有、用户也没选过」——这时按 SKILL §0.2 弹窗问用户，**只问一次**。

退出码：0 = 就绪（bsk 可用，或后端=builtin）；4 = 缺东西但可按 actions 修；
        5 = 尝试安装后仍不可用。
"""

import argparse
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time

MIN_PY = (3, 9)
BACKENDS = ("bsk", "builtin")
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


# ---------------------------------------------- 浏览器后端配置（0.1.3）

def config_path(env=None) -> str:
    """后端选择落在 ~/.starpilot/config.json；测试可用 STARPILOT_CONFIG 重定向。"""
    env = os.environ if env is None else env
    return env.get("STARPILOT_CONFIG") or os.path.join(
        os.path.expanduser("~"), ".starpilot", "config.json")


def load_config(env=None) -> dict:
    try:
        with io.open(config_path(env), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}  # 不存在 / 损坏 = 没选过，不报错


def set_backend(v, env=None) -> str:
    """记录用户的选择（只记录，不安装）。返回配置文件路径。"""
    if v not in BACKENDS:
        raise SystemExit("未知后端：%s（可选：%s）" % (v, " / ".join(BACKENDS)))
    p = config_path(env)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    cfg = load_config(env)
    cfg["browser_backend"] = v
    cfg["backend_decided_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with io.open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return p


def resolve_backend(flag=None, env=None, bsk_found=None) -> str:
    """bsk 参数 > config > 自动（找到 bsk 就算 bsk，否则 unconfigured）。

    返回值："bsk" / "builtin" / "unconfigured"。
    "unconfigured" = bsk 没有、用户也没选过 → 按 SKILL §0.2 弹窗问一次。
    """
    if flag in BACKENDS:
        return flag
    v = load_config(env).get("browser_backend")
    if v in BACKENDS:
        return v
    if bsk_found:
        return "bsk"
    return "unconfigured"


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


def transport() -> str:
    """传输层（0.1.5）：bsk（默认）| iab。与 browser_backend 正交。"""
    return (os.environ.get("STARPILOT_TRANSPORT") or "bsk").strip().lower()


def check(with_doctor=True, runner=None, timeout=25, backend=None, env=None) -> dict:
    py = python_info()
    bsk = find_bsk(env)
    backend = resolve_backend(backend, env, bsk_found=bsk)
    if backend == "unconfigured" and transport() == "iab" and not bsk:
        # 0.1.5：IAB 传输下 bsk 是可选的，env 自检自动豁免（SKILL §5.1），
        # 报 backend=iab——脚本侧传输层，不是 browser_backend 配置项。
        backend = "iab"
    needs_decision = (backend == "unconfigured")
    report = {
        "ok": False,
        "platform": _plat(),
        "backend": backend,
        "transport": transport(),
        "needs_decision": needs_decision,
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
    if needs_decision:
        acts.append("浏览器后端未定（SKILL §0.2）：用按钮弹窗问用户「安装 bsk（推荐）还是用 agent 内置浏览器」，"
                    "然后 python scripts/env.py --set-backend bsk|builtin 记录；选 bsk 再跑 --install。只问一次。")
    elif backend == "bsk" and not bsk:
        acts.append("bsk 未安装：跑 python scripts/env.py --install（官方脚本，用户级）；"
                    "或改用内置浏览器：--set-backend builtin")
    elif backend == "bsk" and bsk and report["bsk"]["doctor"] \
            and report["bsk"]["doctor"].get("connected") is False:
        acts.append("bsk 已装但扩展没连上：在浏览器启用 BrowserSkill 扩展"
                    "（Chrome %s / Edge %s），再跑 bsk doctor" % (EXT_CHROME, EXT_EDGE))
    report["ok"] = not acts and bool(py["ok"]) and (
        bool(bsk) or backend in ("builtin", "iab"))
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


BACKEND_LABEL = {
    "bsk": "bsk（外部浏览器 + BrowserSkill 扩展，全自动循环）",
    "builtin": "builtin（agent 内置浏览器，手动规程：references/builtin-mode.md）",
    "iab": "iab（脚本侧 IAB 传输层，STARPILOT_TRANSPORT=iab，实验性）",
    "unconfigured": "未定（SKILL §0.2：弹窗问用户，只问一次）",
}


def human(rep: dict) -> str:
    py, b = rep["python"], rep["bsk"]
    mark = lambda ok: "✓" if ok else "✗"  # noqa: E731
    lines = [
        "%s Python %s（需 %s+）  %s" % (mark(py["ok"]), py["version"], py["required"], py["exe"]),
        "  浏览器后端：%s" % BACKEND_LABEL.get(rep.get("backend"), "?"),
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
        lines.append("环境就绪，可以直接 wa.py bind <地址> 开工。" if rep.get("backend") != "builtin"
                 else "builtin 后端就绪：脚本不碰浏览器，按 references/builtin-mode.md 的规程手动执行。")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="星舵环境自检 / bsk 自动安装 / 浏览器后端选择")
    ap.add_argument("--json", action="store_true", help="输出 JSON（agent 用）")
    ap.add_argument("--install", action="store_true", help="缺 bsk 时自动安装")
    ap.add_argument("--no-doctor", action="store_true", help="跳过扩展连接探测")
    ap.add_argument("--backend", choices=BACKENDS,
                    help="本次按指定后端判定（不写配置；持久化用 --set-backend）")
    ap.add_argument("--set-backend", choices=BACKENDS, dest="set_backend",
                    help="记录用户选择的浏览器后端到 ~/.starpilot/config.json（首次激活 §0.2，只问一次）")
    ap.add_argument("--timeout", type=int, default=300, help="安装超时秒数")
    a = ap.parse_args(argv)

    if a.set_backend:
        p = set_backend(a.set_backend)
        print("已记录浏览器后端：%s → %s" % (a.set_backend, p))
        if a.set_backend == "bsk":
            print("下一步：python scripts/env.py --install（装 bsk CLI），并让用户在浏览器启用扩展。")
        else:
            print("不装 bsk。浏览器操作由 agent 内置能力执行，规程见 references/builtin-mode.md。")
        return 0

    rep = check(with_doctor=not a.no_doctor, backend=a.backend)
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
