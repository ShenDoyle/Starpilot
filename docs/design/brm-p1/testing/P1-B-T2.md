# P1-B-T2 daemon restart/crash 恢复测试设计

BRM P1-B · 只读设计（不启动/停止任何 daemon）
状态：送审稿 v1 · 2026-11-03 · 供 Web AI 裁定

## 1. 实验目的

裁定要求区分三种 daemon 终止形态（正常退出 / 崩溃 / 重启）并各自验证 session/request/浏览器窗口的恢复行为；消除「CLI 无 attach/restore 参数 ⇒ 不恢复」这一 INFERRED 的不确定性。

## 2. 隔离条件

继承 T0；实验 daemon（试验端口+独立状态目录）；浏览器独立 profile；三种形态各用独立 session。

## 3. 操作步骤

| 实验 | 形态 | 步骤 |
|---|---|---|
| T2.1 | 正常退出 | session 活跃时向 daemon 发优雅退出信号（CLI 若提供 stop/graceful；否则 Ctrl-Break 到前台进程）→ 观察退出日志 → 用同参数重启 → `session list` |
| T2.2 | 崩溃 | session 活跃时 `taskkill /F` 实验 daemon PID（先核对命令行含实验标记）→ 立即观察浏览器窗口/扩展状态 → 重启 daemon → `session list` |
| T2.3 | 崩溃+在途 request | start 后立刻发 navigate（目标本地慢页 8s）→ 1s 后 kill daemon → 重启后：旧 request 令牌再 start / 再 inspect |
| T2.4 | 崩溃+在途动作对页面影响 | 同 T2.3，观察重启后目标页面最终状态（导航是否完成/残留） |
| T2.5 | 恢复后旧 session 接受新操作？ | T2.1/T2.2 重启后对旧 session_id 发 `tab list` / `navigate` |
| T2.6 | reconcile 孤儿识别 | T2.2 后账本（临时库）仍记 session ACTIVE → 跑 reconcile 流程 → 验证 IDLE_EXPIRED 分类与孤儿报告 |

## 4. 观察指标

- 重启后 `session list` 是否含旧 session（恢复 or 清空）。
- 旧 session_id 上新操作的返回码（not_found / 正常 / 其他）。
- 浏览器窗口残留清单（kill 前后对比）。
- request 令牌的存活性与语义（能否 start、inspect 返回什么）。
- 在途导航的最终页面 URL（迟到执行证据）。
- daemon 日志对崩溃/恢复的记录完整度（可解析性，供 takeover 判据 b 评估）。

## 5. 预期结果与失败判据

| 实验 | 预期（均 INFERRED 待证） | 失败判据 |
|---|---|---|
| T2.1 | 正常退出会话清空，重启后 list 空 | 旧 session 恢复 → 修正 architecture §6 结论 |
| T2.2 | 崩溃后窗口残留、list 清空 | 出现「幽灵可恢复 session」→ 全新风险项，升级报告 |
| T2.3 | 旧令牌作废（start rc≠0） | 令牌仍可 start → request 持久化风险项 |
| T2.5 | 旧 session 上操作返回 not_found | 操作成功 → session 状态恢复风险项 |
| T2.6 | reconcile 正确分类 + 不自动转可认领 | 错误放行 → fail-closed 缺口 |

**特别观察（裁定 T3 关联）**：T2.3/T2.4 若重启后导航仍完成 = 「旧操作迟到执行」REAL_PASS 证据 → takeover 观察期设计必须吸收。

## 6. 清理方法

kill 仅限实验 daemon（三重核对：PID→命令行含标记→端口为试验端口）；浏览器整进程关；残留窗口人工确认后关闭；临时目录删除。

## 7. 业务环境保护

- 业务 daemon PID 启动时快照存档，每次 kill 前比对（PID 复用防线）。
- kill 用 PowerShell `Stop-Process -Id <实验PID>`，先 `Get-CimInstance` 输出命令行确认。
- 全部产物在 `<tmp>`；不触碰 `~/.starpilot/locks` 业务目录。
