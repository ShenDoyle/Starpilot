# 星舵 Starpilot

> 需求建立后，网页版 AI 规划完整方案，本地 Agent 取回指令、一步一步执行，每步如实回报，最终收口交付。

**分工边界（不可越界）**

| 角色 | 负责 | 不负责 |
|---|---|---|
| 网页版 AI | 方案、步骤、判据、验收标准 | 不接触本地文件，不知道真实执行结果 |
| 本地 Agent | 落地执行、采集证据、如实回报 | 不擅自改方案、不预估方向、不美化结果 |
| 人 | 需求确认、授权、验收 | — |

> **语言 / Language**：本文件为中文版。English version: [README.md](README.md)（默认）。
> 深层作业文档（`SKILL.md`、`references/`、`docs/`）以中文维护——`references/lessons.md`
> 的 13 段格式被 `scripts/lessons_audit.py` 机器解析，不做翻译。

---

## 一、这是什么

一个**双引擎**的自主开发系统，**与具体业务无关**：

```
① 需求建立 → 需求卡片（三列表：已明确/未明确/有冲突）
② 网页 AI 规划 → 项目总纲 + 契约 + 阶段路线
③ 步骤拆解 → 阶段（带准出标准 + 验收方式）
④ 取指令执行 → 代码/产物 + 留证（runtime/runs/<run_id>/）
⑤ 如实回报 → 反馈六段（证据/数据/偏差/待裁定）
⑥ 阶段收口 → 阶段报告 + 阻塞项清单 + 裁定
                    ↑________回到①
```

**它真正的价值不是自动化程度，而是那条分工纪律**：网页版 AI 当架构师（它看不到你的机器，所以它必须给出可验证的判据），本地 Agent 当执行者（它跑得动，所以它必须交出带证据的回报）。两者互不能代替——AI 不得凭空猜执行结果，Agent 不得擅自改方案。

---

## 二、目录结构

```
Starpilot/
├── SKILL.md                      星舵本体：六环模型 + 六条硬规则 + INIT/BIND/LOOP/CLOSE
├── README.md                     本文件
│
├── docs/
│   ├── orchestrator-contract.md    ★ 编排契约 v1.3（O0~O5 准出 + §2.4 指标/§2.5 三值语义/§2.6 对话归属门禁）
│   └── requirements/               需求卡片（进规划前必须先过否定式闸门）
│
├── references/                   知识层（只读，随用随查）
│   ├── protocol.md                   循环协议：state 结构、反馈模板、上下文压缩规则
│   ├── web-ai-platforms.md           ★ 平台选型分级 + 断片机制 + 接入六步（含页面侦察与信号标定）
│   ├── dom-probes.md                 DOM 判定片段（复制即用，不靠截图）
│   ├── process-charter.md            人类流程总纲：六阶段 + 流程红线
│   └── lessons.md                    实战踩坑沉淀（跨项目复用；附录是自动回流区）
│
├── scripts/
│   ├── platforms.py                 ★ 平台适配层：选择器与绑定形态的唯一来源
│   ├── wa.py                        循环桥接 CLI：bind/probe/send/wait/read/state/unlock/stop
│   ├── write_gate.py                 ★ 写闸门：缺省 dry-run → 批准 → 单条真实写 → 核验
│   ├── orchestrator.py               ★ 编排器：14 态状态机 + 守卫 + 预算 + 恢复 + 收口回流
│   ├── confirm.py                    ★ 发送/等待判定的唯一实现（锚点口径，两层共用）
│   ├── statelock.py                  state 单写锁（O_EXCL + 死进程接管，纯标准库）
│   ├── requirement_gate.py           需求卡片否定式校验（只拒绝，不生成）
│   └── lessons_audit.py              知识库体检（格式/重复/超期 open/blocking）
│
├── templates/                    产出物模板（照着填，别自由发挥）
│   ├── requirement-card.md           ① 需求卡片
│   ├── feedback.md                   ⑤ 反馈 + 失败转交 + 写操作回报
│   ├── phase-report.md               ⑥ 阶段报告
│   ├── done-check.md                 收口前四方一致性核对
│   └── web-ai-eval.md                ★ 网页 AI 记忆自测表（选平台时用）
│
├── tests/                            ★ 165 个验收单测（stdlib unittest，无第三方依赖）
│   ├── test_orchestrator.py          状态机 / 预算 / 恢复 / 收口
│   ├── test_orchestrator_gaps.py     迁移矩阵 actor 门控 / 三值语义 / 锚点等待 / 前置断言
│   ├── test_confirm.py               发送确认口径（含两个历史事故回放）
│   ├── test_fill_guard.py            写入接受判定（空态类/发送键/字数）+ 流式补全见证
│   ├── test_statelock.py             state 锁（含死进程接管、外来锁不误删）
│   ├── test_platforms_replay.py      真机 DOM 快照回放（改坏选择器离线就红）
│   ├── test_lessons_audit.py         知识库体检（含 blocking / 超龄）
│   ├── domsel.py                     迷你选择器引擎（回放用，纯 html.parser）
│   └── fixtures/{deepseek,kimi,gemini}/  真机录制的 DOM 快照
│
├── runs/<run_id>/                编排 run 产物：state.json / journal.jsonl / plan.md
│                                 + writes/（dry·approval·result）+ evidence/ + replies/
├── state/                        current（当前 run 指针）、current_goal.md、contract.md
├── stage/<id>/handoff.md         阶段交接（编排器每步重读，绝不假设平台还记得）
│
└── archive/
    └── loop-scaffolding/         早期循环脚手架存档（参考实现，非主流程）
```

