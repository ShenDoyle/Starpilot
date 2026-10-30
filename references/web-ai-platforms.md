# 网页 AI 选型与接入

> 回答两个问题：**哪个网页版 AI 适合当星舵的规划者**，以及**怎么把一个新平台接进来**。
> 文中的平台能力判断来自**公开评测的交叉比对**，不是在本机实测；接下来后必须用
> `templates/web-ai-eval.md` 自测一遍再正式用。带 ⚠️ 的结论置信度较低。

---

## 一、选型判据（本 skill 专用，与通用「哪个 AI 更强」无关）

星舵把网页版 AI 当**架构师**，它必须能撑住一个跑几十轮的工程对话。所以判据只有六条：

| # | 判据 | 为什么是它 | 怎么验 |
|---|---|---|---|
| 1 | **单对话长记忆不衰减** | 星舵全程只用一个对话（硬规则 1），第 40 轮还要能引用第 3 轮定的契约编号 | 10 轮递进引用测试 |
| 2 | **上下文压缩时不丢精确措辞** | 星舵要它记住命令、路径、编号、变量名 | 故意埋「精确串」，看能不能原样复述 |
| 3 | **输出格式稳定** | 反馈模板要求六段固定结构，塌了就得人工重排 | 让它按模板输出，看是否稳定 |
| 4 | **可被浏览器自动化驱动** | 星舵靠 DOM 判定，不靠截图 | 输入框/发送按钮/生成中信号是否可稳定定位 |
| 5 | **长 prompt 不截断** | 每轮反馈是结构化长文，几千字起步 | 贴 5000 字看是否完整进输入框 |
| 6 | **有持久工作区（加分项）** | 官方项目/知识库是对抗上下文压缩最有效的手段 | 看是否有 Projects / 知识库 / 自定义指令 |

**关键提醒**：上下文窗口大 ≠ 长对话连贯。多个来源一致指出：**上下文窗口够大时，仍会在接近上限时做摘要或截断**；而「在长对话里跨多个事实推理」的准确率会明显下滑——哪怕窗口还没满。星舵的用法恰好是「长对话里反复引用早期精确事实」，所以第 1、2 条比「多大窗口」重要得多。

---

## 二、「断片」的三种机制（别混为一谈）

| 机制 | 表现 | 触发条件 | 能不能靠换平台解决 |
|---|---|---|---|
| **截断** | 最早的对话整段消失 | 超出上下文窗口 | 能。换大窗口平台可延后，但只是延后 |
| **摘要/压缩** | 大意还在，**精确措辞没了**（变量名、命令、编号记不准） | 接近窗口上限，平台自动压缩 | 部分能。持久工作区能缓解；纯窗口大小不能 |
| **中间位置注意力衰减** | 还没到上限，埋在中间的细节就已经想不起来 | 对话变长就发生 | **不能**。这是模型侧行为，换平台只是幅度不同 |

> 星舵的第一条硬规则「不新开对话」就是针对这三条：平台一旦压缩或截断，新对话既接不上历史，也救不回被压掉的部分。
> **星舵真正的护栏不在平台，在落盘**：`runtime/wa/turn-<n>.md` 存每轮原文，关键结论写进项目文件（契约/决策），下一轮只把「当前阶段需要的结论」贴回去。这样即使平台断片，事实也不会丢——只是要多贴一次。

---

## 三、平台分级

> 置信度说明：**实测** = 本机跑通；**多源一致** = 3 个以上公开来源指向同一结论；**单一来源** = 只有一个来源提及，待验。

### A 档 · 推荐优先

