# brm-db-migration.md

BRM P1-A · brm.db 统一资源账本迁移子设计（只读设计，不实施）
状态：送审稿 v1 · 2026-11-03 · 供 Web AI 裁定

## 1. 目标与底线

统一账本 = 资源认领、操作授权、异常隔离、故障恢复共用**同一个权威状态来源**（裁定原文）。底线：不是合并文件；不允许双权威；不允许迁移失败退回 fail-open。

## 2. 特别审查结论（逐条，裁定点名七项）

| # | 审查项 | 结论 | 标记 |
|---|---|---|---|
| 1 | wa.py 之外的资源副作用入口 | **存在两个**：① transport.py 的 IAB 通道（navigate/evaluate/fill/press 经 op 队列直驱自带浏览器，完全不过 bsk、不过账本）；② orchestrator/其他脚本如经 wa.bsk 之外的 subprocess 直调。P1 实施时 IAB 通道必须接同一账本门或显式声明为「无窗口资源语义」 | CODE_CONFIRMED |
| 2 | 绝对路径绕过账本的 BSK 调用 | wa.py/env.py 候选顺序直连 `~/.local/bin/bsk.bin.exe` 真身（v6 已实证）；bskcompat 是兼容层非独立入口。绕过 shim 属已知边界，**账本门在 wa.py 内部**，直调真身可绕 shim 但绕不开 wa.py 的账本校验；真正的绕过 = 任何脚本不经过 wa.py 直接 spawn bsk——目前无此调用点，但无法用代码禁止他人这样写 → shim 降级为提示层（v7 裁定）后此风险被接受并记录 | CODE_CONFIRMED / 边界接受 |
| 3 | 先验 fencing 后副作用的竞态 | 存在：close 顺序 = mark_closing（校验 fencing）→ bsk close → mark_closed；第一步校验与第二步副作用之间，租约可能换代（另一进程 takeover），此时旧进程仍会执行 bsk close。**缓解设计**：close_commit 事务里复读 fencing，若已换代则把 bsk 结果按「stale fencing 的副作用」记审计（属换代前授权的合法操作，因 bsk close 幂等，最终状态一致），并告警 | CODE_CONFIRMED（窗口存在）/ 设计缓解 |
| 4 | lease 换代后旧任务继续操作 | 同上窗口。navigate/fill 等读路径动作在动作前校验，动作中途换代无法撤销（浏览器动作不可回滚）——与裁定「旧任务操作风险排除后才换代」一致：换代前置条件（出口门）正是为此设计，**换代本身必须在出口门之后**，而不是靠动作中途复核 | 设计一致 |
| 5 | 账本损坏/不可用 fail-open | **发现一处**：winlease.read 损坏→None→被 is_live 当「无主」（CODE_CONFIRMED，已在 P1 设计申报）；tabclaims 全部抛错 fail-closed（CODE_CONFIRMED）；statelock 读损坏→视为无锁可抢（同源缺陷）。迁移设计统一改为 fail-closed（§5） | CODE_CONFIRMED |
| 6 | close/cancel/stop 状态不同步 | close→closing→closed 两段式已有，但 bsk 成功+账本失败无 reconcile 兜底（已申报）；cancel/stop 与 sessions.state 无联动（现无 session 级状态表）。brm.db sessions 表+reconcile 流程（见 brm-db-reconcile.md）覆盖 | 设计覆盖 |
| 7 | 旧安装位/旧 shim 继续执行 | 旧安装位 `~/.workbuddy/skills/starpilot` 存在且其 winlease 无票据函数（v6 实证）；机器上 shim 是旧 fail-open 版。迁移设计不含自动同步（禁令），列兼容策略于 brm-db-compatibility.md §3 | CODE_CONFIRMED |

## 3. 迁移顺序（实施轮执行，本轮仅设计）

```
阶段 0  冻结写入（可选窗口：无任务运行时）
阶段 1  备份：tabclaims.db / win-*.json / journal / ticket 目录 → 迁移备份目录
        校验：备份文件哈希记录；可打开性验证
阶段 2  建 brm.db（执行 brm-db-schema.sql）
阶段 3  导入（按 §4 规则，一次性脚本，幂等）
阶段 4  校验：行数/主键/约束逐表比对；抽样回放（每张 claimed tab 能否在账本回答
        「谁拥有」）；审计 JSONL 尾部连续性
阶段 5  切换：wa.py 读 brm.db；winlease 文件转只读缓存（不再授权）
阶段 6  观察期：至少一个完整任务周期，期间旧文件保留不动
阶段 7  清理：仅备份归档，不删除旧文件（删除另行授权）
```

