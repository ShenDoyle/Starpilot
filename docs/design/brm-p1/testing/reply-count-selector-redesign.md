# 任务C · ChatGPT 回复计数修复设计（2026-11-03，只读分析 → 设计，本轮不改代码）

> 裁定要求：不是把 verified 翻 True，而是重建可靠回复识别机制。设计须支持后续 5/5 独立问答验收。
> 证据来源：`get-html` 实测目标对话渲染区原始 HTML（994KB，2026-11-03 17:45-18:15 多时点快照）。

## 一、现状与失败根因

`platforms.py` chatgpt 块（第 127-141 行）：
- 11-02 改版后 `data-message-author-role` 属性**整体消失**（实测 HTML 中出现次数 = 0）
- `sel.messages = ["[data-message-author-role=\"assistant\"]"]` → 永远 0 命中 → `reply_count` 恒 0 → `reply_count_delta` 不可计算
- `verified` 因此诚实置 False → 编排器写闸门拒循环写 → 每轮交付被迫走逃生门（32 条 P0-ESC 的直接根因）
- 写路径（editor=div.ProseMirror、send=form button[type=submit]）11-02 复测仍有效，`write_verified=True` 不受影响

## 二、新 DOM 契约（2026-11-03 实测）

| 元素 | 选择器 | 稳定性 |
|---|---|---|
| **消息身份（硬标识）** | 容器属性 `data-chatgpt-search-message-ids="<uuid>"`；内容属性 `data-chatgpt-selection-message-id="<uuid>"` | 高：UUID 由后端分配，跨重排/重渲染不变；**同文重发各自持有不同 UUID** |
| 用户消息头 | `h4[data-conversation-role="user"]`（内含 sr-only「你说：」） | 高（角色属性改名但语义保留） |
| 用户气泡 | `[data-user-message-bubble]` | 高 |
| 助手正文 | `div[data-markdown-text-style="assistant-message"]`（祖先持 selection-message-id） | 高 |
| 助手回复头 | `h4[data-conversation-role="assistant"]` | 高 |
| 轮次键 | `data-chatgpt-search-unit-key="fallback-turn-N:M:role"` | **低：N 是相对编号，虚拟窗口移动后重编号**——只可用于「当前窗口内排序」，绝不可作为消息身份或跨快照对账键 |
| 计数锚（辅助） | 「复制消息」按钮：每条用户消息恰 1 个 | 中（文案可能本地化变化） |

虚拟列表事实：一次只挂载 ~10-12 个 unit；向上滚动懒加载更早消息。因此**节点计数必须走「UUID 集合锚点差」，不能用 querySelectorAll 数量**——这正是现有 anchor_delta 设计的延续，只是锚点键从指纹升级为 UUID。

## 三、设计修订（platforms.py / probe 变更点，本轮不改码）

### 3.1 选择器候选（sel.messages / sel.user_messages 替换）

```python
"messages": [
  # ① 助手正文（带 UUID，首选）：正文容器自证身份
  "div[data-markdown-text-style=\"assistant-message\"][data-chatgpt-selection-message-id], div[data-markdown-text-style=\"assistant-message\"][data-chatgpt-selection-message-id] *",  # 修正：直接用①
  # ② 角色头（改版后语义继任者）
  "h4[data-conversation-role=\"assistant\"]",
  # ③ 旧版兼容（若官方回滚）
  "[data-message-author-role=\"assistant\"]",
],
"user_messages": [
  "h4[data-conversation-role=\"user\"]",
  "[data-chatgpt-search-unit-key$=\":user\"]",
  "[data-message-author-role=\"user\"]",
],
```

（① 的组合以「正文容器自身或其祖先持 UUID」为准，实现时在 keyOf 里向上找，见 3.2。）

### 3.2 keyOf 升级：UUID 优先

现有 keyOf 探测顺序（data-virtual-list-item-key → data-message-id → hex id → data-index）之前，插入第一优先级：

```js
// 新版 ChatGPT：向上最多 8 层找消息 UUID（search-message-ids 在 turn 容器上）
const mid = n.getAttribute('data-chatgpt-selection-message-id')
         || n.getAttribute('data-chatgpt-search-message-ids');
if (mid) return 'u:' + mid;
```

