# orchestrator 契约（O0 基线冻结）· **v1.4**

> 本文件是**实现规格**，不是愿望清单。`scripts/orchestrator.py` / `scripts/write_gate.py` 必须逐条对上。
> 冻结日期：2026-10-29　·　来源：网页 AI（Kimi）第 1 轮规划回复 `runtime/wa/turn-001-reply.md`
> **O0 验收**：本阶段不改任何既有行为。`python scripts/wa.py --help` 必须原样通过；
> `scripts/platforms.py` 与 `scripts/wa.py` 除新增字段外不得改变既有选择器语义。

---

## 0.0 契约版本与冻结规则

**契约版本：v1.1（升级于 2026-10-29，依据 DeepSeek 架构评审第 1/2 轮裁定）**

**冻结范围**：§2.4 的指标定义与单写通过线（`reply_count` / `msg_count` /
`resend_attempts` / `effective_delta == 1`）、§2.4.1 锚点优先级、§2.5 的
`fail_streak` 三值语义、§2.2 的修补型迁移证据要求、§6 的 lessons 13 段格式。

**改动条件**：改上述任一定义必须**同时**满足四件事，**缺一即违规**——

1. 契约版本号 **bump**（v1.1 → v1.2 / v2.0），并在下方变更记录里写清改了什么、为什么；
2. 在 `references/lessons.md` **登记一条 lesson**（含稳定 `lesson_id`、`symptom`、`fix`、`verify_cmd`）；
3. 取得**架构师裁定**（网页 AI 或人）并把裁定原文落进 `runs/<id>/replies/`；
4. **单测同步更新**并全绿（`python -m unittest discover -s tests`）。

**未冻结范围**：Claude / 豆包的平台事实仍未实测（`verified=False`）；
ChatGPT 按硬规则 1 纪律搁置（`deferred_rule1`）——这些是**平台事实**问题，不是口径问题。

### 变更记录

| 版本 | 日期 | 变更 | 依据 |
|---|---|---|---|
| v0.1 | 2026-10-29 | O0 基线（边界、落盘约定、状态机、恢复、准出） | Kimi 第 1 轮规划 |
| v1.0 | 2026-10-29 | 新增 §2.4 指标定义（四个定义 + 通过线 + 气泡拆分判 INCONCLUSIVE）；§5.2 audit 增列 `inconclusive`；§6 收口 PASS 线加 `inconclusive=0`；§2.3/§6/§7 口径对齐 | Kimi 第 3/4 轮裁定 |
| **v1.1** | **2026-10-29** | ① §2.4 通过线由 `reply_count_delta == 1` 改为 **`effective_delta == 1`**，并新增 §2.4.1 锚点优先级（消息 ID > 正文指纹 > 计数）；② 新增 §2.5 `fail_streak` 三值语义与唯一写入口；③ §2.2 增列修补型迁移的 **actor + 证据**双要求（`PAUSED_HUMAN→AWAITING_REPLY` 必须有 `send_id` 且 verdict 为 INCONCLUSIVE*）；④ §5.2 audit 增 `--trace`（因果链）；⑤ §6 lessons 升 **13 段**（+`blocking`），open 超 14 天 WARN、`blocking=true` 阻塞收口；⑥ §4.4 新增 DeepSeek 实测写入路径（textarea） | DeepSeek 评审 F1–F4 / S1–S8 + 第 2 轮裁定 1/2/3；端到端实测缺陷（等待侧计数口径） |


| **v1.2** | **2026-10-29** | ① §4.4 新增 **Gemini** 与 `native` 写入路径（Quill 拒绝 exec）；② §4.3 新增 `fill_not_accepted` 与「空态类优先于文本长度」（`editor_len` 口径归零规则）；③ §2.4.1 锚点级别 1 前置 `anchor_box`（平台声明），泛化兜底改为**组合约束**；④ §2.4.1 新增等待侧第三条见证 **`growth`**（同容器流式补全），`witness` 必记；⑤ §5 新增 `wa.py clear`；⑥ 写权限同源（CLI 也查 `write_allowed`） | Gemini 实测（`runtime/g1`）+ Gemini 第 3/5 条裁定 |

| **v1.3** | **2026-10-29** | ① **§2.6 对话归属门禁**：bind 判「这条对话归谁」（`conv_id` + `msg_count` + `conv_origin` 三元），首次绑到非空对话默认拒绝（`foreign_conv`），`--reuse-existing` 显式豁免；② bind 前置**读数稳定等待**（懒加载平台的 `msg_count=0` 可能是「加载中的假空」，连续 3 次相同才判）；③ `new_chat` 落点必须「无会话 id + 0 条消息」，入口被重定向回已有对话直接拒绑；④ 发送首轮跳转后落盘 `conv_id` + `conv_origin=new_chat`；⑤ `bind --reuse` 校验 conv_id 与 conv_origin 齐全，缺一即拒；⑥ gemini 补 `new_chat_url=/app`（home 会重定向回最近对话） | 用户实测反馈：项目被绑到用户正在聊的 Gemini 对话（`L-GEMINI-FOREIGN-CONV-ff592c`） |

