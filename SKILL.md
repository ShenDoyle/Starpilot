---
name: starpilot
description: "星舵 · 网页版 AI 驱动的项目开发循环：需求建立 → 网页版 AI 规划完整方案 → 拆成可验收的阶段步骤 → 本地 Agent 取指令逐步执行 → 如实回报 → 阶段收口。网页版 AI 当架构师，本地 Agent 当执行者，形成「规划→执行→反馈→再规划」的闭环。触发：网页版AI、挂chatgpt、按chatgpt指示、AI循环开发、外脑开发、星舵、跟着网页AI做项目、让AI规划项目、本地agent自动执行；EN triggers: starpilot, web-ai-loop, web AI loop, AI loop development, outer-brain development, follow web AI instructions, plan with web AI, browser AI driven development。"
version: "2.0.0"
---

# 星舵 Starpilot

> 需求建立后，网页版 AI 规划完整方案，本地 Agent 取回指令、一步一步执行，每步如实回报，最终收口交付。

## 0. 这是什么

把「思考」交给网页版 AI，「执行」留在本地 Agent。每一轮的固定动作：

```
网页版 AI 出指令  →  Agent 本地执行  →  Agent 把结果写成结构化反馈发回去  →  网页版 AI 出下一步
```

Agent 的价值在**执行与如实回报**，不在替用户和网页版 AI 做决策。

**分工边界（不可越界）**

| 角色 | 负责 | 不负责 |
|---|---|---|
| 网页版 AI | 方案、步骤、判据、验收标准 | 不接触本地文件，不知道真实执行结果 |
| Agent（你） | 落地执行、采集证据、如实回报 | **不擅自改方案、不预估方向、不美化结果** |
| 用户 | 需求确认、授权、验收 | — |

**六环模型**

```
① 需求建立 INIT → ② 网页 AI 规划 PLAN → ③ 步骤拆解 STEP
    → ④ 取指令执行 ACT → ⑤ 如实回报 REPORT → ⑥ 阶段收口 CLOSE
                                    ↑__________________|
```

| 环 | 谁主导 | 产出物 | 模板 |
|---|---|---|---|
| ① 需求建立 | Agent 提问 → 用户拍板 | 需求卡片（三列表） | `templates/requirement-card.md` |
| ② 规划 | 网页版 AI | 规划文档 + 契约 | 项目内 `readme.md` / `docs/contracts-*.md` |
| ③ 步骤拆解 | 网页版 AI + 用户确认 | 阶段路线 + 验收标准 | 项目内 `readme.md` 路线表 |
| ④ 执行 | Agent | 代码 / 产物 + 留证 | `runtime/runs/<run_id>/` |
| ⑤ 回报 | Agent | 反馈六段 + 失败转交 | `templates/feedback.md` |
| ⑥ 收口 | 网页版 AI 裁定 + 用户验收 | 阶段报告 + 阻塞项清单 | `templates/phase-report.md` |

---

## 1. 六条硬规则（违反即失败）

1. **同一对话**。整个项目只用一个网页版 AI 对话地址，绑定后永不新开。发现地址变了立即停下问用户。
2. **不预估方向**。需求不明确时**只列疑问，不给方案**。禁止出现「我理解你大概是想……」「建议顺便也做……」这类补充。
3. **不靠截图判断**。网页状态一律用 DOM / 元素属性判定。截图只在「确认渲染样式」这种 DOM 无法表达时才用，且不作为交互判定依据。
4. **失败两次必须停**。同一操作连续失败 2 次、且没有新信息 → 停止执行，把问题打包发给网页版 AI。**不要换第三种办法硬试**。
5. **如实回报**。失败就报失败，跑不通就说跑不通。禁止「应该可以了」「大概没问题」。汇报里的每个数字都要有出处。
6. **单窗口纪律**。**同一浏览器实例内**只允许存在一个浏览器会话 / Agent Window。绑定、续期、巡检共用这一个；用完即收（session stop）。开新会话前必须先收掉**同实例**的旧会话。**禁止重复开新窗口**——窗口堆积 = 违反本条。wa.py 已内置强制：`ensure_session` 每次都会收掉同实例内 keep 之外的会话，**其他实例的会话绝不误杀**。

   **多实例场景**：一个用户常有多个浏览器资料（不同账号/不同环境）。绑定会话固定用**一个**实例，不随巡检目标漂移；巡检别处时用别的实例的会话，**用完立即 stop**；目标实例未连接时停下让用户重连，**不换实例、不省略 `--browser`**。

