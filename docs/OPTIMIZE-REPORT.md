# Starpilot 优化结果说明（2026-10-29）

> 这份文档回答一个问题：**这一夜到底改了什么、凭什么说它比昨天好、还有哪些没做。**
> 所有结论都带证据路径或可复跑的命令，不做「应该没问题」式的表述。
>
> **两轮**：第一轮按 DeepSeek 评审做「判据的信任根」（见 §一~§九）；
> 第二轮接入 Gemini 并**向它索取评审**（见 §十）。两轮都以真机跑通为验收，不是改完就报账。

---

## 一、一句话结论

Starpilot 的**骨架没动**（六环模型、六条硬规则、14 态状态机全部保留），
动的是「判据的信任根」和「纪律的断言化」：

- **判据从「数节点」换成「认消息身份」**——虚拟列表下计数会骗人，消息 ID 不会；
- **两处实现合成一处**——发送确认和等待判定从「各写一套」变成调用同一个纯函数；
- **六条纪律里能断言的都断言了**——窗口数、需求闸门、迁移合法性、fail_streak 归零、lessons 体检；
- **离线能自测了**——真机 DOM 快照 + 迷你选择器引擎，改坏选择器不连浏览器就红。

验收：**112 个单测全绿**，**真机跑通一轮 `INIT → ROUND_DONE`**（`effective_delta=1` 且 `delta_source=anchor`），
`audit --trace` 四项归零且 `verify_verdict=VERIFIED`，`close --require-lessons` PASS。

---

## 二、过程（不是「我改完了」，是「怎么改出来的」）

| 步 | 动作 | 产出 |
|---|---|---|
| 1 | 读项目：`SKILL.md` / `README.md` / 契约 / 6 个脚本 / tests / lessons / 29 轮规划留档 | 结构图 + 问题清单（口径漂移、无自测、无锁、迁移无审计…） |
| 2 | 用项目自身能力（`wa.py` + `platforms.py` + `bsk`）连 DeepSeek，把项目状况发过去 | `runtime/deepseek-review/turn-002-reply.md`（5548 字架构评审） |
| 3 | 按评审落地 S1–S8 + 全部漏项 | 单测 42 → 91 |
| 4 | **真跑**一轮端到端（`runtime/e2e`） | 抓到 2 个真缺陷（子进程 state、地址守卫比副本）→ 修 → 91 全绿 |
| 5 | 再真跑一轮（`runtime/e2e2`） | 又抓到 1 个真缺陷（**等待侧还在数节点**）→ 修 → 112 全绿 |
| 7 | 收尾自检（文档同步时） | 需求闸门把「裁定表」当确认行 → 加词表判定 + 2 条回归 |
| 6 | 把三轮裁定落进契约 v1.1 + 回填 lessons / 平台事实 / 文档 | 本文件 |

**关键一点**：DeepSeek 的评审不是「参考意见」，是**逐条裁定**——它给出四件事：
致命项排序、可执行收口（含验收标准）、明确不做的事、以及「你上轮哪条没做完」。
所以第 3 步之后我做的不是「自由发挥」，是**照验收标准交作业**。

---

## 三、改了什么（按评审编号）

### 3.1 四个致命项

| 编号 | 问题 | 改法 | 验收 |
|---|---|---|---|
| **F1** | 虚拟列表计数不稳，delta 会算出 0 或 2 | 增量改由**锚点**推导：平台消息 ID（`data-virtual-list-item-key`）> 正文指纹 > 计数。`platforms.anchor_delta` 用集合差算 | `tests/test_confirm.py`（含两个历史事故回放）+ `TestKeyAnchor` |
| **F2** | 发送确认两份实现，口径漂移过 | 新增 `scripts/confirm.py`，`classify_send_result` 成为**唯一**判定实现；`wa.py` 与 `write_gate.py` 都调它 | `test_confirm.py` 8 条 classify |
| **F3** | 多 run 并发写 state 无锁 | 新增 `scripts/statelock.py`：`O_EXCL` + 死进程/超龄接管，`wa.save_state` 写必加锁，`wa.py unlock` 兜底 | `test_statelock.py` 7 条（含「死 pid 可接管」「不误删外来锁」） |
| **F4** | 无法离线自测，只能拿真页面赌 | 录制 DeepSeek/Kimi 真机 DOM 快照到 `tests/fixtures/`，写 `tests/domsel.py`（纯 `html.parser` 的迷你选择器引擎）重放 | `test_platforms_replay.py` 12 条（改坏选择器离线就红） |

### 3.2 六个漏项

