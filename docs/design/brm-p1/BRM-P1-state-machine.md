# BRM-P1-state-machine.md

BRM P1 · 资源状态机与状态转换表（只读设计，不实施）
状态：送审稿 v1 · 2026-11-03 · 供 Web AI 裁定

本状态机覆盖「session/run 资源」的生命周期判定。tab 级状态（claimed/closing/closed/unknown/orphan/owner_unknown）已在 tabclaims 实现并测试（CODE_CONFIRMED），本文不重复，只定义 session/run 级状态与 tab 状态的联动。

## 1. 状态定义

| 状态 | 含义 | 允许 takeover？ |
|---|---|---|
| ACTIVE | 旧任务和 session 正常活动 | 否 |
| IDLE_EXPIRED | session 疑似被 idle 回收 | 否 |
| STOPPING | 已发起 cancel/stop | 否 |
| STOPPED_CONFIRMED | 有可靠证据确认旧 session 已停止 | 仍需检查其他条件 |
| STOP_UNKNOWN | 无法确认是否真正停止 | 否 |
| DAEMON_UNAVAILABLE | daemon 不可用，无法确认状态 | 否 |
| OWNER_UNKNOWN | 归属无法证明 | 否 |

裁定原文落表：`session 消失不是 STOPPED_CONFIRMED`。

## 2. 状态转换表

```
ACTIVE ──写操作成功/心跳正常──→ ACTIVE（自环）
ACTIVE ──动作前探测 session not_found──→ IDLE_EXPIRED
ACTIVE ──daemon 调用 connection refused / 超时──→ DAEMON_UNAVAILABLE
ACTIVE ──归属证据缺失(run空/租约读不出/账本不可用)──→ OWNER_UNKNOWN
ACTIVE ──本 run 主动发起 cancel/stop──→ STOPPING
IDLE_EXPIRED ──session list 恢复存在──→ ACTIVE（仅限探测误报，需审计）
IDLE_EXPIRED ──确认不恢复──→ 终态：任务中断，挂起写操作（不转 STOPPED_CONFIRMED）
STOPPING ──stop/cancel 返回成功──→ STOPPED_CONFIRMED（候选，过 §4 出口门）
STOPPING ──返回 session_busy──→ ACTIVE（退回并告警，REAL_PASS：daemon 不强杀）
STOPPING ──超时/响应丢失/结果不明──→ STOP_UNKNOWN
STOP_UNKNOWN ──事后获得确证（日志/下次 list）──→ STOPPED_CONFIRMED 或 IDLE_EXPIRED
STOP_UNKNOWN ──持续无法确证──→ 保持（异常占用保护，不重试破坏性命令）
DAEMON_UNAVAILABLE ──daemon 恢复──→ 按恢复后 list 结果：session 在→ACTIVE；不在→IDLE_EXPIRED
DAEMON_UNAVAILABLE 期间──禁止一切由「list 为空」推导的停止判定
OWNER_UNKNOWN ──归属证据恢复──→ 按证据转入对应状态；否则保持，禁写
```

禁止动作（各态）：

| 状态 | 禁止 |
|---|---|
| IDLE_EXPIRED | 自动重建 session 后继续旧任务写操作（裁定原文）；claim/navigate/close 他人标签 |
| STOP_UNKNOWN | 重试 stop/cancel 至「成功」；fencing 换代；判定任务已死 |
| DAEMON_UNAVAILABLE | 由空 list 推导停止；任何破坏性命令 |
| OWNER_UNKNOWN | claim/navigate/close/清理；fencing 换代 |
| STOPPED_CONFIRMED | 在 §4 出口门未全过前执行 takeover |

## 3. 每个状态的证据要求

