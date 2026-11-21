# 更新日志

版本号只从 `SKILL.md` front-matter 读。对外版本从 `0.1.0` 起算；开发期的内部编号（→ 2.3.4）是当时的迭代序号，不对外。

## 1.9.0（2026-11-21，依赖政策：ego 成为唯一默认/首选，bsk 降级为备用并计划移除）

用户明确裁定后的依赖收敛：**ego-browser（egolite 传输）是默认且首选通路**；bsk 退居备用、
后续版本计划完全移除；只有用户明确表示「不愿使用 ego」时才允许启用 bsk，Playwright 等其他
工具仅在同样前提下作最后手段。macOS 上 ego 更稳定，不能作为回退 bsk 的理由。

### 行为变更
- `scripts/transport.py`：传输兜底从 `bsk` 改为 `egolite`；bsk 必须由 env/config 显式选择。
  单测基线仍由 `tests/__init__.py` 钉在 bsk，避免套件掉进 op 队列轮询。
- `scripts/env.py`：`resolve_backend` 在 egolite 传输下直接返回 egelite，不再「找到 bsk 就算 bsk」；
  `--install` 默认给 ego 安装引导；`--set-backend bsk` / `--set-transport bsk` 增加备用/移除警告；
  人读报告优先显示 ego-browser，bsk 仅作「也在装但不参与」提示。
- `env.py` 的未定后端提示改为「默认先装 ego；只有拒绝 ego 才问 bsk/内置浏览器」。

### 文档
- `SKILL.md` §0.2 重写为依赖政策（ego 默认、bsk 备用、Playwright 最后手段），首次激活不再问
  「装不装 bsk」；§5.1、故障表、命令表同步更新。
- `README.md` / `README.zh-CN.md`、`references/browser-backends.md`、`references/builtin-mode.md`
  统一口径：ego 首选，bsk 备用/计划移除，macOS ego 更稳。
- 版本号 1.8.1 → 1.9.0。

## 1.8.1（2026-11-13，执行安全层落地：动作账本 + 执行闸门 + 裁决协议；README 对齐当前形态）

本版把「一个动作到底发没发出去」从口头约定变成可判定、可追溯的机制，落成**纯标准库独立模块**；
同时把 README 从 0.1.6 时期的描述更新到当前形态（egolite 传输、账本/闸门、测试规模）。

### 新增：动作账本 `actionledger.py`
- **双幂等键严格分离**：`action_id` = `a-` + sha16(task|intent|seq)，同一动作跨 run 同一身份；
  `intent_key` = `ik-` + sha16(规范化业务意图)，与 task / run / 进程无关。换 task_id、换 run、
  换 seq 都绕不开同一条未决意图。
- **状态机**：`CREATED → RESERVED → DISPATCHED → COMPLETED`；`SEND_UNKNOWN →
  ANTI_REPLAY_LOCKED`（`resend_count` 恒 0）；`NOT_SENT` 仅「派发前确证未发出」可达。
- **崩溃恢复只产未知**：`recover_unknown()` 扫「已派发但无结果」→ SEND_UNKNOWN + 反重放锁。
  「没查到 POST」不构成 NOT_SENT——四条禁止推断由接口形状强制，不靠调用方自觉。
- SQLite 单文件 + WAL + busy_timeout；跨进程并发靠唯一索引与事务原子性，不依赖进程内锁。

### 新增：执行闸门 `execution_gate.py`
- 三把锁串行：本地单写者租约（`statelock.py`）→ TaskSpace 所有权 → 页面身份（`pageidentity.py`）。
- **dispatch 与 commit 分离**，锁覆盖到结果落盘之后（INV-2）；派发前做最后一轮 fencing 复核，
  把 TOCTOU 窗口压到最小。
- **fencing 世代**：旧执行者拿着过期锁对象继续跑 → `SP-GATE-007 FENCED_OUT`，不会两个进程各发一次。
- 操作分类表（有副作用 / 只读）由闸门内定，**不由调用方 declare**；未识别操作按「需业务对象」
  拒绝（fail-closed）。

