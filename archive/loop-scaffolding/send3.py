#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ChatGPT 桥接器 v3：修掉「靠 main 长度差切回复」的失败模式。

v2（send2.py）的两个残留问题
----------------------------
1. 发送环节已经可靠了（`execCommand('insertText')` + 发送前硬校验字数）。
2. **取回复仍然不可靠**：v2 用「发送前 `main.innerText` 的长度」当切点，
   即 `main[base:]`。但 ChatGPT 是**虚拟滚动**——发新消息时页面滚到底，
   旧消息节点会被卸载，`main` 总字数可能**变短**（实测 35776 → 32341）。
   于是 `main[base:]` 切出来的是**我自己刚发的那条消息**，而不是回复。
   表面成功，实则拿到错内容，比报错更危险。

v3 的做法：不再数长度，改为**按消息节点取最后一条 assistant**
-------------------------------------------------------------
  · 每轮对话是 `<article>`，assistant 那轮的正文在 `.markdown` 里（user 轮没有）；
  · 发送前记下「最后一条 assistant 的文本指纹」；
  · 发送后轮询，直到「最后一条 assistant 的文本 != 旧指纹」且「停止按钮消失」
    且「文本连续 stable 秒不再变化」→ 认定流式结束，取该节点的完整文本。

这样即使旧节点被卸载、总长度变短，也不影响正确性：我们要的永远只有**最后一条**。

用法：
  python send3.py send --session rdrx --file msg.md --out reply.md [--timeout 600]
  python send3.py status --session rdrx
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time

BSK = "bsk"

CLEAR_JS = r"""
(function(){
  var el = document.querySelector('form .ProseMirror');
  if (!el) return 'NO_COMPOSER';
  el.focus();
  try {
    var sel = window.getSelection(), r = document.createRange();
    r.selectNodeContents(el); sel.removeAllRanges(); sel.addRange(r);
    document.execCommand('delete');
  } catch (e) {}
  return 'CLEARED:' + (el.innerText || '').length;
})()
"""

TYPE_JS = r"""
(function(){
  var text = %s;
  var el = document.querySelector('form .ProseMirror');
  if (!el) return JSON.stringify({ok:false, err:'NO_COMPOSER'});
  el.focus();
  var ok = document.execCommand('insertText', false, text);
  return JSON.stringify({ok:true, cmd:ok, len:(el.innerText||'').length});
})()
"""

SEND_JS = r"""
(function(){
  var btns = [].slice.call(document.querySelectorAll('form button'));
  var cand = btns.filter(function(b){
    var al = b.getAttribute('aria-label') || '';
    var dt = b.getAttribute('data-testid') || '';
    return al === '发送' || al === 'Send' || dt === 'send-button';
  });
  var vis = cand.filter(function(b){ return !b.disabled && b.offsetParent !== null; });
  if (vis.length) { vis[0].click(); return 'CLICKED'; }
  var el = document.querySelector('form .ProseMirror');
  if (!el) return 'NO_COMPOSER';
  el.focus();
  var ev = new KeyboardEvent('keydown', {key:'Enter', code:'Enter', keyCode:13,
                                         which:13, bubbles:true, cancelable:true});
  el.dispatchEvent(ev);
  return 'ENTER';
})()
"""

# 按「轮」取 assistant 正文。
# 2026-10-27 页面改版：`article` / `.markdown` / `[data-message-author-role]` 全部消失，
# 用户消息变成可编辑的 `chatgpt-writing-block`（ProseMirror），
# assistant 正文落在 `[class*="MarkdownRoot"]`（外层 `DilResponseRoot`）。
# 多套选择器并排兜底，任一命中即可，避免下次改版又静默取空。
TURN_JS = r"""
(function(){
  var sels = ['[class*="MarkdownRoot"]', '.markdown', 'article .markdown'];
  var asst = [];
  for (var s = 0; s < sels.length; s++) {
    var nodes = [].slice.call(document.querySelectorAll(sels[s]));
    var texts = nodes.map(function(e){ return (e.innerText || '').trim(); })
                     .filter(function(t){ return t.length > 0; });
    if (texts.length > asst.length) asst = texts;
  }
  var stop = document.querySelector('[data-testid="stop-button"]')
          || document.querySelector('button[aria-label*="停止"]');
  var comp = document.querySelector('form .ProseMirror');
  return JSON.stringify({
    asstCount: asst.length,
    lastText: asst.length ? asst[asst.length - 1] : '',
    streaming: !!stop,
    composer: comp ? (comp.innerText || '').length : -1
  });
})()
"""


