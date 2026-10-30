# 星舵 Starpilot

> 你提需求；网页版 AI 把方案想清楚、给出可验收的步骤；本地 Agent 照着一步一步做，每步如实回报。

> **语言**：本文件为中文版，English version: [README.md](README.md)。

---

# 第一部分 · 五分钟看懂

## 一句话

星舵让两个角色轮流干活：**网页版 AI 当架构师**（出方案、定判据、验收），**本地 Agent 当执行者**（动手、留证据、如实汇报）。

它不限定做什么。写代码、改文档、整理数据表、跑运营流程、做调研——只要是「需要规划 + 需要动手」的任务都能用。

## 为什么需要它

只用聊天框，你得到的是「建议」，还得自己搬去执行；只用本地 Agent，它会自己猜方向，越跑越偏。

星舵把两件事分开：**想清楚的活儿交给出方案的那个，动手的活儿交给能动手的那个，中间用固定的格式交接。** 每一步都有判据、有证据，失败了也必须说出来。

| 角色 | 负责 | 不负责 |
|---|---|---|
| 网页版 AI | 方案、步骤、验收判据 | 看不到你的机器，也不知道真实执行结果 |
| 本地 Agent | 执行、采集证据、如实回报 | 不擅自改方案、不猜方向、不美化结果 |
| 你 | 提需求、授权、验收 | 不用盯过程 |

## 怎么装

1. 装好 **bsk** 浏览器驱动（星舵靠它操作浏览器），确认终端里能直接调用 `bsk`；装在别处就先设环境变量 `BSK_BIN` 指向它。
2. 电脑上有 Python 3.9 或更高版本即可，不需要装任何第三方包。
3. 把本项目整个文件夹放进你的 AI 客户端 skills 目录（例如 `~/.workbuddy/skills/starpilot`），或者直接 `git clone` 到那里。

## 怎么开口

装上之后不用记命令，用人话讲就行：

- 「用星舵做一下 XX」/「让网页版 AI 规划一下 XX」/「跟着 Kimi 做这个项目」
- 它会先跟你确认需求（列清楚哪些明确、哪些没定、哪些互相冲突），你点头之后才去找网页版 AI
- 之后每一轮它都会告诉你：这一步做了什么、证据在哪、有什么没做到
- 想让它一口气跑完不逐步问你：说「不用每步问我，出问题再停」即可（后面能随时改回来）
- 想升级：说**「升到最新版」**

## 一轮循环长什么样

```
① 需求说清楚  →  ② 网页版 AI 出方案与步骤  →  ③ Agent 执行并留证据
                                                    ↓
⑥ 收口交付  ←  ⑤ 把结果写成反馈发回去  ←  ④ 如实记录偏差
                    ↑___________________|
```

关键在 ④⑤：Agent 不许「顺手优化」，也不许报喜不报忧。做了什么、数字从哪来、哪一步没成，都要写回去让网页版 AI 判断下一步。

## 它不做什么

- 不替你想需求，也不替你拍板（需求不清楚就一直问，不会硬猜）
- 不绕过你改文件、发消息（写操作默认只演练，你批准了才真写）
- 不同时开一堆窗口抢同一个对话（一个项目全程只用一个对话，多任务并行会互相排队）
- 不靠截图判断网页状态（用 DOM 读事实，避免「看着像成功了」）

---

# 第二部分 · 给要上手改的人

## 运转方式

六环固定循环：需求建立 → 规划 → 步骤拆解 → 执行 → 回报 → 收口，收口后回到需求，进入下一阶段。

日常由 `scripts/orchestrator.py` 驱动：一个 14 态状态机，每一次状态迁移都有守卫、都能审计；写操作走闸门（演练 → 批准 → 单条真发 → 写后核验）；崩溃之后只复核页面状态、绝不重发，证据不足一律停下来问人。

不装 skill 也能用：`scripts/` 全是纯标准库 Python 脚本，按下面的命令手动驱动即可，此时把 `SKILL.md` 当操作手册读。

## 常用命令

**桥接网页 AI**（`wa.py`）

```bash
python scripts/wa.py platforms                          # 看支持哪些平台、哪些已实测
python scripts/wa.py bind "https://chatgpt.com/c/<id>" --platform chatgpt
python scripts/wa.py bind --new --platform kimi         # 开新对话（仅第一轮；中途禁止）
python scripts/wa.py read --out runtime/wa/turn-1.md    # 读回复，原文落盘
python scripts/wa.py send --platform kimi --file runtime/wa/feedback-1.md
python scripts/wa.py wait --platform kimi --timeout 900 # DOM 三重判定，不靠截图
python scripts/wa.py probe --platform claude            # 新平台先实测选择器再绑定
```

**编排一轮**（`orchestrator.py`，推荐）

```bash
python scripts/orchestrator.py init --goal "<目标>" --stage O1 --card docs/requirements/x.md
python scripts/orchestrator.py bind --reuse              # 复用已绑定对话
python scripts/orchestrator.py plan --file runtime/wa/turn-1.md
python scripts/orchestrator.py loop --once --file runtime/wa/feedback-1.md   # 默认只演练
python scripts/orchestrator.py approve --approval <id> --ttl 900
python scripts/orchestrator.py loop --once               # 批准后才真发
python scripts/orchestrator.py loop --resume             # 崩溃恢复，只复核不重发
python scripts/orchestrator.py audit --run <run_id> --trace
python scripts/orchestrator.py close --stage O1 --require-lessons
```