| 平台 | 记忆表现 | 依据 | 置信度 |
|---|---|---|---|
| **ChatGPT** | 单对话连贯性好；有 Projects 持久工作区（文件+指令+会话历史） | 本机 14 轮循环实测跑通；多源称记忆功能好 | **实测** |
| **Claude** | 「多轮长会话不容易跑偏」；Claude Projects = 独立工作区（知识库 + 自定义指令 + 跨对话记忆 + 200K 上下文 + 自动 RAG 扩展） | 多源一致 | 多源一致 |
| **Kimi** | **长对话连贯性口碑最好的一档**：连续 12 轮后仍能准确引用第 3 轮的变量名；超长上下文 + 文件知识库 | 多源一致 | 多源一致 |
| **DeepSeek** | 单对话记忆强、长会话逻辑连贯；超长会走「摘要延续」，精确措辞衰减 → **必须配合落盘**。2026-10-29 本机实测全链路跑通（bind/send/wait/read），连续两轮给出可执行的架构裁定 | 本机实测 | **实测** |
| **Gemini** | 上下文窗口最大（1M~2M tokens）；**大窗口≠长对话连贯**，跨多跳事实会衰减。2026-10-29 本机实测全链路跑通，且**给出两条反对意见**（锚点太宽松、等待缺第三判据）——不是一味附和，评审价值高 | 本机实测 | **实测** |

### B 档 · 有条件可用

| 平台 | 记忆表现 | 用法建议 | 依据 |
|---|---|---|---|
| **豆包** | 多轮长对话不掉线、不失忆；中文口语化交互好 | 适合做轻量规划/中文文案；严谨性与代码能力弱于第一梯队 | 多源一致 |

### 排除档 · 不适合当星舵的规划者

| 平台 | 排除理由 |
|---|---|
| **Perplexity** | 定位是检索型，「创意和长对话较弱」——长对话是它的弱项 |
| **秘塔** | 连贯性一般，重心在搜索 |
| **灵光** | 偏简短对话，长对话连贯性一般 |
| **文心一言 / 讯飞星火** | 多轮一致性评分偏低；长会话「卡顿明显」，对话体验不稳定 |
| **通义千问** | 长会话卡顿明显，且「新对话清空历史」与星舵纪律相冲 ⚠️（单一来源，待验） |

> **判断不确定时的默认选择**：国内直连走 **DeepSeek**，能到海外走 **ChatGPT**——两者都已全链路实测。
> 全程只用一个对话，规划质量直接决定完成水平：这是推荐顺序的依据，不是兼容性排序。

---

## 四、接入一个新平台（六步）

星舵的平台差异全部收在 `scripts/platforms.py`，主流程 `wa.py` 不含任何平台专属选择器。加新平台不用改主流程。

> **纪律前提**：全程**不靠截图判 DOM**（硬规则 3）。截图只用于「给人看的最终核对」，
> 一切判定依据来自 `evaluate` 返回的 JSON 事实。「工具此刻报的数 ≠ 事实」——
> load 事件、bsk fill 自校验、innerText、加载中的 `msg_count=0`，四个变种都已实测过。

### Step 1 · 建一个对话，拿地址

**第 1 轮（推荐）**：`wa.py bind --new --platform yourkey` 直接从平台新对话入口开
（先在 `platforms.py` 里把 `new_chat_url` 填对）。**沿用已有对话**：手动打开目标对话复制地址。
不能是首页/新对话页（新对话页没有会话 id，不能绑；发第一条后平台会自动跳转出 id）。

### Step 2 · 页面侦察：怎么分析一个陌生的对话页

目标是在写任何平台定义**之前**，用观察拿到六类 DOM 事实。全部用 `bsk evaluate` 跑，
输出 JSON——**看数据，不看长相**。每条命令都能原样复制。

**① 找编辑器（输入框）**：

```js
// bsk evaluate 包在 IIFE 里跑；这里给核心片段
[...document.querySelectorAll('[contenteditable="true"], textarea')].map(e => ({
  tag: e.tagName, id: e.id, cls: (e.className||'').toString().slice(0,80),
  ph: e.getAttribute('placeholder') || ''
}))
```

判别：有 `placeholder` 的通常是真编辑器；页面里同时有搜索框/侧栏输入时会出多个，**发一条字后重跑**，字数变多的那个才是。

**② 判发送键 + 条件渲染**：

```js
[...document.querySelectorAll('button,[role="button"],div[class*="send"],div[class*="button"]')]
  .filter(e => /发送|send|提交|submit/i.test((e.getAttribute('aria-label')||'') + (e.className||'').toString()))
  .map(e => ({ tag: e.tagName, label: e.getAttribute('aria-label'), cls: (e.className||'').toString().slice(0,80) }))
```

