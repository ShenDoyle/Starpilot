# P1-B-T3 cancel/navigate/close/stop 并发竞态测试设计

BRM P1-B · 只读设计（不启动/停止任何 daemon）
状态：送审稿 v1 · 2026-11-03 · 供 Web AI 裁定

## 1. 实验目的

裁定重点：「必须测试旧操作是否可能在 cancel/stop 后迟到执行。」本组实验刻画并发竞态的真实行为，为状态机 STOP_UNKNOWN、临界区 stale-fencing 处理和 takeover 观察期提供实测参数。

## 2. 隔离条件

继承 T0；本地慢服务（127.0.0.1 随机端口，8s 延迟响应）制造受控在途操作；竞态对用脚本并发发起（间隔 50-200ms 扫描）。

## 3. 操作步骤

| 实验 | 场景 | 步骤 |
|---|---|---|
| T3.1 | cancel vs navigate | navigate（慢页 8s）→ 1s 后 cancel → 观察导航是否完成/页面终态/cancel 返回 |
| T3.2 | cancel vs cancel | 两进程并发 cancel 同一 request（100ms 内）→ 各自返回码 |
| T3.3 | stop vs navigate | 同 T3.1 用 session stop → 验证 session_busy 拒绝路径（P0 已见，补并发版） |
| T3.4 | stop vs cancel | 同时发起 → 结果组合 |
| T3.5 | cancel 后迟到执行窗口测量 | cancel 返回后每 500ms 读页面 URL/文本，直至稳定 → 得出「cancel 后操作可能继续的最大时长」 |
| T3.6 | 换代后迟到执行 | 临界区④中（navigate 在途）→ 账本层模拟 fencing 换代 → ⑥ 落库行为验证 stale 审计（需临界区实现存在；实施前用简化版预演） |
| T3.7 | close 幂等 | 同一 tab 连续 close 两次 → 第二次返回码与页面状态 |

## 4. 观察指标

- 每个 bsk 调用的返回码/错误文案（session_busy / not_found / cancelled 等）。
- 页面终态 URL/内容时间线（迟到执行的直接证据）。
- cancel 返回与页面实际静止的时间差（T3.5 的关键数字，直接决定观察期下限）。
- 账本（临时库）状态与页面事实的一致性。

## 5. 预期结果与失败判据

| 实验 | 预期 | 失败判据 |
|---|---|---|
| T3.1 | 导航被中断或完成但 cancel 返回明确；**不允许**「cancel 成功且页面静默继续变化>5s」无记录 | 若出现 → STOP_UNKNOWN 语义与观察期参数必须修正 |
| T3.2 | 恰一进程成功（或双双明确拒绝） | 双双「成功」→ 原子性缺口 |
| T3.5 | 测得迟到窗口 T_late | T_late > 观察期假设（5m）→ 观察期上调（设计修订） |
| T3.7 | 第二次明确 not_found/幂等成功 | 第二次产生未知态 → close 语义缺口 |

## 6. 清理方法

慢服务进程退出；session stop；浏览器关闭；临时目录删除。

## 7. 业务环境保护

- 慢服务/页面/daemon/session 全部实验域；业务 daemon 零接触。
- 并发脚本限制进程数（≤3），避免对实验 daemon 过载产生噪声数据。

## 8. 对 P1 设计的输出

T_late 实测值 → 写回 state-machine 观察期参数（替换 INFERRED 的 5m）；T3.1/T3.5 结果决定「cancel 返回 closed ≠ 浏览器层停止」的量化边界。