| 迁移 | 所需证据（按强度） |
|---|---|
| →IDLE_EXPIRED | session not_found（本 session 动作返回）+ 账本该 session 记录存在 + daemon 日志含 idle 回收记录（第三条为增强项，缺失时仍可判 INFERRED 级） |
| →STOPPING | 账本事务内置位（BEGIN IMMEDIATE）成功后，才允许发出命令 |
| →STOPPED_CONFIRMED | ①stop/cancel 命令成功返回（REAL_PASS 已知返回形态）或 ②daemon 日志确认 stopped 记录。**两者之外（含 list 缺失）都不够** |
| →DAEMON_UNAVAILABLE | 连接层失败（refused/timeout），区别于应用层 not_found |
| →OWNER_UNKNOWN | run_id 空 / 租约损坏或读不出 / 账本不可用 / 跨主机 |
| →STOP_UNKNOWN | 命令超时或 rc 未定义；daemon 崩溃窗口内的 cancel |

## 4. STOPPED_CONFIRMED → takeover 的出口门（全部满足才可换代）

1. **旧任务身份确认**：旧 run 已优雅退出（账本记录）或满足架构文档 §5 判据 b/c（单条件不足）。
2. **资源归属确认**：账本该 session 绑定的 run 与待接管对象一致，无第三方并发持有。
3. **在途风险排除**：旧 run 的 request 队列为空、无未 settle 动作记录（审计流尾部无近期待完成项）。
4. **fencing 换代安全**：新 lease 的 fencing = 旧 +1；旧 fencing 一律拒绝（stale_fencing 已实现，CODE_CONFIRMED）。
5. **观察期**：换代后 T_obs（建议 ≥ idle 周期 5m，INFERRED）内旧 session 无任何活动迹象，才允许写操作。
6. 全程审计：门 1-5 每条结果入 JSONL。

任一门不过 → 退回 OWNER_UNKNOWN 或 STOP_UNKNOWN，不 takeover。

## 5. 与 tab 状态的联动

| session 级事件 | tab 账本联动 |
|---|---|
| →IDLE_EXPIRED | 该 session 名下 owned 标签 → state=unknown + must_reinspect=1 |
| →STOPPING | owned 标签 → closing（不允许新写） |
| →STOPPED_CONFIRMED | owned 标签 → closed（窗口已实证随 stop 关闭，REAL_PASS）；borrowed 标签单独标 owner_unknown |
| →DAEMON_UNAVAILABLE | tab 不动（无法判定），仅在审计标注 |
| 事后 reconcile（session 恢复） | bsk tab list 与账本对账：账本有 BSK 无→closed；BSK 有账本无→只报告不认领（已实现，CODE_CONFIRMED） |

## 6. 双进程/双任务并发场景推演（设计核查）

| 场景 | 行为 |
|---|---|
| 新任务在旧任务 ACTIVE 时启动 | 新任务 ensure_session 发现组窗口有活会话→只读复用或另开 session；账本先到先得，不抢 |
| 新任务在 IDLE_EXPIRED 后启动 | 旧任务标记中断；新 run 走完整出口门；观察期内只读 |
| 两任务同时 cancel 同一 session | 账本事务串行化 STOPPING 置位，后者发现已 STOPPING 则不再发命令；daemon 层 session_busy/not_found 兜底 |
| cancel 与新任务 navigate 竞态 | navigate 先过账本门：STOPPING 态拒写（fail-closed） |
| daemon 崩溃期间旧任务写操作 | bsk 调用连接失败→DAEMON_UNAVAILABLE，账本拒写，无静默成功路径 |

所有失败方向均 fail-closed（拒绝写、不降级为成功），与 v6 裁定一致。

## 7. 本文档假设清单（禁止当已解决）

| # | 假设 | 标记 |
|---|---|---|
| A1 | daemon 日志 idle/stop 记录格式稳定可解析 | INFERRED（格式未审计轮换行为） |
| A2 | 观察期 5m 足够覆盖迟到操作 | INFERRED |
| A3 | session not_found 只出现于「已回收/已停止」 | INFERRED（未排除其他错误映射） |
| A4 | request 队列「为空」可由账本可靠判断 | INFERRED（daemon 无动作级 request_id，REAL_PASS，队列纯本地） |
| A5 | DAEMON_UNAVAILABLE 与 not_found 可稳定区分 | 部分CODE_CONFIRMED（rc/错误文案），NOT_TESTED 全集 |
