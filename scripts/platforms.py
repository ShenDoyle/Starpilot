# -*- coding: utf-8 -*-
"""平台适配层：把「用哪个网页版 AI」的全部差异隔离在这里。

星舵本身与具体平台无关。本模块描述每个平台需要知道的两件事：

  1. 绑定形态 —— 什么样地址算「一个已有的对话」（用来防串台）
  2. DOM 契约 —— 输入框 / 发送按钮 / 停止生成 / 消息节点 / 报错节点的选择器候选

**为什么必须有这层**：不同平台的输入框不是 textarea（ChatGPT 是 ProseMirror 的
div[contenteditable]），发送按钮的 testid 各不相同，「生成中」信号也五花八门。
把这些差异写进主流程，wa.py 就会变成到处 if 的泥球。

**诚实标注**：verified=True 表示该平台的选择器已被实测确认（见 notes）。
verified=False 表示候选选择器来自通用模式推断，**首次使用必须先跑
`wa.py probe --platform <key>`**，用实测结果回填本文件再正式使用。

新增平台流程见 references/web-ai-platforms.md。
"""
from __future__ import annotations

import json
import re


def _css(cands):
    """把候选选择器列表注入 JS：按顺序取第一个命中的。"""
    return json.dumps(cands, ensure_ascii=False)


# ---------------------------------------------------------------- 通用片段

# 在候选里找第一个存在的元素
_JS_PICK = """
  const pick = (cands) => {
    for (const s of cands) {
      try { const e = document.querySelector(s); if (e) return e; } catch (err) {}
    }
    return null;
  };
  const pickAll = (cands) => {
    for (const s of cands) {
      try {
        const list = [...document.querySelectorAll(s)];
        if (list.length) return list;
      } catch (err) {}
    }
    return [];
  };
  // 通用「不可用」判定：原生 disabled / aria-disabled / class 里的 disabled。
  // 有些平台（如 Kimi）的发送按钮是 div，用 .disabled 类表示不可点。
  const isDisabled = (e) => {
    if (!e) return true;
    if (e.disabled) return true;
    if ((e.getAttribute('aria-disabled') || '').toLowerCase() === 'true') return true;
    const cn = (e.className && e.className.toString) ? e.className.toString() : '';
    // 三类命名都要认：`disabled`、`is-disabled`、BEM 变体 `ds-button--disabled`（DeepSeek 实测）
    if (/(^|\\s|--)(disabled|is-disabled)(\\s|$)/.test(cn)) return true;
    return false;
  };
  // 编辑器取值：textarea 用 value，contenteditable 用 innerText。
  // 不区分会踩坑：DeepSeek 的编辑器是 textarea，innerText 恒为空，
  // 「写入成功」会被误判成「一个字没写进去」（2026-10-29 实测）。
  const edText = (e) => {
    if (!e) return '';
    if (e.tagName === 'TEXTAREA' || e.tagName === 'INPUT') return e.value || '';
    return e.innerText || '';
  };
  // 消息指纹：正文去掉 markdown 修饰符后取前 64 字。
  // 用途是**锚点计数**——虚拟列表会反复卸载/重挂节点，但同一条消息渲染多少次
  // 指纹都不变，所以「新增了几个指纹」比「当前渲染出几个节点」稳。
  // 见 docs/orchestrator-contract.md §2.4。
  const fpNorm = (t) => (t || '').replace(/[\\s#*`>\\[\\]\\(\\)~_\\-|]+/g, '');
  const fpOf = (el) => fpNorm((el.innerText || '')).slice(0, 64);
"""

# 通用兜底：靠「主区域文本」判断最后一条回复（不知道消息节点属性时用）
_JS_TAIL_FALLBACK = """
  const mainEl = document.querySelector('main') || document.body;
  const mainText = (mainEl && mainEl.innerText) || '';
  const markers = ['ChatGPT 说：', 'ChatGPT says:', 'Claude', '助手', 'Assistant'];
  let tail = mainText;
  for (const m of markers) {
    const i = mainText.lastIndexOf(m);
    if (i >= 0) { tail = mainText.slice(i + m.length); break; }
  }
"""


# ---------------------------------------------------------------- 平台定义

