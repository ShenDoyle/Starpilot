#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows 多进程真实测试（PROJECT-03-R1 任务书 §7 组 E）。

全部用例启动**独立 OS 进程**（subprocess，每用例全新进程），不是线程。
记录 PID、executor 身份、fencing、锁状态、时间线到结果 JSON（组 E 证据文件）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(ROOT, "tests", "fixtures", "mp_worker.py")

INTENT = {"action": "send_message", "target": "chatgpt", "text": "hello"}
IDENT = {"url": "data:x", "title": "T", "marker": "M", "timeOrigin": 1.0}


class MPCase(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="sp-mp-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.lock = os.path.join(self.d, "res.lock")
        self.ledger = os.path.join(self.d, "ledger.sqlite")
        self.pages = os.path.join(self.d, "pages.json")

    def _start(self, mode: str, timeout: float = 30, **kw):
        """并发场景：启动 worker 但**不等待**，返回 (Popen, out_path)。"""
        kw = dict(kw)
        kw.setdefault("lock", self.lock)
        if mode in ("gate_dispatch", "ledger_reserve", "dispatch_then_die",
                    "recover"):
            kw.setdefault("ledger", self.ledger)
            kw.setdefault("pages", self.pages)
        out = os.path.join(self.d, "%s-%d-%d.json"
                           % (mode, int(time.time() * 1000) % 10 ** 9,
                              abs(hash(tuple(sorted((k, str(v))
                                                    for k, v in kw.items())))) % 10 ** 6))
        argv = [sys.executable, WORKER, mode]
        argv += ["--out=%s" % out]
        mapping = {"lock": "lock", "hold": "hold", "stale_after": "stale_after",
                   "wait": "timeout", "kill_after": "kill_after",
                   "ledger": "ledger", "pages": "pages", "fencing": "fencing",
                   "page_gen": "page_gen", "page_key": "page_key",
                   "identity": "identity", "aid": "aid", "task": "task",
                   "intent": "intent", "seq": "seq", "budget": "budget",
                   "resource": "resource", "executor_id": "executor_id"}
        for k, v in kw.items():
            argv.append("--%s=%s" % (mapping[k], v))
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, cwd=ROOT)
        self.addCleanup(self._kill_quiet, p)
        return p, out

    @staticmethod
    def _kill_quiet(p):
        try:
            if p.poll() is None:
                p.kill()
            p.communicate(timeout=10)
        except Exception:
            pass

    def _finish(self, p, out: str) -> dict:
        """等待 worker 结束并读取证据 JSON。"""
        try:
            _so, _se = p.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            p.kill()
            _so, _se = p.communicate()
        rec = {}
        if os.path.exists(out):
            with open(out, encoding="utf-8") as f:
                rec = json.load(f)
        if not rec and p.returncode not in (0, 9):
            self.fail("worker died without evidence: rc=%s stderr=%s"
                      % (p.returncode, (_se or "")[-500:]))
        rec["_rc"] = p.returncode
        rec["_pid_from_parent"] = p.pid
        # 关键：显式关闭父进程持有的进程句柄。Windows 上只要还有进程句柄
        # 未关闭，被终止的进程对象就仍可被 OpenProcess 查询（token 会判
        # match），导致「死进程接管」测试误判存活。真实场景中由 OS/父进程
        # reaping 完成，这里必须手动释放。
        h = getattr(p, "_handle", None)
        if h is not None and p.returncode is not None:
            try:
                h.Close()
                p._handle = None
            except Exception:
                pass
        return rec

    def _run(self, mode: str, timeout: float = 30, **kw) -> dict:
        p, out = self._start(mode, timeout=timeout, **kw)
        return self._finish(p, out)

    # ---- E1：两个进程同时申请同一锁 → 恰好一个 ACQUIRED ----
    def test_e1_two_processes_contend(self):
        a_p, a_out = self._start("acquire", hold=3.0)
        time.sleep(0.8)  # 确保 A 已拿到锁（证据 JSON 落盘）
        b = self._run("acquire", wait=0.5, stale_after=300)
        a = self._finish(a_p, a_out)
        self.assertEqual(a["verdict"], "ACQUIRED", a)
        self.assertEqual(b["verdict"], "LOCK_BUSY", b)
        self.assertNotEqual(a["pid"], b["pid"], "必须是两个真实 OS 进程")
        self.assertEqual(b["holder"].get("pid"), a["pid"],
                         "LOCK_BUSY 必须指认真实持有者 PID")

    # ---- E2：活进程持锁超过 stale_after → 接管者失败（C3-c 跨进程版）----
    def test_e2_live_holder_over_stale_after_not_taken(self):
        a_p, a_out = self._start("acquire", hold=2.0)
        time.sleep(0.8)
        self.assertTrue(a_p.poll() is None, "A 必须仍在持锁")
        b = self._run("acquire", wait=1.0, stale_after=0.3)
        self.assertEqual(b["verdict"], "LOCK_BUSY", b)
        self.assertEqual(b["code"], "SP-GATE-002", b)
        self.assertIn("stale_warning", json.dumps(b.get("holder", {})),
                      "超龄必须告警")
        self.assertNotIn("took_over", json.dumps(b), "活进程绝不被抢")
        self.assertTrue(os.path.exists(self.lock), "A 仍在持锁，锁文件必须在")
        a = self._finish(a_p, a_out)

    # ---- E3：持锁进程强制终止 → 新进程接管（fencing+1）----
    def test_e3_killed_holder_is_taken_over(self):
        a = self._run("acquire", kill_after=0.6)
        self.assertEqual(a["verdict"], "ACQUIRED")
        gen0 = a["fencing_generation"]
        self.assertIsNone(a.get("released"))
        time.sleep(0.4)  # 等 OS 关闭死进程句柄
        b = self._run("acquire", timeout=5, stale_after=1)
        self.assertEqual(b["verdict"], "ACQUIRED", b)
        self.assertEqual(b["stale"], "took_over:dead_pid", b)
        self.assertEqual(b["fencing_generation"], gen0 + 1, "接管必须 fencing+1")
        self.assertEqual(b["took"]["pid"], a["pid"],
                         "接管记录必须指向真实死者")

    # ---- E4：旧 fencing/旧执行者尝试 dispatch → FENCED_OUT ----
    def test_e4_stale_fencing_dispatch_rejected_cross_process(self):
        # 先在主进程造好账本与页面绑定
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import actionledger
        import pageidentity
        led = actionledger.ActionLedger(self.ledger)
        pi = pageidentity.PageIdentityManager(self.pages)
        r = led.reserve("t-e4", INTENT, budget_units=10,
                        business_target="ts32|p1|t-e4|send")
        aid = r["data"]["action_id"]
        b = pi.bind("ts32|p1", IDENT, "ex-old", 4, "bind")
        led.close()
        # 用伪造的旧 fencing=3（真实持有者不存在，锁文件无 lease）跨进程派发
        w = self._run("gate_dispatch", ledger=self.ledger, pages=self.pages,
                      lock=self.lock, fencing=3, page_gen=b["page_generation"],
                      page_key="ts32|p1", identity=json.dumps(IDENT), aid=aid,
                      executor_id="ex-old")
        self.assertFalse(w["ok"])
        self.assertEqual(w["code"], "SP-GATE-007", w)

    # ---- E5：两进程**并发**预占同一 intent → 唯一索引裁决 ----
    def test_e5_concurrent_reserve_single_winner(self):
        # SQLITE_BUSY 只是并发初始化的瞬时竞争，不是账本语义问题：
        # 整场竞赛最多重赛一次（重赛必须换全新 task/intent —— 否则会命中
        # 上一轮赢家仍活跃的去重，那是账本的正确行为）。
        for round_no in range(2):
            intent_r = dict(INTENT, race_round=round_no)
            pa, outa = self._start("ledger_reserve", task="t1-r%d" % round_no,
                                   intent=json.dumps(intent_r))
            pb, outb = self._start("ledger_reserve", task="t2-other-r%d" % round_no,
                                   intent=json.dumps(intent_r))
            a = self._finish(pa, outa)
            b = self._finish(pb, outb)
            codes = sorted([a.get("code", a.get("verdict", "?")),
                            b.get("code", b.get("verdict", "?"))])
            if "SQLITE_BUSY" in codes and round_no == 0:
                continue  # 瞬时 sqlite 竞争：重赛一次
            self.assertEqual(codes, ["SP-ACT-002", "SP-OK"],
                             "跨 task_id 并发预占同一意图必须只有一个赢家")
            self.assertNotEqual(a["pid"], b["pid"])
            return
        self.fail("E5 两轮都出现 SQLITE_BUSY —— SQLite 并发超预期")

    # ---- E6：dispatch 与 outcome 之间强制退出 → 恢复 SEND_UNKNOWN，重放被拒 ----
    def test_e6_kill_between_dispatch_and_outcome(self):
        w = self._run("dispatch_then_die", ledger=self.ledger, lock=self.lock,
                      task="t-e6", intent=json.dumps(INTENT))
        self.assertEqual(w["_rc"], 9, "worker 必须在 outcome 前被强制终止")
        self.assertTrue(w["dispatch"]["ok"])
        aid = w["dispatch"]["aid"]
        rec = self._run("recover", ledger=self.ledger, task="t-e6",
                        intent=json.dumps(INTENT))
        recovered = rec["recovered"]["recovered"]
        self.assertEqual([x["action_id"] for x in recovered], [aid])
        # 重放尝试：同意图再 reserve / 新 task_id 绕过 → 全拒
        self.assertFalse(rec["re_reserve"]["ok"])
        self.assertIn(rec["re_reserve"]["code"], ("SP-ACT-002", "SP-ACT-003"))
        bypass = self._run("ledger_reserve", ledger=self.ledger,
                           task="t-e6-fresh-task",
                           intent=json.dumps(dict(INTENT)))
        self.assertFalse(bypass["ok"])
        self.assertEqual(bypass["code"], "SP-ACT-002")
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import actionledger
        led = actionledger.ActionLedger(self.ledger)
        try:
            row = led.get(aid)
            self.assertEqual(row["state"], "SEND_UNKNOWN")
            self.assertEqual(row["anti_replay_locked"], 1)
            self.assertEqual(row["resend_count"], 0, "自动重发恒 0")
        finally:
            led.close()


if __name__ == "__main__":
    unittest.main()
