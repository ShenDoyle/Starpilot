<h1 align="center">星舵 Starpilot</h1>

<p align="center"><b>想清楚的交给网页版 AI，动手的交给本地 Agent。</b></p>

<p align="center">
需求建立 → 网页版 AI 规划出可验收的步骤 → Agent 逐条执行并留证 → 如实回报 → 收口交付<br>
<sub>不限定任务类型：写代码、改文档、整理数据、跑运营流程、做调研，凡是「要规划 + 要动手」的活都能用</sub>
</p>

<p align="center">
  <a href="https://github.com/Tencent/BrowserSkill"><img alt="核心依赖 bsk" src="https://img.shields.io/badge/%E6%A0%B8%E5%BF%83%E4%BE%9D%E8%B5%96-bsk-2f75f0"></a>
  <img alt="面向" src="https://img.shields.io/badge/%E9%9D%A2%E5%90%91-%E9%80%9A%E7%94%A8%E4%BB%BB%E5%8A%A1-2f75f0">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.9%2B-2f75f0">
  <img alt="宿主" src="https://img.shields.io/badge/%E5%AE%BF%E4%B8%BB-%E4%B8%8D%E4%BE%9D%E8%B5%96%E7%89%B9%E5%AE%9A%E4%BA%A7%E5%93%81-2f75f0">
  <a href="LICENSE"><img alt="MIT" src="https://img.shields.io/github/license/ShenDoyle/Starpilot?color=2f75f0"></a>
  <a href="https://github.com/ShenDoyle/Starpilot/stargazers"><img alt="Stars" src="https://img.shields.io/github/stars/ShenDoyle/Starpilot?style=social"></a>
</p>

<p align="center">
  <a href="#能做什么"><b>能做什么</b></a> ·
  <a href="#快速开始">快速开始</a> ·
  <a href="#试一个任务">试一个任务</a> ·
  <a href="#有人盯还是没人盯">无人值守</a> ·
  <a href="#支持哪些网页版-ai">平台</a> ·
  <a href="#常用命令">命令</a> ·
  <a href="#六条硬规则违反即失败">硬规则</a> ·
  <a href="#文档导航">文档</a>
</p>

<p align="center"><b>简体中文</b> · <a href="README.md">English</a></p>

**星舵把你和 AI 的分工固定下来。** 网页版 AI 出方案和验收判据，本地 Agent 在你自己电脑上执行，每一步都把结果和证据交回去。方案有人负责，执行有人负责，跑偏能查。

## 能做什么

| 你想完成的事 | 星舵怎么帮 |
| --- | --- |
| **大改动拆开落地** | 网页版 AI 把目标拆成有验收判据的阶段，Agent 一次只做一步，做完回报再要下一步 |
| **长文重排、多份材料合并** | 先定结构再逐节改写，每节都有判据；偏离方案立刻停下重规划 |
| **清洗数据、跨表对账** | 每个数字都要出处，对不上就报差异，不许「大致正确」 |
| **批量核对、按规则出报告** | 规则由网页版 AI 定，Agent 执行并留证，同一处连续失败两次就停 |
| **多方案调研选型** | 判据由网页版 AI 定，证据由 Agent 找，结论能回溯到来源 |

和常见做法比，差别在分工：

|  | 只用聊天框 | 只用本地 Agent | 星舵 |
| --- | --- | --- | --- |
| **谁定方案** | 你 | Agent 自己猜 | 网页版 AI：方案、步骤、验收判据 |
| **谁动手** | 你 | Agent | 本地 Agent：执行、采证、如实回报 |
| **跑偏怎么发现** | 你自己看出来 | 常常发现不了 | 每步反馈必带偏差，失败不许美化 |
| **交接靠什么** | 翻聊天记录 | 无记录 | journal + 阶段交接，随时能续 |

## 快速开始

需要三样：一个 AI Agent、`bsk`（浏览器 CLI）、Python 3.9+。

**后两样 Agent 会自己装。** 你只要放好文件夹、说一句要做什么；唯一要亲手做的是在浏览器里启用扩展开关。

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

**Python 3.9+**，不需要任何第三方包。自检三条命令：

```bash
python scripts/env.py            # Python 版本、bsk 路径、扩展连没连
python scripts/env.py --json     # 给 Agent 读：ok + actions（下一步写在里面）
python scripts/env.py --install  # 缺 bsk 时自动装（官方脚本，用户级）
```

</details>

## 试一个任务

装上之后不用记命令：

> 💬 用星舵做一下 XX ／ 让网页版 AI 规划一下 XX ／ 跟着 Kimi 做这个项目

会看到三件事：

- 开工前把需求问清楚：哪些明确、哪些没定、哪些互相冲突。你点头才开工。
- 每轮回报三句：做了什么、证据在哪、哪里没做到。
- 想升级，说一句 **「升到最新版」**。

## 有人盯，还是没人盯

开工前定三件事，之后照办，不再反复请示：

| 定什么 | 可选 | 结果 |
| --- | --- | --- |
| **执行模式** | 👀 逐步确认 | 每轮结束停下来给你看，点头才继续 |
| | 🚀 直接执行 | 连续往下跑，只在三条例外停 |
| **授权范围** | 整个任务 / 本阶段 / 接下来 N 条 / 每条都问 | 决定中途还弹不弹授权；可带截止时间，过期失效 |
| **无人值守预案** | 两条路都通选哪条 · 能不能装东西花钱 · 外部服务挂了怎么办 · 连续失败两次怎么办 | 人不在场时按预案自己选，依据记进 journal |

