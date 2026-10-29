# 实战教训沉淀（lessons）

> 规则：踩到的坑如果**换个人、换个项目还会踩**，就写进这里。每轮收口强制检查一次（SKILL.md §4.6 第 3 步）。
> 格式：现象 → 根因 → 处置 → 适用范围。每条必须有实证来源，不写猜测。

---

## 一、循环与桥接层

### L-01 外部 CLI 不在 PATH，但到处都在用它

- **现象**：`bsk status` 报找不到可执行文件；定时任务没有 export 必然失败。
- **根因**：`bsk.exe` 装在 `~/.local/bin/`，不在 PATH；会话环境与脚本环境不一致。
- **处置**：代码里做候选路径探测（`BSK_BIN` env → `shutil.which("bsk")` → `~/.local/bin/bsk.exe` → Windows 常见位置）；跑脚本前先 `export PATH="$HOME/.local/bin:$PATH"`。
- **范围**：所有调外部 CLI 的 Python 脚本。别只在自己终端 export 就以为好了——**定时任务没有交互 shell**。

### L-02 手工会话会顶掉 `.wa-state.json` 里的 session

- **现象**：`wa.py send` 报「线程 msg_count=0」误判，页面明明有历史消息。
- **根因**：用 bsk 手工 `session start` 起的会话，被 `wa.py` 读成当前会话，state 里的 `session` 字段被覆盖，指向了非目标页面。
- **处置**：`wa.py bind <url>` 重新绑定（bind 会把当前会话导航到绑定 URL 恢复）；手工会话与 wa.py 会话**分开开**。
- **范围**：任何「脚本管会话 + 人也手工开窗口」的场景。

### L-03 后台 daemon 会跟着「拉起它的那个命令」一起死

- **现象**：跑批 10 分钟零输出，daemon 活着但 active sessions=0。
- **根因**：`bsk daemon start` 是 server 模式（前台常驻），其它命令在没有 daemon 时会自动起一个。用 stdout 管道收 CLI 输出时，daemon 继承管道且永不关闭，`subprocess.run` 永远等不到 EOF。
- **处置**：驱动层 `subprocess.run` 改走**临时文件**收输出（文件没有 EOF 语义）并 `stdin=DEVNULL`；跑批前让 daemon 独立常驻（`tail -f daemon.log | bsk daemon start` 后台跑，保持 stdin 不关）。
- **范围**：任何「CLI 自动拉起常驻服务」的场景。

### L-04 大载荷进不去 Windows 命令行

- **现象**：259KB 的 user.js 注入不进去。
- **根因**：base64 后 345,480 字符，远超 `CreateProcess` 命令行 32767 上限。
- **处置**：base64 切 20000 字符一片（18 片）累加到 `window.__jstB64`，最后一次 `(0,eval)(src)`；带源码指纹缓存，同标签页重复注入 0.08 秒。
- **范围**：任何要注入大脚本的浏览器自动化。

### L-05 控制台中文乱码是显示问题，不是数据坏了

- **现象**：Windows 控制台打印「已付款待审核」全是问号，疑似编码损坏。
- **根因**：控制台默认 GBK，Python 没 `reconfigure(encoding="utf-8")`。
- **处置**：脚本启动时 `sys.stdout.reconfigure(encoding="utf-8")`；判定一律以解码后的字符串为准。字节层面实测 UTF-8 全程无损（`'已付款待审核…'.length==32`）。
- **范围**：所有 Windows + Python 控制台程序。

### L-06 stdout 污染会让 `--json` 直接不可解析

- **现象**：`payload injected: …` 混进 stdout，`json.loads` 失败。
- **处置**：进度/调试信息一律走 **stderr**，stdout 只放结构化输出。
- **范围**：所有带 `--json` 的 CLI。

---

## 二、流程与依赖

### L-07 依赖没声明 = 整个项目起不来

- **现象**：`cli.py` 第 19 行 `ModuleNotFoundError: pyyaml`，项目完全跑不动。
- **根因**：WorkBuddy 自带 Python 升级后 site-packages 被清空，之前靠环境里「恰好有」活着。
- **处置**：**所有第三方依赖必须进 `requirements.txt`**，并在 runbook 写明「遇到就重装，不是代码问题」。
- **范围**：全部项目。这是环境依赖，不是代码问题——别浪费时间改代码。

### L-08 YAML 里的 `default` 不铺，参数会在运行到那一步时才炸

- **现象**：`${params.base_token}` 报「参数引用不存在」，但报错发生在流程中段，前面的步骤都白跑了。
- **根因**：`_flow_params` 只取命令行有值的参数，没铺 yaml 的 `default`。
- **处置**：先铺 default，再由命令行覆盖；**参数校验必须发生在流程开始前**，不能延迟到步骤执行时。
- **范围**：所有声明式流程编排。

### L-09 并发测试的时间窗要考虑启动开销

