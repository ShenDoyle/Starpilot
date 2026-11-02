# 执行契约 · 动作卡（L2 颗粒度层）

> 这份文件解决一件事：**同一个任务，不同 agent 执行出来必须是同一个样子。**
> 做法不是"写得更详细"，而是把每个动作钉成四段：前置断言 / 唯一通路 / 通过判据 / 失败分支。
>
> 读法（硬规定）：
> 1. 执行任何动作前，**只读对应那张卡**，不要凭 SKILL 段落的叙述即兴发挥。
> 2. 四段缺一不可。**卡里没写的做法 = 禁止**（不是"没禁止所以可以"）。
> 3. 同一动作连续失败 2 次 → 不许试第三种做法（硬规则 4），按卡的失败分支走。
> 4. 判据必须是**机器可判的字段**，不是"看起来正常"。判据说不出字段名 = 没判据。

---

## 1. 通用条款（所有卡都适用）

### 1.1 动作白名单：这 6 类必须走脚本，禁止手搓

| 动作类 | 唯一入口 | 为什么不能手搓 |
|---|---|---|
| 接入对话 | `wa.py bind` | 手搓 bsk 会跳过窗口归属校验、单窗口纪律、租约 |
| 写入并发送 | `orchestrator.py loop` / `wa.py send` | 手搓会跳过写闸门、幂等守卫、发送三重判据 |
| 等回复 / 读回复 | `wa.py wait` / `wa.py read` | 手搓等法会数错（思考区、虚拟列表、假生成） |
| 卡住判定 | `wa.py triage` | 手搓判断 = 猜；triage 有九态口径 |
| 状态探测 | `wa.py probe` | 手搓 DOM 会漏 URL 漂移和 window 归属 |
| 清空输入框 | `wa.py clear` | 手搓清空会触发"追加而非替换"的受控编辑器坑 |

**裸调 bsk 子命令 = 违规**（会跳过 `bskcompat` 的 cooperate sync，直接报 `shared page control` 错）。
唯一例外：`references/builtin-mode.md` 的 builtin 通路，按该文件的分步规程手动执行，并在日志里标明 `backend=builtin`。

### 1.2 禁止清单（黑名单，做了就是失败）

| 禁止动作 | 后果 | 正确做法 |
|---|---|---|
| 截图判断"发出去了没" | 截图看不到服务端状态 | 用 `probe` 的 `msg_count`/`reply_count` 增量 |
| 发送失败后狂点重发 | 重复提问，污染对话 | 先 `triage` 判 `send_not_taken` 再决定 |
| 回复慢了就刷新页面 | 会杀掉正在生成的回复 | `triage` 判 `still_generating` 就等 |
| 选择器失效就换平台 | 丢失上下文，且问题没解决 | 走 `runbooks/dom-drift.md` 重标定 |
| 遇到验证码就换 IP 硬刚 | 触发更严风控，可能锁号 | 走 `runbooks/anti-bot.md`，该找人时找人 |
| 为了通过测试而放宽判据 | 假成功，比失败更贵 | 判据只能收紧，放宽要用户裁定 |
| 用 `|| true` / 吞异常"让它过去" | 现场失真，后续全错 | 失败就按分支走，如实报 rc |

### 1.3 每步必落盘（缺一个 = 这步没做）

任何写类动作（send / clear / approve / close / stage）完成后，state 或 journal 必须留下：

| 字段 | 含义 | 谁写 |
|---|---|---|
| `url` + `conv_id` | 动作发生在哪条对话 | state（bind/send 自动） |
| `prompt_sha256` / `text_len` | 发出去的到底是什么 | state（send 自动） |
| `send_base` 快照 | 发之前的基线（判增量用） | state（send 自动） |

手动通路（builtin）也要落三样：正文文件路径、`sha256`、发送确认判据结果。

### 1.4 判据反例（这些不是判据）

- ❌「输入框清空了」——Kimi 限流时会清空但拒绝建会话。
- ❌「点了发送按钮，返回 ok」——点击成功 ≠ 服务端收到。
- ❌「页面出现新气泡」——可能是历史消息重新渲染（虚拟列表）。
- ❌「等够了 60 秒」——时间不是证据。
- ✅「`msg_count` 增量 ≥1 **且** `conv_id` 与绑定一致 **且** 正文指纹命中」。

---

## 2. CARD-bind · 接入网页版 AI 对话

**前置断言**
- [ ] 已定平台（`references/web-ai-platforms.md` 选型表；不随手挑）
- [ ] 已登录（未登录时 `probe` 会 `login_required`，别在这之后才发现）
- [ ] 整个项目**只允许一条活跃对话**；要新开必须走 `bind --new` 且仅限第 1 轮