### 新增：裁决协议（UNKNOWN 的唯一解锁通道）
- **两阶段**：`decide_adjudication()` 只记录 + 封存，**不改动作状态**；`consume_adjudication()`
  消费，才真正解锁。`commit_outcome()` 对 UNKNOWN 保持无条件拒绝——代码自己说「应该成功了」不算数。
- 结构由 DB 强制（2 表 + 7 索引 + 8 触发器）：证据只追加；封存不可逆且要求 ≥1 条证据；
  封存后证据不可增删改；业务字段不可变；生命周期单向一次；封存不得与消费/撤销同一条 UPDATE。
- 证据摘要 `adj-v1`：seq 升序 + NFC 规范化 + 固定分隔符 → SHA-256；提交时由**落库后的实际证据集**
  复算比对。
- 结论边界写在门里：裁决只能认定执行结果并解除本地锁，**不能证明外部副作用只发生一次**；
  `DECIDED_NOT_SENT` 必须有支持该结论的外部证据，不得以「没找到成功记录」反推。

### 新增：支撑件
`envelope.py` / `envelope_node.py`（Envelope 与 Node 验签）、`ego_backend.py`（ego 后端能力表与
所有权探针）、`procidentity.py` / `dispatch_marker.py` / `gate_lint.py` / `sp_errors.py`
（进程身份、派发标记、契约 lint、错误码表 `SP-ACT-001~016` / `SP-GATE-001~012`）。

### 修复
- **连接级持久故障用同一连接重试注定失败**：`_init_schema_with_retry` 对
  `attempt to write a readonly database` / `disk I/O error` 只在原地 sleep 重试，六次必然全失败
  （Windows + WAL + 多进程首次建库时最易触发，全量回归暴露过一次）。改为命中连接级故障 →
  重建连接 → 再建 schema。
- **PRAGMA 三处连接不同源**：`foreign_keys` / `recursive_triggers` 原先只在部分连接设置，事务内
  设置还会静默失效（读回仍是旧值）。收敛为 `_apply_pragmas()` 单入口 + 读回断言，漏一处即启动
  失败。原因：`recursive_triggers=OFF` 时 `INSERT OR REPLACE` **先删冲突行且不触发 DELETE 触发器**，
  实测能把未封存证据替换成任意值——生产连接必须 ON，这是硬需求不是保守选择。
- 另修两处实现缺陷：撤销裁决时把裁决 ID 当 `action_id` 写进事件流（造出不存在的 action，
  按 action 追溯不可见）；supersedes 撞唯一槽位时只回原始 SQLite 文本（调用方无从判断原因）。

### 状态（务必照读）
- 这一层是**独立模块，尚未接入默认循环**：`orchestrator.py` / `wa.py` 的执行路径仍是原来的写闸门
  （仅 `statelock` 已被 wa.py 使用）。安全层要等整体验收通过才接管执行路径；未通过前不半开使用。
- 测试：全量 **1157 条** 通过（其中裁决协议专项 50 条）。

### 文档
- README / README.zh-CN：版本徽章、传输层（egolite 默认 / bsk 兜底）、测试条数、执行安全层说明、
  辅助命令表全部对齐当前代码。

## 1.8.0（2026-11-13，egolite 传输：用 ego-browser 替代 bsk + 依赖生命周期）

### 新增
- **egolite 传输层（默认）**：页面动作（navigate/evaluate/fill/press）走 op 队列
  （协议与 iab 一致），由 `scripts/egolite-worker.mjs` 驱动 **ego-browser（ego lite）**。
  不装 bsk、不装浏览器扩展；worker 以 `ego-browser nodejs` 后台常驻（单轮 10 分钟，
  TaskSpace 固定名 `starpilot` 跨轮复用）——修掉 IAB「agent 必须持续手动跑 worker」的
  固有节奏。取值优先级：`STARPILOT_TRANSPORT` env > config `"transport"` > bsk 兜底
  （代码兜底保持 bsk：单测套件传输无关，运行时默认若随代码翻转会漏进未显式选传输的用例）。
  本机经 `env.py --set-transport egolite` 启用。写闸门/绑定归属/幂等守卫/平台锁全不变。
  文档：`references/egolite-mode.md`。
