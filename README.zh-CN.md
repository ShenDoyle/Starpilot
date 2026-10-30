<h1 align="center">星舵 Starpilot</h1>

<p align="center"><b>想清楚的交给网页版 AI，动手的交给本地 Agent。</b></p>

<p align="center">
需求建立 → 网页版 AI 规划出可验收的步骤 → Agent 逐条执行并留证 → 如实回报 → 收口交付<br>
<sub>不限定任务类型：写代码、改文档、整理数据表、跑运营流程、做调研，只要是「要规划 + 要动手」的活都能用</sub>
</p>

<p align="center">
  <img alt="面向" src="https://img.shields.io/badge/%E9%9D%A2%E5%90%91-%E9%80%9A%E7%94%A8%E4%BB%BB%E5%8A%A1-2f75f0">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.9%2B-2f75f0">
  <img alt="宿主" src="https://img.shields.io/badge/%E5%AE%BF%E4%B8%BB-%E4%B8%8D%E4%BE%9D%E8%B5%96%E7%89%B9%E5%AE%9A%E4%BA%A7%E5%93%81-2f75f0">
  <a href="LICENSE"><img alt="MIT" src="https://img.shields.io/github/license/ShenDoyle/Starpilot?color=2f75f0"></a>
  <a href="https://github.com/ShenDoyle/Starpilot/stargazers"><img alt="Stars" src="https://img.shields.io/github/stars/ShenDoyle/Starpilot?style=social"></a>
</p>

<p align="center">
  <a href="#为什么是星舵"><b>为什么</b></a> ·
  <a href="#怎么装">安装</a> ·
  <a href="#怎么开口">上手</a> ·
  <a href="#有人盯还是没人盯">无人值守</a> ·
  <a href="#支持哪些网页版-ai">平台</a> ·
  <a href="#常用命令">命令</a> ·
  <a href="#六条硬规则违反即失败">硬规则</a>
</p>

<p align="center"><b>简体中文</b> · <a href="README.md">English</a></p>

## 为什么是星舵

只用聊天框，你拿到的是**建议**，还得自己搬去执行；只用本地 Agent，它会自己**猜方向**，越跑越偏。

星舵把两件事拆开：想清楚的活儿交给出方案的那个，动手的活儿交给能动手的那个，中间用固定格式交接。每一步都有判据、有证据，失败了也必须说出来。

|              | 只用聊天框        | 只用本地 Agent      | **星舵**                     |
| :----------- | :------------ | :-------------- | :------------------------- |
| **谁定方案**   | 你             | Agent 自己猜       | 网页版 AI：方案、步骤、验收判据           |
| **谁动手**    | 你             | Agent           | 本地 Agent：执行、采证、如实回报        |
| **跑偏了怎么发现** | 靠你自己看出来      | 常常发现不了          | 每步反馈必带偏差，失败不许美化            |
| **交接靠什么**   | 聊天记录，翻起来费劲   | 无记录             | journal + 阶段交接，随时能续、能回溯      |
| **谁负责边界**   | 你             | Agent 的自律       | 写闸门 + 授权范围 + 越界风险分级         |

谁干什么，边界在哪：

| 角色 | 负责 | 不负责 |
|---|---|---|
| **网页版 AI** | 方案、步骤、验收判据 | 看不到你的机器，也不知道真实执行结果 |
| **本地 Agent** | 执行、采集证据、如实回报 | 不擅自改方案、不猜方向、不美化结果 |
| **你** | 提需求、定授权、验收 | 方案怎么定、活怎么干 |

### 能用在

- **代码与工程**：拆阶段重构、补测试、按验收判据逐条推进
- **文档与内容**：长文结构重排、多份材料合并、按清单逐节改写
- **数据与表格**：清洗、比对、出结论，每个数字都要有出处
- **运营流程**：批量核对、跨表对账、按规则生成报告
- **调研与选型**：多个方案对比，判据由网页版 AI 定，证据由 Agent 找

## 怎么装

1. 装好 **bsk** 浏览器驱动（星舵靠它操作浏览器），确认终端里能直接调用 `bsk`；装在别处就先设环境变量 `BSK_BIN` 指向它。
2. 电脑上有 Python 3.9 或更高版本即可，不需要装任何第三方包。
3. 把本项目整个文件夹放进你的 AI 客户端 skills 目录（例如 `~/.workbuddy/skills/starpilot`），或者直接 `git clone` 到那里。

