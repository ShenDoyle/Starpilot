# BRM-P1-architecture.md

BRM P1 · 资源生命周期架构设计（只读设计，不实施）
状态：送审稿 v1 · 2026-11-03 · 供 Web AI 裁定
核心安全原则（不可放宽）：**宁可任务因 session 被回收而中断，也不能在无法证明旧任务已停止时，把同一浏览器资源交给新任务。**

---

## 0. 证据标记约定

| 标记 | 含义 |
|---|---|
| REAL_PASS | 已有 P0 真实实验支持 |
| CODE_CONFIRMED | 代码 / CLI 帮助明确证明 |
| INFERRED | 由现有证据推断，未实测 |
| NOT_TESTED | 未验证，禁止当已解决 |

## 1. 分层总览（已批准的 C 方案）

```
Workspace（本地工作区）
  └─ Run（唯一 run_id，任务级心跳）
       └─ Lease + Fencing（资源所有权与操作世代）
            └─ BSK Session（一个 session 只绑定一个 run）★ daemon 5m idle 回收
                 └─ Agent Window（session ↔ 窗口 1:1，REAL_PASS：stop 空闲连窗关闭）
                      └─ Tabs（归属由 SQLite 账本管理；user/borrowed 不可自动关闭）
```

权威与旁证分工（v7 裁定原文落实）：

| 层 | 权威 | 旁证 |
|---|---|---|
| 归属绑定 | SQLite 账本（事务） | bsk session list（仅显示用） |
| 授权/世代 | winlease 租约 + fencing | — |
| 会话存活 | 账本状态机 + 证据链 | session list / daemon 日志 |
| session name | 仅显示标签 | 不承担身份认证 |

P0 已证实事实（本架构的地基）：
- 空闲 `session stop` → 会话注销 + Agent Window 一并干净关闭 + `not_found`。REAL_PASS
- 忙时 stop 被拒 `session_busy`，daemon 不强杀在途操作。REAL_PASS
- `request --cancel` 可清理已启动 session；被取消令牌再启动 rc=2 `cancelled`。REAL_PASS
- daemon `--session-idle` 默认 5m、`--daemon-idle` 默认 10m（CLI 帮助）。CODE_CONFIRMED
- daemon 日志含 idle 回收记录（当日 17 条 `idle session stopped`）。REAL_PASS
- session list 无归属字段；同名 start 不互斥；动作级无 request_id。REAL_PASS
- daemon 有常驻进程（`bsk.bin.exe daemon start --foreground`）。CODE_CONFIRMED

## 2. 五分钟 idle 回收治理（首要设计问题）

先给结论：**推荐 D（允许回收 + 任务显式降级）为主，C（Supervisor 受控活动）为任务活跃期的例外通道，且 C 只允许使用 daemon 原生、无页面副作用的机制——一旦验证失败即回落 D。** A、B 均不推荐。未实测假设一律标 INFERRED/NOT_TESTED。

### 方案 A：调整 daemon `--session-idle`

| 审查项 | 分析 | 标记 |
|---|---|---|
| 能否刷新 idle 计时 | 不刷新——它只是把阈值调大，非活动 session 仍会被回收，只是更晚 | CODE_CONFIRMED |
| 影响其他 session | 是：idle 是 **daemon 级全局参数**，影响全部 session，包括探针/他人任务 | CODE_CONFIRMED（参数层级） |
| 页面副作用 | 无 | CODE_CONFIRMED |
| 风控风险 | 无 | CODE_CONFIRMED |
| daemon 重启后有效 | 仅当重启命令行再次携带；daemon 由外部方式启动（P0 未见 systemd/计划任务）时不可控 | INFERRED |
| 误判存活可能 | **高**：回收延迟 ≠ 存活证明；恰好把「停止」证据变得模糊 | INFERRED |
| 复杂度 | 低 | — |
| 验证方式 | 双 daemon（独立环境）不同 idle 值对照 | — |
| 失败恢复 | 回退默认 5m | — |