- **`env.py --set-transport bsk|iab|egolite`**：传输层持久化选择（config.json）。
- **依赖生命周期（安装/升级/反馈）**：
  - R1 ego 版本锚点：`env.EGO_ANCHOR=0.6.2.10`（实测锚点）+ `ego_info()` 四态
    （ok/older/newer/unknown）。**只记录不拦**（用户裁定）：`newer` 时 agent 按
    `egolite-mode.md` §F 特性清单对照评估——变化巨大→提示暂勿无人值守并记 lessons；
    无变化→无感使用并回填锚点。wa.py 会话建立时把 ego 版本记进 state。
  - R2 `env.py --install` 在 egolite 下改为 ego 安装三步引导（lite.ego.app 下载 →
    onboarding 从日常登录目标平台的浏览器导入数据 → 复检三件套）；GUI 必须人做。
  - R3 `update_check.py` 三路检查（starpilot / bsk / ego）；ego 按锚点比对输出
    「无感使用 / 须升级 / ⚠ 新于锚点走 §F 评估」。
  - R4 `scripts/feedback.py` + `.github/ISSUE_TEMPLATE/`（bug / platform-drift）：
    用户同意后生成可粘贴的报告块（版本三元组 + 环境自检 + state 摘要；
    隐私红线——无正文、无凭证、URL 只留 host）。流程见 SKILL §7.5。

### 修复
- `_explain` 在 op 队列传输（iab/egolite）下误报「没装 bsk」（BSK 为空是常态）。
- fill paste 路径清障：第三方扩展（实测 AIX 智能下载器）全视口遮罩吞编辑器事件
  导致发送键不渲染——按特征移除其注入节点（platforms.py，2026-11-11）。

### 测试
- 新增 `tests/test_lifecycle.py` 16 条（锚点检测/三路检查/反馈收集，全离线 mock）+
  egolite 路由/引导文件/后端检测 6 条；`tests/__init__.py` 把测试进程传输钉在 bsk
  基线（discover 须用包模式：`python -m unittest discover -s tests -t .`）。
  全量 769 条。

## 0.1.7.2 候选追加（2026-11-11，bsk vendor 钉住版随仓分发 + 跨平台适配盘点）

### 背景（用户在 zcode 环境实测反馈）
bsk 的 `BSK_AUTO_UPDATE` 绕过一切提示直接把机器级二进制换成 0.4.2（已知劣化：
浏览器任务被频繁打断）。提示层钉不住，必须物理隔离。

### 实现
- **vendor 目录**：`vendor/bsk/0.4.1/<triple>/` 五平台官方产物（win x64、
  linux x64/arm64 musl、mac x64/arm64）+ `manifest.json`（版本/来源/sha256）。
- **解析顺序**（`scripts/bsk_vendor.py`，env.find_bsk 与 wa.py 共用）：
  `BSK_BIN`（人工覆盖）→ vendor 钉住版（sha256 校验不过即跳过，fail-closed）
  → shim 真身 → PATH → ~/.local/bin。机器级 bsk 升不升级都不再影响星舵。
- **shim 真身回落**：`tools/bsk_shim.py` 真身缺失时落 vendor——机器级被
  BSK_AUTO_UPDATE 换成劣化版，闸门放行后执行的仍是 0.4.1。
- **安装器跨平台**：`tools/install_l2_shim.py` 增加 macOS/Linux 分支
  （真身 `bsk→bsk.bin` + bash shim 脚本），Windows 行为不变。
- **`env.py --install` / `update_check.py`**：机器级安装降级为 PATH 兜底并
  明说；bsk 的升级提示改为 vendor 口径（换钉住版须改 vendor 重新发版）。