- **现象**：锁测试全量跑必失败、单独跑必通过。
- **根因**：持锁窗口 2 秒，全量跑时机器负载高，第二个子进程晚于 2 秒才启动，第一个已释放 → 两个都 WIN → 被误报成「锁没生效」。
- **处置**：持锁窗口拉到 8 秒。
- **范围**：**这不是产品缺陷**，但必须记：并发测试失败先怀疑测试自己，别急着改产品。

### L-10 活跃工作目录不能当只读基线

- **现象**：`legacy_dirhash --expect <hash>` 硬失败，旧目录哈希从 `6e5d333d…` 变成 `56ed8622…`。
- **根因**：旧目录每天都有定时任务在跑、有改动，不是冻结快照。
- **处置**：硬失败废弃，改为 `--record` 记录基线 + 逐文件 diff（增/删/改清单，只报不拦）。
- **范围**：所有「新旧比对」场景。**先问对方是活跃目录还是冻结快照**，否则门禁会一直误拦。

### L-11 外部工具的硬上限要实测，不能照文档猜

- **现象**：某表格 API 报 `invalid --limit 500: must be between 1 and 200`。
- **处置**：代码、清单、流程定义三处统一改为 200。另一模式能到 2000，但写文件、不返回内联 JSON。
- **范围**：所有外部 CLI/接口。**实测一次写进配置，别让运行到那步才炸**。

---

## 三、业务与被自动化对象

### L-12 页面载荷有双副本，改一边不改另一边会静默按错口径跑

- **现象**：油猴版 v2.7.4、bsk 版 v2.5.1，同一个 Edge 资料里两份都挂 `window.jstBox`，注入被静默跳过，py 拿到错口径的页面对象在跑。
- **处置**：两份 js 同源同版本 + **哈希校验**；分叉时 `check` 报「页面 VER ≠ 载荷 VER」并停手；注入后核对版本，指纹命中则跳过重注入。
- **范围**：任何「同一份载荷在两条通道运行」的场景。**版本号一致不等于内容一致，要比哈希**。

### L-13 虚拟滚动会吃掉批量勾选

- **现象**：某后台表格批量操作时，一次提交 23 条，有 12 条落在渲染区外，「滚动后仍未渲染出这一行」，勾选核对 `ok:false` 并中止且不提交。
- **处置**：单次操作 ≤10 条，超了拆批（8+8+7 全通过）。逐条处理的路径不受此限。
- **范围**：所有虚拟滚动表格。**报错是「安全但活没干完」，不是「失败」，必须如实区分**。

### L-14 平台改版会把历史数据挪走

- **现象**：某个日期线之前的历史单据被归档到独立页面，原列表查不到。
- **处置**：查历史数据要切页；把「归档策略」写进能力矩阵，别当成数据缺失。
- **范围**：所有带归档策略的后台。

### L-15 浏览器扩展会污染你的页面

- **现象**：①下载器扩展的悬浮球注入所有页面，干扰 snapshot；②agent tab 被扩展页占据后 `evaluate` 永久报 `Cannot access a chrome-extension:// URL of different extension`，但 `navigate` 能 reach；③关掉唯一 tab = session 直接终止。
- **处置**：snapshot 干扰可忽略；evaluate 永久坏只能关 tab，但**关 tab 会杀会话**，需要重开；`request-help` 会把会话挂 unfinished command，期间 evaluate 全被拒（那是等待机制，不是故障）。
- **范围**：所有在用户日常浏览器里跑的自动化。**不要在用户正在用的标签页上做实验**。

---

## 四、汇报与验收纪律

### L-16 单测全绿 ≠ 生产路径没 bug

- **现象**：`notify_dispatch` 在 flow 路径必然 `UnboundLocalError`（`ruleset` 先用后赋值），但单测全绿。
- **根因**：所有用例都显式传 `transport=`，而 flow 从不传——**测试绕过了生产路径**。
- **处置**：补一条走真实调用路径的用例（不传可选参数）。
- **范围**：所有带默认参数/可选依赖的模块。**专门补一条「什么都不传」的用例**。

### L-17 人工确认不是系统验证

- **现象**：「群里没看到消息」被当成「没送达」。
- **处置**：区分三种结论——`confirmed_delivered`（人工找到并确认）、`confirmed_not_delivered`（按约定检查流程确认未送达，且明确承担判断责任）、`无法确定`（继续阻塞）。**不允许仅凭「我没看到」自动解除未知状态**。
- **范围**：所有对端状态不可查询的场景。

### L-18 阶段通过 ≠ 生产就绪

- **现象**：审计阶段 P2-08 全部通过，很容易被读成「可以上生产了」。
- **处置**：裁定语必须写明范围（「P2-08 审计阶段通过，不等于真实发送适配器已具备生产验收资格」），并把未解决项逐条列成阻塞项。
- **范围**：所有阶段收口。**通过结论要带范围限定词**。

### L-19 账本类文件压缩会改变语义