**空输入时跑一次、输入文字后再跑一次**：两次结果集不同 = **条件渲染**（Gemini 实测：空态时那个位置是麦克风），
必须在平台定义里写 `send_requires_text: true`，且「写完没出现发送键」必须判失败。再看空态类：
文字输入前后 `className` 里有哪个类消失了（Gemini 是 `ql-blank`）→ 那是 `blank_class`。

**③ 定位用户消息节点（最可靠的一招）**：发一条**已知文字**的消息（如「锚点探针9x7」），然后：

```js
[...document.querySelectorAll('*')].filter(e => e.children.length === 0 && (e.innerText||'').includes('锚点探针9x7'))
  .map(e => { let n = e, chain = []; for (let i=0;i<4 && n;i++){ chain.push(n.tagName+'.'+((n.className||'').toString().split(' ')[0]||'')); n = n.parentElement; } return chain.join(' < '); })
```

打出的是「文本节点 → 上溯 3 层」的容器链。**含这句字的容器 = 用户消息节点**；回复渲染完后，同一层级的、不含用户文字的兄弟节点 = **助手消息节点**。

**④ 数一数三层消息 + 找轮次身份**：拿到候选选择器后，对比 `querySelectorAll` 的数量与页面肉眼可见的条数。同时看每个消息容器（或其祖先）有没有稳定 id（16 进制 / `data-xxx-key`）——有就是现成锚点（Gemini 的 `conversation-container#<16hex>`、DeepSeek 的 `data-virtual-list-item-key` 都是这样找到的）。

**⑤ 找滚动容器 + 判虚拟化**：

```js
[...document.querySelectorAll('div')].filter(e => e.scrollHeight > e.clientHeight + 50 && e.clientHeight > 200)
  .map(e => ({ cls: (e.className||'').toString().slice(0,60), sh: e.scrollHeight, ch: e.clientHeight })).slice(0,5)
```

**虚拟化判定**：滚动前后各数一次消息节点——数量变了 = 虚拟列表（DeepSeek/Kimi 实测），
计数口径立刻降级为「只做同一页面同一时刻的前后对比」，不许跨会话比计数。

**⑥ 标定「生成中 / 完成」标志**：发送一条长问题（让它生成 30 秒以上），生成中跑②的按钮清单找「停止/stop」；
结束后再跑一次，**出现的新按钮**（重新生成 / 语音 / 发送键回归）就是完成标志。两个标志都要进 `sel.stop` 与探针。

**顺手记录的坑位**（写进平台 `notes`）：弹窗/遮罩节点（`[role="dialog"]`、限流 modal）、
恒空容器（Kimi 的 `message-list-container` 教训：存在但永远空，拿它计数 = 全程 0）、折叠区（「已思考」不能算正文）。

### Step 3 · 加平台定义

在 `scripts/platforms.py` 的 `PLATFORMS` 里加一项：

```python
"yourkey": {
    "label": "显示名",
    "home": "https://...",
    "verified": False,          # 实测回填后改 True
    "notes": "实测日期 + 注意事项",
    "url_pattern": r"/(chat|conversation)/[0-9a-fA-F-]{8,}",   # 判定「已有对话」
    "url_hint": "地址形态说明，写给人看",
    "write_method": "paste",    # exec=execCommand；paste=剪贴板事件；native=真实键盘（Quill 类必须）
    "write_allowed": [],        # 实测过的**真实写**路径白名单；空 = 禁止真写（闸门会拦）
    "new_chat_url": "",         # 新对话入口；空 = 用 home（注意 home 可能被重定向回最近对话）
    "blank_class": "",          # 「编辑器为空」的平台判定类（Quill: ql-blank）；空 = 没有这条事实
    "send_requires_text": False,# 发送键是条件渲染：没内容时页面上根本没有它
    "anchor_box": "",           # 轮次容器（单个选择器）：其 id 是现成的轮次身份/锚点
    "send_method": "click",     # 发送方式：click=点发送键；enter=编辑器持焦点按 Enter
    "regen_selector": "",       # 「重新生成」节点声明（纯 icon 按钮没有 aria-label 时必配）
    "prefer_marker": False,     # True = 正文区混着 UI 文案时按标记切
    "markers": ["助手"],          # 正文中「回复从这里开始」的标记
    "tail_junk": [],              # 正文尾部要切掉的 UI 文案
    "user_msg_regex": "",         # 正则源串，用于数用户消息；留空则用总消息数
    "sel": {
        "editor":  ["..."],       # 输入框候选，按优先级
        "send":    ["..."],       # 发送按钮候选
        "stop":    ["..."],       # 「生成中」标志
        "messages":   ["..."],    # assistant 消息节点
        "msg_containers": ["..."],# 用户+助手 全部消息节点
        "alert":   ["..."],       # 页面级报错
        "speech":  ["..."],       # 空闲态标志（语音按钮）
        "scroll":  ["..."],       # 消息滚动容器
        "read_ignore": [],        # 读正文时剔除的噪声子树（推荐提问/操作栏渲染在消息节点内部时必配）
    },
},
```

