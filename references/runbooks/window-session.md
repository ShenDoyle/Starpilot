# RUNBOOK · 窗口 / 会话 / 渲染器

> **什么时候用这份**：`WINDOW_UNVERIFIED`、会话被顶掉、`msg_count=0` 但页面正常、
> 同一浏览器多份扩展、`A page frame is not responding`、`screenshot` 超时、窗口堆积。
>
> 这类问题的共同点：**页面本身可能是好的，坏的是"我们和页面之间的那条线"。**
> 先分清坏在哪一层，再动手——90% 的错误处置是"以为页面坏了，去刷新页面"。

---

## 1. 三层定位（先定位，再动手）

| 层 | 判据 | 典型症状 |
|---|---|---|
| L1 归属层 | `wa.py window status` 的三态 | `WINDOW_UNVERIFIED`；字打到用户窗口 |
| L2 会话层 | `wa.py doctor` 的 `sessions` / `duplicates` / `lease` | `msg_count=0`、读不到回复、会话不见 |
| L3 渲染层 | `screenshot` 超时 / `cooperate sync` 报 frame not responding | 页面卡死、DOM 查询全超时 |

**定位顺序不可颠倒**：L3 的症状也可能是 L1/L2 引起的（比如会话被停 → 页面无响应）。

## 2. L1 归属层

```bash
python scripts/wa.py window status          # 只读：三态 + 判定时间
python scripts/wa.py doctor                 # window.ok / window_kind
```

- `verified` → 这层没问题，往下查。
- `misplaced`（开在用户窗口）→ **禁止发送**。人工确认后在用户侧把页面挪回，
  仍不行就走显式接管：`wa.py window release --confirm '<谁确认、为什么>'` → 重新 `bind`。
- `unknown`（探不到）→ 先查扩展：版本 ≥0.4.0、Automation 设置为「允许独立 Agent Window」、
  扩展是否被停用。修完 `bind` 重探。
- **没有逃生阀**：不许用环境变量 / 调试参数硬过（阶段一已删）。

## 3. L2 会话层

| 症状 | 判据 | 处置 |
|---|---|---|
| `msg_count=0` 但页面正常 | state 里的 session 已不存在（被手工 session 顶掉） | `wa.py bind <同一个 url>`（**不加 `--new`**） |
| 总是读不到回复 + 一堆标签页 | `doctor.duplicates` 非空（同浏览器多份扩展） | 让人在扩展页删到只留一份 → `doctor` 确认 `ok:true` |
| 租约过期但窗口还挂着 | `doctor.lease.left_s <= 0` | `wa.py reap --once`（常驻用 `--watch`） |
| 另一个任务在抢同一平台 | `wa.py plock` 显示他人持有 | 双方都停；错开平台或 `STARPILOT_LOCK_KEY`；**不自动重绑** |
| **另一个任务在抢同一浏览器实例** | `wa.py lease status` → `state: busy`，`owner` 是别人的工作区 | **不抢**：等它收口 / 换在线实例 `--browser` / 确认对方已死且人工授权 → `lease takeover --confirm '<说明>'`。详见 §3.1 |
| **某个会话突然消失、自己没停过** | `session list` 里没了，`lease status` 的 `sessions` 也没记它 | 对方用的是**旧版**（无租约）：它的 `enforce_single_session` 会清掉同实例所有会话。处置：让对方升级/同步安装位，或换实例；本方重 `bind` 同一 url 复接 |
| 租约过期但窗口还挂着 | `doctor.lease.left_s <= 0` | `wa.py reap --once`（常驻用 `--watch`） |
| `bind` 反复被拒 rc≠0 | 会话反复失效 | 停下来看 `doctor` + 最近一次 `wait` 的 `stage`，别用重试掩盖 |

### 3.1 多任务共用一台浏览器：租约的三条边界

2026-11-02 现场：任务 A（本项目，13:40:56 开 `wibt`）被任务 B（另一个工作区，
13:40:57 开 `dxfp`）在 60 秒内顶掉；B 的 state 里**没有 `browser` 字段**，旧清理逻辑
`if browser and ...` 的过滤因此全失效，把**所有实例**上所有会话都停了。双方事后都不知情。

| 边界 | 规则 | 判据 |
|---|---|---|
| 归属 | 一个浏览器实例只归一个**活**租约；心跳新鲜即「有主」 | `wa.py lease status` → `state: busy` / `remaining` |
| 清理 | 只停**自己名下**的会话；别人的活会话跳过并打印原因 | 输出里出现「跳过清理：会话 X 被另一个活任务声明」 |
| 接管 | 只能显式 `takeover --confirm`，覆盖活租约并落 journal | `~/.starpilot/locks/win-<实例>.journal.jsonl` |
| 降级 | 实例探不到 `tab list --scope`（扩展 <0.4.0）→ 自动串行，有界等待 `STARPILOT_SERIAL_WAIT`（缺省 120s），等不到仍 rc=9 | 输出 `WINDOW_BUSY` + `waited` |

**诊断顺序**（不要跳步）：`wa.py lease list`（全局谁占了什么）→ `wa.py lease status`
（当前实例细节）→ `wa.py doctor`（实例/扩展/会话）→ 再决定等、换实例还是找人接管。


## 4. L3 渲染层（渲染器卡死）

实测（2026-11-02）：把长文注入错元素（线程 writing-block）后，整个 tab 卡死——
`cooperate sync` 报 `A page frame is not responding`，`screenshot` 30s 超时，`evaluate` 也挂。