| **v1.4** | **2026-10-30** | ① **§2.7 首次人工确认门与执行模式**：INIT 出口扩为三条件（+执行模式），卡片「执行模式」字段机器校验（`EXEC_MODE_MISSING` / `EXEC_MODE_INVALID`，词根二选一 fail-closed）；直接执行模式下仍必须停的三条例外；**三轮审查法则**（同性质审查第 3 轮必须当面问网页 AI 是否继续）；② **§2.8 平台占用锁**：`wa.py with_platform_lock` 装饰 bind/send/clear，锁文件 `~/.starpilot/locks/lock-<平台>-<key>.json`（跨项目可见），被占 rc=9 报持有者、不排队死等，语义与 state 单写锁同源（死进程/超时/坏锁接管），`wa.py plock` 查看/强拆，`STARPILOT_LOCK_KEY` 按浏览器 profile 隔离 | 用户实测反馈：① agent 每步强等人工确认（未裁定过执行模式）；② 两任务共用 ChatGPT 抢同一 tab、触发风控（回复加载不出、误判对话异常自动跳转） |

### v1.0 冻结证据（历史，保留备查）

> v1.1 的改动由 DeepSeek 第 2 轮裁定 + 端到端实测缺陷驱动，
> 裁定原文见 `runtime/e2e2/runs/20261029T054159-e2e2/replies/round-001.md`，
> 实测证据见同 run 的 `writes/*.result.json`（`effective_delta=1` 而 `reply_count_delta=0`）。
> lessons 已按 §0.0 第 2 条登记（`L-DEEPSE-REPLY-TIMEOUT-a8ab2e` 等 5 条）。


| 文件 | before（本轮改动前） | after（冻结版） |
|---|---|---|
| `README.md` | `2324d4fa90fdb658` | `13619354d2079864` |
| `SKILL.md` | `97c228f215da6aec` | `eed835e62253a9a9` |

改前哈希的出处：`README.md` 由本轮 5 处编辑**反向重建**（反向重放后与当前文件的差异恰为这 5 处，
且与 IDE 记录的更早基线相比，差集恰为上一轮会话的改动）；`SKILL.md` 由 IDE 的 file-changes
diff **反向重放**得到，正向重放与当前文件逐字节相同。

---

## 0. 边界（来自「不做做什么」清单）

| 不做 | 含义 |
|---|---|
| 不常驻服务化 | orchestrator **按需拉起、单次循环**（`loop --once`），不是 daemon |
| 不引第三方库 | 只用 Python 3.9+ 标准库 |
| 不做截图判断 | 状态判定一律 DOM / 元素属性 |
| 不 push / 删文件 / 动凭证 | 代码里不得调用 git push、不得 `os.remove` 用户文件、不得读凭证 |
| 不顺手重构 archive | `archive/` 只读参考 |
| 不批量「修」未验证平台 | 未实测平台只跑**只读**探测，不改 `platforms.py` 的 `sel` |
| 不把未实测平台标为 supported | `verified` 只在实测通过后置 True |
| **不新增第二个网页 AI 对话地址** | 全程只认 state 里那一个已绑定 URL；写闸门测试也发在**这个**对话上 |

---

## 1. 落盘约定

```
<workspace>/
├─ state/
│  ├─ current              # 纯文本：当前 run_id（指针，不放状态）
│  ├─ current_goal.md      # 目标、预算、成功判据（每轮重读）
│  └─ contract.md          # 已冻结的契约（每轮重读）
├─ stage/
│  └─ <stage_id>/handoff.md # 该阶段的交接说明（每轮重读）
└─ runs/
   └─ <run_id>/             # run_id = <UTC 时间>-<slug>，如 20261029T120000-orchestrator
      ├─ state.json         # 编排状态机快照
      ├─ journal.jsonl      # append-only 事件流（一行一个 JSON）
      ├─ evidence/          # <round>-<action>-<sha8>.json
      └─ writes/            # <approval_id>.dry.json / <approval_id>.result.json
```

- **写入原子性**：一律 `tmp 文件 → flush → os.fsync → os.replace`；`journal.jsonl` 追加后 `flush + fsync`。
- **每轮重读**：轮次开始必须重新读 `state/current_goal.md`、`state/contract.md`、`stage/<stage_id>/handoff.md`，
  **绝不假设上一轮还在平台上下文里**（平台会忘，见 `references/web-ai-platforms.md`）。
- `state/current` 只存指针，状态真身在 `runs/<run_id>/state.json`。

### 1.1 审计键（每条 journal 事件 / 每份 evidence 都有）

```
run_id, round_id, target_url_sha256, prompt_sha256,
action, before, after, result, error_class
```

`target_url_sha256` = `sha256(url)` 前 16 位；`prompt_sha256` = `sha256(正文)` 前 16 位。

---

## 2. 状态机

### 2.1 状态集合

```
INIT → BOUND → PLAN_OK → READY → DRY_RUN_OK → APPROVED
      → SENDING → AWAITING_REPLY → REPLY_VERIFIED → ROUND_DONE
任意状态 → PAUSED_HUMAN | RECOVERING | HANDOFF | CLOSED
```

### 2.2 合法迁移与守卫（**守卫不过 = 拒绝迁移，不许硬闯**）

正向链的守卫：