---

## 三、怎么用

### 获取

```bash
git clone https://github.com/ShenDoyle/Starpilot.git
cd Starpilot
python -m unittest discover -s tests -p "test_*.py" -t tests   # 165 个单测，纯标准库
```

> **前置**：请先安装 **`bsk`** 浏览器驱动 CLI——本循环当前基于它构建并全链路实测
> （见「六、外部依赖」；等价 CLI 可用 `BSK_BIN` 接入）。

### 两种用法

**A. 装成 agent skill** —— 星舵遵循事实上的 `SKILL.md` 约定（一个文件夹，front-matter 声明
`name` / `description`）。如果你的 AI agent 宿主按此约定加载 skill，把本文件夹复制或软链进
它的 skills 目录即可；宿主会凭 `description` 与触发词（网页版AI、AI循环开发、web-ai-loop、星舵…）激活。

**B. 独立 CLI** —— `scripts/` 全部是 Python 3.9+ 纯标准库脚本，不装 skill 也能直接按下面的
命令驱动循环；此时 `SKILL.md` 当操作手册读。

> 星舵是**宿主无关**的：不依赖任何特定 agent 产品、操作系统或某台机器。唯一外部依赖是
> 一个本地浏览器驱动 CLI（见「六、外部依赖」）。

### 起一轮循环

```bash
cd <项目工作区>

# 0. 填需求卡片 → 拿给用户确认 → 用户明确通过才进规划
#    templates/requirement-card.md

# 1. 绑定对话（只绑一次，地址形态随平台而异）
python scripts/wa.py bind "https://chatgpt.com/c/<conversation-id>" --platform chatgpt

# 1'. 开一个**新**对话（仅限循环第 1 轮；循环中途禁止新开）
python scripts/wa.py bind --new --platform kimi

# 2. 读网页 AI 的回复，原文落盘
python scripts/wa.py read --out runtime/wa/turn-1.md

# 3. 按它说的做（不加戏），留下证据

# 4. 填反馈 → 发出去
#    templates/feedback.md
python scripts/wa.py send --platform chatgpt --file runtime/wa/feedback-1.md

# 5. 等回复完成（DOM 三重判定，不靠截图）
python scripts/wa.py wait --platform chatgpt --timeout 900

# 6. 收口：阶段报告 + done-check + 坑回灌 lessons
```

### 用编排器跑一轮（推荐：写侧有闸门，崩溃可恢复）

