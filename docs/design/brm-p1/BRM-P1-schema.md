# BRM-P1-schema.md

BRM P1 · SQLite 数据模型与约束（只读设计，不实施）
状态：送审稿 v1 · 2026-11-03 · 供 Web AI 裁定

目标：把 tabclaims（tabs 单表）扩展为统一资源账本 `brm.db`（runs / leases / sessions / requests / tabs 五表 + 审计流）。实施轮才迁移，本文只定义模型。

## 1. 表关系

```
runs (run_id PK)
  │ 1:N
leases (lease_id PK, run_id FK, instance, fencing)     ← 现 winlease 文件租约的权威镜像
  │ 1:1(活跃)
sessions (session_id PK, run_id FK UNIQUE, window_id)
  │ 1:N                                    │ 1:N
tabs (session_id,tab_id PK)              requests (request_id PK, run_id FK)
```

## 2. 表定义

```sql
-- 任务身份：一切归属的根（v6 缺陷修复：run_id 非空唯一）
CREATE TABLE runs (
  run_id      TEXT PRIMARY KEY,             -- run-<yyyymmddHHMMSS>-<8hex>，禁止空
  workspace   TEXT NOT NULL,                -- 绝对路径
  owner_key   TEXT NOT NULL,                -- workspace + '\n' + run_id（同工作区不同任务必不同）
  platform    TEXT NOT NULL DEFAULT '',
  created_at  REAL NOT NULL,
  graceful_exit INTEGER NOT NULL DEFAULT 0, -- 优雅退出标志（takeover 判据 a）
  UNIQUE(owner_key)
);
-- 约束：owner_key 由触发器/应用层强制 = workspace+run_id；run_id 空值插入必须被拒（CHECK run_id <> ''）

-- 租约与世代：文件租约（winlease）的权威副本；文件损坏时以此为准
CREATE TABLE leases (
  lease_id    TEXT PRIMARY KEY,
  run_id      TEXT NOT NULL REFERENCES runs(run_id),
  instance    TEXT NOT NULL,                -- 浏览器实例（窗口组标识）
  fencing     INTEGER NOT NULL,             -- 单调递增，换代 +1，续租不变
  mode        TEXT NOT NULL CHECK(mode IN ('serial','exclusive')),
  lease_until REAL NOT NULL,
  heartbeat_at REAL NOT NULL,
  created_at  REAL NOT NULL
);
CREATE INDEX idx_leases_run ON leases(run_id);
CREATE INDEX idx_leases_inst ON leases(instance, lease_until);

-- session 绑定：一 session 只绑一 run（v7 裁定），绑定生命周期内不可改绑
CREATE TABLE sessions (
  session_id  TEXT PRIMARY KEY,
  run_id      TEXT NOT NULL REFERENCES runs(run_id),
  instance    TEXT NOT NULL,
  name        TEXT NOT NULL DEFAULT '',     -- 仅显示标签，不承担认证（v7 裁定）
  window_id   INTEGER,                      -- Agent Window（stop 空闲实证连窗关闭 REAL_PASS）
  state       TEXT NOT NULL CHECK(state IN
              ('ACTIVE','IDLE_EXPIRED','STOPPING','STOPPED_CONFIRMED',
               'STOP_UNKNOWN','DAEMON_UNAVAILABLE','OWNER_UNKNOWN')),
  state_evidence TEXT NOT NULL DEFAULT '',  -- 进入当前状态的证据摘要
  bound_at    REAL NOT NULL,
  updated_at  REAL NOT NULL
);
CREATE INDEX idx_sessions_run ON sessions(run_id);

-- request 生命周期（P0 实测行为建模 REAL_PASS）
CREATE TABLE requests (
  request_id  TEXT PRIMARY KEY,
  run_id      TEXT NOT NULL REFERENCES runs(run_id),
  session_id  TEXT,                          -- 启动后回填（未启动为 NULL）
  lease_id    TEXT NOT NULL,
  fencing     INTEGER NOT NULL,
  state       TEXT NOT NULL CHECK(state IN
              ('PREPARED','STARTED','COMPLETED','CANCELLED','UNKNOWN')),
  cancel_requested_at REAL,                  -- 置位先行（事务），命令后发
  settled_at  REAL,
  created_at  REAL NOT NULL
);

-- 标签归属：现有 tabs 表并入，键不变（session_id,tab_id）
CREATE TABLE tabs (
  session_id  TEXT NOT NULL REFERENCES sessions(session_id),
  tab_id      TEXT NOT NULL,
  run_id      TEXT NOT NULL,
  owner_key   TEXT NOT NULL,
  lease_id    TEXT NOT NULL,
  fencing_token INTEGER NOT NULL,
  instance_id TEXT,
  window_id   INTEGER,
  intent      TEXT NOT NULL CHECK(intent IN ('opened','reused','borrowed')),
  origin_scope TEXT,
  first_url   TEXT, last_url TEXT,
  state       TEXT NOT NULL CHECK(state IN
              ('claimed','closing','closed','unknown','orphan','owner_unknown')),
  must_reinspect INTEGER NOT NULL DEFAULT 0,
  updated_at  REAL NOT NULL,
  PRIMARY KEY(session_id, tab_id)
);
```