| 迁移 | 守卫 |
|---|---|
| `INIT → BOUND` | state 里有已绑定 URL；`sha256(url)` 与 `target_url_sha256` 一致 |
| `BOUND → PLAN_OK` | 规划产物已落盘（`stage/<stage_id>/handoff.md` 或 `runs/<id>/plan.md` 存在且非空） |
| `PLAN_OK → READY` | DOM probe 返回 `editable=true`（编辑器存在、可写、无弹窗遮罩）**且** URL hash 等于绑定值 |
| `READY → DRY_RUN_OK` | `write_gate plan` 产出 `<approval_id>.dry.json`，前置校验全过 |
| `DRY_RUN_OK → APPROVED` | 存在 `approval_id`、**未过期**、目标与 dry-run 记录**一致**（URL hash + prompt hash 都要对） |
| `APPROVED → SENDING` | **先**记录发送前锚点快照（`reply_count_before` / `asst_fps_before` / `asst_keys_before`），再允许发送（禁止先发后补记录） |
| `SENDING → AWAITING_REPLY` | 发送前快照已落盘，且 `verdict != NOT_SENT` |
| `AWAITING_REPLY → REPLY_VERIFIED` | **锚点口径**判「来了新回复」（新锚点 ≥1 或末条助手锚点变化）**且** 无「停止生成」信号、末条正文长度连续两次一致 |
| `AWAITING_REPLY → ROUND_DONE` | 回复原文已落盘 **且** 本轮反馈已生成 |
| `REPLY_VERIFIED → ROUND_DONE` | 回复落盘后 **必须**跑一次 `write_gate verify`（因果链第三环，见 §5.2 `--trace`） |
| `ROUND_DONE → READY` | 预算未耗尽（下一轮） |
| 任意 → `PAUSED_HUMAN` | 命中人工卡点，或恢复证据不足 |
| 任意 → `HANDOFF` | 同目标连续失败 2 次，或预算耗尽。**只进 HANDOFF，不许自动换平台/换选择器** |
| `HANDOFF → CLOSED` | `close --stage <id> --require-lessons` 通过 |

**修补型迁移（把状态往回推）必须同时满足「actor 合法」和「证据成立」两条**；
只有 actor 合法不算通过——身份挡不住「诱导人点」，证据挡得住（第 2 轮裁定 1）。

| 修补型迁移 | 允许的 actor | 前置证据 |
|---|---|---|
| `PAUSED_HUMAN → READY` | `human` / `recovery` | 无（人确认环境已修复） |
| `PAUSED_HUMAN → AWAITING_REPLY` | `human` / `recovery` | **必须有 `last_send.send_id`**，且 `last_send.verdict ∈ {INCONCLUSIVE_NOT_SUCCESS, INCONCLUSIVE_COLLISION}`。已判清（如 `SENT`）却没等到回复 = 真失败，该走 `HANDOFF` 或重发，**不许原地继续等** |
| `PAUSED_HUMAN → HANDOFF` | `human` / `agent` | 无 |
| `PAUSED_HUMAN → CLOSED` | `human` | 无 |
| `RECOVERING → READY / AWAITING_REPLY / REPLY_VERIFIED / PAUSED_HUMAN / HANDOFF` | `recovery` | 见 §3.2（恢复只复核、不重发） |

`agent` 不被允许走 `PAUSED_HUMAN → AWAITING_REPLY` / `→ READY`：
否则 agent 可以自己绕过写闸门把状态往前推，「两次失败停」形同虚设。

**不合法迁移一律拒绝并记 journal（`result=transition_rejected`），不改状态。**

### 2.3 发送成功信号口径（网页 AI 第 2 轮裁定）

| 信号 | 强度 | 用途 |
|---|---|---|
| 消息数增加 | **主** | 可判成功 |
| 地址变了 | **主** | 可判成功 |
| 正文指纹命中（`js_has_text`） | **主** | 可判成功 |
| 输入框变空 | **弱** | **只能作弱信号** |

三选一主信号都不满足、但输入框已清空 → 必须输出 **`INCONCLUSIVE_NOT_SUCCESS`**：
**既不报成功，也不报失败**，转人工复核页面。
（实测依据：Kimi 限流时会清空输入框却拒绝建会话——见 `references/lessons.md` L-28。）

`reply_count_delta` 的**口径与判定**见 §2.4（指标定义）——
一句话：**它数的是「助手回复条数」，通过线是正好 1**；
用消息总数会把「我这条 + 平台那条」算成 2，那是口径错，不是重发。

---

## 2.4 指标定义（metrics）

> **收口硬性前置**：指标定义必须写在文档里，不能只活在代码里——代码一改，文档就漂。
> 本节由网页 AI 第 3/4 轮裁定冻结，代码与本节不一致时以本节为准并修代码。

| 指标 | 定义 | 出处 |
|---|---|---|
| `reply_count` | **助手消息气泡数**（平台无关口径，DOM 计数 `asst.length`） | `platforms.js_probe` |
| `msg_count` | **平台原始计数**（Kimi=消息总数；ChatGPT=用户消息数）。**平台相关，不可当判据** | `platforms.js_probe` |
| `reply_count_delta` | `after.reply_count − before.reply_count`（**仅审计参考**，虚拟列表下会失真） | `write_gate.execute_send` |
| `effective_delta` | **判定实际采用的增量**：锚点可用时取锚点增量，否则退回 `reply_count_delta` | `confirm.classify_send_result` |
| `delta_source` | `anchor`（锚点口径）/ `count`（计数口径，已降级） | `confirm.classify_send_result` |
| `count_anchor_disagree` | 两种口径结论不一致（**出现即说明计数不可信**，必须记进 journal） | `confirm.classify_send_result` |
| `user_msg_delta` | 用户消息条数增量（DOM 口径），用来区分「气泡拆分」与「真重发」 | `platforms.js_probe` |
| `resend_attempts` | **严格晚于 `resume_marker` 的 send 数**（标记之前的合法发送不算） | `orchestrator.do_resume` |
| `sends_after_resume` | 同口径的审计复算；**没有 resume 标记就记 0**（没恢复过就不该判失败） | `orchestrator.cmd_audit` |

