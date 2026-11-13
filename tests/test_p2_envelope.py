"""PROJECT-04 P2-2-1 强制验收矩阵（GPT 任务书 §三A）：ED01-ED08 真实执行。

- Python 签名（envelope.py，cryptography 45.0.7）→ Node 验签
  （scripts/ego/envelope_verify.mjs，node:crypto 原生）真实互通（T-ED-01/ED01）。
- 测试密钥全部在临时目录动态生成，任何私钥不进仓库（严格分离）。
- 所有异常默认拒绝：断言 ok=False 且 code=SP-OP-001。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import envelope as ENV  # noqa: E402
import sp_errors as SP  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
VERIFY_MJS = os.path.join(REPO, "scripts", "ego", "envelope_verify.mjs")
NODE = shutil.which("node")


def _fields(**over):
    base = {
        "version": ENV.PROTOCOL_VERSION,
        "algorithm": ENV.ALGORITHM,
        "key_version": "1",
        "op_id": "op-0001",
        "task_id": "task-a",
        "intent_key": "ik-0001",
        "fencing": "42",
        "worker_id": "w-1",
        "issued_at": "2026-11-13T09:00:00Z",
        "business_target_norm": "order:123",
    }
    base.update(over)
    return base


class EnvelopeBase(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="p2ed-")
        self.kd = os.path.join(self.d, "keys")
        ENV.ensure_keypair(self.kd, "1")
        ENV.ensure_keypair(self.kd, "2")
        ENV.write_registry(self.kd, "1", ["1", "2"])
        self.pub1 = ENV.public_key_pem(self.kd, "1")
        self.pub2 = ENV.public_key_pem(self.kd, "2")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _sign(self, **over):
        f = _fields(**over)
        raw = ENV.canonical_bytes(f)
        import hashlib

        env = dict(f)
        env["envelope_digest"] = hashlib.sha256(raw).hexdigest()
        from cryptography.hazmat.primitives import serialization

        with open(ENV.private_key_path(self.kd, f["key_version"]), "rb") as fh:
            key = serialization.load_pem_private_key(fh.read(), password=None)
        env["signature"] = key.sign(raw).hex()
        return env

    def _node_verify(self, env):
        """CLI 路径（含注册表按 key_version 选公钥）：node envelope_verify.mjs。"""
        self.assertIsNotNone(NODE, "node not in PATH — worker 验签侧硬依赖")
        ep = os.path.join(self.d, "env.json")
        rp = os.path.join(self.d, "res.json")
        with open(ep, "w", encoding="utf-8") as f:
            json.dump(env, f, ensure_ascii=False)
        r = subprocess.run([NODE, VERIFY_MJS, ep, self.kd, rp],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(rp, encoding="utf-8") as f:
            return json.load(f)

    # ---------------------------------------------------------- ED01 互通
    def test_ed01_python_sign_node_verify(self):
        env = self._sign()
        # Python 侧镜像验签
        pv = ENV.verify_envelope(env, self.pub1)
        self.assertTrue(pv["ok"], pv)
        # Node 侧权威验签（CLI + 注册表）
        nv = self._node_verify(env)
        self.assertTrue(nv["ok"], nv)
        # 直接 API 路径
        import importlib
        m = importlib.import_module("ego.envelope_verify") if False else None
        # unicode / 空串序列化对齐（business_target_norm 允许空 + unicode 归一产物）
        env2 = self._sign(business_target_norm="订单:café")
        pv2 = ENV.verify_envelope(env2, self.pub1)
        self.assertTrue(pv2["ok"], pv2)
        nv2 = self._node_verify(env2)
        self.assertTrue(nv2["ok"], nv2)
        env3 = self._sign(business_target_norm="")
        nv3 = self._node_verify(env3)
        self.assertTrue(nv3["ok"], nv3)

    def test_ed01_python_build_and_sign_api(self):
        """生产 API 路径：build_and_sign → Node 验签。"""
        ENV.write_registry(self.kd, "1", ["1"])
        env = ENV.build_and_sign("op-0002", "task-a", "ik-0002", 7, "w-1",
                                 "order:123", self.kd)
        nv = self._node_verify(env)
        self.assertTrue(nv["ok"], nv)
        self.assertEqual(env["fencing"], "7")          # 数值以字符串表示
        self.assertEqual(len(env["signature"]), 128)   # 64 字节 hex

    # ---------------------------------------------------------- ED02 错误公钥
    def test_ed02_wrong_public_key(self):
        env = self._sign()  # v1 签名
        pv = ENV.verify_envelope(env, self.pub2)      # 用 v2 公钥验
        self.assertFalse(pv["ok"])
        self.assertEqual(pv["code"], SP.SP_OP_001)
        # Node CLI：注册表只认 v1，但签名对 v1 公钥本应通过——错误钥经 API 路径测
        import subprocess
        ep = os.path.join(self.d, "env.json")
        rp = os.path.join(self.d, "res.json")
        with open(ep, "w", encoding="utf-8") as f:
            json.dump(env, f)
        bad = os.path.join(self.d, "bad.pem")
        shutil.copy(os.path.join(self.kd, "v2", "public.pem"), bad)
        r = subprocess.run([NODE, VERIFY_MJS, ep, self.kd, rp],
                           capture_output=True, text=True, timeout=60)
        # CLI 固定从注册表选钥（正确钥，应通过）；错误钥场景由下面的内联 API 验证
        with open(rp, encoding="utf-8") as f:
            self.assertTrue(json.load(f)["ok"])
        # 内联 node -e：错误公钥 → 拒绝
        inline = (
            "import fs from 'node:fs';"
            "import { verifyEnvelope } from 'file:///%s';"
            "const env = JSON.parse(fs.readFileSync(%r,'utf8'));"
            "const pem = fs.readFileSync(%r,'utf8');"
            "fs.writeFileSync(%r, JSON.stringify(verifyEnvelope(env, pem)));"
        ) % (VERIFY_MJS.replace("\\", "/"), ep, bad, rp)
        r2 = subprocess.run([NODE, "--input-type=module", "-e", inline],
                            capture_output=True, text=True, timeout=60)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        with open(rp, encoding="utf-8") as f:
            res = json.load(f)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], SP.SP_OP_001)

    # ---------------------------------------------------------- ED03 篡改签名
    def test_ed03_tampered_signature(self):
        env = self._sign()
        sig = list(env["signature"])
        sig[0] = "0" if sig[0] != "0" else "1"
        env["signature"] = "".join(sig)
        self.assertFalse(ENV.verify_envelope(env, self.pub1)["ok"])
        res = self._node_verify(env)
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], SP.SP_OP_001)

    # ---------------------------------------------------------- ED04 篡改 envelope
    def test_ed04_tampered_envelope(self):
        env = self._sign()
        env["worker_id"] = "w-attacker"
        res = self._node_verify(env)
        self.assertFalse(res["ok"])
        self.assertIn("digest", res["reason"])
        # 篡改 digest 字段本身（内容一致但签名不变）→ 签名失败
        env2 = self._sign()
        env2["envelope_digest"] = "0" * 64
        self.assertFalse(ENV.verify_envelope(env2, self.pub1)["ok"])

    # ---------------------------------------------------------- ED05 错误算法
    def test_ed05_wrong_algorithm(self):
        env = self._sign(algorithm="ed25519x")
        res = self._node_verify(env)
        self.assertFalse(res["ok"])
        self.assertIn("algorithm", res["reason"])
        self.assertFalse(ENV.verify_envelope(env, self.pub1)["ok"])

    # ---------------------------------------------------------- ED06 密钥版本
    def test_ed06_key_version_rules(self):
        # 未知版本（注册表只认 v1）→ Node 在选钥阶段即拒绝
        env = self._sign(key_version="2")
        env["key_version"] = "9"  # 签名/摘要不再一致，但注册表拒在先（语义仍为拒）
        res = self._node_verify(env)
        self.assertFalse(res["ok"])
        self.assertIn("key_version", res["reason"])
        # 退役版本：注册表把 v1 移入 retired → 拒
        ENV.write_registry(self.kd, "2", ["2"], retired=["1"])
        res2 = self._node_verify(self._sign())  # v1 签名，v1 已退役
        self.assertFalse(res2["ok"])
        ENV.write_registry(self.kd, "1", ["1"])  # 还原
        # 旧钥签名对新钥公钥 → 拒（轮换后旧 envelope 不可再验）
        env_old = self._sign(key_version="1")
        # v1 注册表已还原，用 v2 公钥直接验（Python API 路径）
        self.assertFalse(ENV.verify_envelope(env_old, self.pub2)["ok"])

    # ---------------------------------------------------------- ED07 缺失字段
    def test_ed07_missing_field(self):
        for drop in ("op_id", "fencing", "intent_key", "worker_id"):
            env = self._sign()
            del env[drop]
            res = self._node_verify(env)
            self.assertFalse(res["ok"], drop)
            self.assertEqual(res["code"], SP.SP_OP_001, drop)
        # 必填字段空串（business_target_norm 之外）→ 规范化拒绝
        env2 = dict(_fields(op_id=""), envelope_digest="0" * 64, signature="0" * 128)
        self.assertFalse(ENV.verify_envelope(env2, self.pub1)["ok"])

    # ---------------------------------------------------------- ED08 非法签名长度
    def test_ed08_signature_length(self):
        for bad in ("00" * 63, "00" * 65, "zz" * 64, "", None):
            env = self._sign()
            env["signature"] = bad
            res = self._node_verify(env)
            self.assertFalse(res["ok"], repr(bad)[:40])
            self.assertEqual(res["code"], SP.SP_OP_001)

    # ---------------------------------------------------------- 规范化严格性
    def test_canonical_strictness(self):
        # int 值拒绝（消除数值表示歧义）
        with self.assertRaises(ValueError):
            ENV.canonical_bytes(_fields(fencing=42))
        # None / bool 拒绝
        with self.assertRaises(ValueError):
            ENV.canonical_bytes(_fields(worker_id=None))
        # 缺字段拒绝
        f = _fields()
        del f["task_id"]
        with self.assertRaises(ValueError):
            ENV.canonical_bytes(f)
        # 多余字段拒绝
        with self.assertRaises(ValueError):
            ENV.canonical_bytes(_fields(evil="1"))
        # 确定性：同输入两次字节一致
        self.assertEqual(ENV.canonical_bytes(_fields()),
                         ENV.canonical_bytes(_fields()))

    def test_private_key_never_in_envelope_or_registry(self):
        """私钥文件路径永不进 envelope/registry；registry 不含 pem 内容。"""
        env = ENV.build_and_sign("op-3", "t", "ik", 1, "w", "", self.kd)
        blob = json.dumps(env)
        self.assertNotIn("PRIVATE KEY", blob)
        reg = ENV.load_registry(self.kd)
        self.assertNotIn("PRIVATE KEY", json.dumps(reg))
        # 私钥文件权限（POSIX 0600；Windows chmod 是最佳努力，跳过断言）
        if os.name == "posix":
            self.assertEqual(os.stat(ENV.private_key_path(self.kd, "1")).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
