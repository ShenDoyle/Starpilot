# RUNBOOK · 平台 DOM 改版重标定

> **什么时候用这份**：`probe` 报 `editor: false`、`triage` rc=13 `selector_stale`、
> 或「页面明显在动但读不到正文/发不出去」。**不要**用换平台、刷新、狂点来应付。
>
> 平台改版是**必然事件**，不是故障。目标：改动后 15 分钟内重新标定，而不是现场摸索两小时。

---

## 0. 铁律

1. **先证明是改版，再改代码**。页面在动 + 我们的选择器读不到 = 改版；页面不动 = 别的问题（先看 rc=12/15）。
2. **只改 `scripts/platforms.py`**，不许在业务代码里塞新选择器（两份实现必然漂移）。
3. **改完必须标 `verified: false` + `write_verified` 分账**——没跑通 5 连问答前不许宣称已验证。
4. 全程只读优先：**先用 `evaluate` 摸结构，不要一边猜一边真发**。

---

## 1. 取证（3 分钟，只读）

```bash
python scripts/wa.py probe                 # 拿到 editor/msg_count/reply_count/generating
python scripts/wa.py triage                # 判定是哪一态（rc=13 才是改版）
bsk evaluate --session <sid> "<探测表达式>" # 摸真实 DOM 结构
```

取证必须落盘三样：`probe` 输出、`triage` 输出、DOM 探测结果（结构摘要，不要全量 HTML）。

## 2. 定位四步（2026-11-02 ChatGPT 实战提炼）

平台改版最常见的三个坑，按顺序排掉：

### 2.1 坑一：全局选择器命中的不是你以为的那个元素

ChatGPT 2026-11 实测：线程内**长消息以 writing-block 形式渲染，也是 `contenteditable`**。
`document.querySelector('div.ProseMirror')` 命中的是线程里的写作块，不是输入框。
结果：字注进了线程草稿，输入框永远空，发送按钮永远不出现，甚至把渲染器拖死。

**判据**：`innerText` 有内容，但 aria snapshot 里 composer 仍是 `[empty]` → 命中了错的元素。

**做法**：不要用全局选择器，用**结构锚点**收窄：

```js
// 找真正的 composer：按容器类名前缀过滤，并核对尺寸
[...document.querySelectorAll('[contenteditable="true"]')].filter(e => {
  var p = e.parentElement;
  for (var i = 0; i < 5 && p; i++) {
    if ((p.className||'').toString().indexOf('ComposerLayoutInput') >= 0) return true;
    p = p.parentElement;
  }
  return false;
})
```

**通用规则**：给 composer 加**尺寸断言**（宽 > 200px、高 < 200px）——线程里的写作块通常上千 px 高。

### 2.2 坑二：发送按钮不再是 `button[type=submit]`

ChatGPT 2026-11 实测：`data-testid="send-button"` 已弃用；发送按钮是 `button[aria-label="发送"]`，
且**只在 composer 非空时存在**（空框时那里是语音按钮）。

**做法**：按 `aria-label` 多语言候选 + 图标特征双判，并接受「暂时不存在」这个状态：

```js
var cands = ['发送','Send','提交','Submit'];
[...document.querySelectorAll('button')].filter(b =>
  cands.some(k => (b.getAttribute('aria-label')||'').includes(k)));
```

**禁止**：`form button[type=submit]` 硬编码（对话页与主页形态不同，实测对话页无匹配）。

### 2.3 坑三：cooperate context 单次消费

`bsk fill` / `bsk click` / `bsk press` 在 ChatGPT 对话页持续报
`observed content changed`（`effect_state: unknown`），即使紧挨着 sync 也一样；
`fill` 稳定报 `fill_focus_lost`（`fill target changed or lost focus while positioning the caret`）。

**这不是配置问题，是当前页面的动作门与编辑器不兼容。** 处置顺序：

1. 先试 ref（`@eN`）而不是 CSS 选择器。
2. 仍失败 → 用 `evaluate` + `execCommand('insertText')` 写入、`button.click()` 发送（**属受控降级，不是绕过：判据照旧三重**）。
3. 降级时必须记录：`backend` / 选择器 / 判据结果，并在 lessons 里登记一条。

### 2.4 坑四：大文本注入把渲染器拖死

实测：把 3.7KB Markdown 注进 writing-block 后，整个 tab `A page frame is not responding`，
`screenshot` 30 秒超时，`cooperate sync` 全挂。

**处置**：`bsk session stop <sid>` → `bsk session start --browser <id>` →
`bsk navigate <同一个 conv_id 地址>`（服务端对话不丢）→ 重新注入。
**教训**：注入前先确认目标是 composer（2.1 的尺寸断言），别往线程块里灌长文。

---

## 3. 写回平台事实（必须成组改）

改 `scripts/platforms.py` 时，下面这几项**一次改齐**，否则会出现「写对了读错」：

| 字段 | 改什么 |
|---|---|
| `sel.editor` | 首选结构锚点（容器类名前缀），退化候选放后 |
| `sel.send` | 首选 `aria-label` 多语言候选，退化候选说明适用页型 |
| `messages` / `msg_containers` | 计数口径；失效就留空并注明失效原因 |
| `verified` / `write_verified` | **分开标**：读通了标 read，写通了标 write，没跑通 5 连问答不许标 verified |
| `notes` | 写日期 + 实测结论 + 失效的旧值（后来人要知道为什么改） |

## 4. 验证（不跑完不算改完）

```bash
python scripts/wa.py probe                 # editor:true, 字段齐全
python scripts/wa.py send --file <小样>     # 三重判据：msg_count 增量 + conv_id + 指纹
python scripts/wa.py wait --timeout 120    # reply_count 增量 == 1
python scripts/wa.py read                  # 正文首尾完整
python scripts/orchestrator.py matrix      # 看该平台 send_allowed / read_probe 两账
```

**5 连问答**（`orchestrator.py matrix` 要求的标准）：连续 5 次独立提问→回复→关联成功，
中间不许靠人工干预。少一次就标 `write_verified`，不标 `verified`。

## 5. 修不好怎么升级（不许硬试第三种）

取证齐全后按 `templates/protocol.md` §3 转交网页版 AI，附件三样：probe 输出、triage 输出、DOM 结构摘要。
转交时明确写：「已排除 A/B/C 三种可能，现在卡在 D」——空白转交等于把问题原样丢回去。

## 6. 禁止动作

- ❌ 改选择器前不取证（改了也不知道对不对）
- ❌ 在业务代码里临时塞选择器
- ❌ 没跑通就更新 `verified: true`
- ❌ 用「刷新页面」当诊断手段（会杀掉生成中的回复）
- ❌ 同一个平台连改三版选择器还不做结构锚点分析
