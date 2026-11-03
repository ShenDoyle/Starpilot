# -*- coding: utf-8 -*-
"""safeprint：控制台编码健壮性。

回归锚点（2026-11-03 实测）：zh-CN Windows 控制台默认 cp936(GBK)。星舵所有 CLI
往 stdout 打中文 JSON，遇到 GBK 编不进去的字符（U+FFFD、↔、emoji）就抛
UnicodeEncodeError。当时 `test_orchestrator_gaps.TestCardGate` 3 条在 cp936 下红、
在 PYTHONUTF8=1 下绿——同一个仓库两种结论，这类「换个 agent / 换台机器结论就变」
的抖动最贵。

本文件的断言分两类：
  · **不炸**：任何字符都要把判定说出来（转义可以，丢话不行）。
  · **不失真**：接管道（= agent 在读）时必须输出 UTF-8，否则宿主按 UTF-8 解码
    看到的是乱码，等于没说；接真控制台（= 人在看）时保持本机代码页，中文才显示。
"""
import io
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import safeprint  # noqa: E402


class _FakeStream(object):
    """可控 isatty / encoding / reconfigure 的假流，用来验加固策略。

    刻意**不**继承 io.StringIO：`io._TextIOBase.encoding` 是只读描述符，
    子类里 `self.encoding = ...` 会 AttributeError（本文件的第 5 条红测就是这么来的）。
    """

    def __init__(self, encoding="gbk", tty=False):
        self.encoding = encoding
        self._tty = tty
        self.reconfigured = {}
        self.buf = []

    def isatty(self):
        return self._tty

    def reconfigure(self, **kw):
        self.reconfigured.update(kw)

    def write(self, s):
        self.buf.append(s)
        return len(s)

    def flush(self):
        pass

    def getvalue(self):
        return "".join(self.buf)


class TestHardenPolicy(unittest.TestCase):
    def test_pipe_switches_to_utf8(self):
        st = _FakeStream(encoding="gbk", tty=False)
        safeprint.harden_streams([st])
        self.assertEqual(st.reconfigured.get("encoding"), "utf-8")
        self.assertEqual(st.reconfigured.get("errors"), "backslashreplace")

    def test_real_console_keeps_codepage(self):
        st = _FakeStream(encoding="gbk", tty=True)
        safeprint.harden_streams([st])
        self.assertNotIn("encoding", st.reconfigured,
                         "终端里改编码会让人看到乱码")
        self.assertEqual(st.reconfigured.get("errors"), "backslashreplace")

    def test_already_utf8_pipe_untouched(self):
        st = _FakeStream(encoding="utf-8", tty=False)
        safeprint.harden_streams([st])
        self.assertNotIn("encoding", st.reconfigured)

    def test_idempotent(self):
        st = _FakeStream(encoding="gbk", tty=False)
        safeprint.harden_streams([st])
        first = dict(st.reconfigured)
        st.reconfigured.clear()
        safeprint.harden_streams([st])
        self.assertEqual(st.reconfigured, {}, "第二次不该再动同一条流")
        self.assertEqual(first.get("encoding"), "utf-8")

    def test_plain_stringio_survives(self):
        """没有 reconfigure / 没有 encoding 的流（被 redirect_stdout 换掉的）：
        不许抛，且中文照打。"""
        st = io.StringIO()
        safeprint.harden_streams([st])
        safeprint.jprint({"判定": "NO_CONFIRMATION", "脏字符": "\ufffd"},
                         stream=st, indent=None)
        out = json.loads(st.getvalue())
        self.assertEqual(out["判定"], "NO_CONFIRMATION")


class TestJprintNeverLosesTheVerdict(unittest.TestCase):
    def test_strict_gbk_stream_accepts_escaped_output(self):
        """往 errors=strict 的 GBK 流打 U+FFFD：必须写成转义而不是抛。"""
        raw_r, raw_w = os.pipe()
        w = io.open(raw_w, "w", encoding="gbk", errors="strict", newline="")
        r = None
        try:
            # strict 流 + 不硬换编码：模拟「编码不可协商」的宿主
            safeprint.jprint({"verdict": "NO_CONFIRMATION", "detail": "违规 \ufffd ↔"},
                             stream=w, indent=None)
            w.flush()
            w.close()
            r = io.open(raw_r, "r", encoding="gbk", errors="replace")
            got = r.read()
        except UnicodeEncodeError as e:            # pragma: no cover - 断言用
            self.fail("jprint 把判定弄丢了：%s" % e)
        finally:
            for f in (w, r):
                if f is not None:
                    try:
                        f.close()          # 文件对象持有 fd，不能再 os.close（双关会 EBADF）
                    except OSError:
                        pass
        self.assertIn("NO_CONFIRMATION", got)
        self.assertIn("违规", got, "只该转义编不进去的字符，不许把中文一起毁掉")

    def test_unserializable_object_degrades_to_report(self):
        st = io.StringIO()
        safeprint.jprint({"ok": False, "obj": object()}, stream=st, indent=None)
        out = json.loads(st.getvalue())
        self.assertFalse(out["ok"])
        self.assertIn("unserializable", out)


class TestChildEnv(unittest.TestCase):
    def test_sets_io_encoding_and_keeps_other_keys(self):
        env = safeprint.utf8_child_env({"PATH": "x"})
        self.assertEqual(env["PATH"], "x")
        self.assertIn("utf-8", env["PYTHONIOENCODING"])
        self.assertEqual(env["PYTHONUTF8"], "1")

    def test_respects_explicit_user_override(self):
        env = safeprint.utf8_child_env({"PYTHONIOENCODING": "cp1252"})
        self.assertEqual(env["PYTHONIOENCODING"], "cp1252")

    def test_real_child_would_crash_without_hardening(self):
        """对照组：证明这层加固是必要的，不是装饰。

        卡片闸门 GATE_CRASH 的真实成因就在这里——子进程按 GBK 建 stdout，
        打 U+FFFD 时它自己先崩，父进程只拿到半截 stdout，`json.loads` 失败。
        """
        code = ("import json;"
                "print(json.dumps({'ok': False, 'detail': "
                "chr(0xfffd) + '需求未定型'}, ensure_ascii=False))")
        base = {k: v for k, v in os.environ.items()
                if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}

        bad = dict(base)
        bad["PYTHONIOENCODING"] = "gbk:strict"
        p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, encoding="utf-8",
                           errors="backslashreplace", env=bad, timeout=60)
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn("需求未定型", p.stdout)

        good = dict(base)
        good.update(safeprint.utf8_child_env({}))
        p2 = subprocess.run([sys.executable, "-c", code], capture_output=True,
                            text=True, encoding="utf-8",
                            errors="backslashreplace", env=good, timeout=60)
        self.assertEqual(p2.returncode, 0, p2.stderr[-300:])
        self.assertIn("需求未定型", json.loads(p2.stdout)["detail"])


if __name__ == "__main__":
    unittest.main()
