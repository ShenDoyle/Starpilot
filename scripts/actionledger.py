#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""actionledger.py —— 动作幂等账本（append-only 事件流 + SQLite 权威库）。

规范来源：PROJECT-02-R1 ARCHITECTURE §4.1 / ACTION-LEDGER + PROJECT-03-R1
任务书 §5 + 终审补充 D。

两个幂等键严格分离（终审补充：action_id 不能单独承担业务意图去重）：
- **action_id**：一次具体动作记录的身份 = a- + sha16(task_id|intent_digest|seq)。
  跨 run 提交同一 (task_id, intent, seq) 必命中同一 action_id → SP-ACT-001。
- **intent_key**：业务意图的去重身份 = ik- + sha16(canonical(intent_payload))。
  与 task_id / run / 进程**无关**。去重作用域 = 本机账本全局（边界：单机、
  本地磁盘文件；不适合网络盘/多机）。活跃意图（非终态）唯一约束：
  新 task_id、新 run、新 seq 都绕不开同一个未决意图 → SP-ACT-002 /
  SP-ACT-003。COMPLETED/NOT_SENT 为终态后意图不再占用（合法后续操作应在
  intent_payload 里带区分字段如订单号/序号，规则在 normalize 文档化）。

状态机（唯一规范 ARCHITECTURE §4.1）：
CREATED → RESERVED → DISPATCHED → COMPLETED
                         ├→ SEND_UNKNOWN → ANTI_REPLAY_LOCKED（resend_count 恒 0）
                         └→ NOT_SENT（仅派发前确证未发出）

并发与持久化边界：SQLite 单文件 + WAL + busy_timeout；同机多进程并发预占由
唯一索引 + 事务原子性保证（跨进程，不依赖进程内锁）。不适用网络文件系统。
崩溃恢复：recover_unknown() 扫描「有 DISPATCHED 无 outcome」→ SEND_UNKNOWN +
anti_replay_locked + resend_count=0（SP-ACT-005 OUTCOME_MISSING）。
四条禁止推断（无 POST ⇒ NOT_SENT 等）由接口形状强制：恢复只产生 UNKNOWN，
NOT_SENT 仅能由「派发前明确失败」路径 commit。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import unicodedata

import sp_errors as SP

_ACTIVE_STATES = ("CREATED", "RESERVED", "DISPATCHED", "SEND_UNKNOWN",
                  "ANTI_REPLAY_LOCKED")


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class MigrationBlocked(Exception):
    """P2-1-E：旧库迁移预检发现规范化后活跃态重复 —— 停止迁移并报告，
    禁止自动删除/合并/覆盖（GPT 任务书 §P2-1-E）。conflicts 为冲突清单。"""

    def __init__(self, conflicts):
        self.conflicts = conflicts
        super().__init__(
            "business_target migration blocked: %d normalized conflicts — "
            "resolve manually, original rows preserved" % len(conflicts))


DELEGATED_KIND = "DISPATCH_DELEGATED"
DELEGATED_PROTOCOL_VERSION = "1"
# 权威事件身份字段（与 P2-2-1 Envelope / P2-2-2 Marker 完全一致的那部分）
_DELEGATED_IDENTITY = ("op_id", "envelope_digest", "fencing", "worker_id",
                       "key_version", "business_target_norm")


def _delegated_validate(action_id, op_id, envelope_digest, fencing, worker_id,
                        key_version, business_target_norm):
    """严格校验（任何缺失/类型不符/格式非法 → ValueError）。"""
    vals = {"action_id": action_id, "op_id": op_id,
            "envelope_digest": envelope_digest, "fencing": fencing,
            "worker_id": worker_id, "key_version": key_version}
    for k, v in vals.items():
        if not isinstance(v, str) or not v:
            raise ValueError("%s must be non-empty str" % k)
    if not re.match(r"^[0-9a-f]{64}$", envelope_digest):
        raise ValueError("envelope_digest not sha256 hex64")
    if not re.match(r"^[0-9]{1,19}$", fencing):
        raise ValueError("fencing not decimal string")
    if not isinstance(business_target_norm, str):
        raise ValueError("business_target_norm must be str")
    return {"action_id": action_id, "op_id": op_id,
            "envelope_digest": envelope_digest, "fencing": fencing,
            "worker_id": worker_id, "key_version": key_version,
            "business_target_norm": business_target_norm}


def normalize_business_target(target):
    """P2-1-A：normalize(business_target) **唯一实现**（所有 reserve/查询/
    冲突检测/风险查询路径统一走本函数，禁止各入口自行清洗）。

    规则（保守，防过度归一——不同业务对象不得被错误合并）：
    - None → None（是否 fail-closed 由调用方按 side_effect 语义决定）；
    - 非 str → ValueError（fail-closed）；
    - 去首尾空白（P1S2-A §3 案例 7：'order:123 ' 变体归一）；
    - Unicode NFC（组合/预组合字符等价）；
    - casefold（P1S2-A §3 案例 7：大小写变体归一）；
    - **不做**：内部空白折叠、URL/路径语义改写、全半角折叠（NFKC 不用）。
    空串/纯空白 → None（视为缺失）。
    """
    if target is None:
        return None
    if not isinstance(target, str):
        raise ValueError("business_target must be str, got %r" % type(target).__name__)
    t = unicodedata.normalize("NFC", target.strip()).casefold()
    return t or None


def normalize_intent(intent_payload: dict) -> str:
    """intent 规范化（唯一规则来源）：
    - 递归排序键、紧凑分隔、字符串去首尾空白（值内部不动，不改写业务语义）；
    - 明文存 digest，原始 payload 落库供审计；
    - 相似但不同的文本是**不同意图**；合法后续操作必须带区分字段
      （如 order_no / seq / round），由调用方负责放入 intent_payload。
    """
    return json.dumps(_norm(intent_payload), sort_keys=True,
                      ensure_ascii=False, separators=(",", ":"))