| 漏项 | 改法 |
|---|---|
| 迁移不可逆却无审计 | `REPAIR_EDGES` 声明每条修补边的合法 actor；非法迁移记 `transition_rejected` |
| `fail_streak` 恢复语义未定义 | 恢复**不改** `fail_streak`（崩溃既不算失败也不算成功） |
| 审计缺因果链 | `send_id` 贯穿 `write_send → write_verify → replies/round-N.md`；新增 `audit --trace` |
| lessons 无写入协议 | 13 段定长格式（+`trigger`/`rollback`/`blocking`），`lessons_audit.py` 体检 |
| 硬规则 6 无运行时断言 | `orchestrator.py windows`：同实例会话数 > 1 即拒绝继续，并把原因写进 journal |
| 预算与 fail_streak 交互未定义 | `budget_gate()` 成唯一判定点：`max_rounds` 数实际发送轮次，失败也消耗、恢复不返还 |

### 3.3 第二轮裁定三项（本轮新增）

| 裁定 | 要求 | 落地 |
|---|---|---|
| 1 | `PAUSED_HUMAN → AWAITING_REPLY` 不能只靠 actor，要有证据 | `repair_precondition()`：必须有 `last_send.send_id` 且 `verdict ∈ INCONCLUSIVE*`；已判清的没等到回复按**真失败**处理 |
| 2 | `fail_streak` 只统计「有结论的失败」 | `VERDICT_EFFECT` 映射表 + `apply_verdict_effect` **唯一写入口**；未知 verdict 按失败处理；源码守卫测试禁止裸写 |
| 3 | lessons 的 open 不能长期挂着 | open 必须带 `blocking`；超 14 天 WARN（`--strict` 非 0）；`blocking=true` 的 open **直接拒绝 close** |

---

## 四、三个真缺陷（端到端跑出来的，不是推的）

这一节是这份报告最该看的部分：**它们都是「离线全绿、真机炸」型**。

### 缺陷 1：闸门是子进程，读的是子进程 cwd 的 state

`--workspace runtime/e2e` 时，编排器记的是 deepseek 的 URL hash，闸门子进程却读到了项目根的
`.wa-state.json`（Kimi 绑定）→ 平台一致性校验错乱。
**修**：新增 `wg.wa_state_path(root, explicit)`，`run_gate()` 显式传 `--state`。

### 缺陷 2：地址守卫比的是 state 副本，不是活页面

`guard_ok()` 只看 `editor_len` / `modal`，因此「state 说在 A、页面其实在 B」查不出来。
**修**：`guard_ok(snap, bound_url)` 比**活页面** URL（`_url_key` 去 query/fragment），不匹配即
`WRITE_BLOCKED_GUARD`「地址漂移」。

### 缺陷 3：F1 只修了「发」，「等」还在数节点

发送确认改成锚点后，真发一条：发送前 `reply_count=2`，回复 2597 字已经渲染出来，
`reply_count` 还是 2 —— 虚拟列表把旧气泡卸载了，净数量持平。
`wait_for_reply` 当时还写着 `reply_count >= before+1`，于是判 `reply_timeout` 等满 300 秒；
重试再等 300 秒，两次失败把状态推到 `HANDOFF`。

**这不是新 bug，是同一个 bug 的另一半**：同一份数据、两个口径，注定漂移。

**修**：
- `wait_for_reply(before=...)` 收发送前锚点快照，走 `anchor_delta`（新锚点 ≥1 或末条助手锚点变化）；
- `last_send` 落 `asst_keys_before / asst_fps_before / last_asst_fp_before`；
- 等待成功走 `apply_verdict_effect(st, "SENT")` 清零（三值语义里的「有结论的成功」）；
- 补 3 条回归：计数持平但锚点变 → 判到达；无锚点 → 退回计数；修补边 agent 被拒。

修复后的现场（`runtime/e2e2/runs/20261029T054159-e2e2/writes/*.result.json`）：

```json
{"verdict": "SENT", "delta_source": "anchor", "effective_delta": 1,
 "reply_count_before": 2, "reply_count_after": 2, "msg_count_before": 4, "msg_count_after": 3,
 "count_anchor_disagree": true, "signals": ["needle"]}
```

**计数口径全线失真（`reply_count_delta=0`、`msg_count_delta=-1`），锚点口径唯一可用。**
这就是把「判据建立在哪」当架构问题的原因。

### 附带修的第四个（不是评审点出来的，是实测撞出来的）