### 跨平台兼容盘点（macOS / Linux 可用性）
核心已跨平台：env.py 有 darwin/linux 安装分支；winlease 判活有 /proc 分支；
写入原语走 bsk CLI（五平台产物齐）；主体纯 Python。已知残留：L2 shim 的
PATH 拦截在 POSIX 上首次可用（本次补齐）；`find_bsk` 文件名候选仍含 .exe
（vendor 与 PATH `bsk` 兜底可覆盖）；macOS 无 /proc，租约死锁恢复需人工
（fail-closed，不阻塞）。前提：Python 3.9+、Chrome/Edge + 扩展、bsk CLI。

### 测试
新增 `tests/test_bsk_vendor.py` 24 例（triple 映射、manifest 解析、fail-closed
哈希校验、find_bsk 优先级、shim 回落、安装器模板）；test_env 补 vendor 隔离
+ 新增优先级用例。全量 807 OK（`test_concurrent_redeem` 偶发时序抖动，
复跑通过，非本次引入）；contract lint PASS。

## 0.1.7.2 候选（2026-11-09，webai 交互识别八条优化：等待时钟收紧、弹窗分类、New chat 恢复、回复图标信号、bsk 钉 0.4.1、多任务错峰）

不升版号（`SKILL.md` 仍 0.1.7.1）：发版与推送由用户决定。八条需求逐条落点：

### 用户口径 → 实现
1. **正常回复 ≤2 分钟**：`ttft_s` 45→30、`stall_s` 60→45、`wait --timeout` 默认 900→150。
   超窗不再干等——首字/停滞时钟到点就分诊。特殊长任务显式给 `--timeout`。
2. **弹窗 ≠ 限制类**：`platforms.classify_modal()` 只认文案关键词（中英覆盖），
   命中才归类（context_full / rate_limit / quota），没命中落 `other` 只留证，
   **不改变既有判定路径**。判定依据记在 `evidence.modal.basis`，事后能查。
3. **上下文满 → New chat**：新增 `wa.py modal-click --match 'new ?chat|新聊天'`——
   只在可见弹窗**内部**找按钮（绝不全局搜，防误点侧边栏常驻「新聊天」丢对话）。
   点成功 = 平台官方的带记忆开新对话；随后 rebind + 贴任务摘要继续，不重发原问题。
4. **限流 vs 其它分开**：新判定档 `STALL_CONTEXT_FULL`（rc=16）/ `STALL_RATE_LIMITED`
   （rc=17），优先级在风控之后、生成判定之前——对着限流弹窗干等是最差的处置。
   处置相反且都写死在 hint：限流=冷却重试一次；上下文满=New chat 恢复。
5. **bsk 钉 0.4.1**：0.4.2 起已知劣化（浏览器任务被频繁打断）。`update_check.py`
   远端 ≥0.4.2 显示 ⛔（钉住勿升）而非 ⬆；本地 ≥0.4.2 显示 ⚠ 建议降回；
   `env.py --install` 装完验版本，装上劣化版当场警告（官方脚本无版本参数，只能装后验）。
6. **回复完成 = 图标 + 发送键状态**：chatgpt 声明 `sel.reply_done`（回复下方
   复制/朗读/评价图标，11-03 真机 dump 验证，中英覆盖）。wait 的完成判据从
   「regen 或 文本稳定两次」扩成「regen / reply_done / 文本稳定」三选一；
   orchestrator `wait_for_reply` 新增第 4 条见证 `reply_done`（图标轮稳定 1 次即可——
   平台事实比文本长度可信）。
7. **上下文按 token 不按轮数**：分类关键词表禁入轮数词（测试盯着）；rc=16 的
   契约行、SKILL 出路表、hint 三处都写死「token 口径」；全仓库无任何按消息轮数
   推断容量的逻辑（msg_count 只用于归属判定/审计/变化检测）。
8. **多任务错峰**：机制本就有（实例级租约 + 各任务各绑各的对话 + 非抢占等待），
   补齐的是「说清楚」：rc=9 输出从裸 JSON 改成结构化错峰交接说明（谁占着、
   等多久、下一步重跑或换实例）；orchestrator FIX_HINTS 补 window_busy/
   context_full/rate_limited 三条处置。

