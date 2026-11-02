#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""裁定台账单测（2026-11-02 GPT 审计 P0-2 + D03/D04/D05）。

  D03 未获裁定提前执行 → check 必须 False；
  D04 WebAI 仅表达倾向（含「倾向于批准」这种软话）→ 不得视为批准；
  D05 裁定后事实变化 → 原裁定失效，必须重新申请。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))
import decisions as dc  # noqa: E402


class TestClassify(unittest.TestCase):
    def test_d04_soft_wording_is_tentative(self):
        for text in ("我倾向于方案 A。", "我觉得可以用 A", "或许可以按 A 做",
                     "我倾向于批准方案 A", "probably fine", "建议采用 A"):
            self.assertEqual(dc.classify_verdict(text)[0], "tentative", text)

    def test_explicit_approval(self):
        for text in ("批准 A", "按 B 执行", "同意，开始", "approved"):
            self.assertEqual(dc.classify_verdict(text)[0], "approved", text)

    def test_rejection_and_stop(self):
        self.assertEqual(dc.classify_verdict("不批准，改方案")[0], "rejected")
        self.assertEqual(dc.classify_verdict("先停一下，别动")[0], "stopped")

    def test_need_info(self):
        self.assertEqual(dc.classify_verdict("信息不足，需要补充部署环境")[0], "need_info")

    def test_garbage_is_not_approval(self):
        self.assertEqual(dc.classify_verdict("嗯嗯")[0], "tentative")


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ledger = os.path.join(self.tmp, "state", "decisions.json")

    def test_d03_no_record_not_authorized(self):
        ok, why, _ = dc.check(self.ledger, "合并阶段 S2+S3", "h1")
        self.assertFalse(ok)
        self.assertIn("无该动作的裁定记录", why)

    def test_approved_allows_when_facts_same(self):
        dc.record(self.ledger, "合并阶段 S2+S3", "批准合并，按 A", "h1")
        ok, why, _ = dc.check(self.ledger, "合并阶段 S2+S3", "h1")
        self.assertTrue(ok, why)

    def test_d04_tentative_record_not_authorizing(self):
        rec = dc.record(self.ledger, "改方案", "我倾向于方案 A", "h1")
        self.assertEqual(rec["verdict"], "tentative")
        ok, why, _ = dc.check(self.ledger, "改方案", "h1")
        self.assertFalse(ok)
        self.assertIn("tentative", why)

    def test_d05_facts_changed_invalidates(self):
        dc.record(self.ledger, "改方案", "批准方案 A", "h1")
        ok, why, _ = dc.check(self.ledger, "改方案", "h2")
        self.assertFalse(ok)
        self.assertIn("失效", why)

    def test_approved_without_facts_hash_not_authorizing(self):
        dc.record(self.ledger, "改方案", "批准方案 A", "")
        ok, why, _ = dc.check(self.ledger, "改方案", "h1")
        self.assertFalse(ok)
        self.assertIn("事实指纹", why)

    def test_latest_verdict_wins(self):
        dc.record(self.ledger, "改方案", "批准方案 A", "h1")
        dc.record(self.ledger, "改方案", "先停，重新讨论", "h1")
        ok, why, _ = dc.check(self.ledger, "改方案", "h1")
        self.assertFalse(ok)
        self.assertIn("stopped", why)


if __name__ == "__main__":
    unittest.main()
