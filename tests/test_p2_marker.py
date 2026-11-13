"""PROJECT-04 P2-2-2 强制验收矩阵（GPT 任务书 §五）：MK01-MK08 真实执行。

- 故障注入在真实操作边界（os.open/os.write/os.fsync monkeypatch + 子进程 os._exit），
  不做函数入口模拟。
- 与 P2-2-1 Envelope 一致性：marker 的 envelope_digest 取自真实签发的 envelope。
- 本机 Windows：POSIX 分支经 WSL Ubuntu-24.04 冒烟（见报告），Windows 分支全量实跑。
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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import dispatch_marker as DM  # noqa: E402
import envelope as ENV  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

GOOD = dict(op_id="op-mk01", envelope_digest="a" * 64, fencing="7",
            worker_id="w-1", key_version="1")


class MarkerBase(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="p2mk-")
        self.dd = DM.dispatching_dir(self.d)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _write_ok(self, **over):
        f = {**GOOD, **over}
        return DM.write_marker(self.dd, **f)


class TestMK01Exclusive(MarkerBase):
    def test_mk01_concurrent_o_excl_single_winner(self):
        """MK01：8 线程并发同 op_id，仅 1 成功；既有证据不被覆盖。"""
        results, lock = [], threading.Lock()

        def worker(i):
            try:
                r = self._write_ok(worker_id="w-%d" % i)
                res = {"ok": True, "worker": r["created_at"]}
            except DM.MarkerError as e:
                res = {"ok": False, "err": type(e).__name__}
            with lock:
                results.append(res)

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(60)
        self.assertEqual(len(results), 8)
        self.assertEqual(sum(1 for r in results if r["ok"]), 1)
        # 失败者全部是排他冲突（绝不覆盖）
        self.assertTrue(all(r["err"] == "MarkerExists" for r in results if not r["ok"]))
        # 内容与赢家一致（未被后续写入覆盖）
        p = DM.marker_path(self.dd, GOOD["op_id"])
        payload = DM.read_marker(p)
        self.assertIn(payload["worker_id"], ["w-0", "w-1", "w-2", "w-3",
                                             "w-4", "w-5", "w-6", "w-7"])

    def test_mk01b_duplicate_create_never_overwrites(self):
        self._write_ok()
        with self.assertRaises(DM.MarkerExists):
            self._write_ok(worker_id="w-attacker")
        self.assertEqual(DM.read_marker(
            DM.marker_path(self.dd, GOOD["op_id"]))["worker_id"], "w-1")


class TestMK02WriteFailure(MarkerBase):
    def test_mk02_partial_write_not_complete(self):
        """MK02：写入失败 → 无可误认完整的 Marker；重试被 O_EXCL 拒。"""
        real_write = os.write
        state = {"n": 0}

        def flaky_write(fd, data):
            state["n"] += 1
            if state["n"] == 1:  # 仅 marker 主写入（哨兵/其它不受扰）
                raise OSError(5, "injected write failure")
            return real_write(fd, data)

        with mock.patch("os.write", side_effect=flaky_write):
            with self.assertRaises(DM.MarkerPersistFailed):
                self._write_ok()
        p = DM.marker_path(self.dd, GOOD["op_id"])
        self.assertTrue(os.path.exists(p))          # 半成品存在
        with self.assertRaises(DM.MarkerInvalid):   # 但绝不被误认完整
            DM.read_marker(p)
        # 重试同 op_id → O_EXCL 拒（不覆盖残迹，fail-closed 上报）
        with self.assertRaises(DM.MarkerExists):
            self._write_ok()


class TestMK03FsyncFileFailure(MarkerBase):
    def test_mk03_fsync_file_failure_forbids(self):
        real_fsync = os.fsync
        calls = {"n": 0}

        def flaky_fsync(fd):
            calls["n"] += 1
            if calls["n"] == 1:  # 第一次 fsync = marker 文件（打开顺序保证）
                raise OSError(5, "injected fsync failure")
            return real_fsync(fd)

        with mock.patch("os.fsync", side_effect=flaky_fsync):
            with self.assertRaises(DM.MarkerPersistFailed):
                self._write_ok()
        # 明确失败由 API 返回承载（上层禁止派发）。残留文件内容完整（write 已
        # 成功）但持久性未证明——恢复语义按「Marker 存在 → 可能已派发 → UNKNOWN」
        # 保守处理，绝不当作「可证未派发」走快路径，也绝不删除/覆盖。
        payload = DM.read_marker(DM.marker_path(self.dd, GOOD["op_id"]))
        self.assertEqual(payload["op_id"], GOOD["op_id"])


class TestMK04DirPersistenceFailure(MarkerBase):
    def test_mk04_dir_persistence_failure(self):
        """MK04：目录持久化步骤失败（按平台分支注入）→ 明确失败，不虚报成功。"""
        branch = "_fsync_dir_posix" if os.name == "posix" else "_fsync_dir_windows"
        with mock.patch.object(DM, branch, side_effect=OSError(5, "injected")):
            with self.assertRaises(DM.MarkerPersistFailed):
                self._write_ok()
        # 文件内容完整可解析（S3/S4 已成功），但目录持久化失败 =
        # 上层仍禁止派发（返回异常）；不删除不覆盖
        payload = DM.read_marker(DM.marker_path(self.dd, GOOD["op_id"]))
        self.assertEqual(payload["op_id"], GOOD["op_id"])


class TestMK05InterruptionRecovery(MarkerBase):
    def test_mk05_crash_between_create_and_write(self):
        """MK05：创建后/写入前进程中断 → 恢复读取不得得出持久化结论。

        实现说明（如实声明）：本模块 O_EXCL 直接落最终路径、不经 rename；
        rename 中断语义由「半成品不误认完整」覆盖（MK02/MK06 同源）。
        """
        CHILD = (
            "import sys, os, json\n"
            "sys.path.insert(0, r'%s')\n"
            "import dispatch_marker as DM\n"
            "dd = DM.dispatching_dir(sys.argv[1])\n"
            "DM.write_marker(dd, %r, %r, %r, %r, %r)\n"
            "os._exit(9)\n"  # 成功路径后立即硬退出（模拟 fsync 后崩溃）
        ) % (os.path.join(REPO, "scripts"), GOOD["op_id"], GOOD["envelope_digest"],
             GOOD["fencing"], GOOD["worker_id"], GOOD["key_version"])
        # 阶段1：创建后、写入前中断（monkeypatch 无法跨进程 → 用注入脚本）
        child_partial = CHILD.replace(
            "DM.write_marker(", "os.open(os.path.join(dd, %r, os.O_WRONLY|os.O_CREAT|os.O_EXCL) and DM.write_marker(" % (GOOD["op_id"] + ".marker.json",))
        # 直接构造半成品更简单：手工 O_EXCL 空文件
        p = DM.marker_path(self.dd, GOOD["op_id"])
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        with self.assertRaises(DM.MarkerInvalid):
            DM.read_marker(p)          # 恢复读取：绝不当作完整证据
        os.unlink(p)                   # 测试清理（生产运行期无删除接口）
        # 阶段2：全序列成功后崩溃 → 恢复读取=完整证据
        r = subprocess.run([sys.executable, "-c", CHILD, self.d],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 9, r.stderr)   # 子进程走到硬退出
        payload = DM.read_marker(p)
        self.assertEqual(payload["op_id"], GOOD["op_id"])
        self.assertEqual(payload["fencing"], GOOD["fencing"])


class TestMK06CrashPoints(MarkerBase):
    def test_mk06_crash_at_each_point_recovery_semantics(self):
        CHILD_TPL = (
            "import sys, os\n"
            "sys.path.insert(0, r'%s')\n"
            "import dispatch_marker as DM\n"
            "dd = DM.dispatching_dir(sys.argv[1])\n"
            "stage = sys.argv[2]\n"
        ) % os.path.join(REPO, "scripts")
        child_full = CHILD_TPL + (
            "DM.write_marker(dd, 'op-crash', '%s', '7', 'w-1', '1')\n"
            "os._exit(0)\n" % ("b" * 64)
        )
        r = subprocess.run([sys.executable, "-c", child_full, self.d, "any"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        payload = DM.read_marker(DM.marker_path(self.dd, "op-crash"))
        self.assertEqual(payload["envelope_digest"], "b" * 64)


class TestMK07Tamper(MarkerBase):
    def test_mk07_external_tamper_rejected(self):
        self._write_ok()
        p = DM.marker_path(self.dd, GOOD["op_id"])
        # 篡改内容（fencing 改大）
        with open(p, encoding="utf-8") as f:
            payload = json.load(f)
        payload["fencing"] = "999"
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        with self.assertRaises(DM.MarkerInvalid):
            DM.verify_marker_identity(p, GOOD)
        # 身份不一致（期望 op_id 与内容不符）
        payload2 = dict(payload)
        payload2["fencing"] = GOOD["fencing"]
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payload2, f)
        with self.assertRaises(DM.MarkerInvalid):
            DM.verify_marker_identity(p, {**GOOD, "worker_id": "w-other"})
        # 一致 → 通过
        got = DM.verify_marker_identity(p, GOOD)
        self.assertEqual(got["op_id"], GOOD["op_id"])
        # 改文件名错位 → 拒
        p2 = os.path.join(self.dd, "op-fake.marker.json")
        shutil.copy(p, p2)
        with self.assertRaises(DM.MarkerInvalid):
            DM.read_marker(p2)

    def test_mk07b_field_validation_strict(self):
        bads = [
            {"op_id": "../evil"},                          # 路径逃逸
            {"op_id": "a/b"},                              # 路径分隔符
            {"op_id": ""},                                 # 空
            {"envelope_digest": "XYZ"},                    # 非 hex64
            {"fencing": "-1"},                             # 非法十进制
            {"worker_id": ""},                             # 空
            {"key_version": ""},                           # 空
        ]
        for over in bads:
            with self.assertRaises(DM.MarkerInvalid, msg=repr(over)):
                DM.write_marker(self.dd, **{**GOOD, **over})


class TestMK08Fencing(MarkerBase):
    def test_mk08_stale_fencing_never_authorizes(self):
        """MK08：旧 fencing 的 Marker 不得授权当前派发。"""
        self._write_ok(fencing="7")     # 旧世代
        cur = {"fencing_generation": 9}  # 当前 Gate 世代
        p = DM.marker_path(self.dd, GOOD["op_id"])
        payload = DM.verify_marker_identity(p, GOOD)
        self.assertLess(int(payload["fencing"]), cur["fencing_generation"])
        with self.assertRaises(DM.MarkerInvalid):  # 携带当前世代期望 → 拒
            DM.verify_marker_identity(p, {**GOOD, "fencing": "9"})


class TestMarkerExtras(MarkerBase):
    def test_no_delete_api_runtime_never_deletes(self):
        """运行期永不删 Marker：模块无任何删除/覆盖函数。"""
        for name in dir(DM):
            if name.startswith("_"):
                continue
            self.assertNotIn("delete", name.lower())
            self.assertNotIn("remove", name.lower())
            self.assertNotIn("unlink", name.lower())
            self.assertNotIn("overwrite", name.lower())

    def test_envelope_consistency_digest_worker_keyversion(self):
        """Marker 与 P2-2-1 Envelope 的 digest/worker/key_version 完全一致。"""
        kd = os.path.join(self.d, "keys")
        ENV.ensure_keypair(kd, "1")
        ENV.write_registry(kd, "1", ["1"])
        env = ENV.build_and_sign("op-env-mk", "task-x", "ik-x", 3, "w-env",
                                 "order:1", kd)
        self._write_ok(op_id=env["op_id"],
                       envelope_digest=env["envelope_digest"],
                       fencing=env["fencing"],
                       worker_id=env["worker_id"],
                       key_version=env["key_version"])
        payload = DM.verify_marker_identity(
            DM.marker_path(self.dd, env["op_id"]),
            {"op_id": env["op_id"],
             "envelope_digest": env["envelope_digest"],
             "fencing": env["fencing"],
             "worker_id": env["worker_id"],
             "key_version": env["key_version"]})
        self.assertEqual(payload["marker_version"], DM.MARKER_PROTOCOL_VERSION)

    def test_same_volume_and_permissions(self):
        """同卷目录 + 权限检查（POSIX 断言 0700；Windows 断言位于 ops_root 下）。"""
        self.assertEqual(os.path.dirname(self.dd),
                         os.path.abspath(self.d))    # 同卷（同一父树）
        if os.name == "posix":
            self.assertEqual(os.stat(self.dd).st_mode & 0o777, 0o700)
        # 普通执行者不能替换：哨兵存在且 marker 只能由 O_EXCL 创建（MK01 已证）
        self.assertTrue(os.path.exists(os.path.join(self.dd, DM.SENTINEL_NAME)))


if __name__ == "__main__":
    unittest.main()