同一台机器两个 Edge profile 同时在线时，`bind` 直接报 `multiple browsers are online`；
`state.browser` 又没被持久化，每次都得人肉传环境变量。
**修**：`ensure_session` 把 `WA_BROWSER` 给的实例写进 `state.browser`（含早退路径）；
多个实例且记不住时**仍然拒绝猜**（拿 A 账号的登录态操作 B 账号是真事故）。

---

## 五、验收证据（可复跑）

```bash
# 1. 全部单测
python -m unittest discover -s tests
# → Ran 112 tests ... OK

# 2. 真机跑通的那一轮：状态、预算、窗口断言
python scripts/orchestrator.py --workspace runtime/e2e2 status

# 3. 因果链：send_id → write_send → write_verify → replies/round-001.md
python scripts/orchestrator.py --workspace runtime/e2e2 audit --trace
# → duplicate_sends=0 unconfirmed_sends=0 unexpected_write=0 lessons_pending=0
#   inconclusive=0  verdict=PASS  trace[0].verify_verdict=VERIFIED

# 4. 知识库体检
python scripts/lessons_audit.py
# → lessons: 10 条 状态={merged:9, open:1} ... verdict: PASS

# 5. 平台矩阵
python scripts/orchestrator.py matrix
```

真机那一轮的完整链路（`runtime/e2e2/runs/20261029T054159-e2e2/state.json`）：

```
INIT → BOUND → PLAN_OK → READY → DRY_RUN_OK → APPROVED → SENDING
     → AWAITING_REPLY（锚点判定）→ REPLY_VERIFIED（3187 字）→ ROUND_DONE → CLOSED
```

DeepSeek 的两轮裁定原文：
`runtime/deepseek-review/turn-002-reply.md`（第 1 轮评审，5548 字）、
`runtime/e2e2/runs/20261029T054159-e2e2/replies/round-001.md`（第 2 轮裁定，3187 字）。

---

## 六、现在怎么用（最短路径）

```bash
# 一次性体检（不碰浏览器）
python -m unittest discover -s tests
python scripts/lessons_audit.py
python scripts/orchestrator.py matrix

# 起一轮（DeepSeek 已实测，地址换成你自己的会话）
export WA_BROWSER=<instance_id>            # 多 profile 时必给；给一次会记进 state
python scripts/orchestrator.py init --goal "<目标>" --stage O5 --card docs/requirements/xxx.md
python scripts/orchestrator.py bind "https://chat.deepseek.com/a/chat/s/<uuid>"
python scripts/orchestrator.py plan --file <规划产物>
python scripts/orchestrator.py loop --once --file <本轮正文>
# → DRY_RUN_OK，然后 approve，再 loop --once 真发
python scripts/orchestrator.py approve --approval <id> --ttl 900
python scripts/orchestrator.py loop --once     # 真发 → 等回复 → 核实 → 落盘
python scripts/orchestrator.py audit --trace
python scripts/orchestrator.py close --stage O5 --require-lessons
```

`--card` 是硬的：卡片里「未明确」没清空、「有冲突」没裁定、「确认记录」没通过，
`init` 直接拒绝建 run。**闸门只拒绝，不生成、不补全、不建议**（硬规则 2 的工具级落法）。

---

## 七、没做的，和为什么（诚实清单）

| 没做 | 为什么 | 什么时候该做 |
|---|---|---|
| **Claude / Gemini / 豆包 实测** | 硬规则 1「全程只用一个对话地址」：不绑就不能探。改绑是架构师级决定，执行侧不发起 | 人明确改绑之后（`platforms.py` 加候选 → `probe` 回填 → 跑通一轮 → `verified=True`） |
| **ChatGPT 解封** | 同上；矩阵里记 `deferred_rule1`（纪律搁置，不是技术失败），lessons 里留 `open` + `blocking=false` | 人明确改绑后跑 `matrix --probe` |
| **`INCONCLUSIVE_COLLISION` 显式分支** | 判据是「有效增量 ≥1 但用户消息 +0」＝可能是别人在同一对话里发话。风险低（单账号单窗口），但**一旦误判会把「别人发的」当成「我发的」**，需要先把 user 侧锚点的可信度单独验证 | 已进 `VERDICT_EFFECT`（按无结论处理，不动 streak），但**暂不主动产出**这个结论 |
| **长连 soak（20 轮）** | 会烧掉大量平台额度与时间，且当前 12 轮预算的边界行为已被纯函数测试覆盖 | 下一轮候选 |
| **真机 vs 回放偏差对照** | 回放验的是「选择器命中」，不验「渲染时序」——这是 DeepSeek 第 2 轮明确点出的下一个风险 | 下一轮首要项 |
| **lessons 正文去重（语义级）** | 现在只按 `lesson_id` + 症状前缀去重，不做语义合并（宁可留两条，不误删一条） | 条目数过 30 条再考虑 |