---

## 2. INIT：接需求（激活 skill 的第一件事）

### 2.1 先读完所有上下文，再开口

激活后**不要立刻提问**，先把这些读完：

- 本次会话里用户发过的**全部消息**（含截图、附件、粘贴的报错）
- 工作区里已有的 `readme.md` / `docs/` / `AGENTS.md` / 记忆与日志目录（若存在）近几日内容
- 项目当前状态（已完成的、进行中的、明确不做的）

读不完就说明读不完，不要跳过。

### 2.2 需求分析只做三件事

产出一张表，**只写事实，不写判断**（模板：`templates/requirement-card.md`）：

| 类别 | 内容 |
|---|---|
| **已明确** | 用户原话里能直接落地的要求（尽量贴近原话） |
| **未明确** | 必须用户回答才能开工的点（每个都写清「不同答案会导致什么不同做法」） |
| **有冲突** | 前后要求矛盾、或与现有代码/文档矛盾的地方 |

**判定标准**：如果某条需求你有两种以上合理解读，它就属于「未明确」，不属于「已明确」。

### 2.3 拿这张表跟用户确认

一次问完，不要挤牙膏。用选择题优先，开放式兜底。

确认通过的标准：用户明确说「就这样」「开始」「没问题」之类。**用户沉默或只回答部分问题，不构成通过。**

### 2.4 需求成熟度分流

| 用户给的 | 做法 |
|---|---|
| 几乎完善的需求 | 仍要过 §2.2，重点核「有冲突」；确认后直接进 §3 |
| 只有初步想法 | §2.2 的「未明确」会很长 → **逐条问**，问完再进 §3。**不要在需求没定型时去问网页版 AI**——那等于让它替你猜用户要什么 |

---

## 3. BIND：接入网页版 AI

### 3.0 先定平台（选型不是随手挑的）

网页版 AI 是星舵的**架构师**，选错了整个循环质量都塌。定平台前必读 `references/web-ai-platforms.md`，核心判据只有一条硬的：**单对话长记忆不衰减**（星舵全程只用一个对话）。

```bash
python scripts/wa.py platforms        # 看支持哪些平台、哪些已实测
```

已实测：**ChatGPT**、**Kimi**（2026-10-29 跑通完整一轮）。候选：Claude（持久工作区对抗压缩）、Gemini、DeepSeek（国内记忆强，需落盘兜底）、豆包。
**换平台前跑一遍 `templates/web-ai-eval.md`**（20 分钟，自测比评测准）。
选定后写进 `.wa-state.json` 的 `platform`，后续命令自动沿用。

### 3.1 前置：登录

网页版 AI 需要登录、且当前未登录时：

- **让用户自己登录。不要代填账号密码，不要读取任何凭证。**
- 用浏览器驱动 CLI 起独立窗口（`--no-focus`），把地址给用户，说清「登录完告诉我」。
- **发给网页版 AI 的正文会进平台服务器**（与本地 CLI 不同）：密钥、令牌、密码、
  客户敏感数据、未公开的经营数据**一律不进对话正文**。必须引用时只给结构（字段名/占位符），
  数据让用户当面补，或改成本地处理。
- 登录完成后校验：页面出现输入框 + 用户头像，才算登录成功。仅出现「登录」按钮 = 未成功。

### 3.2 绑定对话（只绑定一次）

```bash
python scripts/wa.py bind "https://chatgpt.com/c/<conversation-id>" --platform chatgpt
```

绑定后写入 `.wa-state.json`（含 `url` / `platform` / `conv_id` / `conv_origin`）。**此后每一轮发送前都校验当前 URL 与绑定值一致**，不一致立刻停。