def _norm(obj):
    if isinstance(obj, str):
        return obj.strip()
    if isinstance(obj, dict):
        return {str(k): _norm(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_norm(v) for v in obj]
    return obj


def intent_key_of(intent_payload: dict) -> str:
    return "ik-" + _sha16(normalize_intent(intent_payload))


def action_id_of(task_id: str, intent_digest: str, seq: int) -> str:
    return "a-" + _sha16("%s|%s|%d" % (task_id, intent_digest, int(seq)))


# ---------------------------------------------------------------- SP3-1 DDL
# 裁决协议结构。设计冻结依据：SP3-0R / SP3-0R2 / SP3-0R3-FINAL（均经终审）。
# 核心不变量（全部由 DB 强制，不依赖任何单一应用入口）：
#   ① 证据只追加：封存后不得 INSERT；UPDATE/DELETE 恒禁；REPLACE 由
#      recursive_triggers=ON + DELETE 触发器拦截（D13）。
#   ② 封存不可逆：插入时必须未封存；封存要求 ≥1 条证据；封存后 sealed_at
#      不可再变；封存不得与消费/撤销发生在同一条 UPDATE。
#   ③ 生命周期单向一次：consumed_* / revoked_* 只能 NULL→值 一次；两者互斥；
#      且必须在封存之后（D05）。
#   ④ 业务内容不可变：16 个业务字段任何 UPDATE 均 ABORT。
#   ⑤ 主表 append-only：DELETE 恒禁，槽位只能靠撤销释放。
_ADJUDICATION_DDL = (
    """CREATE TABLE IF NOT EXISTS adjudications (
        adjudication_id      TEXT PRIMARY KEY,
        action_id            TEXT NOT NULL,
        intent_key           TEXT NOT NULL,
        business_target_norm TEXT NOT NULL,
        fencing_generation   INTEGER NOT NULL CHECK (fencing_generation >= 0),
        decision             TEXT NOT NULL CHECK (decision IN
                               ('DECIDED_SENT','DECIDED_NOT_SENT',
                                'CONFIRMED_UNKNOWN','DEFER','ESCALATE')),
        evidence_hash        TEXT NOT NULL CHECK (length(evidence_hash) = 64
                               AND evidence_hash NOT GLOB '*[^0-9a-f]*'),
        operator             TEXT NOT NULL CHECK (length(trim(operator)) > 0),
        reviewer             TEXT,
        authorization_ref    TEXT NOT NULL CHECK (length(trim(authorization_ref)) > 0),
        reason               TEXT NOT NULL CHECK (length(trim(reason)) >= 20),
        scope                TEXT NOT NULL CHECK (length(trim(scope)) > 0),
        decided_at           REAL NOT NULL,
        reviewed_at          REAL,
        consumed_at          REAL,
        consumed_by          TEXT,
        revoked_at           REAL,
        revoked_by           TEXT,
        revocation_reason    TEXT,
        supersedes_id        TEXT REFERENCES adjudications(adjudication_id)
                                 ON DELETE RESTRICT,
        created_at           REAL NOT NULL,
        sealed_at            REAL,
        CHECK (decision <> 'DECIDED_NOT_SENT' OR (
                   reviewer IS NOT NULL AND length(trim(reviewer)) > 0
                   AND reviewer <> operator AND reviewed_at IS NOT NULL)),
        CHECK ((consumed_at IS NULL) = (consumed_by IS NULL)),
        CHECK ((revoked_at IS NULL) = (revoked_by IS NULL)
               AND (revoked_at IS NULL) = (revocation_reason IS NULL)),
        CHECK (consumed_at IS NULL OR consumed_at >= decided_at),
        CHECK (revoked_at IS NULL OR revoked_at >= decided_at),
        CHECK (consumed_at IS NULL OR revoked_at IS NULL),
        CHECK (revoked_at IS NULL OR decision IN
                 ('DECIDED_SENT','DECIDED_NOT_SENT')),
        CHECK (revoked_at IS NULL OR length(trim(revocation_reason)) >= 20),
        CHECK (supersedes_id IS NULL OR supersedes_id <> adjudication_id),
        CHECK (sealed_at IS NULL OR sealed_at >= decided_at),
        CHECK (consumed_by IS NULL OR length(trim(consumed_by)) > 0),
        CHECK (revoked_by IS NULL OR length(trim(revoked_by)) > 0),
        CHECK (consumed_at IS NULL OR sealed_at IS NOT NULL),
        CHECK (revoked_at IS NULL OR sealed_at IS NOT NULL)
    )""",
    """CREATE TABLE IF NOT EXISTS adjudication_evidence (
        adjudication_id TEXT NOT NULL
            REFERENCES adjudications(adjudication_id) ON DELETE RESTRICT,
        seq             INTEGER NOT NULL CHECK (seq >= 1),
        type            TEXT NOT NULL CHECK (type IN
                          ('page_observation','backend_receipt','external_query',
                           'manual_attestation','storage_audit')),
        locator         TEXT NOT NULL CHECK (length(trim(locator)) > 0),
        captured_at_ms  INTEGER NOT NULL CHECK (captured_at_ms > 0),
        PRIMARY KEY (adjudication_id, seq)
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS ux_adj_consumable
        ON adjudications(action_id)
        WHERE consumed_at IS NULL AND revoked_at IS NULL
          AND sealed_at IS NOT NULL
          AND decision IN ('DECIDED_SENT','DECIDED_NOT_SENT')""",
    """CREATE INDEX IF NOT EXISTS ix_adj_action
        ON adjudications(action_id, decided_at DESC)""",
    """CREATE INDEX IF NOT EXISTS ix_adj_pending
        ON adjudications(action_id)
        WHERE consumed_at IS NULL AND revoked_at IS NULL
          AND sealed_at IS NOT NULL""",
    """CREATE INDEX IF NOT EXISTS ix_adj_supersedes
        ON adjudications(supersedes_id) WHERE supersedes_id IS NOT NULL""",
    """CREATE INDEX IF NOT EXISTS ix_adj_revoked
        ON adjudications(revoked_at) WHERE revoked_at IS NOT NULL""",
    """CREATE INDEX IF NOT EXISTS ix_ev_adj
        ON adjudication_evidence(adjudication_id, seq)""",
    """CREATE INDEX IF NOT EXISTS ix_ev_type
        ON adjudication_evidence(type, captured_at_ms DESC)""",
    """CREATE TRIGGER IF NOT EXISTS trg_adj_insert_unsealed
        BEFORE INSERT ON adjudications
        WHEN NEW.sealed_at IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'must_seal_after_evidence'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_adj_seal_requires_evidence
        BEFORE UPDATE OF sealed_at ON adjudications
        WHEN NEW.sealed_at IS NOT NULL AND OLD.sealed_at IS NULL
         AND (SELECT COUNT(*) FROM adjudication_evidence
              WHERE adjudication_id = NEW.adjudication_id) < 1
        BEGIN SELECT RAISE(ABORT, 'seal_requires_evidence'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_adj_seal_exclusive
        BEFORE UPDATE OF sealed_at ON adjudications
        WHEN NEW.sealed_at IS NOT NULL AND OLD.sealed_at IS NULL
         AND (NEW.consumed_at IS NOT NULL OR NEW.revoked_at IS NOT NULL)
        BEGIN SELECT RAISE(ABORT, 'seal_must_be_separate_from_lifecycle'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_ev_no_insert_after_seal
        BEFORE INSERT ON adjudication_evidence
        WHEN (SELECT sealed_at FROM adjudications
              WHERE adjudication_id = NEW.adjudication_id) IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'evidence_sealed'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_ev_no_update
        BEFORE UPDATE ON adjudication_evidence
        BEGIN SELECT RAISE(ABORT, 'evidence_immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_ev_no_delete
        BEFORE DELETE ON adjudication_evidence
        BEGIN SELECT RAISE(ABORT, 'evidence_immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_adj_immutable_cols
        BEFORE UPDATE ON adjudications
        WHEN NEW.adjudication_id      IS NOT OLD.adjudication_id
          OR NEW.action_id            IS NOT OLD.action_id
          OR NEW.intent_key           IS NOT OLD.intent_key
          OR NEW.business_target_norm IS NOT OLD.business_target_norm
          OR NEW.fencing_generation   IS NOT OLD.fencing_generation
          OR NEW.decision             IS NOT OLD.decision
          OR NEW.evidence_hash        IS NOT OLD.evidence_hash
          OR NEW.operator             IS NOT OLD.operator
          OR NEW.reviewer             IS NOT OLD.reviewer
          OR NEW.authorization_ref    IS NOT OLD.authorization_ref
          OR NEW.reason               IS NOT OLD.reason
          OR NEW.scope                IS NOT OLD.scope
          OR NEW.decided_at           IS NOT OLD.decided_at
          OR NEW.reviewed_at          IS NOT OLD.reviewed_at
          OR NEW.supersedes_id        IS NOT OLD.supersedes_id
          OR NEW.created_at           IS NOT OLD.created_at
          OR (OLD.sealed_at IS NOT NULL AND NEW.sealed_at IS NOT OLD.sealed_at)
          OR (OLD.consumed_at IS NOT NULL AND NEW.consumed_at IS NOT OLD.consumed_at)
          OR (OLD.consumed_by IS NOT NULL AND NEW.consumed_by IS NOT OLD.consumed_by)
          OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NOT OLD.revoked_at)
          OR (OLD.revoked_by IS NOT NULL AND NEW.revoked_by IS NOT OLD.revoked_by)
          OR (OLD.revocation_reason IS NOT NULL
              AND NEW.revocation_reason IS NOT OLD.revocation_reason)
        BEGIN SELECT RAISE(ABORT, 'adjudication_immutable_field'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_adj_no_delete
        BEFORE DELETE ON adjudications
        BEGIN SELECT RAISE(ABORT, 'adjudication_append_only'); END""",
)

_ADJ_EVIDENCE_TYPES = ("page_observation", "backend_receipt", "external_query",
                       "manual_attestation", "storage_audit")
_ADJ_DECISIONS = ("DECIDED_SENT", "DECIDED_NOT_SENT", "CONFIRMED_UNKNOWN",
                  "DEFER", "ESCALATE")
_ADJ_CONSUMABLE = ("DECIDED_SENT", "DECIDED_NOT_SENT")
_ADJ_RESERVED_OPERATORS = ("system", "bot", "auto")


def _adj_hash(rows) -> str:
    """evidence_hash 规范化（SP3-0R2 §2.2，逐字节确定）。

    rows: [(seq, type, locator, captured_at_ms)]，按 seq 升序编码：
        b"adj-v1\x1e" + Σ(type \x1f NFC(locator).strip() \x1f str(ms) \x1e)
    \x1f/\x1e 不在任何合法字段内容中，拼接无歧义。换算法必须换前缀。
    """
    payload = b"adj-v1\x1e"
    for seq, typ, loc, ms in sorted(rows, key=lambda r: int(r[0])):
        payload += (str(typ).encode("ascii") + b"\x1f"
                    + unicodedata.normalize("NFC", str(loc)).strip().encode("utf-8")
                    + b"\x1f" + str(int(ms)).encode("ascii") + b"\x1e")
    return hashlib.sha256(payload).hexdigest()


def _is_conn_level_fault(exc) -> bool:
    """SQLite 错误是否属于**连接级持久**故障（换连接才有救，重试同一连接无效）。

    实测依据（Windows + WAL + 多进程首次建库）：
    - `attempt to write a readonly database`：连接持有的映射已失效，同一连接
      上重试恒定失败；
    - `disk I/O error` / `unable to open database file`：同上。
    普通 `database is locked` 不在此列——那是可等待的事务锁，busy_timeout 已覆盖。
    """
    msg = str(exc).lower()
    return any(k in msg for k in (
        "readonly", "disk i/o error", "unable to open database file"))


def _apply_pragmas(conn) -> None:
    """连接级 PRAGMA 的唯一实现（SP3-1 / D06 + D13）。

    为什么必须集中：`foreign_keys` 与 `recursive_triggers` 都是**连接级**配置，
    且**在事务内设置会静默失效**（实测：事务内 `PRAGMA foreign_keys=OFF` 后
    读回仍为 1）。三个连接入口若各写一份，必然出现「同一进程内两套约束语义」
    ——主连接拒绝孤儿证据、委派写连接却允许，这类不一致正是 G6 的成因。

    `recursive_triggers=ON` 不是可选项：`INSERT OR REPLACE` 在 OFF 时会**先删
    冲突行且不触发 DELETE 触发器**，实测能把已写入的证据行静默替换掉
    （详见 SP3-1 P1-2 实验）。开启后 DELETE 触发器才会在 REPLACE 路径生效。

    设置后立即读回断言，任何一项没生效即抛错（fail-closed，不允许静默降级）。
    """
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA recursive_triggers=ON")
    if int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise RuntimeError("foreign_keys not enabled on this connection")
    if int(conn.execute("PRAGMA recursive_triggers").fetchone()[0]) != 1:
        raise RuntimeError("recursive_triggers not enabled on this connection")


def _infra_retry(fn):
    """P2-1-C（GPT 任务书）：**基础设施瞬态**重试——仅 sqlite3.OperationalError    （Windows WAL 多进程偶发 readonly / busy），最多 3 次尝试、指数退避。
    语义安全：重试**从头重跑整个事务**（含全部 SELECT 检查），不跳过任何门。
    **严禁**用于 sqlite3.IntegrityError——那是业务约束冲突（L-biz/L-int 唯一
    索引裁决），重试即二次绕过（GPT 明令禁止的失败模式）。IntegrityError 在
    方法内部被捕获分流返回，不会到达本装饰器。"""
    import functools

    @functools.wraps(fn)
    def wrapper(self, *a, **k):
        for i in range(3):
            try:
                return fn(self, *a, **k)
            except sqlite3.OperationalError as e:
                try:
                    self._conn.execute("ROLLBACK")  # 清理可能残留的打开事务
                except Exception:
                    pass
                if i == 2:
                    raise
                try:
                    self._reconnect()  # readonly 为连接级持久故障：换新连接再试
                except Exception:
                    pass
                time.sleep(0.05 * (i + 1))
    return wrapper


class ActionLedger:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
        self._conn = sqlite3.connect(db_path, timeout=15.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        # busy_timeout 必须先于 journal_mode：多进程同时打开时 WAL 设置本身
        # 也可能撞锁（否则用默认 5s 超时，且 WAL 判定失败的报错不可控）
        # SP3-1：PRAGMA 统一走 _apply_pragmas（含 foreign_keys / recursive_triggers
        # 与读回断言），三个连接入口必须同源，漏一处即两套约束语义。
        _apply_pragmas(self._conn)
        self._init_schema_with_retry()

    def _init_schema_with_retry(self, attempts: int = 6) -> None:
        import time as _t
        for i in range(attempts):
            try:
                self._init_schema()
                return
            except sqlite3.OperationalError as e:
                if i == attempts - 1:
                    raise
                # readonly / disk I/O 是**连接级持久**故障（与 _reconnect 同一结论）：
                # 同一连接上重试注定无效，必须换新映射再重建 schema。多进程同时
                # 首次建库时最易撞上——只用 sleep 重试等于把偶发故障变成必然失败。
                if _is_conn_level_fault(e):
                    try:
                        self._reconnect()
                    except Exception:
                        pass
                _t.sleep(0.5 * (i + 1))

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def _reconnect(self) -> None:
        """基础设施瞬态恢复：关闭并重建连接（Windows WAL 多进程偶发
        readonly 为**连接级持久**故障——同一连接重试无效，必须新映射）。
        重建后 schema 不变（库文件权威），无需重跑 _init_schema。"""
        try:
            self._conn.close()
        except Exception:
            pass
        self._conn = sqlite3.connect(self.db_path, timeout=15.0,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        _apply_pragmas(self._conn)

    def _init_schema(self) -> None:
        c = self._conn
        c.execute("""
            CREATE TABLE IF NOT EXISTS actions (
                action_id        TEXT PRIMARY KEY,
                task_id          TEXT NOT NULL,
                intent_key       TEXT NOT NULL,
                intent_digest    TEXT NOT NULL,
                intent_payload   TEXT NOT NULL,
                seq              INTEGER NOT NULL DEFAULT 0,
                state            TEXT NOT NULL,
                result_tag       TEXT,
                dispatch_count   INTEGER NOT NULL DEFAULT 0,
                resend_count     INTEGER NOT NULL DEFAULT 0,
                anti_replay_locked INTEGER NOT NULL DEFAULT 0,
                fencing_generation INTEGER,
                executor_id      TEXT,
                run_id           TEXT,
                reservation      TEXT,
                outcome_evidence TEXT,
                created_at       REAL,
                dispatched_at    REAL,
                committed_at     REAL,
                updated_at       REAL
            )""")
        # 活跃意图全局唯一：跨 task_id / 跨 run 防重绕不过去（终审补充的核心）
        c.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_intent_active
            ON actions(intent_key)
            WHERE state IN ('CREATED','RESERVED','DISPATCHED','SEND_UNKNOWN',
                            'ANTI_REPLAY_LOCKED')""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        REAL NOT NULL,
                action_id TEXT NOT NULL,
                kind      TEXT NOT NULL,
                detail    TEXT
            )""")
        # R3-S（GPT 终审 §3.3）：业务对象风险约束——Intent 身份防重与业务对象
        # 风险约束是两个层级。business_target 记录动作的业务对象标识；
        # 同对象存在未决 UNKNOWN 时，换 intent_key 的新意图不得自动放行。
        # 列追加兼容旧库（列已存在则忽略）。
        try:
            c.execute("ALTER TABLE actions ADD COLUMN business_target TEXT")
        except sqlite3.OperationalError:
            pass
        # P2-1（GPT 任务书 §P2-1-B/E）：业务对象锁唯一主键 = normalize(business_target)。
        # ① 规范化列追加（兼容旧库）② 历史行幂等回填 ③ 迁移预检（规范化后活跃态
        # 重复 → MigrationBlocked 停止迁移，禁止自动合并/删除）④ 部分唯一索引——
        # 由数据库保证并发唯一性；应用层 SELECT 仅作提前判断（P2-1-B）。
        # 状态谓词覆盖全部活跃态（GPT §P2-1-B：RESERVED/DISPATCHED/SEND_UNKNOWN/
        # 其他未完成风险解除的状态；UNKNOWN 必须继续占有 L-biz）。
        try:
            c.execute("ALTER TABLE actions ADD COLUMN business_target_norm TEXT")
        except sqlite3.OperationalError:
            pass
        rows = c.execute(
            "SELECT action_id, state, business_target FROM actions "
            "WHERE business_target IS NOT NULL AND business_target_norm IS NULL"
        ).fetchall()
        # P2-1-E：迁移预检**先于回填**（索引可能已由历史 init 建立，直接 UPDATE
        # 会撞唯一索引而非给出可读冲突清单）。用 Python 计算规范化结果，检测：
        # ① 候选行与库内既有活跃 norm 冲突 ② 候选行之间的活跃冲突。
        # 命中 → MigrationBlocked 停止迁移，禁止自动删除/合并/覆盖。
        _idx_exists = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='ux_biz_active'"
        ).fetchone() is not None
        if rows:
            existing_active = {r[0] for r in c.execute(
                "SELECT DISTINCT business_target_norm FROM actions "
                "WHERE business_target_norm IS NOT NULL AND state IN (%s)"
                % ",".join("?" * len(_ACTIVE_STATES)),
                tuple(_ACTIVE_STATES)).fetchall()}
            computed, seen, conflicts = [], {}, []
            for r in rows:
                try:
                    norm = normalize_business_target(r["business_target"])
                except ValueError:
                    norm = None  # 历史非法值保留原样供审计，不参与锁键
                computed.append((r, norm))
                if norm and _idx_exists and r["state"] in _ACTIVE_STATES and \
                        (norm in existing_active or norm in seen):
                    conflicts.append({"action_id": r["action_id"],
                                      "business_target_norm": norm, "n": 2})
                seen[norm] = r["action_id"]
            if conflicts:
                raise MigrationBlocked(conflicts)
            for r, norm in computed:
                c.execute("UPDATE actions SET business_target_norm=? WHERE action_id=?",
                          (norm, r["action_id"]))
        dup = c.execute(
            "SELECT business_target_norm, COUNT(*) AS n FROM actions "
            "WHERE business_target_norm IS NOT NULL AND state IN (%s) "
            "GROUP BY business_target_norm HAVING n > 1"
            % ",".join("?" * len(_ACTIVE_STATES)),
            tuple(_ACTIVE_STATES)).fetchall()
        if dup:
            raise MigrationBlocked([dict(r) for r in dup])
        c.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_biz_active
            ON actions(business_target_norm)
            WHERE business_target_norm IS NOT NULL
              AND state IN ('CREATED','RESERVED','DISPATCHED','SEND_UNKNOWN',
                            'ANTI_REPLAY_LOCKED')""")
        # ---------------- SP3-1：裁决协议（adjudications + 证据子表 + 触发器）
        # 设计冻结见 SP3-0R/0R2/0R3-FINAL；此处只落结构，不改既有 actions/events。
        for _sql in _ADJUDICATION_DDL:
            c.execute(_sql)

    def _ev(self, action_id: str, kind: str, detail: str = "") -> None:
        self._conn.execute(
            "INSERT INTO events(ts, action_id, kind, detail) VALUES (?,?,?,?)",
            (time.time(), action_id, kind, detail))

    @staticmethod
    def _r(ok: bool, code: str, data: dict | None = None, evidence: str = "",
           fencing_generation: int | None = None) -> dict:
        return SP.result(ok, code, data, evidence, fencing_generation)

    # ------------------------------------------------------------- API
    @_infra_retry
    def reserve(self, task_id: str, intent_payload: dict, seq: int = 0,
                run_id: str = "", executor_id: str = "",
                fencing_generation: int | None = None,
                budget_units: int = 1,
                business_target: str | None = None,
                authorization_ref: str = "",
                side_effect: bool = False) -> dict:
        """登记意图并预占额度：CREATED→RESERVED（原子）。

        - 同 (task_id, intent, seq) 已存在 → SP-ACT-001 ACTION_DUPLICATE；
        - intent_key 已被**任何** task 的活跃意图占用 → SP-ACT-002
          INTENT_COLLISION（跨 task_id 绕过检测点）；
        - intent_key 的历史记录里已有 **COMPLETED(SENT)** 的同业务意图 →
          SP-ACT-008 INTENT_REPLAY_NEEDS_NEW_IDENTITY（R3-D D2/D5）；
        - 业务对象风险约束（R3-S U1/U2 + P2-0 终审 T09 修订）：业务对象
          normalize 后存在活跃 L-biz 锁（含未决 UNKNOWN）时，**任何新意图
          一律 SP-ACT-009 拒绝——authorization_ref 只作为审计参考记录在
          拒绝证据里，不构成绕过**。授权、风险裁定、锁释放是三个独立动作；
          释放只能走 unlock/裁决流程（P2-3 落地）。
        - business_target 规范化（P2-1-A）：normalize_business_target()
          唯一实现；side_effect=True 且 target 缺失/空/非法 → SP-ACT-010
          fail-closed（P1-A §68 四档必填：仅 side-effect 档强制，只读档
          不触发 L-biz，P1S2-A §3 案例 10）；
        - 历史为 COMPLETED(NOT_SENT) → **允许**重新预占（R3-D D3）；
        - 并发竞速输家由 ux_biz_active / ux_intent_active 两个唯一索引
          裁决，IntegrityError 按约束分流（P2-1-C），**无任何自动重试**；
        - 额度约束：同一 task_id 活跃预占总数 ≤ budget 上限 → SP-ACT-004。
        """
        ik = intent_key_of(intent_payload)
        digest = _sha16(normalize_intent(intent_payload))
        aid = action_id_of(task_id, digest, seq)
        now = time.time()
        c = self._conn
        # P2-1-A：规范化（唯一实现）；非 str 类型 fail-closed 归入缺失
        try:
            tnorm = normalize_business_target(business_target)
        except ValueError:
            tnorm = None
            tnorm_invalid = True
        else:
            tnorm_invalid = False
        if side_effect and (tnorm is None or tnorm_invalid):
            return self._r(False, SP.SP_ACT_010,
                           {"business_target": business_target,
                            "invalid_type": tnorm_invalid},
                           "side_effect reserve requires a non-empty, valid "
                           "business_target — fail-closed (SP-ACT-010)")
        ik = intent_key_of(intent_payload)
        digest = _sha16(normalize_intent(intent_payload))
        aid = action_id_of(task_id, digest, seq)
        now = time.time()
        c = self._conn
        try:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM actions WHERE action_id=?",
                            (aid,)).fetchone()
            if row is not None:
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_001,
                               {"action_id": aid, "state": row["state"]},
                               "same (task_id, intent, seq) already ledgered")
            active = c.execute(
                "SELECT action_id, task_id, state FROM actions "
                "WHERE intent_key=? AND state IN (%s)"
                % ",".join("?" * len(_ACTIVE_STATES)),
                (ik, *(_ACTIVE_STATES))).fetchone()
            if active is not None:
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_002,
                               {"action_id": aid, "intent_key": ik,
                                "holder_action_id": active["action_id"],
                                "holder_task_id": active["task_id"],
                                "holder_state": active["state"]},
                               "intent already active under another action/task — "
                               "new task_id cannot bypass")
            # R3-D D2/D5：同 intent_key 的历史终态里已有 SENT → 原样重放必须
            # 走新意图身份（payload 带业务区分字段），不允许只换 task_id。
            prior = c.execute(
                "SELECT action_id, task_id, result_tag FROM actions "
                "WHERE intent_key=? AND state='COMPLETED' ORDER BY created_at",
                (ik,)).fetchall()
            sent_prior = [p for p in prior if p["result_tag"] == "SENT"]
            if sent_prior:
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_008,
                               {"intent_key": ik,
                                "prior_action_id": sent_prior[-1]["action_id"],
                                "prior_task_id": sent_prior[-1]["task_id"]},
                               "identical business intent already completed SENT — "
                               "replay needs a new intent identity (payload round/order field), "
                               "not just a new task_id")
            # R3-S U1/U2 + P2-0 终审 T09（GPT §一.1）：业务对象风险约束。
            # 同一业务对象存在活跃 L-biz 锁时，任何新意图一律拒绝；
            # authorization_ref 仅作为审计参考随拒绝证据返回，不构成绕过。
            if tnorm:
                pending = c.execute(
                    "SELECT action_id, task_id, state, intent_key FROM actions "
                    "WHERE business_target_norm=? AND state IN (%s)"
                    % ",".join("?" * len(_ACTIVE_STATES)),
                    (tnorm, *(_ACTIVE_STATES))).fetchone()
                if pending is not None:
                    c.execute("ROLLBACK")
                    return self._r(False, SP.SP_ACT_009,
                                   {"business_target": business_target,
                                    "business_target_norm": tnorm,
                                    "holder_action_id": pending["action_id"],
                                    "holder_task_id": pending["task_id"],
                                    "holder_state": pending["state"],
                                    "authorization_ref_recorded": bool(authorization_ref)},
                                   "business object has an active L-biz lock — "
                                   "new intent identity (and/or authorization_ref) "
                                   "does not bypass; release only via unlock/"
                                   "adjudication flow")
            n_active = c.execute(
                "SELECT COUNT(*) FROM actions WHERE task_id=? AND state IN (%s)"
                % ",".join("?" * len(_ACTIVE_STATES)),
                (task_id, *(_ACTIVE_STATES))).fetchone()[0]
            if budget_units > 0 and n_active >= int(budget_units):
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_004,
                               {"active": n_active, "budget": budget_units},
                               "task active reservations exhausted")
            c.execute(
                "INSERT INTO actions(action_id, task_id, intent_key, intent_digest,"
                " intent_payload, seq, state, dispatch_count, resend_count,"
                " anti_replay_locked, fencing_generation, executor_id, run_id,"
                " reservation, business_target, business_target_norm,"
                " created_at, updated_at)"
                " VALUES (?,?,?,?,?,?, 'RESERVED',0,0,0,?,?,?,?,?,?,?,?)",
                (aid, task_id, ik, digest, normalize_intent(intent_payload),
                 int(seq), fencing_generation, executor_id, run_id,
                 json.dumps({"budget_units": budget_units,
                             "reserved_at": now, "state": "RESERVED"}),
                 business_target, tnorm, now, now))
            if prior:  # COMPLETED(NOT_SENT) 历史 → 同一意图的合法重试（无副作用已确证）
                self._ev(aid, "replay_after_not_sent",
                         "prior=%s task=%s (D3: NOT_SENT proven, safe retry)"
                         % (prior[-1]["action_id"], prior[-1]["task_id"]))
            self._ev(aid, "reserved", "intent_key=%s task=%s seq=%s" % (ik, task_id, seq))
            if tnorm:
                self._ev(aid, "business_target_recorded",
                         "target=%s norm=%s (no active L-biz lock at reserve time)"
                         % (business_target, tnorm))
            c.execute("COMMIT")
        except sqlite3.IntegrityError as e:
            try:
                c.execute("ROLLBACK")
            except Exception:
                pass
            # P2-1-C（GPT 任务书）：并发竞速输家由唯一索引裁决，**按约束分流**，
            # 严禁把所有 IntegrityError 混为一种冲突，**严禁自动重试**
            # （防 IntegrityError→通用捕获→重试→二次绕过）。
            # ① intent_key 活跃唯一索引输家 → SP-ACT-002
            ih = c.execute(
                "SELECT action_id, task_id, state FROM actions "
                "WHERE intent_key=? AND state IN (%s)"
                % ",".join("?" * len(_ACTIVE_STATES)),
                (ik, *(_ACTIVE_STATES))).fetchone()
            if ih is not None:
                return self._r(False, SP.SP_ACT_002,
                               {"action_id": aid, "intent_key": ik,
                                "holder_action_id": ih["action_id"],
                                "holder_task_id": ih["task_id"],
                                "sqlite_error": str(e)},
                               "concurrent reserve lost ux_intent_active race — no retry")
            # ② business_target_norm 活跃唯一索引输家 → SP-ACT-009
            if tnorm:
                bh = c.execute(
                    "SELECT action_id, task_id, state FROM actions "
                    "WHERE business_target_norm=? AND state IN (%s)"
                    % ",".join("?" * len(_ACTIVE_STATES)),
                    (tnorm, *(_ACTIVE_STATES))).fetchone()
                if bh is not None:
                    return self._r(False, SP.SP_ACT_009,
                                   {"action_id": aid,
                                    "business_target": business_target,
                                    "business_target_norm": tnorm,
                                    "holder_action_id": bh["action_id"],
                                    "holder_task_id": bh["task_id"],
                                    "holder_state": bh["state"],
                                    "sqlite_error": str(e)},
                                   "concurrent reserve lost ux_biz_active race — "
                                   "database enforced L-biz uniqueness, no retry")
            # ③ 其余约束冲突（如 action_id 主键并发）→ 通用不变量破坏
            return self._r(False, SP.SP_ACT_007,
                           {"action_id": aid, "sqlite_error": str(e)},
                           "unexpected constraint violation — no retry (fail-closed)")
        return self._r(True, SP.SP_OK,
                       {"action_id": aid, "intent_key": ik, "state": "RESERVED"},
                       "reserved", fencing_generation)

    @_infra_retry
    def mark_dispatched(self, action_id: str, fencing_generation: int,
                        executor_id: str) -> dict:
        """RESERVED→DISPATCHED，dispatch_count+1。必须在外部副作用**之前**调用。"""
        c = self._conn
        now = time.time()
        try:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM actions WHERE action_id=?",
                            (action_id,)).fetchone()
            if row is None:
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_005, {},
                               "unknown action_id")
            if row["state"] != "RESERVED":
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_003,
                               {"state": row["state"], "locked": bool(row["anti_replay_locked"])},
                               "dispatch refused from state %s (anti-replay)" % row["state"])
            if row["dispatch_count"] >= 1:
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_007, {"dispatch_count": row["dispatch_count"]},
                               "dispatch_count would exceed 1 — budget invariant broken")
            c.execute("UPDATE actions SET state='DISPATCHED', dispatch_count=1,"
                      " fencing_generation=?, executor_id=?, dispatched_at=?,"
                      " updated_at=? WHERE action_id=?",
                      (int(fencing_generation), executor_id, now, now, action_id))
            self._ev(action_id, "dispatched", "fencing=%s" % fencing_generation)
            c.execute("COMMIT")
        except sqlite3.Error as e:
            try:
                c.execute("ROLLBACK")
            except Exception:
                pass
            return self._r(False, SP.SP_ACT_007, {}, "sqlite error: %s" % e)
        return self._r(True, SP.SP_OK, {"action_id": action_id, "state": "DISPATCHED"},
                       "dispatched (side effect may now happen)", int(fencing_generation))

    @_infra_retry
    def takeover_fencing(self, action_id: str, from_generation: int,
                         new_executor: str) -> dict:
        """显式接管（SP1-A）：generation + 1，所有权转移给 new_executor。

        安全约束（全部 fail-closed）：
        - 状态只允许 RESERVED / DISPATCHED。SEND_UNKNOWN / ANTI_REPLAY_LOCKED /
          COMPLETED 一律拒绝——**接管不得成为解锁或改判通道**（与 G6 一致方向）。
        - `from_generation` 必须严格等于账本当前值（CAS）；不等 → SP-ACT-013，
          且不写任何状态（旧持有者不能靠猜 generation 抢所有权）。
        - 账本当前无 fencing（NULL）时不得接管（无所有权可转移）。
        - `new_executor` 必须非空，且不得等于当前 executor_id（否则是空转）。
        - 只改 fencing_generation 与 executor_id；**不改 state / dispatch_count /
          result_tag / anti_replay_locked**——接管不是状态推进。
        """
        if not isinstance(new_executor, str) or not new_executor.strip():
            return self._r(False, SP.SP_ACT_013, {"new_executor": new_executor},
                           "takeover denied — new_executor must be a non-empty string")
        c = self._conn
        now = time.time()
        try:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM actions WHERE action_id=?",
                            (action_id,)).fetchone()
            if row is None:
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_005, {}, "unknown action_id")
            st = row["state"]
            if st not in ("RESERVED", "DISPATCHED"):
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_013,
                               {"state": st,
                                "reason": "state_not_takeoverable"},
                               "takeover denied — state %s is not takeoverable "
                               "(unknown/completed cannot be taken over)" % st)
            cur_f = row["fencing_generation"]
            if cur_f is None:
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_013,
                               {"state": st, "reason": "no_fencing_held"},
                               "takeover denied — no fencing recorded to transfer")
            if int(cur_f) != int(from_generation):
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_013,
                               {"state": st, "locked_fencing": int(cur_f),
                                "from_generation": from_generation,
                                "reason": "cas_mismatch"},
                               "takeover denied — from_generation does not match "
                               "ledger (compare-and-swap failed)")
            if row["executor_id"] == new_executor:
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_013,
                               {"state": st, "executor_id": row["executor_id"],
                                "reason": "same_executor"},
                               "takeover denied — new_executor equals current executor")
            new_f = int(cur_f) + 1
            c.execute("UPDATE actions SET fencing_generation=?, executor_id=?,"
                      " updated_at=? WHERE action_id=?",
                      (new_f, new_executor, now, action_id))
            self._ev(action_id, "fencing_takeover",
                     "from=%d to=%d old_executor=%s new_executor=%s state=%s"
                     % (int(cur_f), new_f, row["executor_id"] or "", new_executor, st))
            c.execute("COMMIT")
        except sqlite3.Error as e:
            try:
                c.execute("ROLLBACK")
            except Exception:
                pass
            return self._r(False, SP.SP_ACT_007, {}, "sqlite error: %s" % e)
        return self._r(True, SP.SP_OK,
                       {"action_id": action_id, "state": st,
                        "fencing_generation": new_f,
                        "previous_generation": int(cur_f),
                        "executor_id": new_executor},
                       "fencing taken over (ownership transferred, state unchanged)",
                       new_f)

    @_infra_retry
    def commit_outcome(self, action_id: str, result_tag: str, evidence: str = "",
                       unlock: dict | None = None,
                       expected_fencing: int | None = None) -> dict:
        """落 outcome：DISPATCHED→COMPLETED(result_tag)。

        **SP1-A（2026-11-13）所有权校验下沉**
        此前 fencing 复核只在 `ExecutionGate.commit()` 应用层做，账本自身不校验，
        任何直连账本的入口（恢复、运维脚本、未来新入口）都能绕过（P2-3-0 C0）。
        现改为账本内校验：账本记录了 fencing（非 NULL）时，调用方**必须**声明
        `expected_fencing` 且严格相等；否则 SP-ACT-011 / SP-ACT-012 拒绝。
        账本无 fencing（RESERVED 且从未标记派发）时，调用方也不得凭空声明。

        **G6-HOTFIX（2026-11-13，PROJECT-04 独立安全修复）**
        SEND_UNKNOWN / ANTI_REPLAY_LOCKED 状态下**拒绝所有解锁调用**。
        历史缺陷：旧实现只校验 `unlock.get("kind")` 非空，任意 `{"kind":"x"}`
        即可把 UNKNOWN 推成 COMPLETED 并释放 L-biz —— 安全边界失效（G6 / P0）。
        当前不存在可信裁决数据源（`adjudications` 表与 `decide()` 属 P2-3 / SP3），
        因此没有任何「可验证、可审计、作用域匹配且未消费」的授权记录可供校验，
        任何解锁请求都必须 fail-closed。`unlock` 参数保留以兼容调用点签名，
        但其内容一律不再被采信。
        SP3 落地后此处改为校验 adjudication_id 对应记录（存在/未消费/decision
        与 result_tag 匹配/fencing 一致/作用域匹配），**不得保留 kind 兼容分支**。
        result_tag 只允许 SENT / NOT_SENT；NOT_SENT 仅限派发前路径。
        """
        if result_tag not in ("SENT", "NOT_SENT"):
            return self._r(False, SP.SP_ACT_007, {"result_tag": result_tag},
                           "result_tag must be SENT|NOT_SENT (unknown is not an outcome)")
        c = self._conn
        now = time.time()
        try:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM actions WHERE action_id=?",
                            (action_id,)).fetchone()
            if row is None:
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_005, {}, "unknown action_id")
            st = row["state"]
            if st == "DISPATCHED":
                pass
            elif st == "RESERVED":
                # 派发前崩溃/取消：唯一合法路径是 NOT_SENT（禁止推断的例外）
                if result_tag != "NOT_SENT":
                    c.execute("ROLLBACK")
                    return self._r(False, SP.SP_ACT_007, {"state": st},
                                   "RESERVED can only commit NOT_SENT (never inferred SENT)")
            elif st in ("SEND_UNKNOWN", "ANTI_REPLAY_LOCKED"):
                # G6-HOTFIX：解锁入口无条件拒绝（不区分 unlock 形态、不做 kind 兼容）
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_003,
                               {"state": st,
                                "unlock_present": bool(unlock),
                                "reason": "unknown_unlock_disabled"},
                               "unknown unlock disabled — no trusted adjudication "
                               "source (G6-HOTFIX); UNKNOWN cannot be completed by "
                               "caller-supplied fields")
            elif st == "COMPLETED":
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_001, {"state": st},
                               "already completed")
            else:
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_007, {"state": st},
                               "commit from state %s not allowed" % st)
            # ---------------- SP1-A：所有权校验（账本层，任何入口不可绕过）
            locked_f = row["fencing_generation"]
            if expected_fencing is None:
                if locked_f is not None:
                    c.execute("ROLLBACK")
                    return self._r(False, SP.SP_ACT_011,
                                   {"state": st, "locked_fencing": int(locked_f)},
                                   "fencing owner unproven — ledger records ownership "
                                   "but caller supplied no expected_fencing")
            else:
                if locked_f is None:
                    c.execute("ROLLBACK")
                    return self._r(False, SP.SP_ACT_012,
                                   {"state": st, "locked_fencing": None,
                                    "claimed": expected_fencing},
                                   "fencing mismatch — caller claims ownership but "
                                   "ledger holds none")
                if int(locked_f) != int(expected_fencing):
                    c.execute("ROLLBACK")
                    return self._r(False, SP.SP_ACT_012,
                                   {"state": st, "locked_fencing": int(locked_f),
                                    "claimed": expected_fencing},
                                   "stale fencing — caller is not the current owner")

            if result_tag == "NOT_SENT" and row["dispatch_count"] >= 1:
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_007, {"dispatch_count": row["dispatch_count"]},
                               "NOT_SENT forbidden after dispatch (four forbidden inferences)")
            c.execute("UPDATE actions SET state='COMPLETED', result_tag=?,"
                      " outcome_evidence=?, committed_at=?, updated_at=?,"
                      " anti_replay_locked=0 WHERE action_id=?",
                      (result_tag, evidence, now, now, action_id))
            self._ev(action_id, "outcome", "tag=%s evidence=%s" % (result_tag, evidence))
            c.execute("COMMIT")
        except sqlite3.Error as e:
            try:
                c.execute("ROLLBACK")
            except Exception:
                pass
            return self._r(False, SP.SP_ACT_007, {}, "sqlite error: %s" % e)
        return self._r(True, SP.SP_OK, {"action_id": action_id,
                                        "state": "COMPLETED", "result_tag": result_tag},
                       "outcome committed")

    # ------------------------------------------------- SP3-1 裁决协议 API
    @_infra_retry
    def decide_adjudication(self, action_id: str, decision: str, evidence,
                            operator: str, authorization_ref: str, reason: str,
                            scope: str, expected_fencing: int | None = None,
                            reviewer: str | None = None,
                            reviewed_at: float | None = None,
                            supersedes_id: str | None = None,
                            adjudication_id: str | None = None,
                            decided_at: float | None = None) -> dict:
        """记录一条裁决并封存（**只记录，不解锁**；SP3-0R2 §5.1）。

        evidence: [(type, locator, captured_at_ms), ...]，seq 由本方法按 1..n 分配。
        全流程单事务，任一步失败整体 ROLLBACK（失败不留主表/证据残留）。

        与 `commit_outcome` 的分工（P0）：本方法**不改变 action 状态**，也**不**
        授予派发权限。解锁只能由 `consume_adjudication()` 消费一条已封存裁决完成。
        """
        # ---------- 阶段 A：事务外预校验（不写任何数据）
        if decision not in _ADJ_DECISIONS:
            return self._r(False, SP.SP_ACT_015, {"decision": decision},
                           "decision not in protocol enum")
        if not isinstance(evidence, (list, tuple)) or not evidence:
            return self._r(False, SP.SP_ACT_015, {"count": 0},
                           "at least one evidence row required")
        rows = []
        for i, item in enumerate(evidence, 1):
            try:
                typ, loc, ms = item
            except Exception:
                return self._r(False, SP.SP_ACT_015, {"index": i},
                               "evidence item must be (type, locator, captured_at_ms)")
            if typ not in _ADJ_EVIDENCE_TYPES:
                return self._r(False, SP.SP_ACT_015, {"index": i, "type": typ},
                               "evidence type not in whitelist")
            if not isinstance(loc, str) or not loc.strip():
                return self._r(False, SP.SP_ACT_015, {"index": i},
                               "evidence locator must be non-empty string")
            try:
                ms = int(ms)
            except Exception:
                return self._r(False, SP.SP_ACT_015, {"index": i},
                               "captured_at_ms must be integer")
            if ms <= 0:
                return self._r(False, SP.SP_ACT_015, {"index": i, "ms": ms},
                               "captured_at_ms must be positive")
            rows.append((i, typ, loc.strip(), ms))
        if not isinstance(operator, str) or not operator.strip():
            return self._r(False, SP.SP_ACT_015, {},
                           "operator must be non-empty (no pseudonymous system actor)")
        operator = operator.strip()
        if operator.lower() in _ADJ_RESERVED_OPERATORS:
            return self._r(False, SP.SP_ACT_015, {"operator": operator},
                           "operator may not be a reserved system identity")
        if not isinstance(authorization_ref, str) or not authorization_ref.strip():
            return self._r(False, SP.SP_ACT_015, {}, "authorization_ref required")
        if not isinstance(scope, str) or not scope.strip():
            return self._r(False, SP.SP_ACT_015, {}, "scope required")
        if not isinstance(reason, str) or len(reason.strip()) < 20:
            return self._r(False, SP.SP_ACT_015, {"reason_len": len((reason or "").strip())},
                           "reason must be at least 20 characters")
        if decision == "DECIDED_NOT_SENT":
            if not isinstance(reviewer, str) or not reviewer.strip():
                return self._r(False, SP.SP_ACT_015, {},
                               "DECIDED_NOT_SENT requires a reviewer")
            if reviewer.strip() == operator:
                return self._r(False, SP.SP_ACT_015, {"operator": operator},
                               "reviewer must differ from operator (two-person rule)")
            if reviewed_at is None:
                return self._r(False, SP.SP_ACT_015, {},
                               "DECIDED_NOT_SENT requires reviewed_at")
        now = time.time()
        decided_at = float(decided_at) if decided_at is not None else now
        lo_ms, hi_ms = int((decided_at - 86400) * 1000), int(decided_at * 1000)
        for i, _t, _l, ms in rows:
            if not (lo_ms <= ms <= hi_ms):
                return self._r(False, SP.SP_ACT_015,
                               {"index": i, "captured_at_ms": ms,
                                "window": [lo_ms, hi_ms]},
                               "evidence captured_at outside [decided-24h, decided]")
        digest = _adj_hash(rows)
        if adjudication_id is None:
            adjudication_id = "adj-" + _sha16("%s|%s|%s|%d"
                                              % (action_id, decision, digest,
                                                 int(decided_at * 1000)))

        c = self._conn
        try:
            c.execute("BEGIN IMMEDIATE")
            act = c.execute("SELECT * FROM actions WHERE action_id=?",
                            (action_id,)).fetchone()
            if act is None:
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_005, {}, "unknown action_id")
            if act["state"] == "COMPLETED":
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_016, {"state": act["state"]},
                               "action already completed — nothing to adjudicate")
            # fencing：与 commit_outcome 同规则（账本有则必须声明且相等）
            locked_f = act["fencing_generation"]
            if expected_fencing is None:
                if locked_f is not None:
                    c.execute("ROLLBACK")
                    return self._r(False, SP.SP_ACT_011,
                                   {"locked_fencing": int(locked_f)},
                                   "fencing owner unproven at adjudication")
            else:
                if locked_f is None or int(locked_f) != int(expected_fencing):
                    c.execute("ROLLBACK")
                    return self._r(False, SP.SP_ACT_012,
                                   {"locked_fencing": None if locked_f is None
                                    else int(locked_f), "claimed": expected_fencing},
                                   "fencing mismatch at adjudication")
            # supersedes：存在 / 同 action / 已封存 / 已退出槽位 / 链无环
            if supersedes_id is not None:
                prev = c.execute("SELECT action_id, sealed_at, consumed_at, revoked_at"
                                 " FROM adjudications WHERE adjudication_id=?",
                                 (supersedes_id,)).fetchone()
                if prev is None:
                    c.execute("ROLLBACK")
                    return self._r(False, SP.SP_ACT_015,
                                   {"supersedes_id": supersedes_id},
                                   "supersedes target missing")
                if prev["action_id"] != action_id:
                    c.execute("ROLLBACK")
                    return self._r(False, SP.SP_ACT_015,
                                   {"prev_action": prev["action_id"],
                                    "action_id": action_id},
                                   "supersedes must reference the same action_id")
                if prev["sealed_at"] is None:
                    c.execute("ROLLBACK")
                    return self._r(False, SP.SP_ACT_015,
                                   {"supersedes_id": supersedes_id},
                                   "supersedes target not sealed")
                # 槽位唯一（D05）与取代（D11）的交点：旧裁决必须**已退出可消费
                # 槽位**，否则新裁决无法落位（唯一索引会抛原始 UNIQUE 文本，对
                # 调用方毫无指引）。这里给出可执行的错误，而不是让它撞索引。
                if prev["consumed_at"] is None and prev["revoked_at"] is None:
                    c.execute("ROLLBACK")
                    return self._r(False, SP.SP_ACT_015,
                                   {"supersedes_id": supersedes_id,
                                    "occupies_slot": True},
                                   "supersedes target still holds the consumable "
                                   "slot — revoke it before superseding")
                seen, cur, depth = set(), supersedes_id, 0
                while cur is not None and depth < 64:
                    if cur in seen:
                        c.execute("ROLLBACK")
                        return self._r(False, SP.SP_ACT_015,
                                       {"supersedes_id": supersedes_id},
                                       "supersedes chain already cyclic")
                    seen.add(cur)
                    nxt = c.execute("SELECT supersedes_id FROM adjudications "
                                    "WHERE adjudication_id=?", (cur,)).fetchone()
                    cur = nxt["supersedes_id"] if nxt else None
                    depth += 1
                if depth >= 64:
                    c.execute("ROLLBACK")
                    return self._r(False, SP.SP_ACT_015,
                                   {"supersedes_id": supersedes_id},
                                   "supersedes chain too deep — refuse")
            c.execute(
                "INSERT INTO adjudications(adjudication_id, action_id, intent_key,"
                " business_target_norm, fencing_generation, decision, evidence_hash,"
                " operator, reviewer, authorization_ref, reason, scope, decided_at,"
                " reviewed_at, supersedes_id, created_at, sealed_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                (adjudication_id, action_id, act["intent_key"],
                 act["business_target_norm"] or "", int(expected_fencing or 0),
                 decision, digest, operator,
                 (reviewer.strip() if isinstance(reviewer, str) and reviewer.strip()
                  else None),
                 authorization_ref.strip(), reason.strip(), scope.strip(),
                 decided_at, reviewed_at, supersedes_id, now))
            c.executemany(
                "INSERT INTO adjudication_evidence(adjudication_id, seq, type,"
                " locator, captured_at_ms) VALUES (?,?,?,?,?)",
                [(adjudication_id, sq, tp, lc, ms) for sq, tp, lc, ms in rows])
            stored = c.execute("SELECT COUNT(*) FROM adjudication_evidence "
                               "WHERE adjudication_id=?", (adjudication_id,)).fetchone()[0]
            if int(stored) < 1:
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_015, {}, "no evidence rows persisted")
            # hash 绑定：由**实际落库的证据集合**复算，必须与主表摘要一致（D14）
            got = c.execute("SELECT seq, type, locator, captured_at_ms FROM "
                            "adjudication_evidence WHERE adjudication_id=? "
                            "ORDER BY seq", (adjudication_id,)).fetchall()
            if _adj_hash([tuple(r) for r in got]) != digest:
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_015, {},
                               "evidence hash does not match persisted evidence set")
            c.execute("UPDATE adjudications SET sealed_at=? WHERE adjudication_id=?",
                      (max(now, decided_at), adjudication_id))
            self._ev(action_id, "adjudicated",
                     "id=%s decision=%s hash=%s evidence=%d"
                     % (adjudication_id, decision, digest[:16], len(rows)))
            c.execute("COMMIT")
        except sqlite3.IntegrityError as e:
            try:
                c.execute("ROLLBACK")
            except Exception:
                pass
            return self._r(False, SP.SP_ACT_015, {"detail": str(e)[:160]},
                           "adjudication rejected by schema constraint")
        except sqlite3.Error as e:
            try:
                c.execute("ROLLBACK")
            except Exception:
                pass
            return self._r(False, SP.SP_ACT_007, {}, "sqlite error: %s" % e)
        return self._r(True, SP.SP_OK,
                       {"adjudication_id": adjudication_id, "decision": decision,
                        "evidence_hash": digest, "action_id": action_id},
                       "adjudication recorded and sealed (no state change)")

    @_infra_retry
    def consume_adjudication(self, action_id: str, adjudication_id: str,
                             result_tag: str, expected_fencing: int | None = None,
                             consumed_by: str = "", evidence: str = "") -> dict:
        """消费一条已封存裁决 → 只对 SEND_UNKNOWN / ANTI_REPLAY_LOCKED 生效。

        这是 UNKNOWN 的**唯一**解锁通道。`commit_outcome()` 对 UNKNOWN 仍保持
        G6-HOTFIX 的无条件拒绝（不得修改、不得绕过）。
        """
        if result_tag not in ("SENT", "NOT_SENT"):
            return self._r(False, SP.SP_ACT_007, {"result_tag": result_tag},
                           "result_tag must be SENT|NOT_SENT")
        c = self._conn
        now = time.time()
        try:
            c.execute("BEGIN IMMEDIATE")
            adj = c.execute("SELECT * FROM adjudications WHERE adjudication_id=?",
                            (adjudication_id,)).fetchone()
            if adj is None:
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_016, {"adjudication_id": adjudication_id},
                               "unknown adjudication")
            if adj["action_id"] != action_id:
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_016,
                               {"adj_action": adj["action_id"], "action_id": action_id},
                               "adjudication/action mismatch")
            if adj["sealed_at"] is None:
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_016, {}, "adjudication not sealed")
            if adj["decision"] not in _ADJ_CONSUMABLE:
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_015, {"decision": adj["decision"]},
                               "decision is not consumable")
            want = "SENT" if adj["decision"] == "DECIDED_SENT" else "NOT_SENT"
            if want != result_tag:
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_015,
                               {"decision": adj["decision"], "result_tag": result_tag},
                               "decision/result_tag mismatch")
            act = c.execute("SELECT * FROM actions WHERE action_id=?",
                            (action_id,)).fetchone()
            if act is None:
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_005, {}, "unknown action_id")
            if act["state"] not in ("SEND_UNKNOWN", "ANTI_REPLAY_LOCKED"):
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_016, {"state": act["state"]},
                               "state not adjudicable")
            # fencing：账本与裁决必须一致，调用方也必须证明自己是当前持有者
            if expected_fencing is None:
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_011, {},
                               "consumer must prove fencing ownership")
            if (act["fencing_generation"] is None
                    or int(act["fencing_generation"]) != int(expected_fencing)
                    or int(adj["fencing_generation"]) != int(expected_fencing)):
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_012,
                               {"action_fencing": None if act["fencing_generation"] is None
                                else int(act["fencing_generation"]),
                                "adj_fencing": int(adj["fencing_generation"]),
                                "claimed": expected_fencing},
                               "stale fencing — consumer is not the current owner")
            if (act["intent_key"] != adj["intent_key"]
                    or (act["business_target_norm"] or "") != adj["business_target_norm"]):
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_016, {},
                               "adjudication binding mismatch (intent/target)")
            # 证据摘要复算（不信任已存值）
            got = c.execute("SELECT seq, type, locator, captured_at_ms FROM "
                            "adjudication_evidence WHERE adjudication_id=? "
                            "ORDER BY seq", (adjudication_id,)).fetchall()
            if not got or _adj_hash([tuple(r) for r in got]) != adj["evidence_hash"]:
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_015, {},
                               "evidence hash mismatch at consume")
            cur = c.execute(
                "UPDATE adjudications SET consumed_at=?, consumed_by=?"
                " WHERE adjudication_id=? AND consumed_at IS NULL"
                " AND revoked_at IS NULL AND sealed_at IS NOT NULL",
                (now, consumed_by or adj["operator"], adjudication_id))
            if cur.rowcount != 1:
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_014, {"adjudication_id": adjudication_id},
                               "adjudication already consumed or revoked")
            cur2 = c.execute(
                "UPDATE actions SET state='COMPLETED', result_tag=?, outcome_evidence=?,"
                " committed_at=?, updated_at=?, anti_replay_locked=0"
                " WHERE action_id=? AND state IN ('SEND_UNKNOWN','ANTI_REPLAY_LOCKED')",
                (result_tag, evidence or adj["evidence_hash"], now, now, action_id))
            if cur2.rowcount != 1:
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_016, {},
                               "action state changed during consumption")
            self._ev(action_id, "adjudication_consumed",
                     "id=%s decision=%s" % (adjudication_id, adj["decision"]))
            self._ev(action_id, "outcome",
                     "tag=%s evidence=%s via=adjudication:%s"
                     % (result_tag, evidence or adj["evidence_hash"], adjudication_id))
            c.execute("COMMIT")
        except sqlite3.Error as e:
            try:
                c.execute("ROLLBACK")
            except Exception:
                pass
            return self._r(False, SP.SP_ACT_007, {}, "sqlite error: %s" % e)
        return self._r(True, SP.SP_OK,
                       {"action_id": action_id, "adjudication_id": adjudication_id,
                        "state": "COMPLETED", "result_tag": result_tag},
                       "unknown resolved by sealed adjudication")

    @_infra_retry
    def revoke_adjudication(self, adjudication_id: str, revoked_by: str,
                            reason: str) -> dict:
        """撤销一条**未消费**的裁决（释放待消费槽位）。

        撤销不篡改历史：decision / evidence / evidence_hash 均不可变，只是把
        `revoked_*` 从 NULL 写成值一次。已消费的裁决不可撤销（SP-ACT-014）。
        """
        if not isinstance(revoked_by, str) or not revoked_by.strip():
            return self._r(False, SP.SP_ACT_015, {}, "revoked_by required")
        if not isinstance(reason, str) or len(reason.strip()) < 20:
            return self._r(False, SP.SP_ACT_015, {}, "revocation reason must be >=20 chars")
        c = self._conn
        now = time.time()
        try:
            c.execute("BEGIN IMMEDIATE")
            # 事件必须落在**该裁决所属的 action** 上：events.action_id 是 action 维度
            # 审计轴，写入裁决 ID 会让该事件在按 action 追溯时不可见（且凭空造出
            # 一个不存在的 action_id）。
            owner = c.execute("SELECT action_id FROM adjudications WHERE adjudication_id=?",
                              (adjudication_id,)).fetchone()
            if owner is None:
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_016, {"adjudication_id": adjudication_id},
                               "unknown adjudication")
            cur = c.execute(
                "UPDATE adjudications SET revoked_at=?, revoked_by=?, revocation_reason=?"
                " WHERE adjudication_id=? AND consumed_at IS NULL"
                " AND revoked_at IS NULL AND sealed_at IS NOT NULL",
                (now, revoked_by.strip(), reason.strip(), adjudication_id))
            if cur.rowcount != 1:
                c.execute("ROLLBACK")
                return self._r(False, SP.SP_ACT_014, {"adjudication_id": adjudication_id},
                               "not revocable: already consumed, revoked or unsealed")
            self._ev(owner["action_id"], "adjudication_revoked",
                     "adj=%s by=%s reason=%s"
                     % (adjudication_id, revoked_by.strip(), reason.strip()[:80]))
            c.execute("COMMIT")
        except sqlite3.Error as e:
            try:
                c.execute("ROLLBACK")
            except Exception:
                pass
            return self._r(False, SP.SP_ACT_007, {}, "sqlite error: %s" % e)
        return self._r(True, SP.SP_OK, {"adjudication_id": adjudication_id},
                       "adjudication revoked (slot released)")

    @_infra_retry
    def recover_unknown(self) -> dict:
        """崩溃恢复：DISPATCHED 且无 outcome → SEND_UNKNOWN + anti_replay_locked。

        resend_count 恒 0（INV-8）。NOT_SENT 永不由恢复产生（禁止推断）。
        """
        c = self._conn
        now = time.time()
        recovered = []
        try:
            c.execute("BEGIN IMMEDIATE")
            rows = c.execute(
                "SELECT action_id, task_id FROM actions WHERE state='DISPATCHED'"
            ).fetchall()
            for row in rows:
                c.execute("UPDATE actions SET state='SEND_UNKNOWN',"
                          " anti_replay_locked=1, updated_at=? WHERE action_id=?",
                          (now, row["action_id"]))
                self._ev(row["action_id"], "recover_unknown",
                         "SP-ACT-005 OUTCOME_MISSING; resend_count stays 0")
                recovered.append({"action_id": row["action_id"],
                                  "task_id": row["task_id"]})
            c.execute("COMMIT")
        except sqlite3.Error as e:
            try:
                c.execute("ROLLBACK")
            except Exception:
                pass
            return self._r(False, SP.SP_ACT_007, {}, "sqlite error: %s" % e)
        return self._r(True, SP.SP_OK, {"recovered": recovered,
                                        "count": len(recovered)},
                       "DISPATCHED-without-outcome → SEND_UNKNOWN (anti-replay locked)")

    def business_risk(self, business_target: str) -> dict:
        """业务对象风险查询（只读，R3-S U1~U5 治理检查用）：
        该业务对象是否存在活跃 L-biz 锁。按 normalize(business_target)
        规范化查询（P2-1-A：所有查询路径统一走唯一规范化实现）。"""
        tnorm = normalize_business_target(business_target)
        rows = self._conn.execute(
            "SELECT action_id, task_id, intent_key, state, business_target "
            "FROM actions WHERE business_target_norm=? AND state IN (%s)"
            % ",".join("?" * len(_ACTIVE_STATES)),
            (tnorm, *(_ACTIVE_STATES))).fetchall() if tnorm else []
        return {"business_target": business_target,
                "business_target_norm": tnorm,
                "pending_unknown": [dict(r) for r in rows],
                "count": len(rows)}

    def get(self, action_id: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM actions WHERE action_id=?",
                                 (action_id,)).fetchone()
        return dict(row) if row else None

    def get_by_intent(self, intent_key: str) -> list:
        rows = self._conn.execute(
            "SELECT * FROM actions WHERE intent_key=? ORDER BY created_at",
            (intent_key,)).fetchall()
        return [dict(r) for r in rows]

    def locked_intents(self, task_id: str | None = None) -> list:
        if task_id:
            rows = self._conn.execute(
                "SELECT action_id, task_id, intent_key, state FROM actions "
                "WHERE task_id=? AND anti_replay_locked=1", (task_id,)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT action_id, task_id, intent_key, state FROM actions "
                "WHERE anti_replay_locked=1").fetchall()
        return [dict(r) for r in rows]

    def events(self, action_id: str) -> list:
        rows = self._conn.execute(
            "SELECT ts, kind, detail FROM events WHERE action_id=? ORDER BY id",
            (action_id,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------- P2-2-3 DISPATCH_DELEGATED
    # 权威派发事件：Marker 证明本地派发前证据已写入；DELEGATED 回答
    # 「某 op_id 是否已持久提交允许进入派发边界的权威记录」。二者不可互替。
    # 零 Schema 变更：复用 events 表（append-only，仅 INSERT、无 UPDATE/DELETE
    # 路径）；kind=DISPATCH_DELEGATED；全部身份字段入 detail JSON。
    def write_delegated(self, action_id: str, op_id: str, envelope_digest: str,
                        fencing: str, worker_id: str, key_version: str,
                        business_target_norm: str = "") -> dict:
        """独立 SQLite 事务写入权威事件（先于 mark_dispatched 提交）。

        独立事务边界：每次调用使用**独立连接**（BEGIN IMMEDIATE … COMMIT），
        与调用方事务无共享状态——COMMIT 成功返回即证明事件已持久提交。
        BEGIN IMMEDIATE 串行化写者：SELECT-then-INSERT 在写锁内原子，
        无需新增唯一索引即可保证单 op_id 单事件（零 Schema 变更）。

        幂等与身份冲突（任务书 §3）：
        - 同 action_id 已有 DELEGATED 事件：
          * op_id/digest/fencing/worker/key_version 全等 → 幂等成功（不重复写）；
          * 任一不等 → SP-OP-002 身份冲突拒绝，原事件绝不覆盖。
        - 基础设施瞬态（OperationalError）最多重试 3 次，每次重试重新执行
          全部门检查（含幂等判定）——已提交事件在重试中按幂等返回，
          绝不重复写入；IntegrityError 永不重试。
        - 任何失败 → ok=False（上层必须禁止派发）。
        """
        try:
            fields = _delegated_validate(action_id, op_id, envelope_digest,
                                         fencing, worker_id, key_version,
                                         business_target_norm)
        except ValueError as e:
            return self._r(False, SP.SP_OP_001, {"reason": str(e)},
                           "delegated field validation failed")
        last = ""
        for attempt in range(3):
            try:
                return self._write_delegated_once(fields)
            except sqlite3.OperationalError as e:  # 仅基础设施瞬态
                last = str(e)
                time.sleep(0.05 * (attempt + 1))
        return self._r(False, SP.SP_OP_001,
                       {"reason": "ledger unavailable: %s" % last},
                       "delegated write unavailable — dispatch forbidden")

    def _write_delegated_once(self, fields: dict) -> dict:
        conn = sqlite3.connect(self.db_path, timeout=15.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            _apply_pragmas(conn)
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT id, ts, detail FROM events WHERE action_id=? AND kind=? "
                "ORDER BY id", (fields["action_id"], DELEGATED_KIND)).fetchall()
            if rows:
                try:
                    existing = json.loads(rows[0]["detail"])
                except ValueError:
                    conn.execute("ROLLBACK")
                    return self._r(False, SP.SP_OP_001,
                                   {"reason": "existing delegated event unparseable",
                                    "event_id": rows[0]["id"]},
                                   "delegated conflict: corrupt existing evidence")
                diff = [k for k in _DELEGATED_IDENTITY
                        if existing.get(k) != fields[k]]
                conn.execute("ROLLBACK")
                if diff:
                    return self._r(False, SP.SP_OP_002,
                                   {"conflict_fields": diff,
                                    "op_id": fields["op_id"],
                                    "existing_event_id": rows[0]["id"]},
                                   "delegated identity conflict — original "
                                   "evidence never overwritten")
                return self._r(True, "", {"event_id": rows[0]["id"],
                                          "idempotent": True,
                                          "op_id": fields["op_id"]},
                               "delegated already committed (idempotent)")
            now = time.time()
            detail = json.dumps({**fields, "protocol_version":
                                 DELEGATED_PROTOCOL_VERSION,
                                 "created_at": now},
                                sort_keys=True, ensure_ascii=False,
                                separators=(",", ":"))
            cur = conn.execute(
                "INSERT INTO events(ts, action_id, kind, detail) VALUES (?,?,?,?)",
                (now, fields["action_id"], DELEGATED_KIND, detail))
            conn.execute("COMMIT")   # 独立事务真实提交边界
            return self._r(True, "", {"event_id": cur.lastrowid,
                                      "idempotent": False,
                                      "op_id": fields["op_id"]},
                           "delegated committed in independent transaction")
        except sqlite3.IntegrityError as e:  # 永不重试
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            return self._r(False, SP.SP_OP_002,
                           {"reason": "integrity: %s" % str(e)[:120]},
                           "delegated integrity conflict — no retry")
        finally:
            conn.close()

    def delegated_events(self, action_id: str | None = None,
                         op_id: str | None = None) -> dict:
        """权威事件查询（恢复对账用）。

        返回 {"ok", "events", "complete", "present"}：
        - 查询失败（Ledger 不可用）→ ok=False（调用方必须 UNKNOWN，
          **禁止把事件缺失解释为未派发**——DG07）；
        - 同一 action_id 的事件 detail 解析失败 → complete=False
          （拒绝给出安全结论）；
        - present 只表示「存在完整可解析的 DELEGATED 事件」。
        """
        try:
            if action_id:
                rows = self._conn.execute(
                    "SELECT id, ts, action_id, detail FROM events "
                    "WHERE action_id=? AND kind=? ORDER BY id",
                    (action_id, DELEGATED_KIND)).fetchall()
            elif op_id:
                rows = self._conn.execute(
                    "SELECT id, ts, action_id, detail FROM events "
                    "WHERE kind=? ORDER BY id", (DELEGATED_KIND,)).fetchall()
            else:
                return {"ok": False, "reason": "action_id or op_id required",
                        "events": [], "complete": False, "present": False}
        except sqlite3.Error as e:
            return {"ok": False, "reason": "ledger unavailable: %s" % e,
                    "events": [], "complete": False, "present": False}
        out, complete = [], True
        for r in rows:
            try:
                d = json.loads(r["detail"])
            except ValueError:
                complete = False
                continue
            if op_id and d.get("op_id") != op_id:
                continue
            out.append({"event_id": r["id"], "ts": r["ts"],
                        "action_id": r["action_id"], **d})
        return {"ok": True, "events": out, "complete": complete,
                "present": bool(out)}

    def check_delegated(self, action_id: str, expected: dict) -> dict:
        """身份核验（恢复侧）：present + complete + 逐字段一致 + fencing 不过期。

        任何不一致/查询不完整 → ok=False（保守，禁止自动重派）。
        """
        st = self.delegated_events(action_id=action_id,
                                   op_id=expected.get("op_id"))
        if not st["ok"] or not st["complete"]:
            return {"ok": False, "reason": st.get("reason") or "incomplete query",
                    "state": "UNKNOWN"}
        if not st["present"]:
            return {"ok": True, "state": "ABSENT", "event": None}
        ev = st["events"][-1]
        diff = [k for k in _DELEGATED_IDENTITY
                if k in expected and ev.get(k) != expected[k]]
        if diff:
            return {"ok": False, "state": "CONFLICT", "fields": diff,
                    "event": ev}
        try:
            if int(ev["fencing"]) < int(expected.get("fencing", ev["fencing"])):
                return {"ok": False, "state": "STALE_FENCING", "event": ev}
        except (TypeError, ValueError):
            return {"ok": False, "state": "UNKNOWN", "event": ev}
        return {"ok": True, "state": "PRESENT", "event": ev}