回滚：阶段 5 前任意失败 → 停止迁移，wa.py 继续用旧实现（旧实现未动，天然可回滚）；**阶段 5 后回滚 = 代码回退（git revert），不是数据回退**——brm.db 保留为证据，不反向导入旧格式。禁止「迁移失败自动退回 fail-open」（裁定原文）：任何一步失败都停在当前阶段人工介入。

## 4. 数据导入规则（对应裁定「旧状态不能无条件导入为可信 owner」）

| 旧数据 | 导入规则 |
|---|---|
| tabclaims.tabs：state=claimed，owner 完整，lease 未过期 | 导入为 claimed，fencing_token 原值保留；**注意**：旧 fencing 无数据库层单调保证，导入时按 instance 重排基线（取旧值最大者 +1 为新基线，写迁移日志） |
| tabs：state=claimed 但租约读不出/过期 | 导入为 **owner_unknown**（不是 claimed！租约失效≠无主，裁定语义） |
| tabs：unknown / must_reinspect=1 | 原样导入，仍禁写 |
| tabs：borrowed | 原样导入，永不自动关闭 |
| tabs：closed | 导入为历史记录（closed 终态） |
| winlease 文件：健康 | 转入 leases 表（lease_id/fencing/heartbeat 原值）；此后文件为只读缓存 |
| winlease 文件：损坏/缺失 | **不导入、不视为无主**：对应 instance 的活跃资源若在 tabs/sessions 有记录 → OWNER_UNKNOWN；无记录 → 不建行 |
| statelock 锁文件 | 不导入（进程级瞬态锁，brm.db 用 BEGIN IMMEDIATE 取代）；运行中的旧锁自然过期 |
| 旧 HMAC 票据 | 全部作废（迁移即换代日）：shim 计数层从迁移完成起按新纪元计数，旧票据视为未签发 |
| run_id 为空的旧记录 | **拒绝导入**，全部转 owner_unknown + 迁移审计记录（空 run_id = 身份不成立，v6 裁定） |

## 5. 双写与权威切换

- **禁止长期双写**（双写 = 两个权威源，裁定）。切换是一次性开关：导入校验通过后，wa.py 单点改为读 brm.db；winlease.write 保留但仅作镜像（写失败只告警不阻断——因为库才是权威；库失败仍然 fail-closed）。
- statelock 在实施轮退役，由 BEGIN IMMEDIATE 取代（跨进程互斥由 SQLite 承担）。

## 6. 兼容与回滚的禁止事项

- 回滚不得恢复旧 PID 死亡判据（裁定）——代码回退必须连判据一起审，若 revert 回到含 PID 判据的提交，必须同时 revert 掉 takeover 调用点（实施轮用特性开关而非 revert 整段历史）。
- 回滚不得恢复「无主放行」：任何代码路径回到「读不出→无主」即违反裁定，测试计划含专项用例（见 brm-db-migration-test-plan.md §3）。

## 7. 资源临界区完整生命周期（P1-A 修订新增，v1.1）

裁定要求逐段定义临界区。以 tab close 为例（navigate/cancel/stop 同构）：

```
┌─ ① 获取临界区：BEGIN IMMEDIATE，事务内完成 ②③ 后立即 COMMIT（不含 ④⑤⑥）
│  ② 最后一次 fencing 校验：owner/run/lease/fencing 四项在**同一事务内**核对，
│     任一不匹配 → ABORT，无副作用发出
│  ③ 操作意图登记：tabs.state=closing + audit_refs(event='close_prepare',
│     fencing=N) 落库 —— 此刻起该 tab 对其他 run 不可见为可写
│     （事务边界：临界区获取与意图登记原子完成）
│
│  ④ BSK 副作用（无锁窗口）：bsk tab close —— 长时间网络调用**不持有任何
│     SQLite 锁**；超时/异常/结果不明均不影响 ③ 已登记的意图
│
│  ⑤ 结果确认：解析 bsk 返回（成功/失败/未知三类，未知=超时或模糊文案）
│
│  ⑥ SQLite 结果落地：BEGIN IMMEDIATE，**复读 fencing**（critical）：
│     ├─ fencing 未变：按 ⑤ 结果落 close_commit / 保持 closing+告警
│     ├─ fencing 已换代：本操作记 audit outcome='stale_fence_side_effect'
│     │   （换代前授权的合法副作用，close 幂等 → 状态一致），**不覆盖**新持有者的状态
│     └─ 账本不可用：不重试到成功为止；closing 残留留给 reconcile（见 reconcile 文档）
│
│  ⑦ 释放或异常隔离：
│     ├─ 正常：closed 落库即释放（tab 维度）；session 维度由 sessions.state 说话
│     ├─ 异常：closing/unknown/STOP_UNKNOWN 保留占用保护
│     └─ 禁止：TTL 单独解除保护（裁定）；保护解除只走 reconcile 决策表或人工
└─
```