**不推荐的核心理由**：与安全原则冲突——它把「无法证明旧任务已停」的窗口拉得更长，且与「心跳不等于 session 活动」的裁定互相掩盖。Web AI 已明示不批准现在调大。REAL_PASS（裁定）

### 方案 B：任务侧定期发无副作用保活请求

| 审查项 | 分析 | 标记 |
|---|---|---|
| 能否刷新 idle 计时 | 取决于「什么算活动」：动作类请求大概率刷新；**但哪些动作算、哪些不算，daemon 未公开定义** | INFERRED |
| 影响其他 session | 否（按 session 计时） | INFERRED |
| 页面副作用 | `session list`、`cooperate sync` 等非动作命令应无页面副作用，**但未被证实它们刷新 idle 计时**；一旦需要动作类请求（navigate/evaluate）就必然碰页面 | INFERRED + NOT_TESTED |
| 风控风险 | evaluate 空 JS / 对 chrome:// 或 about:blank 的导航风险低；对业务页面的任何动作有风控风险 | INFERRED |
| daemon 重启后有效 | 定时器仍在任务侧，重启后继续有效 | CODE_CONFIRMED（任务侧定时） |
| 误判存活可能 | **高**：保活心跳本身就是「假活动」，daemon 无法区分保活与真实业务 | INFERRED |
| 复杂度 | 中（定时器 + 生命周期对齐） | — |
| 验证方式 | 独立环境：起 session → 只发非动作命令 → 观察是否被 idle 回收 | NOT_TESTED |
| 失败恢复 | 保活失败 = 视同 session 已回收，走 §3 异常降级 | — |

**不推荐的核心理由**：裁定明确「保活请求与真正的任务活动必须能够区分」——本方案在结构上做不到区分。Web AI 已明示不批准定时保活。REAL_PASS（裁定）

### 方案 C：任务活跃期由受控 Supervisor 维持 session 活动

形态：不是独立进程「替任务点页面」，而是**把「真实业务动作」本身作为唯一的活动源**——任务在活跃期内任何一次真实动作（navigate/fill/evaluate）都会刷新 idle 计时（INFERRED：daemon 对任何经 session 的请求可能都算活动，NOT_TESTED）。Supervisor 只做一件事：**监测** session 剩余 idle 时间（旁证：daemon 日志时间戳），在 session 即将到期且任务**确实还有排队动作**时，提醒任务尽快提交下一动作；不制造任何假动作。

| 审查项 | 分析 | 标记 |
|---|---|---|
| 能否刷新 idle 计时 | 真实动作能（待实测）；监测本身不能 | 部分NOT_TESTED |
| 影响其他 session | 否 | — |
| 页面副作用 | **零新增**——只依赖任务本来就要做的动作 | 设计保证 |
| 风控风险 | 无新增 | — |
| daemon 重启后有效 | Supervisor 是进程内组件，随任务重启 | — |
| 误判存活可能 | 低：不做假活动 | — |
| 复杂度 | 中 | — |
| 验证方式 | 独立环境：连续真实动作间隔 > 5m，观察是否被回收 → 得出「真实动作是否刷新 idle」 | NOT_TESTED |
| 失败恢复 | 回落 D | — |

**定位修正**：若实测「真实动作刷新 idle」成立，C 实际上**不需要独立 Supervisor 进程**——活跃任务自然满足；长思考间隔（>5m 无动作）才触发 D 降级。若实测不成立，C 退化为「每次动作前先探测 session 存活」的检查模式，仍无假动作。

### 方案 D：不保活，允许回收，任务进入异常并禁止自动续跑（推荐基线）

设计：

1. 任务在**每次触页动作前**经导航门校验 session 仍存活（账本 + session list 旁证）。
2. session 消失 → 不重开、不续跑写操作，任务状态置 `IDLE_EXPIRED`（见状态机文档），剩余动作全部挂起，输出「任务被回收中断，待人工/新任务裁决」。
3. 恢复路径只有一条：**新 run_id 的新任务**按 takeover 状态机走完整证据链（STOPPED_CONFIRMED + fencing 换代），旧任务的身份、绑定的对话上下文（bind_code、表单进度）由账本持久化，新任务可**读**旧进度但**必须**以新身份重新走窗口/标签认领。
4. 与原则对齐：中断可接受；静默续跑不可接受。

