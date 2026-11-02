# 更新日志

版本号只从 `SKILL.md` front-matter 读。对外版本从 `0.1.0` 起算；开发期的内部编号（→ 2.3.4）是当时的迭代序号，不对外。

## 0.1.6（2026-11-02，窗口/tab 资源治理闭环 + 介入语义修正）

### 变更
- **tab 生命周期闭环**：heal 复用优先（同址标签 → 空白残留 → 最后才 create，不再无条件 tab create 留孤儿）；新增 `reap_own_tabs`（只收自己会话的同地址重复与空白残留；用户窗口标签只报告不动）自动接进 ensure_session，手动入口 `wa.py tab-reap`（--dry-run）
- **介入≠打断**：`WINDOW_BUSY_HUMAN`（rc=9 报人裁决即停）废止；有人值守改为 30s 被占先提醒（`WINDOW_BUSY_NOTICE`，非阻塞）后继续等满 600s 轮换预算（2026-11-02 用户裁定：人工介入是并行监督，星舵持续推进到任务完成）
- **人的想法转达纪律**（SKILL §5.5b）：流程中人提出的想法/意见/疑问必须原样整体转达 webai 裁决，agent 不得自行裁决或只记本地
- **L2 PATH shim**（tools/install_l2_shim.py）：`bsk.exe`→`bsk.bin.exe` + `bsk.cmd`，仅 session start 过闸（无 `STARPILOT_WINDOW_GATE=1` 令牌 → 全局计数，超限拒绝）；wa.py/env.py 直连真身
- `contract_lint` 新增 `no_raw_tab_ops`（wa.py 之外禁裸调 tab create/close）；命令提取支持连字符子命令
- SKILL §5.5 硬边界表扩为窗口/tab 双资源纪律；runbook window-session 新增 §8（tab 生命周期 + 介入语义）

## 0.1.5（2026-11-02，窗口资源治理 v3：轮换协议/窗口组/禁无脑重开/全局看门狗，未单独发版）

### 变更
- 窗口租约（跨任务互斥，不抢占）：`winlease.py` 租约 + journal 组登记 + fencing token + 进程三元组防 PID 复用
- 组窗口复用（`STARPILOT_GROUP`）：组内活会话直接认领，在组窗口建自己的 tab，不新开窗口
- 全局跨任务开窗计数（锁目录 session-starts.json，10 分钟/实例 ≤4 次）
- `reap --global` 看门狗：按 journal 组登记只收本组租约过期会话，无主/别组只报告
- `contract_lint.no_raw_session_start`（开窗唯一入口=wa.py）；删除裸调脚本 send_once.py / tab_clean.py
- `tab_scope_report` dict 形状解析修复（此前永远 unknown，发送屡被拒）

## 0.1.4（2026-10-31，ZCode 宿主服从性针对性优化）

### 变更
- 新增 §2.0 INIT 硬前置门（绑定/需求提交/留档/执行模式四项缺一禁止执行动作）
- 新增 §2.5.1 无人值守异常信号协议（记录→暂停→优先报告，禁止忽略）
- §5.3 窗口纪律：无人值守开窗上限收紧为 2 次/10 分钟（STARPILOT_UNATTENDED=1）；session 结束原因进汇报违例栏
- ORCHESTRATOR_REQUIRED 熔断：工作区有编排 run 时 wa.py send 直接拒绝（rc=7）
- 逃生门 STARPILOT_ALLOW_DIRECT_SEND 单次消费、理由必填、强制落账 P0-ESC 违例条目
- bsk 版本变更首查提示；回执证据段增加「版本核对」行
- pending_gate 未清项两道门（P0 门 + 回执完整性门）接入 orchestrator READY
- templates/feedback.md 首段强制三栏台账对账表
- §5.3bis 宿主适配节（ZCode 实测事实）；lessons 新增 6 条
- bsk 0.4.0 兼容层 scripts/bskcompat.py（cooperate 上下文自动补齐）

### 强制声明 A（交付裁定，2026-10-31 DeepSeek）
0.1.4 的 P0 门为建议性门禁，非机器强制。P0 项无法被「引用处理」时（依赖阻塞/外部等待），agent 当前无豁免路径，可能被卡死或诱导伪造引用。豁免路径（BLOCKED/DEFERRED 分层）为 0.1.5 首项，不得拖延超过一个版本。