**单写通过线：`effective_delta == 1`（正好 1）。**
`reply_count_delta` 不再是判据——它是「渲染出几个气泡」，不是「来了几条回复」。

### 2.4.1 锚点优先级（对虚拟列表免疫）

虚拟列表（DeepSeek `div.ds-virtual-list`）只渲染视口附近的消息，节点卸载/重挂会让
「当前气泡数」忽大忽小。因此增量必须由**消息身份**推导，优先级从硬到软：

| 级别 | 锚点 | 来源 | 可靠性 |
|---|---|---|---|
| 1 | `asst_keys` / `msg_keys` | 平台消息 ID：`anchor_box` 容器的 `id` → `data-virtual-list-item-key` → `data-message-id`/`data-id` → 祖先纯十六进制 id（**且**类名像消息容器） → `data-index` | **消息身份**：正文折叠、markdown 重排、两条回复正文相同都不受影响 |
| 2 | `asst_fps` / `user_fps` | 正文去 markdown 修饰符后前 64 字 | 可用，但同话题连续两轮可能撞指纹 |
| 3 | `reply_count` | DOM 气泡计数 | **仅兜底**，虚拟列表下会算出 0 或负数 |

判定规则（`platforms.anchor_delta`）：

- 两侧都能取到级别 1 → `anchor_kind="key"`，用**集合差**算 `new_asst`；
- 否则两侧都有级别 2 → `anchor_kind="fp"`，同样用集合差；
- 都取不到 → `anchors_available=false`，退回计数口径并把 `delta_source` 标成 `count`；
- `last_asst_changed`（末条助手锚点变化）是**强判据**：只要它变了，就说明来了新回复，
  与计数增减无关。

**锚点必须是「平台事实」，不是「长得像 id」。** `anchor_box` 是平台显式声明的轮次容器
（Gemini：`div.conversation-container`），有它时只认它的 `id`；泛化兜底必须**组合约束**——
纯十六进制（≥8 位、无横线）**且**该祖先类名匹配 `message|conversation|turn|chat|response`。
只凭十六进制会命中路由哈希与组件 uid（Gemini 第 3 条裁定：原规则过于宽松）。

**等待侧的第三条见证 `growth`**：锚点判的是「有没有**新的**消息」。若平台把回复流式写进
**已有**容器（Gemini 一类 SPA），集合差恒为 0、末条锚点也不变——这是锚点口径唯一的盲区。
此时以「末条正文显著变长（绝对 ≥40 字 **且** ≥2%）+ 不在生成 + 连续两次长度不变」为证，
返回值记 `witness=growth`，事后可分辨这一轮凭什么判到的（Gemini 第 5 条裁定）。
三条见证 `anchor` / `count` / `growth` 三选一，`witness` 必记，不许只记结论。

> 实测教训（2026-10-29）：发送前 `reply_count=2`，回复 2597 字渲染完成后仍为 2，
> 计数口径判 `reply_timeout` 白等 300 秒；锚点口径当场判到达。
> 同一次发送里 `msg_count_delta=-1`、`reply_count_delta=0`、`effective_delta=1`
> ——计数口径全线失真，锚点口径唯一可用。

判定分支（`write_gate verify`）：

| 情形 | 判定 | 退出码 |
|---|---|---|
| `effective_delta == 1` | `VERIFIED` | 0 |
| `effective_delta == 0` | `MISMATCH → PAUSED_HUMAN`（送达了但没回） | 5 |
| `effective_delta > 1` 且 `user_msg_delta >= 2` | 实锤重发 → `MISMATCH → PAUSED_HUMAN` | 5 |
| `effective_delta > 1` 且 `user_msg_delta == 1` | 一条逻辑回复被拆成 N 个气泡 → **`INCONCLUSIVE`，不自动 MISMATCH** | 6 |
| `effective_delta > 1` 且 `user_msg_delta == 0` 或缺失 | 用户消息数不可信（选择器失效 / 消息消失），无法区分气泡拆分与重发 → **`INCONCLUSIVE`**，不自动 MISMATCH | 6 |

**实锤重发的唯一条件：`user_msg_delta >= 2`**（我发出了两条以上）。
`user_msg_delta` 为 1 或 0 时**都不许判失败**——1 是「一条逻辑回复被拆成气泡」，
0 是「用户消息数不可信」，两者都是「无法判断」，不是「失败」。

`INCONCLUSIVE` 时闸门**必须**记下 `reply_bubbles`（气泡数）与 `logical_replies`（逻辑回复数）交人工复核，
不许用「看起来像重发」直接判失败。

弱信号纪律（沿用 §2.3）：「输入框变空」只能作弱信号；
三选一主信号都不满足时输出 `INCONCLUSIVE_NOT_SUCCESS`（对应 `references/lessons.md` L-28）。

---

## 2.5 `fail_streak` 三值语义（第 2 轮裁定 2）

`fail_streak` = **同一目标上「连续『有结论的失败』」的次数**。它只有一条口径：

