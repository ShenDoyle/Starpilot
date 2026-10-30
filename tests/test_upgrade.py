#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""升级器单测：版本解析、镜像选择、覆盖安装、残留清理。

不联网：probe / http_get 全部用 mock 替身。
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import upgrade as ug  # noqa: E402


def _write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(text)


class TestVersion(unittest.TestCase):
    def test_parse_front_matter_version(self):
        self.assertEqual(ug.parse_version('---\nname: starpilot\nversion: "2.2.0"\n---\n'),
                         (2, 2, 0))

    def test_parse_unquoted_and_missing(self):
        self.assertEqual(ug.parse_version("version: 1.10.3\n"), (1, 10, 3))
        self.assertEqual(ug.parse_version("no version here"), ())

    def test_numeric_compare_not_lexicographic(self):
        self.assertGreater(ug.parse_version("version: 2.10.0"), ug.parse_version("version: 2.9.9"))

    def test_fmt(self):
        self.assertEqual(ug.fmt((2, 2, 0)), "2.2.0")
        self.assertEqual(ug.fmt(()), "(未知)")

    def test_local_version_reads_skill_md(self):
        d = tempfile.mkdtemp(prefix="sp-ug-v-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        _write(os.path.join(d, "SKILL.md"), '---\nversion: "3.1.0"\n---\n')
        self.assertEqual(ug.local_version(d), (3, 1, 0))
        self.assertEqual(ug.local_version(os.path.join(d, "nope")), ())


class TestSourceSelect(unittest.TestCase):
    def test_mirror_urls_keep_full_github_url(self):
        self.assertTrue(ug.raw_url("SKILL.md").startswith("https://raw.githubusercontent.com/"))
        self.assertEqual(ug.raw_url("SKILL.md", "https://ghfast.top"),
                         "https://ghfast.top/https://raw.githubusercontent.com/%s/main/SKILL.md" % ug.REPO)
        self.assertEqual(ug.tarball_url("https://ghfast.top"),
                         "https://ghfast.top/https://github.com/%s/archive/refs/heads/main.tar.gz" % ug.REPO)
        self.assertIn("codeload.github.com", ug.tarball_url())

    def test_auto_falls_back_to_working_mirror_when_direct_times_out(self):
        def fake_probe(mirror, timeout, path="SKILL.md"):
            if mirror == "":
                return {"ok": False, "mirror": mirror, "err": "TimeoutError"}
            if mirror == "https://ghfast.top":
                return {"ok": True, "mirror": mirror, "ms": 900}
            if mirror == "https://gh-proxy.com":
                return {"ok": True, "mirror": mirror, "ms": 400}
            return {"ok": False, "mirror": mirror, "err": "URLError"}
        with mock.patch.object(ug, "probe", side_effect=fake_probe):
            cands = ug.candidate_list("auto", 1.0)
        self.assertEqual(cands[0], "https://gh-proxy.com", "直连死了应把最快的镜像排在首位")
        self.assertNotIn("", cands, "不可用的源不该出现在候选里")

    def test_direct_preferred_when_alive(self):
        def fake_probe(mirror, timeout, path="SKILL.md"):
            return {"ok": True, "mirror": mirror, "ms": 50 if mirror == "" else 10}
        with mock.patch.object(ug, "probe", side_effect=fake_probe):
            self.assertEqual(ug.candidate_list("auto", 1.0)[0], "",
                             "直连可用时优先直连（即便镜像更快）")

    def test_explicit_direct_fails_loudly(self):
        with mock.patch.object(ug, "probe", return_value={"ok": False, "mirror": "", "err": "timeout"}):
            with self.assertRaises(RuntimeError) as cm:
                ug.candidate_list("direct", 1.0)
        self.assertIn("自动切加速镜像", str(cm.exception))

    def test_all_sources_dead_raises(self):
        with mock.patch.object(ug, "probe", return_value={"ok": False, "mirror": "x", "err": "down"}):
            with self.assertRaises(RuntimeError):
                ug.candidate_list("auto", 1.0)

    # ---- 版本以「安装包」为准，不信 raw 的 CDN 缓存 ----

    def test_version_comes_from_tarball_not_raw(self):
        good = tempfile.mkdtemp(prefix="sp-ug-pkg-")
        self.addCleanup(shutil.rmtree, good, ignore_errors=True)
        _write(os.path.join(good, "SKILL.md"), '---\nversion: "2.3.0"\n---\n')

        def fake_fetch(dest, mirror="", timeout=60.0):
            # 第一个源的内容是「坏包」（没有 version），第二个才是好包
            if mirror == "":
                _write(os.path.join(dest, "pkg", "SKILL.md"), "no front matter\n")
                return os.path.join(dest, "pkg")
            _write(os.path.join(dest, "pkg", "SKILL.md"), '---\nversion: "2.3.0"\n---\n')
            return os.path.join(dest, "pkg")

        with mock.patch.object(ug, "candidate_list", return_value=["", "https://ghfast.top"]), \
                mock.patch.object(ug, "fetch_tarball", side_effect=fake_fetch):
            r = ug.download_latest("auto", 1.0)
        try:
            self.assertEqual(r["ver"], (2, 3, 0))
            self.assertEqual(r["mirror"], "https://ghfast.top", "坏包要跳过，换下个源")
        finally:
            shutil.rmtree(r["tmp"], ignore_errors=True)

    def test_all_downloads_broken_raises_with_reasons(self):
        with mock.patch.object(ug, "candidate_list", return_value=["", "https://ghfast.top"]), \
                mock.patch.object(ug, "fetch_tarball", side_effect=OSError("boom")):
            with self.assertRaises(RuntimeError) as cm:
                ug.download_latest("auto", 1.0)
        self.assertIn("所有候选源都下载失败", str(cm.exception))


class TestInstall(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="sp-ug-i-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.src = os.path.join(self.d, "src")
        self.dst = os.path.join(self.d, "dst")
        _write(os.path.join(self.src, "SKILL.md"), '---\nversion: "2.2.0"\n---\n')
        _write(os.path.join(self.src, "scripts", "a.py"), "print(1)\n")
        _write(os.path.join(self.src, "runtime", "secret.md"), "旧运行数据（不该被覆盖）\n")
        _write(os.path.join(self.src, "runs", "x", "state.json"), "{}\n")
        os.makedirs(self.dst)
        _write(os.path.join(self.dst, "SKILL.md"), '---\nversion: "1.0.0"\n---\n')
        _write(os.path.join(self.dst, "runtime", "mine.md"), "用户现场\n")

    def test_install_copies_core_files_and_keeps_local_data(self):
        info = ug.install(self.src, self.dst)
        self.assertTrue(os.path.exists(os.path.join(self.dst, "scripts", "a.py")))
        self.assertEqual(ug.local_version(self.dst), (2, 2, 0))
        self.assertEqual(ug.local_version(os.path.join(self.src)), (2, 2, 0))
        # 用户运行数据没被动过，源里的 runtime/runs 也没被搬过来
        with io.open(os.path.join(self.dst, "runtime", "mine.md"), encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "用户现场")
        self.assertFalse(os.path.exists(os.path.join(self.dst, "runtime", "secret.md")))
        self.assertFalse(os.path.exists(os.path.join(self.dst, "runs")))
        # 备份成对出现，且备份里还是旧版
        self.assertTrue(os.path.isdir(info["backup"]))
        self.assertEqual(ug.local_version(info["backup"]), (1, 0, 0))

    def test_manifest_written_and_stale_file_pruned(self):
        _write(os.path.join(self.dst, "old_only.py"), "# 旧版遗留\n")
        ug.install(self.src, self.dst, prune=False)
        # 手工把遗留文件记进清单，模拟「上一次安装确实装过它」
        man = os.path.join(self.dst, ug.MANIFEST)
        with io.open(man, encoding="utf-8") as f:
            data = json.load(f)
        data["files"].append("old_only.py")
        with io.open(man, "w", encoding="utf-8") as f:
            json.dump(data, f)
        info = ug.install(self.src, self.dst, prune=True)
        self.assertIn("old_only.py", info["removed"])
        self.assertFalse(os.path.exists(os.path.join(self.dst, "old_only.py")))

    def test_prune_never_deletes_files_outside_manifest(self):
        _write(os.path.join(self.dst, "user_note.md"), "用户自己的笔记\n")
        ug.install(self.src, self.dst, prune=True)
        self.assertTrue(os.path.exists(os.path.join(self.dst, "user_note.md")),
                        "不在安装清单里的文件一律不动")


class TestCli(unittest.TestCase):
    def _args(self, **kw):
        base = dict(check=True, to_latest=False, dir="", mirror="auto", timeout=1.0,
                    force=False, no_prune=False, no_test=True, json=False)
        base.update(kw)
        return mock.Mock(**base)

    def _fake_src(self, ver):
        tmp = tempfile.mkdtemp(prefix="sp-ug-src-")
        return {"mirror": "https://ghfast.top", "ver": ver, "root": tmp, "tmp": tmp}

    def test_check_returns_4_when_remote_is_newer(self):
        d = tempfile.mkdtemp(prefix="sp-ug-c-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        _write(os.path.join(d, "SKILL.md"), '---\nversion: "1.0.0"\n---\n')
        with mock.patch.object(ug, "download_latest", return_value=self._fake_src((2, 3, 0))):
            self.assertEqual(ug.cmd_check(self._args(dir=d)), 4)
        with mock.patch.object(ug, "download_latest", return_value=self._fake_src((1, 0, 0))):
            self.assertEqual(ug.cmd_check(self._args(dir=d)), 0)

    def test_git_repo_needs_force(self):
        d = tempfile.mkdtemp(prefix="sp-ug-g-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        _write(os.path.join(d, "SKILL.md"), '---\nversion: "1.0.0"\n---\n')
        os.makedirs(os.path.join(d, ".git"))
        with mock.patch.object(ug, "download_latest", return_value=self._fake_src((9, 9, 9))):
            self.assertEqual(ug.cmd_upgrade(self._args(check=False, to_latest=True, dir=d)), 1)

    def test_same_version_skips_reinstall(self):
        d = tempfile.mkdtemp(prefix="sp-ug-s-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        _write(os.path.join(d, "SKILL.md"), '---\nversion: "2.3.0"\n---\n')
        with mock.patch.object(ug, "download_latest", return_value=self._fake_src((2, 3, 0))), \
                mock.patch.object(ug, "install") as inst:
            self.assertEqual(ug.cmd_upgrade(self._args(check=False, to_latest=True, dir=d)), 0)
        inst.assert_not_called()


if __name__ == "__main__":
    unittest.main()