### 强制声明 B（交付裁定，2026-10-31 DeepSeek）
逃生门 STARPILOT_ALLOW_DIRECT_SEND 为单次消费、理由必填、强制落账。同进程第二次直发被拒。真实浏览器直发链路未在本版验证（避免制造真实违例），熔断逻辑由单测覆盖。

### 已知限制（0.1.5 待办锁定）
- 多进程 nonce 原子性未证：单次消费用进程内 del environ 实现，单进程宿主模型下充分，写为已知限制不修（DeepSeek 裁定）
- P0-ESC 回执对抗验证未做：与 0.1.5 豁免路径同批交付，不得拆成两个版本
- 回执模板待加「禁止重复粘贴」提示（0.1.5）

## 0.1.5（2026-10-31，IAB 传输层 + 0.1.4 锁定项同批交付）

### 变更
- **传输层抽象**（用户裁定：ZCode 中星舵默认自带浏览器，不依赖 bsk）：
  `STARPILOT_TRANSPORT=iab` 时页面动作走 op 队列（`scripts/transport.py`），
  node 侧 `scripts/iab-worker.mjs` 驱动 ZCode 自带浏览器（Playwright/cua，isTrusted）；
  bsk 变为可选依赖（`STARPILOT_TRANSPORT=bsk` 默认不变）；用户需在自带浏览器登录一次
- pending_gate：**BLOCKED(reason) 豁免路径**——P0 门放行但 BLOCKED 项必须在回执三栏出现（豁免不隐身）
- templates/feedback.md：新增「禁止重复粘贴」提示（0.1.4 交付轮实测踩坑）
- 0.1.4 锁定项兑现：P0 门豁免路径 + P0-ESC 对抗验证（stale-id 测试）同批交付，无版本拖延

### 测试
- 新增 6 条（BLOCKED 豁免/对抗 stale-id/op 队列往返/无 worker 超时/参数翻译/iab 路由短路），全量 356 通过

### 已知限制（0.1.5 实测，诚实申报）
- **IAB 传输的 e2e 部分验证**：op 队列/路由/navigate/fill/evaluate/发送链路已跑通
  （worker 消费 60+ op 零失败，fill 实测到达编辑器）；但「goto 对话 URL 后内容区
  挂载」当晚在实测中失效（msgs=0，侧栏点击不导航，bsk 传输同样命中——判定为
  DeepSeek 平台侧行为，非传输层缺陷）。IAB 传输标记为**实验性**，默认仍为 bsk；
  生产使用前需在平台正常时段重验。
- 等待期间必须由 agent 持续运行 worker（每次 MCP 调用是全新内核）。

## 0.1.6 — 2026-11-02 · 不装 bsk 也能跑：内置浏览器后端 + 首次激活弹窗

很多宿主 agent 自带浏览器，bsk 不该是唯一路径。这一版把「装不装 bsk」变成**用户的一次显式选择**，
并给不装的那条路补齐了完整规程。

**改了什么**

- **首次激活弹窗（SKILL §0.2）**：`env.py --json` 报 `needs_decision: true`（bsk 没有、用户也没选过）时，
  agent 用宿主的按钮弹窗问一次——「安装 bsk（推荐）/ 用我的内置浏览器」，选择经
  `env.py --set-backend bsk|builtin` 记到 `~/.starpilot/config.json`，**只问一次**，可随时改。
- **builtin 后端**（`references/builtin-mode.md` 新）：脚本完全不碰浏览器；绑定 / 发送 / 等待 /
  分诊由 agent 用内置浏览器手动执行，判据（三重发送确认、三时钟、分诊阶梯）与 bsk 模式一致。
  选择器与三时钟参数有了单一出口：`python scripts/platforms.py spec <平台> [--json]`（新 CLI）。
- **`env.py` 后端判定**：backend 取值顺序 = `--backend` 参数 > config > 自动（找到 bsk 即 bsk，
  否则 unconfigured）；`builtin` 下没有 bsk 也算就绪（rc=0）。
