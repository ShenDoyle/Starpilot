# -*- coding: utf-8 -*-
"""pending_gate（未清项两道门）+ ORCHESTRATOR_REQUIRED 熔断的回归测试。

背景：2026-10-31 两次审计（Benchyard「欠账蒸发」+ agent 服从性）指向同一病——
「没做的项在汇报里隐身」「绕过编排器直发」。本文件验证机器断言能拦住这两类违例。
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pending_gate  # noqa: E402


def write(tmp, name, text):
    p = os.path.join(tmp, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


PENDING = """# 未清项清单
- [P0][P0-1] 生产重装授权定性 | open | 来源:第6轮裁决
- [P1][P1-2] 文档条件重引 | open | 来源:第6轮裁决
- [P2][P2-3] 已闭环项 | done(第5轮) | 证据:tests
"""


class TestPendingGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_parse(self):
        p = write(self.tmp, "p.md", PENDING)
        items = pending_gate.parse_pending(p)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["priority"], "P0")
        self.assertEqual(items[0]["id"], "P0-1")
        self.assertEqual(items[2]["status"], "done")

    def test_p0_gate_blocks_when_unreferenced(self):
        p = write(self.tmp, "p.md", PENDING)
        ok, reason = pending_gate.check_p0_gate(p, "本轮只修了 L1 探针，其他无")
        self.assertFalse(ok)
        self.assertIn("P0-1", reason)

    def test_p0_gate_passes_when_referenced(self):
        p = write(self.tmp, "p.md", PENDING)
        ok, _ = pending_gate.check_p0_gate(
            p, "- [P0-1] 生产重装授权定性：✅ 已附授权链证据 docs/auth.md\n其余正常")
        self.assertTrue(ok)

    def test_p0_gate_deferred_counts_as_closed(self):
        p = write(self.tmp, "p.md",
                  PENDING.replace("生产重装授权定性 | open",
                                  "生产重装授权定性 | deferred(用户裁定顺延)"))
        ok, _ = pending_gate.check_p0_gate(p, "本轮做别的")
        self.assertTrue(ok)

    def test_receipt_gate_requires_every_open_item(self):
        p = write(self.tmp, "p.md", PENDING)
        ok, _, missing = pending_gate.check_receipt(
            p, "- [P1-2] 文档重引：⚠️ 部分完成，差条件4")
        self.assertFalse(ok)
        self.assertEqual(missing, ["P0-1"])

    def test_receipt_gate_allows_honest_fail(self):
        """如实报 ❌/未做 合格——门只要求「出现」，不要求「成功」。"""
        p = write(self.tmp, "p.md", PENDING)
        ok, _, missing = pending_gate.check_receipt(
            p, "- [P0-1] ❌ 未做：授权链证据在用户手里，需人提供\n"
               "- [P1-2] ⚠️ 进行中")
        self.assertTrue(ok)
        self.assertEqual(missing, [])

    def test_no_pending_list_gate_disabled(self):
        ok, reason = pending_gate.check_p0_gate(
            os.path.join(self.tmp, "nonexistent.md"), "anything")
        self.assertTrue(ok)
        self.assertEqual(reason, "no_pending_list")

    def test_pending_path_for_order(self):
        root = self.tmp
        stage = os.path.join(root, "stage", "s1")
        os.makedirs(stage)
        self.assertEqual(
            pending_gate.pending_path_for(root, "s1", "r1"),
            os.path.join(stage, "pending.md"))
        self.assertEqual(
            pending_gate.pending_path_for(root, "", ""),
            os.path.join(root, "pending.md"))


class TestOrchestratorFuse(unittest.TestCase):
    """ORCHESTRATOR_REQUIRED：state/current 存在时 cmd_send 必须拒绝。"""

    def setUp(self):
        import wa
        self.wa = wa
        self.tmp = tempfile.mkdtemp()
        self.oldcwd = os.getcwd()
        os.chdir(self.tmp)
        self._env = os.environ.copy()
        os.environ["BSK_BIN"] = self.wa.BSK  # 版本首查会跑真实 bsk --version
        # 锁目录隔离（2026-11-02）：默认锁目录是全局的 ~/.starpilot/locks，
        # 本机只要有并行真实任务占用同平台（如 deepseek），就会以 rc=9（平台锁）
        # 抢在 rc=7（run 熔断）之前拒绝，测试随环境漂红。指向临时目录即可。
        os.environ["STARPILOT_LOCK_DIR"] = os.path.join(self.tmp, "locks")

    def tearDown(self):
        os.chdir(self.oldcwd)
        os.environ.clear()
        os.environ.update(self._env)

    def _prep(self):
        st_path = self.wa.state_path(None)
        st = self.wa.load_state(st_path)
        st["url"] = "https://chat.deepseek.com/a/chat/s/12345678-1234-1234-1234-123456789abc"
        st["platform"] = "deepseek"
        self.wa.save_state(st_path, st)
        os.makedirs(os.path.join(self.tmp, "state"), exist_ok=True)
        with open(os.path.join(self.tmp, "state", "current"), "w", encoding="utf-8") as f:
            f.write("runs/demo-run")

    def test_send_refused_when_run_active(self):
        self._prep()
        rc = self.wa.main(["send", "--text", "hi"])
        self.assertEqual(rc, 7)

    def test_send_allowed_outside_loop(self):
        st_path = self.wa.state_path(None)
        st = self.wa.load_state(st_path)
        st["url"] = ""
        self.wa.save_state(st_path, st)   # 未绑定：应停在「尚未绑定」(rc=2)，
        rc = self.wa.main(["send", "--text", "hi"])  # 而不是熔断 rc=7
        self.assertEqual(rc, 2)

    def test_escape_hatch_single_consumption_and_ledger(self):
        """判据 A①：设标记后第一次 send 放行，第二次 rc=7；判据 A②：pending.md 出现 P0-ESC。"""
        self._prep()
        st_path = self.wa.state_path(None)
        st = self.wa.load_state(st_path)
        st["url"] = ""      # 放行后停在「尚未绑定」rc=2，避免触碰真实浏览器
        self.wa.save_state(st_path, st)
        os.environ["STARPILOT_ALLOW_DIRECT_SEND"] = "调试：验证逃生门"
        rc1 = self.wa.main(["send", "--text", "hi"])
        self.assertEqual(rc1, 2)
        # 单次消费：env 已被清除
        self.assertNotIn("STARPILOT_ALLOW_DIRECT_SEND", os.environ)
        # 落账：pending.md 出现 P0-ESC 条目
        led = os.path.join(self.tmp, "pending.md")
        self.assertTrue(os.path.exists(led))
        content = open(led, encoding="utf-8").read()
        self.assertIn("P0-ESC-", content)
        self.assertIn("escape_hatch", content)
        # 第二次：标记已被消费，回到熔断 rc=7
        rc2 = self.wa.main(["send", "--text", "hi"])
        self.assertEqual(rc2, 7)

    def test_escape_hatch_empty_reason_refused(self):
        """理由必填：env 为空值视为未声明，拒绝。"""
        self._prep()
        os.environ["STARPILOT_ALLOW_DIRECT_SEND"] = ""
        rc = self.wa.main(["send", "--text", "hi"])
        self.assertEqual(rc, 7)

    def test_receipt_must_report_escape_entry(self):
        """判据 A③：回执无 P0-ESC 条目 → 退回；有 → 通过。"""
        p = write(self.tmp, "p.md",
                  PENDING + "- [P0][P0-ESC-ab12ef34] direct_send 绕过编排器直发 | "
                  "deferred(违例：2026-10-31 reason=调试) | 来源:escape_hatch\n")
        ok, _, missing = pending_gate.check_receipt(p, "- [P1-2] ✅ 完成")
        self.assertFalse(ok)
        self.assertIn("P0-ESC-ab12ef34", missing)
        ok2, _, missing2 = pending_gate.check_receipt(
            p, "- [P0-1] ❌ 未做：等人提供授权链\n- [P1-2] ✅ 完成\n"
               "- [P0-ESC-ab12ef34] ⚠️ 违例已报备：本轮直发 1 次")
        self.assertTrue(ok2)
        self.assertEqual(missing2, [])


if __name__ == "__main__":
    unittest.main()


class TestBlockedExemptionAndAdversarial(unittest.TestCase):
    """0.1.5 锁定项：BLOCKED 豁免路径 + P0-ESC 对抗验证（DeepSeek 裁定同批交付）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _write(self, text):
        p = os.path.join(self.tmp, "p.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def test_blocked_exempts_p0_gate_but_must_report(self):
        """BLOCKED：P0 门放行，但回执必须带该条（豁免不许隐身）。"""
        p = self._write("- [P0][P0-9] 依赖外部授权 | blocked(等用户提供授权链) | 来源:第6轮\n")
        ok, _ = pending_gate.check_p0_gate(p, "本轮做别的任务")   # 门放行
        self.assertTrue(ok)
        ok2, _, missing = pending_gate.check_receipt(p, "本轮做别的任务")
        self.assertFalse(ok2)                                     # 回执隐身 → 退回
        self.assertEqual(missing, ["P0-9"])
        ok3, _, m3 = pending_gate.check_receipt(
            p, "- [P0-9] ⚠️ BLOCKED：等用户授权链，本轮跳过")
        self.assertTrue(ok3)

    def test_adversarial_stale_receipt_fails(self):
        """对抗：把上一轮回执里的 P0-ESC 行复制到本轮，ID 与新台账不匹配 → 拒绝。"""
        old_ledger = self._write(
            "- [P0][P0-ESC-aaaa1111] direct_send | deferred(违例：第1轮) | 来源:escape_hatch\n")
        old_receipt = "- [P0-ESC-aaaa1111] ⚠️ 违例已报备"
        ok, _, _ = pending_gate.check_receipt(old_ledger, old_receipt)  # 同台账：过
        self.assertTrue(ok)
        new_ledger = self._write(
            "- [P0][P0-ESC-bbbb2222] direct_send | deferred(违例：第2轮) | 来源:escape_hatch\n")
        ok2, _, missing = pending_gate.check_receipt(new_ledger, old_receipt)
        self.assertFalse(ok2)                                     # 旧 ID 在新台账无效
        self.assertIn("P0-ESC-bbbb2222", missing)


class TestTransport(unittest.TestCase):
    """IAB 传输层：op 队列协议 + bsk 参数翻译。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["STARPILOT_IAB_OPS"] = os.path.join(self.tmp, "ops")
        os.makedirs(os.environ["STARPILOT_IAB_OPS"], exist_ok=True)

    def tearDown(self):
        os.environ.pop("STARPILOT_IAB_OPS", None)
        os.environ.pop("STARPILOT_TRANSPORT", None)

    def test_call_roundtrip_with_fake_worker(self):
        import threading
        import transport

        def fake_worker():
            time.sleep(0.3)
            req = transport.pending_reqs(self.tmp)[0]
            op = transport.read_req(os.path.join(transport.ops_dir(self.tmp), req))
            transport.write_res(transport.ops_dir(self.tmp), op["id"], True,
                                data={"echo": op["params"]["js"]})

        threading.Thread(target=fake_worker, daemon=True).start()
        rc, out, err = transport.call("evaluate", {"js": "1+1"}, timeout=10, root=self.tmp)
        self.assertEqual(rc, 0)
        self.assertIn("1+1", out)

    def test_call_timeout_when_no_worker(self):
        import transport
        rc, out, err = transport.call("evaluate", {"js": "x"}, timeout=1.5, root=self.tmp)
        self.assertEqual(rc, 124)
        self.assertIn("worker", err)

    def test_parse_bsk_args(self):
        import transport
        self.assertEqual(transport.parse_bsk_args(["navigate", "https://x"]),
                         ("navigate", {"url": "https://x"}))
        self.assertEqual(transport.parse_bsk_args(["evaluate", "1+1"]),
                         ("evaluate", {"js": "1+1"}))
        a, p = transport.parse_bsk_args(
            ["fill", "--selector", "textarea", "--value", "hi", "--json"])
        self.assertEqual((a, p), ("fill", {"selector": "textarea", "value": "hi"}))
        a, p = transport.parse_bsk_args(["press", "Ctrl+A", "--selector", "textarea"])
        self.assertEqual((a, p), ("press", {"key": "Ctrl+A", "selector": "textarea"}))
        a, p = transport.parse_bsk_args(["session", "start", "--no-focus"])
        self.assertEqual(a, "noop")
        self.assertEqual(transport.parse_bsk_args(["doctor"])[0], None)

    def test_wa_routing_under_iab(self):
        """STARPILOT_TRANSPORT=iab：wa.bsk() 走队列；list_sessions/instance_report 短路。"""
        import wa
        os.environ["STARPILOT_TRANSPORT"] = "iab"
        try:
            self.assertTrue(wa._transport_is_iab())
            rc, out, _ = wa.bsk(["session", "list", "--json"])
            self.assertEqual(rc, 0)
            self.assertIn("iab", out)
            rep = wa.instance_report()
            self.assertTrue(rep["healthy"])
            self.assertEqual(wa.list_sessions()[0]["id"], "iab")
        finally:
            os.environ.pop("STARPILOT_TRANSPORT", None)


class TestEscapeLedgerDoesNotGrowPerSend(unittest.TestCase):
    """逃生门台账必须按**目标**聚合，不能按发送次数线性增长。

    实证（2026-11-03）：一次一发一个 id，加上「每条 P0-ESC 都要在回执里出现」
    = 自激振荡：24 小时攒出 32 条，回执正文比交付物长；回执越长越容易不合格，
    越不合格越走逃生门，逃生门又落一条——门变成了放大器。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.led = os.path.join(self.tmp, "pending.md")

    def test_same_target_is_one_item_with_exact_count(self):
        for i in range(7):
            pending_gate.record_escape(self.led, "aabbccddee11", "写闸门拒写 %d" % i)
        items = pending_gate.parse_pending(self.led)
        self.assertEqual(len(items), 1, "同一目标必须合成一条")
        self.assertEqual(items[0]["id"], "P0-ESC-aabbccddee11")
        self.assertIn("违例 7 次", items[0]["detail"])
        self.assertIn("写闸门拒写 6", items[0]["detail"], "最近理由要留得住")
        self.assertNotIn("写闸门拒写 0", items[0]["detail"], "只留最近几条，不是全量复读")

    def test_different_targets_are_different_items(self):
        pending_gate.record_escape(self.led, "t1", "r1")
        pending_gate.record_escape(self.led, "t2", "r2")
        ids = [i["id"] for i in pending_gate.parse_pending(self.led)]
        self.assertEqual(ids, ["P0-ESC-t1", "P0-ESC-t2"])

    def test_receipt_needs_one_report_then_settle(self):
        """报备一次 → settle 核销 → 之后不必再复读，但条目仍在账上。"""
        pending_gate.record_escape(self.led, "t1", "写闸门 verified=false 拒写")
        receipt = "### 违例栏\n- P0-ESC-t1 ✅ 已核验，未造成重复投递\n"
        ok, why, missing = pending_gate.check_receipt(self.led, receipt)
        self.assertTrue(ok, why)
        res = pending_gate.settle(self.led, receipt, "第3轮")
        self.assertEqual(res["settled"], ["P0-ESC-t1"])
        # 核销后：本轮回执不提它也算合格
        ok2, why2, missing2 = pending_gate.check_receipt(self.led, "本轮没有任何违例")
        self.assertTrue(ok2, why2)
        self.assertEqual(missing2, [])
        # 但记录本身没消失，审计仍可查
        items = pending_gate.parse_pending(self.led)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "settled")
        self.assertIn("违例 1 次", items[0]["detail"])

    def test_settle_refuses_items_not_reported(self):
        """没在回执里出现的条目不许被核销——否则 settle 就是「让违例消失」的后门。"""
        pending_gate.record_escape(self.led, "t1", "r1")
        pending_gate.record_escape(self.led, "t2", "r2")
        res = pending_gate.settle(self.led, "只有 P0-ESC-t1 ✅ 报过了", "第4轮")
        self.assertEqual(res["settled"], ["P0-ESC-t1"])
        st = {i["id"]: i["status"] for i in pending_gate.parse_pending(self.led)}
        self.assertEqual(st["P0-ESC-t2"], "deferred", "未报备的条目必须留在待报状态")

    def test_blocked_items_still_cannot_hide(self):
        """BLOCKED 豁免项同样「报一次可核销」，但没报就绝对不行。"""
        p = os.path.join(self.tmp, "blocked.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write("- [P0][P0-B1] 某豁免 | blocked(用户裁定放行) | 来源:第1轮\n")
        ok, _, missing = pending_gate.check_receipt(p, "本轮回执只谈别的")
        self.assertFalse(ok)
        self.assertIn("P0-B1", missing)
        ok2, _, _ = pending_gate.check_receipt(p, "P0-B1 ⚠️ 已在本轮说明豁免理由")
        self.assertTrue(ok2)


class TestPendingGateCliIsActuallyRunnable(unittest.TestCase):
    """`python scripts/pending_gate.py <子命令>` 必须真的能跑起来。

    这里踩过一次典型的「静默失败伪装成门禁」：入口写的是 `main(sys.argv)`，
    而 main() 约定第 0 位是**子命令名**——于是 SKILL.md 教给 agent 的每条命令
    都返回 rc=2 并打出整篇 docstring。rc=2 在本模块语义里接近「不通过」，
    agent 读到的是「我被门禁拦住了」，真相是「命令根本没执行」。
    单测以前直接 `main([...])`，永远照不到这一面，所以这条必须走真子进程。
    """

    def _run(self, *args):
        import subprocess
        script = os.path.join(str(Path(__file__).resolve().parents[1]),
                              "scripts", "pending_gate.py")
        return subprocess.run([sys.executable, script] + list(args),
                              capture_output=True, text=True,
                              encoding="utf-8", errors="backslashreplace",
                              timeout=60)

    def test_check_receipt_via_real_cli(self):
        tmp = tempfile.mkdtemp()
        led = os.path.join(tmp, "pending.md")
        with open(led, "w", encoding="utf-8") as f:
            f.write("- [P0][P0-1] 事情 | open | 来源:第1轮\n")
        fb = os.path.join(tmp, "fb.md")
        with open(fb, "w", encoding="utf-8") as f:
            f.write("P0-1 ❌ 未做，理由：页面改版\n")
        p = self._run("check-receipt", led, "--file", fb)
        self.assertNotEqual(p.returncode, 2,
                            "又被 usage 挡在门外：%s" % p.stdout[:200])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('"receipt"', p.stdout)
        bad = self._run("check-receipt", led, "--file", led)
        self.assertEqual(bad.returncode, 1, bad.stdout)

    def test_settle_requires_receipt_file(self):
        tmp = tempfile.mkdtemp()
        led = os.path.join(tmp, "pending.md")
        pending_gate.record_escape(led, "t1", "r1")
        p = self._run("settle", led)
        self.assertEqual(p.returncode, 1)
        self.assertIn("SETTLE_NEEDS_RECEIPT", p.stdout)
