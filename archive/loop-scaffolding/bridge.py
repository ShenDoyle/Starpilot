#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ChatGPT 桥接器：把本地文本发到已打开的 ChatGPT 对话页，等回答，取回文本。

用法：
  python bridge.py send   --session tkrr --file msg.md [--out reply.md]
  python bridge.py wait   --session tkrr --out reply.md
  python bridge.py status --session tkrr

依赖：bsk（browser-skill CLI）已在 PATH，目标页是该对话页且已登录。
"""
import argparse
import json
import os
import subprocess
import sys
import time

BSK = "bsk"


def run_bsk(args, timeout=120):
    cmd = [BSK] + args
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, (p.stdout or ""), (p.stderr or "")


def evaluate(session, expr, timeout=120):
    code, out, err = run_bsk(["evaluate", expr, "--session", session, "--json"], timeout=timeout)
    if code != 0:
        raise RuntimeError("evaluate failed: %s %s" % (err.strip(), out.strip()))
    # --json 输出 {"ok":true,"tab_id":...,"value":...}；前面可能混有升级提示行
    s = out.strip()
    i = s.find("{")
    if i < 0:
        return s
    try:
        data = json.loads(s[i:])
    except Exception:
        return s
    if isinstance(data, dict) and "value" in data:
        return data["value"]
    return data


PASTE_JS = r"""
(function(){
  var text = %s;
  var el = document.querySelector('form .ProseMirror')
        || document.querySelector('form [contenteditable="true"]')
        || document.querySelector('#prompt-textarea')
        || document.querySelector('div[contenteditable="true"]');
  if(!el) return 'NO_COMPOSER';
  el.focus();
  /* 先清空输入框，避免「追加」到上一轮残留文本后面 */
  try {
    var sel = window.getSelection();
    var range = document.createRange();
    range.selectNodeContents(el);
    sel.removeAllRanges();
    sel.addRange(range);
    document.execCommand('delete');
  } catch (e) {}
  var dt = new DataTransfer();
  dt.setData('text/plain', text);
  var ev = new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true});
  el.dispatchEvent(ev);
  return 'PASTED:' + (el.innerText || '').length;
})()
"""

STATE_JS = r"""
(function(){
  var el = document.querySelector('form .ProseMirror')
        || document.querySelector('form [contenteditable="true"]')
        || document.querySelector('#prompt-textarea')
        || document.querySelector('div[contenteditable="true"]');
  var stop = document.querySelector('[data-testid="stop-button"]') || document.querySelector('button[aria-label*="停止"]');
  var send = document.querySelector('form button[aria-label="发送"]')
          || document.querySelector('form button[aria-label="Send"]')
          || document.querySelector('[data-testid="send-button"]');
  var nodes = document.querySelectorAll('div[data-message-author-role="assistant"]');
  var copyBtns = document.querySelectorAll('[data-testid="copy-turn-action-button"]').length
    + document.querySelectorAll('button[aria-label="重新生成回复"]').length;
  var last = '';
  var main = document.querySelector('main') || document.body;   /* 两个分支都要用，先取 */
  var count = nodes.length;
  if (count) {
    last = nodes[count - 1].innerText;
  } else {
    var t = main.innerText || '';
    var i = t.lastIndexOf('ChatGPT 说：');
    if (i >= 0) {
      last = t.slice(i + 'ChatGPT 说：'.length);
      count = 1;
    } else if (t.length) {
      last = t;
    }
  }
  // 去掉页面尾部 UI 文案
  var junk = ['你正在使用 ChatGPT', 'ChatGPT 可能会出错', '免费试用 Plus', '最新一条回复', '询问 ChatGPT'];
  for (var k = 0; k < junk.length; k++) {
    var j = last.indexOf(junk[k]);
    if (j > 0) last = last.slice(0, j);
  }
  last = (last || '').replace(/\n思考\s*$/, '').trim();
  var thinking = /正在思考|思考中/.test(last.slice(-80));
  return JSON.stringify({
    composer: el ? (el.innerText || '').length : -1,
    streaming: !!stop,
    canSend: !!send,
    thinking: thinking,
    copyBtns: copyBtns,
    mainLen: (main.innerText || '').length,
    count: count,
    lastLen: last.length,
    last: last
  });
})()
"""


def get_state(session):
    raw = evaluate(session, STATE_JS)
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {"raw": raw}
    return raw


def paste(session, text):
    expr = PASTE_JS % json.dumps(text)
    raw = evaluate(session, expr)
    return raw


SEND_SELECTORS = [
    'form button[aria-label="发送"]',      # 2026-10 现行页面（ProseMirror 输入框）
    'form button[aria-label="Send"]',
    'button.size-token-button-composer',
    '[data-testid="send-button"]',         # 旧版兜底
]


def submit(session):
    """点发送；逐个选择器尝试，全失败再回车。返回实际使用的方式（便于留证）。"""
    for sel in SEND_SELECTORS:
        code, out, err = run_bsk(["click", sel, "--session", session], timeout=60)
        if code == 0:
            return sel
    run_bsk(["press", "Enter", "--session", session], timeout=60)
    return "Enter"


def wait_reply(session, timeout=300, settle=8.0, min_main=None):
    """等 AI 回答结束。

    · 完成信号：无 stop 按钮、非思考中、最后一条下方有操作栏
    · **必须**正文长度比发送前增长（`min_main`），否则会把「上一轮的旧回复」当成新回复
    · 再要求长度连续 settle 秒不变，避免半截回复
    """
    t0 = time.time()
    last_len = -1
    stable_since = None
    started = False
    while time.time() - t0 < timeout:
        st = get_state(session)
        if "last" not in st:
            time.sleep(2)
            continue
        ln = st["lastLen"]
        ml = st.get("mainLen") or 0
        grew = (min_main is None) or (ml > min_main + 30)
        done = (not st.get("streaming")) and (not st.get("thinking")) \
            and st.get("copyBtns", 0) > 0 and grew and ln > 0
        if st.get("streaming") or st.get("thinking") or (grew and ln > 0):
            started = True
        if started and done and ln == last_len:
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= settle:
                return st
        else:
            stable_since = None
            last_len = ln
        time.sleep(2)
    st = get_state(session)
    st["timeout"] = True
    return st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["send", "wait", "status"])
    ap.add_argument("--session", required=True)
    ap.add_argument("--file")
    ap.add_argument("--out")
    ap.add_argument("--timeout", type=int, default=300)
    a = ap.parse_args()

    if a.cmd == "status":
        st = get_state(a.session)
        st.pop("last", None)
        print(json.dumps(st, ensure_ascii=False))
        return

    if a.cmd == "send":
        with open(a.file, "r", encoding="utf-8") as f:
            text = f.read()
        before = get_state(a.session)
        base_main = before.get("mainLen") or 0
        r = paste(a.session, text)
        print("paste ->", r, file=sys.stderr)
        time.sleep(1.0)
        st = get_state(a.session)
        if st.get("composer", 0) < len(text) * 0.5:
            print("WARN 粘贴长度异常: composer=%s 期望≈%s" % (st.get("composer"), len(text)),
                  file=sys.stderr)
        how = submit(a.session)
        print("submitted via %s, waiting reply..." % how, file=sys.stderr)
    else:
        before = get_state(a.session)
        base_main = None

    st = wait_reply(a.session, timeout=a.timeout, min_main=base_main)
    reply = st.get("last", "")
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(reply)
        print("saved -> %s (%d chars)" % (a.out, len(reply)), file=sys.stderr)
    print(reply)


if __name__ == "__main__":
    main()
