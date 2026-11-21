#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""环境自检与自举：Python 版本、浏览器通路（ego-browser 默认 / bsk 备用 / 内置浏览器）。

给 agent 用的入口——**别猜、别让用户先去装**，跑一遍这个脚本看现状：
  python scripts/env.py --json      # 机器读：ok / python / backend / transport / actions
  python scripts/env.py             # 人读报告
  python scripts/env.py --install   # 缺依赖时自动安装（egolite 走 GUI 引导；bsk 走官方脚本）

浏览器通路（2026-11-21 依赖政策）：
  ego-browser（egolite 传输）是**默认且首选**的依赖；bsk 已降级为备用、后续计划移除。
  只有用户明确表示「不愿使用 ego」时才允许切 bsk；Playwright 等仅在同样前提下作最后手段。
  内置浏览器（builtin）仍可作为「什么都不装」的兜底规程。
  python scripts/env.py --set-transport egolite|bsk|iab   # 传输层（默认 egolite）
  python scripts/env.py --set-backend bsk|builtin         # 旧后端选择，仅 bsk 备用/内置浏览器需要
  取值顺序：--backend > config > 自动（egolite 传输默认；bsk 只在显式选择后生效）。

退出码：0 = 就绪；4 = 缺东西但可按 actions 修；
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import safeprint  # noqa: E402

# env.py 是 agent 开工前跑的第一条命令。它在 GBK 控制台上崩掉，
# 后面所有「环境没问题」的结论都无从谈起。
safeprint.harden_streams()

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
    """定位顺序与 wa.py 完全一致：BSK_BIN → vendor 钉住版 → shim 真身 bsk.bin.exe
    → PATH → ~/.local/bin。vendor（bsk_vendor.vendor_bsk）sha256 校验不过就跳过，
    落到机器级候选——机器级 bsk 被 BSK_AUTO_UPDATE 换成劣化版也不影响星舵。"""
    env = os.environ if env is None else env
    which = shutil.which if which is None else which
    import bsk_vendor  # noqa: F401  同目录模块，延迟导入便于测试打桩
    for c in [
        env.get("BSK_BIN") or "",
        bsk_vendor.vendor_bsk(),
        os.path.expanduser("~/.local/bin/bsk.bin.exe"),
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


# ---- 无人值守硬开关（0.1.6 未发）：解决「agent 每轮都停下来要确认」----
# 取值顺序：环境变量 STARPILOT_UNATTENDED（1/true/on 开，0/false/off 关）> config > 关。
# 开了之后：orchestrator init 的执行模式缺省从 step（每轮停）变成 auto（无人值守），
# 写闸门照旧 fail-closed——安全边界不放松，放的是「每轮重复确认」，不是「无授权写」。
_TRUTHY = ("1", "true", "on", "yes")
_FALSY = ("0", "false", "off", "no", "")


def unattended(env=None) -> bool:
    env = os.environ if env is None else env
    raw = (env.get("STARPILOT_UNATTENDED") or "").strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY and raw != "":
        return False
    return bool(load_config(env).get("unattended"))


def set_unattended(v: bool, env=None) -> str:
    p = config_path(env)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    cfg = load_config(env)
    cfg["unattended"] = bool(v)
    with io.open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return p


def set_idle_close(seconds: int, env=None) -> str:
    """窗口闲置收割时限（秒）：agent 超过这么久没有续租，收割器就关它的窗口。"""
    seconds = int(seconds)
    if seconds < 60:
        raise SystemExit("idle_close_s 最小 60 秒（给太短会误杀正常等待）")
    p = config_path(env)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    cfg = load_config(env)
    cfg["idle_close_s"] = seconds
    with io.open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return p


def resolve_backend(flag=None, env=None, bsk_found=None) -> str:
    """--backend > config > 传输层默认（egolite 传输 → egelite；显式 bsk 且已装 → bsk）。

    返回值："egolite" / "bsk" / "builtin" / "iab" / "unconfigured"。
    默认传输是 egolite（2026-11-21 依赖政策），因此 bsk「装着」也不会自动接管；
    只有用户显式选 `--set-transport bsk` 或 `--set-backend bsk` 才走 bsk。
    "unconfigured" 只在 bsk 传输下既没装 bsk、用户也没选过时出现。
    """
    if flag in BACKENDS:
        return flag
    v = load_config(env).get("browser_backend")
    if v in BACKENDS:
        return v
    if transport() == "egolite":
        return "egolite"
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
                   encoding="utf-8", errors="backslashreplace", timeout=timeout)
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
    """传输层：env STARPILOT_TRANSPORT > config "transport" > egolite（默认）。

    2026-11-21 依赖政策：ego 是唯一默认/首选依赖；bsk 仅备用、计划移除，
    必须由用户显式选择（env 或 config）才启用。
    """
    try:
        import transport as _t
        return _t.current_transport()
    except Exception:
        return (os.environ.get("STARPILOT_TRANSPORT") or "egolite").strip().lower()


