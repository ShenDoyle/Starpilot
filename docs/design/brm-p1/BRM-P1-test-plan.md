# BRM-P1-test-plan.md

BRM P1 · 分阶段实测计划（只读设计，不实施）
状态：送审稿 v1 · 2026-11-03 · 供 Web AI 裁定

原则：所有实验在**独立测试环境**进行，不触碰业务 daemon、业务 session、业务浏览器 profile；实施前先取得 Web AI 对实验方案的裁定（P1 结束条件）。

## 1. 独立测试环境定义

- 独立浏览器 profile（Chrome/Edge `--user-data-dir=临时目录`），不加载生产扩展配置（INFERRED：扩展可用独立 profile 加载，NOT_TESTED）。
- 独立 daemon：`bsk daemon start --listen 127.0.0.1:0 --session-idle 30s`（短 idle 加速实验）。多 daemon 实例共存能力本身是待验证假设（INFERRED，P1-T0 验证）。
- 数据隔离：BRM 账本环境变量指向临时目录；实验结束整目录可弃。
- 不变量：不修改任何业务状态；不 install shim；不动安装位。

## 2. 实验组

### T0 · 环境前置（全部后续实验的门）

| 实验 | 验证点 | 预期 |
|---|---|---|
| T0.1 | 第二个 daemon 实例能否共存于同一浏览器 | 能/不能（决定独立环境形态） |
| T0.2 | 短 `--session-idle 30s` 生效性 | 30s 后 session 消失且日志记录 |
| T0.3 | `--daemon-idle` 行为 | 空闲 daemon 自行退出（CODE_CONFIRMED 参数存在） |

### T1 · idle 计时机制（方案选择的关键）

| 实验 | 验证点 | 对应设计问题 |
|---|---|---|
| T1.1 | 开 session 后零操作，30s idle 回收 | 基线（CODE_CONFIRMED 参数） |
| T1.2 | 期间每 15s 发 `session list`（非动作命令） | 非动作命令是否刷新 idle → 方案 B 可行性 |
| T1.3 | 期间每 15s 发 `cooperate sync` | 同上，第二类候选 |
| T1.4 | 对 about:blank 每 15s `evaluate 1+1` | 动作命令是否刷新 idle → 方案 C 前提 |
| T1.5 | 对 about:blank 每 15s `navigate` | 导航类动作的副作用边界 |
| T1.6 | `tab list` | 只读命令刷新与否 |

判读：任一「无页面副作用命令」刷新 idle → 方案 B/C 出现安全实现；全部不刷新 → 锁定方案 D。**结果标记 REAL_PASS/REAL_FAIL，不推断。**

### T2 · daemon 生命周期

| 实验 | 验证点 |
|---|---|
| T2.1 | session 存活时 kill daemon → session list（新 daemon 起后）是否为空；旧 session 是否可恢复（预期不可，INFERRED） |
| T2.2 | kill daemon 后浏览器窗口残留状态；扩展行为 |
| T2.3 | daemon 重启后旧 request 令牌是否还能 start（预期作废，INFERRED） |
| T2.4 | daemon 崩溃瞬间发起 navigate 的调用方收到什么错误（连接层 or 应用层）——验证 DAEMON_UNAVAILABLE 与 not_found 可区分（状态机假设 A5） |
| T2.5 | 优雅 stop daemon（若 CLI 提供）vs kill 的差异 |

### T3 · request/cancel 竞态

| 实验 | 验证点 |
|---|---|
| T3.1 | 并发 2 进程同时 cancel 同一 prepared request | 原子性（账本层设计验证） |
| T3.2 | cancel 与在途 navigate 竞态 | 谁赢、错误形态；验证 session_busy/not_found 兜底 |
| T3.3 | cancel 已启动 session 后立即 tab list | 窗口/标签清理完整性（REAL_PASS 初证，补强） |
| T3.4 | cancel 超时模拟（daemon 挂起） | 调用方行为 → STOP_UNKNOWN 路径 |

### T4 · 账本崩溃恢复

| 实验 | 验证点 |
|---|---|
| T4.1 | 账本事务中 kill 进程 | WAL 恢复、状态一致性 |
| T4.2 | 磁盘满模拟（小 tmpfs/quota） | fail-closed 行为 |
| T4.3 | 多进程并发 claim/takeover | 事务串行性 |
| T4.4 | 「STOPPING 置位后命令未发」崩溃恢复 | 恢复逻辑按 schema §5 |

## 3. 阶段划分与验收门

| 阶段 | 内容 | 验收门 |
|---|---|---|
| P1-a | T0+T1（idle 机制） | 拿到方案 A-D 的 REAL_PASS/FAIL 表，Web AI 确认方案选择 |
| P1-b | T2+T3 | 状态机假设 A3/A5 验证；request 治理设计确认 |
| P1-c | T4（可与实施轮合并） | fail-closed 全路径验证 |
| P2（实施轮） | 按批准后的设计写代码 | 全量测试 + Web AI 验收 + 放行提交 |

## 4. 风险与止损

- 每个实验独立 profile/目录，失败不影响业务。
- 实验 daemon 一律 `--daemon-idle 10m` 内自动退出，不留常驻。
- T2 系 kill 的是**测试 daemon**（独立进程 PID），误伤可能性低，但执行前必须 `Get-CimInstance` 核对进程命令行含测试标记（如独立 listen 端口）后才 kill。