**归属门禁（2026-10-29 事故后新增，默认开启）**：bind 会判「这条对话归谁」——

- **首次绑到一条有历史消息的对话 → 默认拒绝**（`foreign_conv`）：那多半是用户自己正在聊的对话，项目挂上去 = 上下文是旧的 + 发送会污染用户对话 + 锚点基线错位。要干净上下文用 `bind --new`；确实要接管这条对话，加 `--reuse-existing` 显式承认（锚点基线从此处起算）。
- **懒加载平台的「假空」已兜住**：打开旧对话时消息区会先空转几秒，probe 读到 0 不代表对话是空的。bind 内部先等读数稳定（`settle_msg_count`：连续 3 次相同）再做归属判定。
- **state 里的对话必须能自证来源**（`conv_origin`：`new_chat` / `empty` / `adopted`）。门禁上线前的老 state 缺这个字段，续绑时会被要求重新确认，不许靠 url 相等蒙混。

**验证绑定是否成功**：地址形态与平台匹配（ChatGPT 是 `/c/<uuid>`，各平台形态见 `platforms.py` 的 `url_pattern`），且页面上能看到历史消息。只有首页或「新对话」页 = **不接受**。

### 3.2.1 开新对话（仅限循环第 1 轮）

```bash
python scripts/wa.py bind --new --platform kimi      # 只接受该平台自己的新对话入口
```

`bind --new` 走的是平台的**新对话入口**（`platforms.py` 的 `new_chat_url`；Kimi 是首页
`https://www.kimi.com/`，Gemini 是 `https://gemini.google.com/app` —— 注意**不是** home：
gemini.google.com 在登录态下会重定向回最近一次对话，等于没有新开）。
不接受任意地址冒充；发第一条消息后平台会自己跳到真实会话地址，state 会自动改写并落盘
`conv_id` + `conv_origin=new_chat`。落点必须「无会话 id + 0 条消息」，入口被重定向回已有对话会直接拒绑。

纪律不变：**第 1 轮可以用它开新对话，循环中途一律禁止**。

### 3.3 已有对话 vs 新对话的判定

| 现象 | 含义 | 动作 |
|---|---|---|
| 地址含平台约定的会话 id，且有历史消息 | 正确 | 继续 |
| 地址是首页/新对话页，或有「开始新对话」提示 | 新对话 | **停下，问用户要正确地址**；确属第 1 轮才用 `bind --new` |
| 页面显示「对话不存在 / 已删除」 | 地址失效 | 停下，让用户重建并给新地址 |

禁止「找不到就新开一个」——历史上下文一断，整个循环就废了。

---

## 4. LOOP：单轮循环

### 4.1 发送（一次只发一件事）

```bash
python scripts/wa.py send --file <本轮反馈文件.md> --platform chatgpt
```

发送要点：

- **长文本一次写入，不要分段 append**。分段会产生重复内容。
- 写入后**先校验输入框字数**，再点发送；字数不符就重写一次，不要连点发送。
- 发送成功后记下「本轮序号 + 发送时间 + 正文哈希」到 state，**防重复发送**。
- 关键：若上一次发送后未确认回复完成就重发，会出现两条相同提问。**发送前必须先确认「输入框为空」且「上一轮已结束」**。
- **判断「真的发出去了」不能只看输入框变空**：实测 Kimi 免费额度繁忙时会**清空输入框却拒绝创建会话**（弹限流弹窗）。
  可靠判据是三者之一：**消息数增加 / 地址变了 / 我发的那段文字出现在页面上**（`platforms.js_has_text` 用正文指纹比对）。
  三者都不满足 = **没发出去**，别记 turn、别等回复，先查页面弹窗。

### 4.2 等待回复完成（DOM 判定，不用截图）

```bash
python scripts/wa.py wait --timeout 900 --platform chatgpt
```

判定逻辑（详见 `references/dom-probes.md`）：

| 信号 | 含义 |
|---|---|
| 「停止生成」按钮存在 | 仍在生成 |
| 「停止生成」消失 + 出现语音/发送按钮 | 本轮结束 |
| 末条回复文本长度两次采样一致 | 已稳定 |
| 页面出现「Something went wrong / 网络错误」 | 失败，按 §4.4 处理 |