无人值守按预案走，只在三种情况停下来找人：

1. 同一个操作连续失败两次
2. 对话地址或身份漂移（可能串到别的对话去了）
3. 冒出新歧义，而预案没覆盖

中途改主意说一句就行：「这几步我自己看」「接下来不用问我」「只授权这个阶段」，下一轮生效。

## 一轮循环长什么样

```
① 需求说清楚  →  ② 网页版 AI 出方案与步骤  →  ③ Agent 执行并留证据
                                                    ↓
⑥ 收口交付  ←  ⑤ 把结果写成反馈发回去  ←  ④ 如实记录偏差
                    ↑___________________|
```

关键在 ④⑤：Agent 不许「顺手优化」，也不许报喜不报忧。做了什么、数字从哪来、哪一步没成，都写回去，由网页版 AI 判断下一步。

## 它不会做的事

- **不硬猜需求**——有人盯时先问清；无人值守时按预案走，依据记下来
- **不擅自扩大授权**——超范围请求分级处理：无害的放行留痕，有风险的换做法继续，碰凭证、删除、跨站的停下
- **不绕过你动文件**——写操作默认只演练；要么逐条批准，要么开工时授权「这个阶段可以直接写」
- **不抢同一个对话**——一个项目全程一个对话，多任务并行会排队
- **不靠截图判断页面**——用 DOM 读事实，避免「看着像成功了」

## 六条硬规则（违反即失败）

这六条写死在 `SKILL.md` §1，Agent 没有解释空间：**同一对话** · **不预估方向** · **不靠截图判断** · **失败两次必须停** · **如实回报** · **单窗口**。

## 支持哪些网页版 AI

Kimi、ChatGPT、DeepSeek、Gemini、豆包都已实测，开箱可用。平台差异全部收在 `scripts/platforms.py`，主流程没有平台专属代码，别的对话界面也能接。

- **未实测的平台**：能读，写入前先跑一次 `wa.py probe` 标定（约 20 分钟）。
- **界面改版**：每个元素挂着多个候选选择器，还有命中的就照常跑；全失配时跑一次 probe，回填即可。
- **推荐顺序 Kimi → ChatGPT → DeepSeek**：这是规划质量排序，不是兼容性排序。全程只用一个对话，记不住上下文的平台会断片。不推荐 ≠ 不能用。

| key | 平台 | 状态 | 一句话 |
| --- | --- | --- | --- |
| `kimi` | Kimi | 已实测 · 首选 | 长对话连贯性最好 |
| `chatgpt` | ChatGPT | 已实测 | 选择器最精确，完成信号最全 |
| `deepseek` | DeepSeek | 已实测 | 国内首选；虚拟列表，按消息 ID 增量读 |
| `gemini` | Gemini | 已实测 | 写入必须走真实键盘，模拟写入会静默失败 |
| `doubao` | 豆包 | 已实测 | 发送只能靠回车，点按钮无效 |
| `claude` | Claude | 未实测 | 配置已备好，标定一次即可用 |

接新平台：

```bash
python scripts/wa.py platforms                  # 看清单与标定状态
python scripts/wa.py probe --platform <key>     # 在真页面标定选择器
python scripts/wa.py bind "<对话地址>" --platform <key>
```

细节见 `references/web-ai-platforms.md`；挑平台前建议先跑 `templates/web-ai-eval.md` 的自测，比看评测准。

## 常用命令

日常由 `scripts/orchestrator.py` 驱动：14 态状态机，每次迁移都有守卫、可审计；写操作走闸门（演练 → 批准 → 单条真发 → 写后核验）；崩溃后只复核页面，绝不重发。

不装 skill 也能用：`scripts/` 全是纯标准库脚本，把 `SKILL.md` 当操作手册即可。

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
python scripts/orchestrator.py audit --run <run_id> --trace
python scripts/orchestrator.py close --stage O1 --require-lessons
```

- 通过判据：`effective_delta == 1`（锚点口径：消息 ID > 正文指纹 > 计数），冻结在 `docs/orchestrator-contract.md` §2.4。
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
| 🌐 [bsk](https://github.com/Tencent/BrowserSkill) | 核心依赖（MIT）：CLI + Chrome/Edge 扩展，用你已经登录的浏览器干活。星舵只调六个子命令：`session` / `navigate` / `evaluate` / `fill` / `press` / `browsers`。`wa.py` 依次在 `BSK_BIN`、`PATH`、`~/.local/bin` 找它 |
| 🐍 Python 3.9+ | 仅标准库，无第三方依赖 |
| 🤖 网页版 AI | 一个已登录的对话地址 + 一个浏览器实例 |

```bash
python -m unittest discover -s tests -p "test_*.py" -t tests   # 254 条，纯标准库
python scripts/upgrade.py --check        # 有没有新版
python scripts/upgrade.py --to-latest    # 直接升：自动选源、备份、装完自测
```

国内直连 GitHub 常常超时，升级器会并发探测直连和一批公益加速镜像，谁的快用谁；`runtime/ runs/ state/ stage/` 不动，覆盖前整体备份，装完自动跑单测。

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

维护约定：改硬规则留修订记录；改 `platforms.py` 的选择器同步更新 `verified` 与实测日期；改指标口径同步契约 §2.4；凭证不进仓库、不进对话、不进日志。

## 许可证

[MIT](LICENSE) © 2026 左立方（TOPcbm）