| 结论 | 对 `fail_streak` 的作用 | 典型 verdict |
|---|---|---|
| **有结论的失败** | **+1** | `NOT_SENT`、`reply_timeout`、`write_len_mismatch`、`guard_failed`、`send_unconfirmed` |
| **有结论的成功** | **清零**（打断「连续」） | `SENT`、`SENT_NO_REPLY`、`SENT_MULTI` |
| **无结论** | **不动** | `INCONCLUSIVE_NOT_SUCCESS`、`INCONCLUSIVE_COLLISION`、崩溃恢复 |

- 「恢复不改 `fail_streak`」与「成功清零」**不冲突**：恢复是「无结论」，成功是「有结论」，
  两者都不是「有结论的失败」；但只有成功能打断「连续」。
- **唯一写入口**：`orchestrator.apply_verdict_effect(st, verdict)`。别处不许 `+= 1` / `= 0`；
  源码守卫测试（`TestVerdictEffect.test_no_naked_fail_streak_writes`）会抓裸写。
- 映射表 `VERDICT_EFFECT` 是**唯一归类点**：新增任何 verdict 必须在这里落一行，
  否则查不到 → 按失败处理（宁可停，不可静默放过）。
- 不许给 `fail_streak` 加权重、拆半次：那会让硬规则 4「失败两次停」变得不可解释。

---

## 2.6 对话归属门禁（v1.3 新增）

**背景**：bind 曾直接吃下浏览器里用户正在聊的对话（msg_count=10）——上下文是旧的、
发送会污染用户对话、锚点基线错位。且懒加载平台打开旧对话时消息区先空转，
probe 读到的 `msg_count=0` 是「加载中的假空」（「工具自证≠事实」第四变种）。

**规则**（唯一实现在 `platforms.bind_verdict`，纯函数，离线可测）：

1. 判据三元组：`conv_id`（平台 `url_pattern` 抽出的会话身份）、`msg_count`（它是不是空的）、
   `conv_origin`（这条对话当初怎么进项目的：`new_chat` / `empty` / `adopted`）。
2. `--new`：落点必须「无会话 id + 0 条消息」。入口被重定向回已有对话 = `new_chat_redirected`，拒绑。
3. 首次绑到非空对话 = `foreign_conv`，**默认拒绝**；`--reuse-existing` 显式豁免并留 warn。
4. state 缺 `conv_origin` 的老数据不许靠 url 相等自证清白——回落到「首次绑定」规则重审。
5. bind 前置读数稳定等待（`wa.settle_msg_count`：读到 >0 立即过；否则连续 3 次读数相同才判）。
6. 发送首轮跳转后落盘 `conv_id` + `conv_origin=new_chat`——此后换绑别的对话会被 `conv_changed` 拦下。

**验收**：`python -m unittest tests.test_bind_guard`；真机链
`bind --new → msg_count=0 → send → conv_origin=new_chat → 回复确认全新上下文`。

---

## 2.7 首次人工确认门与执行模式（v1.4 新增）

**背景**：部分 agent 与网页 AI 沟通结束后「强行等人工确认」——既不是用户要求的，
也没问过用户该不该等。「何时找人工」必须是用户的一次性裁定，不是 agent 的习惯。

**规则**（文档纪律 + 机器校验双层）：

1. INIT 往返出口从两条件扩为**三条件**：网页 AI 说信息足够 + 用户拍板方向 +
   **执行模式已确认**（逐步确认 / 直接执行，二选一，记用户原话）。
2. 机器校验：需求卡片基本信息表「执行模式」字段必填，且只认词根
   `逐步确认` / `直接执行`（`requirement_gate.py`，code=`EXEC_MODE_MISSING` /
   `EXEC_MODE_INVALID`）。缺失或写法不认 → 拒进 LOOP（fail-closed）。
3. **直接执行**模式下 agent 仍必须停的三条例外（阻塞，非确认）：连续失败 2 次
   （硬规则 4）；URL/conv 漂移（硬规则 1）；新歧义或冲突需用户取舍（硬规则 2）。
4. 定了模式后 agent 不得反向违规：直接执行模式不得擅自加确认点；逐步确认模式
   不得跳过确认。
5. **三轮审查法则**（SKILL §4.7）：连续 3 轮同性质审查指令（目标/判据同、无新增
   发现），agent 执行第 3 轮后必须在反馈里直接问网页 AI「是否有必要继续」，不得
   无限陪跑；继续则新判据清零重算。

---

## 2.8 平台占用锁（v1.4 新增）

**背景**：两个任务同时用星舵、且都打同一个平台（同账号同浏览器）→ 抢同一个
tab、对话打架、触发平台风控（2026-10-29 实测：ChatGPT 回复加载不出，另一任务
误判对话异常自动跳转）。state 单写锁（§ statelock）只防「同一项目两个 run」，
防不了「两个项目共抢一个平台」。

**规则**（唯一实现在 `wa.py with_platform_lock`，装饰 `bind`/`send`/`clear`）：

1. 锁文件：`~/.starpilot/locks/lock-<platform>-<key>.json`（用户级目录，跨项目可见；
   `STARPILOT_LOCK_DIR` 可改位置）。`key` 默认 `default`，不同浏览器 profile/账号
   设 `STARPILOT_LOCK_KEY` 隔离。
2. 拿不到锁 → 命令立即退出 rc=9，stderr 打印持有者（pid / run_id / 持有时长）。
   **不排队死等**（`STARPILOT_LOCK_WAIT` 可改为等待 N 秒，默认 0）。
3. 锁语义与 state 单写锁同源（`statelock.acquire_named`）：O_EXCL 创建 +
   死进程接管 + 超 30 分钟（`PLATFORM_LOCK_STALE`）接管 + 坏锁文件接管。
