# -*- coding: utf-8 -*-
"""标签所有权账本（2026-11-02 Web AI 裁定后的实施）。

## 为什么必须有它

bsk 0.4.1 的 `tab list` 只给 `tab_id / title / url / window_id / active / scope`——
**没有任何创建者、任务、时间信息**（实测，见 runtime/wa/review-1102-ownership-probe.md）。
`scope=agent` 只说明「这个标签在 Agent Window 里」，不说明「它是哪个任务开的」。
所以「谁的标签」这件事，bsk 答不了，只能我们在开标签时记账。

## 形态（裁定的硬要求）

- **权威状态用 SQLite**（事务 + 唯一约束），`tab-claims.jsonl` 只作审计事件流；
- 状态变更一律在事务里校验 `owner_key + run_id + lease_id + fencing_token`；
- 同一 tab 同时只能有一个有效所有者；
- 账本写失败 / 读失败 / 与 BSK 实际列表不一致 → **fail-closed**（拒绝写操作，
  不是「当作成功」）。

## 状态

```
claimed ──close ok + verify absent──→ closed
claimed ──close 超时/结果未知───────→ unknown（must_reinspect=1，隔离）
claimed ──进程崩溃 / 租约丢失───────→ orphan（只读恢复，不自动关）
claimed ──创建成功但账本写失败──────→ owner_unknown（不能当自己的用）
borrowed（原属用户窗口）── 默认只读，永不自动关闭
```

`unknown` 是**资源隔离态**，不是普通等待态：它不允许靠 TTL 自动解除，只能
「原持有者确认死亡 + 租约过期 + 只读恢复对账」后解除（裁定 §4/§5）。

本模块不引入第二套锁：并发保护用 SQLite 事务，资源占用仍由 winlease 的
租约 + fencing 说话（裁定「不得引入与 BRM 主租约独立的锁机制」）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time

import winlease

DB_ENV = "STARPILOT_TABCLAIM_DB"
AUDIT_ENV = "STARPILOT_TABCLAIM_AUDIT"

STATE_CLAIMED = "claimed"
STATE_CLOSING = "closing"
STATE_CLOSED = "closed"
STATE_UNKNOWN = "unknown"
STATE_ORPHAN = "orphan"
STATE_OWNER_UNKNOWN = "owner_unknown"

INTENT_OPENED = "opened"
INTENT_REUSED = "reused"
INTENT_BORROWED = "borrowed"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tabs (
  session_id     TEXT NOT NULL,
  tab_id         TEXT NOT NULL,
  instance_id    TEXT NOT NULL DEFAULT '',
  window_id      INTEGER,
  owner_key      TEXT NOT NULL DEFAULT '',
  run_id         TEXT NOT NULL DEFAULT '',
  lease_id       TEXT NOT NULL DEFAULT '',
  fencing_token  INTEGER NOT NULL DEFAULT 0,
  intent         TEXT NOT NULL DEFAULT 'opened',
  origin_scope   TEXT NOT NULL DEFAULT 'agent',
  origin_window  INTEGER,
  first_url      TEXT DEFAULT '',
  last_url       TEXT DEFAULT '',
  state          TEXT NOT NULL DEFAULT 'claimed',
  must_reinspect INTEGER NOT NULL DEFAULT 0,
  updated_at     TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (session_id, tab_id)
);
CREATE INDEX IF NOT EXISTS idx_tabs_owner ON tabs(owner_key, run_id);
CREATE INDEX IF NOT EXISTS idx_tabs_state ON tabs(state);
"""

# 这些状态一律不许写（fail-closed 的落点）
_BLOCKING_STATES = (STATE_CLOSING, STATE_CLOSED, STATE_UNKNOWN, STATE_ORPHAN)