- 通过判据：`effective_delta == 1`（锚点口径：消息 ID > 正文指纹 > 计数），口径冻结在 `docs/orchestrator-contract.md` §2.4。
- 每一步都停太烦：`orchestrator.py mode --set auto --reason "<用户原话>"`，下一轮生效，不用重开 run。

**辅助**

| 命令 | 用途 |
|---|---|
| `write_gate.py` | 写闸门本体：授权范围（single / count / stage / task + 过期时间）|
| `risk.py` | 越界请求分级：低风险放行留痕 / 中风险换替代方案 / 高风险停下问人 |
| `requirement_gate.py` | 需求卡片否定式校验（只拒绝，不生成、不补全）|
| `lessons_audit.py --strict` | 经验库体检：格式、重复、超期未闭合 |
| `statelock.py` | 状态文件单写锁（防两个任务同时改）|
| `upgrade.py` | 从 GitHub 拉最新版：自动选源、备份、装完自测 |

## 支持哪些网页版 AI

推荐顺序 **Kimi → ChatGPT → DeepSeek**。唯一硬判据是「单对话长记忆连贯性」——全程只用一个对话，记不住的平台会断片。

| key | 平台 | 状态 | 一句话 |
|---|---|---|---|
| `kimi` | Kimi | 已实测 · 推荐 | 长对话连贯性最好 |
| `chatgpt` | ChatGPT | 已实测 · 推荐 | 选择器最精确、完成信号最全 |
| `deepseek` | DeepSeek | 已实测 · 推荐 | 国内首选；虚拟列表，靠消息 ID 增量读 |
| `gemini` | Gemini | 已实测 | 写入必须走真实键盘，模拟写入会静默失败 |
| `doubao` | 豆包 | 已实测 | 发送只能靠回车，点按钮无效 |
| `claude` | Claude | 待实测 | 未绑定；受「一个项目一个对话」约束，改绑需人来定 |

所有平台差异只在 `scripts/platforms.py` 里定义，主流程不含任何平台专属选择器。接入新平台按 `references/web-ai-platforms.md` 的六步走（侦察 → 定义 → probe → 信号标定 → 跑通 → 记录）。换平台前建议先做一遍 `templates/web-ai-eval.md` 的自测，比看评测准。

## 六条硬规则（违反即失败）

1. **同一对话**——一个项目全程只用一个对话地址，永不新开。
2. **不预估方向**——需求不明只列疑问，不给方案；用户原话原样转交，Agent 只补充事实并标注。
3. **不靠截图判断**——状态一律用 DOM / 元素属性判定。
4. **失败两次必须停**——同一目标连续失败 2 次就打包转交，不硬试第三种办法。
5. **如实回报**——失败报失败，每个数字都要有出处。
6. **单窗口纪律**——同一浏览器实例内只允许一个会话。

## 依赖与自测

| 项 | 说明 |
|---|---|
| bsk 浏览器驱动 CLI | 本循环基于它构建并全链路实测；`wa.py` 依次在 `BSK_BIN`、`PATH`、`~/.local/bin` 查找 |
| Python 3.9+ | 仅标准库，无第三方依赖 |
| 网页版 AI | 一个已登录的对话地址 + 一个浏览器实例 |

```bash
python -m unittest discover -s tests -p "test_*.py" -t tests   # 232 条，纯标准库
```

升级：`python scripts/upgrade.py --check` 看有没有新版，`--to-latest` 直接升。国内直连 GitHub 常常超时，脚本会并发探测直连和一批公益加速镜像，谁的快用谁；`runtime/ runs/ state/ stage/` 等现场文件不会被覆盖，覆盖前先整体备份，装完自动跑单测，坏了当场就红。

## 装好之后会产生什么

```
<工作区>/.wa-state.json      绑定地址、平台、轮次、发送哈希、失败计数
<工作区>/runtime/wa/         turn-<n>.md（AI 原文）、feedback-<n>.md（我方反馈）
<工作区>/runs/<run_id>/      state.json + journal.jsonl（真相）+ writes/ + evidence/ + replies/
<工作区>/state/              current 指针、current_goal.md、contract.md
<工作区>/stage/<id>/         handoff.md（阶段交接，每步重读，不假设平台还记得）
```

## 要改它，先读这几份

| 文档 | 什么时候看 |
|---|---|
| `SKILL.md` | 改流程、改硬规则、改 agent 行为 |
| `docs/orchestrator-contract.md` | 改状态机、改指标口径、改确认门与授权 |
| `references/web-ai-platforms.md` | 接新平台、某平台开始出怪问题 |
| `references/lessons.md` | 想让 agent 避开前人踩过的坑（也是回流目标）|
| `templates/` | 需求卡片、确认单、反馈、阶段报告——照填，别自由发挥 |

**维护约定**：改硬规则要留修订记录；改 `platforms.py` 的选择器必须同步更新 `verified` 与实测日期；改指标口径必须同步契约 §2.4——指标不许只活在代码里；凭证不进仓库、不进对话、不进日志。

---

## 许可证

[MIT](LICENSE) © 2026 左立方（TOPcbm）
