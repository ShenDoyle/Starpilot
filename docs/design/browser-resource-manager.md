# Browser Resource Manager 设计修订 v2（2026-11-02）

> 依据：Web AI 裁定（review-1102-reply4.md）。本轮只做设计修订与测试设计，
> 不扩大实现、不提交、不升版本、不推送、不同步安装位。
> 前置文档：`runtime/wa/inventory-stage2.md`（只读盘点，行号证据都在那里）。

## 1. 资源模型（修订版，补齐身份字段）

```json
{
  "instance": "7cb0cac4",                 // browser_instance_id
  "window_id": "2095513696",              // bsk agent_window_id（session start 返回）
  "task_id": "<run_id 或 workspace 短名>",
  "owner_id": "<workspace>|<run_id>",     // owner_key，跨进程可比
  "session_ids": ["wibt"],
  "lease_id": "hex12",                    // 每次全新 acquire 重新生成
  "fencing_token": 7,                     // 实例级单调递增，acquire/takeover 各 +1
  "host_id": "WIN-51LK043P2QE",           // socket.gethostname()
  "process_id": 4184,
  "process_start_time": 1793590000.123,   // 进程创建时刻，防 PID 复用
  "heartbeat_at": "...",                  // ISO 时间
  "lease_expiry": 1793598952.9,
  "mode": "exclusive | serial",
  "release_reason": null                  // stop|reap|takeover|crash|explicit
}
```

**进程身份 = (host_id, process_id, process_start_time) 三元组**。
只对 PID 会撞上复用（2026-11-02 裁定点名）；`process_start_time` 用 ctypes
`OpenProcess + GetProcessTimes` 取（标准库，无新依赖，取不到 = 身份不可确认）。

## 2. 状态机

```
租约状态机（实例级，一份）
  FREE ──acquire──▶ BUSY ──heartbeat──▶ BUSY
  BUSY ──release(owner 校验)──▶ FREE
  BUSY ──TTL 过期 + 进程死亡确认──▶ STALE ──acquire──▶ BUSY(token+1)
  BUSY ──takeover --confirm──▶ BUSY(token+1, release_reason=takeover)
  BUSY ──旧 token 写──▶ 拒绝 STALE_LEASE（租约本身不变）

回收判据（全部满足才自动回收，任一不满足 = 只能人工 takeover）：
  ① lease_expiry 已过
  ② (host_id, pid, start_time) 三元组确认进程已死
  ③ 跨主机租约一律不自动回收（只能 takeover --confirm）
```

**资源状态机**（会话级声明，挂在租约的 `sessions` 里）：

```
UNCLAIMED ──claim_session──▶ CLAIMED(owner_key)
  CLAIMED ──租约过期/接管/释放──▶ UNCLAIMED（sessions 清空）
```

`enforce_single_session` / `reap` / `stop` / `window release` 只对
UNCLAIMED 或「owne_key == 自己」的会话动手。

## 3. 锁原子性设计（修 TOCTOU）

现状缺陷（`statelock.py:150-156`）：接管 = `remove(p)` 再 `create(p)`，
A 看到「已死」→ B 建新锁 → A 删 B 的新锁。修订：

**接管协议（rename-claim，两步走）**

```
1. os.rename(lock, lock.taken-<uuid>)      # 原子改名；Windows 下 dst 不存在必成功，
                                           # 两个竞争者只有一个 rename 成功
2. 读 taken 文件内容，确认就是要接管的那个 holder（按 lease_id+token 比对）
   ├─ 不符（别人刚换了锁）→ os.rename(taken, lock) 原路放回，重试外层循环
   └─ 相符 → O_EXCL 新建 lock（带自己的 payload，token = 旧 token + 1）
              → 删 taken；失败则 rename 回去
3. 释放同理：rename-claim 拿到文件 → 校验 owner_key 是自己 → 删除；
   校验失败 → 原路放回。旧持有者永远删不掉新持有者的锁。
```

`os.link` 在 Windows/NTFS 可用，但 rename-claim 不依赖链接语义，且失败路径可回滚，选它。