class TabClaimError(RuntimeError):
    """账本层面的拒绝：调用方必须**放弃这次写**，不是重试。"""

    def __init__(self, code: str, message: str, detail=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def as_dict(self) -> dict:
        return {"ok": False, "code": self.code, "message": self.message,
                "detail": self.detail}


# ------------------------------------------------------------------ 基础

def db_path() -> str:
    p = os.environ.get(DB_ENV) or ""
    if p:
        return p
    return os.path.join(winlease.lease_dir(), "tab-claims.db")


def audit_path() -> str:
    p = os.environ.get(AUDIT_ENV) or ""
    if p:
        return p
    return os.path.join(winlease.lease_dir(), "tab-claims.jsonl")


def connect(path: str = "") -> sqlite3.Connection:
    """打开账本（建表）。失败直接抛——账本不可用就什么都不许做。"""
    p = path or db_path()
    d = os.path.dirname(p)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    try:
        conn = sqlite3.connect(p, timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=8000")
        conn.executescript(SCHEMA)
        return conn
    except Exception as e:  # 建不了库 = fail-closed
        raise TabClaimError("TABCLAIM_DB_UNAVAILABLE",
                            "标签账本不可用（%s）：%s" % (p, e))


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _audit(event: dict) -> None:
    """审计事件流：只追加，纯记录，**不参与权限判断**。写失败不影响主流程。"""
    try:
        with open(audit_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _tx(conn: sqlite3.Connection):
    """事务上下文：BEGIN IMMEDIATE 保证写之间不交错。"""
    class _T:
        def __enter__(self):
            conn.execute("BEGIN IMMEDIATE")
            return conn

        def __exit__(self, et, ev, tb):
            if et is None:
                conn.execute("COMMIT")
            else:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
            return False
    return _T()


# ------------------------------------------------------------------ 认领

def claim(conn, session_id: str, tab_id: str, owner_key: str, *, run_id: str = "",
          lease_id: str = "", fencing_token: int = 0, instance_id: str = "",
          window_id=None, intent: str = INTENT_OPENED, url: str = "",
          origin_scope: str = "agent", origin_window=None,
          take_over: bool = False) -> dict:
    """原子认领一个标签。已被别人持有时抛 ClaimConflict（除非 take_over）。"""
    tab_id = str(tab_id)
    with _tx(conn):
        row = conn.execute("SELECT * FROM tabs WHERE session_id=? AND tab_id=?",
                           (session_id, tab_id)).fetchone()
        if row is not None:
            cur = dict(row)
            same_owner = (cur.get("owner_key") == owner_key
                          and (cur.get("run_id") or "") == (run_id or ""))
            # 已关闭的标签不阻止重新认领：浏览器可能把同一个 tab_id 复用给新标签
            # （关闭后立刻新建），账本按 (session, tab_id) 覆盖即可，状态会重置为 claimed。
            if cur["state"] in _BLOCKING_STATES and not take_over:
                raise TabClaimError(
                    "TAB_NOT_CLAIMABLE",
                    "标签 %s 处于 %s 态，不可认领（fail-closed）"
                    % (tab_id, cur["state"]), {"tab": cur})
            if cur.get("owner_key") and not same_owner and not take_over:
                raise TabClaimError(
                    "TAB_OWNED_BY_OTHER",
                    "标签 %s 属于另一个任务（%s/%s）"
                    % (tab_id, cur.get("owner_key"), cur.get("run_id")),
                    {"tab": cur})
        conn.execute("""
            INSERT INTO tabs (session_id, tab_id, instance_id, window_id, owner_key,
                              run_id, lease_id, fencing_token, intent, origin_scope,
                              origin_window, first_url, last_url, state,
                              must_reinspect, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)
            ON CONFLICT(session_id, tab_id) DO UPDATE SET
              instance_id=excluded.instance_id, window_id=excluded.window_id,
              owner_key=excluded.owner_key, run_id=excluded.run_id,
              lease_id=excluded.lease_id, fencing_token=excluded.fencing_token,
              intent=excluded.intent, last_url=CASE WHEN excluded.last_url<>''
                THEN excluded.last_url ELSE tabs.last_url END,
              state='claimed', must_reinspect=0, updated_at=excluded.updated_at
        """, (session_id, tab_id, instance_id, window_id, owner_key, run_id,
              lease_id, int(fencing_token or 0), intent, origin_scope, origin_window,
              url, url, STATE_CLAIMED, _now()))
        row = conn.execute("SELECT * FROM tabs WHERE session_id=? AND tab_id=?",
                           (session_id, tab_id)).fetchone()
    out = dict(row)
    _audit({"ts": _now(), "event": "claim", "session": session_id, "tab": tab_id,
            "owner_key": owner_key, "run_id": run_id, "intent": intent,
            "state": STATE_CLAIMED})
    return out


def get(conn, session_id: str, tab_id: str):
    row = conn.execute("SELECT * FROM tabs WHERE session_id=? AND tab_id=?",
                       (session_id, str(tab_id))).fetchone()
    return dict(row) if row else None


# ------------------------------------------------------------------ 导航硬门

def navigate_allowed(conn, session_id: str, tab_id: str, owner_key: str, *,
                     run_id: str = "", lease_id: str = "",
                     fencing_token: int = 0) -> tuple:
    """导航前所有权校验（裁定 §2 的硬门）。

    返回 (ok, reason, row)。**任何不确定都返回 False**——不确定就不许写。
    """
    tab_id = str(tab_id)
    row = get(conn, session_id, tab_id)
    if row is None:
        return False, "unclaimed", None          # 无主：必须先 claim
    if row["state"] in _BLOCKING_STATES:
        return False, "state_%s" % row["state"], row
    if row["state"] == STATE_OWNER_UNKNOWN:
        return False, "owner_unknown", row
    if row["must_reinspect"]:
        return False, "must_reinspect", row
    if row["intent"] == INTENT_BORROWED:
        return False, "borrowed_read_only", row
    if (row["owner_key"] or "") != (owner_key or ""):
        return False, "owned_by_other", row
    if run_id and (row["run_id"] or "") != run_id:
        return False, "run_mismatch", row
    if lease_id and row["lease_id"] and row["lease_id"] != lease_id:
        return False, "lease_mismatch", row
    if fencing_token and row["fencing_token"] and fencing_token < row["fencing_token"]:
        return False, "stale_fencing", row
    return True, "ok", row


def touch_url(conn, session_id: str, tab_id: str, url: str, owner_key: str) -> None:
    conn.execute("""UPDATE tabs SET last_url=?, updated_at=?
                    WHERE session_id=? AND tab_id=? AND owner_key=?""",
                 (url, _now(), session_id, str(tab_id), owner_key))


# ------------------------------------------------------------ 无主残留接管
#
# 为什么必须有这条路（2026-11-02 实测）：组窗口复用是常态——上一个任务结束后
# 会话与标签留在原地，下一个任务认领同一会话。账本里那些标签还写着旧任务的名字。
# 若一律「别人的标签不许碰」，新任务连自己的窗口都导航不了，等于把自己的门锁死。
# 判据不是「名字不同」，而是**那任租约还算不算数**（由调用方用 winlease 独立判：
# lease_id 已换代 / 心跳过期 / 租约文件消失 = 原持有者不在了）。
# 接管仍然禁止：closing/closed/unknown/orphan/owner_unknown、must_reinspect、
# borrowed（借用标签永远归用户，不随租约消失而转移）。

def takeover(conn, session_id: str, tab_id: str, owner_key: str, *,
             run_id: str = "", lease_id: str = "", fencing_token: int = 0,
             instance_id: str = "", reason: str = "stale_owner",
             url: str = "") -> dict:
    """接管「原持有者已不在」的标签。调用方必须先证明原租约失效。"""
    tab_id = str(tab_id)
    with _tx(conn):
        row = conn.execute("SELECT * FROM tabs WHERE session_id=? AND tab_id=?",
                           (session_id, tab_id)).fetchone()
        if row is None:
            raise TabClaimError("TAB_UNCLAIMED", "标签 %s 无账本记录" % tab_id)
        cur = dict(row)
        if cur["state"] in _BLOCKING_STATES:
            raise TabClaimError("TAB_NOT_CLAIMABLE",
                                "标签 %s 处于 %s 态，不可接管" % (tab_id, cur["state"]),
                                {"tab": cur})
        if cur.get("must_reinspect"):
            raise TabClaimError("TAB_NEEDS_REINSPECT",
                                "标签 %s 待复检（隔离态），不可接管" % tab_id, {"tab": cur})
        if cur["intent"] == INTENT_BORROWED:
            raise TabClaimError("TAB_BORROWED",
                                "标签 %s 是借用标签，归用户，不随租约转移" % tab_id,
                                {"tab": cur})
        conn.execute("""UPDATE tabs SET owner_key=?, run_id=?, lease_id=?,
                          fencing_token=?, instance_id=?, intent=?, state=?,
                          must_reinspect=0,
                          last_url=CASE WHEN ?<>'' THEN ? ELSE last_url END,
                          updated_at=?
                        WHERE session_id=? AND tab_id=?""",
                     (owner_key, run_id, lease_id, int(fencing_token or 0),
                      instance_id, INTENT_REUSED, STATE_CLAIMED, url, url, _now(),
                      session_id, tab_id))
        new = dict(conn.execute("SELECT * FROM tabs WHERE session_id=? AND tab_id=?",
                                (session_id, tab_id)).fetchone())
    _audit({"ts": _now(), "event": "takeover", "session": session_id, "tab": tab_id,
            "from_owner": cur.get("owner_key"), "from_run": cur.get("run_id"),
            "to_owner": owner_key, "to_run": run_id, "reason": reason})
    return new


# ------------------------------------------------------------------ 关闭

def mark_closing(conn, session_id: str, tab_id: str, owner_key: str, *,
                 run_id: str = "", fencing_token: int = 0) -> dict:
    """进入关闭临界区前占位。**不是 owner 就不许关。**"""
    tab_id = str(tab_id)
    with _tx(conn):
        row = conn.execute("SELECT * FROM tabs WHERE session_id=? AND tab_id=?",
                           (session_id, tab_id)).fetchone()
        if row is None:
            raise TabClaimError("TAB_UNCLAIMED", "标签 %s 无账本记录，拒绝关闭" % tab_id)
        cur = dict(row)
        if cur["owner_key"] != (owner_key or ""):
            raise TabClaimError("TAB_OWNED_BY_OTHER",
                                "标签 %s 不属于当前任务，拒绝关闭" % tab_id, {"tab": cur})
        if run_id and cur["run_id"] and cur["run_id"] != run_id:
            raise TabClaimError("TAB_RUN_MISMATCH", "标签 %s 属于另一轮 run" % tab_id,
                                {"tab": cur})
        if fencing_token and cur["fencing_token"] and fencing_token < cur["fencing_token"]:
            raise TabClaimError("TAB_STALE_FENCING", "fencing 落后，拒绝关闭", {"tab": cur})
        if cur["intent"] == INTENT_BORROWED:
            raise TabClaimError("TAB_BORROWED", "借用标签只能由用户关闭", {"tab": cur})
        if cur["state"] in _BLOCKING_STATES:
            raise TabClaimError("TAB_NOT_CLOSABLE", "标签处于 %s 态" % cur["state"],
                                {"tab": cur})
        conn.execute("""UPDATE tabs SET state=?, updated_at=?
                        WHERE session_id=? AND tab_id=?""",
                     (STATE_CLOSING, _now(), session_id, tab_id))
        cur["state"] = STATE_CLOSING
    _audit({"ts": _now(), "event": "closing", "session": session_id, "tab": tab_id})
    return cur


def mark_closed(conn, session_id: str, tab_id: str) -> None:
    conn.execute("""UPDATE tabs SET state=?, must_reinspect=0, updated_at=?
                    WHERE session_id=? AND tab_id=?""",
                 (STATE_CLOSED, _now(), session_id, str(tab_id)))
    _audit({"ts": _now(), "event": "closed", "session": session_id, "tab": tab_id})


def mark_unknown(conn, session_id: str, tab_id: str, reason: str = "") -> None:
    """关闭结果未知：**隔离**该标签，并要求重新检视（裁定 §4）。"""
    conn.execute("""UPDATE tabs SET state=?, must_reinspect=1, updated_at=?
                    WHERE session_id=? AND tab_id=?""",
                 (STATE_UNKNOWN, _now(), session_id, str(tab_id)))
    _audit({"ts": _now(), "event": "close_unknown", "session": session_id,
            "tab": tab_id, "reason": reason})


def mark_owner_unknown(conn, session_id: str, tab_id: str, reason: str = "") -> None:
    """BSK 侧成功、账本写失败：不能当成自己的标签用（裁定 §1 末段）。"""
    conn.execute("""UPDATE tabs SET state=?, must_reinspect=1, updated_at=?
                    WHERE session_id=? AND tab_id=?""",
                 (STATE_OWNER_UNKNOWN, _now(), session_id, str(tab_id)))
    _audit({"ts": _now(), "event": "owner_unknown", "session": session_id,
            "tab": tab_id, "reason": reason})


# ------------------------------------------------------------------ 恢复 / 对账

def reconcile(conn, session_id: str, bsk_tabs: list) -> dict:
    """账本 ↔ BSK 实际标签列表 对账（裁定 §5 第二点）。

    · 账本有、BSK 无 → 标 closed（标签确已消失）；
    · BSK 有、账本无 → 保持「未知归属」，只报告，绝不自动关；
    · 两边都有但状态异常 → 保持原状，报告。
    """
    live = {str(t.get("tab_id")): t for t in (bsk_tabs or [])}
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM tabs WHERE session_id=?", (session_id,)).fetchall()]
    gone, unknown_owner, mismatch = [], [], []
    for r in rows:
        tid = r["tab_id"]
        if tid not in live:
            if r["state"] not in (STATE_CLOSED,):
                mark_closed(conn, session_id, tid)
                gone.append(tid)
            continue
        if not (r["owner_key"] or ""):
            unknown_owner.append(tid)
        if r["state"] == STATE_CLAIMED and r["must_reinspect"]:
            mismatch.append(tid)
    known = set(r["tab_id"] for r in rows)
    for tid in live:
        if tid not in known:
            unknown_owner.append(tid)
    return {"closed_gone": gone, "unknown_owner": unknown_owner,
            "mismatch": mismatch,
            "live_in_bsk": len(live), "in_ledger": len(rows)}


def release_unknown(conn, session_id: str, tab_id: str, *, holder_alive: bool,
                    lease_expired: bool) -> tuple:
    """尝试解除 unknown 隔离（裁定 §4 的表）。

    只有「原持有者已确认死亡 + 租约已过期」才允许进入只读恢复；其余一律保持隔离。
    """
    if holder_alive:
        return False, "holder_alive_quarantine"
    if not lease_expired:
        return False, "lease_not_expired"
    conn.execute("""UPDATE tabs SET must_reinspect=1, updated_at=?
                    WHERE session_id=? AND tab_id=?""",
                 (_now(), session_id, str(tab_id)))
    _audit({"ts": _now(), "event": "read_only_recovery", "session": session_id,
            "tab": tab_id})
    return True, "read_only_recovery"


def own_tabs(conn, session_id: str, owner_key: str, run_id: str = "") -> list:
    q = "SELECT * FROM tabs WHERE session_id=? AND owner_key=?"
    args = [session_id, owner_key]
    if run_id:
        q += " AND run_id=?"
        args.append(run_id)
    q += " AND state='claimed' AND must_reinspect=0 AND intent<>'borrowed'"
    return [dict(r) for r in conn.execute(q, args).fetchall()]