4. 只读命令（probe / wait / read / state）不取锁：读不产生导航，不打架。
5. 查看与强拆：`wa.py plock`（全量/按平台）、`wa.py plock --release --platform <key>`
   （pid 校验保护，别人的活锁拆不动；死锁可直接拆）。
6. 平台解析失败的命令不拦（让原命令自己报「未绑定/平台名错」），锁只在
   平台可解析后才生效。

**验收**：`python -m unittest tests.test_platformlock`；并发实测：
任务 A 持锁期间任务 B `send` → rc=9 且 stderr 含 A 的 run_id。

---

## 3. 恢复、预算、人工卡点、幂等

### 3.1 预算（默认值，可被 `state/current_goal.md` 覆盖）

| 项 | 默认 | 触顶行为 |
|---|---|---|
| `max_rounds` | 12 | → `HANDOFF` |
| `max_same_error` | 2 | 同目标连续失败 2 次 → `HANDOFF` |
| `round_timeout` | 300s | 本轮超时 → 记 `error_class=round_timeout` |
| `send_timeout` | 60s | 发送后 60s 内没确认 → `send_status=sent_unconfirmed` |

**口径**：`max_rounds` 数的是**实际发生的发送轮次**（`rounds_used` 在 `APPROVED → SENDING`
时 +1，失败也消耗，不返还）；恢复不返还预算（恢复是复核，不是重来）。
预算判定只有一个点：`orchestrator.budget_gate(st)`，别处不许另算。

### 3.2 崩溃恢复（`loop --resume`）

1. 读 `state.json` + `journal.jsonl`（journal 是真相，state 是快照）。
2. 状态在 `SENDING` 且 `send_status=sent_unconfirmed` → **只复核 DOM，不重发**：
   - `reply_count >= reply_count_before + 1` → 判定已送达，补进 `AWAITING_REPLY`；
   - 否则 → `PAUSED_HUMAN`（证据不足，不猜）。
3. 状态在 `AWAITING_REPLY` → 重新探测等回复，不重发。
4. 恢复动作必须产出 `runs/<id>/evidence/recovery-<n>.json`。
5. **恢复不改变 `fail_streak`**：崩溃既不算失败也不算成功（无结论），
   清零它会绕过「两次失败停」，加一它会冤枉一次崩溃。见 §2.5。

### 3.3 人工卡点（四处，命中即 `PAUSED_HUMAN`，等人）

1. **绑定后首跑前** —— 人工确认目标与对话地址。
2. **首次真实写前** —— `DRY_RUN_OK → APPROVED` 这步必须有人批。
3. **恢复不确定时** —— `sent_unconfirmed` 且 DOM 复核不出结论。
4. **两次失败 / 预算耗尽时** —— 转 `HANDOFF`，等人接手。

### 3.4 幂等

- 发送前查 `last_send` 的 **URL hash + prompt hash 双哈希**；命中同指纹 → 输出 `SKIP_DUPLICATE_SEND=1` 并退出，**不重发**。
- 发送后必须 `reply_count_delta >= 1` 才算完成。

---

## 4. 写闸门（`scripts/write_gate.py`）

### 4.1 模式

`STARPILOT_WRITE_MODE`：`dry_run`（**缺省必须是 dry_run**）| `single_write`。
真实写发生当且仅当：模式 = `single_write` **且** 存在有效 `approval_id`。

### 4.2 四步

| 步 | 命令 | 产物 |
|---|---|---|
| 计划 | `write_gate.py plan --file <正文> --stage <id>` | `writes/<approval_id>.dry.json`：目标 URL hash、输入 hash、选择器、写入前后 DOM hash、**将要执行的动作**（**不发送**） |
| 批准 | `write_gate.py approve --approval <id> --ttl 600` | 批准记录（含过期时间戳） |
| 单写 | `write_gate.py send --approval <id>` | `writes/<approval_id>.result.json`：`reply_count_before/after`、实际 DOM hash |
| 复核 | `write_gate.py verify --approval <id> --send-id <send_id>` | 比对 dry 预期 vs 实际；不一致 → `PAUSED_HUMAN`，**不得自动继续** |

**复核不是可选项**：`REPLY_VERIFIED → ROUND_DONE` 必须自动跑一次 `verify`，
否则 journal 里只有 `write_send` 没有 `write_verify`，`audit --trace` 会显示「(未核验)」。
`send_id` 贯穿 `write_send → write_verify → replies/round-N.md` 三段，是因果链的主键。

### 4.3 阻断码（前置校验失败一律 `WRITE_BLOCKED_*`，且**不产生** result.json）

`WRITE_BLOCKED_MODE_DRY_RUN`（默认模式）、`WRITE_BLOCKED_NO_APPROVAL`（无/过期批准）、
`WRITE_BLOCKED_TARGET_MISMATCH`（目标与 dry 不一致）、`WRITE_BLOCKED_PLATFORM_UNVERIFIED`（平台未实测）、
`WRITE_BLOCKED_WRITE_METHOD`（写入路径与该平台已实测路径不符）、`WRITE_BLOCKED_GUARD`（DOM 守卫不过）。

**新增（v1.2）**：`fill_not_accepted`（字进了 DOM 但编辑器 model 是空：空态类仍在 /
条件渲染的发送键不出现）。它不是阻断码而是**发送前的失败类**——判定在点发送之前，
不点空气、不白耗一轮预算。