---

## 八、文件变更清单

**新增**

| 文件 | 作用 |
|---|---|
| `scripts/confirm.py` | 发送/等待判定的唯一实现（锚点口径） |
| `scripts/statelock.py` | state 单写锁（`O_EXCL` + 死进程接管） |
| `scripts/requirement_gate.py` | 需求卡片否定式校验（只拒绝） |
| `scripts/lessons_audit.py` | 知识库体检（格式/重复/超龄 open/blocking） |
| `tests/domsel.py` | 迷你选择器引擎（回放用） |
| `tests/test_confirm.py` / `test_statelock.py` / `test_platforms_replay.py` / `test_orchestrator_gaps.py` / `test_lessons_audit.py` | 新增 60+ 条验收测试 |
| `tests/fixtures/{deepseek,kimi}/` | 真机 DOM 快照（选择器的事实基线） |
| `docs/requirements/2026-10-29-optimize.md` | 本项目自己的需求卡片（六节齐全，闸门 PASS） |
| `docs/OPTIMIZE-REPORT.md` | 本文件 |

**修改**

| 文件 | 改了什么 |
|---|---|
| `scripts/platforms.py` | BEM `--disabled` 识别；`edText()`（textarea 读 `value`）；消息指纹 + 消息 ID 锚点；`anchor_delta()`；textarea 写入分支；DeepSeek 升为 `verified=True` |
| `scripts/wa.py` | `save_state` 持锁写入；`unlock` 命令；`browser` 持久化；发送确认改调 `confirm` |
| `scripts/write_gate.py` | `wa_state_path()`（子进程 state 解析）；地址漂移守卫；`effective_delta` 判定；`--send-id`；事件补口径字段 |
| `scripts/orchestrator.py` | 迁移矩阵 + `repair_precondition`；`VERDICT_EFFECT` + `apply_verdict_effect`；锚点等待；`send_id` 因果链 + 自动 `verify`；`windows` / `unblock` / `card` / `audit --trace`；lessons 13 段；close 前体检 |
| `docs/orchestrator-contract.md` | **v1.0 → v1.1**（§2.4.1 锚点优先级、§2.5 三值语义、§2.2 修补边证据、§6 lessons 协议、O5 准出） |
| `state/contract.md` | 同步为 v1.1（编排器每步重读的就是它） |
| `references/lessons.md` | 新增 6 条（附录「O5 优化轮回流」5 条 + 闸门词表 1 条），现有 10 条 |
| `references/web-ai-platforms.md` | DeepSeek 升 A 档 + Step 5 实测事实表（含「虚拟列表的后果」） |
| `SKILL.md` / `README.md` | 命令面、平台状态、缺口表、单测数（41 → 112）、判据口径同步 |

---

## 九、一句话交底

> 这次优化的本质不是「加功能」，是**把「我觉得发出去了」换成「有证据证明发出去了」**，
> 并把每一处「靠自觉」改成「跑不过就红」。
> 骨架、纪律、边界一条没松——松的是我自己那些「差不多就行」的判断。

---

## 十、第二轮：接入 Gemini（第 4 个实测平台）并继续收敛

### 10.1 为什么这一轮不是「加一个平台配置」

Gemini 是第一个**受控富文本编辑器**（Quill）。它把前两轮没暴露的一整类问题掀开了：
**「工具说它成功了」和「事情真的发生了」是两件事**。这一轮一共撞到同一类错误的三个变种。