**唯一通路**
```bash
python scripts/wa.py bind <对话URL>            # 已有对话（缺省）
python scripts/wa.py bind <首页URL> --new      # 仅循环第 1 轮
python scripts/wa.py bind <URL> --reuse-existing  # 明确沿用同一条
```

**通过判据**（三条全中才算绑上）
- `probe` 返回 `editor: true` 且 `editor_len` 可读
- `conv_id` 与地址一致（`url_mismatch`/`conv_mismatch` 必须为 false）
- `window.verified == "verified"`（`unknown`/`misplaced` 都不算绑上）

**失败分支**
| 现象 | 含义 | 下一步 |
|---|---|---|
| rc=6 `WINDOW_UNVERIFIED` | 归属未知/开错窗口 | `wa.py window status` 看三态 → 人工确认后 `window release --confirm` → 重新 bind |
| `editor: false` | 平台改版，选择器失效 | → `runbooks/dom-drift.md`（不是换平台） |
| `login_required` | 掉登录态 | 停下找人登录；不要用脚本模拟登录 |
| `duplicates` 非空 | 同浏览器多份扩展 | 让人在扩展页删到只留一份，再 `doctor` 确认 `ok:true` |
| 地址跳到 `/` 或 `/new` | 页面重定向 | 立刻停，回绑原地址；不要在新页面继续发 |

---

## 3. CARD-send · 写入并发送

**前置断言**
- [ ] 上一条回复已确认结束（`awaiting_reply` 为 false；否则 `wait` 先跑完）
- [ ] 输入框干净（`editor_len == 0`；有残留先 `clear`）
- [ ] 本轮内容已落盘成文件（`--file`，不接受临时拼接的长字符串）
- [ ] 有编排 run 时：**必须走 `loop`**，不许直接 `wa.py send`（会被 rc=7 熔断）

**唯一通路**

有 run（正常形态）：
```bash
python scripts/orchestrator.py loop --once --file <反馈文件>
# 内部：dry-run 计划 → 写闸门 → （需批准时 approve）→ 真实写 → 三重确认
```

无 run（builtin 手动形态，见 `references/builtin-mode.md`）：
```bash
python scripts/wa.py send --file <正文文件>
```

**通过判据**（三重，缺一不算成功）
- `msg_count` **增量 ≥1**（不是总数，是增量）
- `conv_id` 与绑定一致（地址漂移守卫为 false）
- 正文指纹命中（`needle` 判据） 或 `reply_count` 增量 ≥1

**失败分支**
| 现象 | 含义 | 下一步 |
|---|---|---|
| rc=7 `ORCHESTRATOR_REQUIRED` | 有 run 却直发 | 改走 `loop --once`；**不许**用 `STARPILOT_ALLOW_DIRECT_SEND` 绕过 |
| rc=6 `WINDOW_UNVERIFIED` | 归属未确认 | CARD-bind 的失败分支 |
| rc=9 | 平台锁被别的任务占 | 等对方收口；确认是残留锁才 `plock --release` |
| rc=8 `WRITE_BLOCKED_*` 白名单 | 写入路径未实测 | 走 `runbooks/dom-drift.md` 标定写路径后再写 |
| `fill_focus_lost` / 写入字数不符 | 受控编辑器抢焦点/超时 | 先 `probe` 读字数：**已写入就别重写**；不符才重写一次 |
| rc=14 `send_not_taken` | 字还在框里，消息数没涨 | 先 `triage` 看有无弹窗/遮罩，再按分支处置，**不连点** |
| 两条相同提问 | 未确认上轮就重发 | 停，告知用户，按 §4.1 校验；不许删消息假装没发生 |

---

## 4. CARD-wait-read · 等回复与读取

**前置断言**
- [ ] 发送已通过三重判据（不是"点了按钮"）
- [ ] state 里有 `send_base` 快照（没有就先 `probe` 补一份基线）

**唯一通路**
```bash
python scripts/wa.py wait --timeout 900 --interval 3     # 三时钟：首字 / 静默 / 总超时
python scripts/wa.py read                                 # 只读末条回复
python scripts/wa.py triage [--relocate]                  # wait 判死时用它定位
```

**通过判据**
- `wait` rc=0 且 `reply_count` 增量 == 1（**正好 1**，不是 ≥1）
- `read` 返回非空且首尾完整（`truncated: false`；截断不重发，报给网页版 AI）
- 读取正文**排除思考区**（DeepSeek 只取 `div.ds-assistant-message-main-content`）

**失败分支**
按 `triage` 的 rc 走 CARD-triage 的对应行，**不要在这里即兴**：
rc=10 找人 / 11 rebind / 12 停一次再发 / 13 复探或重标定 / 14 查发送 / 15 换平台或冷却。