def bsk(args, timeout=120):
    p = subprocess.run([BSK] + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, p.stdout or "", p.stderr or ""


def evaluate(session, expr, timeout=120):
    rc, out, err = bsk(["evaluate", expr, "--session", session, "--json"], timeout=timeout)
    s = out.strip()
    i = s.find("{")
    data = json.loads(s[i:]) if i >= 0 else None
    if rc != 0:
        raise RuntimeError("evaluate failed: %s %s" % (err.strip()[:200], s[:200]))
    if isinstance(data, dict) and "value" in data:
        return data["value"]
    return data


def turn(session):
    raw = evaluate(session, TURN_JS)
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return {"asstCount": -1, "lastText": "", "streaming": False, "composer": -1}


def fp(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:12]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["send", "status"])
    ap.add_argument("--session", required=True)
    ap.add_argument("--file")
    ap.add_argument("--out")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--settle", type=float, default=8.0)
    ap.add_argument("--skip-type", action="store_true",
                    help="输入框已写好内容时用：跳过复用写入，直接发送并等回复"
                         "（长文本 insertText 可能让 bsk 的 30s RPC 超时，但文字其实已写入）")
    a = ap.parse_args()

    if a.cmd == "status":
        st = turn(a.session)
        st["fp"] = fp(st.get("lastText"))
        st["len"] = len(st.get("lastText") or "")
        st.pop("lastText", None)
        print(json.dumps(st, ensure_ascii=False))
        return 0

    text = open(a.file, encoding="utf-8").read()
    before = turn(a.session)
    old_fp = fp(before.get("lastText"))
    print("[1] 发送前：assistant 轮 %d 条，末条指纹 %s（%d 字），输入框 %d 字" % (
        before["asstCount"], old_fp, len(before.get("lastText") or ""), before["composer"]),
        file=sys.stderr)

    if a.skip_type:
        have = turn(a.session).get("composer", 0)
        print("[2] 跳过清空+写入（保留输入框现有内容）：%d 字" % have, file=sys.stderr)
        if have < min(200, len(text) // 2):
            print("ERROR 输入框内容不足（%d 字），拒绝发送" % have, file=sys.stderr)
            return 2
    else:
        print("[2] 清空 ->", evaluate(a.session, CLEAR_JS), file=sys.stderr)
        typed = evaluate(a.session, TYPE_JS % json.dumps(text))
        typed = json.loads(typed) if isinstance(typed, str) else typed
        print("[3] 写入 ->", typed, file=sys.stderr)
        if not typed or not typed.get("ok") or typed.get("len", 0) < min(200, len(text) // 2):
            print("ERROR 输入框写入失败（len=%s），已中止，不发送" % (typed or {}).get("len"),
                  file=sys.stderr)
            return 2

    print("[4] 发送 ->", evaluate(a.session, SEND_JS), file=sys.stderr)
    time.sleep(2)
    st = turn(a.session)
    if st.get("composer", 0) > 20 and not st.get("streaming"):
        # 输入框还有内容且没在生成 → 大概没发出去
        print("[4b] 疑似未发出（输入框仍有 %d 字），重试 -> %s" % (
            st["composer"], evaluate(a.session, SEND_JS)), file=sys.stderr)
        time.sleep(2)
        st = turn(a.session)

    deadline = time.time() + a.timeout
    seen_new = False
    stable_since = None
    last_fp = fp(st.get("lastText"))
    last_txt = st.get("lastText") or ""

    while time.time() < deadline:
        st = turn(a.session)
        cur_fp = fp(st.get("lastText"))
        cur_txt = st.get("lastText") or ""
        if cur_fp != old_fp:
            seen_new = True
        if st.get("streaming"):
            stable_since = None
        elif seen_new and cur_txt:
            if cur_fp == last_fp:
                if stable_since is None:
                    stable_since = time.time()
                elif time.time() - stable_since >= a.settle:
                    break
            else:
                stable_since = time.time()
        last_fp, last_txt = cur_fp, cur_txt
        time.sleep(2)

    if not seen_new:
        print("ERROR 没有出现新的 assistant 轮（末条指纹仍是 %s）："
              "拒绝把旧回复当新回复返回" % old_fp, file=sys.stderr)
        return 3
    if not last_txt:
        print("ERROR 新 assistant 轮是空的，拒绝保存空回复", file=sys.stderr)
        return 4

    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(last_txt)
        print("saved -> %s (%d chars, fp=%s)" % (a.out, len(last_txt), last_fp),
              file=sys.stderr)
    print(last_txt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
