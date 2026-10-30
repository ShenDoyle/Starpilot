"""授权范围（grant）与越界风险分级。

用户裁定（2026-10-30）：
  · Q9 一次授权覆盖整个任务 —— 但范围、条数上限、失效时间必须写在明面上；
  · Q11 越界请求先判安全性：安全无害就通过，风险大就换方案，别让整个任务停下来。

这两条合起来就是「无人值守能跑下去」的两个前提：不反复问、也不乱来。
"""

import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import risk  # noqa: E402
import write_gate as wg  # noqa: E402

URL = "https://www.kimi.com/chat/1a185c66-1b62-8384-8000-09e09cb2a518"
SHA = wg.sha16(URL)


class TestUrlDriftRisk(unittest.TestCase):
    """地址漂移三级：换参数 ≠ 换对话 ≠ 跨站。"""

    def test_query_only_is_low_and_allowed(self):
        r = risk.classify_url_drift(URL, URL + "?chat_enter_method=home")
        self.assertEqual(r["level"], "low")
        self.assertTrue(r["allow"], "只是查询参数变了，对话本体没变，该放行")
        self.assertIn("放行", r["human"])

    def test_trailing_slash_is_low(self):
        r = risk.classify_url_drift(URL + "/", URL)
        self.assertTrue(r["allow"])

    def test_other_conversation_is_high_and_gives_alternative(self):
        other = "https://www.kimi.com/chat/ffffffff-0000-0000-0000-000000000000"
        r = risk.classify_url_drift(URL, other)
        self.assertEqual(r["level"], "high")
        self.assertFalse(r["allow"])
        self.assertIn("bind", r["alternative"], "高风险必须给出换方案，不能只说不行")

    def test_cross_origin_is_high(self):
        r = risk.classify_url_drift(URL, "https://chatgpt.com/c/abc12345")
        self.assertEqual(r["level"], "high")
        self.assertIn("跨站", r["human"])          # 人话里必须说清是什么事
        self.assertEqual(r["reason"], "url_drift_cross_origin")  # 机器码用于回溯


class TestOtherRisks(unittest.TestCase):
    def test_every_block_kind_has_alternative(self):
        for kind in ("platform_unverified", "write_method", "guard", "duplicate",
                     "prompt_drift", "platform_mismatch"):
            r = risk.classify(kind)
            self.assertTrue(r["alternative"], "%s 缺替代方案 = 把决策丢回给 agent" % kind)
            self.assertFalse(r["allow"])
            self.assertIn("human", r)

    def test_unverified_platform_is_medium_not_high(self):
        # 未实测平台换个已验证的就能继续，属于「换方案」而不是「必须人工」
        self.assertEqual(risk.classify("platform_unverified")["level"], "medium")


class TestBlockedCarriesRisk(unittest.TestCase):
    def test_blocked_output_has_next_step(self):
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            rc = wg.blocked("WRITE_BLOCKED_GUARD", "有弹窗",
                            risk=risk.classify("guard"))
        finally:
            sys.stdout = old
        out = json.loads(buf.getvalue())
        self.assertEqual(rc, 2)
        self.assertEqual(out["risk"]["level"], "medium")
        self.assertTrue(out["next_step"])
        self.assertIn("probe", out["next_step"])