PLATFORMS = {
    # ---------------------------------------------------------- ChatGPT
    "chatgpt": {
        "label": "ChatGPT",
        "home": "https://chatgpt.com",
        "verified": True,
        "notes": "2026-10-27/28 实测，14 轮循环跑通。选择器精确、信号最完整。",
        "url_pattern": r"/c/[0-9a-fA-F-]{8,}",
        "url_hint": "对话地址形如 https://chatgpt.com/c/<uuid>；出现 / 或 /new 即为新对话，拒绝绑定。",
        "write_method": "exec",
        # ChatGPT 的正文区混着 UI 文案，实测必须「先按标记切、再读节点」，否则 last_len 会带上按钮字
        "prefer_marker": True,
        "markers": ["ChatGPT 说："],
        "tail_junk": ["ChatGPT 可能会出错"],
        "user_msg_regex": "你说：",
        # 已实测的写入路径：只有 exec（execCommand）这一条被允许真实写
        "write_allowed": ["exec"],
        "sel": {
            "editor": ["#prompt-textarea", "main div[contenteditable=\"true\"]",
                       "div[contenteditable=\"true\"]"],
            "send": ["button[data-testid=\"send-button\"]",
                     "main button[aria-label=\"发送\"]",
                     "main button[aria-label=\"Send\"]"],
            "stop": ["button[data-testid=\"stop-button\"]",
                     "main button[aria-label*=\"停止\"]",
                     "main button[aria-label*=\"Stop\"]"],
            "messages": ["[data-message-author-role=\"assistant\"]"],
            "msg_containers": ["[data-message-author-role]"],
            "user_messages": ["[data-message-author-role=\"user\"]"],
            "alert": ["[role=\"alert\"]"],
            "speech": ["[data-testid=\"composer-speech-button\"]",
                       "button[aria-label*=\"语音\"]",
                       "button[aria-label*=\"Voice\"]"],
            "scroll": [".thread-scroll-container"],
        },
    },

    # ---------------------------------------------------------- Claude
    "claude": {
        "label": "Claude",
        "home": "https://claude.ai",
        "verified": False,
        "notes": "候选选择器为通用模式推断，首次使用必须 probe 实测回填。"
                 "推荐配合 Claude Projects 使用（持久知识库是它对抗上下文压缩的核心手段）。",
        "url_pattern": r"/(chat|chats|projects)/[0-9a-fA-F-]{8,}",
        "url_hint": "对话地址形如 https://claude.ai/chats/<uuid>；新对话页必须先手动开好并拿到地址。",
        "write_method": "paste",
        "sel": {
            "editor": ["div[contenteditable=\"true\"]", "textarea",
                       "[contenteditable=\"true\"][role=\"textbox\"]"],
            "send": ["button[aria-label*=\"Send\"]", "button[aria-label*=\"发送\"]",
                     "button[data-testid=\"send-button\"]", "button[type=\"submit\"]"],
            "stop": ["button[aria-label*=\"Stop\"]", "button[aria-label*=\"停止\"]",
                     "button[data-testid=\"stop-button\"]"],
            "messages": ["[data-testid=\"chat-message\"]",
                         "div[data-testid^=\"chat-message\"]"],
            "msg_containers": ["[data-testid^=\"chat-message\"]"],
            "alert": ["[role=\"alert\"]", "[data-testid=\"error\"]"],
            "speech": ["button[aria-label*=\"voice\"]", "button[aria-label*=\"语音\"]"],
            "scroll": ["div[class*=\"overflow-y-auto\"]", "main"],
        },
    },

    # ---------------------------------------------------------- Gemini
    "gemini": {
        "label": "Gemini",
        "home": "https://gemini.google.com",
        # 新对话入口 = /app（无会话 id）。**不是** home：gemini.google.com 首页
        # 在有登录态时会重定向到「最近一次对话」，用它当入口等于没有新开。
        "new_chat_url": "https://gemini.google.com/app",
        "verified": True,
        "notes": "2026-10-29 实测（登录态、写入、发送、等待、读取全链路）。"
                 "**编辑器是 Quill（div.ql-editor）**，这是最大的坑："
                 "`execCommand('insertText')` 会把字塞进 DOM（`innerText` 读到 15 字），"
                 "但 Quill 的内部 model 收不到 —— `ql-blank` 类还在、**发送键根本不渲染**、"
                 "点了也没反应。属于「看起来写进去了、其实没写」的静默失败。"
                 "所以 **写入必须走原生键盘（write_method=native）**，"
                 "判「写进去了没有」只能看 `ql-blank` 与「发送键是否出现」，不能看 innerText。"
                 "**发送键只在有内容时渲染**（button[aria-label=发送]，空态位置放的是麦克风）。"
                 "**助手正文 = model-response**（正文节点 .model-response-text）；"
                 "**用户消息 = user-query**；**一问一答共处一个 div.conversation-container**，"
                 "该容器的 `id` 是 16 位十六进制字符串 —— 这是现成的**轮次身份**（锚点）。"
                 "地址形态 /app/<16 位 hex>；新对话页是 /app（无 id），发第一条后自动跳转。",
        "url_pattern": r"/app/[0-9a-fA-F]{8,}",
        "url_hint": "对话地址形如 https://gemini.google.com/app/<16 位 hex>；"
                    "新对话页是 https://gemini.google.com/app（没有 id，不能绑），"
                    "发第一条消息后平台会跳到真实会话地址。",
        "write_method": "native",
        # 已实测的写入路径：只有 native（真实键盘输入）这一条被允许真实写。
        # 实测来源 2026-10-29：exec 写入后 ql-blank 仍在、发送键不出现。
        "write_allowed": ["native"],
        # 「编辑器为空」的平台判定类：有它就是空，innerText 里有字也不算（Quill 的 model 没收到）
        "blank_class": "ql-blank",
        # 轮次容器：一问一答共处 `div.conversation-container`，其 id 就是轮次身份。
        # 放在顶层而不是 sel 里：sel 里全是「候选选择器列表」，这是**平台事实**（单个）。
        "anchor_box": "div.conversation-container",
        # 发送键是**条件渲染**的：没内容时页面上根本没有这个按钮。
        # 所以「写完却没出现发送键」= 写入没被编辑器接受，必须当失败，不许点空气。
        "send_requires_text": True,
        "sel": {
            "editor": ["rich-textarea div.ql-editor", "div.ql-editor",
                       "rich-textarea div[contenteditable=\"true\"]",
                       "div[contenteditable=\"true\"]"],
            "send": ["button[aria-label=\"发送\"]", "button[aria-label=\"Send\"]",
                     "button.send-button"],
            "stop": ["button[aria-label*=\"停止\"]", "button[aria-label*=\"Stop\"]"],
            # messages = 助手回复条目（js_read_last 取最后一条 = 末条回复）
            "messages": ["model-response"],
            # msg_containers = 轮次容器（一问一答算一轮）：发送后平台**先建容器、后填回复**，
            # 所以 msg_count 会比 reply_count 早一步 +1 —— 这个先后差正好用来判断「发出去了」。
            "msg_containers": ["div.conversation-container"],
            # user_messages = 用户消息条目（DOM 口径，区分气泡拆分与真重发）
            "user_messages": ["user-query"],
            "alert": ["[role=\"alert\"]", ".error-message"],
            "speech": ["button[aria-label*=\"语音\"]", "button[aria-label*=\"voice\"]"],
            "scroll": ["infinite-scroller.chat-history",
                       "div.chat-history-scroll-container", "main"],
        },
    },

    # ---------------------------------------------------------- DeepSeek
    "deepseek": {
        "label": "DeepSeek",
        "home": "https://chat.deepseek.com",
        "verified": True,
        "notes": "2026-10-29 实测（登录态、发送、等待、读取全链路跑通）。"
                 "**编辑器 = textarea**（placeholder「给 DeepSeek 发送消息」，无 id；"
                 "不是 contenteditable，所以 Range 圈内容会抛错、innerText 恒为空 —— "
                 "必须用 el.select() + insertText，长度读 el.value）；"
                 "**发送按钮 = div.ds-button--primary.ds-button--circle**（不是 button），"
                 "空输入时带 `ds-button--disabled` 类（BEM 变体，旧 isDisabled 正则认不出来）；"
                 "**助手正文 = div.ds-assistant-message-main-content**（不含「已思考」折叠区）；"
                 "**全部消息 = div.ds-message**；**用户消息 = div.ds-message:not(:has(.ds-assistant-message-main-content))**；"
                 "**滚动容器 = div.ds-virtual-list（虚拟列表）**：只渲染视口附近的消息，"
                 "所以计数口径是「当前渲染出的气泡数」，必须先滚到底再计数；"
                 "地址形态 /a/chat/s/<uuid>。",
        "url_pattern": r"/(a/chat/s|chat)/[0-9a-fA-F-]{8,}",
        "url_hint": "对话地址形如 https://chat.deepseek.com/a/chat/s/<uuid>；需确认可刷新复现。",
        "write_method": "exec",
        "write_allowed": ["exec"],
        "sel": {
            "editor": ["textarea[placeholder*=\"DeepSeek\"]", "textarea", "div[contenteditable=\"true\"]"],
            "send": ["div[class*=\"ds-button--primary\"][class*=\"circle\"]",
                     "div[class*=\"ds-button--primary\"]"],
            "stop": ["div[class*=\"ds-button--primary\"][class*=\"circle\"][class*=\"stop\"]",
                     "button[aria-label*=\"停止\"]", "button[aria-label*=\"Stop\"]"],
            "messages": ["div.ds-assistant-message-main-content"],
            "msg_containers": ["div.ds-message"],
            "user_messages": ["div.ds-message:not(:has(.ds-assistant-message-main-content))"],
            "alert": ["[role=\"alert\"]"],
            "speech": [],
            "scroll": ["div.ds-virtual-list", "div[class*=\"ds-scroll-area\"]", "main"],
        },
    },

    # ---------------------------------------------------------- Kimi
    "kimi": {
        "label": "Kimi",
        "home": "https://www.kimi.com",
        "new_chat_url": "https://www.kimi.com/",
        "verified": True,
        "notes": "2026-10-29 实测（首轮新建对话打通）。"
                 "编辑器 = div.chat-input-editor（Lexical：span[data-lexical-text]）；"
                 "发送按钮 = div.send-button-container（**不是 button**，空输入时带 .disabled 类）；"
                 "**消息条目 = div.chat-content-item-user / div.chat-content-item-assistant**"
                 "（等价的 segment 变体：div.segment.segment-user / div.segment.segment-assistant）；"
                 "**滚动容器 = div.chat-detail-main**（注意：div.message-list-container 存在但恒为空，"
                 "拿它当消息区会数出 1 条假消息）；"
                 "输入区整体 = div.chat-editor-wrap / div.chat-box / div.chat-editor-content。"
                 "**写入方式 = exec（execCommand insertText），不是 paste**："
                 "实测 paste 事件是追加不是替换，且 execCommand('delete') 被受控编辑器忽略；"
                 "只有「Range 圈定编辑器内容 + insertText」才能精确替换（重写不会产生重复段）。"
                 "已知平台侧限制：免费额度繁忙时发送会弹 modal 提示"
                 "「和Kimi聊天的人太多了，订阅会员可进入优先队列」，"
                 "弹窗 data-testid=confirm-dialog-cancel 是「我知道了」、confirm-dialog-confirm 是「去升级」。",
        "url_pattern": r"/(chat|share)/[0-9a-fA-F-]{8,}",
        "url_hint": "对话地址形如 https://www.kimi.com/chat/<id>；"
                    "新对话入口是首页 https://www.kimi.com/（发第一条消息后自动跳到 /chat/<id>），"
                    "用 `bind --new` 显式新建，不要在循环中途「新开」。",
        "write_method": "exec",
        # 已实测的写入路径：只有 exec（Range 圈定 + execCommand insertText）这一条被允许真实写。
        # 实测来源：2026-10-29；paste 是追加、execCommand('delete') 被受控编辑器忽略。
        "write_allowed": ["exec"],
        "sel": {
            "editor": ["div.chat-input-editor",
                       "div.chat-input-editor-container div[contenteditable=\"true\"]",
                       "div[contenteditable=\"true\"]"],
            # 优先选「非 disabled」变体；退化候选让 js_click_send 自己做 disabled 类检查
            "send": ["div.send-button-container:not(.disabled)",
                     "div.send-button-container",
                     "svg.send-icon"],
            "stop": ["div.send-button-container.stop",
                     "button[aria-label*=\"停止\"]", "button[aria-label*=\"Stop\"]"],
            # messages = 助手消息条目（js_read_last 取最后一条 = 末条回复）
            "messages": ["div.chat-content-item-assistant",
                         "div.segment.segment-assistant"],
            # msg_containers = 全部消息条目（用户+助手），用于消息计数
            "msg_containers": ["div.chat-content-item",
                               "div.segment.segment-user, div.segment.segment-assistant"],
            # user_messages = 用户消息条目（DOM 口径）：
            # 用来区分「一条回复被拆成 N 个气泡」与「真的重发了 N 条」
            "user_messages": ["div.chat-content-item-user",
                              "div.segment.segment-user"],
            "alert": ["[role=\"alert\"]"],
            "speech": ["button[aria-label*=\"语音\"]"],
            "scroll": ["div.chat-detail-main",
                       "div.chat-detail-content", "main"],
        },
    },

    # ---------------------------------------------------------- 豆包
    "doubao": {
        "label": "豆包",
        "home": "https://www.doubao.com",
        # 首页 /chat/ 就是新对话入口（无 id），发第一条后自动跳 /chat/<数字id>
        "new_chat_url": "https://www.doubao.com/chat/",
        "verified": True,
        "notes": "2026-10-29 实测（登录态、写入、发送、等待、读取全链路）。"
                 "**编辑器 = Tiptap ProseMirror（div.tiptap.ProseMirror）**："
                 "execCommand('insertText') **有效**——与 Gemini 的 Quill 不同，字能进 model"
                 "（实测 execOk=true 且 len 与文字一致）。"
                 "**但 bsk fill（键盘模拟）对豆包编辑器无效**：报「the page moved focus away"
                 " from the fill target」，先 click 聚焦再 fill 也一样——所以写入必须走 exec。"
                 "**发送不能点发送按钮**：button#flow-end-msg-send 存在但 bsk click 无反应，"
                 "正确路径 = exec 写入后**保持编辑器焦点** → press Enter；"
                 "点击按钮会把焦点从编辑器移走，之后 Enter 也发不出（实测两次）。"
                 "发送键是**条件渲染**：空输入时 #flow-end-msg-send 根本不存在。"
                 "消息节点是全平台最规范的 data-testid 体系：用户=send_message、"
                 "助手=receive_message、正文=message_text_content、总消息=union_message、"
                 "列表=message-list；**完成标志 = message_action_regenerate 出现**。"
                 "**陷阱：探针文字会出现在侧栏会话标题**（conversation-item /"
                 " editable_conversation_name）——找消息节点必须限定在"
                 " [data-testid=message-list] 内，否则数到侧栏。"
                 "union_message 无每条唯一 id（data-target-id 是固定值），锚点走正文指纹。"
                 "未发现空态类（Tiptap 的 DOM 与 model 一致，innerText 可信）。"
                 "地址：/chat/<数字id>；新对话页 /chat/ 无 id。",
        "url_pattern": r"/chat/\d{8,}",
        "url_hint": "对话地址形如 https://www.doubao.com/chat/<数字id>；"
                    "新对话入口是 https://www.doubao.com/chat/（无 id，不能绑），"
                    "发第一条消息后平台自动跳到真实会话地址。",
        "write_method": "exec",
        # 已实测的写入路径：exec（execCommand insertText）。
        # 实测来源 2026-10-29：bsk fill 对豆包编辑器必然失败（焦点被移走），不要试第二条。
        "write_allowed": ["exec"],
        # 发送方式：click 点发送键（全平台默认）；enter = 编辑器持焦点按 Enter。
        # 豆包实测：发送按钮 bsk click 无反应，只有「exec 写入 + 焦点不丢 + Enter」发得出去。
        "send_method": "enter",
        # 豆包的「重新生成」是纯 icon 按钮（无 aria-label/文本）：通用探测必失效，必须显式声明。
        # 实测：生成中 regen 恒 0，回复完成后才渲染——这是豆包唯一可靠的「本轮已结束」信号。
        "regen_selector": "[data-testid=\"message_action_regenerate\"]",
        # 发送键条件渲染：空输入时 #flow-end-msg-send 不在页面上。
        "send_requires_text": True,
        "sel": {
            "editor": ["div.tiptap.ProseMirror", "div[contenteditable=\"true\"]"],
            # 条件渲染按钮；实际发送走「编辑器焦点 + Enter」，这是点击无效时的后备候选
            "send": ["#flow-end-msg-send", "div.send-btn-wrapper"],
            "stop": ["[data-testid=\"message_action_stop\"]",
                     "button[aria-label*=\"停止\"]", "button[aria-label*=\"Stop\"]"],
            # messages = 助手回复条目（js_read_last 取最后一条 = 末条回复）
            "messages": ["[data-testid=\"receive_message\"]"],
            # msg_containers = 全部消息条目（用户+助手），用于消息计数
            "msg_containers": ["[data-testid=\"union_message\"]"],
            # user_messages = 用户消息条目（DOM 口径，区分气泡拆分与真重发）
            "user_messages": ["[data-testid=\"send_message\"]"],
            "alert": ["[role=\"alert\"]"],
            "speech": ["button[aria-label*=\"语音\"]"],
            "scroll": ["[data-testid=\"message-list\"]", "main"],
            # 读末条回复时剔除的噪声子树：豆包把「推荐提问」渲染在回复节点内部
            "read_ignore": ["[data-testid=\"suggest_message_list\"]"],
        },
    },
}