- **现象**：初版压缩把三次尝试压成一次，绕过了 `max_attempts`。
- **处置**：压缩前后**逐幂等键比对决策完全一致**；`sending`/`unknown`/`resolved` 与失败次数语义不变；损坏账本拒绝压缩（fail-closed）；写入阶段绝不重试，只对 open 阶段有界重试。
- **范围**：所有 append-only 账本/日志的压缩与归档。

### L-20 改机器环境会静默毁掉基线

- **现象**：升级运行时后 site-packages 被清空，项目直接起不来（见 L-07）；指纹缓存、页面载荷版本也出现「内容变了但 `@version` 没变」的不一致。
- **处置**：改动前后重算哈希并落盘；版本号与内容必须同步升级；基线哈希失效要重新确立并留痕。
- **范围**：跨机器、跨运行时、跨版本的任何资产。

---

## 五、网页 AI 与平台适配（这一类只有跑循环才会遇到）

### L-21 「记不住」有三种机制，混为一谈会开错药

- **现象**：AI 开始忘掉早期定的细节，细节程度各不相同。
- **根因**：三种机制——**截断**（早期整段没了）、**摘要压缩**（大意在、精确措辞没了）、**中间位置注意力衰减**（还没到上限就想不起来）。
- **处置**：先做区分测试（问「最开始约定的那个串是什么」+「第 1 条大概意思还记得吗」）：
  - 答不出精确串、答得出大意 → **摘要压缩** → 落盘 + 重贴结论可解
  - 答不出大意 → **截断** → 只能换大窗口平台或分段
- **范围**：所有长对话。换平台前必须先分清是哪种，否则会误判「这个平台不行」。
> 完整测试表见 `templates/web-ai-eval.md` 第 4 步。

### L-22 「上下文窗口大」不等于「长对话连贯」

- **现象**：某平台号称百万 token，长对话里照样记不住第 20 轮的细节。
- **根因**：窗口大小决定的是「装不装得下」，不是「注意力分配得均不均」。多跳事实推理在长上下文下准确率明显下滑。
- **处置**：选型看「单对话长记忆」口碑，不看窗口数字；**星舵本身靠落盘对冲，不靠平台内存**。
- **范围**：所有以「上下文窗口」为卖点的平台。

### L-23 `probe` 报 `editor: false` 不是平台坏了，是 DOM 改了

- **现象**：昨天还跑得好好的，今天 `probe` 说找不到输入框。
- **根因**：平台改版，选择器失效。
- **处置**：回填 `platforms.py` 的 `sel` 候选；改完把 `verified` 与 `notes` 一起更新。
- **范围**：所有平台。**别把这误判成「平台记忆出问题」**——两者症状完全不同。

### L-24 正文区混着 UI 文案，长度判定会误判「还在生成」

- **现象**：`last_len` 一直变，`wait` 永远等不到「文本稳定」。
- **根因**：正文区把「重新生成」「复制」等操作按钮的字也算进去了。
- **处置**：`prefer_marker=True`，先按「ChatGPT 说：」这类标记切出正文，再切掉尾部 UI 文案。
- **范围**：所有把回复渲染成富文本的平台。

### L-25 候选选择器是从通用模式推断的，不实测就是赌

- **现象**：某平台 `probe` 能出 `editor: true` 但 `last_len` 恒为 0，看起来「能用」其实读不到回复。
- **根因**：输入框选择器猜对了，消息节点选择器猜错了。
- **处置**：接入后必须跑完四步：probe → 回填 → 跑通一轮 → 跑记忆自测；每一步都有可观测的通过标准，不靠「看起来对」。
- **范围**：所有 `verified=False` 的平台条目。**默认假设它是坏的，直到实测证明它好。**

### L-26 平台记忆不能当唯一真相源

- **现象**：靠对话历史推进到第 15 轮时，一个早期决定的细节已经模糊。
- **根因**：把平台上下文当成了项目状态。
- **处置**：关键结论（契约、裁定、决定）落进项目文件；每轮只贴「当前阶段需要的结论 + 最近一次裁定」。平台会忘，但文件不会。
- **范围**：所有长循环。**这是星舵的设计前提：假设平台会忘，然后补回来。**

### L-27 受控编辑器会忽略 `delete`、`paste` 是追加而不是替换（实测，Kimi）

- **现象**：往输入框重写同一段内容，出来的是「旧+新」拼在一起，不是替换。
- **根因**：Kimi 用 Lexical（`span[data-lexical-text]`）做受控编辑器——
  - `document.execCommand('delete')` 被**忽略**（它不是自己的 state 编辑器）；
  - 合成 `paste` 事件是**追加**（实测 30 字 + 40 字 = 70 字，不是替换）。
- **处置**：用 **Selection Range 圈定编辑器内容 + `execCommand('insertText')`** 精确覆盖：
  `range.selectNodeContents(el)` → `sel.removeAllRanges()` → `sel.addRange(range)` → `execCommand('insertText', false, text)`。
  **不要用 `execCommand('selectAll')`**——编辑器为空时它可能选到整页。