三条同时满足（无 stop 按钮 + 文本稳定 + 输入框可用）才判「完成」。**单看一条会误判**：文本长度稳定也可能只是生成中断在长句中间。

### 4.3 读取与执行

1. 读末条 AI 回复全文，落盘到 `runtime/wa/turn-<n>.md`（原文不改写）。
2. 拆出「要做什么」+「验收标准」+「禁止事项」。
3. **按它说的做，不加戏**。它说要跑 A，你就跑 A；不要顺手把 B 也做了。
4. 执行中每个关键结论都要留下证据（命令、输出、文件路径、数字）。

### 4.4 失败处理：两次原则

```
同一目标第 1 次失败 → 自己排查，换一种**同类**方法再试一次
同一目标第 2 次失败 → 停止，禁止再试第三种
```

第 2 次失败后，写给网页版 AI 的内容必须包含这五项：

1. **目标**：本来要达成什么
2. **现象**：完整报错 / 实际输出（原文，不概括）
3. **已试**：试过的每种方法和各自结果
4. **怀疑**：你判断哪里可能有问题（**包括「怀疑指令本身有问题」**）
5. **待定**：需要它裁定什么

> 这条规则的意义：**任务本身可能有问题，指令可能是错的**。连续硬试会把「指令错误」变成「一堆废墟」，事后无法分辨。

**例外**：明确属于环境瞬时故障（网络抖动、进程被占用），可以重试第 3 次，但要在反馈里写明这是瞬时故障重试。

### 4.5 反馈模板

固定六段，缺段视为不合格反馈（模板：`templates/feedback.md`）：

```markdown
## 第 N 轮执行结果

**状态**：完成 / 部分完成 / 失败 / 阻塞待裁定

**做了什么**
- （动作 + 结果，一行一条）

**证据**
- 命令：`...`
- 输出：（关键行原文）
- 文件：`path/to/artifact`

**数据**
| 项 | 值 | 出处 |
|---|---|---|

**问题 / 偏差**
- （有就写，没有写「无」）

**下一步请裁定**
- （只列真实需要它决定的点）
```

禁止在反馈里写「看起来没问题」「应该可以」这类无证据结论。

### 4.6 CLOSE：阶段收口（收工前必做）

一个阶段做完，**不能「交付即结束」**。收工前必须：

1. 写阶段报告（`templates/phase-report.md`）：做了什么、证据、**未解决阻塞项逐条列出**。
2. 把「网页版 AI 没看过的失败」和「只有我这一侧知道的坑」写进反馈，别等它问。
3. **知识回流**：本轮踩到的坑，判断是不是通用坑 → 是就补进 `references/lessons.md`（规则六 §R11 的做法）。
4. 跑一次完成判定（`templates/done-check.md`）：文档状态、报告编号、契约编号、代码实际**四方一致**。
5. 结论不许粉饰：条件未满足就写「未满足」，不要用「基本完成」糊过去。

---

## 5. 落地实现

### 5.1 依赖

- **浏览器驱动 CLI**：任何能「起浏览器会话 + navigate / evaluate / click / press / fill」
  的本地 CLI（缺省按 `bsk` 查找，用 `BSK_BIN` 环境变量指向你的二进制；不在 PATH 时必须设）
- Python 3.9+（**仅标准库，无第三方依赖**；任何标准 Python 安装即可）

### 5.2 目录约定

```
<工作区>/.wa-state.json      # 绑定地址、平台、轮次、发送哈希、失败计数
<工作区>/runtime/wa/         # turn-<n>.md（AI 原文）、feedback-<n>.md（我方反馈）
<工作区>/runs/<run_id>/      # 编排 run：state.json（快照）/ journal.jsonl（真相）/ plan.md
                             #   + writes/（dry/approval/result）+ evidence/（恢复证据）+ replies/
<工作区>/state/              # current（当前 run 指针）、current_goal.md（目标）、contract.md（契约副本）
<工作区>/stage/<id>/         # handoff.md（阶段交接；编排器每步重读，绝不假设平台还记得）
```

