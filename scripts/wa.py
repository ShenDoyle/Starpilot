#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wa.py —— 网页版 AI 循环的桥接命令（配合 starpilot skill 使用）。

只做「确定性动作」：绑定、探测、写入、发送、等待、读取。
不做任何内容判断（"这段回复靠不靠谱" 由调用它的 agent 负责）。

平台差异（输入框、发送按钮、绑定地址形态）全部收在 platforms.py，
本文件不写任何平台专属选择器。

用法：
    wa.py platforms                         # 列出支持的平台
    wa.py bind <url> [--platform chatgpt]
    wa.py probe [--platform chatgpt]
    wa.py send --file <path> | --text "..." [--platform chatgpt]
    wa.py wait [--timeout 900] [--interval 3] [--platform chatgpt]
    wa.py read [--out <path>] [--platform chatgpt]
    wa.py state
    wa.py stop
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import confirm  # noqa: E402
import platforms  # noqa: E402
import safeprint  # noqa: E402
import statelock  # noqa: E402
import tabclaims  # noqa: E402
import winlease  # noqa: E402

# 导入即加固输出流：本文件的每一句实话（拒绝原因、判定、triage 结论）都打在
# stdout/stderr 上，zh-CN Windows 控制台默认 cp936，遇到 GBK 编不进去的字符
# （页面原文里的 U+FFFD、平台文案、emoji）会直接 UnicodeEncodeError ——
# 判定死在打印那一行，比打印难看严重得多。详见 scripts/safeprint.py。
safeprint.harden_streams()

# ---------------------------------------------------------------- bsk 定位

_CANDIDATES = [
    os.environ.get("BSK_BIN") or "",
    # L2 shim（2026-11-02）：装了 shim 时真身改名为 bsk.bin.exe，本脚本直连真身
    # （自己已有租约+全局计数，不必过 shim；也避免所有调用多一层 cmd.exe 开销）。
    os.path.expanduser("~/.local/bin/bsk.bin.exe"),
    shutil.which("bsk"),
    os.path.expanduser("~/.local/bin/bsk.exe"),
    os.path.expanduser("~/.local/bin/bsk"),
]


def bsk_path() -> str:
    for c in _CANDIDATES:
        if c and os.path.exists(c):
            return c
    raise SystemExit(
        "找不到 bsk（星舵的核心依赖）。别猜，按顺序做：\n"
        "  1) python scripts/env.py --json    # 看 Python 版本、bsk 是否已装\n"
        "  2) python scripts/env.py --install # 缺就用官方脚本装（用户级，不需要 root）\n"
        "  3) 在浏览器启用 BrowserSkill 扩展，bsk doctor 显示 Connected 才算通\n"
        "  4) 装到了非默认目录，设 BSK_BIN 指向可执行文件\n"
        "都试过还不行，再回来找用户。"
    )


def _transport_is_iab() -> bool:
    try:
        import transport
        return transport.current_transport() == "iab"
    except Exception:
        return False


# 传输层为 iab 时不要求 bsk（自带浏览器驱动）；bsk 传输才需要可执行文件
BSK = "" if _transport_is_iab() else bsk_path()

# L2 shim 一次性票据的环境变量名（值由 winlease.gate_issue 生成，兑换即失效）。
# 旧的 `STARPILOT_WINDOW_GATE=1` 已废弃——布尔量谁都能设，等于没有门（复审裁定）。
GATE_TICKET_ENV = "STARPILOT_WINDOW_TICKET"


def bsk(args: list, timeout: int = 120, check: bool = False) -> tuple[int, str, str]:
    """底层 bsk 调用统一入口。

    **传输层抽象（0.1.5）**：STARPILOT_TRANSPORT=iab 时页面动作改走 IAB op 队列
    （scripts/transport.py + scripts/iab-worker.mjs，驱动 ZCode 自带浏览器，
    不需要 bsk）；默认 bsk 传输行为与 0.1.4 完全一致（cooperate 上下文由
    bskcompat 自动补齐）。
    """
    import transport
    if transport.current_transport() == "iab":
        action, params = transport.parse_bsk_args(args)
        if action is None:
            return 0, "", ""   # iab 传输下无对应的本地命令（doctor/browsers 等）按健康空值处理
        if action == "noop":
            return 0, '{"id": "iab", "browser": "iab"}', ""
        return transport.call(action, params, timeout=timeout)
    import bskcompat
    bskcompat.set_bin(BSK)
    rc, out, err = bskcompat.run(_bsk_raw, args, timeout)
    if check and rc != 0:
        raise SystemExit("bsk 失败 rc=%s\n%s" % (rc, (err or out)[:600]))
    return rc, out, err


class BskQueryError(RuntimeError):
    """bsk 查询**没拿到可信答案**（非零退出、或输出里解析不出 JSON）。

    这个异常存在的唯一理由：把「查不到」和「查到的是空」分开。
    两者混在一起时，上层会把「看不清」当成「没有了」处理 —— 于是重开窗口、
    重发消息、报「浏览器未连接」，本来没坏的东西被修坏了。
    """


def parse_bsk_json(out: str) -> tuple[bool, object]:
    """从 bsk 的输出里取**第一个平衡的 JSON 值**，返回 (是否拿到, 值)。

    为什么要平衡扫描而不是 `json.loads(整段)`：bsk 偶尔会在 JSON 前后带一行
    提示（daemon 自动拉起、版本 skew 警告、cooperate 同步说明）。整段 loads
    必然失败，而失败在旧代码里被当成「没有会话 / 没有浏览器」——静默的错误结论。
    从每个 `{`/`[` 起点试一次平衡切片，能容忍噪声行，也能容忍尾巴上的日志。
    """
    text = (out or "").strip()
    if not text:
        return False, None
    try:                                    # 快路：干净的一整份 JSON
        return True, json.loads(text)
    except Exception:
        pass
    starts = sorted({i for i, ch in enumerate(text) if ch in "{["})
    for i in starts:
        stack = []                      # 期望的闭括号**栈**，不是单个变量：
        in_str = False                  # 用单变量时 {"a":[{}]} 收到 } 之后
        esc = False                     # 就不知道下一个该是 ] 了（本函数第一版
        for j in range(i, len(text)):   # 就栽过：banner 版输出只能解析到内层，
            ch = text[j]                # 拿回来的根本不是那个对象）
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch in "{[":
                stack.append("}" if ch == "{" else "]")
            elif ch in "}]":
                if not stack or ch != stack[-1]:
                    break
                stack.pop()
                if not stack:
                    try:
                        return True, json.loads(text[i:j + 1])
                    except Exception:
                        break
                continue
    return False, None