- **范围**：所有受控富文本编辑器（Lexical / ProseMirror / Slate / React contenteditable）。
  **这是平台适配层的活，写进 `platforms.py` 的 `write_method`，别写进主流程。**

### L-28 「输入框变空」不能当发送成功的证据（实测，Kimi）

- **现象**：`send` 报成功、输入框也空了，但页面上根本没有这条消息，侧边栏也没有新会话。
- **根因**：Kimi 免费额度繁忙时，提交后**清空输入框却拒绝创建会话**，同时弹限流弹窗
  「和Kimi聊天的人太多了，订阅会员可进入优先队列」。
  此时若把「输入框清空」当成功信号，工具会**谎报成功**，下一轮就对着一个不存在的会话继续等。
- **处置**：发送成功的判据改成三者之一——**消息数增加 / 地址变了 / 我发的那段文字出现在页面上**。
  第三条最可靠：把正文里最长的实质行去掉 markdown 修饰符当**指纹**，在页面文本里归一化后子串匹配
  （`platforms.js_has_text`）。三者都不满足 = 没发出去，**别记 turn、别等回复**。
- **口径补记（2026-10-29，第 4 轮裁定）**：三选一主信号都不满足、但输入框已清空时，
  **既不能报成功也不能报失败**，必须输出 `INCONCLUSIVE_NOT_SUCCESS` 转人工复核页面
  （实现见 `scripts/orchestrator.py` 的 `INCONCLUSIVE_NOT_SUCCESS` 分支）。
  另外「一条回复被平台拆成 N 个气泡」也归 **INCONCLUSIVE**，不自动判失败——
  闸门记 `reply_bubbles` 与 `logical_replies` 交人工，定义见 `docs/orchestrator-contract.md` §2.4。
- **范围**：所有会「先清空再判断」的平台。**发送成功的判据必须能区分「平台收下了」和「平台扔了」。**

### L-29 消息区选择器命中「空壳」会让整条链路静默失效（实测，Kimi）

- **现象**：`msg_count` 恒等于 1，`wait` 永远等不到回复完成，读回来的正文也是空的。
- **根因**：Kimi 的 `div.message-list-container` 存在，但里面只有一个恒空的 `div.message-list`；
  写成 `div.message-list-container div[class*="message"]` 会命中这个空壳而不是任何一条消息。
- **处置**：真实消息条目是 **`div.chat-content-item-user` / `div.chat-content-item-assistant`**
  （等价变体 `div.segment.segment-user` / `div.segment.segment-assistant`），
  滚动容器是 **`div.chat-detail-main`**。接入新平台时：
  **先在一条已有会话上把 DOM 结构摸清再回填选择器，别拿首页猜。**
- **范围**：所有平台。**选择器命中一个存在但恒空的容器，比选不中更危险——它不报错。**

### L-30 平台的弹窗/遮罩是硬阻塞，必须先查再发（实测，Kimi）

- **现象**：一切正常，输入框能写、按钮能点，但消息就是发不出去。
- **根因**：页面上有全屏遮罩 `div.modal-mask`（限流提示），真实鼠标点击会被挡住；
  DOM 级 `click()` 也不保证能穿透。
- **处置**：探测时把 `div.modal-mask` / `[role="dialog"]` 一起查（`probe` 要报「页面有弹窗」）；
  弹窗按钮优先用稳定的 `data-testid`（Kimi：`confirm-dialog-cancel`=我知道了、`confirm-dialog-confirm`=去升级），
  用 bsk 的真实点击关掉，再发。
- **范围**：所有有营销弹窗 / 引导层的平台。**「点击没反应」先看有没有遮罩，别急着换选择器。**

### L-31 平台会持久化草稿，刷新也删不掉（实测，Kimi）

- **现象**：上一轮发失败后，刷新页面输入框里又变回那 3477 字的原文，长度还少了 100 多（markdown 被渲染掉了）。
- **根因**：Kimi 把草稿存进了本地存储，**页面重载不会清空它**——而这恰恰是「上一轮到底写进去没有」的好证据。
- **处置**：① 清空用原生按键（focus → `Ctrl+A` → `Backspace`），`execCommand('delete')` 对它无效；
  ② 清空前先把草稿内容抓下来与原文比对，**指纹命中就说明上一轮确实写进去了**（是发送环节失败，不是写入环节）。
- **范围**：所有自动保存草稿的编辑器。**「残留内容」是免费证据，先读它再删它。**

### L-32 平台的档位/套餐决定要不要排队（实测，Kimi）

- **现象**：同一段话，同样的账号，关掉限流弹窗后立刻重发就成功了。
- **根因**：Kimi 的模型选择器（`div.current-model[data-testid="model-select-trigger"]` → `button.model-item[data-testid="model-option"]`）
  有 K3.1 Pro / K2.8 / **快速** 三档，外加一个「思考强度」档；不同组合的排队策略不同。
- **处置**：先显式点选目标档位并核验 `aria-checked="true"`，再发；
  菜单项用 `data-moon-key`（`k3d1-agent` / `k28-agent-preview` / `k28-instant`）定位，比读文案稳。