| # | 缺陷（全部真机撞到，不是推的） | 后果 | 修法 |
|---|---|---|---|
| 1 | **exec 写入静默失败**：`execCommand('insertText')` 让 `innerText` 读到 15 字，但 Quill 的 model 是空的（`ql-blank` 还在），发送键不渲染、点发送无反应 | 「看着写好了、其实没写」，然后对空气发一轮 | 新增 `native` 写入路径（真实键盘）；判据看**空态类 + 条件渲染的发送键 + 字数**，不看 `innerText` |
| 2 | **`bsk fill` 自校验误判**：它拿自己期望的值与页面严格比对，多行文本被编辑器重排后返回 `cdp_failed`——而 `editor_len=1574`、`can_send=true`，字早进去了 | 明明成功却报失败，白耗一轮预算 | `fill_native` 不看退出码：填完立刻 probe + `fill_check`，由**页面事实**定性；`rc` 存进 `note` 备查 |
| 3 | **`load` 事件永远不来**：Gemini 长轮询，`navigate --wait-until load` 30 秒超时返回非 0，页面却早已可用 | `bind` 这种纯本地动作被一个不会发生的事件卡死 | `navigate_soft`：load → domcontentloaded；仍失败由「地址对不对 + 编辑器在不在」定性 |
| 4 | **写完未发就停住时无路可走**：守卫正确地拒绝「脏输入框」再写，但没有清空工具 | 恢复路径断在「只能人工点浏览器」 | 新增 `wa.py clear`（Ctrl+A + Delete，清完复核空态） |
| 5 | **写权限两套**：闸门查 `write_allowed`，`wa.py send` 不查 | CLI 能绕过白名单写到不该写的地方 | 写权限同源，CLI 也查，退出码 8 |
| 6 | **lesson 字段夹换行**：失败详情里的多行 JSON 把「一条一行」撑成十几行 | 体检工具把续行当垃圾，格式坏得极其安静 | `_oneline()` + `_lesson_line()`，并加「生成的行能被体检工具读回」的往返测试 |

第 2、3 条是同一个道理：**事件的触发、工具的自我报告，都不是判据；页面事实才是。**
这条被写进了 SKILL 的缺口表，因为它是「以后还会再踩」的那类。

### 10.2 向 Gemini 索取评审（用项目自身能力）

同一套 `wa.py` + `platforms.py` + `bsk`，绑到一个真实的 10 轮 Gemini 对话，发了一段
「五条事实 + 五条裁定请求」的评审提纲（`runtime/g1-prompt.md`）。回复 719 字，
**有两条明确反对**——这比一致同意有价值：

| 裁定 | Gemini 的结论 | 我做了什么 |
|---|---|---|
| 1 空态类是否够稳 | 同意但提醒：类名是最脆的一环，应配合**发送键 disabled/是否存在** | 已满足（`fill_check` 第二、三见证）；并把「发送键」提为主体判据，类名降级为辅助——类名改名时退化为「字数+发送键」，仍然成立 |
| 2 发送键缺失即失败 | 完全认同（快速失败避免死循环与 Token 浪费） | 采纳，`FILL_NO_SEND` |
| 3 十六进制祖先 id 太宽松 | **反对**：会误命中路由哈希、组件 uid、layout 容器；应加**容器语义约束** | 采纳：新增平台事实 `anchor_box`（`div.conversation-container`），有它只认它的 id；泛化兜底改成「十六进制 **且** 类名像消息容器」 |
| 4 导航按「地址+编辑器」定性 | 不等于放宽守卫：SPA 天然无法依赖 load，地址与编辑器就绪即说明 DOM 已就绪 | 采纳，`navigate_soft` |
| 5 同容器流式补全会漏消息 | **必须加第三条判据**（长度单调增长 + 生成态转静止），否则必漏 | 采纳：`confirm.reply_grew` + `wait_for_reply` 的 `witness=growth`（绝对 ≥40 字 **且** ≥2%，且不在生成，且连续两次长度不变） |

Gemini 最后点名「最可能半年后反咬一口的是裁定 1」——理由正是**类名会被静默改版**。
这条被写成 lessons 里的 `trigger`，而不是一句感慨。

### 10.3 真机验收（`runtime/g1`，可复跑）

```
bind     → msg_count=10, 锚点 10 条（t:<16hex>），editor_blank=true
dry-run  → ap-10efa69c26a5a3d8
approve  → ttl 1200
send     → verdict=SENT  delta_source=anchor  effective_delta=1  signals=[needle]
           （发送前 reply_count=10 → 发送后 11，且我发的那段文字已出现在页面上）
wait     → REPLY_VERIFIED 719 字 → ROUND_DONE
audit    → duplicate_sends=0 unconfirmed_sends=0 unexpected_write=0 inconclusive=0
           lessons_pending=0  verdict=PASS
close    → CLOSED，8 条 lesson 候选全部回流
```

中途真出过一次事故并留证：第一次发送 `bsk fill` 返回 `cdp_failed` → 判 `fill_failed` →
`PAUSED_HUMAN`；随后 probe 证明 `editor_len=1574`、`can_send=true`，
即**字其实已经写进去了**。这就是上表第 2 条缺陷的现场。修复后重跑，一次成功。

### 10.4 数字

