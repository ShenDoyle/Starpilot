#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Page 身份组（PROJECT-03-R1 任务书 §7 组 B）：fingerprint 与 generation 分离验收。"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import pageidentity  # noqa: E402
import sp_errors as SP  # noqa: E402

IDENT1 = {"url": "data:text/html,x", "title": "T", "marker": "GEN-1",
          "timeOrigin": 11111.0}


class TestPageIdentity(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="sp-pageid-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.sp = os.path.join(self.d, "pages.json")
        self.m = pageidentity.PageIdentityManager(self.sp)

    def _bind(self, ident=IDENT1, ex="ex-1", fencing=7, key="ts32|p1",
              reason="bind"):
        return self.m.bind(key, ident, ex, fencing, reason)

    # ---- B1：首次绑定 ----
    def test_first_bind_generation_one(self):
        b = self._bind()
        self.assertEqual(b["generation"], 1)
        self.assertEqual(b["page_generation"], "pg-1")
        self.assertTrue(b["fingerprint"].startswith("pf-"))

    # ---- B2：相同 label 不同页面（marker/timeOrigin 变）----
    def test_same_key_different_page_new_generation(self):
        b1 = self._bind()
        ident2 = dict(IDENT1, marker="GEN-2", timeOrigin=22222.0)
        b2 = self.m.bind("ts32|p1", ident2, "ex-1", 7, "navigate")
        self.assertEqual(b2["generation"], 2)
        self.assertNotEqual(b2["fingerprint"], b1["fingerprint"],
                            "页面事实变了指纹必须变")
        # 旧 generation 立即失效
        v = self.m.verify("ts32|p1", IDENT1, "pg-1", "ex-1", 7)
        self.assertFalse(v["ok"])
        self.assertEqual(v["code"], SP.SP_GATE_010)

    # ---- B3：页面导航（URL 变）----
    def test_navigation_invalidates_old(self):
        self._bind()
        ident2 = dict(IDENT1, url="data:text/html,y")
        self.m.bind("ts32|p1", ident2, "ex-1", 7, "navigate")
        v = self.m.verify("ts32|p1", IDENT1, "pg-1", "ex-1", 7)
        self.assertEqual(v["code"], SP.SP_GATE_010)

    # ---- B4：相同 fingerprint 重新绑定 → 仍推进代次（终审补充 A 核心）----
    def test_same_fingerprint_rebind_mints_new_generation(self):
        self._bind()
        b2 = self._bind(reason="rebind_after_invalidate")
        self.assertEqual(b2["generation"], 2)
        self.assertEqual(b2["fingerprint"], self.m.status("ts32|p1")["fingerprint"])
        self.assertTrue(b2["same_fingerprint_as_prev"],
                        "指纹相同但代次必须推进")
        # 旧 pg-1 不得复活
        v = self.m.verify("ts32|p1", IDENT1, "pg-1", "ex-1", 7)
        self.assertFalse(v["ok"])

    # ---- B5：进程更换（executor 变）→ 旧执行者核验被拒 ----
    def test_executor_change_requires_rebind(self):
        self._bind(ex="ex-1", fencing=7)
        v = self.m.verify("ts32|p1", IDENT1, "pg-1", "ex-2-other", 7)
        self.assertFalse(v["ok"])
        self.assertEqual(v["code"], SP.SP_GATE_007)

    # ---- B6：fencing_generation 变化 → 旧核验被拒 ----
    def test_fencing_change_rejected(self):
        self._bind(ex="ex-1", fencing=7)
        v = self.m.verify("ts32|p1", IDENT1, "pg-1", "ex-1", 8)
        self.assertFalse(v["ok"])
        self.assertEqual(v["code"], SP.SP_GATE_007)

    # ---- B7：observation 失效语义：invalidate 后 verify 拒绝，rebind 后旧代次不复用 ----
    def test_invalidate_then_rebind_never_reuses(self):
        self._bind()
        inv = self.m.invalidate("ts32|p1", "identity_changed")
        self.assertTrue(inv["ok"])
        st = self.m.status("ts32|p1")
        self.assertFalse(st["bound"])
        b = self._bind(reason="rebind_after_invalidate")
        self.assertEqual(b["generation"], 2)
        v = self.m.verify("ts32|p1", IDENT1, "pg-1", "ex-1", 7)
        self.assertFalse(v["ok"])

    # ---- 持久化跨实例 ----
    def test_persistence_across_instances(self):
        self._bind()
        m2 = pageidentity.PageIdentityManager(self.sp)
        st = m2.status("ts32|p1")
        self.assertEqual(st["generation"], 1)
        v = m2.verify("ts32|p1", IDENT1, "pg-1", "ex-1", 7)
        self.assertTrue(v["ok"])

    # ---- fingerprint 不含 label；规范化防参数顺序绕过 ----
    def test_fingerprint_canonical_stable(self):
        a = pageidentity.fingerprint_of({"url": " u ", "title": "T", "marker": "m",
                                         "timeOrigin": 1})
        b = pageidentity.fingerprint_of({"title": "T", "marker": "m",
                                         "timeOrigin": 1, "url": "u"})
        self.assertEqual(a, b, "键序/首尾空白不得绕过指纹")


if __name__ == "__main__":
    unittest.main()