| 审查项 | 分析 | 标记 |
|---|---|---|
| 能否刷新 idle 计时 | 不需要 | — |
| 影响其他 session | 无 | — |
| 页面/风控副作用 | 无 | — |
| 误判存活可能 | 无（反方向：只判死不判活） | — |
| 复杂度 | 低（状态机 + 门已有雏形） | — |
| 失败恢复 | 即 §3 的恢复路径本身 | — |

**代价（如实申报）**：长对话任务（多轮 GPT 问答、每轮间隔 > 5m）会频繁被回收中断。缓解不靠保活，而靠「动作密度设计」：任务把待办排成批，缩短动作间隔（设计建议，NOT_TESTED）；以及 §4 的「read 旧进度」让新任务低成本接续。

### 推荐组合

- 基线 = D（任何环境、任何任务）。
- 例外 = C 的监测模式，仅在 P1-T2 实测确认「真实动作刷新 idle」后启用。
- A、B 不推荐，除非 Web AI 推翻裁定。
- **所有方案的 idle 计时假设（什么刷新计时、计时器是否持久化）都是 NOT_TESTED**，P1-T2 实验组专门验证。

## 3. 归属模型与一致性问题

完整关系链：`workspace → run → lease/fencing → session → Agent Window → tab`。逐层唯一标识、创建/释放时机、一致性约束详见 schema 文档。此处仅列架构级规则（每条对应裁定原文）：

1. `run_id` 任务级唯一、禁止空值（v6 实测空 run_id 是缺陷根源，REAL_PASS）。创建时机：run 启动时由 orchestrator 生成 `run-<时间戳>-<熵>`。
2. `owner_key = workspace + run_id`（同 workspace 不同任务不得共用）。REAL_PASS（缺陷证据）
3. `session_id ↔ run_id` 唯一绑定，SQLite 事务维护；**绑定一旦建立，本 run 生命周期内不可改绑**（改绑只能走 takeover 全证据链）。
4. session name 仅显示，不认证。
5. `borrowed` / `scope=user` 标签永不自动关闭。
6. 未知归属（unclaimed 之外的任何 `owner_unknown/must_reinspect` 态）禁止 claim/navigate/close/清理。
7. 账本写入失败 = fail-closed（拒写操作，不降级为「当作成功」）。
8. 所有状态变更走事务 + JSONL 审计。

**daemon 与账本的一致性窗口（如实申报的设计缺口）**：daemon 回收 session 是异步事件，账本只能靠「下次动作前探测」或「日志轮询」发现，存在最长 5 分钟的「账本认为 ACTIVE、daemon 已回收」窗口。该窗口内写操作会被 bsk 自身拒绝（session not_found），属安全失败方向（fail-closed），可接受。

## 4. request 生命周期治理

基于 P0 实测的真实行为设计（prepared/inspect/cancel 状态均 REAL_PASS）：

```
          prepare            start-with-rid
 (无) ──────────→ PREPARED ────────────→ STARTED(session 已建)
                     │  cancel                │ cancel
                     ▼                        ▼
                 CANCELLED(未启动，令牌作废)  CANCELLED(+session 连带清理，REAL_PASS)
                                          │
                              daemon 崩溃/超时/响应丢失
                                          ▼
                                     UNKNOWN
```

设计规则：

