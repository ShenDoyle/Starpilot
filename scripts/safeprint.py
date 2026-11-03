# -*- coding: utf-8 -*-
"""safeprint —— 打印永不炸（Windows 控制台健壮性）。

## 为什么会炸

星舵所有 CLI 都往 stdout 打 `json.dumps(..., ensure_ascii=False)` 的中文报告。
在 zh-CN 的 Windows 上，控制台默认编码是 **cp936(GBK)**：只要输出里出现一个
GBK 编码不了的字符——U+FFFD（读文件时 `errors="replace"` 的产物）、U+2194、
emoji、平台原文里的生僻符号——`print` 就抛 `UnicodeEncodeError`。

后果不是「不好看」，是**判定被吞**：`orchestrator.py` 的需求卡片闸门在
`print` 那一行崩掉，上层拿到的是 GATE_CRASH，而不是它本来要报的
NO_CONFIRMATION / CLARIFIED_EMPTY。也就是说：**闸门想说的实话被编码问题
顶掉了**——这正是「静默成功是最危险的失败模式」的镜像面（响亮失败反而失败在
响亮的路上）。2026-11-03 在 cp936 控制台上实测：592 条单测里 3 条红，
同一批在 PYTHONUTF8=1 下全绿，差异只在控制台编码。

## 本模块的做法（两条都要，一条都不能少）

1. `harden_streams()`：把 stdout/stderr 的 `errors` 改成 `backslashreplace`。
   **只改错误处理，不改编码**——把 GBK 控制台强行换成 UTF-8 会让用户在
   控制台里看到乱码，那是更大的伤害；`\ufffd` 变成字面量 `\\ufffd` 则可读、
   可比对、可 grep。幂等，且任何异常都吞掉（不是终端时本来就 reconfigure 不了）。
2. `jprint()`：输出前自己判一次能不能编进去，编不进去就退回
   `ensure_ascii=True`（纯 ASCII，任何控制台都吃得下）。
   这样即使宿主没走 CLI 入口、或被 `redirect_stdout` 换成 StringIO，也不会崩。

规则：**所有闸门的结论必须落进这两个函数里**，宁可显示成转义，也不许把
判定本身弄丢。
"""

import io
import json
import sys

def _is_tty(st) -> bool:
    try:
        return bool(st.isatty())
    except Exception:
        return False


def harden_streams(streams=None) -> None:
    """加固输出流：**编码问题永远不许吃掉判定，也别把中文变成乱码**。

    分两种下游，处理完全不同（2026-11-03 实测出来的分歧）：

    1. **接的是管道**（agent / harness / bash 抓输出，`isatty()` 为假）：
       zh-CN Windows 上 Python 会按 cp936 建 stdout，而 Qoder / WorkBuddy / CI
       这类宿主按 UTF-8 解码 → 中文全变乱码。星舵的输出是给 agent 读的，
       读不懂就等于没说。此时**改编码为 UTF-8**。
    2. **接的是真控制台**（人眼看）：保持本机代码页，中文才显示得出来；
       只把 `errors` 改成 `backslashreplace`——编不进去的个别字符转义，
       而不是整条命令抛 UnicodeEncodeError。

    幂等靠**读流的当前状态**判断，不靠「记住哪些对象处理过」：
    第一版用 `id(st)` 去重，而 CPython 的 id 会在对象回收后被新对象复用 ——
    全量跑单测时另一条流复用了旧 id，直接被跳过加固（test_safeprint 在
    单跑时绿、整套跑时红就是这个原因）。看状态不会说谎，也不会误伤新对象。
    """
    for st in (streams if streams is not None else (sys.stdout, sys.stderr)):
        try:
            if st is None or not hasattr(st, "reconfigure"):
                continue
            enc = (getattr(st, "encoding", None) or "").lower().replace("-", "")
            errs = (getattr(st, "errors", None) or "").lower().replace("-", "")
            kw = {}
            if not _is_tty(st) and enc not in ("utf8", ""):
                kw["encoding"] = "utf-8"
            if errs != "backslashreplace":
                kw["errors"] = "backslashreplace"
            if not kw:
                continue                 # 已经加固过了
            st.reconfigure(**kw)
        except Exception:
            # 不是真实控制台（StringIO、被宿主换掉的流）→ 本来就不会炸
            pass