| 项 | 第一轮后 | 第二轮后 |
|---|---|---|
| 验收单测 | 112 | **140**（+28：`test_fill_guard` 20 条 + Gemini 回放 6 条 + lesson 格式 2 条） |
| 实测平台 | 3（ChatGPT / Kimi / DeepSeek） | **4（+Gemini）** |
| lessons | 10 条 | **17 条**（+7 条 Gemini 实测；体检 `verdict: PASS`） |
| 契约版本 | v1.1 | **v1.2** |
| 真机 DOM 夹具 | deepseek / kimi | **+gemini**（composer 13KB + messages 84KB） |

### 10.5 这一轮改动清单

| 文件 | 改了什么 |
|---|---|
| `scripts/platforms.py` | Gemini 升 `verified`；新增平台事实 `blank_class` / `send_requires_text` / `anchor_box` 与写入路径 `native`；`editor_blank` 上报；`normalize_probe`（口径归一，纯函数）；`fill_check`（三条见证）；`keyOf` 支持容器 id + 收紧十六进制兜底 |
| `scripts/wa.py` | `do_fill` / `fill_native`（写入唯一实现，按页面事实判定）；`clear_editor` + `wa.py clear`；`navigate_soft`；`read_state` 过 `normalize_probe`；CLI 补写路径白名单 |
| `scripts/write_gate.py` | 改调 `wa.do_fill`（删掉第二份写入实现）；`fill_not_accepted` 错误类 |
| `scripts/confirm.py` | `reply_grew`（流式补全见证）；`trim` 增加 `last_len` |
| `scripts/orchestrator.py` | `wait_for_reply` 三条见证 + `witness`；`last_len_before` 落盘；`_oneline`/`_lesson_line`；修正 `fill_failed` 的处置建议（原文对 Quill 是反的） |
| `docs/orchestrator-contract.md` | **v1.1 → v1.2**（§4.4 Gemini/native、§4.3 空态类优先、§2.4.1 anchor_box + growth、§5 `wa.py clear`、写权限同源） |
| `references/web-ai-platforms.md` | Gemini 升 A 档 + Step 5 实测事实表（Quill / 条件渲染发送键 / 轮次 id / 懒加载） |
| `references/lessons.md` | +7 条 Gemini 实测；删掉 1 条被取代且**建议是错的**自动条目（并在附录写明原因） |
| `tests/` | +`test_fill_guard.py`（20）、+Gemini 回放 6 条、+lesson 格式 2 条、`fixtures/gemini/` |
| `SKILL.md` / `README.md` | 平台 3→4、新增 `wa.py clear`、缺口表加「工具自证≠事实」、单测数 112→140 |

### 10.6 还没做（诚实清单）

- **Claude / 豆包** 仍未实测（硬规则 1：不绑不能探，改绑是人的决定）；
- **Gemini 懒加载**只做了「不跨会话比计数」的纪律约束，没做「落盘锚点快照再对账」的能力
  （lessons 里以 `open` 挂着，带判据与回退）；
- **`growth` 见证有误报面**：旧回复被展开/重排也会让长度变长。已用「不在生成 + 连续两次不变 + 记 `witness`」
  收窄，但没做「展开 vs 新回复」的语义区分；
- **bsk daemon 重启会丢会话**（本轮真撞到）：`ensure_session` 能自愈（重开会话 + 重导航），
  但没做「daemon 掉线」的显式探测与告警。

---

## 十一、第三轮：对话归属门禁（用户实测反馈驱动）

> 触发：用户指出「Gemini 刚刚对话有一个问题，是用了我之前的对话，不是项目单独的新对话」。
> 复盘：bind 直接绑定了浏览器里用户正在聊的那条 Gemini 对话（msg_count=10）——
> 项目的提问被插进用户自己的对话里，上下文是旧的、锚点基线全是别人的。

### 11.1 两个真缺陷

| # | 缺陷 | 根因 | 实测证据 |
|---|---|---|---|
| 1 | **bind 不判「这条对话归谁」**：只查地址形态，非空对话只打一行警告就放行 | 绑定判定缺「归属」维度，只有「形态」维度 | 事故 state：`url=/app/a8b8143806a8b296`、`msg_count=10`、`new_chat=false` |
| 2 | **「加载中的假空」骗过门禁**：重绑旧对话时首次 probe 读到 `msg_count=0`（懒加载页面主区还在转圈，截图为证）——若以 0 判「空对话」放行，门禁形同虚设 | 「工具此刻报的数 ≠ 事实」，与 load 事件、bsk 自校验、innerText 同源的**第四变种** | 截图 `conv-check-loaded-empty.png.bak`：侧栏 spinner + 主区空白，probe 全 0 |