def find_ego_browser(env=None) -> str:
    """ego-browser CLI 位置：EGO_BROWSER_BIN > PATH。"""
    env = env or os.environ
    p = env.get("EGO_BROWSER_BIN")
    if p and os.path.exists(p):
        return p
    return shutil.which("ego-browser") or ""


# ego 版本锚点（2026-11-12 用户裁定：只记录不拦，靠特性清单对照分析告警）。
# 每次实测新版本后由 agent 回填 cli 锚点并更新 references/egolite-mode.md 锚点表。
# 痛点前科：bsk 0.4.1 一夜改坏星舵（36 条 ESC 违例）——锚点 + 清单就是为此。
EGO_ANCHOR = (0, 6, 3, 14)          # 星舵全链实测通过的 ego CLI 版本
EGO_ANCHOR_GUIDE = "v2.2.1"         # `ego-browser skill` 输出的 guide 版本
EGO_ANCHOR_DATE = "2026-11-14"
EGO_MIN = (0, 6, 2, 10)             # 低于此版本：缺 worker 依赖的 API 面，提示升级
EGO_SITE = "https://lite.ego.app/"


def ego_version(path=None, runner=None, timeout=20):
    """解析 `ego-browser --version`。返回 (主版本元组, guide版本|None, 原始首行)。

    输出形态（实测 0.6.2.10）：第一行 'ego-browser 0.6.2.10'；guide 版本取
    `ego-browser skill` 首行 '[ego-browser:skill] ego-browser usage guide v2.2.0'
    ——guide 另查（开销大），本函数不查，由调用方按需取。
    解析不出如实返回 ((), None, 首行)，不猜。
    """
    if not path:
        return (), None, ""
    r = _run([path, "--version"], timeout, runner)
    text = ((r["out"] or "") + "\n" + (r["err"] or "")).strip()
    first = text.splitlines()[0].strip() if text else ""
    m = re.search(r"(\d+(?:\.\d+)+)", first)
    if not m:
        return (), None, first
    v = tuple(int(x) for x in m.group(1).split("."))
    return v, None, first


def ego_info(env=None, runner=None, timeout=20) -> dict:
    """ego 依赖体检（R1/R3 共用）。status 四态：
    ok（=锚点）/ older（<EGO_MIN，须升）/ newer（>锚点，走 §F 清单评估）/ unknown。
    只记录不拦——拦不拦由 agent 按评估结论决定（§F），本函数只给事实与指引。
    """
    path = find_ego_browser(env)
    v, _, first = ego_version(path, runner, timeout)
    if not path:
        return {"ok": False, "path": "", "version": "", "guide": "",
                "anchor": upg_fmt(EGO_ANCHOR), "status": "unknown",
                "hint": "未安装：按 ego-browser skill 的 install 文档装（%s）" % EGO_SITE}
    if not v:
        return {"ok": False, "path": path, "version": first, "guide": "",
                "anchor": upg_fmt(EGO_ANCHOR), "status": "unknown",
                "hint": "版本号解析不出（输出：%s）——如实上报，勿猜" % first[:80]}
    anchor = upg_fmt(EGO_ANCHOR)
    if v < EGO_MIN:
        return {"ok": False, "path": path, "version": upg_fmt(v), "guide": "",
                "anchor": anchor, "status": "older",
                "hint": "低于最低可用版本：请用户执行 ego-browser upgrade"}
    if v > EGO_ANCHOR:
        return {"ok": True, "path": path, "version": upg_fmt(v), "guide": "",
                "anchor": anchor, "status": "newer",
                "hint": ("新于实测锚点 %s：按 references/egolite-mode.md §F 对照特性清单评估；"
                         "特性有破坏性变化则提示「暂勿使用无人值守模式」，无变化则无感使用并回填锚点"
                         % anchor)}
    return {"ok": True, "path": path, "version": upg_fmt(v), "guide": "",
            "anchor": anchor, "status": "ok",
            "hint": ""}


def upg_fmt(v) -> str:
    return ".".join(str(x) for x in (v or ())) or "(未知)"