审计流沿用现有 `tab-claims.jsonl` 机制（追加 JSONL，只增不改，损坏不影响判断）。

## 3. 一致性约束（每条对应裁定/实测）

| # | 约束 | 依据 | 标记 |
|---|---|---|---|
| C1 | runs.run_id 非空、全局唯一；owner_key 唯一 | v6 实测空 run_id 缺陷 | REAL_PASS |
| C2 | sessions.run_id UNIQUE —— 一个 run 也可有多 session，但一 session 只属一 run | v7 裁定「一个 session 只绑定一个 run」 | 裁定落实 |
| C3 | sessions.name 不参与任何权限判断 | v7 裁定 | 裁定落实 |
| C4 | tabs.intent='borrowed' 与 scope='user' 永不自动关闭 | 用户指令 + 裁定 | 已实现 CODE_CONFIRMED |
| C5 | 所有写路径 fail-closed：库打不开/写失败 → 拒绝对应操作（含 winlease 读损坏不再当「无主」） | v6 裁定 fail-closed | 设计（现 winlease 有缺口，见架构文档 §7） |
| C6 | 状态迁移必须带 state_evidence 摘要 | v6「证据要求」 | 设计 |
| C7 | fencing 全局对每个 instance 单调递增，换代必须旧值 +1 | winlease 已实现 | CODE_CONFIRMED |
| C8 | requests.cancel_requested_at 先置位后发命令（STOPPING 串行点） | 竞态防护设计 | 设计 |
| C9 | 跨主机 run 一律 OWNER_UNKNOWN | v7 裁定 | 裁定落实 |

## 4. 事务边界

| 操作 | 事务 |
|---|---|
| claim tab | BEGIN IMMEDIATE：读冲突→无冲突写→提交（现状已如此） |
| takeover | **单事务内**完成：验出口门→改 owner/run/lease→fencing 换代→写审计（v6 要求） |
| session 状态迁移 | BEGIN IMMEDIATE：置 state+evidence→提交后才发外部命令（stop/cancel）——命令结果回来后第二个事务落终态 |
| cancel 请求 | 事务置 cancel_requested_at（STOPPING 串行点）→ 发命令 → 事务落 CANCELLED |
| 续租 | 更新 lease_until/heartbeat_at，fencing 不变（CODE_CONFIRMED 语义） |

## 5. 崩溃恢复与并发

- WAL + busy_timeout（现状 8000ms，CODE_CONFIRMED）：多进程写竞争时后者重试，超时则 fail-closed。
- 进程崩溃在「事务已提交、外部命令未发」→ 状态与命令不一致：以账本为准（state=STOPPING 但命令未发），恢复逻辑=对 STOPPING 超时项重查 daemon 侧证据（list/日志），转入 STOPPED_CONFIRMED 或 STOP_UNKNOWN，不盲目重发。
- 磁盘满/库损坏 → 所有写路径 fail-closed；读路径中「账本读不出」→ OWNER_UNKNOWN（不是无主）。
- 审计 JSONL 损坏 → 不影响任何判断（设计原则已实现）。

## 6. 与现有实现的迁移映射（实施轮用）

| 现有 | 迁移到 | 说明 |
|---|---|---|
| tabclaims.tabs | brm.db.tabs | 结构基本不变，补 run 非空校验 |
| tabclaims.jsonl 审计 | brm-audit.jsonl | 沿用 |
| winlease 文件租约 | brm.db.leases（权威）+ 文件（快速路径缓存） | 读损坏时以库为准 |
| wa.py _LAST_CTX/owner 组装 | runs 表查询 | 消灭空 run_id |
| statelock PID 判据 | run 级判据（graceful_exit + 心跳） | v6 裁定 |
| gate 票据（v5.1） | 保留为 shim 计数层 | v7 裁定 shim=提示+计数 |

## 7. 假设清单

| # | 假设 | 标记 |
|---|---|---|
| S1 | window_id 可由 bsk tab list 稳定获得 | REAL_PASS（P0 探测含 window_id） |
| S2 | leases 表作为权威后，文件租约损坏不产生双权威 | 设计保证，实施后需专项测试 |
| S3 | SQLite 在本机并发度（≤5 进程）下无性能问题 | INFERRED |
