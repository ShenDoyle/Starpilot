<h1 align="center">星舵 Starpilot</h1>

<p align="center"><b>想清楚的交给网页版 AI，动手的交给本地 Agent。</b></p>

<p align="center">
<sub>写代码、改文档、整理数据、跑运营流程、做调研——凡是「要规划 + 要动手」的活都能用</sub>
</p>

<p align="center">
  <a href="https://github.com/Tencent/BrowserSkill"><img alt="核心依赖 bsk" src="https://img.shields.io/badge/%E6%A0%B8%E5%BF%83%E4%BE%9D%E8%B5%96-bsk-2f75f0"></a>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.9%2B-2f75f0">
  <img alt="版本" src="https://img.shields.io/badge/%E7%89%88%E6%9C%AC-0.1.1%20preview-6b7280">
  <a href="LICENSE"><img alt="MIT" src="https://img.shields.io/github/license/ShenDoyle/Starpilot?color=2f75f0"></a>
  <a href="https://github.com/ShenDoyle/Starpilot/stargazers"><img alt="Stars" src="https://img.shields.io/github/stars/ShenDoyle/Starpilot?style=social"></a>
</p>

<p align="center">
  <a href="#能做什么"><b>能做什么</b></a> ·
  <a href="#用你熟悉的-agent">Agent</a> ·
  <a href="#快速开始">快速开始</a> ·
  <a href="#试一个任务">试一个任务</a> ·
  <a href="#支持哪些网页版-ai">平台</a> ·
  <a href="#常用命令">命令</a> ·
  <a href="#文档导航">文档</a>
</p>

<p align="center"><b>简体中文</b> · <a href="README.md">English</a></p>

## 能做什么

| 你想完成的事 | 星舵怎么帮 |
| --- | --- |
| **大改动拆开落地** | 网页版 AI 把目标拆成有验收判据的阶段，Agent 一次只做一步，做完回报再要下一步 |
| **长文重排、多份材料合并** | 先定结构再逐节改写，每节都有判据；偏离方案立刻停下重规划 |
| **清洗数据、跨表对账** | 每个数字都要出处，对不上就报差异 |
| **批量核对、按规则出报告** | 规则由网页版 AI 定，Agent 执行并留证 |
| **多方案调研选型** | 判据由网页版 AI 定，证据由 Agent 找，结论能回溯到来源 |

和常见做法比，差别在分工：

|  | 只用聊天框 | 只用本地 Agent | 星舵 |
| --- | --- | --- | --- |
| **谁定方案** | 你 | Agent 自己猜 | 网页版 AI：方案、步骤、验收判据 |
| **谁动手** | 你 | Agent | 本地 Agent：执行、采证、如实回报 |
| **跑偏怎么发现** | 你自己看出来 | 常常发现不了 | 每步反馈必带偏差，失败不许美化 |
| **交接靠什么** | 翻聊天记录 | 无记录 | journal + 阶段交接，随时能续 |

## 用你熟悉的 Agent

<p align="center">
<table><tr>
<td align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="assets/agents/codex-dark.svg"><img alt="Codex" src="assets/agents/codex.svg" width="36"></picture><br><b>Codex</b></td>
<td align="center"><img alt="Claude Code" src="assets/agents/claude.svg" width="36"><br><b>Claude Code</b></td>
<td align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="assets/agents/cursor-dark.svg"><img alt="Cursor" src="assets/agents/cursor.svg" width="36"></picture><br><b>Cursor</b></td>
<td align="center"><img alt="OpenClaw" src="assets/agents/openclaw-avatar.png" width="36"><br><b>OpenClaw</b></td>
</tr><tr>
<td align="center"><img alt="CodeBuddy" src="assets/agents/codebuddy.svg" width="36"><br><b>CodeBuddy</b></td>
<td align="center"><img alt="WorkBuddy" src="assets/agents/workbuddy.svg" width="36"><br><b>WorkBuddy</b></td>
<td align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="assets/agents/pi-dark.svg"><img alt="Pi" src="assets/agents/pi.svg" width="36"></picture><br><b>Pi</b></td>
<td align="center"><picture><source media="(prefers-color-scheme: dark)" srcset="assets/agents/shell-dark.svg"><img alt="任何能跑命令的 Agent" src="assets/agents/shell.svg" width="36"></picture><br><b>其它 Agent</b></td>
</tr></table>
</p>

这些，以及任何能跑 Shell 命令的 Agent，都能直接装。Agent 和模型由你挑。

## 快速开始

一个上面这样的 Agent，其余它自己装；唯一要亲手做的是在浏览器里启用扩展开关。

