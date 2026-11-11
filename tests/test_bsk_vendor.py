#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vendor 钉住版 bsk 解析与闸门回落（2026-11-11）。

背景：bsk 0.4.2+ 已知劣化 + BSK_AUTO_UPDATE 会绕过提示直接换机器级二进制，
用户裁定把匹配版本随仓库分发（vendor/bsk/…）并在解析顺序上钉住。
"""
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import bsk_vendor  # noqa: E402
import env as envmod  # noqa: E402


def _mk_vendor(root, version="0.4.1", sha="a" * 64, triple="x86_64-pc-windows-msvc",
               fname="bsk.exe", content=b"MZ-fake"):
    d = os.path.join(root, "vendor", "bsk", version, triple)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, fname)
    with open(p, "wb") as f:
        f.write(content)
    import hashlib
    man = {"version": version, "source": "test",
           "assets": {triple: {"file": fname,
                               "sha256": hashlib.sha256(content).hexdigest() if sha is None else sha,
                               "bytes": len(content)}}}
    with open(os.path.join(root, "vendor", "bsk", "manifest.json"), "w",
              encoding="utf-8") as f:
        json.dump(man, f)
    return p


class TripleTest(unittest.TestCase):
    def test_windows(self):
        self.assertEqual(bsk_vendor.triple("win32", "AMD64"),
                         "x86_64-pc-windows-msvc")
        self.assertEqual(bsk_vendor.triple("win32", ""), "x86_64-pc-windows-msvc")

    def test_darwin_arm_and_intel(self):
        self.assertEqual(bsk_vendor.triple("darwin", "arm64"), "aarch64-apple-darwin")
        self.assertEqual(bsk_vendor.triple("darwin", "x86_64"), "x86_64-apple-darwin")

    def test_linux_arm_and_x64(self):
        self.assertEqual(bsk_vendor.triple("linux", "aarch64"),
                         "aarch64-unknown-linux-musl")
        self.assertEqual(bsk_vendor.triple("linux", "x86_64"),
                         "x86_64-unknown-linux-musl")

    def test_unknown_platform(self):
        self.assertIsNone(bsk_vendor.triple("freebsd", "amd64"))


class ManifestTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()

    def test_missing_manifest(self):
        self.assertIsNone(bsk_vendor.manifest(self.tmp))
        self.assertIsNone(bsk_vendor.pinned_version(self.tmp))

    def test_corrupt_manifest(self):
        p = os.path.join(self.tmp, "vendor", "bsk")
        os.makedirs(p)
        with open(os.path.join(p, "manifest.json"), "w", encoding="utf-8") as f:
            f.write("{broken")
        self.assertIsNone(bsk_vendor.manifest(self.tmp))

    def test_manifest_without_assets(self):
        p = os.path.join(self.tmp, "vendor", "bsk")
        os.makedirs(p)
        with open(os.path.join(p, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"version": "0.4.1"}, f)
        self.assertIsNone(bsk_vendor.manifest(self.tmp))

    def test_real_repo_manifest_and_binaries(self):
        """仓库自带的 manifest 与五平台产物必须齐全（防漏提交）。"""
        m = bsk_vendor.manifest(ROOT)
        self.assertIsNotNone(m)
        self.assertEqual(m["version"], "0.4.1")
        self.assertEqual(len(m["assets"]), 5)
        for t, a in m["assets"].items():
            self.assertEqual(len(a.get("sha256") or ""), 64, t)
            fp = os.path.join(ROOT, "vendor", "bsk", m["version"], t, a["file"])
            self.assertTrue(os.path.exists(fp), "缺产物：%s" % fp)
            self.assertGreater(a.get("bytes") or 0, 1_000_000, t)

    def test_real_repo_binary_hash_matches(self):
        """至少当前平台产物 sha256 与 manifest 一致（防上传截断）。"""
        p = bsk_vendor.vendor_bsk(ROOT)
        self.assertTrue(p and os.path.exists(p))


class VendorBskTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()

    def test_resolves_and_verifies(self):
        _mk_vendor(self.tmp, sha=None)  # 写入时按内容算真 sha
        p = bsk_vendor.vendor_bsk(self.tmp, sysp="win32")
        self.assertTrue(p and p.endswith("bsk.exe"))

    def test_hash_mismatch_is_fail_closed(self):
        _mk_vendor(self.tmp, sha="b" * 64)  # manifest 声称的哈希与文件不符
        self.assertIsNone(bsk_vendor.vendor_bsk(self.tmp, sysp="win32"))

    def test_verify_false_skips_hash(self):
        _mk_vendor(self.tmp, sha="b" * 64)
        self.assertTrue(bsk_vendor.vendor_bsk(self.tmp, sysp="win32", verify=False))

    def test_missing_binary(self):
        _mk_vendor(self.tmp, sha=None)
        os.remove(os.path.join(self.tmp, "vendor", "bsk", "0.4.1",
                               "x86_64-pc-windows-msvc", "bsk.exe"))
        self.assertIsNone(bsk_vendor.vendor_bsk(self.tmp, sysp="win32"))

    def test_platform_without_asset(self):
        _mk_vendor(self.tmp, sha=None)
        self.assertIsNone(bsk_vendor.vendor_bsk(self.tmp, sysp="darwin", machine="arm64"))

    def test_no_platform_triple(self):
        _mk_vendor(self.tmp, sha=None)
        self.assertIsNone(bsk_vendor.vendor_bsk(self.tmp, sysp="freebsd"))


class FindBskPriorityTest(unittest.TestCase):
    """BSK_BIN 显式覆盖永远最高，其次 vendor，机器级候选只作兜底。"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.vp = _mk_vendor(self.tmp, sha=None)

    def test_bsk_bin_still_wins(self):
        fake = os.path.join(self.tmp, "override.exe")
        open(fake, "wb").close()
        got = envmod.find_bsk(env={"BSK_BIN": fake}, which=lambda n: None)
        self.assertEqual(got, fake)

    def test_vendor_beats_machine(self):
        with mock.patch.object(bsk_vendor, "_ROOT", self.tmp):
            got = envmod.find_bsk(env={"BSK_BIN": ""}, which=lambda n: None)
        self.assertEqual(got, self.vp)

    def test_falls_back_when_vendor_unusable(self):
        with mock.patch.object(bsk_vendor, "vendor_bsk", return_value=None):
            got = envmod.find_bsk(env={"BSK_BIN": ""}, which=lambda n: None)
        # 机器上装了 shim 真身/PATH bsk 的话命中机器级；都没有则 None——两者都合法
        self.assertTrue(got is None or os.path.exists(got))

    def test_corrupt_vendor_falls_back_not_returned(self):
        # 哈希不符的 vendor 二进制绝不能被 find_bsk 返回
        _mk_vendor(self.tmp, sha="c" * 64)
        got = envmod.find_bsk(env={"BSK_BIN": ""}, which=lambda n: None)
        self.assertNotEqual(got, self.vp)