**处置顺序（别跳步）**：

```bash
bsk cooperate wait --session <sid> --timeout-ms 15000   # 1) 先等，可能是瞬时卡顿
bsk screenshot --session <sid> --out <png>              # 2) 能出图 = 没死，继续查 DOM
bsk session stop <sid>                                   # 3) 真死了：停会话（收窗口）
bsk session start --browser <instance_id>                # 4) 新会话
bsk navigate --session <new_sid> "<同一个 conv_id 地址>"  # 5) 按 conv_id 重开（服务端对话不丢）
```

**关键**：重开时**必须用原 conv_id 地址**，不要开新对话——上下文还在服务端，开新对话等于丢记忆。
重开后 `wa.py bind <同一个 url>` 复接，并重跑 `probe` 确认 `editor:true`。

## 5. 输出要求

报告固定三行：

```
层：L1 归属 / L2 会话 / L3 渲染
证据：<window status 或 doctor 的关键字段，原样>
处置：<做了什么 / 需要人做什么>
```

## 6. 禁止动作

- ❌ 归属未知就"试试能不能发"（打错窗口 = 往用户正在用的窗口打字）
- ❌ 用环境变量越过窗口门（已删除，也不许加回来）
- ❌ 渲染卡死就反复 `Ctrl+R`（可能连带杀掉生成中的回复，且掩盖真实原因）
- ❌ 会话丢了就开新对话（= 丢上下文；除非走 `rebind` 正式流程）
- ❌ 多份扩展并存时继续跑（发送在 A、读回在 B，所有判据都失真）
- ❌ 抢占别人的窗口租约、或停掉别人名下的会话（对方正跑到一半，你停的是别人的现场）

## 7. 多任务轮换协议（2026-11-02 用户指令）

一台浏览器 = 一份租约（锁目录共享）。谁都能用，但**一次只有一个**在用：

```
任务 A：acquire → 干活（send/read）→ lease release（会话保留）
任务 B：          └── 等 A 释放后拿到租约 → 复用同一个组窗口 → 干活 → release
```

| 你的状态 | 被占时怎么办 |
|---|---|
| 有人值守（`WA_ATTENDED=1`） | 等 30s；仍被占 → `WINDOW_BUSY_HUMAN`（rc=9），**把现场报给人**：谁占着、还剩多久、可选动作。由人裁决 |
| 无人值守（默认） | 等 600s 轮换排队（对方 release 即拿到）；等不到 → `WINDOW_BUSY`（rc=9）退出，外层循环稍后重试，**不打扰人** |

铁律：

- 拿到租约后**先复用组窗口**（journal 登记的组内活会话），在里面建/复用自己的 tab；
- 导航前先查同地址标签（`navigate_reuse`），同一对话出现第 2 个标签 = 违例；
- 一轮做完就 `wa.py lease release`——会话是组的，租约是轮到谁归谁；
- 全局开窗计数（10 分钟/实例 ≤4 次，跨任务共享）超限时先查环境，不许硬闯。

## 8. 人工介入语义与 tab 生命周期（2026-11-02 用户裁定 v2）

**介入≠打断**：人工介入是并行监督，不是执行暂停。有人值守（`WA_ATTENDED=1`）
被占时的行为已改——等 30s 仍被占 → `WINDOW_BUSY_NOTICE` **提醒人**（非阻塞，
报占用者/剩余时间/可选动作），然后**继续等满 600s 轮换预算**；除非人主动要求
停止，星舵持续推进直到任务完成（规划、完成情况汇报、验收全链路）。
`WINDOW_BUSY_HUMAN`（rc=9 报人裁决即停）已废止。

**tab 生命周期**（只要使用 bsk，必须遵守）：

| 阶段 | 规则 | 唯一通路 |
|---|---|---|
| 打开 | 先查同地址标签，有 → select 复用；无 → 才新开 | `navigate_reuse`（wa.py 内部） |
| 自愈 | 归属 misplaced 时复用优先：同址标签 → 空白残留 → 最后才 create | `heal_window_placement`（wa.py 内部） |
| 回收 | 会话复用/续租时自动收：同地址重复（留最新/活动）+ 空白残留 | `reap_own_tabs`（ensure_session 自动）/ `wa.py tab-reap`（手动，--dry-run） |
| 用户窗口 | scope=user 的标签**只报告不动**，由人关闭 | `window_report` 的 bound_in_user 上报 |

- 脚本层直接 `bsk tab create/close` = 违例（`contract_lint.no_raw_tab_ops` FAIL）。
- L2 PATH shim 在调用层拦截：wa.py 正路用**一次性 HMAC 票据**
  （`STARPILOT_WINDOW_TICKET`，`winlease.gate_issue` 签发、shim 兑换即焚、120s
  过期、不可重放）直接放行；无票的非 wa.py 来源须过全局计数，超限拒绝并指引正路。
  **旧 `STARPILOT_WINDOW_GATE=1` 已废弃**（布尔量谁都能设，等于没有门）。
- **fail-closed**：shim 自身故障（winlease 不可导入 / 计数读不出 / 记账写不进 /
  票据校验异常）一律 rc=9 拒绝，不再 fail-open 放行。

**人的想法转达**：流程中人提出的想法/意见/疑问（含选型类）必须原样整体转达
webai，由 webai 出决策与方案；agent 只补事实与现场证据（SKILL §5.5b）。