### 11.2 修复（三层联动）

1. **`platforms.bind_verdict`**（纯函数，唯一口径）：判据 = `conv_id`（url_pattern 抽会话身份）+ `msg_count` + `conv_origin`（这条对话怎么进项目的：`new_chat`/`empty`/`adopted`）。首次绑到非空对话 → `foreign_conv` **默认拒绝**；`--reuse-existing` 显式豁免；state 缺 `conv_origin` 的老数据不许靠 url 相等自证清白。
2. **`wa.settle_msg_count`**：bind 前置读数稳定等待——读到 >0 立即过，否则连续 3 次读数相同才判（实测旧对话约 4–6 秒内出数）。
3. **Gemini 补 `new_chat_url=/app`**：home 在登录态下重定向回最近对话，等于没有新开。`bind --new` 落点必须「无会话 id + 0 条消息」，被重定向回已有对话直接拒绑；发送首轮跳转后落盘 `conv_id` + `conv_origin=new_chat`，此后换绑别的对话被 `conv_changed` 拦下。

配套：`wa.py bind --reuse-existing` / `orchestrator.py bind --reuse-existing`；`orchestrator bind --reuse` 校验 `conv_id`+`conv_origin` 齐全，缺一即拒；`probe` 输出 `conv_id` / `conv_mismatch`（对话身份对账点）。

### 11.3 真机验收（完整链，可复跑）

```
重绑旧对话（页面已加载） → msg_count=10 …（v1 state 已有 conv_id 落盘，暴露 resume 漏洞 → 补 conv_origin）
重置 state 后 bind --new  → bound=/app, msg_count=0, settle_waited_s=4.0, bind_code=new_chat
send（首轮）              → verdict=SENT, url 跳到 /app/015a53b81a2db8bf, delta=1
state 核对                → conv_id=/app/015a53b81a2db8bf, conv_origin=new_chat
wait                      → 回复 49 字「我完全理解当前是一个独立的全新上下文，与之前的任何对话无关」
```

### 11.4 数字变化

| 项 | 第二轮末 | 本轮末 |
|---|---|---|
| 验收单测 | 140 | **159**（+19：`test_bind_guard` 17 + resume/origin 语义 2 处改判） |
| lessons | 17 | **18**（+`L-GEMINI-FOREIGN-CONV-ff592c`；体检 PASS） |
| 契约版本 | v1.2 | **v1.3**（新增 §2.6 对话归属门禁） |

### 11.5 遗留说明

- 旧对话（`/app/a8b8143806a8b296`）里已被插入了之前测试的两轮消息，**工具无法代删**，需要用户自行清理（或忽略）。
- `settle_msg_count` 的稳定判据是「连续 3 次读数相同」，理论上存在「加载极慢且恰好稳定」的残余风险窗，但 3×2s 的观察窗已覆盖本次实测的全部加载时长。

---

## 十二、第四轮：新平台接入 SOP 固化 + 安全机制盘点（用户点名）

> 触发：用户确认四平台已实测可用，要求① 把「新平台如何分析对话、如何等待并第一时间获取内容」固化进 skill；② 问安全机制现状。

### 12.1 接入 SOP：四步 → 六步（`references/web-ai-platforms.md`）

新增两个此前只存在「经验里」、没写进文档的环节：

- **Step 2 · 页面侦察**：拿到陌生对话页后怎么分析——六类 DOM 事实全部用 `bsk evaluate` 的可复制片段拿（编辑器 / 发送键条件渲染与空态类 / 发已知文字定位用户消息容器 / 三层消息计数与轮次身份 / 滚动容器与虚拟化判定 / 生成中与完成标志标定），并把四轮实测的判别技巧写死（恒空容器、折叠区、气泡拆分等坑位）。
- **Step 5 · 等待 + 第一时间读取的信号标定**：before 快照 → 逐字段观察变化先后（容器先建/回复后到、URL 跳转、同容器流式 growth）→ 完成三见证（生成中消失 + 文本两次采样一致 + 完成标志）→ 三见证一满足立即 read 落盘，不加固定等待。

同步：平台定义模板补上 `write_allowed` / `new_chat_url` / `blank_class` / `send_requires_text` / `anchor_box` 五个本轮实战新增字段；probe 字段表补 `editor_blank` / `asst_keys`。

### 12.2 安全机制总览（SKILL.md §5.4，新增）