编排器入口（`scripts/orchestrator.py`）：
`init [--card 需求卡片] → bind --reuse → plan → loop --once [--file 正文] → approve → loop --once → loop --resume → close --stage <id> --require-lessons`。
真实写**只能**经 `scripts/write_gate.py` 的闸门（缺省 dry_run）；
判据口径见 `docs/orchestrator-contract.md` §2.4（通过线 `effective_delta == 1`，锚点优先）。

辅助命令（都是「让纪律变成断言」的那一类）：

| 命令 | 用途 |
|---|---|
| `orchestrator.py windows` | 硬规则 6 运行时断言：同一浏览器实例下的会话数必须 =1 |
| `orchestrator.py unblock --to <状态> --reason <理由>` | 人工解除卡点；`PAUSED_HUMAN→AWAITING_REPLY` 还要求 `send_id` + `verdict=INCONCLUSIVE*` |
| `orchestrator.py card --file <卡片>` | 需求卡片否定式校验（只拒绝，不生成） |
| `orchestrator.py audit --trace` | 因果链：`send_id → write_send → write_verify → replies/round-N.md` |
| `orchestrator.py lesson --blocking` | 登记一条「不闭合就不许收口」的 lesson |
| `wa.py unlock` | 清残留的 state 写锁（O_EXCL 锁 + 死进程接管） |
| `wa.py clear` | 清空输入框（不发送）：给「已写入未发送」的恢复态用 |
| `lessons_audit.py [--strict]` | 知识库体检：格式/重复/超期 open/blocking open |

### 5.3 会话纪律

- 用 `--no-focus` 起窗口，**不抢用户焦点**，不干扰用户正常用电脑。
- 任务结束才 `session stop`；中途停止会丢 Agent Window 状态。
- 同一时刻只允许一个 session 操作该对话，避免双写。

### 5.4 安全机制总览（已有，别重复造，更别绕开）

这些机制**不是愿望清单**，全部已实现并有实测/单测背书。做任何改动前先对照这张表：
你要改的东西撞上哪一行，就去看那一行的实现和测试。

| # | 机制 | 防什么 | 在哪 | 怎么验 |
|---|---|---|---|---|
| 1 | 写闸门四步 + 单写模式 | 未批准的真实写、二次发送 | `write_gate.py`（缺省 dry_run） | dry→approve→send→verify；失败不产生 result.json |
| 2 | 写路径白名单 `write_allowed` | 未实测的写入方式毁编辑器（Quill 事故） | `platforms.py`；CLI 与闸门**同源**校验 | 不在白名单 rc=8，拒真写 |
| 3 | **对话归属门禁** | 项目挂到别人正在聊的对话上、污染用户对话 | `platforms.bind_verdict`（conv_id+msg_count+conv_origin） | `tests.test_bind_guard`；非空对话默认拒绑 |
| 4 | 地址漂移守卫 + conv 对账 | 消息写进错的对话（人切了标签页/工具复用会话） | `write_gate.guard_ok`；`wa.py probe` 输出 `conv_mismatch` | 换页后 send 直接被拒 |
| 5 | 发送确认三重判据 | 「点了发送但没发出去」被谎报成功（Kimi 限流实测） | `confirm.classify_send_result`（msg_count/url/needle 三选一） | 输入框清空**不**算成功信号 |
| 6 | 幂等（同 prompt+URL hash 拒重发） | 崩溃恢复后重复提问 | `write_gate._already_sent` | `loop --resume` 实测零重发 |
| 7 | 两次失败停 + fail_streak 唯一写入口 | 无限重试把「指令错误」变成废墟 | SKILL §4.4；`orchestrator.VERDICT_EFFECT` | `tests.test_orchestrator_gaps`；源码守卫禁裸写 |
| 8 | state 写锁 | 两个 run 同时写互相覆盖（=「同一对话」被无声破坏） | `statelock.py`（O_EXCL + 死进程接管） | `tests.test_statelock` |
| 9 | 弹窗/遮罩不硬发 | modal 挡住点击 → 点空气白耗一轮 | `wa.py send` 前置 modal 检查 | Kimi 限流弹窗实测 |
| 10 | 空输入不点空气 | 「看着写好了、其实没写」（工具自证≠事实） | `platforms.fill_check` 三见证 | `tests.test_fill_guard` |
| 11 | 单窗口纪律 | 窗口堆积、双写同一对话 | `ensure_session` 收同实例旁支会话 | `orchestrator.py windows` 运行时断言 |
| 12 | 登录不代填凭证 | 凭证进对话/日志 | SKILL §3.1 | 人工纪律 |
| 13 | 敏感数据不进正文 | 密钥/客户数据进平台服务器 | SKILL §3.1（本轮新增） | 发送前自查正文 |
| 14 | 审计 journal + evidence + lessons blocking | 事后无法追溯、带病收口 | `runs/*/journal.jsonl`；`lessons_audit.py` | `audit --trace`；blocking open 拒绝 close |