```bash
python scripts/orchestrator.py init --goal "<本轮目标>" --stage O1 --card docs/requirements/x.md
                                                       # 带 --card：先过需求闸门，不通过不建 run
python scripts/orchestrator.py bind --reuse            # 复用已绑定对话，不新增地址
python scripts/orchestrator.py plan --file runtime/wa/turn-1.md   # 规划产物落盘
python scripts/orchestrator.py windows                 # 硬规则 6 断言：同实例会话数必须 = 1

# 真实写四步：dry-run → 批准 → 单条真发 → 写后核验（缺省 dry_run，不批不许写）
python scripts/orchestrator.py loop --once --file runtime/wa/feedback-1.md
python scripts/orchestrator.py approve --approval <id> --ttl 900
python scripts/orchestrator.py loop --once             # APPROVED → SENDING → AWAITING_REPLY
#   判据：effective_delta == 1（锚点口径）；verify 由 loop 在回复落盘后自动跑

# 崩溃恢复：只复核 DOM，绝不重发；证据不足一律 PAUSED_HUMAN
python scripts/orchestrator.py loop --resume
python scripts/orchestrator.py unblock --to AWAITING_REPLY --reason "<为什么能继续>"
#   注意：这条修补边要求 last_send.send_id 存在且 verdict 为 INCONCLUSIVE*

# 审计与收口：四项归零 + 因果链 + lesson 强制回流
python scripts/orchestrator.py audit --run <run_id> --trace
python scripts/orchestrator.py close --stage O1 --require-lessons
python scripts/lessons_audit.py --strict      # 知识库体检（blocking open 会拦住收口）
```

**指标口径**：通过线是 **`effective_delta == 1`**（锚点口径：消息 ID > 正文指纹 > 计数），
`reply_count_delta` 只作审计参考。定义冻结在 `docs/orchestrator-contract.md` §2.4 / §2.4.1，
`fail_streak` 三值语义在 §2.5——**代码一改就要同步这几节**。

### 六条硬规则（违反即失败）

1. **同一对话**——项目全程只用一个网页 AI 对话地址，永不新开。
2. **不预估方向**——需求不明只列疑问，不给方案；**原始需求零加工转交**：用户的需求原话必须原样到达网页版 AI，agent 补充仅限事实并标注，需求疑问由网页版 AI 提出、agent 只负责原样传话。
3. **不靠截图判断**——状态一律用 DOM/元素属性判定。
4. **失败两次必须停**——同一目标连续失败 2 次就打包转交，不换第三种办法硬试。
5. **如实回报**——失败就报失败，每个数字都要有出处。
6. **单窗口纪律**——同一浏览器实例内只允许一个会话/Agent Window。

---

## 四、平台支持

`scripts/platforms.py` 是平台差异的**唯一来源**，主流程 `wa.py` 不含任何平台专属选择器。

> **推荐顺序：Kimi → ChatGPT → DeepSeek。**
> Kimi 的单对话长记忆连贯性最好——这是本系统唯一的硬选型判据（全程只用一个对话）；
> ChatGPT 选择器最精确、完成信号最全；DeepSeek 是国内首选（虚拟列表要求按消息 ID 做增量，工具层已处理）。
> Gemini 与豆包已实测可用，但不在推荐集内；Claude 未实测。