1. **request_id 四元绑定**：`request_id ↔ run_id ↔ session_id(启动后) ↔ lease_id+fencing`，记入账本 `requests` 表（见 schema）。daemon 不提供动作级 request_id（REAL_PASS），该绑定完全由本地账本承担。
2. **cancel 前归属核验**：cancel 是破坏性动作，前置核验 = 请求由本 run 创建 + lease 仍有效 + fencing 匹配；三者任一不满足 → 拒绝并记审计。
3. **未启动 request 的取消**：直接 cancel，令牌作废（rc=2 cancelled，REAL_PASS）。
4. **已启动 request 的取消**：cancel 会连带清理 session + Agent Window（REAL_PASS）。因此 cancel 视同「主动放弃整个 session」，必须先完成 tab 层收尾标记（closing→closed 或 unknown），再 cancel。
5. **cancel 超时 / daemon 不可用 / 结果未知** → 状态置 `STOP_UNKNOWN`，**不得**视为已停止；按状态机保留异常占用保护（不认定失败、不重试到成功为止）。
6. **动作级无 request_id**：navigate/evaluate 等记录进审计流（JSONL，含 run_id/lease/fencing/时间/目标），作为「操作曾经发生」的证据，不做实时权限判断。
7. **并发竞态防护**：cancel 与 navigate/stop 竞态时，以账本事务为串行点——先在事务内把 session 置 STOPPING（BEGIN IMMEDIATE），成功后才发出 cancel 命令；daemon 层自身忙时拒绝（session_busy，REAL_PASS）提供第二道保护。
8. **不把 cancel 返回的 closed 等同于浏览器层完全停止**：closed 只证明 daemon 层会话注销；页面级在途 JS、尚未 settle 的渲染无法证明。裁定原文落实，状态机按此设计。

## 5. stop 与 takeover 状态机

七状态机（ACTIVE / IDLE_EXPIRED / STOPPING / STOPPED_CONFIRMED / STOP_UNKNOWN / DAEMON_UNAVAILABLE / OWNER_UNKNOWN）的完整进出条件、证据要求、禁止动作见 state-machine 文档。架构级要点：

- `session 不在 list` 单独不构成 STOPPED_CONFIRMED（裁定原文）。
- idle 回收不是任务正常结束：进入 IDLE_EXPIRED 而非 STOPPED_CONFIRMED。
- stop 忙时 session_busy：不强杀、不换代，退回 ACTIVE 并告警。
- cancel/stop 超时：保留异常占用保护（STOP_UNKNOWN），禁止为「成功」而重试破坏性命令。
- 旧任务身份无法确认死亡：不 takeover（P0 已把 PID 死亡判据作废，新判据见下）。
- daemon 不可用：不得由「session list 为空」推导停止（DAEMON_UNAVAILABLE 态）。
- fencing 换代只在旧任务操作风险被排除后进行（见 state-machine 文档 STOPPED_CONFIRMED 出口门）。

**旧任务「死亡」的新判据（替代被作废的 PID 判据）**：任务级证据链，按强度排序——
a) 任务自身在账本写入 `STOPPED_CONFIRMED`（主动优雅退出，最高强度，REAL_PASS 设计）；
b) daemon 日志显示该 session 被 stop/cancel 且 cancel 请求由**持有该 run 的账本记录**发起；
c) run 心跳超时 **且** session 空闲回收记录存在 **且** 该 run 的在途 request 队列为空——三者同时满足才允许进入「候选」，仍需 fencing 换代 + 观察期。
d) 任何单一条件（如仅心跳超时、仅 list 缺失）都不足以判死。
（b/c 为 INFERRED：依赖 daemon 日志可读性与格式稳定性，未实测日志轮换与字段稳定性。）

## 6. daemon 生命周期与故障恢复（只读核查结论）