class ShimFallbackTest(unittest.TestCase):
    """shim 真身缺失时落到 vendor（机器级 bsk 被 BSK_AUTO_UPDATE 换掉也不怕）。"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.vp = _mk_vendor(self.tmp, sha=None)

    def test_real_bsk_prefers_sibling(self):
        import bsk_shim
        with mock.patch.object(bsk_shim, "REAL", __file__):  # 任意存在文件
            self.assertEqual(bsk_shim._real_bsk(), __file__)

    def test_real_bsk_falls_to_vendor(self):
        import bsk_shim
        skill_root = os.path.join(self.tmp, "skills", "starpilot")
        fake_scripts = os.path.join(skill_root, "scripts")
        os.makedirs(fake_scripts)
        vp = _mk_vendor(skill_root, sha=None)  # vendor 落在安装位根下
        with mock.patch.object(bsk_shim, "REAL",
                               os.path.join(self.tmp, "no-such-bsk.bin.exe")), \
             mock.patch.object(bsk_shim, "_SKILL_SCRIPT_DIRS", [fake_scripts]):
            self.assertEqual(bsk_shim._real_bsk(), vp)

    def test_real_bsk_keeps_old_behavior_when_nothing(self):
        import bsk_shim
        with mock.patch.object(bsk_shim, "REAL",
                               os.path.join(self.tmp, "no-such-bsk.bin.exe")), \
             mock.patch.object(bsk_shim, "_SKILL_SCRIPT_DIRS",
                               [os.path.join(self.tmp, "empty")]):
            self.assertEqual(bsk_shim._real_bsk(), bsk_shim.REAL)


class WaCandidatesTest(unittest.TestCase):
    def test_vendor_is_second_candidate(self):
        import wa
        self.assertEqual(wa._CANDIDATES[0], os.environ.get("BSK_BIN") or "")
        self.assertIn(os.sep + "vendor" + os.sep, wa._CANDIDATES[1] or "")


class InstallerTemplateTest(unittest.TestCase):
    def test_posix_template_gates_session_start(self):
        import install_l2_shim
        self.assertIn('"$1" = "session"', install_l2_shim.SH_TEMPLATE)
        self.assertIn('exec python3 "$DIR/bsk_shim.py" "$@"',
                      install_l2_shim.SH_TEMPLATE)
        self.assertIn('exec "$REAL" "$@"', install_l2_shim.SH_TEMPLATE)
        self.assertIn("bsk.bin", install_l2_shim.SH_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