**缺什么要补，补完回填这张表。**

---

## 6. 故障速查

| 现象 | 原因 | 处理 |
|---|---|---|
| 输入框写入后字数不符 | 长文本 RPC 超时但实际已写入 | 先读字数，**不要重写**；不符才重写一次 |
| 发送后出现两条相同提问 | 未确认上轮结束就重发 | 停止，告知用户，后续按 §4.1 校验 |
| 等待一直不结束 | 页面报错但无提示 | 读 console/DOM 错误节点，按 §4.4 |
| URL 变成 `/` 或 `/new` | 页面跳转 | 立刻停，回绑地址 |
| `probe` 报 `editor: false` | 平台改版，选择器失效 | **这不是平台「断片」**，是 DOM 变了 → 回填 `platforms.py` 的 `sel` |
| 某平台开始「记不住」早期细节 | 上下文压缩或截断 | 落盘原文 + 定期重贴结论；换平台前先跑 `templates/web-ai-eval.md` |
| 回复被截断 | 生成中断 | 不重发，把截断位置报给网页版 AI |
| `msg_count=0` 但页面正常 | 手工 bsk session 顶掉了 state 里的 session | 重新 `wa.py bind <url>` 恢复 |
| 找不到 bsk | 不在 PATH | 用全路径或设 `BSK_BIN` |
| 发送后输入框空了、页面却没有新会话 | 平台侧限流/拒绝（Kimi 会弹「和Kimi聊天的人太多了」弹窗） | 先关弹窗、确认模型档位，再按 §4.1 的三重判据重发；仍不行按硬规则 4 停下转交 |
| 输入框内容删不掉、重写变追加 | 受控编辑器忽略 `execCommand('delete')`／`paste` 是追加 | 用 Range 圈定内容 + `execCommand('insertText')` 覆盖；Kimi 已按此配置 |
| 输入框里留着上次的草稿 | 平台把草稿持久化了（刷新也还在） | 原生按键清空：focus → `Ctrl+A` → `Backspace`，再探字数 |

更多实战坑见 `references/lessons.md`。

---

## 7. 交接清单（每轮结束前）

- [ ] 本轮 AI 原文已落盘
- [ ] 反馈已发送，且确认「发送成功」而非「点击成功」
- [ ] state 里轮次 +1、记录正文哈希
- [ ] 失败计数正确（成功则清零）
- [ ] 没有擅自改变方案
- [ ] 通用坑已回灌 `references/lessons.md`

---

## 8. 已知缺口（v2.0 现状，别假装有）

