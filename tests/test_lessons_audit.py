#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lessons.md 知识库体检的验收单测（架构评审：知识回流会腐化）。

盯四件事：格式兼容（10 段老条目 + 12 段新条目）、重复检测、
超期 open、平台名拼错。
"""
from __future__ import annotations

import datetime
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import lessons_audit as la  # noqa: E402

OLD = ("- [L-KIMI-SEND-AbC123][2026-01-01][O1][runs/x][kimi][症状甲][][修法][verify][merged]")
NEW = ("- [L-DEEPS-REPLY-Def456][2026-01-02][O5][runs/y][deepseek][症状乙][][修法][verify]"
       "[触发条件][回退方式][open]")
# 13 段（现行）：trigger / rollback / blocking / status
NEW13 = ("- [L-DEEPS-STUCK-Ghi789][2026-01-02][O5][runs/y][deepseek][症状丙][][修法][verify]"
         "[触发条件][回退方式][{blocking}][open]")


class TestLessonsAudit(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="sp-lessons-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.p = os.path.join(self.d, "lessons.md")

    def write(self, *lines):
        with open(self.p, "w", encoding="utf-8") as f:
            f.write("# 教训\n\n## 附录\n\n" + "\n".join(lines) + "\n")

    def test_parses_old_and_new_format(self):
        self.write(OLD, NEW)
        old = la.parse_line(OLD)
        new = la.parse_line(NEW)
        self.assertEqual(old["status"], "merged")
        self.assertEqual(old["trigger"], "")
        self.assertEqual(new["status"], "open")
        self.assertEqual(new["trigger"], "触发条件")
        self.assertEqual(new["rollback"], "回退方式")

    def test_duplicate_symptom_detected(self):
        dup = ("- [L-KIMI-SEND-Zzz999][2026-01-03][O1][runs/z][kimi][症状甲][][修法2][verify][merged]")
        self.write(OLD, dup)
        res = la.audit(self.p)
        self.assertFalse(res["ok"])
        self.assertEqual(len(res["duplicates"]), 1)
        self.assertEqual(res["duplicates"][0]["duplicate_of"], "L-KIMI-SEND-AbC123")

    def test_stale_open_flagged(self):
        self.write(NEW)   # 2026-01-02 的 open
        res = la.audit(self.p, max_age_days=30, today=datetime.date(2026, 6, 1))
        self.assertTrue(res["stale_open"], "open 挂了半年必须被点名")
        self.assertFalse(res["ok"] is False, "stale 只提醒，不判 FAIL")

    def test_unknown_platform_flagged(self):
        bad = ("- [L-DEEPSK-ERR-000001][2026-01-02][O5][runs/y][deepseak][x][][f][v][][ ][merged]")
        self.write(bad)
        res = la.audit(self.p)
        self.assertEqual(res["unknown_platform"], ["L-DEEPSK-ERR-000001"])

    def test_malformed_line_flagged(self):
        self.write("- [L-BROKEN][2026-01-01][O1]")
        res = la.audit(self.p)
        self.assertFalse(res["ok"])
        self.assertEqual(len(res["malformed"]), 1)

    def test_open_entry_reports_age_and_blocking(self):
        """open 不是待办：必须能看出「开了多久」和「阻不阻塞收口」。"""
        self.write(NEW13.format(blocking="true"))
        res = la.audit(self.p, today=datetime.date(2026, 1, 20))
        it = res["open_items"][0]
        self.assertEqual(it["open_since"], "2026-01-02")
        self.assertEqual(it["age_days"], 18)
        self.assertTrue(it["blocking"])
        self.assertEqual(res["blocking_open"], ["L-DEEPS-STUCK-Ghi789"])
        self.assertFalse(res["ok"], "blocking 的 open 未闭合 = FAIL（不许先收口）")
        self.assertEqual(res["verdict"], "FAIL")

    def test_non_blocking_open_only_warns(self):
        self.write(NEW13.format(blocking="false"))
        res = la.audit(self.p, today=datetime.date(2026, 1, 20))
        self.assertTrue(res["ok"], "不阻塞的 open 不该拦住整轮体检")
        self.assertTrue(res["stale_open"], "但超期要报警")
        self.assertEqual(res["verdict"], "WARN")

    def test_default_age_threshold_is_14_days(self):
        self.write(NEW13.format(blocking="false"))
        r13 = la.audit(self.p, today=datetime.date(2026, 1, 15))   # 13 天
        r14 = la.audit(self.p, today=datetime.date(2026, 1, 16))   # 14 天
        r15 = la.audit(self.p, today=datetime.date(2026, 1, 17))   # 15 天
        self.assertFalse(r13["stale_open"])
        self.assertFalse(r14["stale_open"], "14 天整还不算超")
        self.assertTrue(r15["stale_open"], "超过 14 天必须点名")

    def test_neutral_platform_marker_is_not_a_typo(self):
        """平台无关的 lesson（口径/闸门类）写 `—` 不该被当成「平台名拼错」。"""
        neutral = ("- [L-REQUIRE-WORD-000001][2026-01-02][O5][runs/y][—][症状丁][][修法][verify]"
                   "[触发][回退][false][merged]")
        self.write(neutral)
        res = la.audit(self.p)
        self.assertEqual(res["unknown_platform"], [], "中性标记不是拼错")
        self.assertTrue(res["ok"])

    def test_clean_file_passes(self):
        self.write(OLD, NEW)
        res = la.audit(self.p, today=datetime.date(2026, 1, 5))
        self.assertTrue(res["ok"])
        self.assertEqual(res["total"], 2)


if __name__ == "__main__":
    unittest.main()