- **范围**：所有按档位分级限流的平台。**平台侧排队是平台侧的事，别拿代码硬扛——先按用户指定的档位切过去。**

---

## 六、待补充

踩到新坑就往这里加，格式照上面的六段：**现象 → 根因 → 处置 → 范围**。

每条必须注明是**实测**还是**推断**，推断类要写清依据。

---

## 附录：O3 阶段自动回流（2026-10-29）

- [L-KIMI-KILL-WINDOW-MISSED-09385f][2026-10-29][O3][runs/20261029T042822-recovery2][kimi][恢复实测 kill 窗口错过：平台回复约 10 秒，等待进程在 kill 动作落地前已自然走到 REPLY_VERIFIED，未形成 kill 证据][][在 AWAITING_REPLY 窗口内尽早 kill（编排进程起后 4 秒内动手）；按裁定重跑而不是拉长 sleep/预置假状态凑证据][python scripts/orchestrator.py audit --run 20261029T042822-recovery2][merged]
- [L-KIMI-RESUME-METRIC-SCOPE-d59c8d][2026-10-29][O3][runs/20261029T042822-recovery2][kimi][恢复与审计的「恢复后发送数」口径错：把 kill 之前那次合法发送算进分界，resend_attempts=1、sends_after_resume=1][][以 resume_marker 为分界：标记之后的发送才算；没有标记就记 0（没恢复过就不该判失败）][python scripts/orchestrator.py audit --run 20261029T042822-recovery2][merged]

---

## 附录：O3 阶段自动回流（2026-10-29）

- [L-KIMI-DELTA-METRIC-SCOPE-76e8ca][2026-10-29][O3][runs/20261029T043211-turn4][kimi][reply_count_delta 取的是消息总数：一条提问 + 平台一条回复被算成 delta=2，看着像重发，其实不是][][探测新增 reply_count（助手回复条数，平台无关）；delta 按它算，verify 判据收紧为 ==1；state 里 msg_count_* 与 reply_count_* 分开记][python scripts/write_gate.py verify --approval ap-7d0d3f41f0f4a0bb-5000c2a3db494690][merged]
- [L-CHATGP-CHATGPT-DEFERRED-f6319c][2026-10-29][O3][runs/20261029T043211-turn4][chatgpt][ChatGPT 只读探测被硬规则 1 纪律搁置：已实测但本轮未绑定，按裁定不临时绑定、不开侧标签页，矩阵记 deferred_rule1][][待人明确把项目改绑到 ChatGPT 地址后再跑 probe（改绑=架构师级决定，执行侧不发起）；在此之前 send_allowed 保留历史实测结论但本轮不执行][python scripts/wa.py probe --platform chatgpt && python scripts/orchestrator.py matrix --probe][人把项目显式改绑到 ChatGPT 对话地址之后（执行侧不发起改绑）][不改绑即维持「只读不探测」，功能与今天完全一致][false][open]

---

## 附录：O5 优化轮回流（2026-10-29，DeepSeek 评审驱动）

> 来源 run：`runtime/e2e2/runs/20261029T054159-e2e2`（真机跑通 INIT→ROUND_DONE）