把散在代码里、从未汇总的 **14 项安全机制**制成一张表（机制 / 防什么 / 在哪 / 怎么验）：写闸门四步、写路径白名单、对话归属门禁、地址漂移守卫、发送确认三重判据、幂等拒重发、两次失败停 + fail_streak 唯一入口、state 写锁、弹窗不硬发、空输入不点空气、单窗口纪律、登录不代填凭证、敏感数据不进正文、审计 journal + lessons blocking。

### 12.3 内容安全补丁（本轮真缺口）

SKILL §3.1 新增：**发给网页版 AI 的正文会进平台服务器**——密钥、令牌、密码、客户敏感数据、未公开经营数据一律不进正文；必须引用时只给结构（字段名/占位符），数据由用户当面补或本地处理。

---

## 十三、第五轮：豆包平台实测（第 5 个 verified 平台）

> 按上一轮固化的六步 SOP 完整走通：侦察 → 回填 → probe → 信号标定 → 跑通。

### 13.1 豆包实测事实（全部真机）

| 维度 | 事实 | 与既有平台的差异 |
|---|---|---|
| 编辑器 | Tiptap ProseMirror | **exec 写入有效**（与 Gemini 的 Quill 相反）；**bsk fill 键盘模拟无效**（焦点被移走） |
| 发送 | **不能点按钮**（点击无反应且移走焦点）；唯一路径 = exec 写入 + 焦点不丢 + Enter | 全平台第一个 `send_method="enter"` |
| 消息节点 | 官方 data-testid 体系（`send_message`/`receive_message`/`union_message`） | 五平台中最规范 |
| 噪声 | 「推荐提问」渲染在回复节点**内部**；探针文字出现在侧栏标题 | 新增 `sel.read_ignore` 读时剔除（81→30 字） |
| 完成标志 | 无停止按钮；完成 = `message_action_regenerate` 出现（纯 icon 无 aria-label） | 新增 `regen_selector` 平台声明 |
| 锚点 | 无每条唯一 id → 正文指纹 | — |

### 13.2 架构改动（一处实现、同源分派）

- `platforms.py`：`send_method`（click/enter）、`regen_selector`、`sel.read_ignore` 三个新平台字段；`js_focus_editor` / `js_read_last` 剔除逻辑 / `js_probe` regen 哨兵。
- `wa.py`：`do_send` 唯一分派（CLI 与 write_gate 同源）；豆包全链路真机跑通（bind --new → msg_count=0 → send verdict=SENT → wait → read 1646 字回复完整）。
- lessons +3 条（`L-DOUBAO-ENTER-SEND-3c91f7` / `L-DOUBAO-NOISE-READ-b2e40a` / `L-DOUBAO-REGEN-ICON-0d7e42`），21 条体检 PASS。

### 13.3 数字

| 项 | 第四轮末 | 本轮末 |
|---|---|---|
| 实测平台 | 4 | **5（+豆包）** |
| 验收单测 | 159 | **165**（+6 豆包回放） |
| lessons | 18 | **21** |

---

## 十四、第六轮：INIT 权属翻转（用户实测反馈驱动）

> 触发：用户指出分镜任务中 agent 把原始需求深加工成结构化提示词后才发给 ChatGPT。
> 要求：从一开始就不深加工、只做整理；agent 与网页版 AI 协作完成项目；规划主要由网页版 AI 完成。

### 14.1 根因：设计缺陷，不是执行失误

旧 SKILL.md §2.4 与需求卡片模板**明文规定**「不要在需求没定型时去问网页版 AI」——INIT 全程把需求分析压在 agent 身上，与「网页版 AI 当架构师」的根本理念相反。

### 14.2 修复（权属翻转，五处联动）

1. **SKILL.md §2.2 重写**：需求分析由网页版 AI 主导，agent 只做三件事——**原样转交**（原始需求原话 + 最小事实包装并标注「agent 补充」+ 固定导语「请先列你必须向用户确认的问题，信息不足不要出方案」）、**原样传话**（网页版 AI 疑问 ↔ 用户回答，不改写不省略）、**留档**（transcript 落盘）。
2. **硬规则 2 扩充**：原始需求零加工转交。
3. **需求卡片改「摘录制」**：每条必须能在 transcript 找到原话出处；新增「原始往来记录」必填字段。
4. **requirement_gate 新增 `RAW_BRIEF_MISSING`**：无 transcript 不建 run（工具级强制）。
5. **出口双确认**：网页版 AI 说「信息已足够」+ 用户拍板，缺一继续往返。

### 14.3 数字

单测 165 全绿（测试样卡同步补字段）；lessons 22 条体检 PASS；本次属于契约外流程章节（SKILL/模板/闸门），不涉及 §2.4 指标口径。
