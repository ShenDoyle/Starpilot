# DOM 判定片段

所有片段都是**返回 JSON 字符串的 IIFE**，可直接喂给 `bsk evaluate`。
返回 `JSON.stringify(...)` 而不是对象，是为了绕开 CDP 对复杂返回值的序列化差异。

> **本文是参考实现，不是运行时真相。**
> 星舵运行时用的片段由 `scripts/platforms.py` 按平台生成（`platforms.js_probe` / `js_fill` /
> `js_click_send` / `js_read_last` / `js_scroll`）。下面的片段是 ChatGPT 的实测基准，
> 用来理解判定逻辑、用来给新平台写选择器，**不要直接复制到主流程里**。
> 各平台的选择器以 `platforms.py` 的 `sel` 为唯一来源。

---

## 1. 综合探测（首选，一个片段拿全部状态）

```js
(() => {
  const q = (s) => document.querySelector(s);
  const stop   = q('button[data-testid="stop-button"]');
  const send   = q('button[data-testid="send-button"]');
  const speech = q('[data-testid="composer-speech-button"], button[aria-label*="语音"], button[aria-label*="Voice"]');
  const editor = q('#prompt-textarea') || q('div[contenteditable="true"]');
  const msgs   = [...document.querySelectorAll('[data-message-author-role]')];
  const last   = msgs.length ? msgs[msgs.length - 1] : null;
  const text   = last ? (last.innerText || '').trim() : '';
  const errs   = [...document.querySelectorAll('[role="alert"]')]
                   .map(e => (e.innerText || '').trim()).filter(Boolean).join(' | ');
  return JSON.stringify({
    url: location.href,
    generating: !!stop,
    can_send: !!send,
    speech: !!speech,
    editor: !!editor,
    editor_len: editor ? (editor.innerText || '').length : -1,
    msg_count: msgs.length,
    last_role: last ? last.getAttribute('data-message-author-role') : null,
    last_len: text.length,
    last_tail: text.slice(-300),
    error: errs
  });
})()
```

字段用途：

| 字段 | 判定 |
|---|---|
| `url` | 是否为绑定的 `/c/<uuid>`；变成 `/` 或 `/new` 立即停 |
| `generating` | `true` = 仍在生成，不能发下一条 |
| `editor_len` | 写入后校验字数；`0` 说明没写进去 |
| `msg_count` | 发送后必须增长，否则「点了但没发出去」 |
| `last_role` | 必须是 `assistant` 才算收到回复 |
| `last_len` | 两次采样相同才判稳定 |
| `speech` | 输入框右侧是语音按钮 = 空闲态（发送按钮位置已还原） |
| `error` | 页面级报错，非空即按失败处理 |

---

## 2. 只判「回复是否结束」

```js
(() => {
  const stop = document.querySelector('button[data-testid="stop-button"]');
  const msgs = document.querySelectorAll('[data-message-author-role]');
  const last = msgs.length ? msgs[msgs.length - 1] : null;
  const t = last ? (last.innerText || '').trim() : '';
  return JSON.stringify({ done: !stop && !!last && last.getAttribute('data-message-author-role') === 'assistant', len: t.length });
})()
```

**不要单独用它做最终判定**——`done: true` 也可能发生在「生成中断」瞬间。必须配合 `len` 两次采样一致。

---

## 3. 取末条回复全文

```js
(() => {
  const msgs = document.querySelectorAll('[data-message-author-role="assistant"]');
  if (!msgs.length) return JSON.stringify({ ok: false, text: '' });
  return JSON.stringify({ ok: true, text: (msgs[msgs.length - 1].innerText || '').trim() });
})()
```

---

## 4. 写入文本（不发送）

```js
(() => {
  const el = document.querySelector('#prompt-textarea') || document.querySelector('div[contenteditable="true"]');
  if (!el) return JSON.stringify({ ok: false, why: 'no-editor' });
  el.focus();
  document.execCommand('selectAll', false, null);
  document.execCommand('insertText', false, __TEXT_JSON__);
  return JSON.stringify({ ok: true, len: (el.innerText || '').length });
})()
```

`__TEXT_JSON__` 用 `json.dumps(text)` 的产物替换（含引号与转义），不要手工拼接。

**已知问题**：文本较长（>4000 字符）时 RPC 可能超时，但内容**其实已写入**。
超时后**不要重写**，直接读 `editor_len` 核对；只有长度不符才重写一次。

---

## 5. 点击发送

```js
(() => {
  const b = document.querySelector('button[data-testid="send-button"]');
  if (!b) return JSON.stringify({ ok: false, why: 'no-send-button' });
  if (b.disabled || b.getAttribute('aria-disabled') === 'true') return JSON.stringify({ ok: false, why: 'disabled' });
  b.click();
  return JSON.stringify({ ok: true });
})()
```

点击成功 ≠ 发送成功。必须再核对 `editor_len == 0` 且 `msg_count` 增长。

---

## 6. 登录态判定

```js
(() => {
  const editor = document.querySelector('#prompt-textarea') || document.querySelector('div[contenteditable="true"]');
  const loginBtn = [...document.querySelectorAll('button, a')]
    .map(e => (e.innerText || '').trim())
    .filter(t => /^(登录|Log in|Sign in)$/i.test(t)).length;
  return JSON.stringify({ logged_in: !!editor && loginBtn === 0, login_buttons: loginBtn });
})()
```

**仅凭「没看到登录按钮」不算登录成功**——必须 `editor` 存在（能输入才算真的进去了）。

---

## 适配其他站点

换站点时**只改选择器，判定逻辑不动**。选择器写在 `scripts/platforms.py` 的 `sel` 里，每个平台一份。

各平台的选择器优先级表（`platforms.py` 的 `sel`）：

| 用途 | ChatGPT（已实测） | 其他平台的候选形态 |
|---|---|---|
| 输入框 | `#prompt-textarea` | `div[contenteditable="true"]`、`textarea`、`[contenteditable][role="textbox"]` |
| 发送按钮 | `button[data-testid="send-button"]` | `button[aria-label*="Send"]`、`button[type="submit"]` |
| 停止生成 | `button[data-testid="stop-button"]` | `button[aria-label*="Stop"]`、`[aria-label*="停止"]` |
| assistant 消息 | `[data-message-author-role="assistant"]` | 站点自定义，需实测后固化为常量 |
| 全部消息 | `[data-message-author-role]` | 用于数消息总数 |
| 报错 | `[role="alert"]` | 站点自定义 |
| 滚动容器 | `.thread-scroll-container` | `div[class*="overflow-y-auto"]`、`main` |

**三条硬要求**：

1. 候选列表按优先级排，`platforms.py` 取第一个命中的；实测发现更准的候选就往前提。
2. **换站后第一次必须跑 `wa.py probe --platform <key>` 打真实值**，把结果回填 `platforms.py`，
   不要凭猜。回填后把 `verified` 改成 `True` 并在 `notes` 写上实测日期。
3. `prefer_marker=True` 的平台（正文区混着 UI 文案）必须同时配好 `markers` 与 `tail_junk`，
   否则 `last_len` 会把按钮字算进去，`wait` 永远等不到「文本稳定」。

接入完整流程（选平台 → 四步接入 → 记忆自测）见 `references/web-ai-platforms.md`。
