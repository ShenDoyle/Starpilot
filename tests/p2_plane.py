"""P2-2-4 测试辅助：临时 SecurityPlane（测试密钥/临时 ops 目录，零入库）。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import envelope  # noqa: E402
import execution_gate  # noqa: E402


def make_plane(base_dir: str, worker_id: str = "w-test") -> execution_gate.SecurityPlane:
    kd = os.path.join(base_dir, "keys")
    envelope.ensure_keypair(kd, "1")
    envelope.write_registry(kd, "1", ["1"])
    return execution_gate.SecurityPlane(
        key_dir=kd, worker_id=worker_id,
        ops_root=os.path.join(base_dir, "ops"))