## 4. fencing_token 覆盖表（副作用入口 × 检查点）

| # | 副作用入口 | 位置 | token 检查点 |
|---|---|---|---|
| 1 | 页面输入（fill/insert） | `wa.py do_send` / `write_gate.execute_send` | 效果前最近一次 bsk 调用前 |
| 2 | 点击发送 | 同上 | 同上（与 1 同事务，check→effect 间隔 < 1 个 bsk 调用） |
| 3 | 清空输入框 | `wa.py cmd_clear` | cmd 入口 + ensure_session 返回后 |
| 4 | 页面切换（navigate） | `navigate_soft` 调用方（bind/ensure_session） | 同 1 |
| 5 | **session stop** | `enforce_single_session:860` / `reap:496` / `cmd_stop:2132` / `cmd_window` | **每次 stop 前**逐会话检查（不只 CLI 入口） |
| 6 | window release | `cmd_window` | 入口 + stop 前 |
| 7 | tab create（自愈） | `heal_window_placement:575` | 同 1 |

**诚实声明（竞态残留）**：扩展侧不校验 token，「检查 → 效果」之间存在窗口
（检查后、bsk 调用生效前，租约可能恰好过期被接管）。缓解：
- 检查点紧贴效果（同一个函数内、间隔 < 1 次 bsk 往返）；
- 效果完成后再 heartbeat 一次，失败即告警（事后可发现）；
- **高危操作（session stop / window release）额外要求串行模式**：这两类操作只在
  `mode=exclusive` 且 token 校验通过时执行，serial 排队中的任务根本拿不到执行权。
- 彻底消除需扩展侧支持 token（列为对 bsk 的需求，不在本轮范围）。

## 5. 能力探测（不看版本号）

实例级探测缓存（`locks/caps-<instance>.json`，TTL 10 分钟）：

| 探测 | 方法 | 判定 |
|---|---|---|
| P1 window_id 可得 | `session start` 返回含 `agent_window_id` | 缺 → 串行 |
| P2 tab scope 可用 | `tab list --scope agent --json` rc=0 | 失败 → 串行 |
| P3 双窗口独立 | 同实例第二个 session 的 evaluate 返回不同 tab_id | 失败 → 串行 |
| P4 结果回对窗口 | 对第二会话 evaluate 读到的 URL 是自己导航的 | 失败 → 串行 |

任一探测**无法确认**（超时/异常/结果含糊）→ 串行。串行等待上限
`STARPILOT_SERIAL_WAIT` 缺省 120s（2026-11-02 裁定数值），超时 rc=9 `WINDOW_BUSY`。

## 6. 旧版兼容（裁定点名「最重要」）

**事实**：旧安装位（`.trae-cn` v0.1.5 等）不认租约，其 `enforce_single_session`
照样会杀新任务的会话——**新代码无法阻止**，这是硬边界，不装作能解决。

**兼容期措施（分层）**：

| 层 | 措施 | 效果 |
|---|---|---|
| L1 检测 | 新任务 acquire 时发现实例上有**未被任何租约声明**的活会话 → 记 `legacy_conflict` 事件 | 至少双方知情，可归因 |
| L2 自保 | 见 L1 情形时，新任务**不并行开窗**：转串行等待，或 fail-closed 提示「实例上有旧版任务的会话」 | 新任务不成为互杀发起方 |
| L3 收敛 | 同步新代码到全部安装位（`.trae-cn`、`~/.workbuddy`）——**需要用户逐个授权**，本轮不执行 | 互杀根除的唯一办法 |

无法阻止旧版杀我们，能做的是：让每一次被杀都可归因（journal + 回执三栏报备），
并把「同步安装位」作为验收门槛之一。

## 7. 测试矩阵（12 项，裁定逐条对应）