def _bsk_raw(args: list, timeout: int) -> tuple[int, str, str]:
    env = None
    # L2 shim 通行票据（2026-11-02 用户指令「只要使用 bsk 必须遵守窗口规则」；
    # 同日复审裁定：原 `STARPILOT_WINDOW_GATE=1` 是可伪造的布尔量，任何人 set
    # 一下就自称「内部正路」——改成一次性 HMAC 票据，只能由本入口签发、
    # shim 兑换后即焚。只有 session start 需要票据；其余子命令不加，减少 env 泄面。
    ticket_note = ""
    if len(args) >= 2 and args[0] == "session" and args[1] == "start":
        try:
            t = winlease.gate_issue()
            env = dict(os.environ)
            env[GATE_TICKET_ENV] = t["ticket"]
        except Exception as e:
            # 签不出票 = shim 会拒绝。这里不静默降级（那等于绕过门），
            # 但也不假装成功：把原因带上，让失败可诊断。
            ticket_note = "签发票据失败：%s" % (str(e)[:120])
            sys.stderr.write("BSK-GATE: %s（L2 shim 将拒绝本次 session start）\n"
                             % ticket_note)
    try:
        p = subprocess.run([BSK] + args, capture_output=True, text=True,
                           encoding="utf-8", errors="backslashreplace",
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return 124, "", "bsk 超时（%ss）：%s" % (timeout, " ".join(args[:2]))
    return p.returncode, p.stdout or "", p.stderr or ""


# ---------------------------------------------------------------- state

STATE_NAME = ".wa-state.json"

# ---------------- 平台占用锁（跨项目 / 跨任务互斥） ----------------
# 场景：多个任务同时用星舵、且都打同一个网页 AI 平台（同账号同浏览器）→
# 抢同一个 tab、对话互相打架、触发平台风控（2026-10-29 实测：ChatGPT 回复
# 加载不出，另一任务误判「对话出问题」自动跳转）。
# 解法：**同一平台同一浏览器环境上的写操作（bind/send/clear）必须先拿到
# 平台占用锁**；拿不到立即退出并报持有者，不排队死等。锁文件放在用户级
# 目录（跨项目可见），按 <平台>-<隔离键> 区分。
LOCK_DIR_ENV = "STARPILOT_LOCK_DIR"    # 锁目录（默认 ~/.starpilot/locks）
LOCK_KEY_ENV = "STARPILOT_LOCK_KEY"    # 隔离键：不同浏览器 profile/账号用不同 key
LOCK_WAIT_ENV = "STARPILOT_LOCK_WAIT"  # 等锁秒数，默认 0（立即失败，不排队）
PLATFORM_LOCK_STALE = 1800.0           # 持有超 30 分钟视为残留锁，可接管


def platform_lock_path(platform: str) -> str:
    d = os.environ.get(LOCK_DIR_ENV) or os.path.join(
        os.path.expanduser("~"), ".starpilot", "locks")
    key = os.environ.get(LOCK_KEY_ENV) or "default"
    return os.path.join(d, "lock-%s-%s.json" % (platform, key))


def lock_platform(platform: str) -> dict:
    """取平台占用锁；拿不到抛 statelock.LockBusy（异常里带持有者信息）。

    允许同进程重入：cmd_send 装饰器持锁后，内部的 do_send 会再取一次。
    """
    return statelock.acquire_named(
        platform_lock_path(platform), owner="wa",
        run_id=os.path.basename(os.getcwd()) or "wa",
        timeout=float(os.environ.get(LOCK_WAIT_ENV) or 0),
        stale_after=PLATFORM_LOCK_STALE,
        resource=platform_lock_path(platform),
        allow_reentry=True)


def _resolve_platform_key(a) -> str:
    """从命令行参数或 state 解析平台 key；解析不了抛异常（让原命令自己报错）。"""
    key = getattr(a, "platform", None)
    if key:
        return platforms.get(key)["key"]
    st = load_state(state_path(getattr(a, "state", None)))
    return platforms.get(st.get("platform"))["key"]


def with_platform_lock(fn):
    """装饰器：bind/send/clear 这类「要动浏览器」的命令先拿平台锁。

    平台解析失败不拦（让原命令自己给出准确的报错）；拿到锁后无论命令
    返回什么、抛不抛异常都在 finally 释放。wait/read/probe 只读不导航，
    不需要锁——读别的对话不会打架。
    """
    @functools.wraps(fn)
    def wrapper(a):
        try:
            key = _resolve_platform_key(a)
        except Exception:
            return fn(a)
        try:
            lock = lock_platform(key)
        except statelock.LockBusy as e:
            print("平台 %s 正被另一任务占用，已拒绝执行（不排队死等）。\n"
                  "锁文件：%s\n持有者：%s\n"
                  "处理：等对方任务收口后再跑；并行任务错开平台或浏览器 profile"
                  "（设 STARPILOT_LOCK_KEY）；确认是残留锁强拆用 wa.py plock --release。"
                  % (key, e.path, json.dumps(e.holder, ensure_ascii=False)),
                  file=sys.stderr)
            return 9
        try:
            return fn(a)
        finally:
            statelock.release_named(platform_lock_path(key), lock)
    return wrapper


def state_path(explicit: str | None = None) -> str:
    return explicit or os.path.join(os.getcwd(), STATE_NAME)


def _same_origin(a: str, b: str) -> bool:
    """两个地址是否同源（协议 + 主机 + 端口）。用于校验 --new 入口没被换成别的站。"""
    try:
        pa, pb = urlparse(a.strip()), urlparse(b.strip())
    except Exception:
        return False
    return (pa.scheme, pa.netloc) == (pb.scheme, pb.netloc)


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {"url": "", "session": "", "turn": 0, "last_send_hash": "",
                "awaiting_reply": False, "fail_streak": 0, "history": [],
                "platform": platforms.DEFAULT_PLATFORM}
    with open(path, encoding="utf-8") as f:
        st = json.load(f)
    st.setdefault("platform", platforms.DEFAULT_PLATFORM)
    return st


def save_state(path: str, st: dict, timeout: float = 10.0) -> None:
    """写 state 必须持锁（F3）。

    读不加锁、写必加锁：两个 run 同时写会让 `url` 被静默覆盖 ——
    那等于硬规则 1「同一对话」被无声破坏，你只会看到「AI 好像忘了上下文」。
    """
    try:
        with statelock.state_lock(path, owner="wa",
                                  run_id=str(st.get("run_id") or ""),
                                  timeout=timeout):
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                json.dump(st, f, ensure_ascii=False, indent=2)
    except statelock.LockBusy as e:
        raise SystemExit(
            "state 正在被另一个进程写：%s\n"
            "确认没有别的 run 在跑；崩溃残留可用 `wa.py unlock` 强制解锁。" % e)


# ---------------------------------------------------------------- session

_SID_RE = re.compile(r"\b([a-z0-9]{4})\b")
# 兜底专用的**带上下文**正则：bsk 的会话 id 是 4 位小写字母，但裸的
# `\b[a-z0-9]{4}\b` 会把 `edge`、`true`、路径片段都当成候选 —— 拿错 id 就是
# 往别人的会话里打字。要求前面贴着 "session" 才算，宁可拿不到（报错）也不猜。
_SID_RE2 = re.compile(r"session[^A-Za-z0-9]{0,4}([a-z0-9]{4})\b")


def _norm_session(s: dict) -> dict:
    """bsk 的会话记录 → 本文件统一口径（id / window / browser）。"""
    s = dict(s)
    # bsk 0.3.x 的键是 session_id / agent_window_id，统一别名
    s.setdefault("id", s.get("session_id") or s.get("session") or "")
    s.setdefault("window", s.get("agent_window_id") or "")
    # 实例 id 的键是 browser_instance_id。少了这一行，enforce_single_session
    # 的 `s["browser"] != browser` 永远成立 → **一个旧会话都收不掉**，
    # 每次重绑都留一个窗口（2026-10-30 豆包机上「开很多 tab」的直接原因）。
    s["browser"] = s.get("browser") or s.get("browser_instance_id") or ""
    return s


def list_sessions(strict: bool = False) -> list:
    """`bsk session list --json` → 规范化的会话列表。

    ⚠️ 「查不到」与「查到的是空」是两件事，以前这里把它们压成同一个 `[]`：
    bsk 在 JSON 前多打一行 banner、或 daemon 瞬断、或输出被截断，都会解析失败
    → 返回空 → 上层判「会话不存在」→ **重开一个新窗口**。用户的感受就是
    「页面故障越来越多、窗口一个接一个」，而根本没有故障，是自己吓自己。
    `strict=True` 时解析失败/非零退出会抛 BskQueryError，由调用方如实分成
    alive / dead / unknown 三态（见 session_state）。
    """
    if _transport_is_iab():
        return [{"id": "iab", "browser": "iab", "window": ""}]
    rc, out, _ = bsk(["session", "list", "--json"])
    if rc != 0:
        if strict:
            raise BskQueryError("session list rc=%s" % rc)
        return []
    ok, data = parse_bsk_json(out)
    if not ok:
        if strict:
            raise BskQueryError("session list 输出解析失败：%r" % (out[:160],))
        return []
    if isinstance(data, dict):
        data = data.get("sessions") or data.get("value") or []
    return [_norm_session(s) for s in data if isinstance(s, dict)]


# Agent Window 默认落点：副屏左上角（主屏 2560 宽，副屏在右侧）。
# 可用环境变量 WA_WINDOW_X / WA_WINDOW_Y 覆盖；设为 -1 表示不动窗口。
WA_WINDOW_X = int(os.environ.get("WA_WINDOW_X", "2560"))
WA_WINDOW_Y = int(os.environ.get("WA_WINDOW_Y", "0"))


def place_window(sid: str) -> None:
    """把 Agent Window 挪到副屏，不干扰主屏工作。失败静默（不影响主流程）。"""
    if WA_WINDOW_X < 0:
        return
    try:
        bsk(["evaluate", "window.moveTo(%d,%d); JSON.stringify({x:window.screenX})" % (WA_WINDOW_X, WA_WINDOW_Y),
             "--session", sid], timeout=30)
    except Exception:
        pass


def list_browsers(strict: bool = False) -> list:
    """`bsk browsers --json` → 规范化的实例列表。

    ⚠️ 默认返回空列表**只表示「没查到」**，不表示「一个浏览器都没连」。
    这两种情况以前压在一起，于是「daemon 抖了一下 / JSON 前多打一行提示」
    被当成「扩展没连」，上层据此重开窗口、重绑实例 —— 用户看到的是
    「网页故障 + 窗口越开越多」，而故障是自查过程自己造的。
    要做决策的调用方传 `strict=True`：拿不到可信答案就抛 BskQueryError。

    只用 JSON：`browsers` 的表格列宽是按标签长度对齐的，靠 split 解析会把
    「0.3.3 京东老浦家」整段当成标签，`WA_BROWSER_PREFER` 就永远匹配不上。
    """
    rc, out, _ = bsk(["browsers", "--json"], timeout=30)
    if rc != 0:
        if strict:
            raise BskQueryError("browsers rc=%s" % rc)
        return []
    ok, data = parse_bsk_json(out)
    if not ok:
        if strict:
            raise BskQueryError("browsers 输出解析失败：%r" % (out[:160],))
        return []
    if isinstance(data, dict):
        data = data.get("browsers") or []
    res = []
    for b in data:
        if not isinstance(b, dict):
            continue
        res.append({
            "id": b.get("instance_id") or b.get("id") or "",
            "name": b.get("browser_name") or b.get("browser") or "",
            "label": (b.get("label") or "").strip(),
            "ext": b.get("extension_version") or "",
            "sessions": int(b.get("session_count") or 0),
            "unresponsive": bool(b.get("unresponsive")),
            "skew": bool(b.get("version_skew")),
        })
    return [b for b in res if b["id"]]


def instance_report(browsers: list | None = None) -> dict:
    """在线实例体检：有没有重复安装、失联、能不能唯一确定目标。

    为什么必须有：同一个 Chrome 里装 4 份 BrowserSkill（工具栏并排 4 个图标），
    bsk 就报 4 个实例。会话跟着某个实例走，读回却可能落到另一个实例的窗口上 ——
    表现正是「发得出去、永远读不到回复」，而每次重试又开一个新窗口。
    这种事不能「就近挑一个」：挑错等于拿 A 窗口的页面去等 B 窗口的回复。

    判据是实例自报的 `label` + 浏览器名（都是 bsk 自己的事实，不靠猜）：
    同 label 出现多次 = 同一浏览器装了多份。label 为空的多个实例只算「无法区分」，
    不当成重复 —— 那可能只是两个没起名字的 profile。
    """
    if _transport_is_iab():
        # IAB 由宿主托管：无扩展/实例概念，直接健康
        return {"count": 1, "duplicates": [], "unresponsive": [], "unique": True,
                "healthy": True,
                "instances": [{"id": "iab", "name": "iab", "label": "IAB",
                               "ext": "", "sessions": 0, "unresponsive": False,
                               "skew": False}]}
    bs = list_browsers() if browsers is None else browsers
    groups = {}
    for b in bs:
        groups.setdefault((b.get("name") or "", b.get("label") or ""), []).append(b)
    duplicates = [
        {"browser": k[0], "label": k[1], "ids": [x["id"] for x in v], "count": len(v)}
        for k, v in groups.items() if len(v) > 1 and k[1]
    ]
    return {
        "count": len(bs),
        "instances": bs,
        "duplicates": duplicates,
        "unresponsive": [b["id"] for b in bs if b.get("unresponsive")],
        "unique": len(bs) == 1,
        "healthy": not duplicates,
    }


def duplicate_hint(rep: dict) -> str:
    lines = ["  · %s「%s」在线 %d 次：%s"
             % (d["browser"] or "浏览器", d["label"], d["count"], " ".join(d["ids"]))
             for d in rep.get("duplicates") or []]
    return (
        "检测到同一个浏览器里装了多份 BrowserSkill 扩展：\n%s\n"
        "后果：会话落在实例 A，读回却去查实例 B —— 「发得出去但永远读不到回复」，"
        "重试还会不断开新窗口。\n"
        "处理：打开 chrome://extensions（Edge 是 edge://extensions），把重复的 "
        "BrowserSkill 全部移除，只留一个；再跑 `python scripts/wa.py doctor` 确认只剩一行。\n"
        "确实需要多份共存（多 profile 各装一份）就给每个实例起不同标签；"
        "临时绕过可设 WA_ALLOW_DUPLICATE_EXT=1，但读回错乱要自己承担。"
        % "\n".join(lines))


def _first_browser_instance(browsers: list | None = None) -> str:
    """多浏览器在线时猜一个实例 id。

    偏好标签用环境变量 `WA_BROWSER_PREFER` 指定（逗号分隔的片段，按顺序匹配标签、
    浏览器名或实例 id）。只有一个实例时直接用它；**多个实例且无法判断时返回空**——
    由调用方报错让人选，绝不「就近替代」（那等于拿 A 账号的登录态去操作 B 账号）。
    """
    bs = list_browsers() if browsers is None else browsers
    bs = [b for b in bs if not b.get("unresponsive")]
    if not bs:
        return ""
    prefer = [s.strip() for s in os.environ.get("WA_BROWSER_PREFER", "").split(",") if s.strip()]
    for want in prefer:
        hits = [b["id"] for b in bs
                if want == b["id"] or want in (b.get("label") or "") or want in (b.get("name") or "")]
        if len(hits) == 1:
            return hits[0]
    return bs[0]["id"] if len(bs) == 1 else ""


def session_state(sid: str) -> str:
    """会话的三态：`alive` / `dead` / `unknown`。

    为什么必须三态而不是 bool（2026-11-03）：以前 `list_sessions()` 解析失败会
    返回空列表，`session_alive()` 于是说「不在线」，等待循环就去 **renew 重开窗口**。
    查不清 ≠ 没了。把「查不清」当成「没了」去动作，制造的是真故障（多余窗口、
    多余 tab），而用户看到的是「网页各种故障」。
    """
    if not sid:
        return "unknown"
    try:
        sessions = list_sessions(strict=True)
    except BskQueryError:
        return "unknown"
    return "alive" if any((s.get("id") or "") == sid for s in sessions) else "dead"


def session_alive(sid: str) -> bool:
    """兼容旧调用：**不确定时按「还在线」处理**，绝不因为看不清就去重开窗口。

    这不是把未知当成功，而是把未知当「别动作」——真要判死由 session_state()
    的 `dead` 给出，调用方想区分请用三态版本。旧调用点（read 的收尾判断）
    在「看不清」时最不该做的事就是再开一扇窗。
    """
    return session_state(sid) != "dead"


# 开窗纪律：一次任务里短时间反复 `session start` 不是「重试」，是环境在反复失效。
# 超过这个密度就停下来报现场，而不是继续堆窗口。
WA_START_WINDOW_S = 600
WA_START_MAX = 4


def _note_session_start(st: dict) -> None:
    now = time.time()
    hist = [t for t in (st.get("session_start_at") or []) if now - float(t) < WA_START_WINDOW_S]
    hist.append(now)
    st["session_start_at"] = hist


def _too_many_starts(st: dict) -> bool:
    now = time.time()
    hist = [t for t in (st.get("session_start_at") or []) if now - float(t) < WA_START_WINDOW_S]
    return len(hist) >= WA_START_MAX


# ---------------------------------------------- 窗口租约与自动收割（0.1.6 未发）
#
# 痛点：agent 中途被杀 / 任务中断后，Agent Window 一直挂着没人收。
# 机制：每条浏览器命令都「续租」（lease_until = now + idle_close_s）；
#       收割器（wa.py reap）发现租约过期就 session stop（连同 Agent Window）。
# agent 只要还在干活就永远杀不到它；一旦断联，最多 idle_close_s 后窗口被收。
DEFAULT_IDLE_CLOSE_S = 900


def idle_close_s() -> int:
    """收割时限：config idle_close_s > env WA_IDLE_CLOSE_S > 900。下限 60s。"""
    try:
        import env as _env
        v = _env.load_config(os.environ).get("idle_close_s")
        if isinstance(v, (int, float)) and int(v) >= 60:
            return int(v)
    except Exception:
        pass
    try:
        return max(60, int(os.environ.get("WA_IDLE_CLOSE_S") or DEFAULT_IDLE_CLOSE_S))
    except Exception:
        return DEFAULT_IDLE_CLOSE_S


def touch_lease(st: dict, path: str) -> None:
    now = time.time()
    st["lease_until"] = now + idle_close_s()
    st["lease_at"] = now
    save_state(path, st)


def lease_left(st: dict) -> float:
    try:
        return float(st.get("lease_until") or 0) - time.time()
    except Exception:
        return 0.0


def cmd_reap(a) -> int:
    """收割闲置窗口：租约过期的 state → session stop（Agent Window 一并关闭）。

    用法：
      wa.py reap --once              # 扫一遍（缺省单次）
      wa.py reap --watch             # 常驻循环（绑定后启动，后台任务）
      wa.py reap --scan <工作区根>   # 扫目录下所有 *.wa-state.json（多任务）
      wa.py reap --dry-run           # 只报告不动手
    """
    if _transport_is_iab():
        print("IAB 传输无窗口生命周期，无需收割。")
        return 0
    ttl = int(getattr(a, "ttl", 0) or 0) or idle_close_s()
    interval = max(10, int(getattr(a, "interval", 0) or 60))
    dry = bool(getattr(a, "dry_run", False))
    watch = bool(getattr(a, "watch", False))

    def _targets():
        if getattr(a, "scan", ""):
            found = []
            for root, dirs, files in os.walk(a.scan):
                if STATE_NAME in files:
                    found.append(os.path.join(root, STATE_NAME))
            return found
        return [state_path(getattr(a, "state", None))]

    def _global_audit() -> dict:
        """L1 全局看门狗（2026-11-02 用户指令）：不依赖任何任务的 state 文件。

        直接问 bsk 要全部活会话，对照各实例 journal 的组登记：
          · 组内声明 + 租约过期（stale）→ 收割（对方任务已死，会话成残留）
          · 组内声明 + 租约活 → 跳过（对方正在干活）
          · 无登记（其他 skill / 人工开的）→ **只报告，绝不动手**——本工具的
            清理边界止于自己组的名下，别组的会话死活不归它管。
        另做标签审计：活会话里同地址重复标签 >1 → 报告（不动手，标签属于活会话）。
        """
        rep = {"reaped": [], "skipped_live": [], "unclaimed": [],
               "dup_tabs": [], "dry_run": dry}
        try:
            sessions = list_sessions()
        except Exception as e:
            rep["error"] = "list_sessions 失败：%s" % e
            return rep
        # journal 登记总表：sid -> (instance, live_lease)
        claims = {}
        for st_info in winlease.list_all():
            inst = st_info.get("instance") or "default"
            for sid, meta in winlease.journal_claims(inst).items():
                claims.setdefault(sid, (inst, bool(meta.get("live_lease"))))
        for s in sessions:
            sid = s.get("id") or s.get("session") or ""
            if not sid:
                continue
            inst, live = claims.get(sid, ("", False))
            if not inst:
                rep["unclaimed"].append({"sid": sid,
                                         "browser": s.get("browser") or ""})
                continue
            if live:
                rep["skipped_live"].append(sid)
                continue
            if dry:
                rep["reaped"].append({"sid": sid, "instance": inst,
                                      "note": "DRY-RUN：租约过期，将收割"})
                continue
            rc, out, err = bsk(["session", "stop", sid], timeout=60)
            winlease.drop_session(inst, sid,
                                  winlease.make_owner(workspace=os.getcwd(),
                                                      note="reap --global"),
                                  reason="reap --global 租约过期回收")
            rep["reaped"].append({"sid": sid, "instance": inst,
                                  "rc": rc, "err": (err or out).strip()[:120]})
        # 标签审计：只对「活会话」报告重复（同地址 >1）
        for sid in rep["skipped_live"]:
            try:
                rc, out, err = bsk(["tab", "list", "--session", sid, "--json"],
                                   timeout=30)
                if rc:
                    continue
                tabs = json.loads(out or "{}").get("tabs") or []
                seen = {}
                for t in tabs:
                    u = _norm_url(t.get("url") or "")
                    if not u or t.get("scope") == "user":
                        continue
                    seen[u] = seen.get(u, 0) + 1
                for u, n in seen.items():
                    if n > 1:
                        rep["dup_tabs"].append({"sid": sid, "url": u[:80], "n": n})
            except Exception:
                continue
        return rep

    def _sweep() -> int:
        reaped = 0
        for p in _targets():
            if not os.path.exists(p):
                continue
            st = load_state(p)
            sid = st.get("session") or ""
            if not sid:
                continue
            left = lease_left(st)
            if not st.get("lease_until"):
                print("跳过（无租约记录，不敢乱杀）：%s" % p)
                continue
            if left > 0:
                continue
            if dry:
                print("DRY-RUN 将收割（租约已过期 %ds）：%s session=%s"
                      % (int(-left), p, sid))
                reaped += 1
                continue
            # 双保险：会话若被另一个活任务的窗口租约声明，绝不收割
            _own = _win_owner(st, p)
            _inst = st.get("window_instance") or st.get("browser") or "default"
            _ok, _why = winlease.may_stop_session(_inst, sid, _own)
            if not _ok:
                print("跳过收割：%s 的会话 %s %s" % (p, sid, _why))
                continue
            rc, out, err = bsk(["session", "stop", sid], timeout=60)
            now = time.time()
            st["session"] = ""
            st["reaped_at"] = now
            st.pop("lease_until", None)
            save_state(p, st)
            winlease.drop_session(_inst, sid, _own, reason="reap 闲置回收")
            winlease.release(_inst, _own, reason="reap 闲置回收")
            reaped += 1
            print("已收割：%s session=%s（rc=%d %s）"
                  % (p, sid, rc, (err or out).strip()[:120]))
        return reaped

    if getattr(a, "global_scan", False) and not watch:
        rep = _global_audit()
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0
    if not watch:
        n = _sweep()
        print("收割完成：%d 个%s" % (n, "（dry-run）" if dry else ""))
        return 0
    print("收割器已启动：ttl=%ds interval=%ds targets=%s（Ctrl+C 退出）"
          % (ttl, interval, a.scan or state_path(getattr(a, "state", None))))
    while True:
        try:
            _sweep()
        except KeyboardInterrupt:
            return 0
        except Exception as e:  # 收割器自己不能死：出错记下来继续转
            print("收割器单轮异常（继续）：%s" % e, file=sys.stderr)
        time.sleep(interval)
    return 0


# ---------------------------------------------- 窗口归属校验（0.1.6 未发）
#
# 痛点：页面开到了**用户正在用的窗口**（cooperate 共享 / 扩展旧版 / Automation 设置
# 为共享模式），而不是本会话的 Agent Window。
# 依据：bsk ≥0.4.0 `tab list --scope agent|user` 能机器区分两者。
def tab_scope_report(sid: str) -> dict:
    def _urls(scope):
        rc, out, err = bsk(["tab", "list", "--session", sid,
                            "--scope", scope, "--json"], timeout=60)
        if rc != 0:
            return None  # 探不到就不编，None = 未知
        try:
            data = json.loads(out or "[]")
        except Exception:
            return None
        if isinstance(data, dict):          # bsk 实际返回 {"tabs":[...]}（2026-11-02 实测）
            data = data.get("tabs") or []
        if isinstance(data, list):
            return [t.get("url") or "" for t in data if isinstance(t, dict)]
        return None
    return {"agent": _urls("agent"), "user": _urls("user")}


def window_report(st: dict, sid: str) -> dict:
    """绑定地址落在哪个 scope：agent=正常；user=开到用户窗口，要自愈。"""
    rep = tab_scope_report(sid)
    bound = (st.get("url") or "").split("?")[0][:60]

    def _has(urls):
        if bound and isinstance(urls, list):
            return any((u or "").split("?")[0][:60].startswith(bound) for u in urls)
        return None  # None = 探不到

    out = dict(rep)
    out["bound_in_agent"] = _has(rep.get("agent"))
    out["bound_in_user"] = _has(rep.get("user"))
    if out["bound_in_agent"] is None and out["bound_in_user"] is None:
        out["ok"] = None  # 探不到 = 未知，不误报
    else:
        out["ok"] = bool(out["bound_in_agent"]) and not bool(out["bound_in_user"])
    return out


BLANK_URLS = ("about:blank", "chrome://newtab/", "edge://newtab/", "")


def _agent_tabs(sid: str, timeout: int = 30) -> list:
    """本会话可见的标签列表；失败返回 []（调用方自行兜底，不在这里判死）。"""
    try:
        rc, out, err = bsk(["tab", "list", "--session", sid, "--json"],
                           timeout=timeout)
        if rc:
            return []
        return json.loads(out or "{}").get("tabs") or []
    except Exception:
        return []


def _find_blank_tab(sid: str) -> str:
    """agent 窗口里的空白标签（历史 tab create 的残留）。返回 tab_id 或 ""。"""
    for t in _agent_tabs(sid):
        if t.get("scope") != "user" and (t.get("url") or "").strip() in BLANK_URLS:
            return str(t.get("tab_id") or "")
    return ""


def reap_own_tabs(sid: str, bound: str = "", dry: bool = True,
                  owner_key: str = "", run_id: str = "") -> dict:
    """列出/关闭**当前任务明确拥有**的陈旧标签（2026-11-02 Web AI 裁定后重写）。

    bsk 的 `tab list` 不提供标签归属（只有 tab_id/window_id/active/scope），
    `scope=agent` 只说明「在 Agent Window 里」，说明不了「谁开的」。所以回收
    判据一律来自本地账本 `tabclaims`（SQLite 权威 + JSONL 审计），不看 URL 猜。

    可关的硬条件（全部满足）：
      · 账本里该 tab 的 owner_key（+run_id）＝当前任务；
      · 状态 claimed、未标 must_reinspect、不是 borrowed（借用标签永不自动关）；
      · 不是活动标签；
      · 属于「明确空白残留」或「同任务同 URL 的第 2+ 个副本」。

    其余一律只报告不关闭——裁定原话：「宁可暂时积累标签，也不能误关正在工作的
    标签」。**默认 dry=True**，要真关必须显式 apply。
    """
    rep = {"closed": [], "report_only": [], "close_failed": [], "user_scope": 0,
           "dry": dry, "session": sid}
    tabs = _agent_tabs(sid)
    if not tabs:
        rep["error"] = "tab list 失败或为空（会话可能已失效）"
        return rep
    if not owner_key:
        # 没有 owner 就无从判断归属 → 一律只报告（fail-closed 的不关一侧）
        rep["report_only"] = [{"tab_id": t.get("tab_id"), "url": (t.get("url") or "")[:60],
                               "why": "no_owner_key"} for t in tabs
                              if t.get("scope") != "user"]
        return rep
    try:
        conn = tabclaims.connect()
    except tabclaims.TabClaimError as e:
        # 账本不可用 = 无法证明归属 = 一个都不许关（fail-closed）
        rep["error"] = e.as_dict()
        rep["report_only"] = [{"tab_id": t.get("tab_id"), "url": (t.get("url") or "")[:60],
                               "why": "ledger_unavailable"} for t in tabs
                              if t.get("scope") != "user"]
        return rep
    try:
        own_ids = {str(r["tab_id"]) for r in
                   tabclaims.own_tabs(conn, sid, owner_key, run_id)}
        # 对账：账本说有、BSK 已没有的，标 closed（不算我们关的）
        rep["reconcile"] = tabclaims.reconcile(conn, sid, tabs)
    finally:
        conn.close()

    agent_tabs = [t for t in tabs if t.get("scope") != "user"]
    rep["user_scope"] = len(tabs) - len(agent_tabs)

    # 分组只用于找「同任务同 URL 的副本」与「空白残留」，归属另由账本判定
    groups = {}
    for t in agent_tabs:
        u = _norm_url(t.get("url") or "")
        groups.setdefault(u if u not in BLANK_URLS else "__blank__", []).append(t)
    has_normal = any(u != "__blank__" for u in groups)

    close, report = [], []
    for u, ts in groups.items():
        ordered = sorted(ts, key=lambda t: (bool(t.get("active")),
                                            t.get("tab_id") or 0))
        if u == "__blank__":
            # 空白页没有「有用副本」可言：还有正常标签时它们**全是残留**；
            # 只剩空白页时必须留一个当导航目标。
            keep = None if has_normal else ordered[-1]
            candidates = list(ts) if has_normal else ordered[:-1]
        else:
            keep = ordered[-1]       # 同址保留最新/活动那个，其余算重复
            candidates = ordered[:-1]
        for t in candidates:
            tid = str(t.get("tab_id") or "")
            base = {"tab_id": tid, "url": (t.get("url") or "")[:60]}
            if tid not in own_ids:
                report.append(dict(base, why="not_owner"))
            elif t.get("active"):
                report.append(dict(base, why="active"))
            else:
                close.append(base)
        # 保留的那个也报告出来，便于人核对（不关）
        if keep is not None:
            report.append({"tab_id": str(keep.get("tab_id") or ""),
                           "url": (keep.get("url") or "")[:60], "why": "kept"})

    rep["report_only"] = report
    for c in close:
        if dry:
            rep["closed"].append(dict(c, note="DRY-RUN"))
            continue
        res = close_tab_guarded(sid, c["tab_id"], owner_key, run_id=run_id)
        if res.get("ok"):
            rep["closed"].append(c)
        else:
            rep["close_failed"].append(dict(c, err=res.get("code"),
                                            detail=res.get("message", "")[:100]))
    return rep


def close_tab_guarded(sid: str, tab_id: str, owner_key: str, *,
                      run_id: str = "", fencing_token: int = 0,
                      timeout: int = 30) -> dict:
    """在资源保护下关闭一个标签（2026-11-02 裁定：tab close 页是副作用）。

    顺序：账本 mark_closing（校验 owner/run/fencing）→ bsk tab close →
    成功 `mark_closed`；结果不可判（超时 / cdp_failed / settling）→
    `mark_unknown`（隔离 + must_reinspect，**不重试、不自动释放**）。
    """
    try:
        conn = tabclaims.connect()
    except tabclaims.TabClaimError as e:
        return {"ok": False, "code": e.code, "message": e.message}
    try:
        try:
            tabclaims.mark_closing(conn, sid, tab_id, owner_key, run_id=run_id,
                                   fencing_token=fencing_token)
        except tabclaims.TabClaimError as e:
            return e.as_dict()
        rc, out, err = bsk(["tab", "close", "--session", sid, str(tab_id)],
                           timeout=timeout)
        blob = ((err or "") + (out or "")).lower()
        unknown = (rc == 124 or "timeout" in blob or "settling" in blob
                   or "not responding" in blob or "cdp_failed" in blob)
        if rc == 0:
            tabclaims.mark_closed(conn, sid, tab_id)
            return {"ok": True, "tab_id": str(tab_id)}
        if unknown:
            tabclaims.mark_unknown(conn, sid, tab_id, (err or out)[:120])
            return {"ok": False, "code": "TAB_CLOSE_UNKNOWN",
                    "message": "关闭结果未知，标签已隔离（must_reinspect），"
                               "不许重试也不许当它不存在"}
        tabclaims.mark_closed(conn, sid, tab_id)   # 明确失败（标签本就不存在等）
        return {"ok": False, "code": "TAB_CLOSE_FAILED",
                "message": (err or out).strip()[:120]}
    finally:
        conn.close()


def heal_window_placement(st: dict, path: str, sid: str) -> dict:
    """页面开到用户窗口时的自愈：把绑定地址迁回 Agent Window。

    复用优先（2026-11-02 用户指令：禁止无脑开新标签）——
    ① agent 窗口已有同址标签 → select + 导航；
    ② 没有同址但有空白残留 → 导航空白标签；
    ③ 都没有才 `tab create`（创建后立即导航，不留 about:blank 孤儿）。
    失败只报告，不硬来（反复抢页面会打扰用户，修根在扩展设置，见 doctor 输出）。
    """
    rep = window_report(st, sid)
    if rep.get("ok") is not False:
        return rep
    bound = st.get("url") or ""
    before = None          # None=复用语义；集合=本次新建的判定基线
    tid = find_tab_by_url(sid, bound)
    if tid:
        bsk(["tab", "select", "--session", sid, tid], timeout=30)
        rc, out, err = navigate_soft(sid, bound)
        note = "复用同址标签 %s + 重新导航" % tid
    else:
        blank = _find_blank_tab(sid)
        if blank:
            bsk(["tab", "select", "--session", sid, blank], timeout=30)
            rc, out, err = navigate_soft(sid, bound)
            note = "复用空白标签 %s + 导航" % blank
        else:
            before = {str(t.get("tab_id") or "") for t in _agent_tabs(sid)}
            rc, out, err = bsk(["tab", "create", "--session", sid], timeout=60)
            if rc != 0:
                rep["heal"] = "tab create 失败：%s" % (err or out).strip()[:150]
                return rep
            rc, out, err = navigate_soft(sid, bound)
            note = "tab create + 重新导航"
    # 自愈动了页面 = 动了标签，必须同步账本：新建的记 opened，复用的记 reused。
    # 登记失败不阻断自愈（rep 里带上结果，交给上层决定是否 fail-closed）。
    rep_claim = claim_session_tabs(sid, st, path, before=before)
    rep2 = window_report(st, sid)
    rep2["claim"] = rep_claim
    rep2["heal"] = ("%s%s" % (note, "成功" if rep2.get("ok") else
                    "后仍在用户窗口（扩展 Automation 设置为共享模式？更新扩展或改设置）"))
    if rep2.get("ok") and st is not None:
        st["window_kind"] = "agent"
    return rep2


# ---------------------------------------------- 窗口归属三态门（2026-11-02 GPT 审计 P0）
#
# GPT 2026-11-02 审计裁定：window.ok=null 不得放行受保护操作——「无法确认归属」
# 既不是成功也不是可接受的默认态。三态语义：
#   verified  已验证页面在本会话 Agent Window        → 放行
#   misplaced 已确认页面不在（用户窗口/自愈失败）      → 一律禁止
#   unknown   探不到（tab list 失败/扩展无响应）       → 禁止进入受保护操作
# 受保护操作 = 向页面写入（send/clear：打错窗口 = 往用户窗口打字）。只读操作
# （probe/read/triage）不受此门约束。
#
# 逃生阀已删除（2026-11-02 GPT 复审阶段一裁定）：unknown/misplaced 一律拒绝，
# 无环境变量 / 调试参数 / 隐藏入口可越过。人工接管走显式流程：
#   wa.py window status   —— 看当前三态与判定时间（只读诊断，不受门约束）
#   wa.py window release --confirm '<人已确认>' —— 停会话（释放窗口）+ 清缓存，
#                            必须人工确认后重新 bind（bind 自带归属探测）
# 绕过入口盘点（阶段一）：STARPILOT_ALLOW_DIRECT_SEND 只豁免编排器熔断，
# 其后的窗口门照常执行，不构成窗口验证绕过；IAB 传输无窗口概念，是传输形态
# 而非参数绕过。

WINDOW_CHECK_TTL_S = 300


def window_verified(st: dict, path: str, sid: str, attempts: int = 3) -> str:
    """归属三态判定（探测重试 + 自愈一次）。结果写回 state 并带时间戳。"""
    if _transport_is_iab():
        return "iab"  # IAB 传输无窗口概念，不受此门约束
    # 自愈会动标签（select / navigate / tab create），必须先有记账上下文，
    # 否则 navigate_gate 拿不到 owner 就只能放行或一律拒绝，两种都不对。
    _remember_ctx(st, path)
    v = "unknown"
    for i in range(attempts):
        rep = window_report(st, sid)
        if rep.get("ok") is True:
            v = "verified"
            break
        if rep.get("ok") is False:
            rep = heal_window_placement(st, path, sid)  # 自愈一次再复检
            v = "verified" if rep.get("ok") is True else "misplaced"
            break
        time.sleep(1.5 * (i + 1))  # None：扩展可能还在注册，退避重试
    st["window_verified"] = v
    st["window_checked_at"] = time.time()
    return v


def require_window_verified(st: dict, path: str, sid: str):
    """send/clear 前强制校验。返回 (ok, 拒绝说明)。

    GPT 复审阶段一裁定：unknown/misplaced 一律拒绝，逃生阀已删除——没有
    会话 = 归属未知，同样拒绝；无任何环境变量 / 参数可越过本门。
    """
    if _transport_is_iab():
        return True, ""
    if not sid:
        # 没有会话就没有归属可言：unknown 即拒绝（此前放行给 ensure_session
        # 兜底是隐式信任，复审后改为显式拒绝，堵住「无会话直发」入口）。
        return False, ("state 里没有会话（session 为空）——归属无从谈起，"
                       "按 unknown 拒绝。先跑 wa.py bind 重新绑定。")
    checked_at = float(st.get("window_checked_at") or 0)
    fresh = (st.get("window_verified") in ("verified", "misplaced")
             and time.time() - checked_at < WINDOW_CHECK_TTL_S)
    v = st["window_verified"] if fresh else window_verified(st, path, sid)
    if v == "verified":
        save_state(path, st)
        return True, ""
    save_state(path, st)
    if v == "misplaced":
        why = ("页面被确认开在本会话 Agent Window 之外（自愈失败）。往用户窗口打字 = "
               "直接干扰人用电脑，禁止发送。人工接管（显式流程，无环境变量可越过）："
               "①人确认当前窗口归属 → ②`wa.py window release --confirm '<确认说明>'`"
               "停会话释放窗口 → ③重新 `wa.py bind`（bind 自带归属探测）。")
    else:
        why = ("无法确认窗口归属（tab list 探不到：扩展无响应/版本过旧）。"
               "归属未知 ≠ 归属正确，禁止发送。处置：跑 wa.py doctor 看扩展状态；"
               "需人工接管时走显式流程 `wa.py window release --confirm '<确认说明>'` "
               "后重新 bind——本门没有环境变量逃生阀（GPT 复审阶段一裁定）。")
    return False, why


def cmd_window(a) -> int:
    """窗口归属子命令：status（只读诊断）/ release（显式人工接管）。"""
    path = state_path(a.state)
    st = load_state(path)
    if a.window_cmd == "status":
        sid = st.get("session") or ""
        info = {"window_verified": st.get("window_verified"),
                "window_checked_at": st.get("window_checked_at"),
                "session": sid, "url": st.get("url"),
                "note": "status 是只读诊断，不受窗口门约束"}
        if sid and not st.get("window_checked_at"):
            v = window_verified(st, path, sid)
            info["window_verified"] = v
            save_state(path, st)
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    # release：显式人工接管——停会话（释放窗口）+ 清归属缓存，必须带确认说明
    conf = (a.confirm or "").strip()
    if not conf:
        print("WINDOW_RELEASE_REQUIRES_CONFIRM：release 必须带 "
              "--confirm '<人工已确认的说明>'（谁确认的、为什么释放）。\n"
              "  本命令会停掉当前会话并清除归属缓存，之后必须重新 bind。",
              file=sys.stderr)
        return 6
    sid = st.get("session") or ""
    stopped = ""
    if sid:
        try:
            bsk(["session", "stop", sid], timeout=30)
            stopped = sid
        except Exception as e:
            stopped = "stop 失败(%s)" % e
    for k in ("window_verified", "window_checked_at"):
        st.pop(k, None)
    save_state(path, st)
    print(json.dumps({"ok": True, "released_session": stopped,
                      "confirm": conf, "next": "重新 wa.py bind（自带归属探测）"},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_tab_reap(a) -> int:
    """列出/关闭当前任务**明确拥有**的陈旧标签（2026-11-02 Web AI 裁定后）。

    归属判据来自账本（bsk 不提供标签所有权）。默认 dry（只报告），
    加 --apply 才真关；非本任务持有、活动标签、借用标签一律不关。
    """
    path = state_path(a.state)
    st = load_state(path)
    sid = st.get("session") or ""
    if not sid:
        print("state 里没有会话：先 bind（回收无对象）。", file=sys.stderr)
        return 2
    own = _win_owner(st, path)
    rep = reap_own_tabs(sid, st.get("url") or "",
                        dry=not bool(getattr(a, "apply", False)),
                        owner_key=(own or {}).get("workspace") or "",
                        run_id=(own or {}).get("run_id") or "")
    rep["session"] = sid
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0 if not rep.get("error") else 1


def cmd_lease(a) -> int:
    """窗口租约子命令：status / list / release / takeover。

    status   只读：这个实例被谁占着、还剩多久
    list     只读：所有实例的租约一览
    release  释放**自己的**租约（不是自己的一律拒绝）
    takeover 显式人工接管：必须 --confirm 说明（谁授权、为什么对方已经不在了），落 journal
    """
    sub = getattr(a, "lease_cmd", "status")
    path = state_path(a.state)
    st = load_state(path)
    own = _win_owner(st, path)
    if sub == "list":
        rows = winlease.list_all()
        print(json.dumps({"leases": rows, "me": own}, ensure_ascii=False, indent=2))
        return 0
    inst = (getattr(a, "instance", "") or "").strip() or _instance_id(st)
    if sub == "status":
        print(json.dumps(dict(winlease.status(inst), me=own,
                              may_stop=["只有自己名下的会话可被清理"]),
                         ensure_ascii=False, indent=2))
        return 0
    if sub == "release":
        ok = winlease.release(inst, own, reason=getattr(a, "confirm", "") or "")
        if not ok:
            cur = winlease.status(inst)
            print(json.dumps({"ok": False, "code": "LEASE_NOT_MINE",
                              "holder": cur.get("owner"),
                              "hint": "不是自己名下的租约不许释放；对方已确认死亡时用 "
                                      "`lease takeover --confirm '<说明>'`"},
                             ensure_ascii=False, indent=2), file=sys.stderr)
            return 9
        print(json.dumps({"ok": True, "released": inst}, ensure_ascii=False))
        return 0
    # takeover
    conf = (getattr(a, "confirm", "") or "").strip()
    if not conf:
        print("LEASE_TAKEOVER_REQUIRES_CONFIRM：必须 --confirm '<谁授权、为什么接管>'。\n"
              "  接管会覆盖一个仍活着的租约，对方的会话可能被孤立——只能用于确认对方已死。",
              file=sys.stderr)
        return 6
    rec = winlease.acquire(inst, own, takeover_reason=conf)
    print(json.dumps({"ok": True, "instance": inst,
                      "took_over_from": rec.get("takeover_from"),
                      "reason": conf,
                      "next": "对方会话未被停止（本工具不替人杀别人的窗口），需要时人工关"},
                     ensure_ascii=False, indent=2))
    return 0


def _win_owner(st: dict, path: str) -> dict:
    """本任务的窗口租约身份：工作区 + run_id（同工作区不同 run 也算不同任务）。"""
    ws = os.path.dirname(os.path.abspath(path)) or "."
    run_id = st.get("run_id") or os.environ.get("STARPILOT_RUN_ID", "") or ""
    return winlease.make_owner(workspace=ws, run_id=run_id,
                               platform=st.get("platform") or "")


def _instance_id(st: dict, rep: dict | None = None) -> str:
    """解析当前任务用哪个浏览器实例。

    优先级：state.browser → WA_BROWSER → 唯一在线实例 → "default"。
    为什么要兜到「唯一在线实例」：2026-11-02 实测另一个任务的 state 里根本没有
    `browser` 字段，于是它的清理逻辑 `if browser and ...` 过滤全失效，停掉了
    所有实例上所有会话。实例未知不等于「无实例」，必须解析出来。
    """
    b = (st.get("browser") or os.environ.get("WA_BROWSER", "") or "").strip()
    if b:
        return b
    try:
        rep = rep or instance_report()
        insts = rep.get("instances") or []
        if len(insts) == 1:
            iid = insts[0].get("instance_id") or insts[0].get("id") or ""
            if iid:
                st["browser"] = iid          # 解析出来就记住，别每次都猜
                return iid
    except Exception:
        pass
    return "default"


def _supports_isolation(st: dict, sid: str, rep: dict | None = None) -> bool:
    """该实例能不能同时开多个独立 Agent Window（真隔离）。

    判据：`bsk tab list --scope agent` 可用（扩展 ≥0.4.0 才有 scope）。探不到就按
    扩展版本判；都判不出 → False（保守：宁可串行排队，也不并发抢同一块屏幕）。
    """
    if sid:
        rc, out, err = bsk(["tab", "list", "--session", sid,
                            "--scope", "agent", "--json"], timeout=30)
        if rc == 0:
            return True
    try:
        rep = rep or instance_report()
        iid = _instance_id(st, rep)
        for it in rep.get("instances") or []:
            if (it.get("instance_id") or it.get("id")) == iid:
                ver = str(it.get("extension_version") or "0.0").split(".")
                return (int(ver[0]), int(ver[1] if len(ver) > 1 else 0)) >= (0, 4)
    except Exception:
        pass
    return False


def acquire_window(st: dict, path: str, sid: str = "", rep: dict | None = None) -> str:
    """开窗/写页前取窗口租约。冲突**不抢占**，按值守模式给轮换预算：

    · 有人值守（WA_ATTENDED=1）：短等 30s 仍被占 → **先提醒人**（WINDOW_BUSY_NOTICE，
      报占用者/剩余时间/可选动作），然后**继续等满轮换预算**——2026-11-02 用户裁定：
      人工介入是并行监督，**不打断星舵执行**；除非人主动要求停止，流程持续推进到
      任务完成（规划/进度/验收全链路）。
    · 无人值守（默认）：静默等满预算。等不到才 WINDOW_BUSY 退出（rc=9）——
      agent 的外层循环可以稍后重试，不打扰人。
    """
    inst = _instance_id(st, rep)
    own = _win_owner(st, path)
    serial = not _supports_isolation(st, sid or (st.get("session") or ""), rep)
    attended = winlease.is_attended()
    budget = winlease.wait_budget(False)   # 轮换预算：有人/无人一致（介入≠打断）
    # 预算覆盖口（测试/运维调参）：STARPILOT_LOCK_WAIT 显式设置时取更小者
    try:
        env_wait = float(os.environ.get("STARPILOT_LOCK_WAIT") or "")
    except ValueError:
        env_wait = None
    if env_wait is not None:
        budget = min(budget, max(0.0, env_wait))
    if serial:
        # 串行降级（实例不支持多 Agent Window）：本来就要排队
        budget = min(budget, winlease.SERIAL_DEFAULT_WAIT)
        st["_serial_wait"] = budget
    try:
        if attended:
            try:
                rec = winlease.acquire(inst, own, serial=serial,
                                       wait=min(winlease.ATTENDED_NOTIFY_WAIT, budget))
            except winlease.WindowBusy as e:
                _notify_busy_human(inst, e)   # 提醒人，但不停：继续等满预算
                rec = winlease.acquire(inst, own, serial=serial,
                                       wait=max(0.0, budget - winlease.ATTENDED_NOTIFY_WAIT))
        else:
            rec = winlease.acquire(inst, own, serial=serial, wait=budget)
    except winlease.WindowBusy as e:
        print(json.dumps(e.holder, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(9)
    if sid:
        winlease.claim_session(inst, sid, own)
    st["window_instance"] = inst
    st["lease_owner"] = own
    save_state(path, st)
    return inst


def _notify_busy_human(inst: str, e: "winlease.WindowBusy") -> None:
    """有人值守时的**非阻塞**提醒：占用现场报给人，流程继续等，不要求人裁决。"""
    holder = e.holder or {}
    who = (holder.get("owner") or {}).get("workspace") or "未知工作区"
    left = round((holder.get("lease_until") or 0) - time.time(), 1)
    print(json.dumps({
        "code": "WINDOW_BUSY_NOTICE",
        "instance": inst,
        "holder_workspace": who,
        "holder_lease_left_s": max(0, left),
        "waited_s": round(e.waited, 1),
        "message": ("提醒（不打断执行）：浏览器实例正被另一个任务占用（%s），"
                    "本任务在轮换排队中，等对方收口即自动接手。如需立即介入："
                    "①让对方任务先收口；②换浏览器实例（--browser/WA_BROWSER）；"
                    "③确认对方死锁时人工授权 takeover（wa.py lease takeover --confirm）。"
                    % who),
    }, ensure_ascii=False, indent=2), file=sys.stderr)


def enforce_single_session(keep: str = "", browser: str = "", owner: dict | None = None) -> None:
    """窗口纪律：同一浏览器实例内只留一个会话——**但只清自己名下的**。

    2026-11-02 现场事故：原实现只要 `keep` 之外就 stop，而 `browser` 为空时连
    「其他实例不动」的过滤都失效（另一个任务的 state 没记 browser），结果把本项目
    刚开的会话在 60 秒内停掉，双方都无从知情。现在：会话若被另一个**活**租约声明，
    一律不动——清理的边界止于自己的租约。
    """
    inst_hint = (browser or "").strip()
    try:
        for s in list_sessions():
            sid = s.get("id") or s.get("session") or ""
            if not sid or sid == keep:
                continue
            inst = (s.get("browser") or inst_hint or "default")
            if inst_hint and (s.get("browser") or "") not in ("", inst_hint):
                continue  # 其他实例的会话不动
            if owner is not None:
                ok, why = winlease.may_stop_session(inst, sid, owner)
                if not ok:
                    sys.stderr.write("跳过清理：会话 %s %s\n" % (sid, why))
                    continue
            bsk(["session", "stop", sid], timeout=30)
    except Exception:
        pass


# 当前任务的 state / path 上下文（navigate 门需要它判断归属；ensure_session 写入）
_LAST_CTX = {"st": None, "path": ""}


def _remember_ctx(st: dict, path: str) -> None:
    _LAST_CTX["st"], _LAST_CTX["path"] = st, path


def _owner_ctx(st: dict, path: str, inst: str = "") -> dict:
    """当前任务的归属四要素（owner_key / run_id / lease_id / fencing）。

    四项缺一不可（裁定 §1）：只有 owner_key 能说明「谁」，lease_id + fencing
    才能说明「这任租约还作数」，否则上一个任务留下的旧记录会被当成本任务的。
    """
    own = _win_owner(st, path)
    inst = inst or _instance_id(st)
    rec = winlease.read(inst) or {}
    return {"owner_key": own.get("workspace") or "",
            "run_id": own.get("run_id") or "",
            "lease_id": rec.get("lease_id") or "",
            "fencing": int(rec.get("fencing") or 0),
            "inst": inst}


def claim_tab(sid: str, tab_id: str, st: dict, path: str, *,
              intent: str = tabclaims.INTENT_OPENED, url: str = "",
              window_id=None, inst: str = "") -> dict:
    """把单个标签登记到账本。**不抛异常**：登记失败只让后续导航门 fail-closed。"""
    ctx = _owner_ctx(st, path, inst)
    if not ctx["owner_key"] or not tab_id:
        return {"ok": False, "why": "no_owner_key"}
    try:
        conn = tabclaims.connect()
    except tabclaims.TabClaimError as e:
        return {"ok": False, "code": e.code, "message": e.message}
    try:
        row = tabclaims.claim(conn, sid, str(tab_id), ctx["owner_key"],
                              run_id=ctx["run_id"], lease_id=ctx["lease_id"],
                              fencing_token=ctx["fencing"], instance_id=ctx["inst"],
                              window_id=window_id, intent=intent, url=url)
        return {"ok": True, "tab_id": str(tab_id), "fencing": row.get("fencing_token")}
    except tabclaims.TabClaimError as e:
        return e.as_dict()
    finally:
        conn.close()


def _takeover_evidence(row: dict, inst: str = "") -> tuple:
    """接管他人标签的证据链。返回 (allowed, why)。

    2026-11-02 复审裁定：**租约失效是必要条件，不是充分条件**——它只说明旧授权
    作废，不说明旧进程已死。必须同时证明：
      ① 那任租约已失效（过期 / lease_id 换代 / 文件消失）；
      ② 原持有者进程确实不在（host + pid + pid_start 三件套，防 PID 复用）。
    证不出来 → OWNER_UNKNOWN，拒绝接管（不拿「等一会儿」冒充死亡证明）。
    """
    if not row:
        return False, "owner_unknown_no_record"
    try:
        rec = winlease.read(row.get("instance_id") or inst) or {}
    except Exception:
        return False, "owner_unknown_lease_unreadable"
    if not rec:
        return False, "owner_unknown_no_lease"
    same_lease = (rec.get("lease_id") or "") == (row.get("lease_id") or "")
    try:
        live = float(rec.get("lease_until") or 0) > time.time()
    except Exception:
        live = True
    if same_lease and live:
        return False, "lease_alive"          # 旧任务还活着，不许抢
    dead, why = winlease.holder_dead(rec)
    if not dead:
        return False, "owner_unknown_%s" % why
    return True, "holder_dead_%s" % why


def _try_takeover(conn, sid: str, tid: str, ctx: dict, row: dict,
                  url: str = "") -> dict:
    """证据链通过才接管；否则返回失败（不抢）。"""
    ok, why = _takeover_evidence(row, ctx["inst"])
    if not ok:
        return {"ok": False, "why": why}
    try:
        tabclaims.takeover(conn, sid, tid, ctx["owner_key"],
                           run_id=ctx["run_id"], lease_id=ctx["lease_id"],
                           fencing_token=ctx["fencing"], instance_id=ctx["inst"],
                           reason=why, url=url)
        return {"ok": True, "why": "took_over_stale", "evidence": why}
    except tabclaims.TabClaimError as e:
        return e.as_dict()


def claim_session_tabs(sid: str, st: dict, path: str, inst: str = "",
                       before=None) -> dict:
    """把本会话 Agent Window 里的标签登记进账本（本任务持有）。

    为什么要「登记已有的」：会话可能是上一个任务留下的（组窗口复用），
    那些标签的归属在 bsk 侧查不到。这里按「先到先得」登记：账本里没有的记为
    本任务 reused；已被别的任务登记的**不抢**，留给导航门去拒。

    `before` 语义（区别 opened / reused，回收策略按这个区分对待）：
      · None  → 没做前后对比，一律记 reused（保守：不当成自己新开的）；
      · 集合  → 不在集合里的就是**本次新建**，记 opened。传空集合 = 全是新的。
    """
    ctx = _owner_ctx(st, path, inst)
    if not ctx["owner_key"]:
        return {"ok": False, "why": "no_owner_key"}
    prev = None if before is None else set(str(x) for x in before)
    claimed, opened, skipped = [], [], []
    try:
        conn = tabclaims.connect()
    except tabclaims.TabClaimError as e:
        # 账本不可用 = 无法证明归属 → 不猜、不登记；后续导航门会 fail-closed
        sys.stderr.write("标签账本不可用（%s）：%s\n" % (e.code, e.message))
        return {"ok": False, "code": e.code, "detail": e.message}
    try:
        for t in _agent_tabs(sid):
            if t.get("scope") == "user":
                continue
            tid = str(t.get("tab_id") or "")
            if not tid:
                continue
            intent = (tabclaims.INTENT_OPENED if (prev is not None and tid not in prev)
                      else tabclaims.INTENT_REUSED)
            try:
                tabclaims.claim(conn, sid, tid, ctx["owner_key"],
                                run_id=ctx["run_id"], lease_id=ctx["lease_id"],
                                fencing_token=ctx["fencing"],
                                instance_id=ctx["inst"],
                                window_id=t.get("window_id"),
                                intent=intent, url=t.get("url") or "")
                claimed.append(tid)
                if intent == tabclaims.INTENT_OPENED:
                    opened.append(tid)
            except tabclaims.TabClaimError as e:
                # 旧任务留下的记录：原租约已失效就接管（组窗口复用的必经之路），
                # 还活着就不抢。
                if e.code in ("TAB_OWNED_BY_OTHER", "TAB_NOT_CLAIMABLE"):
                    res = _try_takeover(conn, sid, tid, ctx,
                                        tabclaims.get(conn, sid, tid) or {},
                                        url=t.get("url") or "")
                    if res.get("ok"):
                        claimed.append(tid)
                        continue
                skipped.append({"tab_id": tid, "code": e.code})
    finally:
        conn.close()
    if skipped:
        sys.stderr.write("标签归属：%d 个标签仍在别的活任务名下，未接管（导航会被拒）\n"
                         % len(skipped))
    return {"ok": True, "claimed": claimed, "opened": opened, "skipped": skipped}


def navigate_gate(sid: str, url: str) -> dict:
    """导航前所有权硬门（2026-11-02 裁定 §2）。

    只有「当前任务持有 + 租约有效 + fencing 未落后 + 状态可用」的标签可以导航；
    无主标签先原子 claim 再放行；其余一律拒绝（别的任务持有 / 借用 /
    正在关闭 / 已关闭 / unknown / must_reinspect）。
    """
    if sid == "iab":
        # IAB 传输没有标签生命周期（宿主浏览器自己管），不适用本门
        return {"ok": True, "why": "iab_no_tab_lifecycle"}
    st, path = _LAST_CTX.get("st"), _LAST_CTX.get("path") or ""
    if not st:
        # 没有记账上下文 = 拿不到 owner_key/租约/fencing = 无法证明这个标签是我们的。
        # 裁定 §2：证明不了就不许写。这里以前是 fail-open（「保持旧行为」），
        # 2026-11-02 复审后改为 fail-closed——否则绕开 ensure_session 就能跳过所有权。
        return {"ok": False, "why": "no_owner_ctx", "tab_id": "",
                "message": "没有记账上下文（未经 ensure_session）：无法证明标签归属，拒绝导航"}
    own = _win_owner(st, path)
    owner_key = own.get("workspace") or ""
    run_id = own.get("run_id") or ""
    inst = _instance_id(st)
    rec = winlease.read(inst) or {}
    lease_id = rec.get("lease_id") or ""
    fencing = int(rec.get("fencing") or 0)
    ctx = {"owner_key": owner_key, "run_id": run_id, "lease_id": lease_id,
           "fencing": fencing, "inst": inst}
    tabs = [t for t in _agent_tabs(sid) if t.get("scope") != "user"]
    if not tabs:
        return {"ok": True, "why": "no_agent_tab"}
    active = next((t for t in tabs if t.get("active")), tabs[0])
    tid = str(active.get("tab_id") or "")
    try:
        conn = tabclaims.connect()
    except tabclaims.TabClaimError as e:
        # 账本不可用 → 不能证明归属 → 拒绝写入（fail-closed，但报清原因）
        return {"ok": False, "why": e.code, "tab_id": tid, "message": e.message}
    try:
        ok, why, row = tabclaims.navigate_allowed(
            conn, sid, tid, owner_key, run_id=run_id, lease_id=lease_id,
            fencing_token=fencing)
        if ok:
            tabclaims.touch_url(conn, sid, tid, url, owner_key)
            return {"ok": True, "why": why, "tab_id": tid}
        if why == "unclaimed":
            try:
                tabclaims.claim(conn, sid, tid, owner_key, run_id=run_id,
                                lease_id=lease_id, fencing_token=fencing,
                                instance_id=inst, window_id=active.get("window_id"),
                                intent=tabclaims.INTENT_REUSED, url=url)
                return {"ok": True, "why": "claimed_now", "tab_id": tid}
            except tabclaims.TabClaimError as e:
                return {"ok": False, "why": e.code, "tab_id": tid,
                        "message": e.message}
        # 「别人的名字」不等于「别人还在用」，但「租约过期」也不等于「别人已经死了」。
        # 只有证据链齐（旧授权失效 + 原持有者进程确认死亡）才接管，否则 OWNER_UNKNOWN。
        if why in ("owned_by_other", "run_mismatch", "lease_mismatch",
                   "stale_fencing"):
            res = _try_takeover(conn, sid, tid, ctx, row or {}, url=url)
            if res.get("ok"):
                tabclaims.touch_url(conn, sid, tid, url, owner_key)
                return {"ok": True, "why": res["why"], "tab_id": tid,
                        "evidence": res.get("evidence")}
            return {"ok": False, "why": res.get("why") or why, "tab_id": tid,
                    "row": row,
                    "message": ("该标签仍属另一个任务：%s。要接管必须证明原持有者"
                                "进程已死（host+pid+pid_start），证不出来就拒绝。"
                                % (res.get("why") or why))}
        return {"ok": False, "why": why, "tab_id": tid, "row": row}
    finally:
        conn.close()


def navigate_soft(sid: str, url: str, timeout: int = 180):
    """导航到 url，**容忍 `load` 事件不来**。

    为什么需要：SPA / 长轮询页面（Gemini 实测）可能永远不触发 `load`，
    `--wait-until load` 会在 30 秒后返回非 0 —— 但页面其实早就可用了。
    把它当失败 = 让「绑定」这种纯本地动作被一个永远不会发生的事件卡死。

    退让顺序：load → domcontentloaded。仍失败也**不在这里判死**：
    调用方必须再用「地址对不对 + 编辑器在不在」确认页面确实可用（那才是真正的判据）。
    返回 (rc, out, err)，err 里带上第一次失败原因，便于事后分辨「慢」与「坏」。
    """
    gate = navigate_gate(sid, url)
    if not gate.get("ok"):
        return 3, "", ("导航被所有权门拒绝（%s，tab %s）：该标签不属于当前任务、"
                       "或正处于 closing/unknown/借用状态。不抢别人的标签。"
                       % (gate.get("why"), gate.get("tab_id")))
    rc, out, err = bsk(["navigate", url, "--session", sid, "--wait-until", "load"],
                       timeout=timeout)
    if rc == 0:
        return rc, out, err
    first = (err or out).strip()[:200]
    rc2, out2, err2 = bsk(["navigate", url, "--session", sid,
                           "--wait-until", "domcontentloaded"], timeout=timeout)
    if rc2 == 0:
        return rc2, out2, "load 未触发，已退到 domcontentloaded（%s）" % first
    return rc2, out2, ((err2 or out2).strip()[:300] + " | 首次(load)：%s" % first)


def _norm_url(u: str) -> str:
    u = (u or "").split("#", 1)[0].strip()
    return u[:-1] if u.endswith("/") else u


def find_tab_by_url(sid: str, url: str, timeout: int = 30) -> str:
    """在会话的 Agent Window 里找已开着的同地址标签。返回 tab_id 或 ""。

    背景（2026-11-02 现场）：同一对话被 navigate 了 5 次 = 用户窗口里 5 个重复
    标签——每次重试都新开标签是风控诱因，也是「浏览器不像人」的直接证据。
    """
    try:
        rc, out, err = bsk(["tab", "list", "--session", sid, "--json"],
                           timeout=timeout)
        if rc:
            return ""
        d = json.loads(out or "{}")
        target = _norm_url(url)
        for t in d.get("tabs") or []:
            if _norm_url(t.get("url") or "") == target and t.get("scope") != "user":
                return str(t.get("tab_id") or "")
    except Exception:
        return ""
    return ""


def navigate_reuse(sid: str, url: str, timeout: int = 180):
    """导航但**优先复用同地址标签**：先 tab select 聚焦再导航（导航作用于活动 tab）。

    返回 (rc, out, err, reused)。找不到同地址标签才走普通导航（新标签）。
    """
    tid = find_tab_by_url(sid, url)
    if not tid:
        rc, out, err = navigate_soft(sid, url, timeout=timeout)
        return rc, out, err, False
    bsk(["tab", "select", "--session", sid, tid], timeout=30)
    rc, out, err = navigate_soft(sid, url, timeout=timeout)
    if rc != 0:
        rc, out, err = navigate_soft(sid, url, timeout=timeout)  # select 未生效则原样重试
    return rc, out, err, True


def ensure_session(st: dict, path: str, *, renew: bool = False) -> str:
    if _transport_is_iab():
        # IAB 传输：无窗口生命周期，直接复用宿主浏览器；绑定了地址就导航过去
        bound = st.get("url") or ""
        if bound:
            navigate_soft("iab", bound)
            time.sleep(2)
        return "iab"
    sid = st.get("session") or ""
    browser = st.get("browser") or os.environ.get("WA_BROWSER", "")
    # 开窗前先体检。重复安装的扩展会让「发送」和「读回」落在不同实例上，
    # 那种状态下继续跑只会制造更多窗口和更多「读不到回复」，必须先让人把环境修干净。
    rep = instance_report()
    if rep["duplicates"] and not os.environ.get("WA_ALLOW_DUPLICATE_EXT"):
        raise SystemExit(duplicate_hint(rep))
    # ---- 窗口租约（跨任务隔离）----
    # 冲突 → SystemExit(9) WINDOW_BUSY，绝不抢占、绝不清理别人的会话。
    # 必须在 enforce_single_session / session start 之前拿到，否则就可能已经动手了。
    inst = acquire_window(st, path, sid=sid, rep=rep)
    if not browser and inst != "default":
        browser = inst
    own = _win_owner(st, path)
    # 记账上下文：本进程内所有 navigate 都要过 `navigate_gate`，而门需要
    # owner_key/run_id/lease/fencing。放在这里而不是各命令里，是因为
    # ensure_session 是**唯一**拿租约的入口——绕开它就绕开了归属。
    _remember_ctx(st, path)
    # 会话清单查不清时**不许开新窗**：以前解析失败会当成「一个会话都没有」，
    # 于是 sid 不在列表 → 直接往下走去 session start → 多一扇窗、多一个会话，
    # 用户看到的是「页面故障 + 窗口越开越多」。看不清就停下报现场。
    try:
        _sess = list_sessions(strict=True)
    except BskQueryError as e:
        raise SystemExit(
            "会话状态查不清：%s\n"
            "这种情况下拒绝开新窗（开窗只会让故障变多）。先跑：\n"
            "  python scripts/wa.py doctor        # 实例与会务体检\n"
            "  bsk status --json                  # daemon / 扩展是否真的连着\n"
            "确认通道正常后重试。" % e)
    ids = [s.get("id") or s.get("session") or "" for s in _sess]
    if sid and not renew:
        if sid in ids:
            winlease.claim_session(inst, sid, own)
            enforce_single_session(sid, browser, own)  # 只清自己名下/无主的
            # 自动回收已停用（2026-11-02 Web AI 裁定）：bsk 的 tab list 不提供
            # 创建者/任务归属（只有 tab_id/window_id/active/scope），凭 URL 猜归属
            # 会误关别的任务正在用的标签。裁定原话：「宁可暂时积累标签，也不能
            # 误关正在工作的标签」。回收改为显式手动入口 `wa.py tab-reap`
            # （默认只报告），待标签归属账本落地后再谈自动化。
            claim_session_tabs(sid, st, path, inst)   # 复用会话：只登记，不清理
            if browser and not st.get("browser"):
                st["browser"] = browser  # env 给的实例也记下来，下次不必再传
                save_state(path, st)
            return sid
    # 组窗口复用（2026-11-02 用户指令）：实例上已经有**组内**活会话（journal 登记、
    # 租约仍活），直接认领复用——新任务在这个窗口里建自己的 tab，不新开窗口。
    if not renew and (not sid or sid not in ids):
        gs = [s for s in winlease.group_sessions(inst) if s in ids]
        if gs:
            sid = gs[-1]
            winlease.claim_session(inst, sid, own)
            enforce_single_session(sid, browser, own)
            # 组窗口复用路径**尤其不能**自动回收：组窗口本就是跨任务共享的会话，
            # 这里的「同地址重复」很可能正是别的任务正在用的标签（2026-11-02
            # Web AI 裁定）。只登记、不清理；清理走显式 `wa.py tab-reap`。
            claim_session_tabs(sid, st, path, inst)   # 复用组窗口：只登记，不清理
            st["session"] = sid
            if browser and not st.get("browser"):
                st["browser"] = browser
            save_state(path, st)
            sys.stderr.write("复用组窗口会话 %s（实例 %s），未新开窗口。\n" % (sid, inst))
            return sid
    # 禁止无脑重开（2026-11-02 用户指令）：全局跨任务计数（锁目录共享），
    # 裸调绕不过——所有走 wa.py 的开窗都记在同一个账本上。
    g_many, g_n, g_max = winlease.too_many_starts(inst)
    if g_many or _too_many_starts(st):
        raise SystemExit(
            "禁止开窗：10 分钟内该实例已被开过 %d 次窗口（全局跨任务计数，上限 %d）。"
            "反复重开不是正常的重试节奏——会话在反复失效或实例在反复切换。\n"
            "先跑 `python scripts/wa.py doctor` 看环境；用 `wa.py lease status` 看"
            "是不是别的任务占着。确认环境没问题要强行继续：仅限用户明示授权，"
            "删掉锁目录 session-starts.json 里该实例的计数后重试。"
            % (max(g_n, len(st.get("session_start_at") or [])), g_max))
    # 开新会话前：收掉**本任务名下**的旧会话（别人的活会话一律不动，见 enforce）
    enforce_single_session("", browser, own)
    # 多浏览器（用户可能同时开多个 Edge profile）：state.browser 优先，其次 WA_BROWSER env
    args = ["session", "start", "--no-focus", "--json"]
    # 0.4.1 新增 --name：任务名进 bsk 本地操作历史，session 归属可追溯
    # （2026-11-03 优化：回应审计「session 无归属字段」缺口）。旧版不识别该
    # 参数会整体失败，故失败且提示 unknown argument 时去掉重试一次。
    sess_name = "starpilot-%s" % time.strftime("%m%d-%H%M%S")
    args += ["--name", sess_name]
    if browser:
        args += ["--browser", browser]
    rc, out, err = bsk(args, timeout=120)
    for _flag in ("--name", "--json"):
        if rc != 0 and "unexpected argument" in (out + err).lower():
            args = [a for a in args if a != _flag and a != sess_name]
            if _flag == "--name":
                sess_name = ""
            rc, out, err = bsk(args, timeout=120)
    if rc == 0 and sess_name:
        st["session_name"] = sess_name
    if rc != 0 and "multiple browsers" in (out + err).lower():
        inst = _first_browser_instance(rep["instances"])
        if inst:
            st["browser"] = inst
            save_state(path, st)
            rc, out, err = bsk(args + ["--browser", inst], timeout=120)
    if rc != 0:
        raise SystemExit("session start 失败：%s" % (err or out)[:400])
    # ---- session id 的来源必须写明白（三档，从最硬到最软）----
    # 以前只有「列会话」和「正则扫 4 位小写字母」两档，且正则谁都像：
    # `edge`、`true`、路径里的一段都可能被当成会话 id，拿错 id = 往别的会话里打字。
    sid, sid_from = "", ""
    okj, jv = parse_bsk_json(out)
    if okj and isinstance(jv, dict):
        sid = jv.get("session_id") or jv.get("session") or jv.get("id") or ""
        if sid:
            sid_from = "session start --json（最硬：bsk 自报）"
    if not sid:
        try:
            ids = [s.get("id") or s.get("session") or ""
                   for s in list_sessions(strict=True)]
        except BskQueryError as e:
            ids = []
            sys.stderr.write("session list 查不回可信结果（%s），改用输出兜底\n" % e)
        ids = [i for i in ids if i]
        if ids:
            sid = ids[-1]
            sid_from = "session list 末位（次硬）"
    if not sid:
        m = _SID_RE2.findall(out or "")
        if m:
            sid = m[-1]
            sid_from = "⚠️ 文本正则兜底（最软，可能取错）——请复核窗口归属"
            sys.stderr.write("BSK-WARN: 会话 id 只能从 %r 里猜（候选 %s）。"
                             "这一步取错就是往别人的会话打字，请人工确认。\n"
                             % (sid, m[-3:]))
    if not sid:
        raise SystemExit("拿不到 session id，且没有可信兜底。原始输出：%s" % out[:400])
    enforce_single_session(sid, browser, own)  # 双保险：新会话留下，本任务其余全收
    _note_session_start(st)               # 本任务自己的计数（保留，双保险）
    winlease.note_start(inst)             # 全局跨任务计数：所有走本入口的开窗都记账
    st["session"] = sid
    winlease.claim_session(inst, sid, own)  # 向其他任务公告：这个会话有主，别停
    if browser:
        # 把实例记进 state：下次不用再靠 WA_BROWSER 环境变量，
        # 多浏览器在线时也不会因为「猜不出实例」而 bind 失败。
        st["browser"] = browser
    save_state(path, st)
    place_window(sid)
    # 新会话：动作前这个会话一个标签都没有 → before=() 让登记记成 opened
    claim_session_tabs(sid, st, path, inst, before=())
    bound = st.get("url") or ""
    if bound:
        rc, out, err, _reused = navigate_reuse(sid, bound)
        if rc == 0:
            time.sleep(3)
    return sid


# ---------------------------------------------------------------- probe
# 注：所有页面 DOM 片段由 platforms.py 按平台生成，本文件不写平台专属选择器。


def evaluate(js: str, sid: str, timeout: int = 60):
    """执行 JS 并把结果解析成 dict。返回 (ok, data_or_err)。"""
    rc, out, err = bsk(["evaluate", js, "--session", sid, "--json"], timeout=timeout)
    if rc == 124:
        return False, "timeout"
    if rc != 0:
        return False, (err or out).strip()[:400]
    # 统一走 parse_bsk_json：旧的「按行找以 { 开头的那行」只吃得下**单行** JSON，
    # bsk 一旦 pretty-print（或前面带一行提示）就解析失败，返回 (False, err) ——
    # 表现是「页面明明在，探测却说不清」。
    okj, data = parse_bsk_json(out)
    if not okj:
        data = None
    if isinstance(data, dict) and "value" in data:
        val = data["value"]
        if isinstance(val, str):
            try:
                data = json.loads(val)
            except Exception:
                data = {"raw": val}
        else:
            data = val
    if isinstance(data, str):
        # IAB 传输（0.1.5）：worker 把 evaluate 结果整包序列化成字符串回传，
        # 二次解 JSON 还原真实对象；解不开才降级为 {"raw": ...}
        try:
            data = json.loads(data)
        except Exception:
            data = {"raw": data}
    if not isinstance(data, dict):
        # JS 表达式合法地返回数组/标量也是有效结果（2026-10-31 实测：
        # 返回按钮属性数组被误判为「无法解析」，把工具自检错误当成了页面故障）。
        # 调用方按需取字段；只有 None 才算真的解析失败。
        if data is None:
            return False, "无法解析 evaluate 输出：%s" % s[:300]
        return True, data
    return True, data


# 「还在生成」的第二路证据：末条回复文本是否在变长。
# 为什么必须有：JS 里 `generating: !!stop` 只看**停止按钮在不在**，
# 而停止按钮是平台改版时最先被换掉的东西之一（ChatGPT 2026-11-02 起
# `data-testid="stop-button"` 命中 0）。只信它会导致：
#   · 等待循环永远看不到「生成中」→ 首字钟(ttl 45s)一到就走分诊，把正在流式
#     输出的长回答误杀；
#   · send 前置守卫失效 → 在对方还在写的时候又发一条，页面上出现两条提问。
# 增长阈值取 20 字：流式一秒几十字，而计时器/省略号动画不足 20 字
# （与 platforms.DOC_GROWTH_MIN 同口径，不另立一套）。
GEN_GROWTH_MIN = 20
GEN_STALE_S = 30.0
_GEN_SAMPLE = {}        # sid -> (ts, last_len)：上一条采样，用来判「有没有在变长」


def _generating_composite(p: dict, sid: str, d: dict) -> dict:
    """把 `generating` 从「只看停止按钮」升级成「按钮 or 正文在长」。

    只改这一个点（而不是等待循环里的 8 个判断）：**口径只有一份**，
    否则三处各判各的，就又回到「闸门与 CLI 说的不是一回事」那种坑。
    `generating_basis` 把依据留在快照里，triage/审计能看清这条结论踩在哪。
    """
    if not isinstance(d, dict):
        return d
    hard = bool(d.get("generating"))
    last_len = int(d.get("last_len") or 0)
    now = time.time()
    prev = _GEN_SAMPLE.get(sid)
    _GEN_SAMPLE[sid] = (now, last_len)
    grew = False
    if prev and last_len > 0:
        pts, plen = prev
        if now - float(pts) <= GEN_STALE_S and last_len - int(plen) >= GEN_GROWTH_MIN:
            grew = True
    d["generating"] = bool(hard or grew)
    d["generating_basis"] = ("stop" if hard else ("growth" if grew else "none"))
    if not hard:
        d["stop_present"] = False      # 如实记下「停止按钮这次没查到」，供分诊区分
    return d


def read_state(p: dict, sid: str, settle_ms: int = 600):
    """滚动到底 → 等虚拟化挂载 → 读状态。读到空线程自动加长等待重试（窗口被遮挡时
    Chrome 会卸载虚拟化内容，单次短等待不可靠）。

    返回值已经过 `platforms.normalize_probe` 归一：**JS 报事实，Python 定口径**。
    所有调用方拿到的是同一套语义（例如「空态类为真则 editor_len=0」），
    不必各自再判一次——各自判就是下一个口径漂移点。
    """
    for attempt, ms in enumerate((settle_ms, 1200, 2500)):
        evaluate(platforms.js_scroll(p), sid)
        time.sleep(ms / 1000.0)
        ok, data = evaluate(platforms.js_probe(p), sid)
        if ok and (platforms.normalize_probe(p, data).get("msg_count", 0) > 0 or attempt == 2):
            return ok, _generating_composite(p, sid, platforms.normalize_probe(p, data))
    return (ok, _generating_composite(p, sid, platforms.normalize_probe(p, data))
            if ok else data)


def count_matches(sel: str, sid: str) -> int:
    """这个选择器在页面上命中几个：-1 = 查不动（浏览器不响应 / 语法不通）。

    为什么必须提前数：bsk 的 focus/press/fill 遇到多匹配**直接拒绝**
    （`selector matched 8 elements`，ChatGPT 2026-11-02 实测：页面上有 8 个
    div.ProseMirror）。拒绝本身没错，错在信息量——上层只看到一个 rc≠0，
    clear/fill 全失败、输入框残留清不掉，agent 以为是「平台故障」反复重试。
    提前数一次就能把「选择器歧义」和「页面坏了」分开说，处置完全相反：
    前者收窄选择器，后者找人看页面。
    """
    js = ('(() => { try { return JSON.stringify({n: document.querySelectorAll(%s).length}); } '
          'catch (e) { return JSON.stringify({n: -1, err: String(e).slice(0,120)}); } })()'
          % json.dumps(sel))
    ok, data = evaluate(js, sid)
    if ok and isinstance(data, dict):
        try:
            return int(data.get("n"))
        except (TypeError, ValueError):
            return -1
    return -1


# 写入路径：唯一实现放在本文件（wa 是传输层，闸门与 CLI 都走它）。
NATIVE_MAX_CHARS = 12000   # 真实键盘输入要经命令行传值，超长有被截断/乱码的风险
# exec/paste 写入的分块大小（2026-11-02 实测：单次 3000+ 字会挂死 CDP 会话）
FILL_CHUNK = 800
FILL_CHUNK_GAP = 0.35


def fill_native(p: dict, text: str, sid: str, timeout: int = 180) -> dict:
    """真实键盘输入（bsk fill）——受控编辑器（Quill）只认这一条。

    **不看 bsk 的返回值，看页面事实**：bsk fill 会拿「它期望的值」和页面实际值做严格比对，
    多行文本被编辑器重排后（换行合并、补空行、行尾空白）会被判 `cdp_failed`，
    但字其实已经躺在编辑器里了。2026-10-29 Gemini 实测：rc 非 0，
    而 `editor_len=1574`、`can_send=true` —— 内容确实写进去了。
    **工具的自我校验不是判据**（和 load 事件、innerText 是同一类错误的三个变种），
    判据只能来自页面：`platforms.fill_check`（字数 + 空态类 + 发送键）。

    逐个候选选择器：填 → 探测 → 判。过就停；编辑器仍是空才换下一个
    （bsk fill 默认先清空目标，第二次输入不会叠在第一次后面）。
    有字但不够 / 发送键没出现时不换选择器——那不是「没打中」，是「平台不接受」，
    换选择器只会把同一份内容再写一遍。
    """
    last = {"ok": False, "code": platforms.FILL_SHORT, "detail": "没有可用选择器",
            "facts": {}, "used": "", "note": ""}
    ambiguous = []
    for sel in (p.get("sel") or {}).get("editor") or []:
        n = count_matches(sel, sid)
        if n > 1:
            # 多匹配 = bsk 一定会拒绝这一条；跳过去试下一个候选，
            # 并把歧义如实记下来（不许伪装成「页面坏了」）
            ambiguous.append("%s→%d 个" % (sel, n))
            last = {"ok": False, "code": "SELECTOR_AMBIGUOUS",
                    "detail": "候选选择器命中多个节点，bsk 会拒绝（selector matched N elements）",
                    "facts": {"ambiguous": ambiguous}, "used": sel, "note": ""}
            continue
        rc, out, err = bsk(["fill", "--selector", sel, "--value", text,
                            "--session", sid, "--json"], timeout=timeout)
        note = ""
        if rc != 0:
            first = ((err or out).strip().splitlines() or [""])[0]
            note = "rc=%s %s" % (rc, first[:160])
        time.sleep(1)
        ok, cur = read_state(p, sid)
        if not ok:
            last = {"ok": False, "code": "probe_failed_after_fill", "detail": str(cur),
                    "facts": {}, "used": sel, "note": note}
            continue
        chk = platforms.fill_check(p, text, cur)
        chk["used"] = sel
        chk["note"] = note
        if chk["ok"]:
            return chk
        last = chk
        if not (chk.get("facts") or {}).get("editor_blank"):
            return chk
    return last


def do_fill(p: dict, text: str, sid: str, timeout: int = 180, tries: int = 2) -> dict:
    """把文本写进编辑器（**不发送**）——写入的**唯一实现**，CLI 与闸门共用。

    两种路径，按平台已实测的那一条走：
      · `native` —— 真实键盘（bsk fill）。受控编辑器（Gemini 的 Quill）只认这条：
        `execCommand('insertText')` 会让字进 DOM 却不进 model（`ql-blank` 还在），
        发送键不渲染，属于「看着写好了、其实没写」。
      · `exec` / `paste` —— 注入 JS 片段（Range 圈定 / 剪贴板事件）。

    写完必须过 `platforms.fill_check`。不过就重来一次，仍不过则**如实失败**——
    绝不在「没写进去」的状态下点发送（点空气只会白耗一轮预算，并把真因掩盖成
    send_unconfirmed）。

    返回 {ok, method, used, attempts, code, detail, facts, note}。
    `note` 存放「工具自己说失败但页面说成功」的那类证据，事后复盘靠它分辨，
    不用回头猜。
    """
    method = (p.get("write_method") or "exec").strip()
    n = len(text or "")
    if method == "native" and n > NATIVE_MAX_CHARS:
        return {"ok": False, "method": method, "code": platforms.FILL_SHORT,
                "detail": "native 写入单次上限 %d 字（当前 %d）：命令行传值超长会被截断，"
                          "请拆分后再发" % (NATIVE_MAX_CHARS, n),
                "facts": {"want": n}, "used": "", "note": ""}
    last = {"ok": False, "method": method, "code": "fill_failed",
            "detail": "", "facts": {}, "used": "", "note": ""}
    for i in range(max(1, tries)):
        if method == "native":
            chk = fill_native(p, text, sid, timeout=timeout)
            if chk.get("ok"):
                return {"ok": True, "method": method, "used": chk.get("used", ""),
                        "attempts": i + 1, "code": chk.get("code"),
                        "detail": "", "facts": chk.get("facts") or {},
                        "note": chk.get("note", "")}
            last = {"ok": False, "method": method, "used": chk.get("used", ""),
                    "attempts": i + 1, "code": chk.get("code") or "fill_failed",
                    "detail": chk.get("detail") or "",
                    "facts": chk.get("facts") or {}, "note": chk.get("note", "")}
            time.sleep(1)
            continue
        ok, res = True, None
        # 长文分块写（2026-11-02 实测）：exec 一次性 insertText 3000+ 字会让页面
        # 重排 + 编辑器事务过重，CDP 会话挂住（`Previous operation is still
        # settling` / `A page frame is not responding`），之后整条会话连
        # tab list 都不响应，只能重启会话重来——每条长报告都要多烧两轮。
        # 首块用 js_fill（覆盖式），后续块用 js_fill_append（末尾追加）。
        chunks = [text[j:j + FILL_CHUNK] for j in range(0, len(text), FILL_CHUNK)] or [""]
        for ci, ch in enumerate(chunks):
            js = (platforms.js_fill(p, ch) if ci == 0
                  else platforms.js_fill_append(p, ch))
            ok, res = evaluate(js, sid, timeout=timeout)
            if not ok and res != "timeout":
                break
            if ci + 1 < len(chunks):
                time.sleep(FILL_CHUNK_GAP)
        # 超时不算失败：长文本 RPC 超时但可能已写入，靠下面的字数自证
        if not ok and res != "timeout":
            last = {"ok": False, "method": method, "code": "fill_failed",
                    "detail": str(res), "facts": {}, "used": "", "note": "",
                    "attempts": i + 1}
            time.sleep(1)
            continue
        time.sleep(1)
        ok, cur = read_state(p, sid)
        if not ok:
            last = {"ok": False, "method": method, "code": "probe_failed_after_fill",
                    "detail": str(cur), "facts": {}, "used": "", "note": "",
                    "attempts": i + 1}
            continue
        chk = platforms.fill_check(p, text, cur)
        if chk["ok"]:
            return {"ok": True, "method": method, "used": "",
                    "attempts": i + 1, "code": chk["code"],
                    "detail": "", "facts": chk["facts"], "note": ""}
        last = {"ok": False, "method": method, "used": "", "attempts": i + 1,
                "code": chk["code"], "detail": chk["detail"],
                "facts": chk["facts"], "note": ""}
    return last


def clear_editor(p: dict, sid: str, tries: int = 2) -> dict:
    """清空编辑器（**不发送**）——给恢复态用：上一轮写到一半、内容还留在里面。

    为什么必须有这个动作：写闸门的守卫「输入框必须干净」是对的（脏输入框会把上一轮的
    残留和新内容拼在一起发出去），但没有清空工具时，一旦停在「已写入未发送」，
    唯一的出路就是人工去点浏览器——纪律就破了。

    做法：在编辑器内全选 → 删除，**用真实键盘**（受控编辑器只认这个）。
    清完必须复核 `editor_blank`；清不干净就如实报失败，不假装干净。
    """
    eds = (p.get("sel") or {}).get("editor") or []
    last = {"ok": False, "detail": "没有可用选择器"}
    for _ in range(max(1, tries)):
        for sel in eds:
            # 先聚焦再全选：不聚焦时 Ctrl+A 落不到编辑器上（2026-11-02 实测：
            # 直接 press Ctrl+A --selector 后再 Delete，输入框 226 字一字未少）
            bsk(["focus", "--selector", sel, "--session", sid], timeout=60)
            bsk(["press", "Ctrl+A", "--selector", sel, "--session", sid], timeout=60)
            break
        bsk(["press", "Delete", "--session", sid], timeout=60)
        time.sleep(1)
        ok, cur = read_state(p, sid)
        if not ok:
            last = {"ok": False, "detail": "清理后探测失败：%s" % cur}
            continue
        if cur.get("editor_blank") or int(cur.get("editor_len") or 0) == 0:
            return {"ok": True, "editor_len": 0, "editor_blank": True}
        last = {"ok": False, "detail": "清理后编辑器仍有 %s 字" % cur.get("editor_len")}
    return last


def probe(st: dict, path: str, p: dict) -> dict:
    sid = ensure_session(st, path)
    ok, data = read_state(p, sid)
    if not ok:
        if data == "timeout":
            raise SystemExit("probe 超时：页面可能卡住，先看 console")
        raise SystemExit("probe 失败：%s" % data)
    return data


# ---------------------------------------------------------------- 写入 / 发送
# 全部委托给 platforms.js_*，本文件不含任何选择器。


def js_fill(p: dict, text: str) -> str:
    return platforms.js_fill(p, text)


def js_click_send(p: dict) -> str:
    return platforms.js_click_send(p)


def do_send(p: dict, sid: str) -> tuple:
    """发送动作的唯一分派（CLI 与闸门都走这里，同源）。

    send_method=click：点发送键（全平台默认）。
    send_method=enter：把焦点交回编辑器再 press Enter——豆包实测点击发送键无效，
    且点击本身会把焦点移走；exec 写入后编辑器本就持焦点，只要不再点按钮即可。
    返回 (ok, detail)，失败时 detail 是原因。

    平台占用锁也在这里取：do_send 是所有发送路径的必经点（CLI 装饰器、写闸门
    execute_send 都到这儿），锁下沉到这一层才不会被绕过。同进程重入是安全的
    （外层 cmd_send 已持锁时内层返回 reentrant，不放锁）。
    """
    try:
        _lock = lock_platform(p.get("key") or platforms.DEFAULT_PLATFORM)
    except statelock.LockBusy as e:
        return False, ("platform_busy：平台 %s 正被另一任务占用（%s）"
                       % (p.get("key"), json.dumps(e.holder, ensure_ascii=False)))
    try:
        return _do_send_now(p, sid)
    finally:
        statelock.release_named(platform_lock_path(p.get("key") or platforms.DEFAULT_PLATFORM),
                                _lock)


def _do_send_now(p: dict, sid: str) -> tuple:
    method = p.get("send_method") or "click"
    if method == "enter":
        ok, res = evaluate(platforms.js_focus_editor(p), sid)
        if not ok or not (isinstance(res, dict) and res.get("ok")):
            return False, "focus-editor 失败：%s" % res
        rc, out, err = bsk(["press", "Enter", "--session", sid], timeout=120)
        if rc != 0:
            return False, "press Enter 失败：%s" % ((err or out).strip()[:200])
        return True, "enter"
    ok, res = evaluate(js_click_send(p), sid)
    if not ok:
        return False, "click 发送失败：%s" % res
    return True, "click"


def js_read_last(p: dict) -> str:
    return platforms.js_read_last(p)


# ---------------------------------------------------------------- 绑定


def settle_msg_count(p: dict, sid: str, tries: int = 8, gap: float = 2.0) -> tuple:
    """等「消息计数」变得可信，再谈归属判定。

    为什么必须有它（2026-10-29 实测第四变种）：打开一条旧对话，Gemini 的消息区
    **先空转几秒再渲染**（虚拟化 + 懒加载）—— 这时 probe 读到 msg_count=0。
    拿「加载中的 0」判「这是一条空对话」，归属门禁就会把旧对话当新对话放行：
    和 load 事件、bsk 自校验、innerText 一样，「工具此刻报的数」不等于「事实」。

    规则：读到 >0 立即返回；否则反复读，**连续 3 次读数相同**才认为稳定
    （不区分 0 还是正数——稳定本身才是判据）。总时长 tries×gap 封顶。
    返回 (ok, probe, waited_seconds)。
    """
    ok, data = read_state(p, sid)
    waited = 0.0
    same = 1
    for _ in range(tries):
        if ok and int((data or {}).get("msg_count") or 0) > 0:
            return ok, data, waited
        time.sleep(gap)
        waited += gap
        ok2, d2 = read_state(p, sid)
        if ok2:
            same = same + 1 if int((d2 or {}).get("msg_count") or 0) == int((data or {}).get("msg_count") or 0) else 1
            data = d2
            ok = True
            if same >= 3:
                break
    return ok, data, waited


@with_platform_lock
def cmd_bind(a) -> int:
    p = platforms.get(a.platform)
    url = a.url.strip()
    new_chat = bool(getattr(a, "new", False))
    reuse_existing = bool(getattr(a, "reuse_existing", False))
    if new_chat:
        # --new 只接受「该平台自己的新对话入口」，不接受任意地址冒充
        entry = p.get("new_chat_url") or p.get("home") or ""
        if url and not _same_origin(url, entry):
            print("拒绑：--new 只接受平台的新对话入口 %s" % entry, file=sys.stderr)
            return 2
        url = entry
    elif not re.search(p["url_pattern"], url):
        print("拒绑：地址不像一个已有对话。%s\n新开会话会让上下文断掉。" % p["url_hint"],
              file=sys.stderr)
        return 2
    path = state_path(a.state)
    st = load_state(path)
    st["platform"] = p["key"]
    sid = ensure_session(st, path)
    rc, out, err, _reused = navigate_reuse(sid, url)
    nav_note = "" if rc == 0 else (err or out).strip()[:300]
    time.sleep(3)
    ok, data = read_state(p, sid)
    if not ok:
        print("绑定后探测失败：%s" % data, file=sys.stderr)
        return 4
    # 导航"失败"但页面已经在目标地址 = 只是 load 事件没来，不是导航失败。
    # 判据是「地址对不对 + 编辑器在不在」，不是「事件有没有触发」。
    # --new 例外：入口被重定向（/app → /app/<hex> 或首页）是**要报出来的事实**，
    # 由 bind_verdict 给出准确原因，不在这里判死。
    if not new_chat and not (data.get("url") or "").startswith(url.split("?")[0][:60]):
        print("导航后地址不符：期望 %s，实际 %s\n原因：%s"
              % (url, data.get("url"), nav_note or "（无）"), file=sys.stderr)
        return 3
    if not data.get("editor"):
        print("绑定后页面没有输入框：可能未登录，或地址已失效。", file=sys.stderr)
        return 5

    # ---- 先等「消息计数」可信，再做归属判定 ----
    # 懒加载平台（Gemini 实测）打开旧对话时消息区先空转：读到 0 不代表对话是空的。
    # 新对话页没有历史可加载，通常 2 次稳定读数（约 4 秒）就会通过。
    ok, data, waited = settle_msg_count(p, sid)

    # ---- 归属门禁：绑定的是「哪一条对话」，以及它是不是本项目的 ----
    # 2026-10-29 事故：直接绑了浏览器里用户正在聊的那条 Gemini 对话（10 条消息），
    # 程序只对「空对话」打了警告，非空直接放行 —— 项目上下文、发送目标、锚点基线
    # 全部挂到了别人的对话上。判据与口径统一在 platforms.bind_verdict。
    bv = platforms.bind_verdict(p, url, data, st,
                                new_chat=new_chat, reuse_existing=reuse_existing)
    if not bv["ok"]:
        print(json.dumps({"ok": False, "code": bv["code"], "unbound": True,
                          "msg_count": bv["msg_count"], "conv_id": bv["conv_id"],
                          "reason": bv["reason"], "hint": bv["hint"]},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    # 来源标记：这条对话当初是怎么进项目的（后续 resume 靠它自证清白）
    origin_map = {"new_chat": "new_chat", "empty_conv": "empty", "adopted": "adopted"}
    st.update({"url": data["url"], "conv_id": bv["conv_id"],
               "conv_origin": origin_map.get(bv["code"], st.get("conv_origin") or ""),
               "bound_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "awaiting_reply": False, "turn": 0, "history": [],
               "new_chat": new_chat})
    touch_lease(st, path)
    _wv = window_verified(st, path, sid)  # 三态判定（含重试与自愈），写回 state
    save_state(path, st)
    print(json.dumps({"ok": True, "bound": st["url"], "platform": p["key"],
                      "window": {"verified": _wv},
                      "msg_count": data["msg_count"], "session": sid,
                      "new_chat": new_chat, "conv_id": bv["conv_id"],
                      "bind_code": bv["code"], "warn": bv["warn"],
                      "settle_waited_s": round(waited, 1),
                      "nav_note": nav_note},
                     ensure_ascii=False, indent=2))
    if bv["warn"]:
        print("注意：" + bv["warn"], file=sys.stderr)
    return 0


@with_platform_lock
def cmd_clear(a) -> int:
    path = state_path(a.state)
    st = load_state(path)
    touch_lease(st, path)  # 续租：agent 活着，窗口不收
    p = platforms.get(a.platform or st.get("platform"))
    # clear 也是页面写路径（打错窗口 = 清掉用户正在输入的内容），同受窗口门约束
    # （GPT 复审阶段一：受保护操作 = 向页面写入，不止 send）。
    _wok, _why = require_window_verified(st, path, st.get("session") or "")
    if not _wok:
        print("WINDOW_UNVERIFIED：窗口归属未确认，拒绝清空。\n  " + _why, file=sys.stderr)
        return 6
    sid = ensure_session(st, path)
    res = clear_editor(p, sid)
    res["platform"] = p["key"]
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 6


def cmd_probe(a) -> int:
    path = state_path(a.state)
    st = load_state(path)
    touch_lease(st, path)  # 续租：agent 活着，窗口不收
    p = platforms.get(a.platform or st.get("platform"))
    data = probe(st, path, p)
    if st.get("url") and not data["url"].startswith(st["url"].split("?")[0][:60]):
        data["url_mismatch"] = True
        data["bound_url"] = st["url"]
    data["platform"] = p["key"]
    data["platform_verified"] = p["verified"]
    # 对话身份与「绑定时刻」比对：地址前缀能被 /app → /app/<hex> 这类跳转绕过，
    # conv_id 不会 —— 它直接回答「这是不是同一条对话」。
    data["conv_id"] = platforms.conv_id_of(p, data.get("url") or "")
    if st.get("conv_id") and data["conv_id"] and data["conv_id"] != st["conv_id"]:
        data["conv_mismatch"] = True
        data["bound_conv_id"] = st["conv_id"]
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def cmd_platforms(a) -> int:
    print(json.dumps(platforms.list_platforms(), ensure_ascii=False, indent=2))
    return 0


@with_platform_lock
def cmd_send(a) -> int:
    path = state_path(a.state)
    st = load_state(path)
    touch_lease(st, path)  # 续租：agent 活着，窗口不收
    p = platforms.get(a.platform or st.get("platform"))
    # ---- ORCHESTRATOR_REQUIRED 熔断（2026-10-31 服从性审计新增）----
    # 工作区里存在编排 run（state/current）时，直接 CLI 发送 = 绕过写闸门与
    # journal，属硬规则违例。必须走 orchestrator.py loop（→ write_gate 出
    # 授权单 → 真实写 → 核验）。
    # 逃生门（2026-10-31 DeepSeek 裁定改为「有代价的显式声明」，0.1.4）：
    #   · 单次消费——env 值即理由，读后立即清除，同进程第二次直发仍被拒；
    #   · 理由必填——env 为空值视为未声明，拒绝；
    #   · 强制落账——放行时在未清项台账上**累加**该目标的 P0-ESC 条目
    #     （2026-11-03 改为按目标聚合：一次一发一个 id 会让台账按发送次数增长，
    #      而回执门要求每条都出现 → 回执越长越容易不合格 → 越不合格越发不走正路，
    #      24 小时能攒出 32 条。留痕不变，计数与最近理由都在条目里。）
    #     该条目必须在回执三栏「违例/异常」栏出现（pending_gate.check_receipt 强制），
    #     报备过一次的条目用 pending_gate.py settle 核销，不再逐轮复读。
    #   · 本逃生门只豁免「编排器熔断」，不豁免其后的窗口归属三态门——
    #     即使直发放行，window unknown/misplaced 照样 rc=6（复审阶段一盘点结论）。
    root = os.path.dirname(path) or "."
    cur_ptr = os.path.join(root, "state", "current")
    if os.path.exists(cur_ptr):
        _esc = os.environ.get("STARPILOT_ALLOW_DIRECT_SEND", "")
        if not _esc.strip():
            print("ORCHESTRATOR_REQUIRED：本工作区有编排 run（state/current 存在），"
                  "禁止用 wa.py send 直接发送（会绕过写闸门/journal/幂等守卫）。\n"
                  "  正路：python scripts/orchestrator.py loop --once --file <反馈文件>\n"
                  "  逃生门（单次消费，理由必填）：STARPILOT_ALLOW_DIRECT_SEND='<理由>' 重跑；"
                  "放行会落账 P0-ESC 违例条目，必须在回执三栏报备", file=sys.stderr)
            return 7
        del os.environ["STARPILOT_ALLOW_DIRECT_SEND"]  # 单次消费：用完即焚
        try:
            import pending_gate as _pg
            _led = _pg.pending_path_for(root, "", "")
            _tgt = hashlib.sha256((st.get("url") or "").encode("utf-8")).hexdigest()[:12]
            _rec = _pg.record_escape(_led, _tgt, _esc.strip())
            print("违例放行（单次）：本次发送绕过编排器，台账 %s 条目 %s（累计 %d 次）。"
                  "该条目必须在回执三栏「违例/异常」栏报备一次；报备后可用 "
                  "pending_gate.py settle 核销，不必每轮复读。"
                  % (_led, _rec["id"], _rec["count"]), file=sys.stderr)
        except Exception as _esc_err:
            print("警告：逃生门落账失败（%s）——违例未留痕，请人工补记。" % _esc_err,
                  file=sys.stderr)
    if not st.get("url"):
        print("尚未绑定对话：先跑 wa.py bind <url>", file=sys.stderr)
        return 2

    # ---- 窗口归属三态门（2026-11-02 GPT 审计 P0：null 不得放行）----
    # send 是写路径：打错窗口 = 往用户正在用的窗口打字。归属 unknown/misplaced
    # 一律拒绝，逃生阀已删除（复审阶段一裁定）——人工接管走 wa.py window release。
    _wok, _why = require_window_verified(st, path, st.get("session") or "")
    if not _wok:
        print("WINDOW_UNVERIFIED：窗口归属未确认，拒绝发送。\n  " + _why, file=sys.stderr)
        return 6

    text = a.text or ""
    if a.file:
        with open(a.file, encoding="utf-8") as f:
            text = f.read()
    text = text.strip()
    if not text:
        print("内容为空，拒绝发送", file=sys.stderr)
        return 2

    if st.get("awaiting_reply") and not a.force:
        print("上一轮已发送但尚未确认回复完成。先跑 wa.py wait，确认后再发；确有需要加 --force。",
              file=sys.stderr)
        return 3

    # ---- 内容幂等（2026-11-03）：同一条正文不许在没结论之前再发一遍 ----
    # 为什么 CLI 层也要有：闸门层（write_gate._already_sent）只在走正路时生效，
    # 而「发出去但没确认」恰恰最容易转成 agent 自己重敲一次 wa.py send。
    # 实测过这条路：文本滞留在输入框 → 判未发出 → 重发 → 页面上两条一样的提问。
    # 判据是 内容哈希 + conv_id（同一条对话才算重复），跨对话不算。
    text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    if (not a.force and st.get("last_send_hash") == text_sha
            and (st.get("conv_id") or "") == (st.get("last_send_conv") or "")
            and st.get("last_send_at")):
        print("这条正文已经发过一次（%s，hash=%s），本轮还没确认回复完成。\n"
              "  先跑：python scripts/wa.py wait --timeout 900   # 等回复\n"
              "  或：  python scripts/wa.py triage                # 判卡在哪\n"
              "  确实要再发一遍（比如明知没送达）：加 --force 并说明理由。"
              % (st.get("last_send_at"), text_sha), file=sys.stderr)
        return 9

    # ---- 「发出去了但没确认」的滞留态：禁止盲目重敲 ----
    # 上一次点击后编辑器已清空、却没有任何主信号证明送达（verdict=INCONCLUSIVE_*）：
    # 这时候再 send 一次，极可能就是第二条相同提问。
    if st.get("maybe_sent") and not a.force:
        mb = st.get("maybe_sent") or {}
        print("上一轮判定为「疑似已发出但未确认」（%s，%s）。\n"
              "  直接重发会造成同一条提问发两遍。先跑 wa.py triage 看页面事实，"
              "确认没送达再带 --force 重发。"
              % (mb.get("verdict") or "?", mb.get("at") or "?"), file=sys.stderr)
        return 9

    # 写路径白名单：平台没实测过写入路径就**不许真实写**。
    # 闸门层（write_gate）一直有这条；CLI 层以前没有 —— 于是「绕过闸门直接 wa.py send」
    # 可以写到不该写的地方。两层的写权限必须**同源**（都走 platforms.write_path_ok），
    # 否则白名单只是装饰，而且两层会各说一套、互相甩锅。
    if not platforms.write_path_ok(p):
        print("平台 %s 的写入路径 %r 不具备真实写条件：拒绝真实写。\n"
              "  白名单=%s\n"
              "  标定=%s\n"
              "  要放开这条：按 references/web-ai-platforms.md 实测写路径并回填 "
              "platforms.py 的 write_verified/write_verified_at，别改这里的判断。"
              % (p.get("key"), p.get("write_method"),
                 list(p.get("write_allowed") or []) or "（空）",
                 platforms.calibration_gap(p)),
              file=sys.stderr)
        return 8

    sid = ensure_session(st, path)
    ok, cur = read_state(p, sid)
    if not ok:
        print("发送前探测失败：%s" % cur, file=sys.stderr)
        return 4
    if cur.get("generating"):
        print("上一轮仍在生成，拒绝发送（会造成重复提问）。", file=sys.stderr)
        return 3
    if cur.get("modal"):
        # 平台弹窗/遮罩会挡住真实点击（实测 Kimi 限流提示）。
        # 这里不硬发——先让上层关掉弹窗（关不掉就如实报平台侧阻塞）。
        print("页面上有弹窗/遮罩，拒绝发送：%s（%s）\n"
              "先关掉弹窗再重试（Kimi: bsk click --selector \"[data-testid='confirm-dialog-cancel']\"）。"
              % (cur.get("modal_text") or "（无文案）", cur.get("modal_rect") or "尺寸未知"),
              file=sys.stderr)
        return 3
    if cur.get("editor_len", 0) > 0 and not a.force:
        print("输入框里已有 %d 字未清空，先确认那不是上一条残留。" % cur["editor_len"], file=sys.stderr)
        return 3

    before_msgs = cur.get("msg_count", 0)
    bound_url = st.get("url") or ""

    # ---- 等线程历史加载完成（否则 before_msgs=0 会把后续加载误判为发送成功）----
    # 新对话本来就没有历史，不用等
    _waited = 0
    while before_msgs == 0 and _waited < 30 and not st.get("new_chat"):
        time.sleep(3)
        _waited += 3
        ok, cur = read_state(p, sid)
        if not ok:
            break
        before_msgs = cur.get("msg_count", 0)
        if before_msgs == 0 and _waited >= 30:
            print("警告：30 秒内线程历史仍未加载（msg_count=0）。继续发送，但发送校验可能不可靠。",
                  file=sys.stderr)

    # ---- 写入（唯一实现在 do_fill；写没写进去由 platforms.fill_check 判）----
    fill = do_fill(p, text, sid)
    if not fill.get("ok"):
        print("写入未被编辑器接受（%s）：%s\n%s"
              % (fill.get("code"), fill.get("detail") or "",
                 "facts: %s" % json.dumps(fill.get("facts") or {}, ensure_ascii=False)),
              file=sys.stderr)
        if fill.get("code") == "SELECTOR_AMBIGUOUS":
            print("这是**选择器歧义**，不是页面坏了：收窄 platforms.py 里该平台的 "
                  "sel.editor 候选（ChatGPT 的正解是 `form div.ProseMirror`，"
                  "裸 div.ProseMirror 会命中多个同名节点）。"
                  "反复重试同一条命令不会变好。", file=sys.stderr)
            return 10
        return 6 if fill.get("code") in (platforms.FILL_SHORT, platforms.FILL_BLANK,
                                         platforms.FILL_NO_SEND) else 5
    ok, cur = read_state(p, sid)
    if not ok:
        print("写入后探测失败：%s" % cur, file=sys.stderr)
        return 5

    # ---- 发送（唯一分派 do_send；send_method 由平台定义）----
    ok, res = do_send(p, sid)
    if not ok:
        print("发送失败（%s）：%s" % (p.get("send_method"), res), file=sys.stderr)
        return 7
    st["last_send_msg_count"] = before_msgs

    # ---- 校验「真的发出去了」----
    # 三选一即算发出：消息数增加 / 地址变了（新对话首轮平台会跳到真实会话地址）/
    # **我发的那段文字真的出现在页面上**。
    # 刻意**不**把「输入框清空」当成功信号：实测 Kimi 免费额度繁忙时会清空输入框
    # 却拒绝创建会话（弹限流提示），拿它当成功 = 谎报，下一轮就会对着不存在的会话继续等。
    #
    # 判定**不在本文件实现**：唯一实现在 confirm.classify_send_result，
    # 闸门层（write_gate）调的是同一个函数。历史上这里另写了一套，口径漂移出过「假重发」
    # （references/lessons.md L-KIMI-DELTA-METRIC-SCOPE）。
    before_snap = confirm.trim(cur)
    # 发送前基线：等回复时判「页面动没动」全靠它。
    # 存在 state 里而不是只在内存里，是为了让**旁观者**也能判——
    # 卡住之后人和 orchestrator 跑的 `wa.py triage` 没有这个基线就只能靠猜。
    send_base = _base_snapshot(cur, st, p)
    signals = []
    after_snap = {}
    for _ in range(20):
        time.sleep(1.5)
        ok, cur3 = read_state(p, sid)
        if not ok:
            continue
        cur = cur3
        url_changed = bool(before_snap.get("url")) and (cur3.get("url") or "") != before_snap.get("url")
        msgs_up = (cur3.get("msg_count") or 0) > (before_snap.get("msg_count") or 0)
        if msgs_up and "msg_count" not in signals:
            signals.append("msg_count")
        if url_changed and "url" not in signals:
            signals.append("url")
        if msgs_up or url_changed:
            after_snap = confirm.trim(cur3)
            break
        ok2, nd = evaluate(platforms.js_has_text(p, text), sid)
        if ok2 and isinstance(nd, dict) and nd.get("ok"):
            signals.append("needle")
            after_snap = confirm.trim(cur3)
            break
    if not after_snap:
        after_snap = confirm.trim(cur)
    clas = confirm.classify_send_result(before_snap, after_snap, signals)

    if not clas["delivered"]:
        # 两种失败必须分开说，因为它们接下来该做的事**相反**：
        #   NOT_SENT               输入框还有字 → 没发出去，清掉重发是安全的
        #   INCONCLUSIVE_NOT_SUCCESS 输入框空了但没有主信号 → **可能已经发出去了**，
        #                            盲目重发就是第二条相同提问（实测踩过）
        unconfirmed = clas["verdict"] == confirm.INCONCLUSIVE_NOT_SUCCESS
        if unconfirmed:
            st["maybe_sent"] = {"verdict": clas["verdict"],
                                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "signals": clas["signals"],
                                "conv_id": st.get("conv_id") or "",
                                "text_sha": text_sha}
            save_state(path, st)
        print("点击后未确认消息真的发出（信号：%s）。判定=%s：%s\n%s"
              % ("、".join(clas["signals"]) or "无", clas["verdict"], clas["reason"],
                 ("⚠️ 输入框已清空 = 可能真的发出去了。**不要直接重试**："
                  "先 python scripts/wa.py triage 看页面事实，"
                  "确认没送达再带 --force 重发。" if unconfirmed else
                  "输入框仍有内容 = 确实没发出去。先 wa.py probe 看状态/弹窗，"
                  "再清空重发。")),
              file=sys.stderr)
        return 9 if unconfirmed else 8

    # ---- 新对话首轮：平台已跳到真实会话地址，把 state 换成它，后续轮次据此校验 ----
    if st.get("new_chat"):
        real = cur.get("url") or ""
        if real and real != st.get("url"):
            st["url"] = real
        cid = platforms.conv_id_of(p, real)
        if cid:  # 首轮发出后才有会话身份；记下来，后面改绑别的对话会被门禁拦住
            st["conv_id"] = cid
            st["conv_origin"] = "new_chat"  # 最干净的来源：本项目从新对话入口开出来的
        st["new_chat"] = False

    st["turn"] = int(st.get("turn") or 0) + 1
    st["last_send_hash"] = text_sha
    st["last_send_conv"] = st.get("conv_id") or ""   # 幂等判据的归属：同一条对话才算重复
    st["last_send_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    st["awaiting_reply"] = True
    st.pop("maybe_sent", None)   # 本轮已有结论，滞留标记清掉（否则下一轮被误拦）
    send_base["conv_id"] = send_base.get("conv_id") or st.get("conv_id") or ""
    st["send_base"] = send_base
    st.setdefault("history", []).append({
        "turn": st["turn"], "at": st["last_send_at"],
        "hash": st["last_send_hash"], "chars": len(text),
        "verdict": clas["verdict"], "delta_source": clas["delta_source"],
        "effective_delta": clas["effective_delta"],
    })
    save_state(path, st)
    print(json.dumps({"ok": True, "turn": st["turn"], "chars": len(text),
                      "hash": st["last_send_hash"], "url": cur.get("url"),
                      "verdict": clas["verdict"],
                      "effective_delta": clas["effective_delta"],
                      "delta_source": clas["delta_source"],
                      "signals": clas["signals"],
                      # 下一步该敲什么，写在机器输出里而不是等人猜：
                      # SENT_NO_REPLY 时 agent 的本能反应是「再发一次」，那正是重复提问的来源
                      "next": ("python scripts/wa.py wait --timeout %d"
                               % (int(p.get("timeout_s") or 900))
                               if clas["verdict"] != confirm.SENT_NO_REPLY else
                               "python scripts/wa.py wait --timeout %d；"
                               "45 秒内没首字会自动转分诊（wa.py triage），"
                               "**不要重发**——发出去已确认，只是回复还没认出来"
                               % (int(p.get("timeout_s") or 900)))},
                     ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------- 观察文件
# 「它到底卡在哪」这件事，不该只有当事人（agent 自己）知道。
# state 文件参与状态机、要持锁、只在真实状态变化时写；这个观察文件相反：
# 不持锁、每次探测都刷新、坏了也不抛——它是给旁观者（人、另一个 agent、
# 定时任务、orchestrator）看的事实，用来回答「多久没进展、卡在哪一步」。
PROGRESS_SUFFIX = ".progress.json"


def progress_path(path: str) -> str:
    return path + PROGRESS_SUFFIX


def write_progress(path: str, stage: str, cur: dict | None = None,
                   progressed: bool = False) -> None:
    """刷新观察文件。`progressed=True` 表示「页面确实变了」（不是单纯又探了一次）。"""
    rec = {}
    try:
        with open(progress_path(path), encoding="utf-8") as f:
            rec = json.load(f) or {}
    except Exception:
        rec = {}
    now = time.time()
    rec.setdefault("started_at", now)
    rec["stage"] = stage
    rec["probe_at"] = now
    if progressed or "progress_at" not in rec:
        rec["progress_at"] = now
    if cur is not None:
        rec["seen"] = {k: cur.get(k) for k in
                       ("msg_count", "reply_count", "last_len", "doc_len",
                        "doc_nodes", "generating", "editor_len")}
    try:
        tmp = progress_path(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
        os.replace(tmp, progress_path(path))
    except Exception:
        pass  # 观察文件坏了不影响主流程：它是给旁观者看的，不是判据来源


def read_progress(path: str) -> dict:
    try:
        with open(progress_path(path), encoding="utf-8") as f:
            rec = json.load(f) or {}
    except Exception:
        return {}
    now = time.time()
    if rec.get("probe_at"):
        rec["probe_age_s"] = round(now - rec["probe_at"], 1)
    if rec.get("progress_at"):
        rec["progress_age_s"] = round(now - rec["progress_at"], 1)
    return rec


# verdict → 退出码。人看 hint，机器看 rc：两者都要稳定，才能被上层直接分支。
# 注意码段：9 已经是「平台占用锁被占」，别撞。10 起是分诊结论。
STALL_RC = {
    platforms.STALL_RISK: 10,     # 风控/验证码/限流文案：需要人
    platforms.STALL_LOGIN: 10,    # 掉登录态：需要人
    platforms.STALL_DRIFT: 11,    # 对话漂移：rebind
    platforms.STALL_DEAD: 12,     # 假生成：流死了
    platforms.STALL_STALE: 13,    # 选择器失效：复探/回填
    platforms.STALL_NOT_SENT: 14,  # 发送没生效
    platforms.STALL_SILENT: 15,   # 平台侧静默拒绝
}


def _base_snapshot(cur: dict, st: dict, p: dict) -> dict:
    """发送后的基线：判「页面动没动」只能跟它比。"""
    return {
        "msg_count": cur.get("msg_count") or 0,
        "reply_count": cur.get("reply_count") or 0,
        "doc_len": cur.get("doc_len") or 0,
        "doc_nodes": cur.get("doc_nodes") or 0,
        "editor_len": cur.get("editor_len") or 0,
        "conv_id": st.get("conv_id") or platforms.conv_id_of(p, cur.get("url") or ""),
    }


def triage(p: dict, st: dict, cur: dict, base: dict, *, unchanged_s: float,
           fake_gen_s: float = platforms.FAKE_GEN_DEFAULT_S) -> dict:
    """把探针事实翻成「卡在哪、下一步做什么」。判定口径全在 platforms.judge_stall。"""
    out = platforms.judge_stall(p, cur, base, unchanged_s=unchanged_s,
                                fake_gen_s=fake_gen_s)
    out["stage"] = out["verdict"]
    out["rc"] = STALL_RC.get(out["verdict"], 5)
    out["url"] = cur.get("url") or ""
    return out


def cmd_wait(a) -> int:
    """等回复完成。三个时钟，不是一个总超时。

    · 首字时钟（--first-token，默认按平台 ttft_s）：发送已确认后多久内必须看到
      「在生成」或首字。过了就走分诊——正常路径几秒就有动静，长时间没动静
      大概率是风控/没发出去/选择器失效，而不是「模型在思考」。
    · 停滞时钟（--stall）：已经在生成但页面连续多久毫无变化。停止按钮还在却
      一个字不动 = 假生成（流死了），继续等只是把 15 分钟烧掉。
    · 总时钟（--timeout）：兜底上限，保持 900 秒不变。

    分诊不动 DOM、不刷新、不重发：它只读页面然后把结论说清楚。
    真正要动（reload / rebind / 重发）由上层按 stage 决定——判和做分开，
    才不会出现「一着急就刷新，把正在生成的回复弄丢」。
    """
    path = state_path(a.state)
    st = load_state(path)
    touch_lease(st, path)  # 续租：agent 活着，窗口不收
    p = platforms.get(a.platform or st.get("platform"))
    sid = ensure_session(st, path)
    ttft_s = float(getattr(a, "first_token", 0) or p.get("ttft_s") or 45)
    stall_s = float(getattr(a, "stall", 0) or p.get("stall_s") or 60)
    t0 = time.time()
    deadline = t0 + a.timeout
    stable = 0
    prev_len = -1
    started = False
    recovered = 0
    max_recover = int(getattr(a, "max_recover", 1) or 0)
    notes = []
    base = None
    last_change = t0
    prev_sig = None
    ttft_done = False
    stall_done = False
    slow_warned = False

    def finish(payload: dict, rc: int) -> int:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return rc

    def bail(tri: dict) -> int:
        """分诊结论是「别再等了」：把事实和下一步一起交出去。"""
        write_progress(path, tri["stage"], None)
        payload = {"ok": False, "stage": tri["stage"], "rc": tri["rc"],
                   "waited_s": round(time.time() - t0, 1),
                   "hint": tri["hint"], "evidence": tri.get("evidence"),
                   "risk": tri.get("risk", ""), "recovered": recovered,
                   "notes": notes, "url": tri.get("url")}
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return tri["rc"]

    while time.time() < deadline:
        ok, cur = read_state(p, sid)
        if not ok:
            # 判「会话没了」不该等三次：read_state 一次就要 4 秒（滚动 + 三轮探测），
            # 累计三次近 20 秒，长任务里这 20 秒会白等；`session list` 一次只要几十毫秒。
            # 「死」是硬事实（session list 里没有），立刻处理；「活着但抖动」才等下轮再试。
            write_progress(path, "probe_failed")
            state = session_state(sid)
            if state == "unknown":
                # 看不清就**不重开窗口**：renew 的代价是一扇新窗 + 一条新会话，
                # 而真实情况可能只是 bsk 在 JSON 前多打了一行提示。
                notes.append("第 %d 轮探测失败且会话状态查不清（bsk 输出不可信）："
                             "按「还活着」继续等，不开新窗。要排查请跑 wa.py triage"
                             % (recovered + 1))
                continue
            if state == "dead":
                if recovered < max_recover:
                    # bsk 会按空闲（默认 5 分钟）回收窗口。窗口没了不等于对话没了：
                    # 回复在平台侧已持久化，判据是「同一个 url 还能重新绑定」，
                    # 不是「距上次操作不到 5 分钟」这种被动时间。
                    sid2 = ensure_session(st, path, renew=True)
                    notes.append("第 %d 次续窗：会话已被回收，在原对话地址重开（%s → %s）"
                                 % (recovered + 1, sid, sid2))
                    sid = sid2
                    recovered += 1
                    started = False
                    prev_len = -1
                    last_change = time.time()
                    continue
                print(json.dumps({"ok": False, "stage": "session_lost",
                                  "session": sid, "recovered": recovered,
                                  "notes": notes,
                                  "hint": "窗口已不存在且续窗次数用尽：先 wa.py doctor 看实例与会话，"
                                          "再用 bind <同一个 url> 接上；不要 --new，"
                                          "回复和上下文都在原对话里。"},
                                 ensure_ascii=False, indent=2), file=sys.stderr)
                write_progress(path, "session_lost")
                return 6
            time.sleep(a.interval)
            continue

        if cur.get("error"):
            print(json.dumps({"ok": False, "stage": "page_error", "detail": cur["error"],
                              "url": cur.get("url")}, ensure_ascii=False, indent=2))
            write_progress(path, "page_error", cur)
            return 4

        if base is None:
            # 优先用 send 时刻落盘的基线：它才是「发送前」的样子。
            # 没有就退化成第一次探测（此时回复可能已经开始，判「页面动没动」会偏保守）。
            base = st.get("send_base") or _base_snapshot(cur, st, p)

        sig = (cur.get("msg_count"), cur.get("reply_count"), cur.get("last_len"),
               cur.get("doc_len"), cur.get("doc_nodes"), bool(cur.get("generating")))
        changed = sig != prev_sig
        if changed:
            last_change = time.time()
        prev_sig = sig

        if cur.get("msg_count", 0) > int(st.get("last_send_msg_count") or 0) or cur.get("generating"):
            started = True

        stage = "waiting"
        if started:
            stage = "generating" if cur.get("generating") else "settling"
        write_progress(path, stage, cur, progressed=changed)

        # ---- 首字时钟：过了还没动静，就走分诊，不要空等到总超时 ----
        age = time.time() - t0
        if not started and not ttft_done:
            if age >= ttft_s * 0.55 and not slow_warned:
                slow_warned = True
                print("首字迟迟未到（%.0fs，硬阈值 %.0fs）：再做一轮判定，"
                      "正常路径 2–5 秒就该有动静。" % (age, ttft_s), file=sys.stderr)
            if age >= ttft_s:
                ttft_done = True
                tri = triage(p, st, cur, base, unchanged_s=time.time() - last_change)
                # 「还在生成/回复已到」= 我们的时钟算早了，继续按原逻辑走，不动页面。
                if tri["verdict"] in (platforms.STALL_ALIVE, platforms.STALL_READY):
                    notes.append("首字时钟到点但判定为 %s，继续等" % tri["verdict"])
                else:
                    return bail(tri)

        # ---- 停滞时钟：说在生成，但页面连续不动 ----
        if started and cur.get("generating") and not stall_done:
            if time.time() - last_change >= stall_s:
                stall_done = True
                tri = triage(p, st, cur, base, unchanged_s=time.time() - last_change)
                if tri["verdict"] in (platforms.STALL_ALIVE, platforms.STALL_READY):
                    notes.append("停滞时钟到点但判定为 %s，继续等" % tri["verdict"])
                    last_change = time.time()
                    stall_done = False
                else:
                    return bail(tri)

        if started and not cur.get("generating") and cur.get("last_len", 0) > 0:
            if cur.get("regen"):
                # 硬信号：末条回复的操作按钮已出现 = 本轮生成结束；
                # 再要求文本长度连续两次一致，防虚拟化渲染抖动
                if cur["last_len"] == prev_len:
                    stable += 1
                    if stable >= 1:
                        st["awaiting_reply"] = False
                        st["fail_streak"] = 0
                        st["last_progress_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                        save_state(path, st)
                        write_progress(path, "done", cur, progressed=True)
                        return finish({"ok": True, "chars": cur["last_len"],
                                       "msg_count": cur.get("msg_count"),
                                       "signal": "regen",
                                       "recovered": recovered, "notes": notes,
                                       "tail": cur.get("last_tail", "")}, 0)
                else:
                    stable = 0
            elif cur["last_len"] == prev_len:
                stable += 1
                if stable >= 2:
                    st["awaiting_reply"] = False
                    st["fail_streak"] = 0
                    st["last_progress_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    save_state(path, st)
                    write_progress(path, "done", cur, progressed=True)
                    return finish({"ok": True, "chars": cur["last_len"],
                                   "msg_count": cur.get("msg_count"),
                                   "signal": "text_stable",
                                   "recovered": recovered, "notes": notes,
                                   "tail": cur.get("last_tail", "")}, 0)
            else:
                stable = 0
        else:
            stable = 0

        prev_len = cur.get("last_len", 0) if not cur.get("generating") else -1
        time.sleep(a.interval)

    # 总超时：仍然是兜底，但要把「最后看到的现场」一起交出去，而不是只说超时。
    tri = None
    try:
        ok, cur = read_state(p, sid)
        if ok and base is not None:
            tri = triage(p, st, cur, base, unchanged_s=time.time() - last_change)
    except Exception:
        tri = None
    write_progress(path, "timeout")
    payload = {"ok": False, "stage": "timeout", "waited_s": a.timeout,
               "recovered": recovered, "notes": notes,
               "progress": read_progress(path),
               "hint": "超时不等于失败：再跑一次 wait，或先 triage 看卡在哪一步"}
    if tri:
        payload["triage"] = {k: tri.get(k) for k in ("stage", "hint", "evidence")}
    return finish(payload, 5)


def cmd_read(a) -> int:
    path = state_path(a.state)
    st = load_state(path)
    touch_lease(st, path)  # 续租：agent 活着，窗口不收
    p = platforms.get(a.platform or st.get("platform"))
    sid = ensure_session(st, path)

    def read_once(s: str):
        last_err = ""
        for ms in (600, 1200, 2500):
            evaluate(platforms.js_scroll(p), s)
            time.sleep(ms / 1000.0)
            ok, data = evaluate(js_read_last(p), s)
            if ok and data.get("text"):
                return data["text"], ""
            last_err = str(data)
        return "", last_err

    text, err = read_once(sid)
    if not text and not session_alive(sid):
        # 窗口被回收后 reply 仍在平台侧：重开同一对话再读，比「读取失败」诚实得多
        sid = ensure_session(st, path, renew=True)
        text, err = read_once(sid)
        if text:
            print("会话已被回收，在原对话地址重开后读回。", file=sys.stderr)
    if not text:
        print("读取失败：重试 3 次仍未取到回复文本（%s）" % (err or "")[:200], file=sys.stderr)
        return 5
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print(json.dumps({"ok": True, "chars": len(text), "out": a.out}, ensure_ascii=False))
    else:
        print(text)
    return 0


def cmd_doctor(a) -> int:
    """环境体检：实例、重复扩展、会话、state 摘要。

    存在的意义：所有「读不到回复」最后都落在四件事上 —— 实例重了、实例掉线了、
    会话没了、state 指向别的地址。做成一条命令，是为了让 agent 在重试之前先看一眼，
    而不是再开一个窗口去赌。
    """
    rep = instance_report()
    sessions = [{k: s.get(k) for k in ("id", "browser", "window")} for s in list_sessions()]
    path = state_path(getattr(a, "state", None))
    st = load_state(path)
    sid = st.get("session") or ""
    wr = window_report(st, sid) if sid and not _transport_is_iab() else None
    left = lease_left(st) if st.get("lease_until") else None
    out = {
        "ok": rep["healthy"],
        "browsers": rep,
        "sessions": sessions,
        "state": {k: st.get(k) for k in ("platform", "url", "conv_id", "session",
                                         "browser", "awaiting_reply", "turn",
                                         "window_kind", "window_verified")},
        "window": wr,
        "lease": ({"left_s": int(left), "idle_close_s": idle_close_s(),
                   "reaper": "wa.py reap --watch"} if left is not None else None),
        "state_path": path,
        # 卡住的可见性：state 有锁、只在状态变化时写，观察文件每次探测都刷。
        # 两者都给出去，人才分得清「卡在探测」和「卡在没回复」。
        "progress": read_progress(path),
    }
    if wr and wr.get("ok") is False:
        out["hint"] = ("绑定页面开在**用户的窗口**而不是本会话的 Agent Window："
                       "多半是扩展 Automation 设置为「共享标签页」或扩展版本 <0.4.0。"
                       "先更新扩展 / 把 Automation 设置改为允许独立 Agent Window；"
                       "自愈尝试结果见 window.heal。") + (" 已尝试自愈：%s" % wr["heal"] if wr.get("heal") else "")
    if rep["duplicates"]:
        out["hint"] = duplicate_hint(rep)
    if sid and left is not None and left <= 0:
        out["hint"] = (out.get("hint", "") + " 租约已过期但窗口还挂着：启动收割器 "
                       "`python scripts/wa.py reap --once`（常驻：--watch）。").strip()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["ok"] else 6


def cmd_triage(a) -> int:
    """「它到底卡在哪」——一条只读命令，任何人都能在卡住时跑。

    存在的理由：卡住的现场只有 agent 自己知道，人只能看着不动。把判定做成
    **只读 + 结构化**，人才有话说、另一个 agent 才能接手、定时任务才能报警。
    它不刷新、不重发、不关窗口：只读页面 + 读观察文件 + 给结论。
    """
    path = state_path(a.state)
    st = load_state(path)
    touch_lease(st, path)  # 续租：agent 活着，窗口不收
    p = platforms.get(a.platform or st.get("platform"))
    sid = ensure_session(st, path)
    ok, cur = read_state(p, sid)
    if not ok:
        print(json.dumps({"ok": False, "stage": "probe_failed", "detail": str(cur),
                          "hint": "页面探不通：先 wa.py doctor 看会话与实例；"
                                  "会话没了不是对话没了，bind 同一个 url 接上。"},
                         ensure_ascii=False, indent=2))
        return 4

    prog = read_progress(path)
    base = st.get("send_base") or _base_snapshot(cur, st, p)
    unchanged = float(prog.get("progress_age_s") or 0.0)
    tri = triage(p, st, cur, base, unchanged_s=unchanged)
    out = {
        "ok": tri["verdict"] in (platforms.STALL_ALIVE, platforms.STALL_READY),
        "stage": tri["stage"],
        "hint": tri["hint"],
        "evidence": tri["evidence"],
        "risk": tri.get("risk", ""),
        "url": tri.get("url"),
        "progress": prog,
        "state": {k: st.get(k) for k in ("platform", "conv_id", "awaiting_reply",
                                         "turn", "last_send_at")},
        "platform": p["key"],
        "platform_verified": p["verified"],
    }
    # 兜底读取：选择器失效时，页面文本其实读得到——这一步只读，不刷新页面。
    # 刷新是最后手段（会杀掉正在生成的回复），而且刷新后必须重走发送流程，
    # 所以默认不做，要人显式加 --reload。
    if getattr(a, "relocate", False):
        ok2, nd = evaluate(platforms.js_read_last(p), sid)
        if ok2 and isinstance(nd, dict) and nd.get("text"):
            out["relocate"] = {"ok": True, "fallback": bool(nd.get("fallback")),
                               "chars": len(nd["text"]), "tail": nd["text"][-200:]}
            if nd.get("fallback"):
                out["relocate"]["note"] = ("备用读法能读到正文 → 平台的 messages 选择器失效，"
                                           "不是没回复。按 references/web-ai-platforms.md 回填选择器。")
        else:
            out["relocate"] = {"ok": False,
                               "note": "备用读法也读不到：页面确实没有可读正文。"}
    if getattr(a, "reload", False):
        # 只有确认不在生成态才允许：正在生成时刷新 = 把回复丢掉。
        if cur.get("generating"):
            out["reload"] = {"skipped": "页面仍在生成，刷新会丢回复"}
        else:
            ok3, _ = evaluate("location.reload()", sid, timeout=20)
            out["reload"] = {"done": bool(ok3),
                             "note": "刷新后必须重走 bind + send，不要假设原回复还在"}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    # 0 = 页面本身正常（还在生成 / 回复已到）；其余就是分诊码。
    # 别把「正常」也塞进失败码段——上层要能一眼分清「去点验证码」和「去读回复」。
    return 0 if out["ok"] else tri["rc"]


def cmd_state(a) -> int:
    path = state_path(a.state)
    st = load_state(path)
    holder = statelock.read_holder(path)
    st["_lock"] = holder or None
    st["_lock_free"] = not bool(holder)
    # 平台占用锁：这个平台当前是否被别的任务占着（多任务并发排查用）
    try:
        key = st.get("platform") or platforms.DEFAULT_PLATFORM
        ph = statelock.read_holder_named(platform_lock_path(key))
        st["_platform_lock"] = ph or None
        st["_platform_lock_free"] = not bool(ph)
    except Exception as e:
        st["_platform_lock"] = {"error": str(e)}
        st["_platform_lock_free"] = None
    print(json.dumps(st, ensure_ascii=False, indent=2))
    return 0


def cmd_unlock(a) -> int:
    """强制清掉残留的 state 锁（崩溃恢复用）。只删 .lock，不碰 state 本体。"""
    path = state_path(a.state)
    holder = statelock.read_holder(path)
    lp = statelock.lock_path_for(path)
    if not os.path.exists(lp):
        print(json.dumps({"ok": True, "unlocked": False, "reason": "没有锁文件"},
                         ensure_ascii=False, indent=2))
        return 0
    statelock.release(path, None)
    print(json.dumps({"ok": True, "unlocked": True, "was_holder": holder},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_plock(a) -> int:
    """平台占用锁：查看（缺省）/ 强拆（--release）。给「多任务并发」场景用。"""
    d = os.environ.get(LOCK_DIR_ENV) or os.path.join(
        os.path.expanduser("~"), ".starpilot", "locks")
    if a.release:
        if not a.pplatform:
            print("--release 必须配合 --platform 指明要拆哪把锁", file=sys.stderr)
            return 2
        lp = platform_lock_path(a.pplatform)
        holder = statelock.read_holder_named(lp)
        statelock.release_named(lp, None)  # pid 校验对不上时不会误删
        still = os.path.exists(lp)
        print(json.dumps({"ok": not still, "lock": lp, "was_holder": holder or None,
                          "released": not still}, ensure_ascii=False, indent=2))
        return 0 if not still else 3
    # 查看模式：列锁目录里全部锁（或 --platform 指定的一把）
    if not os.path.isdir(d):
        print(json.dumps({"ok": True, "dir": d, "locks": []},
                         ensure_ascii=False, indent=2))
        return 0
    locks = []
    for name in sorted(os.listdir(d)):
        if a.pplatform and not name.startswith("lock-%s-" % a.pplatform):
            continue
        if not name.startswith("lock-") or not name.endswith(".json"):
            continue
        lp = os.path.join(d, name)
        h = statelock.read_holder_named(lp)
        h["alive"] = statelock._pid_alive(int(h.get("pid") or 0))
        h["lock_file"] = lp
        locks.append(h)
    print(json.dumps({"ok": True, "dir": d, "locks": locks},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_stop(a) -> int:
    path = state_path(a.state)
    st = load_state(path)
    sid = st.get("session")
    if sid:
        bsk(["session", "stop", sid], timeout=60)
        winlease.drop_session(st.get("window_instance") or _instance_id(st),
                              sid, _win_owner(st, path), reason="wa.py stop")
        st["session"] = ""
        save_state(path, st)
        print("已停止 session %s" % sid)
    else:
        print("没有记录中的 session")
    # 停窗即释放本任务的窗口租约：让别人能立刻接手，不必等 TTL 过期
    inst = st.get("window_instance") or _instance_id(st)
    if winlease.release(inst, _win_owner(st, path), reason="wa.py stop"):
        print("已释放窗口租约（%s）" % inst)
    return 0


# ---------------------------------------------- builtin 后端守卫（0.1.6）
#
# builtin 后端 = 脚本不碰浏览器，发送/读取/分诊由 agent 用自己的浏览器工具按
# references/builtin-mode.md 执行。只在 config 明确记了 builtin 时拦截——
# 「bsk 没装」走各命令自己的报错，不在这里替用户做决定。

BROWSER_CMDS = frozenset({"bind", "probe", "clear", "send", "wait", "read",
                          "triage", "doctor", "stop", "reap"})


def builtin_guard(cmd):
    """builtin 后端下浏览器类命令直接拒。返回拦截说明（str）或 None。"""
    if cmd not in BROWSER_CMDS:
        return None
    try:
        import env as _env  # 同目录，env.py 无副作用
        if _env.resolve_backend(env=os.environ) != "builtin":
            return None
    except Exception:
        return None  # 判不了就不拦，让命令自己走原有路径
    return ("builtin 浏览器后端已启用（%s）：wa.py 不再直接操控浏览器。\n"
            "本命令（%s）由 agent 用内置浏览器按规程手动执行：references/builtin-mode.md。\n"
            "要切回 bsk：python scripts/env.py --set-backend bsk && python scripts/env.py --install"
            % (_env.config_path(), cmd))


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="网页版 AI 循环桥接命令")
    ap.add_argument("--state", help="state 文件路径（默认 ./%s）" % STATE_NAME)
    ap.add_argument("--platform", help="网页 AI 平台 key（默认读 state，缺省 %s）" % platforms.DEFAULT_PLATFORM)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_pl(subp):
        # 允许 --platform / --state 写在子命令后面；SUPPRESS 避免子解析器用 None 覆盖主解析器的值
        subp.add_argument("--platform", default=argparse.SUPPRESS,
                          help="网页 AI 平台 key（%s）" % ", ".join(sorted(platforms.PLATFORMS)))
        subp.add_argument("--state", default=argparse.SUPPRESS,
                          help="state 文件路径（默认 ./%s）" % STATE_NAME)
        return subp

    p = sub.add_parser("platforms", help="列出支持的平台")
    p.set_defaults(func=cmd_platforms)

    p = add_pl(sub.add_parser("bind", help="绑定一个已有对话（地址形态随平台而异）"))
    p.add_argument("url", nargs="?", default="")
    p.add_argument("--new", action="store_true",
                   help="显式新建对话：只接受该平台的新对话入口（首页）。"
                        "仅用于循环第 1 轮；循环中途禁止新开。")
    p.add_argument("--reuse-existing", action="store_true", dest="reuse_existing",
                   help="显式接管一条**已有消息**的对话。默认拒绝：非本项目新建的对话"
                        "（msg_count>0）一律不绑，避免把项目挂到别人的对话上。"
                        "用了它就表示接受上下文不连续、锚点基线从此处起算。")
    p.set_defaults(func=cmd_bind)

    p = add_pl(sub.add_parser("probe", help="探测页面状态"))
    p.set_defaults(func=cmd_probe)

    p = add_pl(sub.add_parser("clear", help="清空输入框（不发送）：给「写完未发」的恢复态用"))
    p.set_defaults(func=cmd_clear)

    p = sub.add_parser("window", help="窗口归属：status 只读诊断 / release 显式人工接管")
    p.add_argument("window_cmd", choices=["status", "release"])
    p.add_argument("--confirm", default="",
                   help="release 必填：人工已确认的说明（谁确认、为什么释放）")
    p.set_defaults(func=cmd_window)

    p = sub.add_parser("tab-reap",
                       help="列出本会话 Agent Window 的陈旧标签（默认只报告）；"
                            "加 --apply 才真关。用户窗口标签永不触碰")
    p.add_argument("--apply", action="store_true",
                   help="真的关闭（默认只报告）。bsk 不提供标签归属元数据，"
                        "自动/默认关闭可能误关别的任务正在用的标签")
    p.add_argument("--dry-run", action="store_true", help="兼容旧用法，等同默认行为")
    p.set_defaults(func=cmd_tab_reap)

    p = add_pl(sub.add_parser("lease", help="窗口租约：status/list 只读；release 放自己的；"
                                             "takeover 显式接管（须 --confirm）"))
    p.add_argument("lease_cmd", choices=["status", "list", "release", "takeover"])
    p.add_argument("--instance", default="", help="浏览器实例 id（缺省按 state 解析）")
    p.add_argument("--confirm", default="", help="takeover 必填：谁授权、为什么对方已不在")
    p.set_defaults(func=cmd_lease)

    p = add_pl(sub.add_parser("send", help="发送一条消息"))
    p.add_argument("--file")
    p.add_argument("--text")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_send)

    p = add_pl(sub.add_parser("wait", help="等待回复完成"))
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--interval", type=float, default=3.0)
    p.add_argument("--max-recover", type=int, default=1, dest="max_recover",
                   help="会话被回收后自动续窗的次数（默认 1；续窗＝在原对话地址重开窗口）")
    p.add_argument("--first-token", type=float, default=0, dest="first_token",
                   help="首字时钟（秒）：多久内必须看到生成中或首字，过了就走分诊。"
                        "0 = 用平台的 ttft_s（默认 45）")
    p.add_argument("--stall", type=float, default=0,
                   help="停滞时钟（秒）：已在生成但页面连续多久不变算卡住。"
                        "0 = 用平台的 stall_s（默认 60）")
    p.set_defaults(func=cmd_wait)

    p = add_pl(sub.add_parser("read", help="读取末条回复"))
    p.add_argument("--out")
    p.set_defaults(func=cmd_read)

    p = add_pl(sub.add_parser("triage", help="卡住判定：只读页面，说清卡在哪、下一步做什么"))
    p.add_argument("--relocate", action="store_true",
                   help="用备用读法复探（只读，不刷新）：分辨「选择器失效」与「确实没回复」")
    p.add_argument("--reload", action="store_true",
                   help="最后手段：非生成态下刷新页面（会丢掉页面状态，刷新后必须重走 bind + send）")
    p.set_defaults(func=cmd_triage)

    p = add_pl(sub.add_parser("doctor", help="体检：浏览器实例 / 重复扩展 / 会话 / state"))
    p.set_defaults(func=cmd_doctor)

    p = add_pl(sub.add_parser("state", help="查看 state（含锁持有者）"))
    p.set_defaults(func=cmd_state)

    p = add_pl(sub.add_parser("unlock", help="强制清掉残留的 state 锁（崩溃恢复）"))
    p.set_defaults(func=cmd_unlock)

    p = sub.add_parser("plock", help="平台占用锁：查看跨任务锁 / 强拆残留锁（--release）")
    p.add_argument("--release", action="store_true",
                   help="强拆指定平台的锁（--platform 必填；持锁进程还活着时拆不动）")
    p.add_argument("--platform", dest="pplatform", default="",
                   help="平台 key（release 时必填；查看时可只看这一个平台）")
    p.set_defaults(func=cmd_plock)

    p = add_pl(sub.add_parser("stop", help="停止 session"))
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("reap", help="收割闲置窗口：租约过期就 session stop（防中断后窗口堆积）")
    p.add_argument("--ttl", type=int, default=0,
                   help="收割时限秒数（缺省读 config idle_close_s / WA_IDLE_CLOSE_S / 900）")
    p.add_argument("--watch", action="store_true", help="常驻循环（绑定后启动；interval 控制节奏）")
    p.add_argument("--interval", type=int, default=60, help="watch 模式扫描间隔秒（缺省 60）")
    p.add_argument("--scan", default="", help="扫描目录下所有 %s（多任务并收）" % STATE_NAME)
    p.add_argument("--dry-run", action="store_true", help="只报告将收割哪些，不动手")
    p.add_argument("--global", dest="global_scan", action="store_true",
                   help="L1 全局看门狗：不读 state 文件，直接对 bsk 全部活会话按"
                        " journal 组登记判定；只收自己组内租约过期的，无主会话只报告")
    p.set_defaults(func=cmd_reap)

    a = ap.parse_args(argv)

    guard = builtin_guard(a.cmd)
    if guard:
        sys.stderr.write(guard + "\n")
        return 6

    # ---- bsk 版本变更首查（2026-10-31 服从性审计新增）----
    # 版本与 skill 记录不一致时，第一动作是读变更说明，而不是按旧假设调用。
    # 只提示不拦截：变更内容需要人/agent 判断，机器只能发现「变了」。
    if a.cmd in ("bind", "send", "wait", "read", "probe", "triage", "clear") \
            and not _transport_is_iab():
        try:
            _rc, _out, _err = _bsk_raw(["--version"], 15)
            _ver = (_out or _err).strip().split()[-1] if (_out or _err).strip() else ""
            _st_path = state_path(getattr(a, "state", None))
            if os.path.exists(_st_path):
                _st = load_state(_st_path)
                _known = _st.get("bsk_version") or ""
                if _ver and _known and _ver != _known:
                    print("提示：bsk 版本已从 %s 变为 %s（state 记录）。触页面 API 可能有"
                          "破坏性变更（如 0.3→0.4 的 cooperate 上下文）。第一动作应是"
                          "读该版本变更说明，而不是按旧假设调用；确认兼容后跑 "
                          "`python scripts/wa.py state` 会随下一次 bind/send 更新记录。"
                          % (_known, _ver), file=sys.stderr)
                if _ver and _ver != _known:
                    _st["bsk_version"] = _ver
                    save_state(_st_path, _st)
        except Exception:
            pass  # 版本提示失败不阻断主流程

    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
