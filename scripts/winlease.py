#!/usr/bin/env python3
"""窗口租约：按浏览器实例登记「谁在开窗」，冲突拒绝、不抢占、可降级串行。

为什么需要它（2026-11-02 现场取证）：
`wa.py` 的窗口纪律原本是「同一浏览器实例只允许一个会话」，实现方式是在
`ensure_session` 里调 `enforce_single_session(keep, browser)` —— 它会把同实例上
**所有非本会话**的 session 都停掉。于是：

  · 任务 A（本项目，13:40:56 开 wibt）
  · 任务 B（D:\\OpenClaw\\starpilot-smstoweb，13:40:57 开 dxfp，state 里没记
    browser → browser="" → 连「其他实例不动」的过滤都失效）
  · 结果：A 的会话 60 秒内消失，A 无从得知、也无从拒绝。

平台锁（`lock_platform`）挡不住这件事：它按「平台+隔离键」分，不认浏览器实例，
也不管会话生命周期；而且两个任务可能一个用 deepseek 一个用 chatgpt，平台根本不同，
却共享同一个浏览器实例和同一块屏幕。

本模块把「窗口」当成一种有主的资源：
  · 一个浏览器实例 = 一份租约文件（跨项目可见，在用户级目录）
  · 租约里记 owner（工作区 + run_id）、声明的 session、心跳时间
  · 心跳新鲜 = 有主，别人**一律拒绝**，不抢占（抢占只能显式 takeover 并留痕）
  · 心跳过期 = 无主残留，可回收，但要落账（stale_reclaimed）
  · 实例若不支持多 Agent Window（探不到 tab scope），acquire 自动降级 serial：
    有界等待排队，等不到就报 WINDOW_BUSY，绝不硬挤

纯标准库、不依赖 bsk，便于单测。
"""
from __future__ import annotations

import json
import os
import socket
import time
import uuid

LEASE_DIR_ENV = "STARPILOT_LOCK_DIR"
LEASE_TTL_ENV = "STARPILOT_LEASE_TTL"
LEASE_WAIT_ENV = "STARPILOT_LOCK_WAIT"   # 等租约秒数；默认 0（立即失败，不排队）
DEFAULT_TTL = 300.0                       # 5 分钟没心跳视为无主
SERIAL_DEFAULT_WAIT = 120.0               # 串行降级时的默认有界等待
STATE_NAME = ".wa-state.json"


# ------------------------------------------------------------------ 基础工具
def lease_dir() -> str:
    d = os.environ.get(LEASE_DIR_ENV) or os.path.join(
        os.path.expanduser("~"), ".starpilot", "locks")
    os.makedirs(d, exist_ok=True)
    return d


def lease_path(instance: str) -> str:
    inst = (instance or "default").strip() or "default"
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in inst)
    return os.path.join(lease_dir(), "win-%s.json" % safe)


def journal_path(instance: str) -> str:
    return lease_path(instance).replace(".json", ".journal.jsonl")


def ttl_default() -> float:
    try:
        return float(os.environ.get(LEASE_TTL_ENV) or DEFAULT_TTL)
    except Exception:
        return DEFAULT_TTL


def owner_key(owner: dict) -> str:
    """owner 身份串：工作区路径 + run_id（没有 run_id 时用工作区做身份）。"""
    ws = os.path.normcase(os.path.abspath(owner.get("workspace") or os.getcwd()))
    run = (owner.get("run_id") or "").strip()
    return "%s|%s" % (ws, run)


def make_owner(workspace: str = "", run_id: str = "", platform: str = "",
               note: str = "") -> dict:
    return {
        "workspace": os.path.abspath(workspace or os.getcwd()),
        "run_id": run_id or os.environ.get("STARPILOT_RUN_ID", "") or "",
        "platform": platform or "",
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "note": note or "",
    }