设计不变量：
- INV1：②③ 原子（一个事务），④ 无锁，⑥ 独立事务——锁持有时间与 BSK 网络解耦。
- INV2：任何时点账本都能回答「该 tab 是否处于临界区」——closing 即在途标志。
- INV3：fencing 校验永远发生在临界区内（②③ 与 ⑥），④ 期间的换代由 ⑥ 检出。

## 8. takeover 与临界区互斥证明（P1-A 修订新增，v1.1）

裁定要求：旧任务操作进行中禁止 takeover；BSK 超时/崩溃/结果未知时不释放；TTL 不单独解除；多进程竞争下旧任务不迟到执行。

证明结构（基于上述生命周期）：

| 主张 | 论证 |
|---|---|
| P1 旧任务操作进行中 ⇒ takeover 被阻 | ③ 意图登记后 tabs.state∈{closing,...} 且 fencing_token=旧值；takeover_candidate 事务（BEGIN IMMEDIATE）内检查目标资源所有 tabs 无「临界区内」状态，任一在途 → ABORT。数据库层保证（部分唯一索引+状态触发器+应用级检查在同一事务）。**残余窗口**：④ 无锁期间 takeover 事务可能通过检查（此刻 tabs.state 已是 closing，检查可见）——closing 正是临界区标志，takeover 检查包含 closing ⇒ 无窗口。REAL：设计级证明，实施后 BT/CT 用例回归 |
| P2 BSK 超时/崩溃/未知 ⇒ 不释放 | ⑤ 未知 → ⑥ 不落终态，保持 closing；进程死亡 → closing 残留被 reconcile 检出，决策表 2.1「结果未知+tab 仍在 → 保持 closing+告警」。无任何路径从「未知」直接到「closed 或可认领」。TTL 在任何表中都不参与保护解除（leases.lease_until 只影响租约活性判定，不触碰 tabs/sessions 保护态） |
| P3 TTL 不单独解除保护 | 同上；takeover 出口门 5 条件均非 TTL 单项（观察期只是门之一且 INFERRED） |
| P4 多进程竞争旧任务不迟到执行 | 场景：旧任务 A 在 ④ 中，新任务 B 完成 takeover（fencing 换代）→ A 的 ⑥ 复读 fencing 检出换代 → A 不得写任何新状态（只记 stale 审计）；A 若继续发起新动作 → ② 校验 fencing 失败 ABORT。迟到执行只可能发生在**换代前已授权的那一次副作用**（④ 已发出），且该副作用经 fencing 检查在 ②③ 时合法——这正是裁定「先检查 fencing 后执行副作用之间的竞态」的闭环：**换代发生在 ②③ 之后 ⇒ 换代者看见 closing ⇒ takeover 被阻**。因此「④ 进行中完成换代」要求 takeover 绕过 P1，被数据库层拒绝 ⇒ 时序安全 |
| 推论 | 旧任务迟到执行的唯一窗口 = 换代前已发出且不可撤销的单次 BSK 副作用；其结果由 stale 审计 + close 幂等性吸收；navigate/evaluate 等不可回滚动作因此要求**换代前置的出口门**（观察期）兜底——与裁定「fencing 换代必须在旧任务操作风险被排除后进行」一致 |

## 9. 假设清单

| # | 假设 | 标记 |
|---|---|---|
| M1 | 旧 tabclaims.db 可在迁移窗口以只读方式打开 | INFERRED（WAL 模式正常） |
| M2 | 迁移窗口可找到「无任务运行」时机（人工协调） | 设计假设，需用户配合 |
| M3 | fencing 基线重排不破坏进行中任务的授权 | INFERRED（重排发生在无任务窗口） |
| M4 | IAB 通道在本项目当前使用量近似为零，可最后迁移 | INFERRED，需用户确认 |

## 10. `session start --request-id` 幂等开窗设计（P1-A 修订 v1.2，2026-11-03）

> 依据：GPT 裁定（2026-11-03）「批准纳入 BRM v7 C 方案，作为 Run→BSK Session 创建环节的可恢复幂等机制」。
> 源码依据：Tencent/BrowserSkill main（0.4.1）session_requests.rs——10 分钟有效期 token、同 token 异参数拒绝、过期未认领回收墓碑（verify-1103-bsk-semantics.md）。

### 10.1 职责边界（裁定原文表格的落地）

