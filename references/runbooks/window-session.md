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
| `bind` 反复被拒 rc≠0 | 会话反复失效 | 停下来看 `doctor` + 最近一次 `wait` 的 `stage`，别用重试掩盖 |

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