### 契约
- `references/execution-contract.md` §8 rc 表补 16/17 两行（contract_lint 盯着）。
- `tests/test_webai_signals.py` 新增 21 个口径测试；`tests/test_update_check.py`
  补 BskPinTest 5 个；`tests/test_mode_switch.py` 补机器配置隔离（用户的
  unattended 开关不该劫持「缺省 step」口径测试）。全量 782 OK。


## 0.1.7.1（2026-11-04，用时更新检查：Starpilot 自身 + bsk 插件，有新版提示用户）

版本记 `0.1.7.1` 而不是 `0.1.71`：四段式在元组比较下才是对的——`(0,1,7,1) > (0,1,7)`
且 `(0,1,7,1) < (0,1,8)`；写成三段 `0.1.71` 会永远压过 `0.1.8`，把升级比较搞反。

### 新增
- **`scripts/update_check.py`**：用时顺手查「Starpilot 自身」与「bsk 插件」的最新稳定版，
  有 ⬆ 提示用户、用户拍板才升级（只提示不擅自动）。
  - 稳定版口径：Starpilot = 最高 `v*` tag；bsk = `Tencent/BrowserSkill` 的最高
    `cli-v*` release（该仓库 CLI/扩展分渠道发版，不过滤前缀会错拿成扩展的版本）。
  - 失败即 unknown，绝不猜；检查器自身异常也不连累使用方（退出码永远可判）。
  - 缓存 24h（`runtime/state/update-check.json`），重跑零网络成本，`--force` 真查。
  - 升级出口指到既有命令：`upgrade.py --to-latest` / `env.py --install`。
- **SKILL.md 主干接入**：主干补「用时更新检查」一段——缓存不卡流程，⬆ 口头提示。

## 0.1.7 候选（2026-11-03，Agent↔网页 AI 通信可靠性：闸门自锁、发送幂等、查询三态、bsk 0.4.1 同步）

不升版号（`SKILL.md` 仍 0.1.6）：发版与推送由用户决定，避免 `upgrade.py` 的远端比较把
这批发改覆盖掉。以下按「根因 → 修法」记，四条互相咬合，单看任何一条都像在治标。

### 修复
- **写闸门口径修反了**（`platforms.write_path_ok` / `loop_accepted` / `calibration_gap`）。
  `verified` 回答的是「发出去之后还能不能自动认出回复」，`write_verified` 回答的是
  「往这个框打字点这个键会不会真发出去」。闸门只看 `verified`，于是 ChatGPT
  （写路径 11-02 复测通过、计数锚点当时失效）**每一轮编排写都被拒**，唯一出路是
  逃生门 `STARPILOT_ALLOW_DIRECT_SEND` 直发——而直发绕开的恰恰是幂等守卫与写后核验：
  闸门想保护的东西被自己的口径逼到保护之外。CLI 与闸门改为同源判据（同一函数），
  核验结论新增 `verify_basis`，`write_only` 平台上那条 VERIFIED 不许冒充整链路验收。
- **逃生门台账从 O(发送次数) 打断**（`pending_gate.record_escape/settle`）。
  一次一发一个新 id + 「每条 P0-ESC 都要在之后每一轮回执出现」= 自激振荡：
  24 小时 32 条，回执正文比交付物长，回执越长越不合格，越不合格越发不走正路。
  现在按目标聚合（稳定 id + 精确计数 + 最近理由），报备一次后 `settle` 核销；
  核销只免除重复报备义务，条目仍在台账上可查。**留痕靠聚合，不靠复读。**
- **CLI 发送补幂等 + 三态失败分报**（`wa.py cmd_send`）。`last_send_hash` 以前只写不读。
  现在：内容哈希 + conv_id 命中即拒；「输入框已空但无主信号」落 `maybe_sent` 滞留态，
  拦住下一轮的盲目重发；退出码按处置分开——**8 = 确实没发出去（重发安全）**、
  **11 = 写路径没标定（去 probe，别重发）**、**12 = SEND_UNCERTAIN（别重发）**、
  **10 = 选择器歧义（收窄选择器，重试无用）**；9 仍是 `WINDOW_BUSY`。
  成功输出带 `next` 字段，`SENT_NO_REPLY` 时明写「等，不要重发」。