| # | 场景 | 用例 | 判据 |
|---|---|---|---|
| T1 | A、B 同时申请同窗口 | `test_acquire_conflict` | 一个成功一个 rc=9，持锁文件内容=胜者 |
| T2 | B 不抢占 A | `test_no_preemption` | A 租约不变，B 异常含持有者 |
| T3 | A 心跳中断仍存活 | `test_live_process_not_reclaimed`（pid+start_time 活） | 不可自动回收 |
| T4 | A 进程死亡才可回收 | `test_dead_process_reclaimable` | 三元组确认死亡后可收，journal 有 stale_reclaimed |
| T5 | 旧 token 失效 | `test_stale_token_refused` | rc=9 STALE_LEASE，零副作用 |
| T6 | A 释放不删 B | `test_release_after_takeover` | rename-claim 校验失败原路放回 |
| T7 | A 清理不影响 B | `test_enforce_skips_other_live_owner_session` | B 会话未 stop |
| T8 | 未知归属不执行副作用 | `test_unknown_owner_fail_closed` | stop/send 全拒 |
| T9 | 能力缺失自动串行 | `test_capability_probe_serial` | P1–P4 任一失败 → serial |
| T10 | 串行超时正确返回 | `test_serial_timeout` | 等 120s 内 rc=9，异常含 waited |
| T11 | 双进程并发接管不覆盖 | `test_concurrent_takeover`（两个线程/进程） | 恰好一个成功，败者拿到的锁内容=胜者 |
| T12 | 旧版绕过 | **不是单测能证伪的**：验收 = 实测旧版任务运行时，新任务 fail-closed/串行 + 事件可归因 | `legacy_conflict` 事件存在 |

外加回归：全量 suite 不降、`contract_lint` PASS、真实双任务并发实测（A send、B bind 互不掉会话）。

## 8. 现有草稿处置清单

| 项 | 处置 | 修改点 |
|---|---|---|
| `scripts/winlease.py` | **保留并修改** | +fencing_token、+进程三元组、回收判据收紧（TTL≠唯一）、能力探测缓存 |
| `wa.py enforce_single_session` | **保留并修改** | +逐会话 token 检查（不只 owner_key） |
| `wa.py ensure_session/reap/stop/lease` | **保留并修改** | token 传递、高危操作串行限制、legacy 检测 |
| `scripts/statelock.py` | **新增修改** | rename-claim 原子接管 + 释放校验 |
| `scripts/write_gate.py` | **新增修改** | execute_send 携带/校验 token |
| `tests/test_winlease.py`（19） | **保留并扩展** | 补 T3/T4/T5/T6/T11（现缺） |
| SKILL / execution-contract / runbook 文档 | **保留并修改** | STALE_LEASE 语义、能力探测、legacy 边界 |
| 未提交状态 | **维持** | 裁定通过前不落库 |

## 9. 失败场景与恢复策略

| 场景 | 恢复 |
|---|---|
| 租约文件损坏（JSON 坏） | 视为 FREE（无主），journal 记 `corrupt_lease`；不猜测归属 |
| 心跳进程被挂起（非死亡） | TTL 过期但三元组活着 → 不可回收，任务自己恢复后续租 |
| 接管中途崩溃 | taken 文件残留 → 下次 acquire 先清理孤儿 taken（内容已无效） |
| 系统休眠跨 TTL | 同「挂起」：进程活着就不回收 |
| 双主机共享锁目录（网络盘） | host_id 不同 → 永不自动回收，只能 takeover |

## 10. 补充（2026-11-02 14:10 用户反馈）：标签生命周期管理

用户现场：同一浏览器窗口堆了 13 个标签，其中「审核完成度裁定」同一对话 **5 个副本**、
DeepSeek 同对话 2 个副本；12/13 落在**用户窗口**（扩展处于共享标签页模式）。
用户指认两个问题：①标签堆积不回收 = 风控诱因；②出问题后 agent 只会反复刷新，不像人类操作。

### 10.1 堆积根因（三层叠加）

| 层 | 机制缺陷 | 证据 |
|---|---|---|
| 开 | 每次 `session start` 都导航到对话 → 新标签；互杀导致会话反复重建 → 标签反复新开 | 今日 `wibt/kyml/emai/clgc/afpi/dqxn/inmk…` 每个都留一个 |
| 落 | 扩展 Automation=共享标签页模式时，标签开在**用户窗口**；`session stop` 只关 Agent Window，用户窗口的标签**不跟着关** | `tab list` 12/13 scope=user |
| 收 | `reap` 只 `session stop`，没有 tab 级回收；无「同对话已有标签则复用」探测 | 全仓无 tab close 调用 |

