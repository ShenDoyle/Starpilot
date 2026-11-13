# -*- coding: utf-8 -*-
"""transport：星舵传输层抽象 —— 页面动作可走 bsk CLI 或 op 队列（IAB / ego-browser）。

## 背景

- 2026-10-31：ZCode 宿主自带浏览器（IAB）与用户浏览器共享登录态，实测可跑通 DeepSeek
  全链路；bsk 需要装 CLI + 扩展 + cooperate 协议，在 ZCode 里是多余依赖。
- 2026-11-12：用户裁定不用 bsk，改用 ego-browser（ego lite）——新增
  `STARPILOT_TRANSPORT=egolite` 并设为**默认**。op 队列协议与 iab 完全一致，
  只是 worker 换成 `egolite-worker.mjs`（`ego-browser nodejs` 驱动，可后台常驻，
  修掉 IAB「agent 必须持续手动跑 worker」的限制）。

取值：
- `STARPILOT_TRANSPORT=egolite`：op 队列 + `ego-browser nodejs` worker（本机 config 已选）；
- `STARPILOT_TRANSPORT=iab`：op 队列 + ZCode IAB worker（实验性）；
- `STARPILOT_TRANSPORT=bsk`：直连 bsk CLI（兜底，保留可回退）。

## op 队列协议

目录 `<工作区>/runtime/wa/iab-ops/`（历史命名，iab/egolite 共用）：
- 请求 `<id>.req.json`：{"id","action","params"}
- 结果 `<id>.res.json`：{"ok":bool,"data":...,"error":...}

wa.py 写请求后轮询结果（默认 60s 超时）；worker 消费队列。egolite 下 worker 以
后台进程运行（见 wa.py 的 worker 启动提示与 references/egolite-mode.md）。

## 已知限制
- iab：worker 由 agent 手动驱动；长时间 wait 期间 agent 必须持续运行 worker，否则 op 超时。
- egolite：worker 后台常驻（默认单轮 10 分钟自动退出，超时重跑一次即可）。
- press 的组合键经 isTrusted 键盘事件，fill 用编辑器原生 API。
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


# 走 op 队列的传输模式：iab（ZCode 自带浏览器，agent 手动跑 worker）、
# egolite（ego-browser，worker 可后台常驻）。
OPQUEUE_TRANSPORTS = ("iab", "egolite")

# 传输取值优先级：env STARPILOT_TRANSPORT > config 的 "transport" 键 > "bsk"（兜底）。
# 为什么默认兜底是 bsk 而不是 egolite：单测套件是传输无关的，运行时默认若随代码
# 翻转会漏进几百条未显式选传输的用例（实测掉进 op 队列 60s 轮询，表现为挂死）。
# 「本机改用 egolite」由配置表达（~/.starpilot/config.json 的 "transport": "egolite"，
# 2026-11-12 用户裁定），与 browser_backend 同一机制。


def _config_transport() -> str:
    p = os.environ.get("STARPILOT_CONFIG") or os.path.expanduser(
        "~/.starpilot/config.json")
    try:
        with open(p, encoding="utf-8") as f:
            v = (json.load(f) or {}).get("transport")
        return str(v).strip().lower() if v else ""
    except Exception:
        return ""


def current_transport() -> str:
    return (os.environ.get("STARPILOT_TRANSPORT")
            or _config_transport() or "bsk").strip().lower()


def is_opqueue(t: str = "") -> bool:
    """当前（或给定）传输是否走 op 队列。"""
    return (t or current_transport()) in OPQUEUE_TRANSPORTS


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
            return 1, "", str(r.get("error") or "op 失败")[:400]
        time.sleep(0.25)
    try:
        os.remove(req)
    except OSError:
        pass
    if current_transport() == "egolite":
        hint = ("运行后台 worker：ego-browser nodejs -e "
                "\"await import('file:///<工作区>/runtime/wa/egolite-launch.mjs')\""
                "（wa.py 已自动生成该引导文件；也可看 references/egolite-mode.md）。")
    else:
        hint = "在 node REPL 里运行 scripts/iab-worker.mjs 消费队列。"
    return 124, "", ("op 队列超时（%ss，action=%s，transport=%s）。worker 未运行或页面无响应：%s"
                     % (timeout, action, current_transport(), hint))


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