- **查询失败不再当成「没有」**（`BskQueryError` + `list_sessions/list_browsers(strict=)` +
  `session_state()` 三态）。旧代码在 rc≠0 或输出解析失败时返回 `[]`，等待循环据此
  判「会话没了」→ 重开窗口；`ensure_session` 据此开新会话。用户感受就是
  「网页故障、窗口越开越多」，而故障是自查过程自己造的。现在 `unknown` 一律
  **拒绝开窗**并让人去 `doctor`。
- **bsk 输出的 JSON 扫描器重写**（`parse_bsk_json`）。旧的两处 ad-hoc 解析：
  `json.loads(整段)` 遇 banner 行必挂；`evaluate` 的「找以 `{` 开头的行」吃不下
  pretty-print。新实现用**闭括号栈**做平衡扫描——第一版用单个 closer 变量，
  `{"sessions":[{}]}` 只解析到内层数组、外层键全丢，测试当场抓到。
- **`generating` 不再单点依赖停止按钮**（`_generating_composite`）。JS 里
  `generating: !!stop`；停止按钮是平台改版第一个删的东西（ChatGPT 11-03 快照
  `data-testid="stop-button"` 命中 0），只信它会让 45s 首字钟把正在流式的长回答
  误杀、让 send 的「别在生成中再发」守卫失效。现在 `stop OR 末条文本在变长`
  （阈值与 `platforms.DOC_GROWTH_MIN` 同口径），并留 `generating_basis`。
- **会话 id 取值分级**：`session start --json`（bsk 自报）→ 会话列表 → 带上下文的
  正则兜底（要求紧邻 `session` 字样），最后一档**必打警告**。裸 `\b[a-z0-9]{4}\b`
  会把 `edge`/`true` 当 id，拿错 id = 往别人的会话打字。
- **`pending_gate.py` 的 CLI 一直跑不起来**：入口 `main(sys.argv)` 而 `main` 约定
  第 0 位是子命令名 → SKILL 里教的每条命令都返回 rc=2 并打整篇 docstring，
  而 2 在本模块语义接近「不通过」，agent 读到的是「门禁拦我」，真相是「命令没启动」。
  改 `sys.argv[1:]`，并加真子进程回归测试（单测以前直接 `main([...])`，照不到这一面）。
- **控制台编码吃掉判定**（`scripts/safeprint.py`）：cp936 下打印 U+FFFD/↔ 抛
  UnicodeEncodeError，需求卡片闸门在 `print` 那行崩成 `GATE_CRASH`，把
  `NO_CONFIRMATION` / `CLARIFIED_EMPTY` 的实话顶掉；基线 3 条单测在本机红、
  `PYTHONUTF8=1` 下绿。现在管道→UTF-8（宿主按 UTF-8 解码，中文不再乱码）、
  真控制台→保留本机代码页只改 `errors`；子进程统一 `PYTHONIOENCODING`
  （父进程的 `encoding=` 管不到子进程自己的 stdout，子进程先崩、父进程拿到半截 JSON）。
  幂等判断改成读流状态而非记 `id()`——`id` 会被回收复用，全量跑单测时真跳过过加固。

### 新增（bsk 0.4.1 同步）
- `bskcompat.supports()` 能力探测（问 `<cmd> --help`，不信版本号）+ `step()` / `dialog_status()`。
- `step` 原子发送通道（默认关，`STARPILOT_STEP_SEND=1`）：一次往返替代
  sync→动作→再探测，且 never retried。开启需**发送键唯一命中**；ChatGPT 的发送键
  仍未定标（空编辑器不渲染，快照四条候选全 0 命中），所以默认不启用。
- `TAB_CLOSE_UNKNOWN` 先跑 `dialog status`：卡住关闭的多半是 beforeunload，
  如实报出来并交人处置（星舵不替用户按「确定离开」）。