DEFAULT_PLATFORM = "chatgpt"

# 各平台缺省值：没显式配置的就用这套
_DEFAULTS = {
    "prefer_marker": False,       # 优先按标记切正文；否则优先读 assistant DOM 节点
    "markers": ["助手", "Assistant"],
    "tail_junk": [],
    "user_msg_regex": "",         # 正则源串，空 = 不按用户消息计数
    "write_method": "paste",
    "send_method": "click",       # 发送方式：click=点发送键；enter=编辑器持焦点按 Enter
    "new_chat_url": "",           # 「新建对话」入口地址；空 = 用 home
    "regen_selector": "",         # 「重新生成」节点的平台声明（纯 icon 按钮没有 aria-label 时必配）
    # 允许的**真实写**路径白名单。空 = 该平台禁止真实写（未实测）。
    # 写闸门（write_gate.py）只认这张表；不在表里的写入方法一律 WRITE_BLOCKED_WRITE_METHOD。
    "write_allowed": [],
}


def get(key=None):
    """取平台配置（已填好缺省值）。key 为 None/空 时返回默认平台。"""
    key = (key or DEFAULT_PLATFORM).strip().lower()
    if key not in PLATFORMS:
        raise SystemExit("未知平台：%s\n可选：%s" % (key, ", ".join(sorted(PLATFORMS))))
    p = dict(PLATFORMS[key])
    for k, v in _DEFAULTS.items():
        p.setdefault(k, v)
    if not p.get("new_chat_url"):
        p["new_chat_url"] = p.get("home") or ""
    p["key"] = key
    return p