def check(with_doctor=True, runner=None, timeout=25, backend=None, env=None) -> dict:
    py = python_info()
    bsk = find_bsk(env)
    backend = resolve_backend(backend, env, bsk_found=bsk)
    t = transport()
    ego = ""
    egoinfo = None
    if t == "egolite":
        # 2026-11-12：egolite 传输下页面动作走 op 队列，bsk 即使在装也不参与；
        # env 自检改查 ego-browser，报 backend=egolite——脚本侧传输层，
        # 不是 browser_backend 配置项。ego-browser 缺失时不走 §0.2 弹窗
        # （needs_decision 保持 False），由下方 actions 给安装指引。
        ego = find_ego_browser(env)
        egoinfo = ego_info(env, runner, timeout) if ego else None
        backend = "egolite"
    if backend == "unconfigured" and t == "iab" and not bsk:
        # 0.1.5：IAB 传输下 bsk 是可选的，env 自检自动豁免（SKILL §5.1），
        # 报 backend=iab——脚本侧传输层，不是 browser_backend 配置项。
        backend = "iab"
    needs_decision = (backend == "unconfigured")
    report = {
        "ok": False,
        "platform": _plat(),
        "backend": backend,
        "transport": t,
        "unattended": unattended(env),
        "needs_decision": needs_decision,
        "python": py,
        "bsk": {"ok": bool(bsk), "path": bsk, "version": bsk_version(bsk, runner, timeout),
                "doctor": None},
        "ego_browser": egoinfo or {"ok": False, "path": ego or "", "version": "",
                                   "anchor": upg_fmt(EGO_ANCHOR), "status": "unknown",
                                   "hint": ""},
        "install_command": install_command(),
        "actions": [],
    }
    if bsk and with_doctor:
        report["bsk"]["doctor"] = doctor(bsk, runner, timeout)
    acts = report["actions"]
    if not py["ok"]:
        acts.append("Python %s+ 不满足，换成更高版本的解释器：%s" % (py["required"], PY_HINT[_plat()]))
    if t == "egolite" and (not egoinfo or not egoinfo["path"]):
        acts.append("ego-browser 未安装：让用户从 %s 下载安装并在应用内完成 onboarding"
                    "（onboarding 时从日常登录目标平台的浏览器导入数据），装好后复检" % EGO_SITE)
    elif t == "egolite" and egoinfo and egoinfo["status"] == "older":
        acts.append("ego-browser %s 低于最低可用版本 %s：请用户执行 ego-browser upgrade，装完复检"
                    % (egoinfo["version"], egoinfo["anchor"]))
    elif t == "egolite" and egoinfo and egoinfo["status"] == "newer":
        # newer 只记录不拦（用户裁定）：放 notes，不影响 ok；评估结论由 agent 走 §F
        report.setdefault("notes", []).append(
            "ego-browser %s 新于实测锚点 %s：按 references/egolite-mode.md §F 对照特性清单评估；"
            "有破坏性变化→提示暂勿无人值守并记 lessons；无变化→无感使用并回填锚点"
            % (egoinfo["version"], egoinfo["anchor"]))
    if needs_decision:
        acts.append("浏览器通路未定（仅 bsk 传输会出现）：默认应改用 ego——"
                    "`python scripts/env.py --set-transport egolite` 后按 actions 安装 ego-browser；"
                    "只有用户明确不愿使用 ego 时，才问是否装 bsk（--set-backend bsk --install）"
                    "或用内置浏览器（--set-backend builtin）。")
    elif backend == "bsk" and not bsk:
        acts.append("bsk 未安装：跑 python scripts/env.py --install（官方脚本，用户级）；"
                    "或改用内置浏览器：--set-backend builtin")
    elif backend == "bsk" and bsk and report["bsk"]["doctor"] \
            and report["bsk"]["doctor"].get("connected") is False:
        acts.append("bsk 已装但扩展没连上：在浏览器启用 BrowserSkill 扩展"
                    "（Chrome %s / Edge %s），再跑 bsk doctor" % (EXT_CHROME, EXT_EDGE))
    report["ok"] = not acts and bool(py["ok"]) and (
        bool(bsk) or backend in ("builtin", "iab", "egolite"))
    return report


def ego_install_steps() -> list:
    """R2：egolite 传输下的安装引导（GUI 安装包，无脚本；必须人做的例外）。"""
    return [
        "① 下载：让用户打开 %s 下载 ego lite 安装包并运行（Windows 无脚本安装；"
        "SmartScreen 拦截时选「更多信息→仍要运行」）。" % EGO_SITE,
        "② 首次启动 onboarding（应用内 GUI，必须用户亲手做）：导入浏览器数据时"
        "**选择日常登录目标平台（DeepSeek/ChatGPT/Kimi/Gemini/豆包）的那个浏览器**"
        "作为导入源，确保登录态跟过来；onboarding 同时会把 ego-browser 命令注册进 PATH。",
        "③ 复检（开新终端，PATH 才生效）：ego-browser --version 能出版本号、"
        "ego-browser nodejs -e \"console.log('ego-browser ready')\" 打出 ready、"
        "python scripts/env.py --json 的 ego_browser.ok=true，三者齐了才算通。",
    ]


