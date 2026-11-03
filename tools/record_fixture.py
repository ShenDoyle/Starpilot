#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 `bsk get-html` 录到的真机快照裁成离线回放夹具（tests/fixtures/<平台>/）。

为什么要有这个脚本（2026-11-03）：ChatGPT 11-02 改版把 `data-message-author-role`
整个删掉，而仓库里**没有 ChatGPT 夹具** —— 选择器失效在单测阶段一条都不红，
一路红到线上：`reply_count` 恒 0 ⇒ 每次发送判 `SENT_NO_REPLY` ⇒ agent 以为没发出去
就重发 ⇒ 页面出现重复提问 ⇒ 闸门又拦 ⇒ 走逃生门 ⇒ 台账攒违例。
一条链的起点，只是「没人给这个平台录过夹具」。所以**录制必须便宜到愿意常做**。

裁剪原则：**结构必须真，体积可以小**
  · 删 `<script>`/`<style>` 正文（保留标签本身，选择器不匹配它们）
  · 删 `<svg>` 图形数据（图标不参与任何平台选择器）
  · 删 base64 内联资源
  · 再把切片收敛到「包含全部轮次容器的区域 + 前后各 3KB 上下文」
元素树、class、data-* 一律保持真机原样，`tests/domsel.py` 的重放才等价于真机。

用法：
    bsk get-html --session <sid> --out runtime/wa/_page.html   # 只读录制
    python tools/record_fixture.py chatgpt runtime/wa/_page.html          # 生成夹具
    python tools/record_fixture.py chatgpt _page.html --region form       # 只截 form（composer）
    python tools/record_fixture.py chatgpt _page.html --report            # 只看命中数不写文件

⚠️ 录 composer 时**编辑器里必须有字**：ChatGPT/Gemini 的发送键是条件渲染的，
空输入态录下来的快照里根本没有那个按钮，据此定标会把发送键判成「不存在」。
录完要跑 `python -m unittest tests.test_platforms_replay` —— 断言不过就是标定没成功。
"""
from __future__ import annotations

import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(ROOT, "tests", "fixtures")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import safeprint  # noqa: E402

safeprint.harden_streams()      # 本脚本大量打印中文，管道里必须是 UTF-8

SCRIPT_BODY = re.compile(r"(<(?:script|style)\b[^>]*>)(.*?)(</\1>)", re.S | re.I)
SVG_BLOCK = re.compile(r"<svg\b.*?(</svg>|/>)", re.S | re.I)
DATA_URI = re.compile(r"(data:[a-z/+-]+;base64,)[A-Za-z0-9+/=]{40,}")

# 每个平台「轮次身份」的属性名：切片要包住它们，否则夹具验不到锚点
REGION_ATTRS = {
    "chatgpt": ("data-chatgpt-search-message-ids", "data-user-message-bubble",
                "data-chatgpt-selection-message-id"),
    "deepseek": ("data-virtual-list-item-key", "ds-message"),
    "gemini": ("conversation-container", "user-query", "model-response"),
    "kimi": ("chat-content-item",),
    "doubao": ("union_message", "receive_message", "send_message"),
}


def trim(html: str) -> str:
    html = SCRIPT_BODY.sub(lambda m: m.group(1) + m.group(3), html)
    html = SVG_BLOCK.sub("<svg></svg>", html)
    html = DATA_URI.sub(r"\1TRIMMED", html)
    return re.sub(r"[ \t]{2,}", " ", html)


def slice_region(html: str, platform: str, pad: int = 3000) -> str:
    """把切片收敛到「包住全部消息身份属性」的最小连续区间。"""
    attrs = REGION_ATTRS.get(platform) or ()
    hits = [html.find(a) for a in attrs if html.find(a) >= 0]
    if not hits:
        return html
    first, last = min(hits), max(html.rfind(a) for a in attrs if html.rfind(a) >= 0)
    s = max(0, html.rfind("<div", 0, first) - pad)
    e = html.find("</div>", last)
    e = len(html) if e < 0 else min(len(html), e + pad)
    return html[s:e]


def counts(html: str, platform: str) -> dict:
    out = {}
    for a in REGION_ATTRS.get(platform, ()):
        out[a] = len(re.findall(re.escape(a), html))
    return out


def main(argv: list) -> int:
    """argv 已经是 sys.argv[1:]：0=平台 1=快照路径，所以 `--report` 时长度是 3。
    （第一版按「含脚本名」的下标写，结果 `chatgpt x.html --report` 把 `--report`
    当成文件路径，报「找不到快照」——CLI 的参数错位只有真跑一遍才照得出来。）
    """
    if len(argv) < 2:
        print(__doc__)
        return 2
    platform, src = argv[0], argv[1]
    report_only = "--report" in argv
    region = ""
    for i, a in enumerate(argv):
        if a == "--region" and i + 1 < len(argv):
            region = argv[i + 1]
    if not os.path.exists(src):
        print("找不到快照：%s" % src, file=sys.stderr)
        return 1
    with io.open(src, encoding="utf-8", errors="replace") as f:
        raw = f.read()
    body = trim(raw)
    if region:
        m = re.search(r"<%s\b.*?</%s>" % (region, region), body, re.S | re.I)
        if not m:
            print("快照里找不到 <%s> 元素——是不是页面还没渲染完就录了？" % region,
                  file=sys.stderr)
            return 1
        body = m.group(0)
        name = "%s.html" % region
    else:
        body = slice_region(body, platform)
        name = "messages.html"
    print("原始 %d 字节 → 裁剪后 %d 字节（%.0f%%）"
          % (len(raw), len(body.encode("utf-8")),
             100.0 * len(body.encode("utf-8")) / max(1, len(raw.encode("utf-8")))))
    c = counts(body, platform)
    for k, v in sorted(c.items(), key=lambda x: -x[1]):
        print("  %-38s %d" % (k, v))
    zero = [k for k, v in c.items() if v == 0]
    if zero and not region:
        # composer 切片里本来就没有消息身份属性，不该报警（region 模式只验表单结构）
        print("⚠️ 这些身份属性命中 0：%s\n"
              "   要么平台又改版了（这正是该录新夹具的理由），"
              "要么切片切错了区域——别直接把选择器改掉，先确认快照里到底有什么。"
              % ", ".join(zero), file=sys.stderr)
    if report_only:
        return 0
    d = os.path.join(FIXTURE_DIR, platform)
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, name)
    with io.open(dst, "w", encoding="utf-8", newline="") as f:
        f.write(body)
    print("已写入 %s" % dst)
    print("下一步：把 `tests/fixtures/%s/meta.json` 的 recorded_at / expected 更新成"
          "上面这些数，然后跑 python -m unittest tests.test_platforms_replay" % platform)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