- [L-DEEPSE-REPLY-TIMEOUT-a8ab2e][2026-10-29][O5][runs/20261029T054159-e2e2][deepseek][等回复用计数判：虚拟列表把旧气泡卸载后 reply_count 从 2 还是 2，回复 2597 字已渲染却判 reply_timeout，两次触发 HANDOFF][div.ds-virtual-list 只渲染视口附近；grep data-virtual-list-item-key 可见消息身份][wait_for_reply 接发送前锚点快照，走 platforms.anchor_delta（新锚点>=1 或末条变化）；锚点不可信才退回计数][python -m unittest tests.test_orchestrator_gaps.TestWaitReplyAnchor][任何虚拟列表平台（DeepSeek/Kimi）在长对话里连续对话][回退到计数口径会重新出现 300 秒空等；若必须回退，把 round_timeout 降到 60 并接受误判][false][merged]
- [L-DEEPSE-BROWSER-AMBIGUOUS-95889b][2026-10-29][O5][runs/20261029T054159-e2e2][deepseek][同一账号开了两个浏览器实例时 bind/session start 直接报 multiple browsers are online；工具没有把「用哪个实例」记进 state，每次都要人肉传环境变量][bsk browsers 显示 2 个 instance（7cb0cac4 京东老浦家 / ec39f216 京东佰意佳）][ensure_session 把 WA_BROWSER 给的实例持久化到 state.browser（含早退路径），下次不必再传；多实例且记不住时仍然拒绝猜][python scripts/orchestrator.py windows][同一台机器多个 Edge profile 同时在线][删掉 state.browser 即回到「必须显式指定」的旧行为][false][merged]
- [L-DEEPSE-WEAK-ANCHOR-07ef6e][2026-10-29][O5][runs/20261029T054159-e2e2][deepseek][正文指纹做锚点仍会被折叠/markdown 重排/连续两轮同话题骗到；DeepSeek 每条消息外层本来就有 data-virtual-list-item-key，是现成的消息身份却没用上][div[data-virtual-list-item-key] 包住 div.ds-message；user 为负键、assistant 为正键][js_probe 增加 asst_keys/msg_keys（data-virtual-list-item-key → data-message-id → data-id → data-index）；anchor_delta 优先用 key，其次才用正文指纹，并回报 anchor_kind][python -m unittest tests.test_confirm.TestKeyAnchor][同一对话连续多轮、或回复被折叠成摘要气泡时][平台改版去掉该属性时 anchor_kind 会变 fp，功能不退化为计数，无需回滚][false][merged]
- [L-DEEPSE-WEAK-REPAIR-EDGE-048b4d][2026-10-29][O5][runs/20261029T054159-e2e2][deepseek][修补型迁移只校验「谁是 actor」，人一被诱导点就能把状态往前推，等于用一个身份替代一次证据][][PAUSED_HUMAN→AWAITING_REPLY 增加前置断言：必须有 last_send.send_id 且 verdict∈INCONCLUSIVE*；已判清的没等到回复按真失败处理][python -m unittest tests.test_orchestrator_gaps.TestWaitReplyAnchor][任何「回到等待态继续等」的恢复动作][删掉 repair_precondition 里的断言即回到只看 actor 的旧行为][false][merged]
- [L-DEEPSE-STREAK-DRIFT-e313d1][2026-10-29][O5][runs/20261029T054159-e2e2][deepseek][fail_streak 散落在多处裸 +=1 / =0：成功清零、失败加一、无结论不动这三条规则各写一处，新增一个 verdict 就会有人忘了归类][][VERDICT_EFFECT 表 + apply_verdict_effect 单一写入口；未知 verdict 按失败处理（宁可停）；源码守卫测试禁止裸写][python -m unittest tests.test_orchestrator_gaps.TestVerdictEffect][新增判定结果（verdict）或新增失败类型时][把 VERDICT_EFFECT 里缺失的 verdict 补一行即可，不需要回滚结构][false][merged]
- [L-REQUIRE-CONFIRMWORD-d26e53][2026-10-29][O5][—][—][需求闸门的「确认记录」按 section 切片取最后一行：往该节里加一张技术裁定表，最后一格是文件路径，于是报 NOT_CONFIRMED（detail 里是个路径，看不懂）][确认记录是固定词表：表格行最后两列不出现 已确认/待确认/部分确认/未确认 就当不是确认行][按词表筛确认行；认不出的行计入 stray_rows 报出来，不静默丢；全部认不出 → NO_CONFIRMATION][python -m unittest tests.test_orchestrator_gaps.TestCardGate][往「五、确认记录」里塞任何别的表格时][放宽词表会退化成「什么都算确认」，方向错；要放松就改成显式 --allow-any-table][false][merged]

---

## 附录：O5-b（Gemini 平台实测）回流（2026-10-29）

> 来源 run：`runtime/g1/runs/20261029T060850-g1`（真机 bind→native 写→发送→等待→读取→收口）
> 自动候选里那条 `fill_failed`（症状是多行 cdp_failed 原文）已删除：同一根因已由
> `L-GEMINI-FILL-FAILED-3fa03a` 完整记录，且自动条目的「处置建议」原文是
> 「受控编辑器要用 Range+insertText」——对 Quill **正是错的那条**。提示语源已一并修正。