网页版 AI 用哪个都行：Kimi、ChatGPT、DeepSeek、Gemini、豆包都已实测开箱可用；别的对话界面没标定过也能接，写入前标定一次即可（见[支持哪些网页版 AI](#支持哪些网页版-ai)）。

## 怎么开口

装上之后不用记命令，用人话讲就行：

> 用星舵做一下 XX ／ 让网页版 AI 规划一下 XX ／ 跟着 Kimi 做这个项目

- 它会把需求问清楚（哪些明确、哪些没定、哪些互相冲突），你点头之后才开工
- 之后每一轮它都会告诉你：这一步做了什么、证据在哪、有什么没做到
- 想升级：说**「升到最新版」**

## 有人盯，还是没人盯

开工前一次性把三件事定下来，之后严格照办，不再反复请示：

| 定什么 | 可选 | 结果 |
|---|---|---|
| **执行模式** | 逐步确认（人盯）| 每轮结束停下来给你看，你点头才继续 |
| | 直接执行（无人值守）| 连续往下跑，只在三条例外停 |
| **授权范围** | 整个任务 / 本阶段 / 接下来 N 条 / 每条都问 | 决定中途还会不会弹授权；可带截止时间，过期即失效，要重新取 |
| **无人值守预案** | 两条路都通时选哪条、能不能装东西花钱、外部服务挂了怎么办、连续失败两次怎么办 | 人不在场时遇到岔路按这套预案自己选，并把依据记进 journal |

无人值守不是「出了问题再停」，而是**按预案自己走**。只有三种情况必须停下来找人：

1. 同一个操作连续失败两次
2. 对话地址或身份发生漂移（可能串到别的对话去了）
3. 冒出新歧义，而开工时的预案没有覆盖

中途想改主意，说一句就行——「这几步我自己看」／「接下来不用问我」／「只授权这个阶段」，下一轮立刻生效，不用重开。

## 一轮循环长什么样

```
① 需求说清楚  →  ② 网页版 AI 出方案与步骤  →  ③ Agent 执行并留证据
                                                    ↓
⑥ 收口交付  ←  ⑤ 把结果写成反馈发回去  ←  ④ 如实记录偏差
                    ↑___________________|
```

关键在 ④⑤：Agent 不许「顺手优化」，也不许报喜不报忧。做了什么、数字从哪来、哪一步没成，都要写回去让网页版 AI 判断下一步。

## 它不做什么

- **不硬猜需求**——有人盯时先问清楚再动手；无人值守时按开工前定好的预案走，把选择依据记下来
- **不擅自扩大授权**——超范围的请求按风险处理：无害的放行留痕，有风险的换个做法继续，碰凭证、删除、跨站的停下
- **不绕过你动文件**——写操作默认只演练；要么你逐条批准，要么开工时授权「这个阶段内可以直接写」，超范围一律不写
- **不同时开一堆窗口抢同一个对话**——一个项目全程只用一个对话，多任务并行会互相排队
- **不靠截图判断网页状态**——用 DOM 读事实，避免「看着像成功了」

---

## 怎么运转

六环固定循环：需求建立 → 规划 → 步骤拆解 → 执行 → 回报 → 收口，收口后回到需求，进入下一阶段。

日常由 `scripts/orchestrator.py` 驱动：一个 14 态状态机，每一次状态迁移都有守卫、都能审计；写操作走闸门（演练 → 批准 → 单条真发 → 写后核验）；崩溃之后只复核页面状态、绝不重发，证据不足一律停下来问人。

不装 skill 也能用：`scripts/` 全是纯标准库 Python 脚本，按下面的命令手动驱动即可，此时把 `SKILL.md` 当操作手册读。

## 常用命令

**桥接网页 AI**（`wa.py`）

```bash
python scripts/wa.py platforms                          # 看清单与标定状态
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

**设计上与具体平台无关：任何网页对话界面原则上都能接。** 平台差异全部收敛在 `scripts/platforms.py` 一个文件里——同一个元素给几个候选选择器，按顺序取第一个命中的；主流程不含任何平台专属代码。

「已实测 / 未实测」说的是**标定进度，不是支不支持**：

| 状态 | 含义 | 能不能用 |
|---|---|---|
| **已实测** | 选择器与写入路径都在真机上标定过 | 开箱即用 |
| **未实测** | 选择器由通用模式推断，还没在真机上标过 | 读取先试一次；**写入前必须标定**（未标定的写入路径一律拒绝——这是安全阀，不是不支持）|

界面改版也不至于直接把活干废：每个元素都挂着多个候选选择器，改掉其中一处，只要还有一个命中的就照常跑；全都失配时 `wa.py probe` 会把页面实际结构报出来，照着回填一次即可恢复。**这一层是半自动**：改版后第一次失败，需要人（或 agent）跑一次 probe，不是靠猜自己长好。

推荐顺序 **Kimi → ChatGPT → DeepSeek**。这**不是兼容性排序，是「谁规划得更靠谱」的排序**：全程只用一个对话，记不住上下文的平台会断片，方案质量一般的平台会直接拖低最终完成度。**不推荐 ≠ 不能用**——你手上有别的模型照样能接，只是验收要更严、阶段切得更小。

| key | 平台 | 标定 | 一句话 |
|---|---|---|---|
| `kimi` | Kimi | 已实测 · 首选 | 长对话连贯性最好 |
| `chatgpt` | ChatGPT | 已实测 | 选择器最精确、完成信号最全 |
| `deepseek` | DeepSeek | 已实测 | 国内首选；虚拟列表，靠消息 ID 增量读 |
| `gemini` | Gemini | 已实测 | 写入必须走真实键盘，模拟写入会静默失败 |
| `doubao` | 豆包 | 已实测 | 发送只能靠回车，点按钮无效 |
| `claude` | Claude | 未实测 | 配置已备好，走一次标定即可用 |

接一个没标定过的平台（约 20 分钟，一次就好）：

```bash
python scripts/wa.py platforms                          # 看清单和标定状态
python scripts/wa.py probe --platform <key>             # 在真页面上标定选择器
python scripts/wa.py bind "<对话地址>" --platform <key>
```

步骤细节见 `references/web-ai-platforms.md`（侦察 → 定义 → probe → 信号标定 → 跑通 → 记录）。挑平台前建议先做一遍 `templates/web-ai-eval.md` 的自测，比看任何评测都准。

## 六条硬规则（违反即失败）

1. **同一对话**——一个项目全程只用一个对话地址，永不新开。
2. **不预估方向**——需求不明只列疑问，不给方案；用户原话原样转交，Agent 只补充事实并标注（无人值守时按开工前预案选，依据记进 journal）。
3. **不靠截图判断**——状态一律用 DOM / 元素属性判定。
4. **失败两次必须停**——同一目标连续失败 2 次就打包转交，不硬试第三种办法。
5. **如实回报**——失败报失败，每个数字都要有出处。
6. **单窗口纪律**——同一浏览器实例内只允许一个会话。

## 依赖与自测

| 项 | 说明 |
|---|---|
| **bsk** 浏览器驱动 CLI | 本循环基于它构建并全链路实测；`wa.py` 依次在 `BSK_BIN`、`PATH`、`~/.local/bin` 查找 |
| **Python 3.9+** | 仅标准库，无第三方依赖 |
| **网页版 AI** | 一个已登录的对话地址 + 一个浏览器实例 |

```bash
python -m unittest discover -s tests -p "test_*.py" -t tests   # 232 条，纯标准库
```

升级：`python scripts/upgrade.py --check` 看有没有新版，`--to-latest` 直接升。国内直连 GitHub 常常超时，脚本会并发探测直连和一批公益加速镜像，谁的快用谁；`runtime/ runs/ state/ stage/` 等现场文件不会被覆盖，覆盖前先整体备份，装完自动跑单测，坏了当场就红。

<details>
<summary><b>装好之后会产生什么</b></summary>

```
<工作区>/.wa-state.json      绑定地址、平台、轮次、发送哈希、失败计数
<工作区>/runtime/wa/         turn-<n>.md（AI 原文）、feedback-<n>.md（我方反馈）
<工作区>/runs/<run_id>/      state.json + journal.jsonl（真相）+ writes/ + evidence/ + replies/
<工作区>/state/              current 指针、current_goal.md、contract.md
<工作区>/stage/<id>/         handoff.md（阶段交接，每步重读，不假设平台还记得）
```

出问题时先看这里：`journal.jsonl` 是唯一真相，`writes/` 里能查到每次写的演练、批准与结果。

</details>

## 要改它，先读这几份

| 文档 | 什么时候看 |
|---|---|
| **`SKILL.md`** | 改流程、改硬规则、改 agent 行为 |
| **`docs/orchestrator-contract.md`** | 改状态机、改指标口径、改确认门与授权 |
| **`references/web-ai-platforms.md`** | 接新平台、某平台开始出怪问题 |
| **`references/lessons.md`** | 想让 agent 避开前人踩过的坑（也是回流目标）|
| **`templates/`** | 需求卡片、确认单、反馈、阶段报告——照填，别自由发挥 |

**维护约定**：改硬规则要留修订记录；改 `platforms.py` 的选择器必须同步更新 `verified` 与实测日期；改指标口径必须同步契约 §2.4——指标不许只活在代码里；凭证不进仓库、不进对话、不进日志。

## 许可证

[MIT](LICENSE) © 2026 左立方（TOPcbm）