### Step 4 · probe 实测，回填选择器

```bash
python scripts/wa.py probe --platform yourkey
```

看返回的 JSON：

| 字段 | 期望 | 不对就改什么 |
|---|---|---|
| `editor` | `true` | `sel.editor` 加候选 |
| `editor_len` | ≥0 | 同上 |
| `msg_count` | >0（有历史对话时） | `sel.msg_containers` 或 `user_msg_regex` |
| `last_len` | >0 | `sel.messages` 或 `markers` |
| `generating` | 发送中为 `true` | `sel.stop` |
| `can_send` | `true` | `sel.send` |
| `editor_blank` | 空输入为 `true` | `blank_class`（有平台事实才强制） |
| `asst_keys` / `msg_keys` | 非空 | 锚点身份缺失，回 Step 2 ④ 找稳定 id |

**回填后把 `verified` 改成 `True`，并在 `notes` 写上实测日期。**

### Step 5 · 标定「等待 + 第一时间读取」的信号

wait 不是固定 sleep，是一组**必须逐个实测**的信号。标定动作：

1. **发送前存 before 快照**：`probe` 的 `msg_count` / `url` / `last_len` / `asst_fps` / `msg_fps`，
   原样落盘——这是之后一切「增量」的基准。**没有 before 就没有 delta**。
2. **点发送后每 2 秒 probe 一次**，记录各字段的先后变化：容器先建还是回复先到（Gemini：容器先 +1）、
   URL 变不变（新对话首轮会跳到真实地址）、`last_len` 是不是单调增长（同容器流式补全 = 锚点集合差恒 0，
   只能靠 `last_len` 增长作证，即 `growth` 见证）。
3. **完成三见证**（全满足才判「回复完成」）：① 「生成中」标志消失；② 末条正文长度**连续两次采样一致**；
   ③ 完成标志出现（重新生成 / 语音键 / 发送键回归）。单看一条会误判：文本稳定可能只是卡在长句中间。
4. **第一时间读取**：三见证一满足就 `read`，**不要加固定等待**——读取落盘的时机就是「刚稳定」，
   晚了可能被平台折叠/改写。读到的原文立即落 `turn-<n>.md`，不改写。
5. **把每条标定结论回填 `platforms.py` 的 `notes`**（带实测日期）。换平台前不重标 = 口径漂移。

> 判定实现在 `platforms.anchor_delta`（锚点）+ `confirm.reply_grew`（growth）+ wait 的文本稳定采样，
> 全是纯函数、离线可测；新平台只需要回填事实，不需要新写判定逻辑。

### Step 6 · 跑通一轮再当主力

```bash
python scripts/wa.py bind "<对话地址>" --platform yourkey
python scripts/wa.py send --platform yourkey --text "回复这三个字：收到"
python scripts/wa.py wait --platform yourkey --timeout 300
python scripts/wa.py read --platform yourkey
```

六步全绿，再去测记忆（`templates/web-ai-eval.md`）。

### 附录 · 已实测平台事实（2026-10-29）

**DeepSeek**（`platforms.py` 里 `verified=True`，`write_method="exec"`）