### 10.2 机制修复（并入 BRM）

| 修复 | 规则 |
|---|---|
| 开前查重 | bind/ensure_session 前先 `tab list`：**同 conv_id 的活标签存在 → 复用该标签（select + navigate），绝不新开** |
| 落点记录 | bind 成功后把 `tab_id` 写进 state；归属校验同时核对 tab_id |
| 停时核销 | `session stop` / reap 后核验：该 tab 若仍在用户窗口 → `tab return`（如仍被借）→ 报告给用户；扩展 API 不允许脚本关用户窗口标签（防误删设计，实测 `tab_close: tab is borrowed; call tab_return first`，且用户活动期间直接拒绝） |
| 收割扩展 | reap 增加 tab 审计：同 conv 副本数 >1、且无任何活会话引用 → 列入报告请人关（**不自动关用户窗口标签**） |
| 刷新预算 | 硬规则：同一 URL 人工刷新 ≤1 次；第二次之前**必须** `triage` 定位；连续刷新 = 违例，进回执三栏（反爬角度：同对话高频重开/重刷是风控信号） |

### 10.3 扩展/CLI 侧需求（对 bsk 上游）

1. `session stop` 时把该会话在用户窗口产生的标签一并核销（或提供 `--sweep-tabs`）。
2. 允许关闭「本会话自己开的历史标签」（按 tab_id 白名单），用户手开的仍禁关。

---

## 变更 v3（2026-11-02 14:xx · 用户直接下令，优先级高于 GPT 的中间态指令）

> 用户在听取「为什么反复开窗」的根因汇报（裸调绕过限流 / 计数不跨任务 / 无标签去重）
> 与三层方案（L1 看门狗 / L2 shim / L3 装置收口）后拍板：按建议执行，并追加四条指令。
> 已用 `decide` 留痕（用户指令覆盖 GPT 中间态指令，GPT 复审继续有效）。

### 11.1 多任务互斥与轮换（用户指令①②③）

- **互斥**：一台浏览器实例 = 一份租约（v2 已有），冲突不抢占。
- **轮换**：拿不到租约不再立即失败——
  - 有人值守（`WA_ATTENDED=1`）：短等 30s → `WINDOW_BUSY_HUMAN`（rc=9），
    JSON 带占用方工作区/剩余时长/可选动作，把现场报给人裁决；
  - 无人值守（默认）：长等 600s 排队（对方 release 即拿到）→ 等不到才
    `WINDOW_BUSY` 退出，外层循环稍后重试，不打扰人；
  - 预算可用 `STARPILOT_LOCK_WAIT` 覆盖（测试/运维调参）。
- **轮流协议**：一轮结束 → `wa.py lease release` 让出实例；**会话保留**（journal
  登记），下一棒直接复用同一窗口。

### 11.2 窗口组（用户指令③）

- 组名 `STARPILOT_GROUP`（默认 `starpilot`）；会话归属记 **journal**（jsonl 追加，
  跨租约世代存活——release 清掉租约文件后「这是组窗口」的事实仍在）。
- 新任务开窗前先查组内活会话（`group_sessions` ∩ bsk 活会话表），命中 → 认领复用，
  **在组窗口里建自己的 tab**，不新开窗口。
- tab 级复用：导航前 `find_tab_by_url` 查同地址标签 → `tab select` 聚焦复用；
  同一对话出现第 2 个标签 = 违例。

### 11.3 禁止无脑重开（用户指令④）

- 全局跨任务开窗计数：锁目录 `session-starts.json`，按实例记，10 分钟 ≤4 次；
- wa.py 是**唯一**开窗入口；`contract_lint.no_raw_session_start` 扫 scripts/ + runtime/，
  wa.py 之外出现 `["session","start"]` 直接 FAIL（本次删掉了 send_once.py / tab_clean.py
  两个历史裸调脚本）。

### 11.4 L1 全局看门狗（原方案 L1，落地为 `reap --global`）

