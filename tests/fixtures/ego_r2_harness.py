#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ego_r2_harness.py —— PROJECT-03-R2 真实 Ego 测试夹具。

- Localhost 测试页：Python http.server 127.0.0.1 随机端口，页面含
  #m 标记 / 可安全交互的输入与按钮（无任何业务副作用）。
- 一次性隔离 TaskSpace：唯一名字，绝不触碰用户空间（space 0 主、
  用户会话空间）；handOff 类用例专门建空间，不自主 reclaim。
"""
from __future__ import annotations

import http.server
import json
import os
import socket
import sys
import threading
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import ego_backend as eb  # noqa: E402

TEST_PAGE_TMPL = """<!doctype html><html><head><title>R2TEST-{tag}</title></head>
<body><h1 id="m">GEN-{gen}</h1><p>isolated ego r2 test page</p>
<input id="inp" value="{input_val}" oninput="document.getElementById('mirror').innerText='VAL:' + this.value"/>
<button id="btn" onclick="bump()">{button_text}</button>
<div id="out">{out_val}</div>
<div id="mirror">{mirror_val}</div>
<script>
let n = {gen};
function bump() {{ n += 1; document.getElementById('out').innerText = 'CLICKED-' + n; }}
</script></body></html>"""


class TestPageServer:
    """极简 localhost 页面服务：GET → 测试页。"""

    def __init__(self):
        self.state = {"gen": 1, "input_val": "", "button_text": "BUMP",
                      "out_val": "", "mirror_val": ""}
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = TEST_PAGE_TMPL.format(tag=outer.tag, **outer.state).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.tag = uuid.uuid4().hex[:8]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d/" % self.port

    def bump_gen(self):
        self.state["gen"] += 1

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class EgoSpace:
    """一次性隔离 TaskSpace 包装：唯一名 + 真实 ownership probe。"""

    def __init__(self, label_prefix="r2test"):
        r = eb.create_space("starpilot-%s-%s" % (label_prefix, uuid.uuid4().hex[:8]))
        if not r["ok"]:
            raise RuntimeError("create_space failed: %s" % r)
        self.space_id = r["data"]["taskspace_id"]
        self.ownership = r["data"]["ownership"]
        self.name = r["data"]["name"]
        self.finished = False
        self.handed_off = False

    def finish_disposable(self, keep=None):
        """只允许对一次性空间调用；绝不触碰用户空间。"""
        if self.finished or self.handed_off:
            return
        r = eb.finish_space(self.space_id, keep=keep)
        self.finished = True
        self.last_finish = r
        return r

    def reclaim_and_destroy(self):
        """R3-E 资源自清理（2026-11-13 用户资源回收铁律）。

        用例结束后对本夹具**自建**的一次性空间做终局销毁：委托态
        （handOff）或 user 态（keep-finish）一律收回 → 收编关闭全部标签
        （含非托管——preservedUnmanagedCount 会阻止销毁，finish 返回
        closedSpace:false，2026-11-13 实测签名）→ finish({keep:[]})，且要求
        closedSpace=True。绝不触碰用户自己的空间（id 0 主等）。

        ⚠️ 实现边界：reclaim 只存在于本测试夹具层（直调桥接通道），
        **不进后端能力面**——R2 f4 铁律「后端不得实现 reclaim、业务流
        不得自主取回用户控制权」继续有效。
        """
        from ego_backend import _run_bridge  # 仅借桥接通道；不经 CAPABILITIES
        r = _run_bridge("reclaim_space", {"spaceId": self.space_id}, 120)
        self.finished = True
        self.handed_off = False
        self.last_reclaim = r
        if not r.get("ok"):
            raise RuntimeError("reclaim_and_destroy failed: %s" % r)
        if not (r.get("data") or {}).get("closed_space"):
            raise RuntimeError("space not closed (closedSpace!=true): %s"
                               % r.get("data"))
        return r

    def info(self) -> dict:
        return {"space_id": self.space_id, "name": self.name,
                "ownership": self.ownership}


def wait_page_ready(space_id, label, attempts=6):
    """等 data:/localhost 页面就绪（identity readyState）。"""
    for _ in range(attempts):
        r = eb.page_identity(space_id, label)
        if r["ok"]:
            return r["data"]
        time.sleep(1.0)
    raise RuntimeError("page not ready: %s" % r)