def _can_encode(text: str, st) -> bool:
    enc = getattr(st, "encoding", None)
    if not enc:
        return True          # StringIO 之类：什么字符都收
    try:
        text.encode(enc, "strict")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def sres(text: str, st=None) -> str:
    """把可能要炸的文本预先降级：能原样编就原样，不能就**只转义编不进去的那几个字符**。

    注意 round-trip 的方向：编和 decode 必须用**同一个编码**。
    拿 `decode("ascii")` 收尾会把中文一起毁成 `?` —— 那是「为了不崩而说谎」，
    比崩了更糟（判定还在，内容已经坏了）。
    """
    st = st if st is not None else sys.stdout
    if _can_encode(text, st):
        return text
    enc = getattr(st, "encoding", None) or "utf-8"
    return text.encode(enc, "backslashreplace").decode(enc, "replace")


def jprint(obj, stream=None, indent=2, sort_keys=False, ensure_ascii=False,
           **kw) -> bool:
    """打印一段 JSON，返回是否原样输出（False = 已降级成 ASCII 转义）。

    永不抛 UnicodeEncodeError：降级顺序是 原文 → 转义 → repr。
    """
    st = stream if stream is not None else sys.stdout
    try:
        text = json.dumps(obj, ensure_ascii=ensure_ascii, indent=indent,
                          sort_keys=sort_keys, **kw)
    except Exception as e:                      # 不可序列化对象：不许拖垮闸门
        text = json.dumps({"ok": False, "unserializable": repr(e),
                           "partial_keys": list(obj)[:20]
                           if isinstance(obj, dict) else []},
                          ensure_ascii=True, indent=indent)
    # 降级阶梯（每一级都保住判定本身，只牺牲可读性，且按代价从小到大）：
    #   ① 原文 → ② 只转义编不进去的字符（中文留着）→
    #   ③ 整段 ensure_ascii=True（全变 \uXXXX）→ ④ repr 兜底
    if not _can_encode(text, st):
        esc = sres(text, st)
        if _can_encode(esc, st):
            text = esc
        elif not ensure_ascii:
            try:
                text = json.dumps(obj, ensure_ascii=True, indent=indent,
                                  sort_keys=sort_keys, **kw)
            except Exception:
                text = repr(obj)
    _write(st, text)
    return True


def _write(st, text: str) -> None:
    """写一行，永不因编码抛；实在写不出去时把内容降级到底再写。"""
    try:
        st.write(text + "\n")
        return
    except UnicodeEncodeError:
        pass
    except Exception:
        return
    enc = getattr(st, "encoding", None) or "utf-8"
    try:
        buf = getattr(st, "buffer", None)
        if isinstance(buf, (io.BufferedWriter, io.BufferedRandom)):
            # 直接压字节到二进制缓冲，绕过文本层的编码协商
            buf.write((text + "\n").encode(enc, "backslashreplace"))
            buf.flush()
            return
    except Exception:
        pass
    try:
        st.write(text.encode(enc, "backslashreplace").decode(enc, "replace") + "\n")
    except Exception:
        try:
            st.write(text.encode("ascii", "backslashreplace").decode("ascii") + "\n")
        except Exception:
            pass


def eprint(text: str, stream=None) -> None:
    """往 stderr 写一行实话，同样不许编码问题把话说没了。"""
    _write(stream if stream is not None else sys.stderr, sres(text, stream))


def utf8_child_env(base=None) -> dict:
    """给「我们自己拉起的子进程」一套不会因控制台是 GBK 就崩的环境。

    为什么必要：`subprocess(..., encoding="utf-8")` 只决定**父进程怎么解码**，
    管不到**子进程怎么编码**。子进程的 sys.stdout 仍按本机首选编码（zh-CN Windows
    = cp936）创建，于是它打中文时自己先抛 UnicodeEncodeError：
    父进程拿到半截 stdout + 一段 traceback，`json.loads` 失败，
    闸门只能报 GATE_CRASH——真正的判定死在子进程里（实测 2026-11-03，
    requirement_gate 卡片闸门）。`PYTHONIOENCODING` 是唯一能同时管住
    子进程 stdout/stderr/stdin 的开关。
    """
    import os
    env = dict(base if base is not None else os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8:backslashreplace")
    env.setdefault("PYTHONUTF8", "1")
    return env
