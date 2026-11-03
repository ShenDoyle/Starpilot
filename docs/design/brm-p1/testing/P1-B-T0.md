# P1-B-T0 隔离环境与身份确认测试设计

BRM P1-B · 只读设计（不启动/停止任何 daemon）
状态：送审稿 v1 · 2026-11-03 · 供 Web AI 裁定

## 1. 实验目的

建立后续 T1-T4 全部实验的隔离底座，并验证隔离本身成立：第二 daemon 可共存、独立 profile 不触碰业务、状态目录完全分离、实验身份可从进程/日志层面与业务环境区分。

## 2. 隔离条件

| 维度 | 业务环境 | 实验环境 |
|---|---|---|
| 浏览器 | 用户日常 Edge/Chrome profile | `--user-data-dir=<tmp>/prof` 全新 profile（含测试用扩展加载，INFERRED：独立 profile 可加载扩展，NOT_TESTED） |
| daemon | 业务常驻 daemon（不碰） | `bsk daemon start --listen 127.0.0.1:<试验端口> --session-idle 30s --daemon-idle 10m`，独立进程 |
| 状态目录 | `~/.starpilot/locks` 等 | 全套环境变量重定向到 `<tmp>/state`（STARPILOT_LOCK_DIR / tabclaims DB_ENV / AUDIT_ENV） |
| 身份 | — | 实验命令行统一带标记字符串 `starpilot-p1b-t0`，执行前 `Get-CimInstance` 核对命令行后才允许 kill/清理 |

## 3. 操作步骤

1. E0.1 记录业务环境快照：业务 daemon PID/端口、现有 session list、浏览器窗口清单（只读）。
2. E0.2 起 experiment 浏览器（独立 profile）→ 确认扩展 healthy。
3. E0.3 用试验端口起实验 daemon → `session list`（实验 daemon 侧）应为空；**业务 daemon 侧 list 不得出现实验 session**。
4. E0.4 同机起第二个实验 daemon（不同端口）→ 验证三实例共存；`daemon-idle 10m` 内自行退出验证。
5. E0.5 实验 session `start --name starpilot-p1b-t0` → 30s idle 后观察自动回收与窗口关闭。
6. E0.6 全程结束后快照比对（E0.1 与 E0.6 一致性）。

## 4. 观察指标

- 业务 daemon 的 session list 在实验前后逐项一致（含 idle 计数无关项）。
- 实验daemon/session 的启停时间线与 daemon 日志记录对应。
- 第二 daemon 是否影响第一实例（端口/数据目录冲突形态）。
- 实验 profile 的窗口是否只含实验 session 的 Agent Window。

## 5. 预期结果与失败判据

| 实验 | 预期 | 失败判据 |
|---|---|---|
| E0.3 | 双 daemon 隔离生效 | 业务侧 list 出现实验 session → REAL_FAIL，全部后续实验叫停 |
| E0.4 | 三实例共存 | 端口冲突/数据互踩 → 独立环境形态改串行（每次实验单独起停），计划调整后重审 |
| E0.5 | 30s idle 回收 + 窗口关闭 + 日志记录 | 无回收或窗口残留 → 记 REAL_FAIL，T1 基线作废 |

## 6. 清理方法

实验 daemon `daemon-idle 10m` 自动退出（兜底：核对命令行含 `starpilot-p1b-t0` 后 taskkill）；实验浏览器整进程关闭；`<tmp>` 整目录删除（实验产物，非用户数据）。

## 7. 业务环境保护

- 禁止对业务 daemon PID 执行任何命令（脚本内硬编码检查：目标 PID 命令行必须含实验标记）。
- 实验 session name 固定前缀，业务侧发现同名即告警。
- 全部实验在单一 PowerShell/Python 会话内完成，异常退出时 daemon 随 `--daemon-idle` 自灭。

## 8. 结论标记

预期「第二 daemon 可共存」为 INFERRED，实验后按结果标 REAL_PASS/REAL_FAIL；其余结论同规则，不推断。