- **`wa.py` builtin 守卫**：config 记了 builtin 后，浏览器类命令（bind/send/wait/read/probe/
  clear/triage/doctor/stop）以 **rc=6** 拒绝并指路规程文档；platforms/state/unlock/plock 照常。
  「bsk 没装」仍走各命令自己的报错，守卫不替用户做决定。
- 边界如实写进 SKILL §8：builtin 模式**没有** orchestrator 状态机、写闸门、平台占用锁、
  自动续窗，也不写 `.wa-state.json`（补法：wa.py 加 `--record` 系列）。

测试 356 → 381（新增 `tests/test_builtin_backend.py`，25 条）。基于 0.1.5 的 `STARPILOT_TRANSPORT=iab` 之外补充另一条路：iab 是脚本侧传输层（实验），builtin 是 agent 侧手动规程，两者都不依赖 bsk。

## 0.1.2 — 2026-10-30 · 不再等满 15 分钟：三时钟 + 分诊

0.1.1 修好了「读不到」（实例重复）。这一版修的是**「等太久」**：以前只有一个 900 秒总超时，
等于把「几秒就能判定的异常」拖成「15 分钟后才知道」，人也看不到它卡在哪一步。

**实测打底**：DeepSeek 新对话首字 2–5 秒；开联网搜索会拖到 20 秒以上；`wa.py send` 本身
要 18 秒（写入 + 发送后校验），返回时回复往往已经在流式输出。所以「很久没动静」不是
「模型在思考」，是异常。

**改了什么**

- **三个时钟**（`wa.py wait`）：首字 `--first-token`（默认 45 秒，平台 `ttft_s`）、停滞
  `--stall`（默认 60 秒，`stall_s`）、总超时 `--timeout`（900 秒不变）。到点不硬等，走分诊。
- **分诊阶梯**（`platforms.judge_stall`，纯函数）：风控/覆盖层 → 对话漂移 → 是否真的在生成
  → 回复是否已到 → 选择器是否失效 → 发送是否生效 → 静默拒绝。顺序是「便宜且确定」到
  「昂贵且靠猜」，**不是**一上来就重找元素。
- **`wa.py triage`（新）**：只读命令，任何人（人、另一个 agent、定时任务）在卡住时都能跑。
  输出 stage + evidence + hint，退出码即结论（10 风控/掉登录 · 11 漂移 · 12 假生成 ·
  13 选择器失效 · 14 没发出去 · 15 静默拒绝；9 仍留给平台占用锁）。
- **观察文件** `state.progress.json`：每次探测刷新 `stage` 与 `seen`，只有页面真变了才推
  `progress_at`。不持锁、坏了不抛——它是给旁观者看的事实，不是状态机的一部分。
- **`orchestrator.py status` 新增 `stall`**：读上面那个文件给出 `stalled` 与下一步命令，
  卡住不再只有当事人知道。
- 探针新增事实：全屏覆盖层（面积 + 层级）、验证码 iframe（turnstile/geetest/hcaptcha/
  recaptcha/challenges）、限流文案、掉登录、`doc_len` / `doc_nodes`（判「页面涨了但我们
  读不到」= 选择器失效）。**风控文案只从界面层取**，不扫整页——助手回答里出现「请求过于
  频繁」会被当成风控（真实误报）。

**刻意不做的事**

- **不刷新页面**来「重新找元素」：正在生成时刷新 = 把回复丢掉。要重新定位就用
  `triage --relocate`（备用读法，只读）；`--reload` 是最后手段且只在非生成态，刷完必须
  重走 bind + send。
- **不在没分诊的情况下重发**：限流时重发只会更糟。

测试 294 → 330（新增 `tests/test_stall_triage.py`，36 条）。契约 v1.8（§2.16）。

## 0.1.1 — 2026-10-30 · 读不到回复 / 窗口堆积

现场来自豆包工作机（Chrome）：装好 skill 后跑任务，**总是取不到 GPT 的回复**，而且一次任务开出一堆标签页。查下来是四个独立问题叠在一起，其中三个在代码里。

**根因：同一个 Chrome 装了 4 份 BrowserSkill**（工具栏并排 4 个图标），bsk 因此报 4 个实例。会话跟着某个实例走，读回却可能落到另一个实例的窗口上——发得出去，永远读不到；每次「重试」再开一个新窗口。

**改了什么**