- 不读任何任务的 state 文件：直接问 bsk 要全部活会话，对照 journal 组登记；
- 只收「本组声明过 + 租约已过期」的会话；无主/别组会话**只报告绝不动手**
  （清理边界止于自己组名下——不能因为要看门就去杀别的 skill 的窗口）；
- 附带标签审计：活会话内同地址重复标签 >1 → 报告（不动手，会话是活的）。

### 11.5 L3 装置收口（已执行）

- 新版已同步两个安装位（`~/.workbuddy/skills/starpilot`、`~/.trae-cn/skills/starpilot`，
  后者补齐了缺失的 runbooks/ 目录）；对方任务的 `wa.py wait` 进程恰已结束，无运行时干扰；
- 效果：旧版 `enforce_single_session` 的跨任务互杀随安装位更新消失，裸调的最大动因
  （被杀后应急重开）随之消除。
- L2（PATH shim 全局拦截）暂缓：L3+L1 已覆盖主场景，shim 动全局环境，等 GPT 裁定
  与实际观察后再议。

### 11.6 测试与验证

- winlease 新增 9 条（组登记/跨代复用/全局计数/预算）；contract_lint 新增 2 条（裸调必 FAIL / wa.py 豁免）；
- 全量 503 全绿；修复：acquire 冲突等待预算未接 env 导致用例空等 300s（365s→63s 全量耗时）；
- 双安装位 import 终验通过。

### 11.7 tab 生命周期闭环（2026-11-02 用户指令 v2：tab 累积修复）

- 打开：`navigate_reuse` 同址复用优先（此前已有）；归属自愈 `heal_window_placement`
  改为**复用优先**——同址标签 → 空白残留（`_find_blank_tab`）→ 最后才 `tab create`
  （此前无条件 create，是孤儿标签的直接制造者）；
- 回收：`reap_own_tabs`（新）——只收**自己会话** Agent Window 内的同地址重复
  （留最新/活动）与空白残留；`scope=user` 只报告不动（bsk 无权关用户标签，由人处理）；
  自动接进 ensure_session 两条复用路径，手动入口 `wa.py tab-reap`（--dry-run）；
- 拦截：`contract_lint.no_raw_tab_ops`——wa.py 之外出现 `["tab","create"/"close"]`
  即 FAIL（select/list 只读不拦）。

### 11.8 L2 PATH shim 落地（2026-11-02 用户指令「只要使用 bsk，必须遵守规则」）

- 装置：`~/.local/bin/bsk.exe` 改名 `bsk.bin.exe`，原位放 `bsk.cmd`——仅
  `session start` 路由到 `bsk_shim.py`，其余子命令零开销直通真身；
- 令牌：wa.py 的 `_bsk_raw` 对 session start 注入 `STARPILOT_WINDOW_GATE=1`
  （正路已过租约+计数，shim 放行，不二次计数）；
- 无令牌（非 wa.py 来源）：过全局计数账本（锁目录 `session-starts.json`，与 wa.py
  同一份）——未超限：记账放行；超限：拒绝（rc=9）并指引走 `wa.py`；
- 失效语义：shim 自身故障 **fail-open 并告警**（可用性优先；正路侧仍有
  L3 唯一入口 + lint 双保险）。`env.py --install` 重装 bsk 会还原 exe、绕过 shim，
  需重跑 shim 安装器。

### 11.9 介入语义修正（2026-11-02 用户裁定）

- `WINDOW_BUSY_HUMAN`（rc=9 报人裁决即停）废止。人工介入=并行监督，不打断执行；
- 有人值守：等 30s 仍被占 → `WINDOW_BUSY_NOTICE` 提醒人（非阻塞）→ 继续等满
  轮换预算（600s，与无人值守一致）；等不到才 rc=9 `WINDOW_BUSY`，外层循环重试；
- 流程纪律固化（SKILL §5.5a/§5.5b）：①星舵持续推进到任务完成（规划/汇报/验收），
  除非人主动要求停止；②人提出的想法必须原样整体转达 webai 裁决，agent 只补事实。
