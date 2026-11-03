# -*- coding: utf-8 -*-
"""L2 shim 门禁回归（2026-11-02 复审裁定后重写）。

裁定要点：
· `STARPILOT_WINDOW_GATE=1` 是可伪造布尔量 → 废弃，改**一次性 HMAC 票据**
  （wa.py 签发，shim 兑换即焚，120s 过期，不可重放）；
· shim 自身故障**不再 fail-open**：账本不可导入 / 计数读不出 / 记账写不进 /
  票据校验异常 → 一律 rc=9 拒绝。

不联网：所有 bsk 子进程调用都被替换。
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import winlease  # noqa: E402


def _load_shim():
    p = os.path.join(ROOT, "tools", "bsk_shim.py")
    spec = importlib.util.spec_from_file_location("bsk_shim_under_test", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TicketBase(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="sp-gate-")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.p = mock.patch.dict(os.environ, {"STARPILOT_LOCK_DIR": self.d})
        self.p.start()
        self.addCleanup(self.p.stop)


class TestGateTicket(TicketBase):
    def test_issue_then_redeem_once(self):
        t = winlease.gate_issue()
        self.assertTrue(winlease.gate_redeem(t["ticket"])["ok"])
        # 一次性：同一张票第二次必须失败（文件已被烧掉）
        second = winlease.gate_redeem(t["ticket"])
        self.assertFalse(second["ok"])
        self.assertEqual(second["why"], "unknown_or_used")

    def test_key_is_created_private(self):
        winlease.gate_key()
        p = os.path.join(self.d, winlease.GATE_KEY_NAME)
        self.assertTrue(os.path.exists(p))
        self.assertGreaterEqual(len(open(p, "rb").read()), 32)

    def test_forged_ticket_rejected(self):
        # 没有对应票据文件的「票」= 伪造，拒绝
        fake = "%s.%.0f.%s" % ("a" * 32, time.time(), "b" * 64)
        res = winlease.gate_redeem(fake)
        self.assertFalse(res["ok"])
        self.assertEqual(res["why"], "unknown_or_used")

    def test_bad_signature_rejected(self):
        t = winlease.gate_issue()
        nonce, ts, _sig = t["ticket"].split(".")
        bad = "%s.%s.%s" % (nonce, ts, "0" * 64)
        res = winlease.gate_redeem(bad)
        self.assertFalse(res["ok"])
        self.assertEqual(res["why"], "bad_signature")

    def test_expired_ticket_rejected(self):
        t = winlease.gate_issue()
        nonce, ts, sig = t["ticket"].split(".")
        old = "%.0f" % (time.time() - 600)
        # 造一张「签名正确但时间很旧」的票：用真密钥重算签名
        key = winlease.gate_key()
        rec = {"nonce": nonce, "ts": old, "sig": winlease._gate_sig(key, nonce, old)}
        with open(os.path.join(winlease.gate_dir(), nonce + ".ticket"), "w") as f:
            json.dump(rec, f)
        res = winlease.gate_redeem("%s.%s.%s" % (nonce, old, rec["sig"]))
        self.assertFalse(res["ok"])
        self.assertEqual(res["why"], "expired")


    def test_copied_ticket_file_cannot_be_reused(self):
        """用过之后把 .ticket 副本贴回来 = 仍然无效（.used 墓碑挡住）。"""
        t = winlease.gate_issue()
        nonce = t["ticket"].split(".")[0]
        backup = t["path"] + ".bak"
        shutil.copy2(t["path"], backup)
        self.assertTrue(winlease.gate_redeem(t["ticket"])["ok"])
        shutil.copy2(backup, t["path"])                 # 攻击者贴回副本
        res = winlease.gate_redeem(t["ticket"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["why"], "unknown_or_used")
        # 贴回来的副本文件还在，但已无用：墓碑 .used 让同一 nonce 永久失效
        self.assertTrue(os.path.exists(
            os.path.join(winlease.gate_dir(), nonce + ".used")))

    def test_concurrent_redeem_only_one_wins(self):
        """两进程同时兑换同一张票：只能一个成功（原子改名抢烧）。"""
        import subprocess
        t = winlease.gate_issue()
        code = (
            "import sys;sys.path.insert(0,%r);import winlease;"
            "r=winlease.gate_redeem(%r);print('OK' if r['ok'] else r['why'])"
            % (os.path.join(ROOT, "scripts"), t["ticket"]))
        env = dict(os.environ, STARPILOT_LOCK_DIR=self.d)
        procs = [subprocess.Popen([sys.executable, "-c", code], env=env,
                                  stdout=subprocess.PIPE, text=True)
                 for _ in range(2)]
        outs = [(p.wait(timeout=60), p.stdout.read().strip()) for p in procs]
        wins = [o for rc, o in outs if o == "OK"]
        self.assertEqual(len(wins), 1, outs)
        self.assertEqual(sum(1 for rc, _ in outs if rc == 0), 2, outs)

    def test_wrong_key_ticket_rejected(self):
        """换一把密钥（模拟别的安装位）签的票不被承认。"""
        t = winlease.gate_issue()
        os.remove(os.path.join(self.d, winlease.GATE_KEY_NAME))
        winlease.gate_key()                            # 新密钥
        res = winlease.gate_redeem(t["ticket"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["why"], "bad_signature")

    def test_malformed_rejected(self):
        for bad in ("", "x", "a.b", "zz.123.456"):
            self.assertFalse(winlease.gate_redeem(bad)["ok"])

    def test_redeem_without_key_fails_closed(self):
        t = winlease.gate_issue()
        os.remove(os.path.join(self.d, winlease.GATE_KEY_NAME))
        res = winlease.gate_redeem(t["ticket"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["why"], "no_key")


class TestShimFailClosed(TicketBase):
    def setUp(self):
        super().setUp()
        self.shim = _load_shim()
        self.real = os.path.join(self.d, "bsk.bin.exe")
        open(self.real, "w").close()
        self.patches = [mock.patch.object(self.shim, "REAL", self.real),
                        mock.patch.object(self.shim, "_SKILL_SCRIPT_DIRS",
                                          [os.path.join(ROOT, "scripts")])]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop(self.shim.TICKET_ENV, None)

    def _main(self, argv=("session", "start")):
        ran = []

        def fake_run(args):
            ran.append(args)
            return 0

        with mock.patch.object(sys, "argv", ["bsk"] + list(argv)), \
                mock.patch.object(self.shim, "_run", side_effect=fake_run):
            rc = self.shim.main()
        return rc, ran

    def test_no_ledger_denies(self):
        with mock.patch.object(self.shim, "_SKILL_SCRIPT_DIRS",
                              [os.path.join(self.d, "nope")]):
            rc, ran = self._main()
        self.assertEqual(rc, 9)
        self.assertEqual(ran, [])

    def test_count_failure_denies(self):
        bad = mock.Mock()
        bad.too_many_starts.side_effect = OSError("lock dir gone")
        with mock.patch.object(self.shim, "_winlease", return_value=bad):
            rc, ran = self._main()
        self.assertEqual(rc, 9)
        self.assertEqual(ran, [])

    def test_note_start_failure_denies(self):
        bad = mock.Mock()
        bad.too_many_starts.return_value = (False, 0, 10)
        bad.note_start.side_effect = OSError("readonly")
        with mock.patch.object(self.shim, "_winlease", return_value=bad):
            rc, ran = self._main()
        self.assertEqual(rc, 9)
        self.assertEqual(ran, [])

    def test_bad_ticket_denies(self):
        os.environ[self.shim.TICKET_ENV] = "a" * 32 + ".%.0f." % time.time() + "b" * 64
        rc, ran = self._main()
        self.assertEqual(rc, 9)
        self.assertEqual(ran, [])

    def test_forged_bool_env_no_longer_works(self):
        """旧令牌 STARPILOT_WINDOW_GATE=1 已废弃：设了也必须按无票处理。"""
        os.environ["STARPILOT_WINDOW_GATE"] = "1"
        self.addCleanup(os.environ.pop, "STARPILOT_WINDOW_GATE", None)
        wl = mock.Mock()
        wl.too_many_starts.return_value = (True, 99, 10)   # 超限 → 必须拒
        with mock.patch.object(self.shim, "_winlease", return_value=wl):
            rc, ran = self._main()
        self.assertEqual(rc, 9)
        self.assertEqual(ran, [])
        wl.note_start.assert_not_called()

    def test_valid_ticket_passes_without_counting(self):
        t = winlease.gate_issue()
        os.environ[self.shim.TICKET_ENV] = t["ticket"]
        wl = mock.Mock()
        with mock.patch.object(self.shim, "_winlease", return_value=wl) as m:
            # 让 mock 也能兑换：直接用真实现
            m.return_value = winlease
            rc, ran = self._main()
        self.assertEqual(rc, 0)
        self.assertEqual(ran, [["session", "start"]])
        self.assertFalse(wl.too_many_starts.called)

    def test_over_limit_without_ticket_denies(self):
        wl = mock.Mock()
        wl.too_many_starts.return_value = (True, 10, 10)
        with mock.patch.object(self.shim, "_winlease", return_value=wl):
            rc, ran = self._main()
        self.assertEqual(rc, 9)
        self.assertEqual(ran, [])

    def test_within_limit_records_and_runs(self):
        wl = mock.Mock()
        wl.too_many_starts.return_value = (False, 3, 10)
        with mock.patch.object(self.shim, "_winlease", return_value=wl):
            rc, ran = self._main()
        self.assertEqual(rc, 0)
        wl.note_start.assert_called_once()
        self.assertEqual(ran, [["session", "start"]])


if __name__ == "__main__":
    unittest.main()