def label(key=None):
    return get(key)["label"]


# ---------------------------------------------- 探测归一口径（纯函数，离线可测）

def normalize_probe(p, raw):
    """把 JS 报的**原始事实**归一成**判定口径**。

    分工：JS 只管「页面上有什么」，本函数管「这算什么意思」。
    分成两层不是为了好看，是为了让**判定口径能离线单测**——
    「空态类优先于文本长度」这种规则如果埋在 JS 里，就只能连上浏览器才验得到，
    而它恰恰是最容易悄悄退化的那种规则。

    规则：
      · `editor_blank` 为真 → `editor_len` 归 0。Gemini 实测：exec 写入后 innerText
        有字但 Quill model 是空的（`ql-blank` 还在），按字数判会误判成功。
      · `editor_len` 一律收敛成 int，异常值落 -1（探测失败），不落 0（那是「确实空」）。
    """
    d = dict(raw or {})
    blank = bool(d.get("editor_blank"))
    d["editor_blank"] = blank
    try:
        n = int(d.get("editor_len"))
    except (TypeError, ValueError):
        n = -1
    if blank:
        n = 0
    d["editor_len"] = n
    # 条件渲染发送键的平台：按钮不在就是没内容，`can_send` 不许因为「没有按钮可禁用」
    # 而被算成 True（没有按钮时 isDisabled(null) 会返回 true，兜住了，这里只是显式化）。
    if p.get("send_requires_text") and not d.get("can_send"):
        d["can_send"] = False
    return d


# 写入接受判定的错误码
FILL_OK = "ok"
FILL_BLANK = "fill_blank"          # 编辑器报「空」：字根本没进 model
FILL_SHORT = "fill_short"          # 字数明显不足：被截断（token 上限 / 输入法吃字）
FILL_NO_SEND = "fill_no_send"      # 有条件渲染的发送键没出现：编辑器没接受这份内容


def fill_check(p, text, cur):
    """写入后的**接受判定**：编辑器到底收下了没有。

    返回 `{ok, code, detail, facts}`。

    三条见证，缺一不认：
      1. 字数够（≥ 95%）——必要条件，不是充分条件；
      2. 空态类已消失——Quill 这类受控编辑器「DOM 有字、model 是空」时只在这一条上露馅；
      3. 条件渲染的发送键已出现——「写完了但发不出去」和「没写进去」是两回事，
         必须在点发送**之前**分清，否则会点空气并白耗一轮预算。

    第 2、3 条只在平台声明了 `blank_class` / `send_requires_text` 时才强制：
    没有这两条事实的平台（ChatGPT/Kimi/DeepSeek）不该被凭空加限制。
    """
    want = len(text or "")
    got = int((cur or {}).get("editor_len", -1) or 0)
    facts = {
        "want": want, "got": got,
        "editor_blank": bool((cur or {}).get("editor_blank")),
        "can_send": bool((cur or {}).get("can_send")),
        "blank_class": p.get("blank_class") or "",
        "send_requires_text": bool(p.get("send_requires_text")),
    }
    if facts["editor_blank"]:
        return {"ok": False, "code": FILL_BLANK, "facts": facts,
                "detail": "编辑器报空态（%s）：字进了 DOM 但没进编辑器 model"
                          % (facts["blank_class"] or "empty-class")}
    if got < want * 0.95:
        return {"ok": False, "code": FILL_SHORT, "facts": facts,
                "detail": "期望约 %d 字，实际 %d 字" % (want, got)}
    if facts["send_requires_text"] and not facts["can_send"]:
        return {"ok": False, "code": FILL_NO_SEND, "facts": facts,
                "detail": "内容已在编辑器里（%d 字）但发送键没出现：平台没接受这份输入" % got}
    return {"ok": True, "code": FILL_OK, "facts": facts, "detail": ""}


