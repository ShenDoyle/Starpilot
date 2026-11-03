# brm-db-reconcile.md

BRM P1-A · 副作用与账本不一致的 reconcile 恢复设计（只读设计，不实施）
状态：送审稿 v1 · 2026-11-03 · 供 Web AI 裁定

对应裁定第三补充约束：「close 成功 → SQLite 写入失败 → 重启：不能简单重试 close，也不能释放资源锁。必须先 reconcile 真实资源状态。」

## 1. 不一致的种类与检测

| # | 不一致 | 检测时机 | 检测手段 |
|---|---|---|---|
| D1 | bsk close 成功，mark_closed 提交失败 | 下次任何账本访问该 tab | tabs 残留 closing + 审计有 close 成功记录 |
| D2 | 进程崩溃于「副作用已发、结果未知」 | 重启后 reconcile 扫描 | 审计尾部有 prepare 无 commit + updated_at 超时 |
| D3 | session 被 daemon idle 回收，账本仍 ACTIVE | 动作前探测 / 定期扫描 | 动作返回 not_found / session list 缺失 |
| D4 | 账本有 session，bsk tab list 无对应 tab | reconcile 扫描 | 双向对账（已有实现，CODE_CONFIRMED） |
| D5 | bsk 有 tab，账本无记录 | reconcile 扫描 | 同上；只报告不认领（已实现语义） |
| D6 | bsk close/cancel 实际失败但返回模糊 | 超时归类 STOP_UNKNOWN | 状态机 STOP_UNKNOWN 路径 |

检测入口（实施轮）：① 动作前门（已有）；② `reconcile(session_id)` 显式入口（启动时 + STOP_UNKNOWN 恢复时 + 兜底定时）；③ 迁移后首扫。

## 2. reconcile 决策表

### 2.1 tab 级

| 账本态 | 真实观察 | 动作 | 裁定依据 |
|---|---|---|---|
| closing + 审计有 bsk close 成功 | tab 不在 list | 置 closed（**安全**：真已关闭，只是账本没记上） | 恢复流程而非重试 |
| closing + 审计无 bsk 结果（D2） | tab 不在 list | 置 closed + 审计 inferred_close（真实已不存在，残留只是账本） | 同上 |
| closing（D2） | tab **仍在** list | **保持 closing** + 告警；不重发 close、不回 claimed；人工/新证据裁决 | 未知时保留保护，禁止自动释放 |
| claimed | tab 不在 list | 置 closed + 审计（浏览器侧自行消失：崩溃/手关） | D4 |
| 任何态 | tab 在 list 但 URL/window 变了 | 更新 last_url/window，只报告 | — |
| 任何态 | 账本无记录的 tab | **只报告不认领**（已实现，CODE_CONFIRMED） | D5 |

### 2.2 session 级

| 账本态 | 真实观察 | 动作 |
|---|---|---|
| ACTIVE | session list 无 + not_found 复现 | IDLE_EXPIRED（evidence 记录）+ 名下 owned tabs → unknown/must_reinspect |
| ACTIVE | session list 有 | 不动 |
| STOPPING | list 无 + daemon 日志 stopped（如可得） | STOPPED_CONFIRMED；tabs → closed |
| STOPPING | list 有 | 停止未生效：保持 STOPPING，重查（不盲目重发破坏性命令） |
| STOP_UNKNOWN | list 无 | 升级判定：日志佐证→STOPPED_CONFIRMED；无佐证→保持 STOP_UNKNOWN（list 缺失单独不构成确认，裁定） |
| DAEMON_UNAVAILABLE | daemon 恢复 | 按恢复后 list 重分类（ACTIVE/IDLE_EXPIRED） |
| OWNER_UNKNOWN | 任何观察 | 不自动转出；仅新证据（迁移导入/人工确认）可变更 |

### 2.3 request 级

| 账本态 | 观察 | 动作 |
|---|---|---|
| PREPARED 且超时 | request 令牌实测（P0）：令牌属 daemon 内存态 | 置 CANCELLED（本地作废）+ 审计 |
| STARTED 且 session 已消失 | request 无宿主 | 置 UNKNOWN→COMPLETED 视 session 终态映射（UNKNOWN 不当成功） |
| cancel_requested_at 置位无结果 | 超时 | 保持 STOP_UNKNOWN 路径，不重试至成功 |

## 3. 「重新 close 是否安全」专项回答

- tab 已不在 list：无需 close（无对象）；账本直接 closed。
- tab 仍在 list 且账本 closing：**不自动重发 close**——因为无法区分「第一次 close 其实没生效」和「close 生效后 tab 被复用/恢复」。前者重发无害，后者重发可能误伤复用者；按「未知时保留保护」裁定，保持 closing + 告警 + 人工裁决。
- 例外：观察期内该 tab 一直属于**本 run**（owner/run/lease 全匹配且租约活）→ 重发 close 是本 run 处置自己的资源，允许（记审计）。跨 run 永远不自动重发。

## 4. 资源锁语义

- reconcile 不释放任何锁：unknown/closing/STOP_UNKNOWN 全部保持占用保护。
- 唯一的锁释放路径 = 状态机出口门全过（STOPPED_CONFIRMED + 身份/归属/在途/fencing/观察期）后的 takeover 或用户显式授权。
- reconcile 自身操作全部入 audit_refs（event='reconcile'，含观察证据摘要），保证可回放。

## 5. reconcile 语义细则（P1-A 修订新增，v1.1，对应裁定）

1. **inferred_close 与真实 close 成功分开**：audit_refs.outcome 必须区分 `close_commit`（bsk 返回成功）与 `inferred_close`（tab 不在 list 推断）。下游（迁移校验、回执）不得把 inferred 当作 daemon 确认。
2. **tab 不存在 ≠ 旧任务死亡**：tab 消失只允许驱动 tab 级状态（→closed）与 session 级观察记录，**不允许**据此推进 takeover 出口门的任何一项（身份/在途/换代）。「任务死亡」判据仍在状态机文档 §3/架构文档 §5。
3. **重复 reconcile 幂等**：同一观察输入重复执行产生相同终态；audit_refs 允许重复记录（审计追加制），但状态字段不漂移——实现约束：所有 reconcile 写路径以「目标态 == 现态 → 只记审计不改行」为前置检查（CT3 用例回归）。
4. **未知状态不能释放资源**：决策表中一切「无法确认」分支的输出都是「保持 + 告警」；reconcile 永不产生从 unknown/closing/STOP_UNKNOWN/OWNER_UNKNOWN 到可认领态的直接迁移（触发器白名单已禁止 closed 之外的反向迁移，schema 层兜底）。

## 6. 假设清单

| # | 假设 | 标记 |
|---|---|---|
| RC1 | 「tab 不在 list」观察稳定（无虚拟化/加载半程误报） | INFERRED，读前加 settle 重试（现有 read_state 已有三次重试，CODE_CONFIRMED） |
| RC2 | daemon 日志在 reconcile 时刻可得且可关联 session_id | INFERRED |
| RC3 | closing 超时人工裁决的 SLA 可接受 | 设计假设，需用户确认 |