- `wa.py doctor`（新）：一条命令看清在线实例、**重复安装**判定、会话、state 指向。重试之前先看它。
- 实例唯一性：`instance_report` 按 label 判重复（label 为空只算「无法区分」，不当重复，多 profile 照常可用）；`ensure_session` 开窗前拦下并打印修复路径（`chrome://extensions` 删到只留一份）；`WA_ALLOW_DUPLICATE_EXT=1` 可临时绕过。
- `_norm_session` 把 `browser_instance_id` 折进 `browser`：**这是「开很多 tab」的直接原因**——字段名不对，`enforce_single_session` 的过滤条件永远成立，一个旧窗口都收不掉。
- `_first_browser_instance` 改读 `browsers --json`：老代码按表格空格切列，把「0.3.3 京东老浦家」整段当标签，`WA_BROWSER_PREFER` 从没匹配上过。
- 开窗频率上限：10 分钟内第 4 次 `session start` 直接拒，并打印现场。反复开窗不是重试，是环境在反复失效。
- `wait` / `read` 认出「窗口被回收」：连续探测失败且 session 已不存在 → 在同一 `url` 上续窗一次（`--max-recover`，默认 1），失败才报 `session_lost`。以前这种情况会静默等满 15 分钟再报 timeout。
- 弹窗判据改为**可见 + 面积 ≥24px**，并报 `modal_rect`：ChatGPT 页面常驻一个 0×0 的隐形 `[role="dialog"]`，老判据只看节点存在，于是**永远发不出第一句**。
- `SKILL.md`：§4.9 新增「窗口被回收 vs 对话没了」判据表；§5.3 会话纪律补三条；§5.4 安全表补实例唯一性；§6 故障速查补四行。
- 测试 268 → 294（新增 `tests/test_wa_session.py`，26 条覆盖上述四个根因）。

## 0.1.0 — 2026-10-30 · 首次公开发布（预览版）

还不是正式版。第一个打包对外发的版本，能力已经能跑完整项目，接口还会动。

**发布物**

| 包 | 内容 | 给谁用 |
| --- | --- | --- |
| `starpilot-skill-0.1.0.zip` | 完整 skill：`SKILL.md` + `scripts/` + `references/` + `templates/` + `docs/` + `assets/` + `tests/` | 装进 AI 客户端的 skills 目录，让 agent 按 SKILL 的规矩干活 |
| `starpilot-cli-0.1.0.zip` | 完整 CLI：`scripts/` + `references/` + `templates/` + 契约 + `tests/`，不含 `SKILL.md` | 只要命令行，自己写调度脚本 / cron / 别的 agent 调用 |

两个包功能不缺：CLI 包里的脚本本来就是纯标准库，`SKILL.md` 只是给 agent 的指令，少了它命令行照样跑全。

**能力**

- 六环循环：需求建立 → 规划 → 拆步 → 执行 → 如实回报 → 收口，每环有产物模板
- 14 态状态机 + 18 个 orchestrator 子命令；写操作走四步闸门（演练 → 批准 → 真发 → 写后核验），崩溃恢复只复核、绝不重发
- 三种自愈：失败两次 `replan` 换方案、对话漂移 `rebind` 换对话、新歧义 `decide` 留痕；真正需要停下的只剩四种
- 判定「网页 AI 回完了」靠 DOM 锚点（消息 ID > 正文指纹 > 计数），判据 `effective_delta == 1`，不靠截图
- 5 个平台实测定型（DeepSeek / ChatGPT / Kimi / Gemini / 豆包）；新平台跑一次 `probe` 标定即可接入
- `env.py` 环境自举：查 Python、找 bsk、看扩展开关，缺什么装什么
- 268 条单元测试，纯标准库，无第三方依赖

**已知缺口**：升级包没有签名和哈希校验；prompt A/B 只做过 2×1 轮；auto 模式没跑过跨天长任务；Claude 平台未标定。完整清单见 `SKILL.md` §8。

**从开发期版本迁过来**：开发期最后编号是 `2.3.4`，公开版从 `0.1.0` 重算，升级器的「远端 > 本地」判据会认为不必升级。用 `python scripts/upgrade.py --to-latest --force` 重装一次即可，`runtime/`、`state/`、`stage/` 不会被覆盖。