- `triage` 落 `observe` VOM 快照到 `runtime/wa/obs/`：卡住现场成为可离线复核的历史证据
  （`bsk view <file> --find <文案>`），不必再动浏览器。
- `wa.py dialog`（只读）、`wa.py ask-human`（官方 request-help，默认演练，`--go` 才打扰人）。
- `wa.py doctor --explain`：把体检事实翻译成**有序下一步命令 + 禁止动作**。
  上下文预算小、遵循度低的 agent 不必自己推断「那我该敲什么」。
- `.gitattributes`：`core.autocrlf=true` 下保护 L2 shim 与 DOM 夹具的**逐字节一致**
  （shim 安装位靠 sha256/体积验收，换行被改写会得出「安装位被篡改」的假阳性）。

### 标定（离线，用真机快照）
- 新增 `tests/fixtures/chatgpt/`（messages/composer/meta），来自只读 session 的
  `get-html` 快照裁剪。ChatGPT 计数锚点重标定：`data-chatgpt-selection-message-id`
  （助手，节点自带 UUID）与 `data-chatgpt-search-message-ids`（轮次容器；用户气泡
  自己不带，靠 `key_up` 向上取）；旧 `data-message-author-role` 实测命中 0。
  `reply_count` 改为按身份去重计数，`msg_count` 在中文 marker 落空时退回 DOM 口径（英文界面）。
- **`verified` 保持 false**：还差发送键定标与专用测试对话 5/5 独立问答验收。
  不拿「计数能用」冒充「链路已实测」。

### 测试
- 610 → **668** 条：新增 `test_safeprint`(10) / `test_send_idempotency`(10) /
  `test_bsk_query_robustness`(24) / `test_bsk_caps`(16) + ChatGPT 回放 8 条
  + pending-gate 台账/CLI 7 条。基线 3 条红测转绿。

### 已知未做（如实）
- **5/5 独立问答验收：4/5 通过，第 5 轮判为「对话被并发使用」，`verified` 保持 false。**
  第 1–4 轮五条判据逐项全过（`SENT` / `effective_delta=1` / 新增助手 UUID 恰 1 /
  与本轮新增 user 轮次相邻 / 单轮单发 / 无历史误认）——锚点重标定在真机上成立。
  第 5 轮 `SENT_MULTI`（新增 3 个用户轮次）：读正文确认三句**互不相同**，其中两句是
  用户同期在同一对话里打的字，journal 里 r5 只有一条 `write_send` ⇒
  **不是重复发送、不是锚点算错、不是虚拟列表假象**。要算验收完成，需重跑一次
  「期间无人使用」的干净 5 轮。
- **本轮由验收抓出并已修的两个真缺陷**：
  ① `07c7ddb`：新对话首条发出后的地址采纳只写在 CLI，闸门路径没做 ⇒
     `bind --new` 起跑的循环第 2 轮必被 `guard_ok` 判地址漂移。
  ② `d8ad273`（lessons L-41）：归属判据拿 **URL 当窗口身份**——用户窗口只要开着
     一份同 URL，绑首页形态就必然判 `misplaced`，成败取决于用户当时开着什么网页。
     改用 bsk 自报的 `agent_window_id` + `tab list` 的 `window_id/scope`；
     拿不到身份时退回旧规则并如实标 `basis`。真机复验 `basis=window_id / ok=true`
     与 `bound_in_user=true` 同时成立。
  ③ `4ecbd73`（lessons L-42）：`user_msg_delta>=2` 以前一律判「重发」。现按正文指纹
     分四类：`OURS_ONLY` 真重发 / `CONTAMINATED` 外来轮次（记 inconclusive，不进
     mismatch、不推 fail_streak、不触发 replan）/ `OUR_MISSING` 没送达或被改写 /
     `UNKNOWN` 无指纹不下结论。`fp_of()` 与 JS `fpNorm` 同口径并有测试钉住。
- **仍未修**：ChatGPT 会把 composer 草稿**恢复进新开对话**，`clear_editor` 的键盘路径
  清不掉（已由 exec 回落兜住 `2932092`，但根因在平台侧，见 L-39）。