| 能力 | request-id 承担？ | 承担者 |
|---|---|---|
| 同一开窗请求重试避免重复创建 | ✅ 是 | request-id + daemon 幂等层 |
| 识别同一请求的恢复结果 | ✅ 是 | request-id 查询/恢复 |
| 证明 session 属于哪个 run | ❌ 否 | brm.db sessions 表 run_id 绑定 |
| 证明调用者持有效 lease | ❌ 否 | leases 表 + fencing_token |
| 允许 stop/close/navigate 等副作用 | ❌ 否 | 恢复流程只读认领，不发任何副作用动作 |

### 10.2 run_id 与 request_id 的映射约束

1. `run_id`：任务级唯一、非空（表约束 `runs.run_id TEXT PRIMARY KEY CHECK(length(run_id)>=8)`）。
2. `request_id` = `req-<run_id>`（确定性派生）：同一 run 重试开窗必须复用同一 request_id——这是「重试≠新建」的前提；不同 run 的 request_id 必然不同。
3. 异参数拒绝映射：账本登记开窗参数指纹（browser_id + 窗口参数哈希）。若同 request_id 重试时参数指纹不一致 → **拒绝发送**（对应 BSK 侧「同 token 异参数硬拒」，在账本层提前拦截并告警）。

### 10.3 开窗请求事务边界（SQLite）

```
① BEGIN IMMEDIATE
   INSERT INTO runs(run_id, request_id, open_params_fp, state='opening', created_at)
   -- 唯一约束冲突 ⇒ run 已存在：读取现有行，转 ④ 恢复分支（不新建）
   INSERT INTO open_requests(request_id, run_id, state='sent_pending', sent_at)
   COMMIT
② 调用 bsk session start --request-id <rid> --name <task_name>
③ 返回处理：
   成功 → ④ BEGIN IMMEDIATE
          UPDATE open_requests SET state='claimed', session_id=<复核后的 sid>
          INSERT INTO sessions(session_id, run_id, ...)   -- 账本认领
          UPDATE runs SET state='opened'
          COMMIT
   失败/超时/进程崩溃 → ⑤ 不换新 token：按 10.4 恢复矩阵处理
```

### 10.4 异常处理矩阵（裁定第 5/6/7 条）

| 场景 | 处理 |
|---|---|
| 重试且参数一致 | 复用原 request_id 重发；daemon 幂等层保证不重复建窗 |
| 重试但参数不一致 | 本地拒绝（10.2-3），人工核验 |
| 调用超时/进程崩溃 | 先以原 request_id 查询/恢复结果（bsk 官方恢复通道），**不得直接换新 token 重开** |
| token 过期（10 分钟） | 过期未认领 → BSK 侧回收；BRM 账本把 open_requests 置 `expired`，**进入隔离 + 人工核验**，不允许自动重开 |
| session 创建成功但 SQLite 写失败（③后半崩溃） | 重启后 reconcile 发现：daemon 有 session（按 --name starpilot-* 可查）但账本无映射 → 按 brm-db-reconcile.md「session 存在账本无行」决策：隔离观察，不自动认领、不自动 stop |
| session 存在但账本无映射（任何来源） | 同上：隔离 + 人工核验；**禁止**仅凭 request-id 自动 takeover/stop |

### 10.5 与 lease/fencing 的区别（裁定第 8/9/10 条）

- request-id 有效期（10 分钟）**不是** BRM lease 有效期；lease_until 只由 leases 表管理。
- request-id 是**创建期幂等凭证**，不进入 ownership/fencing 证明链；session 所有权唯一权威 = brm.db sessions 表行。
- 恢复流程（查询/认领）**只读**：不得在恢复分支执行 stop、close、navigate、takeover；一切副作用动作仍走 ②③ 校验（fencing + 临界区）原链路。

## 11. 风险文档更新项（同步至 BRM-P1-risks.md）

- R-AU1：BSK_AUTO_UPDATE daemon 自动升级默认开启（源码确认）；业务 daemon 实测 foreground + auto_update=false，后台自换行为对其他部署形态仍 UNKNOWN
- R-AU2：daemon pair 仍未启用（裁定维持）；授权层双 TTL + 一次性凭证 + revoke_all 已源码确认，在途请求 revoke 后行为 UNKNOWN
- R-ST1：step 原生原子性需 T3 对照实测（CLI 层源码确认，执行端 NOT_TESTED）
- R-UP1：bsk update 运维红线固化（活跃 session 禁默认 update；--no-restart-daemon 存在性已源码确认）
- R-ID1：request-id 10 分钟有效期 ≠ lease 有效期（设计红线，防混用）