| key | 平台 | 状态 | 备注 |
|---|---|---|---|
| `kimi` | Kimi | **已实测** · 推荐 | 长对话连贯性口碑最好的一档；写侧已跑通（2026-10-29 实测，编辑器是 Lexical，发送按钮是 `div` 不是 `button`） |
| `chatgpt` | ChatGPT | **已实测** · 推荐 | 选择器精确、信号最完整，最稳的全能选手 |
| `deepseek` | DeepSeek | **已实测** · 推荐 | 2026-10-29 全链路跑通；编辑器是 `textarea`（读 `value`，不是 `innerText`）、发送键带 BEM `--disabled`、消息容器是**虚拟列表**（增量必须靠消息 ID） |
| `gemini` | Gemini | **已实测** | 2026-10-29 全链路跑通；编辑器是 **Quill**（`ql-editor`）——**exec 写入会静默失败**（字进 DOM 不进 model），必须走 `native` 真实键盘；发送键**条件渲染**（空输入时不存在）；轮次容器 `div.conversation-container` 的十六进制 `id` 是锚点 |
| `doubao` | 豆包 | **已实测** | 2026-10-29 全链路跑通；编辑器是 **Tiptap ProseMirror**——`bsk fill` 键盘模拟无效、`exec` 写入有效；**发送不能点按钮**（点击无反应且移走焦点），唯一路径 = exec 写入 + 焦点不丢 + Enter（`send_method=enter`）；「推荐提问」渲染在回复节点内部，读时剔除（`read_ignore`）；完成标志 = `message_action_regenerate` 出现（纯 icon 按钮，需 `regen_selector`） |
| `claude` | Claude | 待实测 | 可配合 Claude Projects（持久知识库对抗上下文压缩） |

**纪律提醒**：硬规则 1「全程只用一个对话地址」意味着**未绑定的平台不能探测**——
矩阵里 `skipped_not_bound` = 没探测过（不是失败），`deferred_rule1` = 纪律搁置（不是技术失败）。
要实测别的平台，得**由人改绑**，那是架构师级决定。

**候选选择器来自通用模式推断，首次使用必须先 probe 实测并回填**：

```bash
python scripts/wa.py platforms                          # 看清单与状态
python scripts/wa.py probe --platform claude            # 实测选择器
python scripts/wa.py bind "<对话地址>" --platform claude
```

已实测：**ChatGPT**（信号最完整）、**Kimi**（2026-10-29；编辑器 Lexical、发送按钮是 `div`、消息条目是 `div.chat-content-item-*`）、
**DeepSeek**（2026-10-29；`textarea` 编辑器、`div.ds-button--primary.ds-button--circle` 发送键、`div.ds-virtual-list` 虚拟列表）、
**Gemini**（2026-10-29；Quill `ql-editor`、`button[aria-label="发送"]` 条件渲染、`div.conversation-container` 轮次容器）、
**豆包**（2026-10-29；Tiptap ProseMirror 编辑器、exec 写入 + Enter 发送（按钮点击无效）、规范 data-testid 消息体系、推荐提问噪声读时剔除）。
各自平台特有的坑（Kimi 限流弹窗 / DeepSeek 虚拟列表计数失真 / Gemini exec 静默失败 / 豆包发送按钮无效）见 `references/lessons.md`。

接入六步（侦察→定义→probe→信号标定→跑通）、选型判据、「断片」的三种机制 → `references/web-ai-platforms.md`
换平台前建议先跑一遍 `templates/web-ai-eval.md`（20 分钟，自测比评测准）。

---

## 五、现状（2026-10-29，O5 已收口）

> 完成度为**自评 2026-10-29**（自评不是实测：⑤⑥ 的百分比是按「已完成证据 vs 契约准出」估的，
> 逐条依据见下表，可复核）。

| 环 | 完成度 | 依据 |
|---|---|---|
| ① 需求建立 | 75% | 方法论 + 模板 + **工具化闸门**（`requirement_gate.py`：未明确未清空/冲突未裁定/确认未通过一律拒，`init --card` 强制先过） |
| ② 网页 AI 规划 | 90% | 29 轮规划留档，产出总纲 + 契约 + 阶段报告；O0~O4 全部裁定完成 |
| ③ 步骤拆解 | 90% | 阶段路线 + 每阶段准出标准与裁定（O4 收口口径已冻结进契约） |
| ④ 取指令执行 | 88% | `wa.py` + `platforms.py` + `write_gate.py` + `orchestrator.py` 四件套就位；6 平台可路由，**ChatGPT + Kimi + DeepSeek + Gemini + 豆包 已实测** |
| ⑤ 逐步执行 | 95% | 写闸门实测跑通（dry-run→批准→单条真发→核验，`effective_delta=1` 且 `delta_source=anchor`）；等待侧改为锚点口径；崩溃恢复实测零重发；**165 个验收单测全绿** |
| ⑥ 完成项目 | 60% | 知识回流已强制且有**体检**（`lessons_audit.py`：blocking open 阻塞收口、open 超 14 天告警）；缺口剩「多平台实测（需人改绑）」与「长连 soak」 |

