#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ChatGPT 桥接器 v2：修掉「静默发不出去却抓回上一条旧回复」的失败模式。

与 bridge.py 的差别（页面改版后必须的修正）
--------------------------------------------
1. 输入框写入改用 `document.execCommand('insertText')`：新版 ProseMirror 不接收
   合成 ClipboardEvent 的粘贴，`bridge.py` 的 paste 分支会「看起来成功但输入框仍是空的」。
2. 发送前**硬校验**输入框字数 ≥ 阈值，不足直接报错退出，不许继续。
3. 回复判定改为「`main` 总字数必须比发送前增长」+ 流式结束且字数稳定，
   **不再**依赖 `data-message-author-role`（该属性已从页面移除）。
4. 取回复用「发送前 main 文本长度」做切点，只取新增部分，不会抓到上一条回复。

用法：
  python send2.py send --session rdrx --file msg.md --out reply.md [--timeout 600]
  python send2.py status --session rdrx
"""
from __future__ import annotations

import argparse
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

# 文本用 %s 注入（json.dumps 已转义）
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

STATE_JS = r"""
(function(){
  var el = document.querySelector('form .ProseMirror');
  var stop = document.querySelector('[data-testid="stop-button"]')
          || document.querySelector('button[aria-label*="停止"]');
  var main = document.querySelector('main') || document.body;
  var t = main.innerText || '';
  var junk = ['你正在使用 ChatGPT', 'ChatGPT 可能会出错', '免费试用 Plus',
              '最新一条回复', '拖拽到此处完成下载'];
  for (var k = 0; k < junk.length; k++) {
    var j = t.indexOf(junk[k]);
    if (j > 0) t = t.slice(0, j);
  }
  return JSON.stringify({
    composer: el ? (el.innerText || '').length : -1,
    streaming: !!stop,
    mainLen: t.length,
    main: t
  });
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


def state(session):
    raw = evaluate(session, STATE_JS)
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return {"raw": raw}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["send", "status"])
    ap.add_argument("--session", required=True)
    ap.add_argument("--file")
    ap.add_argument("--out")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--settle", type=float, default=8.0)
    a = ap.parse_args()

    if a.cmd == "status":
        st = state(a.session)
        st.pop("main", None)
        print(json.dumps(st, ensure_ascii=False))
        return 0

    text = open(a.file, encoding="utf-8").read()
    before = state(a.session)
    base = before["mainLen"]
    print("[1] 发送前 mainLen=%d composer=%d" % (base, before["composer"]), file=sys.stderr)

    print("[2] 清空 ->", evaluate(a.session, CLEAR_JS), file=sys.stderr)
    typed = evaluate(a.session, TYPE_JS % json.dumps(text))
    typed = json.loads(typed) if isinstance(typed, str) else typed
    print("[3] 写入 ->", typed, file=sys.stderr)
    if not typed or not typed.get("ok") or typed.get("len", 0) < min(200, len(text) // 2):
        print("ERROR 输入框写入失败（len=%s），已中止，不发送" % (typed or {}).get("len"),
              file=sys.stderr)
        return 2

    how = evaluate(a.session, SEND_JS)
    print("[4] 发送 ->", how, file=sys.stderr)
    time.sleep(2)
    st = state(a.session)
    if st["mainLen"] <= base and not st["streaming"]:
        how2 = evaluate(a.session, SEND_JS)
        print("[4b] 未发出，重试 ->", how2, file=sys.stderr)
        time.sleep(2)
        st = state(a.session)

    deadline = time.time() + a.timeout
    grew = False
    stable_since = None
    last_len = st["mainLen"]
    while time.time() < deadline:
        if st["mainLen"] > base:
            grew = True
        if st["streaming"]:
            stable_since = None
        elif grew:
            if st["mainLen"] == last_len:
                if stable_since is None:
                    stable_since = time.time()
                elif time.time() - stable_since >= a.settle:
                    break
            else:
                stable_since = time.time()
        last_len = st["mainLen"]
        time.sleep(2)
        st = state(a.session)

    if not grew:
        print("ERROR 正文没有增长（mainLen 仍为 %d）：消息可能没发出去，"
              "拒绝把旧回复当新回复返回" % st["mainLen"], file=sys.stderr)
        return 3

    reply = st["main"][base:].strip()
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(reply)
        print("saved -> %s (%d chars)" % (a.out, len(reply)), file=sys.stderr)
    print(reply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