# ------------------------------------------------------------------ 读写
def read(instance: str) -> dict | None:
    p = lease_path(instance)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def write(instance: str, rec: dict) -> None:
    p = lease_path(instance)
    tmp = p + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def log(instance: str, event: str, detail: dict) -> None:
    rec = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event}
    rec.update(detail or {})
    try:
        with open(journal_path(instance), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ------------------------------------------------------------------ 状态判定
def remaining(rec: dict | None) -> float:
    if not rec:
        return 0.0
    try:
        return float(rec.get("lease_until") or 0) - time.time()
    except Exception:
        return 0.0


def is_live(rec: dict | None) -> bool:
    """有主 = 心跳还在有效期内。跨机器（host 不同）的租约不自动回收。"""
    if not rec:
        return False
    if rec.get("host") and rec.get("host") != socket.gethostname():
        return remaining(rec) > 0
    return remaining(rec) > 0


def status(instance: str) -> dict:
    rec = read(instance)
    if not rec:
        return {"instance": instance, "state": "free", "owner": None,
                "sessions": [], "remaining": 0}
    live = is_live(rec)
    return {
        "instance": instance,
        "state": "busy" if live else "stale",
        "owner": rec.get("owner"),
        "owner_key": rec.get("owner_key"),
        "sessions": sorted((rec.get("sessions") or {}).keys()),
        "remaining": round(remaining(rec), 1),
        "mode": rec.get("mode"),
        "heartbeat_at": rec.get("heartbeat_at"),
        "takeover_from": rec.get("takeover_from"),
    }


def list_all() -> list:
    out = []
    for fn in sorted(os.listdir(lease_dir())):
        if fn.startswith("win-") and fn.endswith(".json"):
            inst = fn[len("win-"):-len(".json")]
            out.append(status(inst))
    return out


# ------------------------------------------------------------------ 冲突
class WindowBusy(Exception):
    def __init__(self, holder: dict, waited: float = 0.0):
        self.holder = holder or {}
        self.waited = waited
        who = self.holder.get("owner") or {}
        super().__init__(
            "WINDOW_BUSY：浏览器实例被另一个任务占用（%s / %s），已等 %.0fs，不抢占。"
            % (who.get("workspace") or "未知工作区",
               who.get("platform") or "未知平台", waited))


def _conflict_detail(instance: str, rec: dict, owner: dict) -> dict:
    return {
        "code": "WINDOW_BUSY",
        "instance": instance,
        "holder": rec.get("owner"),
        "holder_key": rec.get("owner_key"),
        "holder_sessions": sorted((rec.get("sessions") or {}).keys()),
        "holder_remaining_s": round(remaining(rec), 1),
        "me": owner,
        "hint": ("不要抢占、不要停对方的会话。可选：①等对方收口（holder_remaining_s 后"
                 "自然过期）；②另开一个浏览器实例（--browser/WA_BROWSER）做真隔离；"
                 "③确认对方已死锁且人工授权后 `wa.py lease takeover --confirm '<说明>'`。"),
    }


# ------------------------------------------------------------------ 取/续/放
def acquire(instance: str, owner: dict, *, ttl: float | None = None,
            wait: float | None = None, serial: bool = False,
            takeover_reason: str = "") -> dict:
    """取租约。冲突抛 WindowBusy（不抢占）；serial=True 时有界等待排队。

    takeover_reason 非空 = 显式人工接管，会覆盖活租约并落 journal。
    """
    ttl = float(ttl if ttl is not None else ttl_default())
    if wait is None:
        try:
            wait = float(os.environ.get(LEASE_WAIT_ENV) or 0)
        except Exception:
            wait = 0.0
        # 串行降级的默认行为是「排队等」，但显式给的 wait 优先（也便于测试设上限）
        if serial and wait <= 0:
            wait = SERIAL_DEFAULT_WAIT
    key = owner_key(owner)
    deadline = time.time() + max(0.0, wait)
    started = time.time()

    while True:
        rec = read(instance)
        live = is_live(rec)
        same = rec and rec.get("owner_key") == key
        if (not rec) or same or (not live):
            fresh = {
                "instance": instance,
                "owner": owner,
                "owner_key": key,
                "host": owner.get("host") or socket.gethostname(),
                "mode": "serial" if serial else "exclusive",
                "lease_id": (rec or {}).get("lease_id") or uuid.uuid4().hex[:12],
                "sessions": (rec or {}).get("sessions") or {},
                "heartbeat_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "lease_until": time.time() + ttl,
            }
            if rec and not live and not same:
                fresh["sessions"] = {}   # 过期租约：会话声明作废（对方可能已死）
                log(instance, "stale_reclaimed",
                    {"from": rec.get("owner"), "from_key": rec.get("owner_key"),
                     "by": owner, "expired_s": round(-remaining(rec), 1)})
            if same:
                pass  # 续期不写 journal（否则每次命令一条，噪声淹没接管记录）
            else:
                log(instance, "acquire", {"by": owner, "mode": fresh["mode"]})
            write(instance, fresh)
            return fresh
        # 有主且不是我
        if takeover_reason.strip():
            rec2 = dict(rec)
            rec2.update({
                "owner": owner, "owner_key": key, "sessions": {},
                "mode": "exclusive" if not serial else "serial",
                "heartbeat_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "lease_until": time.time() + ttl,
                "takeover_from": rec.get("owner"),
                "takeover_reason": takeover_reason.strip(),
            })
            log(instance, "takeover", {"from": rec.get("owner"), "by": owner,
                                       "reason": takeover_reason.strip()})
            write(instance, rec2)
            return rec2
        if time.time() < deadline:
            time.sleep(min(1.5, max(0.2, deadline - time.time())))
            continue
        raise WindowBusy(rec, waited=time.time() - started)


def heartbeat(instance: str, owner: dict, ttl: float | None = None) -> bool:
    """续租。只有 owner 一致才续（防止别人替我续命）。"""
    rec = read(instance)
    if not rec or rec.get("owner_key") != owner_key(owner):
        return False
    rec["lease_until"] = time.time() + float(ttl if ttl is not None else ttl_default())
    rec["heartbeat_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    write(instance, rec)
    return True


def release(instance: str, owner: dict, reason: str = "") -> bool:
    """释放。不是我的租约一律拒绝（不许替别人释放）。"""
    rec = read(instance)
    if not rec:
        return True
    if rec.get("owner_key") != owner_key(owner):
        return False
    log(instance, "release", {"by": owner, "reason": reason})
    try:
        os.remove(lease_path(instance))
    except Exception:
        pass
    return True


def claim_session(instance: str, sid: str, owner: dict) -> None:
    """把会话登记到本租约名下——别人据此判断「这个会话有主，不许停」。"""
    if not sid:
        return
    rec = read(instance)
    if not rec or rec.get("owner_key") != owner_key(owner):
        return
    rec.setdefault("sessions", {})[sid] = {
        "owner_key": rec["owner_key"],
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    write(instance, rec)


def owner_of_session(instance: str, sid: str) -> str | None:
    """这个会话被谁声明了？None = 无主（可清理）。"""
    rec = read(instance)
    if not rec:
        return None
    ent = (rec.get("sessions") or {}).get(sid)
    if not ent:
        return None
    if not is_live(rec):
        return None   # 租约过期 → 声明作废
    return ent.get("owner_key")


def may_stop_session(instance: str, sid: str, owner: dict) -> tuple:
    """能否停掉这个会话。返回 (ok, 原因)。别人的活会话 = 不许停。"""
    holder = owner_of_session(instance, sid)
    if holder is None:
        return True, "无主的残留会话"
    if holder == owner_key(owner):
        return True, "本任务自己的会话"
    return False, "被另一个活任务声明（%s），不许停" % holder


# ------------------------------------------------------- 窗口组（2026-11-02 用户指令）
#
# 用户要求：星舵打开的窗口要有「专用组名」，任何 agent 打开的窗口都要能互相识别，
# 新任务在**组窗口里建自己的 tab**，而不是每人开一个新窗口。
# 实现：组身份 = STARPILOT_GROUP（默认 "starpilot"）；会话的组归属记在
# **journal**（jsonl，追加式，跨租约世代存活——release 清掉租约文件后，
# 「这个会话是组里开的」这个事实仍在）。claim/drop 都走 journal。
GROUP_ENV = "STARPILOT_GROUP"
DEFAULT_GROUP = "starpilot"


def group() -> str:
    g = (os.environ.get(GROUP_ENV) or "").strip()
    return g or DEFAULT_GROUP


def _norm_sid(sid: str) -> str:
    return (sid or "").strip()


def claim_session(instance: str, sid: str, owner: dict) -> None:
    """把会话登记到本租约名下——别人据此判断「这个会话有主，不许停」。

    同时记 journal（session_claim，带组名）：租约文件随 release 消失，
    journal 是组会话的持久登记，跨任务/跨租约世代可查。
    """
    sid = _norm_sid(sid)
    if not sid:
        return
    rec = read(instance)
    if rec and rec.get("owner_key") == owner_key(owner):
        rec.setdefault("sessions", {})[sid] = {
            "owner_key": rec["owner_key"],
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        write(instance, rec)
    log(instance, "session_claim",
        {"sid": sid, "group": group(), "by": owner})


def drop_session(instance: str, sid: str, owner: dict, reason: str = "") -> None:
    """会话结束（stop/reap）后注销。journal 里加 session_drop，组登记随之失效。"""
    sid = _norm_sid(sid)
    if not sid:
        return
    rec = read(instance)
    if rec and rec.get("owner_key") == owner_key(owner):
        (rec.get("sessions") or {}).pop(sid, None)
        write(instance, rec)
    log(instance, "session_drop",
        {"sid": sid, "group": group(), "by": owner, "reason": reason})


def journal_claims(instance: str) -> dict:
    """解析 journal，返回 {sid: {"group":…, "owner_key":…, "live_lease":bool}}。

    以最后一个 session_claim/session_drop 为准；live_lease 看**当前**租约文件
    是否还活着（租约死了 = 声明作废，会话成了无主残留，可回收）。
    """
    out: dict = {}
    p = journal_path(instance)
    if not os.path.exists(p):
        return out
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ev = rec.get("event")
                sid = _norm_sid(str(rec.get("sid") or ""))
                if not sid or ev not in ("session_claim", "session_drop"):
                    continue
                if ev == "session_drop":
                    out.pop(sid, None)
                else:
                    out[sid] = {"group": rec.get("group") or "",
                                "owner_key": rec.get("by") and owner_key(
                                    rec["by"] if isinstance(rec["by"], dict) else {}),
                                "owner": rec.get("by")}
    except Exception:
        return {}
    live = is_live(read(instance))
    for sid in list(out):
        out[sid]["live_lease"] = live
    return out


def group_sessions(instance: str, grp: str = "") -> list:
    """这个实例上、组内声明过且未被注销的会话 id 列表。

    注意：**不要求当前租约活着**——轮换（release）之后会话仍存活，下一个任务
    应当复用它而不是再开一个窗口；调用方（wa.py）会拿 bsk 的活会话列表求交集，
    所以「journal 里有、bsk 里已死」的残留不会误命中。
    """
    grp = grp or group()
    claims = journal_claims(instance)
    return [sid for sid, meta in claims.items() if meta.get("group") == grp]


# --------------------------------------------------- 全局开窗计数（禁止无脑重开）
#
# 用户要求「固化一个行为：禁止无脑重复打开新窗口」。原实现把频率计数存在各任务
# 自己的 state 文件里——跨任务不可见，我拦不到你、你也拦不到我（2026-11-02 现场：
# 实际开了 10+ 会话，state 里只记了 3 次，因为裸调根本不走计数）。
# 现在计数放锁目录（跨任务共享），按实例维度记；wa.py 的**唯一**开窗入口负责记。
START_WINDOW_S = 600      # 10 分钟窗口
START_MAX = 4             # 窗口内最多开 4 次（与 wa.py 原阈值一致）


def starts_path() -> str:
    return os.path.join(lease_dir(), "session-starts.json")


def note_start(instance: str) -> None:
    now = time.time()
    p = starts_path()
    data: dict = {}
    try:
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
    except Exception:
        data = {}
    hist = [t for t in (data.get(instance) or [])
            if isinstance(t, (int, float)) and now - t < START_WINDOW_S]
    hist.append(now)
    data = {k: [t for t in v if isinstance(t, (int, float)) and now - t < START_WINDOW_S]
            for k, v in data.items() if isinstance(v, list)}
    data[instance] = hist
    tmp = p + ".tmp.%d" % os.getpid()
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, p)
    except Exception:
        pass


def too_many_starts(instance: str, window: float = START_WINDOW_S,
                    max_starts: int = START_MAX) -> tuple:
    """全局频率判定。返回 (too_many, n, max)。所有任务共享同一份计数。"""
    now = time.time()
    p = starts_path()
    n = 0
    try:
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            n = len([t for t in (data.get(instance) or [])
                     if isinstance(t, (int, float)) and now - t < window])
    except Exception:
        return False, 0, max_starts
    return n >= max_starts, n, max_starts


# --------------------------------------------------- 轮换预算（有人 / 无人值守）
#
# 用户要求：别人占用时——有人值守状态「直接提醒人」；无人值守「等」。
# 预算区别：attended 短等 30s 后带 WINDOW_BUSY_HUMAN 退出（把现场报给人）；
# unattended 长等 600s（轮换排队），等不到才 WINDOW_BUSY 退出（调用方按自己的
# 节奏重试）。wa.py 负责读 WA_ATTENDED 并把两类退出码分开。
ATTENDED_WAIT_S = 30.0
UNATTENDED_WAIT_S = 600.0
ATTENDED_ENV = "WA_ATTENDED"


def is_attended() -> bool:
    return (os.environ.get(ATTENDED_ENV) or "").strip().lower() in (
        "1", "true", "yes", "on")


def wait_budget(attended: bool | None = None) -> float:
    if attended is None:
        attended = is_attended()
    return ATTENDED_WAIT_S if attended else UNATTENDED_WAIT_S