### 已闭合的三个缺口（2026-10-29）

| 原缺口 | 现在的做法 | 证据 |
|---|---|---|
| **无 orchestrator** | `scripts/orchestrator.py`：14 态状态机 + 守卫迁移 + 轮次预算 + 崩溃恢复（只复核 DOM、绝不重发） | `runs/20261029T042822-recovery2/evidence/recovery-003.json` |
| **写侧未落地** | `scripts/write_gate.py`：缺省 dry_run + 必须批准 + 单条真实写 + 写后核验（`reply_count_delta == 1`） | `runs/20261029T043211-turn4/writes/*.result.json` |
| **知识回流靠自觉** | `close --stage <id> --require-lessons` 强制合并 lesson 候选，验收看 `candidates=merged=appendix_added` 与 `lessons_pending=0`；`lessons_audit.py` 再做防腐化体检 | `references/lessons.md` 附录（21 条，lesson_id 可 grep；体检 `verdict: PASS`） |

### 仍然存在的缺口

| 缺口 | 表现 | 补法 |
|---|---|---|
| **多平台未实测** | Claude 只读探测**没做过**——矩阵记 `skipped_not_bound`；受硬规则 1 约束，未绑定不能探测 | 由**人改绑**（架构师级决定）后按 `references/web-ai-platforms.md` 六步接入 |
| **ChatGPT 纪律搁置** | 已实测但本轮未绑定，矩阵记 `deferred_rule1`（纪律搁置，不是技术失败），作为 `status=open` 的 lesson 留在附录 | 同上：人明确改绑才解封 |
| **长连 soak 未做** | 编排器只跑过单轮/双轮，没跑过 20 轮预算级的自动循环 | 下一轮候选主题之一 |
| ~~**需求阶段无工具**~~ | **已补（2026-10-29）**：`requirement_gate.py` 否定式校验 + `init --card` 强制先过 | — |
| **离线回放不覆盖渲染时序** | `tests/fixtures/` 验的是选择器命中，不验真页面的渲染时序；离线全绿仍可能真机出偏差 | 下一轮：真机 vs 回放偏差对照 |

完整缺口清单见 `SKILL.md` §8。

---

## 六、外部依赖

| 项 | 说明 |
|---|---|
| 浏览器驱动 CLI | **本循环当前基于 `bsk` 构建并全链路实测——建议先安装 bsk。** `wa.py` 依次在 `BSK_BIN`、`PATH`、`~/.local/bin` 查找；任何暴露同款命令（`session` / `navigate` / `evaluate` / `click` / `press` / `fill`）的本地 CLI 都可用 `BSK_BIN` 接入。一切 DOM 判定走 `evaluate`（JS 返回事实），不靠截图 |
| Python 3.9+ | 标准库即可，无第三方依赖 |
| 网页版 AI | 一个已登录的对话地址 + 一个浏览器实例 |

---

## 七、维护约定

- 改 `references/lessons.md` 前先问：这是通用坑还是项目坑？通用坑才进。
- 改 `SKILL.md` 的硬规则必须留修订记录（谁改的、为什么）。
- 改 `scripts/platforms.py` 的选择器必须同时更新 `verified` 标记与 `notes` 实测日期。
- **改指标口径**（`reply_count` / `msg_count` / `resend_attempts` / 通过线）必须同步
  `docs/orchestrator-contract.md` §2.4——指标定义不许只活在代码里。
- 凭证不进仓库、不进对话、不进日志。星舵的自我约束同样适用于它自己。

---

## 许可证

[MIT](LICENSE) © 2026 左立方（TOPcbm）