| 问题 | 结论 | 标记 |
|---|---|---|
| daemon 与 session 依赖 | session 存在于 daemon 进程内；daemon 退出 = session 全部消失（session list 为空） | INFERRED（由 stop 实测外推） |
| daemon 重启后 session 恢复 | CLI 帮助未见 attach/restore 类命令；**判定为不恢复** | INFERRED + NOT_TESTED |
| daemon 崩溃后请求继续 | 浏览器窗口残留（不随 daemon 死亡）；扩展仍连浏览器，但无 daemon 仲裁 | INFERRED |
| session 状态持久化 | 未见证据表明持久化（P0 未测） | NOT_TESTED |
| request 状态持久化 | 同上 | NOT_TESTED |
| 恢复后识别旧 session | 账本仍在（本地 SQLite/文件），daemon 侧 session 不可识别 → 账本与 daemon 必然不一致，需 reconcile | INFERRED |
| 旧请求迟到执行 | request 令牌在 daemon 内存中，daemon 重启后令牌作废（无持久化）→ 迟到执行风险低，但**未实测** | INFERRED + NOT_TESTED |

独立测试环境方案（不触碰业务 daemon）：见 test-plan 文档 P1-T4。要点：独立浏览器 profile + 独立 daemon 实例（不同 listen/数据目录，INFERRED：daemon 支持多实例，未实测）+ 全套 daemon restart/crash 实验。

## 7. SQLite 事务与并发安全（只读审查结论）

审查对象：`tabclaims.py`、`winlease.py`、`statelock.py`。

| 审查点 | 现状 | 评估 | 标记 |
|---|---|---|---|
| claim/takeover 原子性 | tabclaims 用 `BEGIN IMMEDIATE` 事务 | 设计正确；但 takeover 在 wa.py 层先读租约再写账本，两步之间存在竞态窗口 | CODE_CONFIRMED / 缺口 INFERRED |
| lease 续租与 fencing 一致 | acquire 时 fencing = 同持有者不变 / 异持有者 +1 | 正确；但续租（renew）路径是否同样处理未见测试 | INFERRED |
| close/navigate 覆盖完整临界区 | mark_closing → bsk close → mark_closed | bsk 调用失败路径已有 mark_unknown；但 bsk **成功**而 mark_closed **失败**（磁盘满）时，账本留 closing 残留，无 reconcile 兜底 | INFERRED |
| 崩溃后事务恢复 | WAL + busy_timeout 8000 | SQLite 自身可恢复；审计 JSONL 可能尾部截断（可接受，审计不参与判断） | CODE_CONFIRMED |
| 多进程竞争 | statelock 文件锁 + SQLite BEGIN IMMEDIATE | 双层互斥存在；statelock._pid_alive 仍是旧 PID 判据（与 v6 缺陷同源），需在实施轮替换为任务级判据 | CODE_CONFIRMED |
| 锁释放时持有者核验 | release 带 payload 比对 | 有；但 owner 含 pid 字段，与 §4 判据替换联动 | CODE_CONFIRMED |
| busy / 损坏 / 磁盘满 fail-closed | tabclaims.connect 抛 TABCLAIM_DB_UNAVAILABLE；写失败抛错 | 方向正确；winlease 是文件型租约，损坏时 read 返回 None = 视为无租约——**这会把「读不出」当成「无主」**，需在实施轮改为 fail-closed | CODE_CONFIRMED + 缺口 |

**统一账本设计建议**（实施轮才动，本轮只设计）：单一 SQLite 库（`brm.db`）合并 tabs / leases / requests / runs 四表，统一 `BEGIN IMMEDIATE`；winlease 保留文件租约作为进程级快速路径，但**权威判断移入库**；所有「读不出/写不进」一律 fail-closed。v5.1 的票据机制（gate_issue/redeem）保留为 shim 提示层计数用途，不再承担安全边界（v7 裁定）。

## 8. 需 Web AI 决策的岔路

1. **长思考间隔的任务体验**：D 方案下任务 >5m 无动作即中断。是否接受「中断后由人/新任务接续」为常态？或批准实测 C 的「真实动作刷新 idle」后再定？（推荐后者）
2. **统一账本迁移**：tabclaims→brm.db 是实施轮的最大改动，是否批准先出迁移子设计？
3. **statelock 的 PID 判据替换**：与 v6 裁定一致，实施轮替换，无异议即可。
4. **takeover 判据 c（心跳超时+回收记录+在途队列为空）**：是否接受为「候选起点」（仍需 fencing 换代+观察期）？