# ------------------------------------------------ 绑定口径（纯函数，离线可测）

# 绑定判定结果码。**既有对话不许默认接管**：这是 2026-10-29 实测事故的直接产物 ——
# 当时 `bind <url>` 拿的是浏览器里「用户正在聊的那条 Gemini 对话」（msg_count=10），
# 程序只在 msg_count==0 时打印一行警告，非空直接放行，于是整个项目挂到了别人的对话上：
# 上下文是旧的、发送会污染用户既有对话、后续锚点基线也全是别人的。
BIND_CODE = {
    "new_chat": "新对话已就绪（0 条消息）",
    "resume": "同一对话续期",
    "empty_conv": "空对话（有会话地址但没有消息）",
    "adopted": "显式接管已有对话",
    "bad_url": "地址不像一个已存在的对话",
    "new_chat_redirected": "新对话入口落回了已有对话",
    "new_chat_not_empty": "新对话页上却有历史消息",
    "conv_changed": "换了另一个对话",
    "foreign_conv": "首次绑定到非空对话、且未显式接管",
}


def conv_id_of(p, url):
    """从地址里抽「这是哪一个对话」的标识；空串 = 不是一个已存在的对话。

    口径来自平台的 `url_pattern`（唯一权威），本函数不自己猜。
    """
    pat = (p or {}).get("url_pattern") or ""
    if not pat:
        return ""
    try:
        m = re.search(pat, url or "")
    except re.error:
        return ""
    return m.group(0) if m else ""


def bind_verdict(p, url, probe, st, *, new_chat=False, reuse_existing=False):
    """绑定前/后的归属判定。返回 `{ok, code, conv_id, msg_count, reason, hint, warn}`。

    设计原则（与 lessons L-KIMI-DELTA-METRIC-SCOPE 同源）：
    **JS 只报事实（消息数、当前地址），是否允许绑定由这里一处定口径。**

    规则优先级：
      1. `--new` ⇒ 必须落在「无会话 id + 0 条消息」的页面上，否则说明入口被重定向；
      2. 无 conv_id ⇒ 地址不是对话（首页 / 新对话页），拒绑；
      3. conv_id 与 state 记录一致 ⇒ 续期（硬规则 1：同一对话），放行；
      4. conv_id 与 state 不一致、或 state 没有记录：
         - 空对话 ⇒ 放行但给警告（可能是新建后还没发）；
         - 非空对话 ⇒ **默认拒绝**，必须 `--reuse-existing` 显式承认才接管。
    """
    probe = probe or {}
    st = st or {}
    real_url = probe.get("url") or url or ""
    conv = conv_id_of(p, real_url)
    msgs = int(probe.get("msg_count") or 0)
    res = {"ok": False, "code": "", "conv_id": conv, "msg_count": msgs,
           "reason": "", "hint": "", "warn": ""}

    def done(ok, code, reason="", hint="", warn=""):
        res.update({"ok": ok, "code": code, "reason": reason, "hint": hint, "warn": warn})
        return res

    if new_chat:
        entry = p.get("new_chat_url") or p.get("home") or ""
        if conv:
            return done(False, "new_chat_redirected",
                        "新对话入口 %s 把页面带到了已有对话 %s" % (entry, conv),
                        "这个入口不是真的新对话：改用手动点「新对话」拿到 /app 后再 bind，"
                        "或先确认该平台的 new_chat_url 是否写对了。")
        if msgs > 0:
            return done(False, "new_chat_not_empty",
                        "新对话页上读到 %d 条消息" % msgs,
                        "页面可能没清干净（残留渲染 / 读到了侧栏历史）。等页面稳定后重试。")
        return done(True, "new_chat")

    if not conv:
        return done(False, "bad_url",
                    "地址不像一个已存在的对话：%s" % real_url,
                    p.get("url_hint") or "先打开目标对话，复制地址再绑。")

    recorded = (st.get("conv_id") or "").strip()
    origin = (st.get("conv_origin") or "").strip()
    if recorded and recorded == conv:
        if origin in ("new_chat", "empty", "adopted"):
            return done(True, "resume")
        # 有 conv_id 但说不出它是怎么进来的（门禁上线前的 state / 他人写入）：
        # 身份存疑，回落到「首次绑定」的规则审一遍，不许靠 url 相等自证清白。
        if msgs == 0:
            return done(True, "empty_conv", "",
                        warn="对话里还没有消息（已等读数稳定）：确认这就是你要用的那条；"
                             "要项目干净的上下文，用 --new 从平台新对话入口起")
        if reuse_existing:
            return done(True, "adopted", "",
                        warn="接管了一条已有 %d 条消息的对话：历史上下文属于别人/别的事，"
                             "锚点基线从此处起算" % msgs)
        return done(False, "foreign_conv",
                    "这是一条**已有对话**（%d 条消息），且 state 里没有它的来源记录" % msgs,
                    "要项目独立就 `bind --new`（从平台新对话入口起）；\n"
                    "确实要用这条已有对话，加 --reuse-existing 显式承认。")

    if recorded and recorded != conv:
        if reuse_existing:
            return done(True, "adopted", "",
                        warn="已从对话 %s 切到 %s：上下文不连续，硬规则 1 已被显式豁免"
                             % (recorded, conv))
        return done(False, "conv_changed",
                    "state 里记的是 %s，当前页面是 %s —— 这不是同一个对话" % (recorded, conv),
                    "硬规则 1：同一对话。回绑原对话；确实要换，加 --reuse-existing 并接受上下文断裂。")

    if msgs == 0:
        return done(True, "empty_conv", "",
                    warn="对话里还没有消息（已等读数稳定）：确认这就是你要用的那条；"
                         "要项目干净的上下文，用 --new 从平台新对话入口起")

    if reuse_existing:
        return done(True, "adopted", "",
                    warn="接管了一条已有 %d 条消息的对话：历史上下文属于别人/别的事，"
                         "锚点基线从此处起算" % msgs)

    return done(False, "foreign_conv",
                "这是一条**已有对话**（%d 条消息），不是本项目新建的对话" % msgs,
                "要项目独立就 `bind --new`（从平台新对话入口起）；\n"
                "确实要用这条已有对话，加 --reuse-existing 显式承认。")


def list_platforms():
    return [
        {
            "key": k,
            "label": v["label"],
            "home": v["home"],
            "verified": v["verified"],
            "notes": v["notes"],
        }
        for k, v in PLATFORMS.items()
    ]


# ---------------------------------------------------------------- JS 生成


