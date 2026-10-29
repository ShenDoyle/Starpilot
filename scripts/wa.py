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
import statelock  # noqa: E402

# ---------------------------------------------------------------- bsk 定位

_CANDIDATES = [
    os.environ.get("BSK_BIN") or "",
    shutil.which("bsk") or "",
    os.path.expanduser("~/.local/bin/bsk.exe"),
    os.path.expanduser("~/.local/bin/bsk"),
    r"C:\Users\Administrator\.local\bin\bsk.exe",
]


def bsk_path() -> str:
    for c in _CANDIDATES:
        if c and os.path.exists(c):
            return c
    raise SystemExit("找不到 bsk：请设 BSK_BIN 环境变量，或确认 bsk 已安装")


BSK = bsk_path()


def bsk(args: list, timeout: int = 120, check: bool = False) -> tuple[int, str, str]:
    try:
        p = subprocess.run([BSK] + args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", "bsk 超时（%ss）：%s" % (timeout, " ".join(args[:2]))
    if check and p.returncode != 0:
        raise SystemExit("bsk 失败 rc=%s\n%s" % (p.returncode, (p.stderr or p.stdout)[:600]))
    return p.returncode, p.stdout or "", p.stderr or ""


# ---------------------------------------------------------------- state

STATE_NAME = ".wa-state.json"


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


def list_sessions() -> list:
    rc, out, _ = bsk(["session", "list", "--json"])
    if rc != 0:
        return []
    try:
        data = json.loads(out.strip())
    except Exception:
        return []
    if isinstance(data, dict):
        data = data.get("sessions") or data.get("value") or []
    norm = []
    for s in data:
        if isinstance(s, dict):
            s = dict(s)
            # bsk 0.3.x 的键是 session_id / agent_window_id，统一别名
            s.setdefault("id", s.get("session_id") or s.get("session") or "")
            s.setdefault("window", s.get("agent_window_id") or "")
            norm.append(s)
    return norm


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


def _first_browser_instance() -> str:
    """多浏览器在线时猜一个实例 id。

    偏好标签用环境变量 `WA_BROWSER_PREFER` 指定（逗号分隔的片段，按顺序匹配）。
    只有一个实例时直接用它；**多个实例且无法判断时返回空**——由调用方报错让人选，
    绝不「就近替代」（那等于拿 A 账号的登录态去操作 B 账号）。
    """
    rc, out, _ = bsk(["browsers"], timeout=30)
    if rc != 0:
        return ""
    prefer = [s.strip() for s in os.environ.get("WA_BROWSER_PREFER", "").split(",") if s.strip()]
    ids, labels = [], []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] != "INSTANCE" and not line.startswith(" "):
            ids.append(parts[0])
            labels.append(" ".join(parts[2:]))
    if not ids:
        return ""
    for want in prefer:
        for inst, lab in zip(ids, labels):
            if want and want in lab:
                return inst
    return ids[0] if len(ids) == 1 else ""


def enforce_single_session(keep: str = "", browser: str = "") -> None:
    """窗口纪律（硬规则）：**同一浏览器实例**内任何时刻只允许一个会话/Agent Window。

    只停 browser 指定实例上、keep 之外的所有会话（连同其 Agent Window）。
    **其他实例上的会话不归本工具管，绝不误杀。**
    防止：死会话续期开新窗口不关旧窗口、临时巡检会话用完不收 → 窗口堆积。
    """
    try:
        for s in list_sessions():
            sid = s.get("id") or s.get("session") or ""
            if not sid or sid == keep:
                continue
            if browser and (s.get("browser") or "") != browser:
                continue  # 其他实例的会话不动
            bsk(["session", "stop", sid], timeout=30)
    except Exception:
        pass


def navigate_soft(sid: str, url: str, timeout: int = 180):
    """导航到 url，**容忍 `load` 事件不来**。

    为什么需要：SPA / 长轮询页面（Gemini 实测）可能永远不触发 `load`，
    `--wait-until load` 会在 30 秒后返回非 0 —— 但页面其实早就可用了。
    把它当失败 = 让「绑定」这种纯本地动作被一个永远不会发生的事件卡死。

    退让顺序：load → domcontentloaded。仍失败也**不在这里判死**：
    调用方必须再用「地址对不对 + 编辑器在不在」确认页面确实可用（那才是真正的判据）。
    返回 (rc, out, err)，err 里带上第一次失败原因，便于事后分辨「慢」与「坏」。
    """
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