- [L-GEMINI-FILL-NOT-ACCEPTED-853ddf][2026-10-29][O5][runs/20261029T060850-g1][gemini][Gemini 的编辑器是 Quill：execCommand('insertText') 把字写进 DOM（innerText 读到 15 字），但 Quill 的 model 是空的——ql-blank 类还在、发送键不渲染、点发送无反应][div.ql-editor.ql-blank；空态下 button[aria-label=发送] 命中 0 个][写入按平台走 native（真实键盘）；判「写进去了没有」只看两条：空态类消失 + 条件渲染的发送键出现，不看 innerText][python -m unittest tests.test_fill_guard.TestFillCheck tests.test_platforms_replay.TestGeminiReplay][接入任何受控富文本编辑器（Quill / ProseMirror / Lexical）][退回 exec 写入会重现「看着有字、发不出去」；若必须退回，先加 fill_check 兜住][false][merged]
- [L-GEMINI-FILL-FAILED-3fa03a][2026-10-29][O5][runs/20261029T060850-g1][gemini][bsk fill 返回 cdps cdp_failed（fill could not verify the expected value）：它拿自己期望的值和页面严格比对，多行文本被编辑器重排后判失败——但 editor_len=1574、can_send=true，字其实已经写进去了][editor_blank=false 且 editor_len≈正文长度 且 can_send=true][工具的自我校验不是判据：fill_native 不看退出码，填完立刻 probe + fill_check，由页面事实定性；把 rc/首行错误存进 note 备查][python -m unittest tests.test_fill_guard.TestWriteWhitelistIsShared][任何「工具说失败但可能已生效」的动作（导航 load 超时、长文本输入、异步落盘）][改回看退出码会重新出现「明明写进去了却报失败」，并白耗一轮预算][false][merged]
- [L-GEMINI-NAV-TIMEOUT-7de372][2026-10-29][O5][runs/20261029T060850-g1][gemini][Gemini 是长轮询页面，load 事件永远不触发：navigate --wait-until load 30 秒超时返回非 0，但页面一开始就可用，bind 直接被判失败][location.href 已在目标地址、rich-textarea div.ql-editor 存在][navigate_soft：load → domcontentloaded 退让；仍失败也不判死，由「地址对不对 + 编辑器在不在」定性（事件没触发≠导航失败）][python scripts/wa.py bind <url> # 实测：nav_note 空、bound ok][SPA / 长轮询 / 有持久连接的页面（Gemini、多数 Web AI 产品）][回退到严格 load 判定会让 bind 对这类平台永久失败，只能人工点浏览器][false][merged]
- [L-GEMINI-WEAK-ANCHOR-5486d6][2026-10-29][O5][runs/20261029T060850-g1][gemini][只凭「祖先 id 是纯十六进制(≥8位)」当消息身份过于宽松：路由哈希、组件 uid、layout 容器的随机 id 都长得一样][div.conversation-container#<16hex>，一问一答共用同一个 id][平台显式声明 anchor_box（Gemini=div.conversation-container），有它只认它的 id；泛化兜底加组合约束——十六进制且祖先类名匹配 message|conversation|turn|chat|response][python -m unittest tests.test_platforms_replay.TestGeminiReplay.test_anchor_box_has_unique_hex_ids][接入把消息包进容器且容器带 id 的平台][去掉 anchor_box 会退化成十六进制启发式，误命中风险回升但不致命（只影响增量判定）][false][merged]
- [L-GEMINI-REPLY-MISSED-f366c7][2026-10-29][O5][runs/20261029T060850-g1][gemini][回复被流式写进**已有**容器时，锚点集合差恒为 0、末条锚点也不变，锚点口径会判「没收到回复」——这是锚点口径唯一的盲区][model-response 数量不变，但 .model-response-text 的 innerText 长度单调增长][wait_for_reply 加第三条见证 growth：末条正文显著变长（绝对≥40字 且 ≥2%）+ 不在生成 + 连续两次长度不变；返回值记 witness=growth 以便复盘][python -m unittest tests.test_fill_guard.TestReplyGrowth][任何在同一容器内流式渲染回复的 SPA][去掉 growth 会漏判这类回复（白等到超时）；保留但记 witness 可事后分辨误判][false][merged]
- [L-GEMINI-RECOVERY-DEADEND-8b0699][2026-10-29][O5][runs/20261029T060850-g1][gemini][写完没发出去就停在 PAUSED_HUMAN 时，输入框里残留 1574 字，写闸门守卫正确地拒绝再写，但工具没有清空编辑器的法子——恢复路径直接断在「只能人工点浏览器」][清空前 editor_len=1574；清空后 ql-blank 回归、can_send=false][新增 wa.py clear：编辑器内 Ctrl+A + Delete（真实键盘），清完复核 editor_blank；清不干净如实报失败][python scripts/wa.py --platform gemini clear --state runtime/g1/.wa-state.json][任何「已写入未发送」的 PAUSED_HUMAN 恢复][没有回滚必要：清空是幂等动作，且不产生对话内容][false][merged]
- [L-GEMINI-COUNT-UNSTABLE-a08a0b][2026-10-29][O5][runs/20261029T060850-g1][gemini][Gemini 历史是懒加载的：重载后只渲染尾部约 10 轮（整段历史更多），msg_count/reply_count 与重载前不一致；跨会话比较计数或锚点集合没有意义][infinite-scroller 只挂载视口附近的 conversation-container][只做**同一页面同一时刻前后**的对比（before/after 都在同一次运行内采），不跨会话比；锚点只留尾部窗口，不假设全量可见][python scripts/wa.py --platform gemini probe # 重载后 reply_count 由 11 变 10，而 last_len 仍是新回复][重连、刷新、切标签页、daemon 重启后再探测][若将来要跨会话对账，必须先落盘本轮锚点快照，不能靠现场计数][false][open]

---

## 附录：O5-c（对话归属门禁）回流（2026-10-29）

> 来源：用户实测反馈 ——「Gemini 刚刚对话有一个问题，是用了我之前的对话，不是项目单独的新对话」。
> bind 直接吃下了浏览器里用户正在聊的那条对话（msg_count=10），项目的提问被插进用户自己的对话里。

- [L-GEMINI-FOREIGN-CONV-ff592c][2026-10-29][O5][runtime/g1][gemini][bind 只校验「地址形态像不像对话」，对「这条对话里已有 10 条消息」只打一行警告就放行——项目整个挂到了用户正在聊的对话上：上下文是旧的、发送会污染用户对话、锚点基线全是别人的][事故 state：url=/app/a8b8143806a8b296、msg_count=10、new_chat=false；且重绑同地址时首次 probe 读到 msg_count=0（懒加载页面主区还在转圈，截图为证）——「加载中的假空」是「工具自证≠事实」的第四变种][三处联动：① platforms.bind_verdict 归属门禁（conv_id + msg_count + conv_origin 三元，首次绑非空对话默认拒绝，--reuse-existing 才放行；state 缺 conv_origin 的老数据不许靠 url 相等自证清白）；② wa.settle_msg_count 先等读数稳定（连续 3 次相同）再做归属判定；③ gemini 补 new_chat_url=/app（home 会被重定向回最近对话），bind --new 才是拿干净上下文的正路；发送首轮跳转后落盘 conv_id + conv_origin=new_chat][python -m unittest tests.test_bind_guard # 真机：bind --new → msg_count=0 → send → conv_origin=new_chat → 回复确认「全新上下文」][绑定任何平台对话时，尤其浏览器里已开着一条正在聊的对话][去掉 bind_verdict 与 settle_msg_count 即回到旧行为，「项目挂到别人对话」必然复发][false][merged]

---

## 附录：O5-d（豆包平台实测）回流（2026-10-29）

> 来源：D:/Project/Starpilot runtime/db 侦察链（登录态、写入、发送、等待、读取全链路真机跑通）。
> 豆包由此升 verified 平台（第 5 个），全链路：bind --new → send（enter 路径）→ wait → read 1646 字回复完整。

- [L-DOUBAO-ENTER-SEND-3c91f7][2026-10-29][O5][runtime/db][doubao][豆包编辑器是 Tiptap ProseMirror：bsk fill（键盘模拟）两次报「the page moved focus away from the fill target」，先 click 聚焦也一样；而 execCommand('insertText') 有效（execOk=true 且 len 一致）——与 Gemini 的 Quill 相反；且发送不能点按钮：button#flow-end-msg-send 存在但 bsk click 无反应，点击还会把焦点从编辑器移走，之后 Enter 也发不出][fill×2 失败 + click 后 edLen 仍 7 + 手工「ed.focus()→press Enter」发出（URL 跳 /chat/<id>、编辑器清空）][写入走 exec（write_allowed=["exec"]）；发送新增平台级 send_method（click 默认 / enter=聚焦编辑器+press Enter），唯一分派 wa.do_send，CLI 与 write_gate 同源；enter 路径先 js_focus_editor 断言 activeElement 是编辑器][python -m unittest tests.test_bind_guard.TestDoubaoReplay][接入 Tiptap/ProseMirror 类编辑器的平台；或任何「发送按钮点了没反应」的平台][退回 fill/点击会复发「写不进、发不出」；豆包改回 click 前必须先重新实测][false][merged]
- [L-DOUBAO-NOISE-READ-b2e40a][2026-10-29][O5][runtime/db][doubao][豆包把「推荐提问」（猜你想问）渲染在回复节点 receive_message **内部**：读末条回复会把 3 条推荐问题当 AI 正文读进来（81 字里 51 字是 UI 文案）；且探针文字会出现在侧栏会话标题（conversation-item）里，找消息节点不限 main 会数到侧栏][读出 81 字，其中尾部 3 行是 suggest_message_list 的推荐问题；侧栏命中 conversation-list-v2-item][平台定义加 sel.read_ignore（读时剔除的噪声子树）：js_read_last 在 clone 上 remove 掉 data-testid=suggest_message_list 节点再取 innerText，页面原文不动；找消息节点限定 data-testid=message-list 容器][python scripts/wa.py read --platform doubao # 剔除后 81→30 字，只剩 AI 真正文][平台把推荐/广告/操作栏渲染进消息节点内部的（很多国产平台都这样）][去掉 read_ignore 会把 UI 文案当指令执行——方向错误，不许回退][false][merged]
- [L-DOUBAO-REGEN-ICON-0d7e42][2026-10-29][O5][runtime/db][doubao][豆包的「重新生成」是纯 icon 按钮：无 aria-label、无 title、无文本，通用探测（aria-label 匹配「重新生成」）恒 false；且生成中没有任何「停止」按钮可抓——完成信号只剩 regen 出现这一条][完成态 probe regen=false 而页面上 data-testid=message_action_regenerate 存在；生成中 regen=0/stop=0/stopAny=0][平台定义加 regen_selector（平台声明优先）：js_probe 先查声明节点再退回 aria-label 文本匹配；豆包完成三见证=regen 出现 + 文本两次采样一致（无 stop 见证）][python -m unittest discover # probe 实测完成态 regen=true][接入用纯 icon 按钮做操作栏的平台][去掉 regen_selector 会退回恒 false，wait 只剩文本稳定单见证，误判面变大][false][merged]