def js_probe(p):
    """综合探测：一个片段拿全部状态。

    保留 ChatGPT 实测时的三个关键语义，其他平台按通用模式走：
      · msg_count  —— **平台相关**：有 user_msg_regex 时是用户消息数，否则是消息总数
        （Kimi 是总数：我这条提问 + 平台那条回复 = +2）
      · reply_count —— **平台无关**：助手回复条数（asst.length）。
        「一条提问对一条回复」的正确口径只有这一个；写闸门的 reply_count_delta
        一律用它，不用 msg_count（上一轮 delta=2 就是口径混用导致的）。
      · last_text  —— prefer_marker 时先按标记切正文，否则先读 assistant DOM 节点
      · regen      —— 「重新生成」按钮出现 = 本轮生成已结束（硬信号）
    """
    s = p["sel"]
    js = """
(() => {
  %s
  const sc = pick(%s);
  if (sc) { sc.scrollTop = sc.scrollHeight; }
  const mainEl = document.querySelector('main') || document.body;
  const full = sc ? (sc.innerText || '') : (mainEl.innerText || '');

  const editor = pick(%s);
  const stop   = pick(%s);
  const send   = pick(%s);
  const speech = pick(%s);
  const asst   = pickAll(%s);
  const all    = pickAll(%s);
  const users  = pickAll(%s);
  const errs   = pickAll(%s).map(e => (e.innerText || '').trim()).filter(Boolean).join(' | ');

  // ---- 空态判定：平台的「空」类比 innerText 可信 ----
  // Gemini 实测：execCommand('insertText') 能把字塞进 DOM（innerText 有 15 字），
  // 但 Quill 的 model 没收到 —— `ql-blank` 类还在，发送键不渲染，点发送毫无反应。
  // 所以「编辑器空不空」必须听平台的空态类，不能听文本长度；否则会把
  // 「一个字没真正写进去」判成「写入成功」，然后对空气发一轮。
  const EMPTY_CLS = ['ql-blank', 'is-empty', 'is-blank'];
  const hasEmptyCls = (el) => {
    if (!el) return false;
    const cn = (el.className && el.className.toString) ? el.className.toString() : '';
    if (!cn) return false;
    const parts = cn.split(/\\s+/);
    for (const c of EMPTY_CLS) { if (parts.indexOf(c) >= 0) return true; }
    return false;
  };
  const edBlank = hasEmptyCls(editor);

  // ---- 末条回复正文 ----
  let lastText = '';
  const useMarker = %s || asst.length === 0;
  if (useMarker) {
    const markers = %s;
    for (const m of markers) {
      const i = full.lastIndexOf(m);
      if (i >= 0) { lastText = full.slice(i + m.length); break; }
    }
    if (lastText) {
      for (const j of %s) {
        const k = lastText.indexOf(j);
        if (k >= 0) { lastText = lastText.slice(0, k); }
      }
      lastText = lastText.trim();
    }
  }
  if (!lastText && asst.length) {
    lastText = (asst[asst.length - 1].innerText || '').trim();
  }
  if (!lastText && !useMarker) {
    lastText = full.trim();
  }

  // ---- 消息计数 ----
  const ure = %s;
  let userCount = 0;
  if (ure) {
    try { userCount = (full.match(ure) || []).length; } catch (e) { userCount = 0; }
  }
  const total = all.length || asst.length;
  const msgCount = userCount || total;
  // 助手回复条数（平台无关口径）：一条提问对一条回复 ⇒ 这个数只涨 1
  const replyCount = asst.length;
  // 用户消息条数（DOM 口径）：用于区分「一条回复被拆成 N 个气泡」与「真的重发了 N 条」
  const userMsgCount = users.length;

  // 弹窗 / 遮罩：有它在，真实点击会被挡死（实测 Kimi 限流提示）。
  // 不报出来 = 「点击没反应」会被误判成选择器坏了。
  const modal = document.querySelector('div.modal-mask, [role="dialog"], [aria-modal="true"]');
  const modalText = modal ? (modal.innerText || '').trim().slice(0, 120) : '';

  // ---- 锚点指纹（计数用，见 §2.4）----
  const asstFps = asst.map(fpOf).filter(Boolean);
  const userFps = users.map(fpOf).filter(Boolean);
  const allFps = all.map(fpOf).filter(Boolean);

  // ---- 平台自带的稳定标识（比正文指纹更硬）----
  // DeepSeek 的虚拟列表在每条消息外层挂了 data-virtual-list-item-key（如 -2/2/-4/4），
  // 这是**消息身份**而不是渲染产物：正文被折叠、markdown 重排、两条回复正文完全相同，
  // 都不会让它变。有它就优先用它，没有才退回正文指纹。
  // 第三级是**祖先的十六进制 id**：Gemini 把一问一答包在 div.conversation-container 里，
  // 那个 div 的 id 是 16 位十六进制串（如 131cec389a0800e1），重载/重排都不变。
  // 只认「纯十六进制且 ≥8 位」：正常 id 是 chat-history / app-root 这类词，
  // uuid 带横线也不匹配，所以这一条不会把框架容器误当消息身份。
  // 平台显式声明的「轮次容器」（Gemini: div.conversation-container）：
  // 有它就只认它的 id —— 「哪个容器算一条消息」是平台事实，不该靠猜。
  const ANCHOR_BOX = __ANCHOR_BOX__;
  const boxOf = (el) => {
    if (!ANCHOR_BOX) { return null; }
    try { return el.closest(ANCHOR_BOX); } catch (e) { return null; }
  };
  const HEX_ID = /^[0-9a-f]{8,}$/i;
  // 兜底规则要**组合约束**（Gemini 第 3 条裁定：只凭十六进制太宽松）：
  // 纯十六进制（≥8 位，uuid 带横线因此不匹配）**且** 这个祖先本身的类名像消息容器。
  // 单看十六进制会命中路由哈希、组件 uid、layout 容器的随机 id。
  const MSGISH_CLS = /(message|conversation|turn|chat|response)/i;
  const keyOf = el => {
    const box = boxOf(el);
    if (box) {
      const bid = box.getAttribute('id');
      if (bid) { return 't:' + bid; }
    }
    let n = el;
    for (let d = 0; d < 4 && n; d++, n = n.parentElement) {
      if (!n.getAttribute) { continue; }
      const k = n.getAttribute('data-virtual-list-item-key');
      if (k !== null && k !== '') { return 'k:' + k; }
      const mid = n.getAttribute('data-message-id') || n.getAttribute('data-id');
      if (mid) { return 'm:' + mid; }
      const hex = n.getAttribute('id');
      const ncls = (n.className && n.className.toString) ? n.className.toString() : '';
      if (hex && HEX_ID.test(hex) && MSGISH_CLS.test(ncls)) { return 't:' + hex; }
      const idx = n.getAttribute('data-index');
      if (idx !== null && idx !== '') { return 'i:' + idx; }
    }
    return '';
  };
  const asstKeys = asst.map(keyOf).filter(Boolean);
  const allKeys = all.map(keyOf).filter(Boolean);

  return JSON.stringify({
    url: location.href,
    generating: !!stop,
    regen: (() => {
      // 平台显式声明的「重新生成」节点优先（豆包是纯 icon 按钮，无 aria-label，文本探测必失效）
      const RS = __REGEN_SEL__;
      if (RS) { try { if (document.querySelector(RS)) { return true; } } catch (e) {} }
      return [...document.querySelectorAll('main button, button')]
        .some(b => /重新生成|Regenerate/i.test((b.getAttribute('aria-label') || '')));
    })(),
    can_send: !!send && !isDisabled(send),
    speech: !!speech,
    editor: !!editor,
    editor_kind: editor ? editor.tagName : '',
    // 空态类优先：平台的空态类是「model 里有没有内容」的事实来源，
    // innerText 只是「DOM 里有什么」——受控编辑器（Quill）两者会不一致。
    editor_blank: edBlank,
    editor_len: editor ? (edBlank ? 0 : edText(editor).trim().length) : -1,
    msg_count: msgCount,
    reply_count: replyCount,
    user_msg_count: userMsgCount,
    // 锚点：只保留尾部（虚拟列表只有尾部稳定可见），delta 由集合差推导
    asst_fps: asstFps.slice(-12),
    user_fps: userFps.slice(-12),
    msg_fps: allFps.slice(-20),
    // 平台稳定标识（有则优先，见 anchor_delta）
    asst_keys: asstKeys.slice(-12),
    msg_keys: allKeys.slice(-24),
    last_asst_fp: asstFps.length ? asstFps[asstFps.length - 1] : '',
    last_user_fp: userFps.length ? userFps[userFps.length - 1] : '',
    last_role: lastText ? 'assistant' : null,
    last_len: lastText.length,
    last_tail: lastText.slice(-300),
    modal: !!modal,
    modal_text: modalText,
    error: errs
  });
})()
""" % (
        _JS_PICK,
        _css(s["scroll"]),
        _css(s["editor"]), _css(s["stop"]), _css(s["send"]), _css(s["speech"]),
        _css(s["messages"]), _css(s["msg_containers"]),
        _css(s.get("user_messages") or []), _css(s["alert"]),
        "true" if p.get("prefer_marker") else "false",
        json.dumps(p.get("markers") or [], ensure_ascii=False),
        json.dumps(p.get("tail_junk") or [], ensure_ascii=False),
        json.dumps(p.get("user_msg_regex") or "", ensure_ascii=False),
    )
    # 轮次容器选择器（可空）：只在平台显式声明时才按容器 id 取锚点，
    # 避免「看见长得像 id 的属性就当消息身份」。
    return (js.replace("__ANCHOR_BOX__", json.dumps(p.get("anchor_box") or ""))
              .replace("__REGEN_SEL__", json.dumps(p.get("regen_selector") or "")))


