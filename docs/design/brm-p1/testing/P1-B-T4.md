# P1-B-T4 SQLite 事务崩溃/磁盘错误/WAL 恢复与 reconcile 测试设计

BRM P1-B · 只读设计（不启动/停止任何 daemon）
状态：送审稿 v1 · 2026-11-03 · 供 Web AI 裁定

## 1. 实验目的

裁定重点：「必须测试 BSK 副作用成功但账本写入失败。」本组实验覆盖统一账本的全部失败方向与恢复路径，验证 fail-closed 全链路 + reconcile 决策表。

## 2. 隔离条件

临时库（环境变量指向 `<tmp>`）；磁盘满用小型 RAM 盘/quota 目录模拟（TP1 INFERRED，E4.0 先验证可行性）；崩溃注入用子进程 kill；可并用 bsk 实验环境或纯账本层（多数用例无需浏览器）。

## 3. 操作步骤

| 实验 | 场景 | 步骤 |
|---|---|---|
| E4.0 | 磁盘满模拟可行性 | RAM 盘 quota 1MB → 写库至满 |
| E4.1 | 事务中 kill | BEGIN IMMEDIATE 写入中 kill 子进程 → 重开库验证 WAL 回滚 |
| E4.2 | close 成功+commit 失败 | 临界区④后置库为满 → ⑥ 失败 → 重启 → reconcile 检出 closing 残留 → 决策表分支验证 |
| E4.3 | 库损坏（首字节破坏） | 全部读路径返回什么；写路径拒绝；**不静默重建空库** |
| E4.4 | 磁盘满下的各操作 | navigate/fill/close 的门全拒（fail-closed） |
| E4.5 | 多进程并发写 | 4 进程同时 claim/close → BEGIN IMMEDIATE 串行 + busy_timeout 行为 |
| E4.6 | reconcile 幂等 | 同一 session 连续 reconcile 3 次 → 状态零漂移 + 审计可重复 |
| E4.7 | 状态机非法迁移 | 直接 SQL UPDATE 触发触发器 ABORT（closed→claimed、ACTIVE→STOPPED_CONFIRMED 直跳） |
| E4.8 | fencing 单调触发器 | 插入非 prev+1 / UPDATE 回退 → ABORT |
| E4.9 | owner_key 触发器 | 插入 owner_key≠workspace|run_id → ABORT |

## 4. 观察指标

- 每个失败方向的返回码/异常类型（应全部 fail-closed，无静默成功）。
- WAL 恢复后数据完整性（E4.1 前后行数/内容比对）。
- reconcile 决策表分支命中率与状态漂移检测（E4.6）。
- 触发器拦截率（E4.7-E4.9 应 100% ABORT）。

## 5. 预期结果与失败判据

| 实验 | 预期 | 失败判据 |
|---|---|---|
| E4.2 | 残留 closing → reconcile → 按表处置，资源保护不解除 | 出现自动放行/自动 close → fail-closed 缺口 |
| E4.3 | 读=明确错误；写=拒绝；无自动重建 | 静默重建空库 → 严重缺口 |
| E4.5 | 无死锁、无丢失写、超时=拒绝 | 出现「超时当成功」→ 缺口 |
| E4.6 | 三次结果一致 | 状态漂移 → 幂等缺口 |
| E4.7-9 | 全 ABORT | 任一放行 → schema 约束缺口 |

## 6. 清理方法

临时库/临时目录删除；子进程确保回收（kill 后 wait）；RAM 盘卸载。

## 7. 业务环境保护

- 全部用例不连接业务库（`~/.starpilot` 只读参照，不写入）。
- E4.3 的库损坏注入只发生在 `<tmp>` 副本。
- 崩溃注入前核对子进程命令行含实验标记。

## 8. 对 P1 设计的输出

产出「fail-closed 全路径验证表」（每方向 REAL_PASS/REAL_FAIL）→ 迁移测试计划 MT/FO 组的实测版；E4.2 结果直接验证裁定补充约束三（close 成功→SQLite 失败→重启→reconcile）。