**空态类优先于文本长度**：`editor_len` 的口径是「编辑器 model 里有多少内容」，
不是「DOM 里有多少字」。平台声明了 `blank_class`（Gemini：`ql-blank`）时，
该类存在即 `editor_len=0`，与 `innerText` 无关——`execCommand('insertText')` 能让
DOM 有字而 model 为空（见 `references/lessons.md` L-GEMINI-FILL-NOT-ACCEPTED）。
`fill_check` 三条见证缺一不认：字数够 / 空态类消失 / 条件渲染的发送键出现。

### 4.4 平台写入路径约束

| 平台 | 允许的写入路径 | 来源 |
|---|---|---|
| Kimi | `exec`（Range 圈定 + `execCommand('insertText')`） | 2026-10-29 实测；`paste` 是追加、`delete` 被忽略 |
| DeepSeek | `exec`（**textarea 专用分支**：`el.select()` + `insertText`，兜底原生 setter + `input` 事件） | 2026-10-29 实测；编辑器是 `textarea`，`innerText` 恒空，必须读 `value` |
| ChatGPT | `exec`（`execCommand`） | 已实测 |
| **Gemini** | **`native`（真实键盘 / bsk fill）** | 2026-10-29 实测；编辑器是 Quill（`div.ql-editor`），**`exec` 会静默失败**：字进 DOM 不进 model，`ql-blank` 还在、发送键不渲染 |
| 其他 | **不允许写**（`verified=False`） | 未实测 |

**写权限同源**：`write_allowed` 是唯一白名单，闸门（`write_gate`）与 CLI（`wa.py send`）
都查它。此前只有闸门查，`wa.py send` 可以绕过——v1.2 补齐，退出码 8。

---

## 5. 命令面

```
python scripts/orchestrator.py init --goal <目标> [--stage <id>] [--max-rounds N] [--card <需求卡片>]
python scripts/orchestrator.py bind --url <对话地址> | --new | --reuse | --reuse-existing
python scripts/orchestrator.py plan --file <规划产物>
python scripts/orchestrator.py loop --once [--run <run_id>] [--file <正文>]
python scripts/orchestrator.py loop --round
python scripts/orchestrator.py loop --resume
python scripts/orchestrator.py approve --approval <id> [--ttl 600]
python scripts/orchestrator.py status
python scripts/orchestrator.py audit --run <run_id> [--trace]
python scripts/orchestrator.py recover
python scripts/orchestrator.py matrix [--probe]
python scripts/orchestrator.py close --stage <id> --require-lessons
python scripts/orchestrator.py lesson --symptom <症状> --fix <处置> --verify-cmd <命令> [--status open] [--blocking]
python scripts/orchestrator.py windows            # 硬规则 6 运行时断言：同实例会话数
python scripts/orchestrator.py unblock --to <状态> --reason <为什么能继续>
python scripts/orchestrator.py card --file <需求卡片>   # 否定式校验（只拒绝，不生成）
python scripts/wa.py unlock --state <.wa-state.json>    # 清残留的 state 写锁（崩溃恢复）
python scripts/wa.py clear --state <.wa-state.json>     # 清空输入框（不发送）：给「写完未发」的恢复态
python scripts/lessons_audit.py [--strict] [--max-age-days 14]  # 知识库体检
```

`bind --reuse`：复用 `wa` state 里**已绑定的对话**，不重新 bind、不新增地址——
这是「同一对话」纪律在工具层的落法。**前置条件**：state 里 `conv_id` 与 `conv_origin`
齐全（缺一 = 门禁上线前的老数据或他人写入，拒绑并提示重绑）。

`init` 带 `--card` 时**先过需求闸门**（`requirement_gate.validate`）：卡片不通过就直接拒绝建 run——
硬规则 2「不预估方向」的工具级落法。闸门只做否定式校验（拒绝），不生成、不补全、不建议。

`bind --reuse`：复用 `wa` state 里**已绑定的对话**，不重新 bind、不新增地址——
这是「同一对话」纪律在工具层的落法。

### 5.1 status 必须打印（缺一不可）

当前状态、最近一次守卫失败原因（`last_guard_failure`）、剩余预算（rounds / same_error / 超时）。

### 5.2 audit 必须输出（归零才算通过）

```
duplicate_sends=0 unconfirmed_sends=0 unexpected_write=0 lessons_pending=0
```

附加输出（供审计复算，不得省略）：`sends_total`、`resume_events`、
`sends_after_resume`（见 §2.4 口径）、`inconclusive`（核验结论里「不自动判失败」的条数）、
`last_reply_count_delta`、`write_mode_at_send`（发送当时的模式）、`write_mode_now`（当前环境模式）。
`verdict=PASS` 要求上面四项归零**且** `sends_after_resume=0` **且** `inconclusive=0`。

`--trace` 额外输出**因果链**：一条 send 从 `send_id` 串到 `approval_id` →
`write_send` 结果 → `write_verify` 判定 → `replies/round-N.md`：

```
trace[] = {send_id, approval_id, round, send_result, signals,
           count_delta, effective_delta, verify_verdict, reply_file, error_class}
```

`verify_verdict == "(未核验)"` 表示复核那一步没跑（§4.2 违规），**链断了就等于没有链**。

---

## 6. 知识回流强制点