def install(timeout=300, runner=None, rep=None) -> dict:
    """egolite 传输：输出 ego 安装引导（人装，agent 引导）；其余传输：官方脚本装 bsk。

    不碰 Python——那是解释器，得用对的那个跑我。
    """
    if transport() == "egolite":
        steps = ego_install_steps()
        return {"ok": False, "mode": "ego-guide", "rc": None,
                "detail": "egolite 传输安装 ego lite（无脚本，需用户亲手完成 onboarding）：",
                "steps": steps}
    cmd = install_command()
    if not cmd:
        return {"ok": False, "rc": None, "detail": "不支持的平台：%s" % sys.platform}
    r = _run(cmd, timeout, runner)
    after = rep or check(with_doctor=False, runner=runner, timeout=25)
    ok = bool(after["bsk"]["ok"])
    ver = after["bsk"]["version"] or ""
    warn = ""
    # 版本钉（2026-11-09）：bsk 0.4.2 有已知劣化——浏览器执行任务时被频繁打断。
    # 官方安装脚本拉的是 main 最新版，没有版本参数，所以装完必须验版本：
    # ≥0.4.2 就如实警告，让用户决定是否降回 0.4.1，绝不静默装上已知劣化版。
    try:
        vt = tuple(int(x) for x in ver.split(".") if x != "")
    except ValueError:
        vt = ()
    if vt and vt >= (0, 4, 2):
        warn = ("⚠ 装到的是 bsk %s：0.4.2 起有已知劣化（执行任务时被频繁打断），"
                "当前策略固定 0.4.1——建议降级或回滚到 0.4.1。" % ver)
    # vendor 钉住版（2026-11-11）：星舵调用走仓库自带的 0.4.1，机器级安装只是
    # PATH 兜底与扩展通信宿主——装了劣化版也不改变星舵自己的行为。
    note = "星舵调用走 vendor 钉住版（bsk_vendor.vendor_bsk），机器级安装仅作 PATH 兜底。"
    return {"ok": ok, "rc": r["rc"], "path": after["bsk"]["path"],
            "version": ver, "warn": warn, "note": note,
            "tail": (r["out"] or r["err"] or "").strip().splitlines()[-6:],
            "doctor": (after["bsk"].get("doctor") or {}).get("connected")}


BACKEND_LABEL = {
    "egolite": "egolite（ego-browser，默认/首选；op 队列 + 后台 worker）",
    "bsk": "bsk（备用，计划移除；外部浏览器 + BrowserSkill 扩展）",
    "builtin": "builtin（agent 内置浏览器，手动规程：references/builtin-mode.md）",
    "iab": "iab（脚本侧 IAB 传输层，STARPILOT_TRANSPORT=iab，实验性）",
    "unconfigured": "未定（仅 bsk 传输且未安装时出现）",
}