| 项 | 实测事实 |
|---|---|
| 地址 | `https://chat.deepseek.com/a/chat/s/<uuid>`（`url_pattern` 只认带 `/a/chat/s/` 的会话地址） |
| 编辑器 | `textarea`，placeholder「给 DeepSeek 发送消息」。**`innerText` 恒为空，必须读 `value`**——用 `innerText` 判长度会恒得 0，把「写入成功」误判成「一个字没写」 |
| 写入 | `exec`：`el.select()` + `document.execCommand('insertText')`；兜底用原生 `value` setter + 派发 `input` 事件。**不能对 textarea 用 `Range.selectNodeContents`**（textarea 无子节点） |
| 发送键 | `div.ds-button--primary.ds-button--circle`；空输入时带 BEM 变体 `ds-button--disabled`（**双横线**，不是 `is-disabled`） |
| 助手正文 | `div.ds-assistant-message-main-content` |
| 全部消息 | `div.ds-message` |
| 用户消息 | `div.ds-message:not(:has(.ds-assistant-message-main-content))` |
| 滚动容器 | `div.ds-virtual-list`（**虚拟列表**，只渲染视口附近） |
| **消息身份** | 每条消息外层 `div[data-virtual-list-item-key]`，值形如 `-2` / `2` / `-4` / `4`（用户负键、助手正键）。**这是最硬的锚点**：正文折叠、markdown 重排、两条回复正文相同都不受影响 |
| 生成中标志 | 「停止生成」按钮存在 |

> 虚拟列表的实际后果（实测）：发送前 `reply_count=2`，回复 2597 字渲染完成后仍为 `2`
> ——旧气泡被卸载，净数量持平。`msg_count` 甚至从 4 掉到 3（`delta=-1`）。
> **判增量只能靠消息 ID / 正文指纹的集合差，不能数节点。**

**豆包**（2026-10-29 实测，`verified=True`，`write_method="exec"`，`send_method="enter"`）

| 项 | 实测事实 |
|---|---|
| 地址 | `https://www.doubao.com/chat/<数字id>`；首页 `/chat/` 就是新对话入口（无 id），发首条后自动跳转 |
| 编辑器 | **Tiptap ProseMirror**（`div.tiptap.ProseMirror`）：`execCommand('insertText')` **有效**（与 Gemini 的 Quill 相反，字进 model）；**`bsk fill` 键盘模拟无效**（报焦点被移走，先 click 聚焦也一样） |
| 发送 | **不能点发送按钮**：`#flow-end-msg-send` 存在但点击无反应、且点击会把焦点移走。唯一实测路径 = exec 写入后**保持编辑器焦点 → press Enter**（`send_method=enter`） |
| 消息节点 | 全平台最规范的 data-testid 体系：用户=`send_message`、助手=`receive_message`、正文=`message_text_content`、总消息=`union_message`、列表=`message-list` |
| **噪声** | 「推荐提问」渲染在回复节点**内部**（`suggest_message_list`）→ `sel.read_ignore` 读时剔除；探针文字会出现在**侧栏会话标题**——找消息必须限定 message-list |
| 生成中标志 | **没有停止按钮可抓**；完成标志 = `message_action_regenerate` 出现（纯 icon 按钮，无 aria-label → 必须配 `regen_selector`） |
| 锚点 | `union_message` 无每条唯一 id（`data-target-id` 是固定值）→ 锚点走正文指纹 |

**Gemini**（`platforms.py` 里 `verified=True`，`write_method="native"`）

| 项 | 实测事实 |
|---|---|
| 地址 | `https://gemini.google.com/app/<16 位 hex>`；新对话页是 `/app`（**无 id，不能绑**），发第一条后平台自动跳转 |
| **新对话入口** | `new_chat_url = https://gemini.google.com/app`。**不能用 home**：`gemini.google.com` 在登录态下重定向回最近一次对话，等于没有新开（2026-10-29 实测）。落点必须是「无 id + 0 条消息」，被重定向回已有对话 bind 会直接拒绑 |
| 编辑器 | **Quill**：`rich-textarea div.ql-editor`（contenteditable，placeholder「为 Gemini 输入提示」） |
| **空态** | 有字时 `ql-editor` 类上的 `ql-blank` 会消失。**`innerText` 不可信**：`execCommand('insertText')` 能让 DOM 有字而 Quill model 为空（`ql-blank` 还在） |
| 写入 | **必须 `native`（真实键盘 / bsk fill）**。exec 写入是静默失败：字进 DOM、发送键不渲染、点发送无反应 |
| 发送键 | `button[aria-label="发送"]`，**条件渲染**：空输入时页面上没有这个按钮（那个位置是麦克风） |
| 助手正文 | `model-response`（正文节点 `.model-response-text`） |
| 用户消息 | `user-query` |
| **轮次身份** | 一问一答共处 `div.conversation-container`，其 `id` 是 16 位十六进制串（实测 10 条全唯一）→ `anchor_box` |
| 滚动容器 | `infinite-scroller.chat-history`（**懒加载**：只挂载视口附近） |
| 生成中标志 | 「停止」按钮存在 |
| 导航 | **`load` 事件永远不触发**（长轮询）：`--wait-until load` 30 秒超时返回非 0，但页面早已可用 → 用 `navigate_soft` |

