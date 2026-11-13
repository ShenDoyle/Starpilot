"""PROJECT-04 P2-2-4R 强制验收矩阵 NR01-NR12：Ego Node 原生验签集成闭环。

设计要点（对应任务书 §3）：
- 每条用例都经过 **真实 ExecutionGate.dispatch → 五道屏障 → backend** 边界；
- 后端为可计数 mock（backend 调用次数逐条留证）；
- Node 验签次数单独计数（spy 包住 envelope_node.verify_via_node，仍走真实实现）；
- **使用实际 Ego Node v24.22.1**（`ego-browser nodejs`，不是系统 node）；
- 凡是「Node 必须拒」的用例，都同时把 **Python 镜像验签打成恒通过**，
  以证明拦截确实来自 Node 原生验签，而不是镜像先把关（NR11 的旁路禁令）。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TESTS_DIR)
sys.path.insert(0, os.path.join(TESTS_DIR, "..", "scripts"))

import actionledger as AL  # noqa: E402
import dispatch_marker as DM  # noqa: E402
import envelope  # noqa: E402
import envelope_node  # noqa: E402
import execution_gate as EG  # noqa: E402
import p2_plane  # noqa: E402
import pageidentity  # noqa: E402
import procidentity  # noqa: E402
import sp_errors as SP  # noqa: E402

REPO = os.path.abspath(os.path.join(TESTS_DIR, ".."))
IDENT = {"url": "data:x", "title": "T", "marker": "M", "timeOrigin": 1.0}
TARGET = "order:nr"          # reserve 时使用的原始业务对象
TARGET_NORM = "order:nr"     # normalize 后（strip+NFC+casefold，此处不变）


def _ego_available() -> bool:
    return os.path.isfile(envelope_node.ego_exe())


class Base(unittest.TestCase):
    def setUp(self):
        if not _ego_available():
            self.skipTest("ego node runtime not available: %s"
                          % envelope_node.ego_exe())
        self.d = tempfile.mkdtemp(prefix="p2nr-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.lock_path = os.path.join(self.d, "gate.lock")
        self.ledger = AL.ActionLedger(os.path.join(self.d, "ledger.sqlite"))
        self.addCleanup(self.ledger.close)
        self.pi = pageidentity.PageIdentityManager(os.path.join(self.d, "pages.json"))
        self.plane = p2_plane.make_plane(self.d, worker_id="w-nr")
        self.gate = EG.ExecutionGate(
            self.lock_path, self.ledger, self.pi,
            ownership_probe=mock.Mock(return_value="agent"),
            resource="ts32", plane=self.plane)
        self.ctx = self._ctx()
        self.node_calls = []      # [(envelope_dict, key_dir)]
        self.verified = []        # Node 实际验签过的 envelope

    # ---- 夹具 ----
    def _ctx(self):
        ex = procidentity.executor_id(run_id="nr")
        self.pi.bind("ts32|p1", IDENT, ex, 0, "bind")
        ac = self.gate.acquire({"executor_id": ex, "run_id": "nr"})
        self.assertFalse(ac["ok"] is False, ac)
        self.lease = ac["data"]["lease"]
        gen = ac["data"]["fencing_generation"]
        self.pi.bind("ts32|p1", IDENT, ex, gen, "bind")
        return {"executor_id": ex, "run_id": "nr", "fencing_generation": gen,
                "page_generation": self.pi.status("ts32|p1").get("page_generation", "")}

    def reserve(self, task="t-nr", target=TARGET):
        r = self.ledger.reserve(task, {"action": "send", "n": task},
                                budget_units=10, business_target=target,
                                side_effect=True)
        self.assertTrue(r["ok"], r)
        return r["data"]["action_id"]

    def countable_backend(self):
        calls = []

        def backend(op, args, ctx):
            calls.append({"op": op, "args": dict(args), "ctx": dict(ctx)})
            return {"ok": True, "result": "mock-post"}

        return backend, calls

    def spy_node(self):
        real = envelope_node.verify_via_node

        def spy(env, key_dir, timeout=30.0):
            self.node_calls.append((dict(env), key_dir))
            out = real(env, key_dir, timeout=timeout)
            if out.get("ok"):
                self.verified.append(dict(env))
            return out

        return mock.patch.object(envelope_node, "verify_via_node", side_effect=spy)

    def dispatch(self, aid, backend, op="send"):
        return self.gate.dispatch(self.ctx, op, backend, {"x": 1},
                                 taskspace_id=32, page_key="ts32|p1",
                                 identity_tuple=IDENT, action_id=aid)

    def assert_no_evidence(self, aid):
        """失败路径不得留下任何「可能已派发」证据。"""
        path = DM.marker_path(self.plane.dispatching, aid)
        self.assertFalse(os.path.exists(path), "marker must not exist")
        chk = self.ledger.check_delegated(aid, {"op_id": aid})
        self.assertEqual(chk.get("state"), "ABSENT", chk)
        row = self.ledger.get(aid)
        self.assertEqual(row["state"], "RESERVED", row)
        self.assertEqual(row["dispatch_count"], 0, row)

    def tamper(self, mutate, mirror_ok=True):
        """签名后篡改 envelope；同时把 Python 镜像打成恒通过（只有 Node 能拦）。"""
        real = envelope.build_and_sign

        def build(*a, **k):
            env = real(*a, **k)
            mutate(env)
            return env

        patches = [mock.patch.object(envelope, "build_and_sign", side_effect=build)]
        if mirror_ok:
            patches.append(mock.patch.object(
                envelope, "verify_envelope",
                return_value={"ok": True, "code": "", "reason": ""}))
        return patches


# ============================================================ NR01 正常验签
class TestNR01HappyPath(Base):
    def test_nr01_node_verified_then_single_backend_call(self):
        backend, calls = self.countable_backend()
        aid = self.reserve()
        with self.spy_node() as spy:
            r = self.dispatch(aid, backend)
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(calls), 1, "backend 必须恰一次")
        self.assertEqual(spy.call_count, 1, "Node 验签必须恰一次")
        self.assertEqual(len(self.verified), 1)
        rt = r["data"].get("outcome")
        self.assertTrue(rt)
        # 运行时必须是 Ego 内置 Node v24（不是系统 node v22）
        self.assertRegex(self.ctx_received(calls)["node_runtime"], r"^24\.")
        # 证据齐全：marker + DELEGATED 已落
        self.assertTrue(os.path.exists(DM.marker_path(self.plane.dispatching, aid)))
        chk = self.ledger.check_delegated(aid, {"op_id": aid})
        self.assertEqual(chk["state"], "PRESENT", chk)

    @staticmethod
    def ctx_received(calls):
        return calls[0]["ctx"]


# ============================================================ NR02-NR07 篡改/错钥
class TestNRTamperRejected(Base):
    def _expect_rejected(self, mutate, stage_hint=None):
        backend, calls = self.countable_backend()
        aid = self.reserve()
        patches = self.tamper(mutate)
        with patches[0], patches[1], self.spy_node():
            r = self.dispatch(aid, backend)
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["code"], SP.SP_OP_001, r)
        self.assertEqual(r["data"].get("stage"), "node_verify", r)
        self.assertEqual(calls, [], "backend 不得被调用")
        self.assertEqual(len(self.verified), 0, "不得有任何 envelope 通过验签")
        self.assert_no_evidence(aid)
        if stage_hint:
            self.assertEqual(r["data"].get("node_stage"), stage_hint, r)
        return r

    def test_nr02_wrong_public_key(self):
        # 把 **磁盘上的公钥** 换成另一把（Node 侧从注册表目录读公钥），
        # 同时把 Python 镜像打成恒通过 → 拦截只能来自 Node 原生验签。
        other = os.path.join(self.d, "other-keys")
        envelope.ensure_keypair(other, "1")
        other_pem = open(os.path.join(other, "v1", "public.pem"),
                         encoding="utf-8").read()
        target = os.path.join(self.plane.key_dir, "v1", "public.pem")
        orig = open(target, encoding="utf-8").read()

        def restore():
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(orig)

        self.addCleanup(restore)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(other_pem)

        backend, calls = self.countable_backend()
        aid = self.reserve()
        with mock.patch.object(envelope, "verify_envelope",
                               return_value={"ok": True, "code": "", "reason": ""}), \
                self.spy_node():
            r = self.dispatch(aid, backend)
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["code"], SP.SP_OP_001, r)
        self.assertEqual(r["data"].get("stage"), "node_verify", r)
        self.assertEqual(r["data"].get("node_stage"), "verify_rejected", r)
        self.assertEqual(calls, [])
        self.assert_no_evidence(aid)

    def test_nr03_tampered_signature(self):
        def mutate(env):
            sig = env["signature"]
            env["signature"] = ("0" if sig[0] != "0" else "1") + sig[1:]
        self._expect_rejected(mutate, "verify_rejected")

    def test_nr04_tampered_envelope_digest(self):
        def mutate(env):
            d = env["envelope_digest"]
            env["envelope_digest"] = ("0" if d[0] != "0" else "1") + d[1:]
        self._expect_rejected(mutate, "verify_rejected")

    def test_nr05_tampered_fencing(self):
        self._expect_rejected(lambda env: env.__setitem__("fencing", "999"),
                              "verify_rejected")

    def test_nr06_tampered_worker_id(self):
        self._expect_rejected(lambda env: env.__setitem__("worker_id", "w-evil"),
                              "verify_rejected")

    def test_nr07_wrong_key_version(self):
        # key_version 改成注册表里不存在的版本。注意：Python 侧取公钥会先失败，
        # 这里把 Python 取钥/镜像都打成「用 v1 公钥恒通过」，逼到 Node 侧
        # 由注册表判定该 key_version 非法 → 拒绝。
        v1_pem = envelope.public_key_pem(self.plane.key_dir, "1")
        backend, calls = self.countable_backend()
        aid = self.reserve()
        patches = self.tamper(lambda env: env.__setitem__("key_version", "9"),
                              mirror_ok=False)
        with patches[0], \
                mock.patch.object(envelope, "public_key_pem",
                                  return_value=v1_pem), \
                mock.patch.object(envelope, "verify_envelope",
                                  return_value={"ok": True, "code": "", "reason": ""}), \
                self.spy_node():
            r = self.dispatch(aid, backend)
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["code"], SP.SP_OP_001, r)
        self.assertEqual(r["data"].get("stage"), "node_verify", r)
        self.assertEqual(r["data"].get("node_stage"), "verify_rejected", r)
        self.assertEqual(calls, [])
        self.assert_no_evidence(aid)

    def test_nr07b_unknown_algorithm(self):
        self._expect_rejected(lambda env: env.__setitem__("algorithm", "rsa"),
                              "verify_rejected")


# ============================================================ NR08-NR10 运行时故障
class TestNRRuntimeFailures(Base):
    def test_nr08_node_unavailable_fail_closed(self):
        backend, calls = self.countable_backend()
        aid = self.reserve()
        with mock.patch.dict(os.environ,
                             {"STARPILOT_EGO_EXE": os.path.join(self.d, "nope.exe")}):
            r = self.dispatch(aid, backend)
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["code"], SP.SP_OP_001, r)
        self.assertEqual(r["data"].get("node_stage"), "runtime_unavailable", r)
        self.assertEqual(calls, [])
        self.assert_no_evidence(aid)

    def test_nr09_node_nonzero_exit_fail_closed(self):
        # 用 python 解释器冒充 ego 运行时：`<exe> nodejs` 非零退出且无结果
        env_override = {"STARPILOT_EGO_EXE": sys.executable}
        backend, calls = self.countable_backend()
        aid = self.reserve()
        with mock.patch.dict(os.environ, env_override):
            r = self.dispatch(aid, backend)
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["code"], SP.SP_OP_001, r)
        self.assertIn(r["data"].get("node_stage"),
                      ("result_unparseable", "node_nonzero_exit"), r)
        self.assertEqual(calls, [])
        self.assert_no_evidence(aid)

    def test_nr10_corrupted_output_fail_closed(self):
        backend, calls = self.countable_backend()
        aid = self.reserve()
        bad = mock.Mock(returncode=0, stdout="SP_NODE_VERIFY_RESULT {not-json",
                        stderr="")
        with mock.patch.object(envelope_node.subprocess, "run", return_value=bad):
            r = self.dispatch(aid, backend)
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["code"], SP.SP_OP_001, r)
        self.assertEqual(r["data"].get("node_stage"), "result_unparseable", r)
        self.assertEqual(calls, [])
        self.assert_no_evidence(aid)

    def test_nr10b_runtime_not_in_allowlist(self):
        # 运行时白名单**严格**：24.22.2（同主版本、不同补丁）也不放行。
        backend, calls = self.countable_backend()
        aid = self.reserve()
        captured = {}
        real = envelope.build_and_sign

        def build(*a, **k):
            env = real(*a, **k)
            captured.update(env)
            return env

        def fake_run(*a, **k):
            payload = {"ok": True, "code": "", "reason": "", "runtime": "24.22.2",
                       "nonce": "n-fixed", "digest": captured["envelope_digest"]}
            return mock.Mock(returncode=0, stdout="",
                             stderr="SP_NODE_VERIFY_RESULT " + json.dumps(payload))

        with mock.patch.object(envelope, "build_and_sign", side_effect=build), \
                mock.patch.object(envelope_node.secrets, "token_hex",
                                  return_value="n-fixed"), \
                mock.patch.object(envelope_node.subprocess, "run",
                                  side_effect=fake_run):
            r = self.dispatch(aid, backend)
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["data"].get("node_stage"), "runtime_untrusted", r)
        self.assertEqual(calls, [])
        self.assert_no_evidence(aid)

    def test_nr10c_untrusted_major_version_rejected(self):
        backend, calls = self.countable_backend()
        aid = self.reserve()
        captured = {}
        real = envelope.build_and_sign

        def build(*a, **k):
            env = real(*a, **k)
            captured.update(env)
            return env

        def fake_run(*a, **k):
            payload = {"ok": True, "code": "", "reason": "", "runtime": "18.0.0",
                       "nonce": "n-fixed", "digest": captured["envelope_digest"]}
            return mock.Mock(returncode=0, stdout="",
                             stderr="SP_NODE_VERIFY_RESULT " + json.dumps(payload))

        with mock.patch.object(envelope, "build_and_sign", side_effect=build), \
                mock.patch.object(envelope_node.secrets, "token_hex",
                                  return_value="n-fixed"), \
                mock.patch.object(envelope_node.subprocess, "run",
                                  side_effect=fake_run):
            r = self.dispatch(aid, backend)
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["data"].get("node_stage"), "runtime_untrusted", r)
        self.assertEqual(calls, [])


class TestNRResultFreshness(Base):
    """P2-2-4R 补充核验 ②：验签结果必须与**本次调用**绑定。"""

    def _fake(self, nonce_value, digest_value):
        captured = {}
        real = envelope.build_and_sign

        def build(*a, **k):
            env = real(*a, **k)
            captured.update(env)
            return env

        def fake_run(*a, **k):
            payload = {"ok": True, "code": "", "reason": "", "runtime": "24.22.1",
                       "nonce": nonce_value, "digest": digest_value(captured)}
            return mock.Mock(returncode=0, stdout="",
                             stderr="SP_NODE_VERIFY_RESULT " + json.dumps(payload))

        return build, fake_run

    def test_nr13_stale_nonce_rejected(self):
        # 旧文件/串号结果：nonce 不是本次的 → 拒绝（即使 ok:true、运行时合规）
        backend, calls = self.countable_backend()
        aid = self.reserve()
        build, fake_run = self._fake("stale-nonce",
                                     lambda c: c["envelope_digest"])
        with mock.patch.object(envelope, "build_and_sign", side_effect=build), \
                mock.patch.object(envelope_node.secrets, "token_hex",
                                  return_value="current-nonce"), \
                mock.patch.object(envelope_node.subprocess, "run",
                                  side_effect=fake_run):
            r = self.dispatch(aid, backend)
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["data"].get("node_stage"), "result_not_bound", r)
        self.assertEqual(calls, [])
        self.assert_no_evidence(aid)

    def test_nr15_digest_binding_mismatch_rejected(self):
        # nonce 对，但结果声称的 digest 不是本次 envelope → 拒绝
        backend, calls = self.countable_backend()
        aid = self.reserve()
        build, fake_run = self._fake("current-nonce", lambda c: "0" * 64)
        with mock.patch.object(envelope, "build_and_sign", side_effect=build), \
                mock.patch.object(envelope_node.secrets, "token_hex",
                                  return_value="current-nonce"), \
                mock.patch.object(envelope_node.subprocess, "run",
                                  side_effect=fake_run):
            r = self.dispatch(aid, backend)
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["data"].get("node_stage"), "result_not_bound", r)
        self.assertEqual(calls, [])
        self.assert_no_evidence(aid)

    def test_nr14_preexisting_result_file_rejected(self):
        # 调用目录里已有结果文件（半成品/上次遗留）→ 直接拒绝，绝不采信
        backend, calls = self.countable_backend()
        aid = self.reserve()
        stale_dir = os.path.join(self.d, "stale-dir")
        os.makedirs(stale_dir)
        with open(os.path.join(stale_dir, "result.json"), "w", encoding="utf-8") as fh:
            json.dump({"ok": True, "code": "", "reason": "", "runtime": "24.22.1",
                       "nonce": "x", "digest": "y"}, fh)
        with mock.patch.object(envelope_node.tempfile, "mkdtemp",
                               return_value=stale_dir):
            r = self.dispatch(aid, backend)
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["data"].get("node_stage"), "stale_result_present", r)
        self.assertEqual(calls, [])
        self.assert_no_evidence(aid)

    def test_nr16_nonce_is_fresh_per_call(self):
        # 每次调用都生成新的 nonce（结果绑定不可能被跨调用复用）
        seen = []
        real = envelope_node.secrets.token_hex

        def spy(n=16):
            v = real(n)
            seen.append(v)
            return v

        backend, calls = self.countable_backend()
        with mock.patch.object(envelope_node.secrets, "token_hex", side_effect=spy), \
                self.spy_node():
            for i in range(2):
                aid = self.reserve(task="t-nr16-%d" % i,
                                   target="order:nr16-%d" % i)
                r = self.dispatch(aid, backend)
                self.assertTrue(r["ok"], r)
        self.assertEqual(len(seen), 2)
        self.assertNotEqual(seen[0], seen[1], "nonce 必须每次随机")
        self.assertEqual(len(calls), 2)


# ============================================================ NR11-NR12 旁路与一致性
class TestNRBypassAndConsistency(Base):
    def test_nr11_no_python_mirror_bypass_on_retry(self):
        # 第一次：Node 拒绝（签名被篡改）→ 不派发、无证据
        def mutate(env):
            env["signature"] = ("f" if env["signature"][0] != "f" else "e") + env["signature"][1:]
        backend, calls = self.countable_backend()
        aid = self.reserve()
        patches = self.tamper(mutate)
        with patches[0], patches[1], self.spy_node():
            r1 = self.dispatch(aid, backend)
        self.assertFalse(r1["ok"], r1)
        self.assertEqual(calls, [])
        self.assert_no_evidence(aid)
        n_after_first = len(self.node_calls)
        self.assertEqual(n_after_first, 1)
        # 第二次：同一 action 再派发（仍篡改）→ 仍必须被 Node 拒，
        # 不得因为「Python 镜像恒通过」而放行（无镜像旁路）。
        patches2 = self.tamper(mutate)
        with patches2[0], patches2[1], self.spy_node():
            r2 = self.dispatch(aid, backend)
        self.assertFalse(r2["ok"], r2)
        self.assertEqual(r2["code"], SP.SP_OP_001, r2)
        self.assertEqual(calls, [], "第二次仍不得触达 backend")
        self.assertEqual(len(self.node_calls), 2, "Node 必须再次被调用（不缓存通过）")
        self.assert_no_evidence(aid)

    def test_nr12_verified_object_equals_executed_object(self):
        backend, calls = self.countable_backend()
        aid = self.reserve()
        with self.spy_node():
            r = self.dispatch(aid, backend)
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(calls), 1)
        executed = calls[0]["ctx"]["envelope"]          # backend 实际收到的
        verified = self.verified[0]                     # Node 实际验签的
        self.assertEqual(executed, verified, "验签对象与执行对象必须完全一致")
        # digest 与规范化字节自洽（只对受保护字段求规范化）
        import hashlib
        protected = {k: executed[k] for k in envelope.PROTECTED_FIELDS}
        self.assertEqual(executed["envelope_digest"],
                         hashlib.sha256(envelope.canonical_bytes(protected)).hexdigest())
        # 绑定字段与本次派发一致
        self.assertEqual(executed["op_id"], aid)
        self.assertEqual(executed["fencing"], str(self.ctx["fencing_generation"]))
        self.assertEqual(executed["worker_id"], self.plane.worker_id)
        self.assertEqual(executed["business_target_norm"], TARGET_NORM)
        self.assertEqual(calls[0]["ctx"]["envelope_digest"], executed["envelope_digest"])
        self.assertRegex(calls[0]["ctx"]["node_runtime"], r"^24\.")
        # 证据与签名绑定：marker / DELEGATED 的 digest 与 envelope 一致
        mk = DM.read_marker(DM.marker_path(self.plane.dispatching, aid))
        self.assertEqual(mk["envelope_digest"], executed["envelope_digest"])
        dg = self.ledger.check_delegated(aid, {"op_id": aid})
        self.assertEqual(dg["event"]["envelope_digest"], executed["envelope_digest"])
        self.assertEqual(dg["event"]["worker_id"], self.plane.worker_id)

    def test_nr12b_no_bypass_switch_in_source(self):
        """P2-2-4R 纪律：不存在「跳过 Node 验签」的开关或旁路分支。"""
        src = open(os.path.join(REPO, "scripts", "execution_gate.py"),
                   encoding="utf-8").read()
        self.assertIn("envelope_node.verify_via_node(", src)
        for bad in ("skip_node", "SKIP_NODE", "node_verify=False",
                    "bypass_node", "verify_node=False"):
            self.assertNotIn(bad, src)
        node_src = open(os.path.join(REPO, "scripts", "envelope_node.py"),
                        encoding="utf-8").read()
        for bad in ("skip", "bypass"):
            self.assertNotIn("def " + bad, node_src)


if __name__ == "__main__":
    unittest.main()
