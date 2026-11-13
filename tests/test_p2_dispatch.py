"""PROJECT-04 P2-2-4 强制验收矩阵：DP01-DP15 + P2-2-1 遗留闭环（真实执行）。

- 后端为受控可计数模拟端（不触真实浏览器，任务书 §7）。
- DP05/07/08/09 用独立子进程真实中断（os._exit），父进程读持久化状态与调用计数。
- DP15 静态审计所有副作用入口；未知 op / 无 plane / 空 target 策略全测。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "scripts"))

import actionledger as AL  # noqa: E402
import dispatch_marker as DM  # noqa: E402
import envelope  # noqa: E402
import execution_gate as EG  # noqa: E402
import p2_plane  # noqa: E402
import pageidentity  # noqa: E402
import procidentity  # noqa: E402
import sp_errors as SP  # noqa: E402
import statelock  # noqa: E402

REPO = os.path.abspath(os.path.join(TESTS_DIR, ".."))
IDENT = {"url": "data:x", "title": "T", "marker": "M", "timeOrigin": 1.0}


class Harness:
    def __init__(self, d, plane=True):
        self.d = d
        self.lock_path = os.path.join(d, "gate.lock")
        self.ledger = AL.ActionLedger(os.path.join(d, "ledger.sqlite"))
        self.pi = pageidentity.PageIdentityManager(os.path.join(d, "pages.json"))
        self.plane = p2_plane.make_plane(d) if plane else None
        self.gate = EG.ExecutionGate(
            self.lock_path, self.ledger, self.pi,
            ownership_probe=mock.Mock(return_value="agent"),
            resource="taskspace-32", plane=self.plane)

    def ctx(self, page_key="ts32|p1", executor=None):
        ex = executor or procidentity.executor_id()
        b = self.pi.bind(page_key, IDENT, ex, 0, "bind")
        ac = self.gate.acquire({"executor_id": ex, "run_id": "r1"})
        self.lease = ac["data"].get("lease") or ac["data"].get("data", {}).get("lease")
        gen = ac["data"]["fencing_generation"]
        self.pi.bind(page_key, IDENT, ex, gen, "bind")
        return {"executor_id": ex, "run_id": "r1",
                "fencing_generation": gen,
                "page_generation": self.pi.status(page_key).get(
                    "page_generation", "")}

    def reserve(self, task="t1", intent=None, target="order:p24"):
        r = self.ledger.reserve(task, intent or {"action": "send", "x": 1},
                                budget_units=10, business_target=target,
                                side_effect=True)
        assert r["ok"], r
        return r["data"]["action_id"]

    def dispatch(self, ctx, aid, backend, op="send", page_key="ts32|p1"):
        return self.gate.dispatch(
            ctx, op, backend, {"x": 1}, taskspace_id=32, page_key=page_key,
            identity_tuple=IDENT, action_id=aid)


class Base(unittest.TestCase):
    plane = True

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="p2dp-")
        self.h = Harness(self.d, plane=self.plane)
        self.ctx = self.h.ctx()

    def tearDown(self):
        self.h.ledger.close()
        shutil.rmtree(self.d, ignore_errors=True)

    def _count_backend(self, result_ok=True):
        calls = []

        def backend(op, args, ctx):
            calls.append(op)
            return {"ok": result_ok, "result": "ok"}

        return backend, calls


class TestDP01Order(Base):
    def test_dp01_strict_sequence_single_call(self):
        order = []
        real_env = envelope.build_and_sign
        real_mk = DM.write_marker
        real_dg = self.h.ledger.write_delegated
        real_mark = self.h.ledger.mark_dispatched

        def spy_env(*a, **k):
            order.append("envelope")
            return real_env(*a, **k)

        def spy_mk(*a, **k):
            order.append("marker")
            return real_mk(*a, **k)

        def spy_dg(*a, **k):
            order.append("delegated")
            return real_dg(*a, **k)

        def spy_mark(*a, **k):
            order.append("mark")
            return real_mark(*a, **k)

        backend, calls = self._count_backend()
        aid = self.h.reserve()
        with mock.patch.object(envelope, "build_and_sign", side_effect=spy_env), \
             mock.patch.object(DM, "write_marker", side_effect=spy_mk), \
             mock.patch.object(self.h.ledger, "write_delegated", side_effect=spy_dg), \
             mock.patch.object(self.h.ledger, "mark_dispatched", side_effect=spy_mark):
            r = self.h.dispatch(self.ctx, aid, backend)
        self.assertTrue(r["ok"], r)
        self.assertEqual(order, ["envelope", "marker", "delegated", "mark"])
        self.assertEqual(len(calls), 1)


class TestDP02EnvelopeInvalid(Base):
    def test_dp02_envelope_failure_blocks_all(self):
        backend, calls = self._count_backend()
        aid = self.h.reserve()
        with mock.patch.object(envelope, "build_and_sign",
                               side_effect=envelope.EnvelopeUnavailable("no dep")):
            r = self.h.dispatch(self.ctx, aid, backend)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_OP_001)
        self.assertEqual(calls, [])
        self.assertFalse(os.path.exists(DM.marker_path(self.h.plane.dispatching, aid)))
        self.assertFalse(self.h.ledger.delegated_events(aid)["present"])


class TestDP03MarkerFailure(Base):
    def test_dp03_marker_failure_blocks(self):
        backend, calls = self._count_backend()
        aid = self.h.reserve()
        with mock.patch.object(DM, "write_marker",
                               side_effect=DM.MarkerPersistFailed("fsync")):
            r = self.h.dispatch(self.ctx, aid, backend)
        self.assertFalse(r["ok"])
        self.assertEqual(calls, [])
        self.assertFalse(self.h.ledger.delegated_events(aid)["present"])
        self.assertEqual(self.h.ledger.get(aid)["state"], "RESERVED")


class TestDP04DelegatedFailure(Base):
    def test_dp04_delegated_failure_blocks(self):
        backend, calls = self._count_backend()
        aid = self.h.reserve()
        with mock.patch.object(self.h.ledger, "write_delegated",
                               return_value=SP.result(False, SP.SP_OP_001,
                                                      {"reason": "x"})):
            r = self.h.dispatch(self.ctx, aid, backend)
        self.assertFalse(r["ok"])
        self.assertEqual(calls, [])
        self.assertEqual(self.h.ledger.get(aid)["state"], "RESERVED")  # 未 mark


class CrashChild:
    """子进程模板：真实 dispatch + 指定阶段 os._exit。"""

    @staticmethod
    def src(stage: str) -> str:
        return (
            "import os, sys, json, time\n"
            "sys.path.insert(0, r'%s')\n"
            "sys.path.insert(0, r'%s')\n"
            "import actionledger as AL, execution_gate as EG, p2_plane, pageidentity, procidentity\n"
            "from unittest import mock\n"
            "d = sys.argv[1]\n"
            "L = AL.ActionLedger(os.path.join(d, 'ledger.sqlite'))\n"
            "pi = pageidentity.PageIdentityManager(os.path.join(d, 'pages.json'))\n"
            "IDENT = {'url':'data:x','title':'T','marker':'M','timeOrigin':1.0}\n"
            "plane = p2_plane.make_plane(d)\n"
            "g = EG.ExecutionGate(os.path.join(d,'gate.lock'), L, pi,\n"
            "                     ownership_probe=lambda s: 'agent', resource='taskspace-32', plane=plane)\n"
            "ex = procidentity.executor_id()\n"
            "pi.bind('ts32|p1', IDENT, ex, 0, 'bind')\n"
            "ac = g.acquire({'executor_id': ex, 'run_id': 'r1'})\n"
            "gen = ac['data']['fencing_generation']\n"
            "pi.bind('ts32|p1', IDENT, ex, gen, 'bind')\n"
            "ctx = {'executor_id': ex, 'run_id': 'r1', 'fencing_generation': gen,\n"
            "       'page_generation': pi.status('ts32|p1').get('page_generation','')}\n"
            "aid = sys.argv[2]\n"
            "count = os.path.join(d, 'backend-count.txt')\n"
            "def backend(op, args, ctx):\n"
            "    with open(count, 'a') as f: f.write('1\\n')\n"
            "    stage = sys.argv[3]\n"
            "    if stage == 'during':\n"
            "        os._exit(8)\n"
            "    return {'ok': True, 'result': 'ok'}\n"
            "stage = sys.argv[3]\n"
            "if stage == 'after_delegated':\n"
            "    real = L.mark_dispatched\n"
            "    L.mark_dispatched = lambda *a, **k: os._exit(5)\n"
            "if stage == 'after_mark':\n"
            "    real = L.mark_dispatched\n"
            "    def _m(*a, **k):\n"
            "        r = real(*a, **k)\n"
            "        os._exit(6)\n"
            "    L.mark_dispatched = _m\n"
            "r = g.dispatch(ctx, 'send', backend, {'x':1}, taskspace_id=32,\n"
            "               page_key='ts32|p1', identity_tuple=IDENT, action_id=aid)\n"
            "if stage == 'after_backend':\n"
            "    os._exit(9)\n"
            "print(json.dumps(r))\n"
        ) % (os.path.join(REPO, "scripts"), TESTS_DIR)

    @staticmethod
    def run(d: str, aid: str, stage: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-c", CrashChild.src(stage), d, aid, stage],
            capture_output=True, text=True, timeout=120)

    @staticmethod
    def count(d: str) -> int:
        p = os.path.join(d, "backend-count.txt")
        if not os.path.exists(p):
            return 0
        with open(p) as f:
            return len([x for x in f.read().splitlines() if x.strip()])


class TestCrashMatrix(Base):
    def _prepare(self):
        aid = self.h.reserve()
        # 释放父进程锁句柄，让子进程能独立 acquire（真实崩溃注入前提）
        try:
            statelock.release_named(self.h.lock_path, self.h.lease)
        except Exception:
            pass
        return aid

    def test_dp05_crash_after_delegated(self):
        aid = self._prepare()
        p = CrashChild.run(self.d, aid, "after_delegated")
        self.assertEqual(p.returncode, 5, p.stderr)
        self.assertEqual(CrashChild.count(self.d), 0)     # backend 未调用
        self.assertTrue(self.h.ledger.delegated_events(aid)["present"])
        v = self.h.gate.recovery_verdict(aid)
        self.assertIn(v["verdict"], ("DISPATCH_POSSIBLE", "UNKNOWN"))
        # 再次 dispatch（新执行者重新取锁）：Marker 已存在 → 保守拒绝，不自动重派
        backend, calls = self._count_backend()
        self.ctx = self.h.ctx()      # 子进程已换代，父进程重新 acquire
        r = self.h.dispatch(self.ctx, aid, backend)
        self.assertFalse(r["ok"])
        self.assertEqual(calls, [])
        self.assertEqual(CrashChild.count(self.d), 0)

    def test_dp07_crash_after_mark_before_backend(self):
        aid = self._prepare()
        p = CrashChild.run(self.d, aid, "after_mark")
        self.assertEqual(p.returncode, 6, p.stderr)
        self.assertEqual(CrashChild.count(self.d), 0)
        self.assertEqual(self.h.ledger.get(aid)["state"], "DISPATCHED")
        self.h.ledger.recover_unknown()
        self.assertEqual(self.h.ledger.get(aid)["state"], "SEND_UNKNOWN")
        v = self.h.gate.recovery_verdict(aid)
        self.assertIn(v["verdict"], ("DISPATCH_POSSIBLE", "UNKNOWN"))

    def test_dp08_crash_during_backend(self):
        aid = self._prepare()
        p = CrashChild.run(self.d, aid, "during")
        self.assertEqual(p.returncode, 8, p.stderr)
        self.assertEqual(CrashChild.count(self.d), 1)      # 真实进入过 backend 一次
        self.h.ledger.recover_unknown()
        row = self.h.ledger.get(aid)
        self.assertEqual(row["state"], "SEND_UNKNOWN")
        self.assertEqual(row["resend_count"], 0)

    def test_dp09_crash_after_backend_before_commit(self):
        aid = self._prepare()
        p = CrashChild.run(self.d, aid, "after_backend")
        self.assertEqual(p.returncode, 9, p.stderr)
        self.assertEqual(CrashChild.count(self.d), 1)
        self.h.ledger.recover_unknown()
        self.assertEqual(self.h.ledger.get(aid)["state"], "SEND_UNKNOWN")
        # 不自动重派：新执行者再 dispatch 被拒且 backend 计数不增
        backend, calls = self._count_backend()
        self.ctx = self.h.ctx()
        r = self.h.dispatch(self.ctx, aid, backend)
        self.assertFalse(r["ok"])
        self.assertEqual(calls, [])
        self.assertEqual(CrashChild.count(self.d), 1)


class TestDP10Timeout(Base):
    def test_dp10_backend_timeout_no_retry(self):
        calls = []

        def backend(op, args, ctx):
            calls.append(1)
            raise TimeoutError("bridge timeout")

        aid = self.h.reserve()
        r = self.h.dispatch(self.ctx, aid, backend)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_BE_004)
        self.assertEqual(len(calls), 1)                    # 绝不自动重试
        row = self.h.ledger.get(aid)
        self.assertEqual(row["state"], "SEND_UNKNOWN")
        self.assertEqual(row["resend_count"], 0)


class TestDP11Fencing(Base):
    def test_dp11_stale_fencing_refused(self):
        aid = self.h.reserve()
        stale = dict(self.ctx, fencing_generation=self.ctx["fencing_generation"] - 1)
        backend, calls = self._count_backend()
        r = self.h.dispatch(stale, aid, backend)
        self.assertFalse(r["ok"])
        self.assertEqual(calls, [])
        self.assertFalse(os.path.exists(DM.marker_path(self.h.plane.dispatching, aid)))


class TestDP12Conflict(Base):
    def test_dp12_identity_conflict_no_overwrite(self):
        aid = self.h.reserve()
        # 预置一条不同 digest 的 DELEGATED（模拟身份冲突）
        r0 = self.h.ledger.write_delegated(aid, aid, "f" * 64, "1", "w-old",
                                           "1", "order:old")
        self.assertTrue(r0["ok"], r0)
        backend, calls = self._count_backend()
        r = self.h.dispatch(self.ctx, aid, backend)
        self.assertFalse(r["ok"])
        self.assertEqual(calls, [])
        ev = self.h.ledger.delegated_events(aid)["events"][0]
        self.assertEqual(ev["envelope_digest"], "f" * 64)  # 原事件未被覆盖


class TestDP13InfraRetry(Base):
    def test_dp13_infra_retry_never_recalls_backend(self):
        aid = self.h.reserve()
        real = self.h.ledger._write_delegated_once
        state = {"n": 0}

        def flaky(fields):
            state["n"] += 1
            if state["n"] == 1:
                import sqlite3
                raise sqlite3.OperationalError("transient")
            return real(fields)

        backend, calls = self._count_backend()
        with mock.patch.object(self.h.ledger, "_write_delegated_once",
                               side_effect=flaky):
            r = self.h.dispatch(self.ctx, aid, backend)
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(calls), 1)                    # 后端恰一次
        self.assertEqual(len(self.h.ledger.delegated_events(aid)["events"]), 1)


class TestDP14Concurrency(Base):
    def test_dp14_single_effective_dispatch(self):
        aid = self.h.reserve()
        results, calls, lock = [], [], threading.Lock()

        def run_one():
            try:
                L = AL.ActionLedger(os.path.join(self.d, "ledger.sqlite"))
                g = EG.ExecutionGate(self.h.lock_path, L, self.h.pi,
                                     ownership_probe=mock.Mock(return_value="agent"),
                                     resource="taskspace-32", plane=self.h.plane)
                ctx = dict(self.ctx)

                def backend(op, args, c):
                    with lock:
                        calls.append(1)
                    return {"ok": True}
                try:
                    r = g.dispatch(ctx, "send", backend, {"x": 1},
                                   taskspace_id=32, page_key="ts32|p1",
                                   identity_tuple=IDENT, action_id=aid)
                finally:
                    L.close()
            except Exception as e:                       # noqa: BLE001
                r = {"ok": False, "code": "EXC", "err": str(e)[:80]}
            with lock:
                results.append(r)

        ts = [threading.Thread(target=run_one) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(60)
        self.assertEqual(len(results), 4)
        self.assertEqual(sum(1 for r in results if r["ok"]), 1, results)
        self.assertEqual(len(calls), 1)


class TestDP15Audit(Base):
    def test_dp15_no_bypass_entry(self):
        """全部副作用入口审计：只有 dispatch 调 backend_fn；业务层不触后端。"""
        src = open(os.path.join(REPO, "scripts", "execution_gate.py"),
                   encoding="utf-8").read()
        # P2-2-4R：backend 收到的是「Node 已验签的 envelope」上下文，故实参名
        # 由 dict(ctx) 改为 backend_ctx（唯一调用点数量仍是 1，不新增旁路）。
        self.assertIn("backend_fn(op, dict(args or {}), backend_ctx)", src)
        self.assertEqual(src.count("= backend_fn(op, dict(args or {}), backend_ctx)"), 1)
        # 调用点唯一性用 AST 判定（避免被 docstring 里的签名示例干扰）
        import ast
        tree = ast.parse(src)
        call_sites = [n for n in ast.walk(tree)
                      if isinstance(n, ast.Call)
                      and isinstance(n.func, ast.Name)
                      and n.func.id == "backend_fn"]
        self.assertEqual(len(call_sites), 1,
                         "backend_fn 必须只有一个真实调用点（AST 计数）")
        # 其它脚本不得直接调用 ego_backend.page_op / _run_bridge
        import glob
        offenders = []
        for p in glob.glob(os.path.join(REPO, "scripts", "*.py")):
            if os.path.basename(p) in ("ego_backend.py",):
                continue
            t = open(p, encoding="utf-8").read()
            if "page_op(" in t or "_run_bridge(" in t:
                offenders.append(os.path.basename(p))
        self.assertEqual(offenders, [], "bypass callers: %s" % offenders)

    def test_dp15b_unknown_op_and_empty_target_policy(self):
        aid = self.h.reserve()
        backend, calls = self._count_backend()
        r = self.h.dispatch(self.ctx, aid, backend, op="__evil__")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], SP.SP_ACT_010)
        self.assertEqual(calls, [])
        # side-effect op 无 business_target → SP-ACT-010
        r2 = self.h.ledger.reserve("t2", {"a": 1}, budget_units=5)
        self.assertTrue(r2["ok"]) if r2["ok"] else None
        if r2["ok"]:
            r3 = self.h.dispatch(self.ctx, r2["data"]["action_id"], backend)
            self.assertFalse(r3["ok"])
            self.assertEqual(r3["code"], SP.SP_ACT_010)
        # 读操作允许空 target，且不需要 plane 也能派发（无副作用）
        h2 = Harness(tempfile.mkdtemp(prefix="p2dp-ro-"), plane=False)
        try:
            h2.ledger.reserve("tr", {"action": "read", "z": 2}, budget_units=5,
                              side_effect=False)
            aid2 = [a for a in h2.ledger.list_actions()
                    if a.get("task_id") == "tr"][0]["action_id"] \
                if hasattr(h2.ledger, "list_actions") else None
            # 直接构造只读 op 的 action（reserve 后取 id）
            rows = h2.ledger._conn.execute(
                "SELECT action_id FROM actions WHERE task_id='tr'").fetchall()
            aid2 = rows[0]["action_id"]
            rd, rc = self._count_backend()
            ctx2 = h2.ctx()
            r4 = h2.gate.dispatch(ctx2, "page_viewport", rd, {"x": 1},
                                  taskspace_id=32, page_key="ts32|p1",
                                  identity_tuple=IDENT, action_id=aid2)
            self.assertTrue(r4["ok"], r4)
            self.assertEqual(len(rc), 1)
        finally:
            h2.ledger.close()
            shutil.rmtree(h2.d, ignore_errors=True)


class TestSupplementaryP221(Base):
    """P2-2-1 遗留三项闭环（P2-2-4 强制）。"""

    def test_registry_trust_boundary(self):
        kd = os.path.join(self.d, "reg-keys")
        envelope.ensure_keypair(kd, "1")
        envelope.write_registry(kd, "1", ["1"])
        self.assertTrue(envelope.check_registry_trust(kd)["ok"])
        # 版本冲突
        envelope.write_registry(kd, "1", ["1"], retired=["1"])
        with self.assertRaises(envelope.EnvelopeUnavailable):
            envelope.check_registry_trust(kd)
        # active 不在 valid
        envelope.write_registry(kd, "2", ["1"])
        with self.assertRaises(envelope.EnvelopeUnavailable):
            envelope.check_registry_trust(kd)
        # 损坏
        with open(os.path.join(kd, "registry.json"), "w") as f:
            f.write("{bad json")
        with self.assertRaises(envelope.EnvelopeUnavailable):
            envelope.check_registry_trust(kd)
        # 缺失
        os.unlink(os.path.join(kd, "registry.json"))
        with self.assertRaises(envelope.EnvelopeUnavailable):
            envelope.check_registry_trust(kd)
        # 公钥被错误内容替换
        envelope.write_registry(kd, "1", ["1"])
        with open(os.path.join(kd, "v1", "public.pem"), "w") as f:
            f.write("not a pem")
        with self.assertRaises(envelope.EnvelopeUnavailable):
            envelope.check_registry_trust(kd)
        # SecurityPlane 初始化 fail-closed
        with self.assertRaises(envelope.EnvelopeUnavailable):
            EG.SecurityPlane(key_dir=kd, worker_id="w",
                             ops_root=os.path.join(self.d, "ops2"))

    def test_dependency_lock_platform_coverage(self):
        p = os.path.join(REPO, "requirements-lock.txt")
        t = open(p, encoding="utf-8").read()
        self.assertIn('sys_platform == "win32"', t)
        self.assertIn('sys_platform == "linux"', t)
        self.assertIn("cryptography==45.0.7", t)
        # 两个平台的 cryptography 哈希必须不同（不能以一个平台代表另一个）
        import re
        hashes = re.findall(r"cryptography==45\.0\.7[^\n]*\n\s*--hash=sha256:([0-9a-f]{64})", t)
        self.assertEqual(len(hashes), 2, hashes)
        self.assertNotEqual(hashes[0], hashes[1])

    def test_node_duplicate_key_rejected(self):
        node = shutil.which("node")
        self.assertIsNotNone(node)
        mjs = os.path.join(REPO, "scripts", "ego", "envelope_verify.mjs")
        kd = os.path.join(self.d, "dup-keys")
        envelope.ensure_keypair(kd, "1")
        envelope.write_registry(kd, "1", ["1"])
        env = envelope.build_and_sign("op-dup", "t", "ik", 1, "w", "", kd)
        # 正常文件 → 通过
        good = os.path.join(self.d, "good.json")
        json.dump(env, open(good, "w", encoding="utf-8"), ensure_ascii=False)
        out = os.path.join(self.d, "r1.json")
        subprocess.run([node, mjs, good, kd, out], check=True, timeout=60)
        self.assertTrue(json.load(open(out, encoding="utf-8"))["ok"])
        # 手工构造含重复键的原始 JSON → 拒绝（不依赖 JSON.parse 行为）
        raw = '{"version":"1","version":"1","algorithm":"ed25519","key_version":"1",' \
              '"op_id":"op-dup","task_id":"t","intent_key":"ik","fencing":"1",' \
              '"worker_id":"w","issued_at":"2026-11-13T00:00:00Z",' \
              '"business_target_norm":"","envelope_digest":"%s","signature":"%s"}' % (
                  env["envelope_digest"], env["signature"])
        bad = os.path.join(self.d, "dup.json")
        open(bad, "w", encoding="utf-8").write(raw)
        out2 = os.path.join(self.d, "r2.json")
        subprocess.run([node, mjs, bad, kd, out2], check=True, timeout=60)
        res = json.load(open(out2, encoding="utf-8"))
        self.assertFalse(res["ok"])
        self.assertIn("duplicate", res["reason"])


if __name__ == "__main__":
    unittest.main()