def human(rep: dict) -> str:
    py, b = rep["python"], rep["bsk"]
    ego = rep.get("ego_browser") or {}
    mark = lambda ok: "✓" if ok else "✗"  # noqa: E731
    lines = [
        "%s Python %s（需 %s+）  %s" % (mark(py["ok"]), py["version"], py["required"], py["exe"]),
        "  浏览器通路：%s" % BACKEND_LABEL.get(rep.get("backend"), "?"),
    ]
    if rep.get("transport") == "egolite":
        lines.append("%s ego-browser %s  %s" % (
            mark(bool(ego.get("ok"))), ego.get("version") or "-", ego.get("path") or "未找到"))
        if b["ok"]:
            lines.append("  （bsk 也在装，但仅作备用，不参与当前传输）")
    else:
        lines.append("%s bsk %s  %s" % (mark(b["ok"]), b["version"] or "-", b["path"] or "未找到"))
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
    ap = argparse.ArgumentParser(description="星舵环境自检 / 依赖安装（ego 默认，bsk 备用）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（agent 用）")
    ap.add_argument("--install", action="store_true",
                    help="缺依赖时自动安装：egolite 输出 ego GUI 引导；bsk 走官方脚本（仅备用）")
    ap.add_argument("--no-doctor", action="store_true", help="跳过扩展连接探测")
    ap.add_argument("--backend", choices=BACKENDS,
                    help="本次按指定后端判定（不写配置；持久化用 --set-backend）")
    ap.add_argument("--set-backend", choices=BACKENDS, dest="set_backend",
                    help="记录旧后端选择（bsk=备用/计划移除；builtin=内置浏览器）；默认 ego 不需要它")
    ap.add_argument("--set-unattended", choices=["on", "off"], dest="set_unattended",
                    help="无人值守硬开关：on=执行模式缺省 auto、不再每轮停（写闸门仍 fail-closed）")
    ap.add_argument("--set-transport", choices=["bsk", "iab", "egolite"], dest="set_transport",
                    help="记录传输层到 ~/.starpilot/config.json（默认 egolite；bsk 仅备用）")
    ap.add_argument("--set-idle-close", type=int, default=0, dest="set_idle_close",
                    help="窗口闲置收割时限（秒，≥60）：agent 超时未续租，wa.py reap 关掉其窗口")
    ap.add_argument("--timeout", type=int, default=300, help="安装超时秒数")
    a = ap.parse_args(argv)

    if a.set_unattended:
        p = set_unattended(a.set_unattended == "on")
        print("已记录无人值守开关：%s → %s" % (a.set_unattended, p))
        if a.set_unattended == "on":
            print("效果：执行模式缺省 auto（不再每轮停下等确认）；写闸门仍需授权单，"
                  "开工前先 write_gate.py approve --scope task 一次授权。")
        return 0

    if a.set_idle_close:
        p = set_idle_close(a.set_idle_close)
        print("已记录窗口闲置收割时限：%d 秒 → %s" % (a.set_idle_close, p))
        print("收割器：python scripts/wa.py reap --watch（绑定后启动，后台常驻）")
        return 0

    if a.set_transport:
        p = config_path(os.environ)
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        cfg = load_config(os.environ)
        cfg["transport"] = a.set_transport
        with io.open(p, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print("已记录传输层：%s → %s" % (a.set_transport, p))
        if a.set_transport == "egolite":
            print("效果：页面动作走 op 队列（默认通路），worker 由 ego-browser 驱动，"
                  "规程见 references/egolite-mode.md；首次使用需在 ego-browser 里登录目标平台。")
        elif a.set_transport == "bsk":
            print("⚠ bsk 是备用通路，计划在后续版本移除；仅当用户明确不愿使用 ego 时才切。"
                  "效果：页面动作直连 bsk CLI，需要 bsk + BrowserSkill 扩展。")
        elif a.set_transport == "iab":
            print("效果：页面动作走 op 队列（实验性），worker 由 ZCode IAB 驱动，见 SKILL §5.1。")
        return 0

    if a.set_backend:
        p = set_backend(a.set_backend)
        print("已记录浏览器后端：%s → %s" % (a.set_backend, p))
        if a.set_backend == "bsk":
            print("⚠ bsk 是备用通路（计划移除），仅在用户明确不愿使用 ego 时使用。")
            print("下一步：python scripts/env.py --install（装 bsk CLI），并让用户在浏览器启用扩展。")
        else:
            print("不装 bsk。浏览器操作由 agent 内置能力执行，规程见 references/builtin-mode.md。")
        return 0

    rep = check(with_doctor=not a.no_doctor, backend=a.backend)
    inst = None
    need_install = (a.install and not rep["bsk"]["ok"]) or \
        (a.install and rep["transport"] == "egolite" and not rep["ego_browser"]["ok"])
    if need_install:
        inst = install(timeout=a.timeout, rep=None)
        if inst.get("mode") != "ego-guide":
            rep = check(with_doctor=not a.no_doctor)

    if a.json:
        out = dict(rep)
        if inst is not None:
            out["install"] = inst
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(human(rep))
        if inst is not None:
            if inst.get("mode") == "ego-guide":
                print("\n" + inst["detail"])
                for line in inst.get("steps") or []:
                    print("  " + line)
            else:
                print("\n安装结果：%s  rc=%s" % ("成功" if inst["ok"] else "失败", inst["rc"]))
                if inst.get("note"):
                    print("  " + inst["note"])
                if inst.get("warn"):
                    print("  " + inst["warn"])  # 版本钉：装上 0.4.2+ 要当场喊出来，不许静默
                for line in inst.get("tail") or []:
                    print("  " + line)

    if rep["ok"]:
        return 0
    if inst is not None and inst.get("mode") != "ego-guide" and not rep["bsk"]["ok"]:
        return 5
    return 4


if __name__ == "__main__":
    sys.exit(main())