1. 把整个文件夹放进 AI 客户端的 skills 目录（例如 `~/.workbuddy/skills/starpilot`），或 `git clone` 到那里。
2. 说一句话（见[试一个任务](#试一个任务)）。

也可以把安装整件事交给 Agent，原话照抄：

```text
按 https://github.com/ShenDoyle/Starpilot 装好星舵。缺什么自己装，装完跑 python scripts/env.py 确认环境，然后告诉我还差什么。
```

<details>
<summary><b>🔧 手动安装</b></summary>

**bsk**（核心依赖，[Tencent/BrowserSkill](https://github.com/Tencent/BrowserSkill) 的浏览器 CLI，默认装到 `~/.local/bin`）：

```bash
# macOS / Linux
curl -fsSL https://raw.githubusercontent.com/Tencent/BrowserSkill/main/install.sh | sh
```

Windows 在 PowerShell 里跑：`irm https://raw.githubusercontent.com/Tencent/BrowserSkill/main/install.ps1 | iex`

再在浏览器启用扩展（[Chrome](https://chromewebstore.google.com/detail/hhcmgoofomhgciiibhipgmgkgnoenaoi) / [Edge](https://microsoftedge.microsoft.com/addons/detail/browserskill/emacgiaaaiojkkpkddmmdfhmokgmnikg)），`bsk doctor` 显示 Connected 才算通。装在非默认目录就设 `BSK_BIN` 指过去。

需要 Python 3.9+（无第三方包）；装完跑 `python scripts/env.py` 确认。

</details>

## 试一个任务

装上之后不用记命令：

> 💬 用星舵做一下 XX ／ 让网页版 AI 规划一下 XX ／ 跟着 DeepSeek 做这个项目

- 开工前把需求问清楚：哪些明确、哪些没定、哪些互相冲突。你点头才开工。
- 每轮回报三句：做了什么、证据在哪、哪里没做到。

## 有人盯，还是没人盯

| 你要的节奏 | 开工前说一句 |
| --- | --- |
| 👀 **逐步确认** | 「每一步都先给我看」——每轮结束停下来，你点头才继续 |
| 🚀 **直接执行** | 「不用每步问我」——它连续跑，只在做登录、扫码这类必须你亲手的事时找你 |

写东西前先授权：「整个任务你直接写」「只授权这个阶段」，或者什么都不说——那就每条都问你。中途改主意随时说，下一轮生效。

## 它不会做的事

- **不硬猜需求**——有人盯时先问清；无人值守时按预案走，依据记下来
- **不美化结果**——失败就报失败，不写「应该可以了」
- **不擅自扩大授权**——无害的放行留痕，有风险的换做法，碰凭证、删除、跨站的停下
- **不绕过你动文件**——写操作默认只演练；要么逐条批准，要么开工时授权「这个阶段可以直接写」
- **不抢同一个对话**——一个项目全程一个对话、一个窗口，多任务并行会排队
- **不靠截图判断页面**——用 DOM 读事实，避免「看着像成功了」

这六条写死在 `SKILL.md` §1，没有解释空间。失败两次不是停下来找你，是带着现场回去换方案。

## 支持哪些网页版 AI

没实测过的对话界面也能接，写入前跑一次 `probe` 标定。

| key | 平台 | 状态 | 适合 |
| --- | --- | --- | --- |
| `deepseek` | DeepSeek | 已实测 · **国内首选** | 国内直连快，规划稳 |
| `chatgpt` | ChatGPT | 已实测 · **海外首选** | 方案与验收判据质量最好 |
| `kimi` | Kimi | 已实测 | 长对话连贯性好 |
| `gemini` | Gemini | 已实测 | 窗口大，长文档吃得住 |
| `doubao` | 豆包 | 已实测 | 国内直连，落地成本低 |
| `claude` | Claude | 未实测 | 标定一次即可用 |

标定流程与平台细节见 `references/web-ai-platforms.md`。

## 常用命令

日常由 `scripts/orchestrator.py` 驱动：14 态状态机，写操作走闸门（演练 → 批准 → 真发 → 写后核验），崩溃后只复核、绝不重发。不装 skill 也能用：`scripts/` 全是纯标准库，把 `SKILL.md` 当操作手册。

**环境**（Agent 开工前会自己跑）

```bash
python scripts/env.py                 # Python 版本、bsk 路径、扩展状态
python scripts/env.py --install       # 缺 bsk 时自动装
```

**桥接网页 AI**（`wa.py`）

```bash
python scripts/wa.py platforms                          # 看清单与标定状态
python scripts/wa.py bind "https://chatgpt.com/c/<id>" --platform chatgpt
python scripts/wa.py bind --new --platform kimi         # 开新对话（仅第一轮）
python scripts/wa.py read --out runtime/wa/turn-1.md    # 读回复，原文落盘
python scripts/wa.py send --platform kimi --file runtime/wa/feedback-1.md
python scripts/wa.py wait --platform kimi --timeout 900 # DOM 判定，不靠截图
python scripts/wa.py probe --platform claude            # 新平台先标定再绑定
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
python scripts/orchestrator.py replan --reason "两次都卡在同一个按钮"   # 失败两次：回去换方案
python scripts/orchestrator.py rebind --reason drift                    # 对话被抢/被删：开新的继续
python scripts/orchestrator.py decide --question "..." --choice "..." --why "网页 AI 裁定：..."  # 自主决定留痕
python scripts/orchestrator.py audit --run <run_id> --trace
python scripts/orchestrator.py close --stage O1 --require-lessons
```

- 通过判据：`effective_delta == 1`（锚点口径：消息 ID > 正文指纹 > 计数），冻结在 `docs/orchestrator-contract.md` §2.4。
- 中途出岔子不找你：失败两次 → `replan` 换方案；对话被抢/被删 → `rebind` 开新的继续；新歧义 → 让网页 AI 按项目要求裁定后 `decide` 留痕（§2.14）。
- 每步都停太烦：`orchestrator.py mode --set auto --reason "<用户原话>"`，下一轮生效。

**辅助**

| 命令 | 用途 |
| --- | --- |
| `write_gate.py` | 写闸门：授权范围（single / count / stage / task + 过期时间） |
| `risk.py` | 越界请求分级：低放行留痕 / 中换方案 / 高停下问人 |
| `requirement_gate.py` | 需求卡片否定式校验（只拒绝，不生成） |
| `lessons_audit.py --strict` | 经验库体检：格式、重复、超期未闭合 |
| `statelock.py` | 状态文件单写锁 |
| `upgrade.py` | 从 GitHub 拉最新版：自动选源、备份、装完自测 |

## 依赖、自测与升级

| 项 | 说明 |
| --- | --- |
| 🌐 [bsk](https://github.com/Tencent/BrowserSkill) | 核心依赖（MIT）：CLI + Chrome/Edge 扩展，用你已经登录的浏览器干活。`wa.py` 依次在 `BSK_BIN`、`PATH`、`~/.local/bin` 找它 |
| 🐍 Python 3.9+ | 仅标准库，无第三方依赖 |
| 🤖 网页版 AI | 一个已登录的对话地址 + 一个浏览器实例 |

```bash
python -m unittest discover -s tests -p "test_*.py" -t tests   # 294 条，纯标准库
python scripts/upgrade.py --check        # 有没有新版
python scripts/upgrade.py --to-latest    # 直接升：自动选源、备份、装完自测
```

也可以直接说一句 **「升到最新版」**。国内直连 GitHub 常常超时，升级器会并发探测直连与一批公益加速镜像，谁的快用谁；`runtime/ runs/ state/ stage/` 不动，覆盖前整体备份，装完自动跑单测。

不想装 skill、只要命令行？用 `starpilot-cli` 包：`scripts/` + `references/` + `templates/` + 契约 + 测试，不含 `SKILL.md`，纯标准库，cron 或别的 agent 都能直接调。

<details>
<summary><b>bsk 连上了又断？（会回收子进程的宿主）</b></summary>

WorkBuddy / CodeBuddy 这类宿主会把命令子进程一并回收，`bsk doctor` 可能通过而中途断连。让 daemon 以独立后台任务常驻：`bsk daemon start --foreground`，再从另一条工具调用里验证连接。共享 `BSK_HOME`、`BSK_AUTO_START=0` 见[沙箱宿主指南](https://github.com/Tencent/BrowserSkill/blob/main/docs/sandboxed-agents.md)。

</details>

<details>
<summary><b>📂 装好之后会产生什么</b></summary>

```
<工作区>/.wa-state.json      绑定地址、平台、轮次、发送哈希、失败计数
<工作区>/runtime/wa/         turn-<n>.md（AI 原文）、feedback-<n>.md（我方反馈）
<工作区>/runs/<run_id>/      state.json + journal.jsonl（真相）+ writes/ + evidence/ + replies/
<工作区>/state/              current 指针、current_goal.md、contract.md
<工作区>/stage/<id>/         handoff.md（阶段交接，每步重读）
```

出问题先看这里：`journal.jsonl` 是唯一真相，`writes/` 里能查到每次写的演练、批准与结果。

</details>

## 文档导航

| 你想做什么 | 看哪份 |
| --- | --- |
| 改流程、改硬规则、改 Agent 行为 | `SKILL.md` |
| 改状态机、改指标口径、改确认门与授权 | `docs/orchestrator-contract.md` |
| 接新平台，或某平台开始出怪问题 | `references/web-ai-platforms.md` |
| 让 Agent 避开前人踩过的坑 | `references/lessons.md` |
| 填需求卡片、确认单、反馈、阶段报告 | `templates/` |
| 看这版发了什么、改了什么 | `CHANGELOG.md` |

## 许可证

[MIT](LICENSE) © 2026 左立方（TOPcbm）