def js_fill(p, text):
    """写入文本（不发送）。"""
    s = p["sel"]
    payload = json.dumps(text, ensure_ascii=False)
    if p.get("write_method") == "paste":
        # 剪贴板事件方式：对 ProseMirror / 富文本编辑器更可靠
        body = """
  const el = pick(%s);
  if (!el) return JSON.stringify({ ok: false, why: 'no-editor' });
  el.focus();
  try {
    const sel = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(el);
    sel.removeAllRanges();
    sel.addRange(range);
    document.execCommand('delete');
  } catch (e) {}
  try {
    const dt = new DataTransfer();
    dt.setData('text/plain', %s);
    const ev = new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true });
    el.dispatchEvent(ev);
  } catch (e) {
    document.execCommand('insertText', false, %s);
  }
  return JSON.stringify({ ok: true, len: (el.innerText || '').trim().length });
""" % (_css(s["editor"]), payload, payload)
    else:
        # exec 分支：精确圈住编辑器内容 → insertText 覆盖选区。
        # 注意：不要依赖 execCommand('delete')——受控编辑器（Kimi 实测）会忽略它，
        # 靠 insertText 直接覆盖选区才可靠。也不要用 paste 事件：Kimi 实测是**追加**。
        # 也不用 execCommand('selectAll')：编辑器为空时它可能选到整页，insertText 会打错地方。
        #
        # textarea 分支（DeepSeek 实测）：textarea 没有子节点，Range 圈内容会抛错，
        # 必须走 el.select() 再 insertText；否则会落到 selectAll 把整页选中。
        body = """
  const el = pick(%s);
  if (!el) return JSON.stringify({ ok: false, why: 'no-editor' });
  el.focus();
  const isTA = (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT');
  let inRange = false;
  if (isTA) {
    try { el.select(); inRange = true; } catch (e) {}
  } else {
    try {
      const sel = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(el);
      sel.removeAllRanges();
      sel.addRange(range);
      const node = sel.rangeCount ? sel.getRangeAt(0).commonAncestorContainer : null;
      inRange = !!node && el.contains(node);
    } catch (e) {}
  }
  if (!inRange && !isTA) {
    try { document.execCommand('selectAll', false, null); } catch (e) {}
  }
  try {
    document.execCommand('insertText', false, %s);
  } catch (e) {
    try { document.execCommand('insertText', false, %s); } catch (e2) {}
  }
  // textarea 兜底：execCommand 被忽略时用原生 setter + input 事件（React 受控组件认这条）
  if (isTA && (el.value || '').trim().length === 0) {
    try {
      const desc = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value');
      desc.set.call(el, %s);
      el.dispatchEvent(new Event('input', { bubbles: true }));
    } catch (e) {}
  }
  const t = (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') ? (el.value || '') : (el.innerText || '');
  return JSON.stringify({ ok: true, len: t.trim().length, in_range: inRange, textarea: isTA,
                          head: t.slice(0, 40), tail: t.slice(-40) });
""" % (_css(s["editor"]), payload, payload, payload)
        return "(() => {\n%s\n%s\n})()" % (_JS_PICK, body)



def js_focus_editor(p):
    """把焦点交回编辑器（enter 发送路径的前置动作）。

    为什么需要：豆包实测，点击发送按钮会把焦点从编辑器移走，
    之后 Enter 就不再触发发送。enter 路径必须保证按下 Enter 那一刻
    activeElement 是编辑器。
    """
    s = p["sel"]
    return """
(() => {
  %s
  const ed = pick(%s);
  if (!ed) { return JSON.stringify({ ok: false, why: 'no-editor' }); }
  ed.focus();
  return JSON.stringify({ ok: document.activeElement === ed });
})()
""" % (_JS_PICK, _css(s["editor"]))