> 三条实测教训：
> ① exec 写入后 `innerText` 有 15 字而 `ql-blank` 仍在 —— 「看着写好了、其实没写」；
> ② 发送键不存在时**不要去点**，那是「平台没接受这份输入」，不是「按钮暂时禁用」；
> ③ 历史懒加载：重载后只渲染尾部约 10 轮，**跨会话比计数/锚点没有意义**，
>    只能比「同一页面同一时刻的前后」。**打开旧对话时消息区先空转几秒再渲染**
>    ——probe 读到的 `msg_count=0` 可能是「加载中的假空」（「工具自证≠事实」第四变种，
>    见 lessons `L-GEMINI-FOREIGN-CONV-ff592c`）。bind 已内置稳定等待后再做归属判定。

---

## 五、跨平台通用经验

1. **正文区混着 UI 文案**：ChatGPT 的正文区会带上「重新生成」「复制」等按钮字，直接读长度会误判「还在生成」。解法是 `prefer_marker=True`，先按「ChatGPT 说：」这类标记切出正文，再切掉尾部 UI 文案。
2. **虚拟化渲染**：长对话不滚到底不渲染，读取前必须先滚到底并等一拍（`wa.py` 内部已做：600ms → 1200ms → 2500ms 三次重试）。
3. **长文本写入可能超时但其实已写入**：不要盲目重写，先读输入框字数核对，不符才重写一次。
4. **手工会话会污染状态**：不要用别的工具手工起会话去操作同一个对话，否则星舵的绑定会指错页面。
5. **两个入口并存**：同源的两份脚本（如同一个页面载荷有两个副本）会互相抢注，版本不一致时静默按错口径跑。**要比哈希，不只比版本号。**
6. **平台选择器会变**：页面改版后 `probe` 会突然报 `editor: false`。这不是平台「断片」，是 DOM 变了——回填 `sel` 即可，与记忆能力无关。

---

## 六、选型建议（按场景）

| 你的情况 | 建议 |
|---|---|
| 在国内直连 | **DeepSeek**（本机全链路实测；记忆强，配合落盘） |
| 网络能到海外 | **ChatGPT**（本机实测，选择器最精确、完成信号最全） |
| 想要长对话最稳 | **Kimi**（本机实测，长对话连贯性好） |
| 主要是长文档分析 | **Gemini**（本机实测，窗口大）——但别指望它记住前 20 轮的细节 |
| 落地成本最低 | **豆包**（本机实测，发送只能靠回车） |
| 想用 Claude | 可用，但 `platforms.py` 里仍是候选选择器推断，**写入前必须 `probe` 标定一次** |

> 「本机实测」指 bind / send / wait / read 全链路跑通，见 `scripts/wa.py platforms` 的 `verified` 字段。
> 实测与否是标定进度，不是能不能用：未标定的平台读取先试一次，写入被闸门拒绝，标定后即可。

---

## 七、星舵自己的对冲手段（不依赖任何平台）

无论选哪个平台，这三件事都该做，它们是星舵不「断片」的真正保障：

1. **每轮原文落盘**：`runtime/wa/turn-<n>.md` 存 AI 原文整段，不摘录。
2. **关键结论写进项目文件**：契约、决策、裁定结论落在 `docs/` 里，那是平台上下文之外的地方。
3. **只发当前阶段需要的上下文**：下一轮不贴全历史，只贴「这一轮要依据的结论 + 最近一次裁定」。这既是省 token，也是防止上下文被无关内容挤掉。

> 平台会压缩、会摘要、会断片。**星舵的正确设计假设是「平台会忘」，然后用落盘把它补回来。**