def ensure_session(st: dict, path: str, *, renew: bool = False) -> str:
    sid = st.get("session") or ""
    browser = st.get("browser") or os.environ.get("WA_BROWSER", "")
    if sid and not renew:
        ids = [s.get("id") or s.get("session") or "" for s in list_sessions()]
        if sid in ids:
            enforce_single_session(sid, browser)  # 清掉同实例的旁支会话（如临时巡检窗口）
            if browser and not st.get("browser"):
                st["browser"] = browser  # env 给的实例也记下来，下次不必再传
                save_state(path, st)
            return sid
    # 开新会话前：先收掉目标实例上的旧会话/旧窗口（其他实例的会话不动）
    enforce_single_session("", browser)
    # 多浏览器（用户可能同时开多个 Edge profile）：state.browser 优先，其次 WA_BROWSER env
    args = ["session", "start", "--no-focus"]
    if browser:
        args += ["--browser", browser]
    rc, out, err = bsk(args, timeout=120)
    if rc != 0 and "multiple browsers" in (out + err).lower():
        inst = _first_browser_instance()
        if inst:
            st["browser"] = inst
            save_state(path, st)
            rc, out, err = bsk(args + ["--browser", inst], timeout=120)
    if rc != 0:
        raise SystemExit("session start 失败：%s" % (err or out)[:400])
    ids = [s.get("id") or s.get("session") or "" for s in list_sessions()]
    sid = ids[-1] if ids else ""
    if not sid:
        m = _SID_RE.findall(out)
        sid = m[-1] if m else ""
    if not sid:
        raise SystemExit("拿不到 session id。原始输出：%s" % out[:400])
    enforce_single_session(sid, browser)  # 双保险：新会话留下，同实例其余全收
    st["session"] = sid
    if browser:
        # 把实例记进 state：下次不用再靠 WA_BROWSER 环境变量，
        # 多浏览器在线时也不会因为「猜不出实例」而 bind 失败。
        st["browser"] = browser
    save_state(path, st)
    place_window(sid)
    bound = st.get("url") or ""
    if bound:
        rc, out, err = navigate_soft(sid, bound)
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
    s = out.strip()
    data = None
    try:
        data = json.loads(s)
    except Exception:
        for line in s.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    break
                except Exception:
                    continue
    if isinstance(data, dict) and "value" in data:
        val = data["value"]
        if isinstance(val, str):
            try:
                data = json.loads(val)
            except Exception:
                data = {"raw": val}
        else:
            data = val
    if not isinstance(data, dict):
        return False, "无法解析 evaluate 输出：%s" % s[:300]
    return True, data


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
            return ok, platforms.normalize_probe(p, data)
    return ok, platforms.normalize_probe(p, data) if ok else data