---

## 5. CARD-triage · 卡住分诊

**前置断言**
- [ ] 已经卡住了（`wait` 超时或异常），**不是**"预防性探查"
- [ ] 不刷新、不重发、不关窗口——triage 是纯只读

**唯一通路**
```bash
python scripts/wa.py triage                # 九态判定 + 结构化 hint
python scripts/wa.py triage --relocate     # 选择器失效时用备用读法复探（仍只读）
python scripts/wa.py triage --reload       # 最后手段：仅确认不在生成态时才刷新
```

**通过判据**
- 输出里 `stage` 落在九个 `STALL_*` 之一，且 `hint` 指向一个具体动作
- 拿到 rc 后**必须**能对上第 8 节 rc 表的某一行；对不上 = triage 缺态，登记 lesson

**失败分支**
| 现象 | 含义 | 下一步 |
|---|---|---|
| rc=4 `probe_failed` | 页面探不通 | `doctor` 看会话/实例；会话没了不是对话没了 → `bind` 同 url |
| triage 自己也报错 | 扩展/通信层坏了 | `doctor` → 仍不通按 `runbooks/window-session.md` 重启会话 |

---

## 6. CARD-probe · 状态探测

**前置断言**
- [ ] 有绑定（state 里有 `url`）；未绑定跑 probe 只会得到误导性的空值

**唯一通路**
```bash
python scripts/wa.py probe          # 结构化页面状态
python scripts/wa.py doctor         # 实例/会话/窗口/租约体检
```

**通过判据**
- probe 返回的 `url` / `conv_id` / `msg_count` / `reply_count` / `editor_len` / `generating` 都有值
- 任何字段为 `null` 都要当成**未知**，不许当 0 用

**失败分支**
- `url_mismatch` / `conv_mismatch` → 页面漂移，按 `runbooks/context-loss.md` 的 rebind 流程
- `platform_verified: false` → 该平台只允许只读，禁止真实写（写闸门会拦）

---

## 7. CARD-clear · 清空输入框（恢复态）

**前置断言**
- [ ] 确认输入框里的是**上一次没发出去的草稿**（`triage` 判 `send_not_taken`），不是用户手打的内容
- [ ] 不在生成态（生成中清空会干扰页面）

**唯一通路**
```bash
python scripts/wa.py clear
# 手工兜底（受控编辑器）：focus → Ctrl+A → Backspace，再 probe 读 editor_len
```

**通过判据**
- `probe` 的 `editor_len == 0`

**失败分支**
- 清不掉 → 用 Range 圈定 + `execCommand('insertText')` 覆盖（Kimi 已按此配置，见 `web-ai-platforms.md`）
- 仍清不掉 → 停，别刷新（刷新会连带丢草稿证据），报给用户

---

## 8. rc 码全表（唯一权威，其他文档只许引用不许各写一套）

| rc | 来源 | 含义 | 谁该动 |
|---|---|---|---|
| 0 | 全部 | 成功 | — |
| 1 | orchestrator | 一般失败（看 `reason`） | agent |
| 2 | wa/orchestrator | 尚未绑定对话 | agent：先 bind |
| 4 | wa triage | 页面探不通 | agent：doctor → bind |
| 5 | orchestrator | 守卫拒绝（PLAN/STAGE/BLOCKED 类，看 `reason`/`error_code`） | agent 按 reason 分支 |
| 6 | wa / 闸门 | 窗口门拒 / 写闸门拒 / 裁定无效 / 白名单外写路径 | agent：看具体 code |
| 7 | wa send | 有 run 却直发（`ORCHESTRATOR_REQUIRED`） | agent：改走 loop |
| 8 | wa send | 写路径不在平台白名单 | agent：重标定写路径 |
| 9 | wa | 平台占用锁被别人持有 | agent：等；残留锁才 plock |
| 10 | wa triage | 风控 / 验证码 / 掉登录 | **人**：`runbooks/anti-bot.md` |
| 11 | wa triage | 对话漂移（不在绑定对话上） | agent：`runbooks/context-loss.md` |
| 12 | wa triage | 假生成（流死了 UI 没复位） | agent：停一次再发 |
| 13 | wa triage | 选择器失效（DOM 改版） | agent：`runbooks/dom-drift.md` |
| 14 | wa triage | 发送没生效（字还在框里） | agent：查遮罩/弹窗后重发一次 |
| 15 | wa triage | 平台静默拒绝（限流） | agent：换平台或冷却 |

> 这张表是**唯一权威**。改 rc 语义 = 契约级变更：同步 `scripts/wa.py` 的 `STALL_RC`、本表、SKILL §6，并跑 `scripts/contract_lint.py`。
