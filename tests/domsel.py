#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""domsel.py —— 回放测试用的**迷你选择器引擎**（纯标准库，只为测试服务）。

为什么需要它（架构评审 S5）：验证一个平台选择器以前必须「真人登录 + 真发一条消息」，
改一行 `sel` 的风险极高，而且受硬规则 1 约束，未绑定平台连探测都不许。
**录制一份真实 DOM 快照 + 离线重放**，能让「选择器改坏了」在 `unittest` 阶段就红。

**明确的边界（不要拿它当浏览器）**：
- 只实现本项目选择器用到的子集：`tag`、`.class`、`#id`、`[attr]`、`[attr=v]`、
  `[attr*=v]`、`[attr^=v]`、`:not(...)`、`:has(...)`、逗号分组、后代组合（空格）。
- **不跑 JS**，因此不验交互时序、不验 `innerText`/`value` 的真实取值——
  它验的是「选择器在这份 DOM 上命中谁、命中几个」。
- 快照是录制时刻的真机 DOM；平台改版后重录即可（`tests/fixtures/<platform>/`）。
"""
from __future__ import annotations

import os
from html.parser import HTMLParser

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr"}


class Node(object):
    __slots__ = ("tag", "attrs", "children", "parent", "text")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = (tag or "").lower()
        self.attrs = attrs or {}
        self.children = []
        self.parent = parent
        self.text = ""

    @property
    def classes(self):
        return (self.attrs.get("class") or "").split()

    def __repr__(self):
        cls = ",".join(self.classes[:3])
        return "<%s%s>" % (self.tag, ("." + cls) if cls else "")

    def ancestors(self):
        p = self.parent
        while p is not None:
            yield p
            p = p.parent

    def descendants(self):
        for c in self.children:
            yield c
            for d in c.descendants():
                yield d


class _Parser(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.root = Node("#document")
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag.lower() not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)

    def handle_endtag(self, tag):
        t = tag.lower()
        n = self.cur
        while n is not None and n.tag != t:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.text += data


def parse(html: str) -> Node:
    p = _Parser()
    p.feed(html or "")
    p.close()
    return p.root


# ---------------------------------------------------------------- 选择器解析


def _split_top(s: str, sep: str = ","):
    out, buf, depth = [], [], 0
    for ch in s:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == sep and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return [x.strip() for x in out if x.strip()]


def _read_ident(s, i):
    j = i
    while j < len(s) and (s[j].isalnum() or s[j] in "-_\\*"):
        j += 1
    return s[i:j], j


def _read_balanced(s, i, open_ch="(", close_ch=")"):
    depth = 0
    j = i
    while j < len(s):
        if s[j] == open_ch:
            depth += 1
        elif s[j] == close_ch:
            depth -= 1
            if depth == 0:
                return s[i + 1:j], j + 1
        j += 1
    raise ValueError("括号不闭合：%s" % s[i:])


def parse_compound(s: str) -> dict:
    """把 `div.a[b*=c]:not(:has(.d))` 拆成结构化描述。"""
    i = 0
    out = {"tag": None, "classes": [], "id": None, "attrs": [], "nots": [], "has": []}
    while i < len(s):
        c = s[i]
        if c == ".":
            name, i = _read_ident(s, i + 1)
            out["classes"].append(name)
        elif c == "#":
            name, i = _read_ident(s, i + 1)
            out["id"] = name
        elif c == "[":
            body, i = _read_balanced(s, i, "[", "]")
            out["attrs"].append(_parse_attr(body))
        elif c == ":":
            name, i = _read_ident(s, i + 1)
            arg = None
            if i < len(s) and s[i] == "(":
                arg, i = _read_balanced(s, i)
            if name in ("not", "has") and arg:
                out[name + "s" if name == "not" else "has"].append(arg)
            elif name:
                raise ValueError("不支持的伪类 :%s" % name)
        elif c == "*":
            out["tag"] = "*"
            i += 1
        else:
            name, i = _read_ident(s, i)
            if not name:
                raise ValueError("看不懂的选择器片段：%s" % s[i:i + 12])
            out["tag"] = name.lower()
    return out


_OPS = ("*=", "^=", "=")


def _parse_attr(body: str):
    for op in _OPS:
        if op in body:
            k, v = body.split(op, 1)
            return (k.strip().lower(), op, v.strip().strip("\"'"))
    return (body.strip().lower(), None, None)


def _complex(s: str):
    return [parse_compound(p) for p in s.split()]


def _match_compound(node: Node, comp: dict) -> bool:
    if comp["tag"] and comp["tag"] != "*" and node.tag != comp["tag"]:
        return False
    if comp["id"] and node.attrs.get("id") != comp["id"]:
        return False
    for c in comp["classes"]:
        if c not in node.classes:
            return False
    for (k, op, v) in comp["attrs"]:
        if k not in node.attrs:
            return False
        if op == "=" and node.attrs[k] != v:
            return False
        if op == "*=" and v not in node.attrs[k]:
            return False
        if op == "^=" and not node.attrs[k].startswith(v):
            return False
    for inner in comp["nots"]:
        if _match_complex_self(node, _complex(inner)):
            return False
    for inner in comp["has"]:
        seq = _complex(inner)
        if not any(_match_complex_self(d, seq) for d in node.descendants()):
            return False
    return True


def _match_complex_self(node: Node, seq: list) -> bool:
    """整条复杂选择器是否匹配到 node（node 必须是最后一个复合选择器的目标）。"""
    if not seq or not _match_compound(node, seq[-1]):
        return False
    rest = seq[:-1]
    if not rest:
        return True
    # 后代组合：ancestors 里按顺序找一个能吃掉 rest 的
    for anc in node.ancestors():
        if _match_complex_self(anc, rest):
            return True
    return False


def select_all(root: Node, selector: str) -> list:
    groups = [_complex(g) for g in _split_top(selector)]
    out = []
    for node in root.descendants():
        for seq in groups:
            if _match_complex_self(node, seq):
                out.append(node)
                break
    return out


def select(root: Node, selector: str):
    got = select_all(root, selector)
    return got[0] if got else None


def first_hit(root: Node, candidates: list):
    """按候选顺序取第一个命中的（与 platforms.py 的 pick 语义一致）。"""
    for s in candidates or []:
        got = select_all(root, s)
        if got:
            return s, got[0], len(got)
    return None, None, 0


# ---------------------------------------------------------------- 夹具


def fixture_path(platform: str, part: str) -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "fixtures", platform, part)


def load_fixture(platform: str, part: str) -> Node:
    p = fixture_path(platform, part)
    with open(p, encoding="utf-8") as f:
        return parse(f.read())