def js_click_send(p):
    s = p["sel"]
    return """
(() => {
  %s
  const b = pick(%s);
  if (!b) return JSON.stringify({ ok: false, why: 'no-send-button' });
  if (isDisabled(b)) return JSON.stringify({ ok: false, why: 'disabled' });
  b.click();
  return JSON.stringify({ ok: true });
})()
""" % (_JS_PICK, _css(s["send"]))


def js_read_last(p):
    s = p["sel"]
    # 平台声明的「读正文时要剔除的噪声子树」（豆包：回复里的推荐提问 suggest_message_list）。
    # 在 clone 上剔除，不动页面——原文仍完整，只是不把 UI 文案当 AI 正文。
    ignore = json.dumps(s.get("read_ignore") or [], ensure_ascii=False)
    return """
(() => {
  %s
  const IGNORE = %s;
  const strip = (el) => {
    if (!IGNORE.length) return el;
    const c = el.cloneNode(true);
    for (const sel of IGNORE) {
      try { c.querySelectorAll(sel).forEach(n => n.remove()); } catch (e) {}
    }
    return c;
  };
  const msgs = pickAll(%s);
  if (msgs.length) {
    const t = (strip(msgs[msgs.length - 1]).innerText || '').trim();
    if (t) return JSON.stringify({ ok: true, text: t });
  }
  %s
  const t = (mainText || '').trim();
  if (t) return JSON.stringify({ ok: true, text: t, fallback: true });
  return JSON.stringify({ ok: false, text: '' });
})()
""" % (_JS_PICK, ignore, _css(s["messages"]), _JS_TAIL_FALLBACK)


def anchor_delta(before: dict, after: dict) -> dict:
    """按**消息身份锚点**算增量，而不是按「当前渲染出的节点数」。

    为什么必须这样：虚拟列表（DeepSeek 的 `div.ds-virtual-list`）只渲染视口附近
    的消息，滚到底部读到的 `reply_count` 只是「渲染出的气泡数」。节点被卸载/重挂
    时计数会跳，delta 可能算出 0（漏判成功）或 2（误判重发）——
    而 delta 是整个闭环的信任根，根不稳上面全是沙上建塔（架构评审 F1）。

    两级锚点，能硬就不软：
      1. **平台消息 ID**（`asst_keys`，如 DeepSeek 的 `data-virtual-list-item-key`）：
         消息身份本身，正文被折叠、markdown 重排、两条回复正文相同都不受影响。
      2. **正文指纹**（`asst_fps`，去 markdown 后前 64 字）：平台没给 ID 时兜底。

    `anchors_available=False` 表示两侧都不可信（发送前明明有回复却一个锚点都给不出，
    说明选择器或渲染出了问题），调用方应退回计数口径并降级为 INCONCLUSIVE。
    """
    b, a = (before or {}), (after or {})
    bkeys = [k for k in (b.get("asst_keys") or []) if k]
    akeys = [k for k in (a.get("asst_keys") or []) if k]
    bfps = list(b.get("asst_fps") or [])
    afps = list(a.get("asst_fps") or [])
    bu = list(b.get("user_fps") or [])
    au = list(a.get("user_fps") or [])
    bcnt, acnt = int(b.get("reply_count") or 0), int(a.get("reply_count") or 0)

    kind = ""
    new_asst, last_changed = [], False
    if bkeys and akeys:
        kind = "key"
        bs, as_ = set(bkeys), set(akeys)
        new_asst = [k for k in akeys if k not in bs]
        last_changed = akeys[-1] != bkeys[-1]
    elif bfps and afps:
        kind = "fp"
        bs, as_ = set(bfps), set(afps)
        new_asst = [f for f in afps if f not in bs]
        last_changed = afps[-1] != bfps[-1]
    else:
        # 两级都拿不到：这一侧不可信
        new_asst, last_changed = [], False

    # 「说有回复却一个锚点都给不出」= 这一侧不可信（另一方为 0 条回复时不算）
    before_ok = bool(bkeys or bfps) or bcnt == 0
    after_ok = bool(akeys or afps) or acnt == 0
    new_user = [f for f in au if f not in set(bu)]
    b_last = b.get("last_asst_fp") or (bfps[-1] if bfps else "")
    a_last = a.get("last_asst_fp") or (afps[-1] if afps else "")
    return {
        "anchors_available": bool(before_ok and after_ok and (kind or (bcnt == 0 and acnt == 0))),
        "anchor_kind": kind,
        "new_asst": len(new_asst),
        "new_user": len(new_user),
        "new_asst_fps": new_asst[:5],
        # 强判据：末条助手锚点变了 = 确实来了一条新回复（与计数无关）
        "last_asst_changed": bool(last_changed or (bool(a_last) and a_last != b_last)),
        "before_asst_fps": len(bfps),
        "after_asst_fps": len(afps),
        "before_asst_keys": len(bkeys),
        "after_asst_keys": len(akeys),
    }


def make_needle(text: str) -> str:
    """从待发文本里挑一段「平台无关、渲染后仍能对上」的指纹。

    取最长的实质行，再去掉 markdown 修饰符（# * ` > 等），只留实义字符——
    因为平台会把 '# 标题' 渲染成标题、把 '**粗体**' 渲染成粗体，
    带修饰符的指纹在渲染后对不上。
    """
    lines = [ln.strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if len(ln) >= 16]
    raw = max(lines, key=len) if lines else (text or "")[:40]
    clean = re.sub(r"[#*`>\[\]\(\)~_\-|]+", "", raw)
    return clean[:40]


def js_has_text(p, text):
    """判断「我发出去的那条消息」是否真的出现在页面上——平台无关的发送确认信号。

    为什么必须有它：实测 Kimi 免费额度繁忙时会**清空输入框却拒绝创建会话**
    （弹限流提示弹窗）。只看「输入框清空了就当发送成功」会谎报成功，
    下一轮就可能重复提问、或对着一个不存在的会话继续等。
    """
    s = p["sel"]
    needle = json.dumps(make_needle(text), ensure_ascii=False)
    return """
(() => {
  %s
  const sc = pick(%s);
  const mainEl = document.querySelector('main') || document.body;
  const full = (sc ? (sc.innerText || '') : (mainEl.innerText || '')) || '';
  const norm = (t) => (t || '').replace(/[\\s#*`>\\[\\]\\(\\)~_\\-|]+/g, '');
  const hay = norm(full);
  const needle = norm(%s);
  const hit = !!needle && hay.indexOf(needle) >= 0;
  return JSON.stringify({ ok: hit, hay_len: hay.length, needle_len: needle.length,
                          haystack: hay.length ? hay.slice(0, 80) : '' });
})()
""" % (_JS_PICK, _css(s["scroll"]), needle)


def js_scroll(p):
    s = p["sel"]
    return """
(() => {
  %s
  const sc = pick(%s);
  if (sc) { sc.scrollTop = sc.scrollHeight; }
  return JSON.stringify({ scrolled: !!sc });
})()
""" % (_JS_PICK, _css(s["scroll"]))