class TestGrant(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="sp-grant-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.run_id = "20261030T000000-grant"
        self.run_dir = os.path.join(self.root, "runs", self.run_id)
        os.makedirs(os.path.join(self.run_dir, "writes"))
        self.ap = "ap-abc-def"

    def _dry(self, stage="s1", platform="kimi"):
        return {
            "approval_id": self.ap,
            "target_url": URL,
            "target_url_sha256": SHA,
            "prompt_sha256": wg.sha16("hello"),
            "platform": platform,
            "stage_id": stage,
        }

    def _approve(self, **kw):
        with open(os.path.join(self.run_dir, "writes", "%s.dry.json" % self.ap), "w",
                  encoding="utf-8") as f:
            json.dump(self._dry(kw.pop("stage", "s1"), kw.pop("platform", "kimi")), f)
        a = SimpleNamespace(workspace=self.root, run=self.run_id, approval=self.ap,
                            ttl=kw.pop("ttl", 3600), by="human", scope="single",
                            max_writes=0, stage="")
        for k, v in kw.items():
            setattr(a, k, v)
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            rc = wg.cmd_approve(a)
        finally:
            sys.stdout = old
        return rc, json.loads(buf.getvalue())

    # ---- single：不产生授权单，只对一条生效 ----

    def test_single_scope_creates_no_grant(self):
        rc, out = self._approve()
        self.assertEqual(rc, 0)
        self.assertEqual(out["scope"], "single")
        self.assertEqual(out["max_writes"], 1)
        self.assertFalse(out["grant_file"], "single 不该有授权单")
        self.assertTrue(os.path.exists(
            os.path.join(self.run_dir, "writes", "%s.approval.json" % self.ap)))

    def test_single_scope_expire_time_is_human_readable(self):
        _, out = self._approve(ttl=600)
        self.assertRegex(out["expire_time"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    # ---- task：一次授权覆盖整个任务（条数 + 时间双兜底）----

    def test_task_scope_creates_grant_with_cap_and_expire(self):
        rc, out = self._approve(scope="task")
        self.assertEqual(rc, 0)
        self.assertEqual(out["scope"], "task")
        self.assertEqual(out["max_writes"], wg.DEFAULT_MAX_WRITES["task"])
        self.assertIn("不再逐条问你", out["what_this_allows"])
        gpath = os.path.join(self.run_dir, out["grant_file"])
        with open(gpath, encoding="utf-8") as f:
            g = json.load(f)
        self.assertEqual(g["used"], 0)
        self.assertEqual(g["target_url_sha256"], SHA)
        self.assertTrue(g["expire_time"])

    def test_task_grant_covers_a_new_message(self):
        # 新的一条消息（新 approval_id、新 prompt hash）也能靠授权单放行
        self._approve(scope="task")
        g, path, why = wg.find_grant(self.run_dir, SHA, "kimi", "s1")
        self.assertIsNotNone(g, why)
        self.assertEqual(why, "")
        self.assertTrue(path)

    def test_grant_rejects_other_conversation(self):
        self._approve(scope="task")
        g, _, why = wg.find_grant(self.run_dir, "deadbeefdeadbeef", "kimi", "s1")
        self.assertIsNone(g)
        self.assertIn("不是这条对话", why)

    def test_grant_rejects_expired(self):
        self._approve(scope="task", ttl=60)
        g, _, why = wg.find_grant(self.run_dir, SHA, "kimi", "s1",
                                  now=time.time() + 120)
        self.assertIsNone(g)
        self.assertIn("已过期", why)

    def test_grant_stage_is_scoped(self):
        self._approve(scope="stage", stage="s1")
        g, _, why = wg.find_grant(self.run_dir, SHA, "kimi", "s2")
        self.assertIsNone(g, "stage 授权不该跨阶段生效")
        self.assertIn("只覆盖阶段", why)
        g2, _, _ = wg.find_grant(self.run_dir, SHA, "kimi", "s1")
        self.assertIsNotNone(g2)

    def test_grant_cap_is_enforced(self):
        self._approve(scope="count", max_writes=2)
        g, path, _ = wg.find_grant(self.run_dir, SHA, "kimi", "s1")
        self.assertIsNotNone(g)
        wg.consume_grant(path)
        wg.consume_grant(path)
        g2, _, why = wg.find_grant(self.run_dir, SHA, "kimi", "s1")
        self.assertIsNone(g2)
        self.assertIn("额度已用完", why)

    def test_consume_increments(self):
        self._approve(scope="task")
        _, path, _ = wg.find_grant(self.run_dir, SHA, "kimi", "s1")
        g = wg.consume_grant(path)
        self.assertEqual(g["used"], 1)
        self.assertTrue(g.get("last_used_at"))


if __name__ == "__main__":
    unittest.main()