- 4 条 P0-ESC 的页面级重复核对维持 UNKNOWN（虚拟窗口外，滚动通道被页面活性竞态阻断）。
- 运行约束（不是 bug）：bsk 默认 **5 分钟空闲即回收 Agent Window**
  （`reason: session_idle_timeout`），所以定标/验收必须**单程跑完**，不能分多轮工具调用。
- 扩展协议混版风险：本机两侧均 0.4.1 / protocol 1.8 实测 `version_skew=false`，但
  0.4.1 起「混版不支持」是硬约束，已进 `doctor --explain` 告警表。
- 扩展人工介入开关已实测生效（`request_help=enabled`、`borrow_confirmation=always`），
  `wa.py ask-human` 的前置条件满足；`doctor --explain` 会读扩展自报值并在关闭时告警。

## 0.1.6（2026-11-02，窗口/tab 资源治理闭环 + 介入语义修正）

### 变更
- **tab 生命周期闭环**：heal 复用优先（同址标签 → 空白残留 → 最后才 create，不再无条件 tab create 留孤儿）；新增 `reap_own_tabs`（只收自己会话的同地址重复与空白残留；用户窗口标签只报告不动）自动接进 ensure_session，手动入口 `wa.py tab-reap`（--dry-run）
- **介入≠打断**：`WINDOW_BUSY_HUMAN`（rc=9 报人裁决即停）废止；有人值守改为 30s 被占先提醒（`WINDOW_BUSY_NOTICE`，非阻塞）后继续等满 600s 轮换预算（2026-11-02 用户裁定：人工介入是并行监督，星舵持续推进到任务完成）
- **人的想法转达纪律**（SKILL §5.5b）：流程中人提出的想法/意见/疑问必须原样整体转达 webai 裁决，agent 不得自行裁决或只记本地
- **L2 PATH shim**（tools/install_l2_shim.py）：`bsk.exe`→`bsk.bin.exe` + `bsk.cmd`，仅 session start 过闸（无 `STARPILOT_WINDOW_GATE=1` 令牌 → 全局计数，超限拒绝）；wa.py/env.py 直连真身
- `contract_lint` 新增 `no_raw_tab_ops`（wa.py 之外禁裸调 tab create/close）；命令提取支持连字符子命令
- SKILL §5.5 硬边界表扩为窗口/tab 双资源纪律；runbook window-session 新增 §8（tab 生命周期 + 介入语义）

### 修复（Web AI 复审后，未发版）
- **标签所有权账本** `scripts/tabclaims.py`：SQLite 权威（事务 + 主键 `(session,tab)`），
  `tab-claims.jsonl` 仅审计；四项校验 owner_key/run_id/lease_id/fencing；状态机
  claimed/closing/closed/unknown/orphan/owner_unknown；UNKNOWN 为隔离态不靠 TTL 解除。
  接入点：`ensure_session`（三条复用/新建路径登记）、`heal_window_placement`、
  `navigate_soft` 前 `navigate_gate` 硬门、`reap_own_tabs` 判据、`close_tab_guarded`。
- **自动回收撤除**：ensure_session 两处 `reap_own_tabs` 调用删除（裁定：宁可暂时积累
  标签也不误关别人的），`tab-reap` 默认 dry，需 `--apply`。
- **L2 shim 改 fail-closed + 一次性票据**：废弃可伪造的 `STARPILOT_WINDOW_GATE=1`，
  改 `STARPILOT_WINDOW_TICKET`（`winlease.gate_issue/gate_redeem`，HMAC + 用后即焚 +
  120s）；shim 导入账本失败/计数读不出/记账失败/校验异常一律 rc=9（旧版 fail-open）。
- `winlease` 补 `fencing` 世代号；`bskcompat` 适配 bsk 0.4.1（sync + 一次性 observation，
  **JSON 解析**取 id）；ChatGPT 编辑器选择器改 `form div.ProseMirror`；长文分块写入
  （800 字/0.35s）避免 CDP 挂死。

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