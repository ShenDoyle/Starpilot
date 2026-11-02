#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""契约自洽检查的测试（SKILL §5.5 ⑤）。

两条线：
1. 解析器本身对不对（_cards / _rc_rows / _wa_commands）；
2. 端到端：真实仓库必须 PASS；把副本改坏一处后必须 FAIL（否则这个 lint 是摆设）。
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import contract_lint as cl  # noqa: E402


class TestParsers(unittest.TestCase):
    def test_cards_split_by_heading(self):
        text = ("## 1. CARD-a · 甲\n**前置断言**\n**唯一通路**\n**通过判据**\n**失败分支**\n"
                "## 8. rc 码全表\n| 10 | 风控 |\n")
        cards = cl._cards(text)
        self.assertIn("CARD-a", cards)
        self.assertEqual(len(cards), 1, "rc 全表不是动作卡，不该被切进来")

    def test_rc_rows_only_picks_numbered_rows(self):
        text = ("| rc | 含义 |\n|---|---|\n| 10 | 风控 |\n| 13 | 改版 |\n"
                "| 处置 | 说明 |\n")
        self.assertEqual(sorted(cl._rc_rows(text)), [10, 13])

    def test_wa_commands_parsed(self):
        cmds = cl._wa_commands(ROOT)
        for c in ("bind", "send", "wait", "read", "triage", "probe", "clear", "doctor"):
            self.assertIn(c, cmds, "wa.py 里应有子命令 %s" % c)


class TestRepoContract(unittest.TestCase):
    def test_real_repo_is_consistent(self):
        res = cl.check(ROOT)
        failed = [r for r in res if not r["ok"]]
        self.assertEqual(failed, [], "契约不自洽：%s" % failed)
        self.assertEqual(len(res), 7, "七项检查都要跑")


class _CopyBase(unittest.TestCase):
    """复制出一份可篡改的仓库副本（只带契约检查用得到的文件）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sp-lint-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.makedirs(os.path.join(self.tmp, "references", "runbooks"))
        shutil.copytree(os.path.join(ROOT, "scripts"),
                        os.path.join(self.tmp, "scripts"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        for rel in ("SKILL.md",
                    os.path.join("references", "execution-contract.md")):
            shutil.copy2(os.path.join(ROOT, rel), os.path.join(self.tmp, rel))
        for f in os.listdir(os.path.join(ROOT, "references", "runbooks")):
            if f.endswith(".md"):
                shutil.copy2(os.path.join(ROOT, "references", "runbooks", f),
                             os.path.join(self.tmp, "references", "runbooks", f))

    def _edit(self, rel, old, new):
        p = os.path.join(self.tmp, rel)
        with io.open(p, encoding="utf-8") as f:
            s = f.read()
        self.assertIn(old, s)
        with io.open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(s.replace(old, new, 1))

    def _failed_names(self):
        return {r["name"] for r in cl.check(self.tmp) if not r["ok"]}


class TestDetectsBreakage(_CopyBase):
    def test_copy_is_clean_first(self):
        self.assertEqual(self._failed_names(), set(), "副本本身必须是干净的基线")

    def test_missing_card_section_is_caught(self):
        self._edit(os.path.join("references", "execution-contract.md"),
                   "**失败分支**", "失败的时候再说")
        self.assertIn("card_sections", self._failed_names())

    def test_missing_rc_row_is_caught(self):
        self._edit(os.path.join("references", "execution-contract.md"),
                   "| 13 | wa triage |", "| x | 改版 |")
        self.assertIn("rc_cover", self._failed_names())

    def test_broken_runbook_link_is_caught(self):
        self._edit("SKILL.md", "references/runbooks/dom-drift.md",
                   "references/runbooks/dom-drift-v2.md")
        self.assertIn("runbook_index", self._failed_names())

    def test_phantom_command_is_caught(self):
        # 硬边界表里塞一个 wa.py 不存在的命令
        self._edit("SKILL.md", "| 状态探测 | `wa.py probe` / `wa.py doctor` |",
                   "| 状态探测 | `wa.py probe` / `wa.py teleport` |")
        self.assertIn("command_exists", self._failed_names())


if __name__ == "__main__":
    unittest.main()


class TestNoRawSessionStart(_CopyBase):
    """禁止裸调开窗：wa.py 之外出现 bsk session start 必须FAIL（2026-11-02）。"""

    def test_offender_fails(self):
        offender = os.path.join(self.tmp, "runtime", "wa")
        os.makedirs(offender, exist_ok=True)
        with open(os.path.join(offender, "rogue.py"), "w", encoding="utf-8") as f:
            f.write('import subprocess\nsubprocess.run(["bsk", "session", "start", "--no-focus"])\n')
        res = {r["name"]: r for r in cl.check(self.tmp)}
        self.assertFalse(res["no_raw_session_start"]["ok"])
        self.assertIn("runtime/wa/rogue.py", res["no_raw_session_start"]["detail"])

    def test_wa_itself_exempt(self):
        res = {r["name"]: r for r in cl.check(self.tmp)}
        self.assertTrue(res["no_raw_session_start"]["ok"])


class TestNoRawTabOps(_CopyBase):
    """禁止裸调开/关 tab：wa.py 之外出现 bsk tab create/close 必须 FAIL
    （2026-11-02 用户指令「只要使用 bsk，必须遵守 tab 规则」）。"""

    def test_tab_create_offender_fails(self):
        offender = os.path.join(self.tmp, "runtime", "wa")
        os.makedirs(offender, exist_ok=True)
        with open(os.path.join(offender, "rogue_tab.py"), "w", encoding="utf-8") as f:
            f.write('import subprocess\nsubprocess.run(["bsk", "tab", "create", "--session", "s"])\n')
        res = {r["name"]: r for r in cl.check(self.tmp)}
        self.assertFalse(res["no_raw_tab_ops"]["ok"])
        self.assertIn("runtime/wa/rogue_tab.py", res["no_raw_tab_ops"]["detail"])

    def test_tab_close_offender_fails_and_wa_exempt(self):
        offender = os.path.join(self.tmp, "runtime", "wa")
        os.makedirs(offender, exist_ok=True)
        with open(os.path.join(offender, "rogue_close.py"), "w", encoding="utf-8") as f:
            f.write("rc, out, err = bsk(['tab', 'close', '--session', sid, tid])\n")
        res = {r["name"]: r for r in cl.check(self.tmp)}
        self.assertFalse(res["no_raw_tab_ops"]["ok"])
        # wa.py 本身是唯一通路，仓库基线必须干净
        clean = {r["name"]: r for r in cl.check(ROOT)}
        self.assertTrue(clean["no_raw_tab_ops"]["ok"])

    def test_tab_select_and_list_not_flagged(self):
        """只拦 create/close（会改标签集合的写操作）；select/list 是只读或聚焦，不拦。"""
        offender = os.path.join(self.tmp, "runtime", "wa")
        os.makedirs(offender, exist_ok=True)
        with open(os.path.join(offender, "ok_tab.py"), "w", encoding="utf-8") as f:
            f.write("bsk(['tab', 'select', '--session', sid, tid])\n"
                    "bsk(['tab', 'list', '--session', sid, '--json'])\n")
        res = {r["name"]: r for r in cl.check(self.tmp)}
        self.assertTrue(res["no_raw_tab_ops"]["ok"])