1. **失败 / 恢复事件**写进 `journal.jsonl` 时，同步生成一条 lesson 候选（`lessons_candidates`）。
2. **阶段 CLOSED 前**必须执行 `close --stage <id> --require-lessons`：
   把候选合并进 `references/lessons.md`，每条格式：

   ```
   [lesson_id][date_utc][stage][source_run_ref][platform][symptom][dom_signal][fix][verify_cmd]
   [trigger][rollback][blocking][status]
   ```

   `lesson_id` 必须是**稳定且可检索**的（同一坑在任何地方都用同一个 id），形如
   `L-<平台>-<错误类>-<症状哈希前 6 位>`。

   - `trigger`：什么情况下会再踩到（写不出触发条件 = 这不是 lesson，是感想）
   - `rollback`：踩到了怎么退回来
   - `blocking`：`true` 表示**这条不闭合就不许阶段收口**（只给纪律类漏洞用；
     平台/体验类一律 `false`）
   - `status`：`open | merged | retired`

   老格式 10 段（无 trigger/rollback）与中间 12 段（无 blocking）仍可读，
   `lessons_audit.py` 三种都认。

3. **验收**（不看是否进正文，看可审计的数字）：
   - **附录新增数 = 候选合并数 = `audit` 的 `lessons_pending=0`**；
   - 每条含可检索的 `lesson_id`；
   - `references/lessons.md` 哈希必须变化（输出 `lessons_md_sha256_before/after`）；
   - 少一条则 `CLOSED` **失败**。

4. **去重**：按 `lesson_id` 去重——已有同一个 id 时**只更新日期与状态**，不追加重复条目。
   故意搁置的事项（纪律搁置、未实测平台等）以 `status=open` 进附录，
   **不许被「回流完成」的措辞吞掉**（沉默缺口正是要消灭的东西）。

   **`open` 不是待办**（第 2 轮裁定 3）：它只能因为「缺证据」或「缺复现」而存在，
   且必须带 `blocking`。判定三问，按序走：
   ① 有没有**可复现的触发条件**？没有 → 不是 lesson，`retired` 或转进需求卡片的「未明确」；
   ② 有没有**明确判据/回退动作**？没有 → 先补判据，补不齐不许长期 open；
   ③ 是否**已被代码/测试/契约覆盖**？已覆盖 → `merged`（并在条目里指回覆盖它的测试名/契约节号）；
      未覆盖 → 落成一条验收测试（落完即 `merged`），或 `retired` 并写明为何不落。

5. **收口前体检**（`lessons_audit.py`）：`open` 超过 14 天未动静 → `WARN`（`--strict` 下非 0）；
   存在 `blocking=true` 的 `open` → **`close` 直接拒绝**（退出码 5）。
   `close` 会先跑一次体检，体检本身出错也不许静默放行。

6. `close` 输出必须含：`candidates` / `merged` / `appendix_added`（本轮这三个数相等）、
   `appendix_updated`、`lessons_pending=0`、`inconclusive=0`、`verdict`、
   `evidence`（journal / writes / evidence 路径）、
   `lesson_ids`（全部可 grep）、`open_lessons`（仍是 open 的搁置项）、
   `lessons_md_sha256_before/after`。
   **`inconclusive != 0` 时 `close` 必须失败**（退出码 5）——「不自动判失败」的核验结论要先人工复核。

### 6.1 恢复实测的隔离要求

恢复实测（杀进程 → `--resume`）的证据必须**另起一个 run**，
**不得复用**单写验证那个 run 的证据混报——两者是不同的验收项。

---

## 7. 阶段准出标准（来自网页 AI 的 O0~O4）

| 阶段 | 目标 | 改动范围 | 准出标准 |
|---|---|---|---|
| **O0** | 基线冻结 | 只加 `docs/orchestrator-contract.md` | 本文件存在；既有行为零改动；`wa.py --help` 原样通过 |
| **O1** | 编排核心 | 新增 `scripts/orchestrator.py` | 状态机 + 守卫迁移 + 原子落盘 + 单轮驱动 + 双哈希幂等；`loop --once` 能跑完一轮 |
| **O2** | 恢复与预算 | `orchestrator.py` | 人为杀进程后 `--resume` **不重复发送**，产出 `recovery/evidence.json`；`audit` 四项归零 |
| **O3** | 写闸门 | 新增 `scripts/write_gate.py` | 默认模式真实写被 `WRITE_BLOCKED_*` 拦下且无 result 文件；授权后单写满足 `reply_count_delta=1`（见 §2.4）、`duplicate_sends=0` |
| **O4** | 收口回流 | `orchestrator.py` + `references/lessons.md` | `close --require-lessons` 输出 `candidates=merged=appendix_added`；`lessons.md` 哈希变化（给出 before/after）；close 后 `audit` 的 `lessons_pending=0`；每条 `lesson_id` 可 grep |
| **O5** | 按评审优化 + 端到端验证 | `scripts/*` + `tests/*` + 本契约 | 单测 ≥ 95 全绿；真机跑通一轮 `INIT→ROUND_DONE`（`effective_delta=1`、`delta_source=anchor`）；`audit --trace` 四项归零且 `verify_verdict=VERIFIED`；`close --require-lessons` PASS |

### 7.1 本仓库对「git status」验收的替代

本仓库**不是 git 仓库**（无 `.git`），网页 AI 的「`git status --short` 只有文档」这一条无法照跑。
替代验收：**改动文件清单**——O0 阶段只允许新增 `docs/orchestrator-contract.md` 一个文件，
`scripts/` 下两个文件的内容哈希不变（用 `sha256` 记录进 `evidence/o0-baseline.json`）。
