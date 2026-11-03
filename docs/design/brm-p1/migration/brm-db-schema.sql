# brm-db-schema.sql

BRM P1-A · 统一资源账本 brm.db 设计 SQL（**仅设计，不执行**）
状态：送审稿 v1 · 2026-11-03 · 供 Web AI 裁定

原则：唯一性由数据库层约束保证（Python 判断只是防呆，不是防线）；所有状态迁移受 CHECK 限制；fencing 世代由数据库层单调性保护。

```sql
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 8000;
PRAGMA foreign_keys = ON;

-- ============================================================ runs：任务身份
CREATE TABLE runs (
  run_id        TEXT PRIMARY KEY,
  workspace     TEXT NOT NULL,
  owner_key     TEXT NOT NULL,
  platform      TEXT NOT NULL DEFAULT '',
  host          TEXT NOT NULL,
  created_at    REAL NOT NULL,
  finished_at   REAL,                          -- 优雅退出时间（判据 a）
  graceful_exit INTEGER NOT NULL DEFAULT 0 CHECK(graceful_exit IN (0,1))
);
-- run_id 非空由主键非空保证；owner_key 唯一 = 同工作区任务不共用身份
CREATE UNIQUE INDEX ux_runs_owner  ON runs(owner_key);
CREATE INDEX        ix_runs_ws     ON runs(workspace, created_at);
-- 触发器：owner_key 必须由 workspace + run_id 派生（数据库层，不靠 Python）
CREATE TRIGGER trg_runs_ownerkey_ins BEFORE INSERT ON runs
WHEN NEW.owner_key <> (NEW.workspace || '|' || NEW.run_id)
BEGIN SELECT RAISE(ABORT, 'owner_key must equal workspace|run_id'); END;
CREATE TRIGGER trg_runs_ownerkey_upd BEFORE UPDATE OF owner_key ON runs
WHEN NEW.owner_key <> (NEW.workspace || '|' || NEW.run_id)
BEGIN SELECT RAISE(ABORT, 'owner_key must equal workspace|run_id'); END;

-- ============================================================ leases：授权与世代
CREATE TABLE leases (
  lease_id      TEXT PRIMARY KEY,
  run_id        TEXT NOT NULL REFERENCES runs(run_id),
  instance      TEXT NOT NULL,
  fencing       INTEGER NOT NULL CHECK(fencing >= 0),
  mode          TEXT NOT NULL CHECK(mode IN ('serial','exclusive')),
  lease_until   REAL NOT NULL,
  heartbeat_at  REAL NOT NULL,
  created_at    REAL NOT NULL,
  expired_at    REAL                           -- 置后不再续
);
-- fencing 单调：同 instance 只允许比现值大 1 的换代；续租（同 lease_id）不变
CREATE INDEX ix_leases_inst ON leases(instance, lease_until);
CREATE TRIGGER trg_fence_mono_ins BEFORE INSERT ON leases
WHEN NEW.fencing <> (SELECT COALESCE(MAX(fencing), -1) + 1 FROM leases
                     WHERE instance = NEW.instance)
BEGIN SELECT RAISE(ABORT, 'fencing must be prev+1 for instance'); END;
CREATE TRIGGER trg_fence_no_reuse BEFORE UPDATE OF fencing ON leases
WHEN NEW.fencing <= OLD.fencing
BEGIN SELECT RAISE(ABORT, 'fencing is monotonic per instance'); END;
-- 同 instance 只允许一个未过期租约（活动资源唯一性）
CREATE UNIQUE INDEX ux_leases_live ON leases(instance)
  WHERE expired_at IS NULL;

-- ============================================================ sessions：会话绑定
CREATE TABLE sessions (
  session_id   TEXT PRIMARY KEY,
  run_id       TEXT NOT NULL REFERENCES runs(run_id),
  instance     TEXT NOT NULL,
  name         TEXT NOT NULL DEFAULT '',       -- 仅显示标签，不承担认证
  window_id    INTEGER,
  state        TEXT NOT NULL CHECK(state IN
               ('ACTIVE','IDLE_EXPIRED','STOPPING','STOPPED_CONFIRMED',
                'STOP_UNKNOWN','DAEMON_UNAVAILABLE','OWNER_UNKNOWN')),
  evidence     TEXT NOT NULL DEFAULT '',       -- 进入当前状态的证据摘要
  bound_at     REAL NOT NULL,
  state_at     REAL NOT NULL,
  released_at  REAL
);
-- run ↔ 活跃 session 一对一：一个 run 同时只能有一个未释放 session
CREATE UNIQUE INDEX ux_sessions_live_run ON sessions(run_id)
  WHERE released_at IS NULL;
CREATE INDEX ix_sessions_inst ON sessions(instance, state);

-- ============================================================ requests：请求生命周期
CREATE TABLE requests (
  request_id   TEXT PRIMARY KEY,
  run_id       TEXT NOT NULL REFERENCES runs(run_id),
  session_id   TEXT REFERENCES sessions(session_id),
  lease_id     TEXT NOT NULL,
  fencing      INTEGER NOT NULL,
  state        TEXT NOT NULL CHECK(state IN
               ('PREPARED','STARTED','COMPLETED','CANCELLED','UNKNOWN')),
  cancel_requested_at REAL,
  settled_at   REAL,
  created_at   REAL NOT NULL
);
CREATE INDEX ix_requests_run ON requests(run_id, state);

-- ============================================================ tabs：标签归属
CREATE TABLE tabs (
  session_id    TEXT NOT NULL REFERENCES sessions(session_id),
  tab_id        TEXT NOT NULL,
  run_id        TEXT NOT NULL REFERENCES runs(run_id),
  owner_key     TEXT NOT NULL,
  lease_id      TEXT NOT NULL,
  fencing_token INTEGER NOT NULL,
  instance_id   TEXT,
  window_id     INTEGER,
  intent        TEXT NOT NULL CHECK(intent IN ('opened','reused','borrowed')),
  origin_scope  TEXT NOT NULL DEFAULT 'agent'
                CHECK(origin_scope IN ('agent','user')),
  first_url     TEXT, last_url TEXT,
  state         TEXT NOT NULL CHECK(state IN
                ('claimed','closing','closed','unknown','orphan','owner_unknown')),
  must_reinspect INTEGER NOT NULL DEFAULT 0 CHECK(must_reinspect IN (0,1)),
  created_at    REAL NOT NULL,
  updated_at    REAL NOT NULL,
  PRIMARY KEY (session_id, tab_id)
);
CREATE INDEX ix_tabs_owner ON tabs(owner_key, state);
CREATE INDEX ix_tabs_state ON tabs(state);

-- ============================================================ 状态转换合法性（触发器层）
-- tabs 状态机：claimed→closing→closed；任何态→unknown/owner_unknown；closed 为终态
CREATE TRIGGER trg_tabs_trans BEFORE UPDATE OF state ON tabs
WHEN NEW.state <> OLD.state AND NOT (
       (OLD.state='claimed' AND NEW.state IN ('closing','unknown','owner_unknown'))
    OR (OLD.state='closing' AND NEW.state IN ('closed','unknown'))
    OR (OLD.state='unknown' AND NEW.state IN ('claimed','closed','owner_unknown'))
    OR (OLD.state IN ('owner_unknown','orphan')
        AND NEW.state IN ('claimed','closed'))
    OR (OLD.state='closed' AND 0))          -- closed 不可回退
BEGIN SELECT RAISE(ABORT, 'illegal tabs state transition'); END;

-- sessions 状态机（P1-state-machine 转换表落库）
CREATE TRIGGER trg_sess_trans BEFORE UPDATE OF state ON sessions
WHEN NEW.state <> OLD.state AND NOT (
       (OLD.state='ACTIVE'            AND NEW.state IN ('IDLE_EXPIRED','STOPPING','DAEMON_UNAVAILABLE','OWNER_UNKNOWN'))
    OR (OLD.state='IDLE_EXPIRED'      AND NEW.state IN ('ACTIVE'))   -- 误报回转需 evidence
    OR (OLD.state='STOPPING'          AND NEW.state IN ('STOPPED_CONFIRMED','STOP_UNKNOWN','ACTIVE'))
    OR (OLD.state='STOP_UNKNOWN'      AND NEW.state IN ('STOPPED_CONFIRMED','IDLE_EXPIRED'))
    OR (OLD.state='DAEMON_UNAVAILABLE'&& NEW.state IN ('ACTIVE','IDLE_EXPIRED'))
    OR (OLD.state='OWNER_UNKNOWN'     AND NEW.state IN ('ACTIVE','IDLE_EXPIRED','STOP_UNKNOWN')))
BEGIN SELECT RAISE(ABORT, 'illegal sessions state transition'); END;

-- ============================================================ 审计关联
-- 审计主体仍在 JSONL（brm-audit.jsonl），此处只留可检索的索引行
CREATE TABLE audit_refs (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  at           REAL NOT NULL,
  run_id       TEXT,
  session_id   TEXT,
  tab_id       TEXT,
  request_id   TEXT,
  lease_id     TEXT,
  fencing      INTEGER,
  event        TEXT NOT NULL,      -- claim/navigate/close/cancel/takeover/reconcile/...
  outcome      TEXT NOT NULL,      -- allowed/denied/error
  detail       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX ix_audit_run ON audit_refs(run_id, at);
CREATE INDEX ix_audit_evt ON audit_refs(event, at);
```

数据库层约束对裁定条款的覆盖：

| 裁定要求 | 落库方式 |
|---|---|
| run_id 禁空、不共用 | PK + ux_runs_owner + owner_key 触发器 |
| session↔run 唯一绑定 | ux_sessions_live_run（活动一对一） |
| fencing 单调换代 | trg_fence_mono_ins / trg_fence_no_reuse |
| 状态转换合法性 | trg_tabs_trans / trg_sess_trans |
| borrowed/user 不自动关闭 | 应用层纪律 + tabs.intent/scope 可查证（约束层无法表达「永不」，以审计+回收 SQL 白名单保证） |
| 时间与主机身份 | runs.host / 各表 created_at、updated_at、finished_at |
| 审计关联 | audit_refs 外键式关联列（弱关联，审计不阻断主流程） |
