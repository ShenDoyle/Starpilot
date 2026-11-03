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
# 0.4.1（2026-11-02 自动升级）新增文案，全部要认得，否则补救不触发：
#   · This action requires a valid control context from cooperate sync
#   · this action needs an explicit current observation
#   · This action requires an unused strict observation from cooperate sync
#   · The media source changed; synchronize and replan
_NEEDS_CONTEXT = (
    "requires the context returned by the latest cooperate sync",
    "requires a valid control context",
    "requires an unused strict observation",
    "needs an explicit current observation",
    "observed content changed",
    "media source changed",
    "shared page control or observation changed",
    "requires the context",
    "control_context_invalid",
    "document_changed",
)

# 这些子命令不触页面，补救没有意义
_LOCAL_CMDS = {
    "session", "browsers", "doctor", "daemon", "status", "logs", "update",
    "install-skill", "view", "debug", "cooperate",
}

# 会触页面的子命令：0.4.1 起这些**先** sync 再动作（预注入），不必先失败一次
_PAGE_CMDS = {
    "navigate", "navigate-back", "navigate-forward", "reload", "evaluate",
    "fill", "press", "click", "hover", "wheel", "drag", "scroll-to",
    "focus", "blur", "snapshot", "screenshot", "observe", "extract",
    "get-html", "step", "emulate", "console", "network",
}

_CTX_RE = re.compile(r'"context_id"\s*:\s*"([^"]+)"')
_OBS_RE = re.compile(r'"observation_id"\s*:\s*"([^"]+)"')
_STOPPED_RE = re.compile(r'"stopped"\s*:\s*true')
_VER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

_VER = None          # bsk 版本缓存：(major, minor, patch)
_CTX_ARGS = ("--context", "--observation")


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
                           encoding="utf-8", errors="backslashreplace",
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", "bsk 超时（%ss）：%s" % (timeout, " ".join(args[:2]))
    return p.returncode, p.stdout or "", p.stderr or ""


def _sync_context(sid):
    """cooperate sync，返回 (context_id, observation_id) 或 None。

    0.4.1（2026-11-02 实测）语义要点：

    · sync **返回 rc!=0 就是没拿到上下文**（典型：`A page frame is not
      responding` / `cooperation_sync_required`），此时绝不能当成功用——
      否则动作不带 context 上去，只会报 `requires an unused strict
      observation`，把真实病因盖掉。必须检查 rc。
    · 这两种态 bsk 自己的 hint 就是「先 cooperate wait，再 sync」：wait 让
      页面 frame 就绪，sync 才拿得到可用 observation。所以 wait 只在 **sync
      失败后** 补救用，健康路径不跑（跑多了反而拖慢每次动作）。
    · 0.4.1 起 navigate/fill/press 还要一个**一次性**的 strict observation
      （`--observation`），也由 sync 返回；0.4.0 只认 context，多给参数会报错，
      所以按版本决定是否带 observation。
    """
    blob = ""
    for attempt in range(3):
        rc, out, err = _run(["cooperate", "sync", "--session", sid,
                             "--policy", "auto", "--json"], timeout=60)
        blob = (out or "") + (err or "")
        if _STOPPED_RE.search(blob):
            return None  # 窗口被关/被停，救不了
        if rc == 0:
            pair = _parse_sync(out or "")
            if pair:
                return pair
        # frame 未就绪：按 bsk hint 先 wait 等它就绪，再 sync 一次
        _run(["cooperate", "wait", "--session", sid, "--json"], timeout=60)
        time.sleep(1.0)
    return None


def _parse_sync(text):
    """从 sync 输出里取 (context_id, observation_id)。

    **必须用 JSON 解析，不能用正则扫第一个匹配**（2026-11-02 踩坑）：0.4.1 的
    sync 响应里 `observation_id` 会出现多次（顶层 + 历史/嵌套里的旧值），正则
    `"observation_id": "..."` 取到的是**旧的那个**，于是动作永远报
    `requires an unused strict observation from cooperate sync` —— 表面上像是
    「策略要求没满足」，实际是取错了 ID，排查会绕很远。
    """
    try:
        d = json.loads(text)
    except Exception:
        d = None
    if isinstance(d, dict):
        ctx = d.get("context_id")
        if isinstance(ctx, str) and ctx:
            obs = d.get("observation_id")
            return (ctx, obs if isinstance(obs, str) else "")
        # 兼容包一层 data 的形状
        inner = d.get("data")
        if isinstance(inner, dict) and isinstance(inner.get("context_id"), str):
            obs = inner.get("observation_id")
            return (inner["context_id"], obs if isinstance(obs, str) else "")
    mc = _CTX_RE.search(text or "")
    if mc:
        mo = _OBS_RE.search(text or "")
        return (mc.group(1), mo.group(1) if mo else "")
    return None


def _version():
    """bsk 版本（缓存）。取不到按 (0,0,0) 处理 = 不预注入、不传 observation。"""
    global _VER
    if _VER is None:
        rc, out, err = _run(["--version"], 20)
        m = _VER_RE.search((out or "") + (err or ""))
        _VER = tuple(int(x) for x in m.groups()) if m else (0, 0, 0)
    return _VER


def _ctx_args(pair):
    """把 sync 结果拼成命令行参数（老版本只给 --context）。"""
    if not pair:
        return []
    ctx, obs = pair
    args = ["--context", ctx]
    if obs and _version() >= (0, 4, 1):
        args += ["--observation", obs]
    return args


def _strip_ctx(args):
    """去掉调用方自带的旧 --context/--observation，避免重复参数。"""
    out, i = [], 0
    while i < len(args):
        if args[i] in _CTX_ARGS:
            i += 2
            continue
        out.append(args[i])
        i += 1
    return out