| 缺口 | 现状 | 补法 |
|---|---|---|
| ~~**无 orchestrator**~~ | **已补（2026-10-29）**：`scripts/orchestrator.py` 落地 14 态状态机 + 守卫迁移 + 轮次预算 + 崩溃恢复（`loop --resume` 只复核 DOM、绝不重发），实测证据见 `runs/20261029T042822-recovery2` | — |
| ~~**写侧未落地**~~ | **已补（2026-10-29）**：`scripts/write_gate.py` 落地「缺省 dry_run + 必须批准 + 单条真实写 + 写后核验」，真实写在 Kimi 上跑通（`reply_count_delta=1`） | — |
| ~~**知识回流靠自觉**~~ | **已补（2026-10-29）**：`orchestrator.py close --stage <id> --require-lessons` 强制把 lesson 候选并进 `references/lessons.md` 附录，验收看 `candidates=merged=appendix_added` 与 `lessons_pending=0` | — |
| **只有 4 个平台实测过** | `platforms.py` 已抽象出适配层，**ChatGPT / Kimi / DeepSeek / Gemini / 豆包已实测**（DeepSeek、Gemini、豆包均于 2026-10-29 全链路跑通）；Claude 的候选选择器**仍为通用模式推断**，未验证。**受硬规则 1 约束，未绑定就不能探测**（矩阵记 `skipped_not_bound`） | 要实测就得**由人改绑**（架构师级决定），再按 `references/web-ai-platforms.md` 六步接入 |
| **「工具说成功/失败」不等于事实** | 三个同源坑：① 导航 `load` 事件不来（Gemini 长轮询）→ 判导航失败；② `bsk fill` 自校验比对多行文本 → 判写入失败（字其实已进去）；③ `innerText` 有字而编辑器 model 是空（Quill）。**一律以页面事实定性**：地址+编辑器、空态类+发送键+字数 | `navigate_soft` / `fill_check` / `normalize_probe` 三处收口；`references/lessons.md` L-GEMINI-NAV-TIMEOUT / FILL-FAILED / FILL-NOT-ACCEPTED |
| **写完未发就卡住时无路可走** | 守卫正确地拒绝「脏输入框」再写，但以前没有清空工具 → 恢复只能人工点浏览器 | `wa.py clear`（Ctrl+A + Delete，真实键盘；清完复核空态） |
| ~~**需求阶段无工具**~~ | **已补（2026-10-29）**：`scripts/requirement_gate.py` 做**否定式**校验（未明确未清空 / 冲突未裁定 / 确认未通过 一律拒），`orchestrator.py init --card` 强制先过。**它只拒绝，不生成、不补全、不建议**（硬规则 2） | 卡片模板见 `templates/requirement-card.md` |
| **长连 soak 未做** | 编排器只跑过单轮/双轮，**没跑过 20 轮预算级的自动循环** | 下一轮可做：只读为主的长连 soak，观察轮次预算与失败计数的真实行为 |
| ~~**指标口径曾漂移**~~ | **已修两轮（2026-10-29）**：① 第一轮把「总消息数」换成「助手气泡数」（契约 v1.0）；② 第二轮发现气泡数在虚拟列表下也不稳（回复 2597 字已渲染、`reply_count` 仍是 2），改为**锚点口径**（消息 ID > 正文指纹 > 计数），通过线 `effective_delta==1`（契约 v1.1 §2.4.1） | 任何口径改动必须同步契约 §2.4/§2.4.1 + 跑 `tests/test_confirm.py` |
| **离线回放只覆盖 DOM 契约** | `tests/fixtures/` 是真机录制的 DOM 快照，`tests/domsel.py` 在 Python 里重放选择器；但它验的是**选择器命中**，不验「真页面渲染时序」 | 下一轮该做「真机 vs 回放偏差」对照（DeepSeek 第 2 轮裁定建议） |

---

## 参考

- `references/protocol.md` — state 结构、反馈模板、多轮上下文压缩规则
- `references/web-ai-platforms.md` — 平台选型分级、「断片」三种机制、接入六步（页面侦察 / 平台定义 / probe / 等待与读取信号标定）
- `references/dom-probes.md` — DOM 判定片段（复制即用）
- `references/process-charter.md` — 人类流程总纲（六阶段 + 流程红线 R1~R14）
- `references/lessons.md` — 实战踩坑沉淀（跨项目复用）
- `templates/` — 需求卡片 / 反馈 / 阶段报告 / 完成判定 / 平台记忆自测
- `scripts/wa.py` — bind / send / wait / read / state 命令实现
- `scripts/platforms.py` — 平台适配层：选择器与绑定形态的唯一来源