# 写入路径：唯一实现放在本文件（wa 是传输层，闸门与 CLI 都走它）。
NATIVE_MAX_CHARS = 12000   # 真实键盘输入要经命令行传值，超长有被截断/乱码的风险


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
    for sel in (p.get("sel") or {}).get("editor") or []:
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
        ok, res = evaluate(platforms.js_fill(p, text), sid, timeout=timeout)
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
    """
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
    rc, out, err = navigate_soft(sid, url)
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
    save_state(path, st)
    print(json.dumps({"ok": True, "bound": st["url"], "platform": p["key"],
                      "msg_count": data["msg_count"], "session": sid,
                      "new_chat": new_chat, "conv_id": bv["conv_id"],
                      "bind_code": bv["code"], "warn": bv["warn"],
                      "settle_waited_s": round(waited, 1),
                      "nav_note": nav_note},
                     ensure_ascii=False, indent=2))
    if bv["warn"]:
        print("注意：" + bv["warn"], file=sys.stderr)
    return 0


def cmd_clear(a) -> int:
    path = state_path(a.state)
    st = load_state(path)
    p = platforms.get(a.platform or st.get("platform"))
    sid = ensure_session(st, path)
    res = clear_editor(p, sid)
    res["platform"] = p["key"]
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 6


def cmd_probe(a) -> int:
    path = state_path(a.state)
    st = load_state(path)
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


def cmd_send(a) -> int:
    path = state_path(a.state)
    st = load_state(path)
    p = platforms.get(a.platform or st.get("platform"))
    if not st.get("url"):
        print("尚未绑定对话：先跑 wa.py bind <url>", file=sys.stderr)
        return 2

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

    # 写路径白名单：平台没实测过写入路径就**不许真实写**。
    # 闸门层（write_gate）一直有这条；CLI 层以前没有 —— 于是「绕过闸门直接 wa.py send」
    # 可以写到不该写的地方。两层的写权限必须同源，否则白名单只是装饰。
    allowed = list(p.get("write_allowed") or [])
    if (p.get("write_method") or "") not in allowed:
        print("平台 %s 的写入路径 %r 不在实测白名单 %s 里：拒绝真实写。\n"
              "先按 references/web-ai-platforms.md 实测回填 platforms.py。"
              % (p.get("key"), p.get("write_method"), allowed or "（空）"), file=sys.stderr)
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
        print("页面上有弹窗/遮罩，拒绝发送：%s\n"
              "先关掉弹窗再重试（Kimi: bsk click --selector \"[data-testid='confirm-dialog-cancel']\"）。"
              % (cur.get("modal_text") or "（无文案）"), file=sys.stderr)
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
        print("点击后未确认消息真的发出（信号：无）。判定=%s：%s\n"
              "可能平台侧拒绝或限流：先 wa.py probe 看状态、看页面弹窗，不要直接重试。"
              % (clas["verdict"], clas["reason"]), file=sys.stderr)
        return 8

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
    st["last_send_hash"] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    st["last_send_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    st["awaiting_reply"] = True
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
                      "signals": clas["signals"]},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_wait(a) -> int:
    path = state_path(a.state)
    st = load_state(path)
    p = platforms.get(a.platform or st.get("platform"))
    sid = ensure_session(st, path)
    deadline = time.time() + a.timeout
    stable = 0
    prev_len = -1
    prev_msgs = -1
    started = False

    while time.time() < deadline:
        ok, cur = read_state(p, sid)
        if not ok:
            time.sleep(a.interval)
            continue

        if cur.get("error"):
            print(json.dumps({"ok": False, "stage": "page_error", "detail": cur["error"],
                              "url": cur.get("url")}, ensure_ascii=False, indent=2))
            return 4

        if cur.get("msg_count", 0) > int(st.get("last_send_msg_count") or 0):
            started = True

        if started and not cur.get("generating") and cur.get("last_len", 0) > 0:
            if cur.get("regen"):
                # 硬信号：末条回复的操作按钮已出现 = 本轮生成结束；
                # 再要求文本长度连续两次一致，防虚拟化渲染抖动
                if cur["last_len"] == prev_len:
                    stable += 1
                    if stable >= 1:
                        st["awaiting_reply"] = False
                        st["fail_streak"] = 0
                        save_state(path, st)
                        print(json.dumps({"ok": True, "chars": cur["last_len"],
                                          "msg_count": cur.get("msg_count"),
                                          "signal": "regen",
                                          "tail": cur.get("last_tail", "")},
                                         ensure_ascii=False, indent=2))
                        return 0
                else:
                    stable = 0
            elif cur["last_len"] == prev_len:
                stable += 1
                if stable >= 2:
                    st["awaiting_reply"] = False
                    st["fail_streak"] = 0
                    save_state(path, st)
                    print(json.dumps({"ok": True, "chars": cur["last_len"],
                                      "msg_count": cur.get("msg_count"),
                                      "signal": "text_stable",
                                      "tail": cur.get("last_tail", "")},
                                     ensure_ascii=False, indent=2))
                    return 0
            else:
                stable = 0
        else:
            stable = 0

        prev_len = cur.get("last_len", 0) if not cur.get("generating") else -1
        time.sleep(a.interval)

    print(json.dumps({"ok": False, "stage": "timeout", "waited_s": a.timeout,
                      "hint": "超时不等于失败：再跑一次 wait，或先 probe 看 generating/last_len"},
                     ensure_ascii=False, indent=2))
    return 5


def cmd_read(a) -> int:
    path = state_path(a.state)
    st = load_state(path)
    p = platforms.get(a.platform or st.get("platform"))
    sid = ensure_session(st, path)
    text = ""
    for attempt, ms in enumerate((600, 1200, 2500)):
        evaluate(platforms.js_scroll(p), sid)
        time.sleep(ms / 1000.0)
        ok, data = evaluate(js_read_last(p), sid)
        if ok and data.get("text"):
            text = data["text"]
            break
        if not ok:
            print("读取失败：%s" % data, file=sys.stderr)
            return 4
    if not text:
        print("读取失败：重试 3 次仍未取到回复文本（线程可能未渲染）", file=sys.stderr)
        return 5
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print(json.dumps({"ok": True, "chars": len(text), "out": a.out}, ensure_ascii=False))
    else:
        print(text)
    return 0


def cmd_state(a) -> int:
    path = state_path(a.state)
    st = load_state(path)
    holder = statelock.read_holder(path)
    st["_lock"] = holder or None
    st["_lock_free"] = not bool(holder)
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


def cmd_stop(a) -> int:
    path = state_path(a.state)
    st = load_state(path)
    sid = st.get("session")
    if sid:
        bsk(["session", "stop", sid], timeout=60)
        st["session"] = ""
        save_state(path, st)
        print("已停止 session %s" % sid)
    else:
        print("没有记录中的 session")
    return 0


def main() -> int:
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

    p = add_pl(sub.add_parser("send", help="发送一条消息"))
    p.add_argument("--file")
    p.add_argument("--text")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_send)

    p = add_pl(sub.add_parser("wait", help="等待回复完成"))
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--interval", type=float, default=3.0)
    p.set_defaults(func=cmd_wait)

    p = add_pl(sub.add_parser("read", help="读取末条回复"))
    p.add_argument("--out")
    p.set_defaults(func=cmd_read)

    p = add_pl(sub.add_parser("state", help="查看 state（含锁持有者）"))
    p.set_defaults(func=cmd_state)

    p = add_pl(sub.add_parser("unlock", help="强制清掉残留的 state 锁（崩溃恢复）"))
    p.set_defaults(func=cmd_unlock)

    p = add_pl(sub.add_parser("stop", help="停止 session"))
    p.set_defaults(func=cmd_stop)

    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
