# brm-db-migration-test-plan.md

BRM P1-A · 迁移测试计划（只读设计，不实施）
状态：送审稿 v1 · 2026-11-03 · 供 Web AI 裁定

原则：迁移脚本与切换后的新路径都要有测试；失败方向全部 fail-closed 用例化；不创建真实 brm.db（本轮禁令），全部用临时库。

## 1. 迁移脚本测试（阶段 3-4，实施轮）

| # | 用例 | 预期 |
|---|---|---|
| MT1 | 空库首次建表（执行 schema.sql） | 全约束生效；重复执行幂等或明确拒绝 |
| MT2 | 导入健康 tabclaims（claimed+租约活） | claimed，fencing 基线重排正确 |
| MT3 | 导入 claimed 但租约读不出 | **owner_unknown**（不是 claimed） |
| MT4 | 导入 unknown/borrowed/closed 混合 | 各态保真；borrowed 保持 |
| MT5 | 导入 run_id 为空的旧记录 | 拒绝 + owner_unknown + 迁移审计 |
| MT6 | winlease 健康/损坏/缺失 三态 | 健康→leases 行；损坏/缺失→不导入不视为无主 |
| MT7 | 迁移中途 kill（各阶段注入崩溃） | 重跑幂等；不产生半套权威（旧文件未动+新库回滚/续跑均可校验） |
| MT8 | 备份完整性 | 哈希校验；可打开性 |
| MT9 | 迁移后抽样回放 | 每个 claimed tab 能回答「谁拥有」 |

## 2. 切换后行为测试（阶段 5-6）

| # | 用例 | 预期 |
|---|---|---|
| BT1 | 库可用+文件健康：授权判断 | 只看库；文件损坏不影响 |
| BT2 | 库不可用：navigate/evaluate/click/fill/send/clear/close/cancel/stop/window release **逐项** | 全部拒绝（裁定：不只写业务操作） |
| BT3 | 旧票+库拒绝 | 拒绝（票非通行证） |
| BT4 | fencing 换代后旧进程动作 | stale_fencing 拒绝；bsk 幂等 close 场景最终一致 |
| BT5 | 状态机非法迁移（如 closed→claimed、ACTIVE→STOPPED_CONFIRMED 直跳） | 触发器 ABORT |
| BT6 | 双进程并发 claim/takeover/close | BEGIN IMMEDIATE 串行；无双授权 |
| BT7 | session idle 回收后原 run 动作 | not_found→IDLE_EXPIRED；不自动重建；写全拒 |
| BT8 | reconcile 各决策表行 | 逐行对照 brm-db-reconcile.md §2 |
| BT9 | 「closing+tab 仍在+跨 run」重发 close | 拒绝自动重发 |
| BT10 | 「closing+本 run+租约活」重发 close | 允许并审计 |

## 3. fail-open 回归专项（对应裁定「回滚不得恢复无主放行」）

| # | 用例 | 预期 |
|---|---|---|
| FO1 | 租约文件损坏 + 库无该 instance 记录 + tabs 有活跃记录 | OWNER_UNKNOWN 禁写（winlease 缺口修复验证） |
| FO2 | 库损坏（首字节破坏） | 任何副作用拒绝；不静默重建空库 |
| FO3 | 磁盘满（quota 目录） | 写失败→fail-closed；已发出的 bsk close 有 reconcile 兜底 |
| FO4 | 回滚测试（临时切回旧代码路径） | 旧「读不出→无主」路径被特性开关禁用；不可复活 PID 判据 |
| FO5 | takeover 出口门 | 门 1-5 任一缺证据 → 拒绝；全过+观察期 → 允许且 fencing=旧+1 |

## 4. 并发与事务专项

| # | 用例 | 预期 |
|---|---|---|
| CT1 | 事务内不做长等待（close_prepare 提交后才发 bsk 命令） | 无长占锁；锁等待超时=拒绝不是降级 |
| CT2 | close_commit 复读 fencing | 换代后旧提交路径行为符合设计（stale 记审计） |
| CT3 | 两进程同时 reconcile 同一 session | 幂等；无重复审计污染判断 |
| CT4 | WAL 恢复 | 崩溃后可打开；未提交事务回滚 |

## 5. 执行环境与顺序

- 全部临时目录/临时库，不触业务数据（延续 P1 测试环境定义）。
- 顺序：MT（迁移脚本）→ BT（行为）→ FO（fail-open 回归）→ CT（并发）。
- 验收门：全绿 + Web AI 验收后才允许真实迁移窗口（需用户协调「无任务运行」时机，M2）。

## 7. 特性开关回滚安全（P1-A 修订新增，v1.1，对应裁定）

裁定要求：回退代码不恢复旧 PID 判据；损坏文件不重新变成无主；不得双权威；回滚过程 fail-closed。

| # | 用例 | 预期 |
|---|---|---|
| FF1 | 开关=brm.db 路径全绿 → 切回旧路径 → 立即跑 FO1/FO2 | 旧路径上「损坏→无主」分支**已被先行修补**或被开关短路；不得出现无主放行 |
| FF2 | 开关回退后 takeover 调用点审计 | 回退包不含 PID 死亡判据调用（结构检查 + grep 门禁进 contract_lint） |
| FF3 | 回退期间双源观察 | winlease 文件为只读缓存（无写入进程）；brm.db 停止更新但保留为证据——无并发双写 |
| FF4 | 回退动作本身失败一半（开关文件损坏） | 默认值=brm.db 路径 + fail-closed；**不得默认 fail-open** |
| FF5 | 再切换回 brm.db | reconcile 首扫补齐回退期间的观察缺口；无双权威残留 |

设计约束：开关是**配置文件单点**（不是环境变量布尔量——吸取 v6 教训），缺失/损坏按 FF4 处理；开关状态入 audit_refs。

## 8. 假设

| # | 假设 | 标记 |
|---|---|---|
| TP1 | 磁盘满可稳定模拟（Windows quota/小容器卷） | INFERRED |
| TP2 | 特性开关机制在实施轮引入（代替 git revert） | 设计建议，需 Web AI 认可 |
