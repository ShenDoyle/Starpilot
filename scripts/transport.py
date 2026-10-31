# -*- coding: utf-8 -*-
"""transport：星舵传输层抽象 —— 页面动作可走 bsk CLI 或 ZCode 自带浏览器（IAB）。

## 背景（2026-10-31 用户裁定）

ZCode 宿主自带浏览器（IAB）与用户浏览器共享登录态，实测可跑通 DeepSeek 全链路；
bsk 需要装 CLI + 扩展 + cooperate 协议，在 ZCode 里是多余依赖。故：

- `STARPILOT_TRANSPORT=bsk`（默认）：行为与 0.1.4 完全一致；
- `STARPILOT_TRANSPORT=iab`：页面动作（navigate/evaluate/fill/press）经 **op 队列**
  交给 node 侧 worker（`iab-worker.mjs`）驱动 IAB；bsk 不需要安装。
  **用户需在 IAB 里登录目标平台一次**（登录态与用户浏览器共享，通常已满足）。

## op 队列协议

目录 `<工作区>/runtime/wa/iab-ops/`：
- 请求 `<id>.req.json`：{"id","action","params"}
- 结果 `<id>.res.json`：{"ok":bool,"data":...,"error":...}

wa.py 写请求后轮询结果（默认 60s 超时）；agent 在 node REPL 里反复运行
`iab-worker.mjs` 消费队列（每次调用是全新内核，worker 每轮重连 BrowserControl）。
等待循环（wa.py wait）天然表现为「多条 evaluate op」，agent 持续跑 worker 即可。

## 已知限制（0.1.5）
- worker 由 agent 手动驱动；长时间 wait 期间 agent 必须持续运行 worker，否则 op 超时。
- press 的组合键经 cua.keypress（isTrusted），fill 用 Playwright locator.fill。
"""

import json
import os
import time
import uuid


def ops_dir(root: str = "") -> str:
    d = os.environ.get("STARPILOT_IAB_OPS") or os.path.join(
        root or os.getcwd(), "runtime", "wa", "iab-ops")
    os.makedirs(d, exist_ok=True)
    return d


def current_transport() -> str:
    return (os.environ.get("STARPILOT_TRANSPORT") or "bsk").strip().lower()


def call(action: str, params: dict, timeout: float = 60.0, root: str = "") -> tuple:
    """发起一次页面操作并等待结果。返回 (rc, out, err)，语义对齐 wa.bsk()。

    rc=0 成功（out 为结果 JSON 文本）；rc=1 失败（err 带原因）；rc=124 超时。
    worker 未运行时表现为超时——错误信息会提示启动 worker。
    """
    d = ops_dir(root)
    oid = "%s-%s" % (time.strftime("%H%M%S"), uuid.uuid4().hex[:6])
    req = os.path.join(d, "%s.req.json" % oid)
    res = os.path.join(d, "%s.res.json" % oid)
    with open(req, "w", encoding="utf-8") as f:
        json.dump({"id": oid, "action": action, "params": params}, f, ensure_ascii=False)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(res):
            try:
                with open(res, encoding="utf-8") as f:
                    r = json.load(f)
            except Exception:
                time.sleep(0.2)
                continue
            try:
                os.remove(req)   # worker 成功后会自己删请求文件，竞态下可能已不存在
            except OSError:
                pass
            if r.get("ok"):
                return 0, json.dumps(r.get("data"), ensure_ascii=False), ""
            return 1, "", str(r.get("error") or "iab op failed")[:400]
        time.sleep(0.25)
    try:
        os.remove(req)
    except OSError:
        pass
    return 124, "", ("iab op 超时（%ss，action=%s）。worker 未运行或页面无响应："
                     "在 node REPL 里运行 scripts/iab-worker.mjs 消费队列。" % (timeout, action))


def parse_bsk_args(args: list) -> tuple:
    """把 wa.py 的 bsk 风格参数翻译成 (action, params)。无法翻译返回 (None, {})。

    支持：navigate <url> / evaluate <js> / fill --selector s --value v /
    press <key> [--selector s] / session start|stop（映射 noop）。
    """
    if not args:
        return None, {}
    cmd = args[0]
    if cmd == "navigate":
        return "navigate", {"url": args[1] if len(args) > 1 else ""}
    if cmd == "evaluate":
        return "evaluate", {"js": args[1] if len(args) > 1 else ""}
    if cmd == "fill":
        sel, val = "", ""
        for i, a in enumerate(args):
            if a == "--selector" and i + 1 < len(args):
                sel = args[i + 1]
            if a == "--value" and i + 1 < len(args):
                val = args[i + 1]
        return "fill", {"selector": sel, "value": val}
    if cmd == "press":
        key = args[1] if len(args) > 1 else ""
        sel = ""
        for i, a in enumerate(args):
            if a == "--selector" and i + 1 < len(args):
                sel = args[i + 1]
        return "press", {"key": key, "selector": sel}
    if cmd == "session":
        return "noop", {"sub": args[1] if len(args) > 1 else ""}
    return None, {}


def pending_reqs(root: str = "") -> list:
    """列出未消费的请求（worker 用）。"""
    d = ops_dir(root)
    return sorted(f for f in os.listdir(d) if f.endswith(".req.json"))


def read_req(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_res(d: str, oid: str, ok: bool, data=None, error: str = "") -> str:
    p = os.path.join(d, "%s.res.json" % oid)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"ok": ok, "data": data, "error": error}, f, ensure_ascii=False)
    return p