def run(bsk_fn, args, timeout):
    """bsk_fn(标准 subprocess 包装) 的上下文感知包装。

    bsk_fn: wa.py 的原 bsk()（保证超时/编码语义一致）。返回 (rc, out, err)。

    流程（0.4.1 起）：触页面的命令**先** sync 拿到 (context, observation)，
    带着参数一次做成；失败再按错误文案补救，最多两轮。本地命令零开销直通。
    """
    args = list(args)
    cmd0 = args[0] if args else ""
    if cmd0 in _LOCAL_CMDS:
        return bsk_fn(args, timeout)
    sid = _sid_of(args)
    pre = None
    if sid and cmd0 in _PAGE_CMDS and _version() >= (0, 4, 1):
        pre = _sync_context(sid)   # 预注入：省掉「先失败一次」的来回
    rc, out, err = bsk_fn(_strip_ctx(args) + _ctx_args(pre), timeout)
    if rc == 0:
        return rc, out, err
    blob = (err or "") + (out or "")
    if not sid or not any(k in blob for k in _NEEDS_CONTEXT):
        return rc, out, err
    for _ in range(2):  # 最多两轮补救：sync→重试；页面又变了再 sync→重试
        pair = _sync_context(sid)
        if not pair:
            return rc, out, err
        rc, out, err = bsk_fn(_strip_ctx(args) + _ctx_args(pair), timeout)
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


# ---------------------------------------------------------------- 0.4.1 能力层
#
# 只靠版本号决定「用不用某个子命令」是不够的：同一版本不同构建、
# 或用户手动降级 CLI，能力就可能对不上。所以能力一律**问 CLI 自己**
# （`<cmd> --help` 通不通），结果缓存一次；问不出来就当没有。
# 探测是纯本地动作（--help 不触页面、不动 daemon），零风险。

_CAPS = {}
_STEP_ACTIONS = ("click", "press", "fill", "hover", "select", "drag",
                 "scroll-to", "wheel", "navigate")


def supports(runner, cmd: str) -> bool:
    """这个 bsk 有没有 <cmd> 子命令。runner 是 wa.bsk 那一层的调用器。"""
    if cmd in _CAPS:
        return _CAPS[cmd]
    try:
        rc, out, err = runner([cmd, "--help"], 20)
    except Exception:
        rc = 1
    help_markers = ("Usage:", "usage:", "Options:")
    ok = rc == 0 and any(m in (out or "") + (err or "") for m in help_markers)
    _CAPS[cmd] = ok
    return ok


def reset_caps() -> None:
    """测试/升级 CLI 后清空能力缓存。"""
    _CAPS.clear()


def step(runner, action: str, params: dict, sid: str, *, until_visible: str = "",
         expect: list = None, policy: str = "target", save_full: str = "",
         wait_timeout_ms: int = 5000, timeout: int = 120):
    """`bsk step`：一个动作 + 一个已知条件 + 同步状态，**never retried**。

    为什么值得用它（0.4.1）：星舵原来每条页面动作都是「cooperate sync → 动作 →
    再探测」三次往返，而 ChatGPT 这类页面在 sync 与动作之间持续变异，
    context 秒级失效（实测：`observed content changed` 反复触发，
    补救要再 sync 一轮）。step 把动作与就绪条件交给扩展原子执行，
    往返次数从 3 降到 1，竞态窗口也随之收窄。

    ⚠️ 不默认启用：`--expect` 的目标是**快照 ref（@e1）**而不是 CSS 选择器，
    星舵现在全走选择器；照搬会把「目标事实校验」做成永远不满足。
    因此这一步只在 `STARPILOT_STEP_SEND=1` 时走，并且
      · 只用 `--until-visible`（CSS，正信号）+ `--save-full` 留证据；
      · 动作后仍由页面事实判成败（platforms.fill_check / confirm 口径不变）——
        bsk 的自我校验不是判据，这条是老教训，不因为换了原语就作废。
    """
    if action not in _STEP_ACTIONS:
        return 2, "", "step 不支持的动作：%s（可选 %s）" % (action, ",".join(_STEP_ACTIONS))
    args = ["step", "--session", sid, "--action", action,
            "--params", json.dumps(params or {}, ensure_ascii=False),
            "--policy", policy, "--wait-timeout-ms", str(int(wait_timeout_ms)),
            "--json"]
    if until_visible:
        args += ["--until-visible", until_visible]
    if expect:
        args += ["--expect", json.dumps(expect, ensure_ascii=False)]
    if save_full:
        args += ["--save-full", save_full]
    return runner(args, timeout)


def dialog_status(runner, sid: str, timeout: int = 30):
    """`bsk dialog status`：读挂起的原生对话框（alert/confirm/prompt/beforeunload）。

    用在**收窗前**：beforeunload 挂起会让 `session stop` / tab close 卡在页面上，
    表现是「收割失败」，而真相是页面在问用户「确定要离开吗」。
    先查出来如实报，让人决定 accept 还是 dismiss——
    星舵不替用户自动 accept（那等于替用户回答「离开这个页面」，是有副作用的决定）。
    """
    if not supports(runner, "dialog"):
        return None
    rc, out, err = runner(["dialog", "status", "--session", sid, "--json"], timeout)
    if rc != 0:
        return {"ok": False, "rc": rc, "detail": (err or out)[:200], "pending": []}
    try:
        data = json.loads((out or "").strip() or "{}")
    except Exception:
        return {"ok": False, "rc": 0, "detail": "dialog status 输出解析失败",
                "pending": []}
    items = data.get("dialogs") if isinstance(data, dict) else data
    items = items if isinstance(items, list) else []
    return {"ok": True, "pending": items, "raw": data}