约束：仍保留 MSGISH_CLS 组合约束思想——`data-chatgpt-search-message-ids` 属性名自带强语义（message-ids），无歧义风险，可直接信任。

### 3.3 计数口径

- `reply_count` = 当前挂载 assistant UUID 集合大小（去重）；`asst_keys` 继续只留尾部
- `msg_count`（用户口径）= user UUID 集合大小；`user_msg_count` 同步改 UUID 口径
- `msg_fps`/`asst_fps` 保留作文本级旁证：**UUID 差 = 「有新消息」，文本指纹重复 = 「疑似重复投递」**（同一文本两条消息 = 两个不同 UUID）——重复发送检测因此有双口径
- 正文读取：`prefer_marker=True`（「ChatGPT 说：」实测仍可用）保持不变；lastText 兜底分支改从「最后一条 assistant 正文容器」取（`[data-markdown-text-style="assistant-message"]` 的最后一个），不再依赖 full 主文本切分

### 3.4 三独立判据 + request_id 关联（对应裁定修复证明清单）

| 判据 | 定义 | 数据源 |
|---|---|---|
| ① 发送成功 | send 点击被接受 **且** 编辑器转空（editor_blank）**且** 本轮 user UUID 出现（新 user turn = 发送的内容落账） | probe：editor_blank + user uuid 集合差 |
| ② 回复出现 | assistant UUID 集合出现**恰好 1 个新增** | asst_keys 集合差（账本存发送前基线） |
| ③ 回复归属本次请求 | 新增 assistant turn 在 DOM 顺序上紧跟本轮新增 user turn（相邻 sibling turn）**且**期间无 regen/重试介入（regen=false 全程） | unit-key 相对序 + regen 探测 |

- **request_id 关联**：BRM 账本在发送前记录基线（本轮 user 预期文本指纹 + 发送时刻 + asst UUID 基线）；发送后新增 user turn 的文本指纹必须与「本次发送内容指纹」一致——这是归属的第一硬判据（回复引用请求内容只作弱旁证，不作判据）。
- **历史误识别 0**：基线集合在发送前取自权威账本（SQLite），不取自「页面内存」；页面重载后基线不丢。
- **超时/歧义 fail-closed**：①或②任一不确定 → 状态记 `pending_manual_check`，**停止自动重试**（与裁定「不得为凑 5 次重复提交」一致）。
- **DOM 改版 fail-closed**：新旧选择器全 0 命中而 doc_len/doc_nodes 增长 → selector_stale（rc=13 现有机制保持）。

### 3.5 已知风险与对策

1. `data-chatgpt-*` 属性是应用内部实现，可能再改名 → 三级候选 + 全灭即 fail-closed，不猜。
2. `fallback-turn` 编号不稳定 → 设计中已禁用其作身份。
3. 协作控制通道竞态（本轮实测：页面持续变异使 cooperate context 秒级失效）→ probe/读取类（get-html、observe）不受影响；输入类动作在页面繁忙期本来就应退避（与 bsk 0.4.1「never replay uncertain action」一致）。
4. 属性名带 `chatgpt` 品牌前缀，若平台推广该属性到其他 OpenAI 产品则命名会变 → keyOf 保留多级探测。

## 四、验收方案（裁定要求 5/5，设计）

- 环境：**专用测试对话**（新开对话，不用业务对话），走 orchestrator 正常路径，**禁逃生门**。
- 每轮断言（全部满足才计 1/1）：
  1. 发送成功：send accepted + editor_blank + 新 user UUID 文本指纹 = 发送内容指纹
  2. 新回复识别：assistant UUID 集合增量 = 1
  3. 关联正确：新增 assistant turn 与本轮 user turn 相邻；asst 新增 UUID ∉ 基线
  4. 重复发送 0：本轮发送仅产生 1 个新 user UUID
  5. 历史误识别 0：全部新增 UUID 均为发送前不存在者
- 5/5 通过后 `verified=True`（notes 记录实测日期与对话 URL）；任一轮不确定 → 停止，报人工。
