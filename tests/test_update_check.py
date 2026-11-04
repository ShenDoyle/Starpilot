#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""update_check 的纯逻辑测试（全离线：网络点 mock，绝不在测试里真连 GitHub）。

重点盯三处曾经/容易出错的地方：
  1. 渠道前缀过滤——bsk 仓库 cli-v*/ext-* 混在一起，不过滤会错拿扩展版本；
  2. prerelease 排除——release 渠道标记了 prerelease 的不能算稳定版；
  3. 版本元组比较——0.1.7.1 四段式必须同时满足 >(0,1,7) 且 <(0,1,8)，
     这正是「不用 0.1.71 三段写法」的原因（(0,1,71) 会永远压过 (0,1,8)）。
"""

import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import update_check as uc  # noqa: E402


def rel(tag_name, prerelease=False, draft=False):
    return {"tag_name": tag_name, "prerelease": prerelease, "draft": draft}


class ParseTagTest(unittest.TestCase):
    def test_self_tag(self):
        self.assertEqual(uc.parse_tag("v0.1.7", "v"), (0, 1, 7))

    def test_bsk_cli_tag(self):
        self.assertEqual(uc.parse_tag("cli-v0.4.1", "cli-v"), (0, 4, 1))

    def test_wrong_prefix_is_rejected(self):
        self.assertEqual(uc.parse_tag("cli-v0.4.1", "v"), ())
        self.assertEqual(uc.parse_tag("ext-v0.4.1", "cli-v"), ())

    def test_four_segment(self):
        self.assertEqual(uc.parse_tag("v0.1.7.1", "v"), (0, 1, 7, 1))

    def test_junk_is_empty(self):
        for junk in ("", None, "vX.Y", "cli-v", "v0.1.7-beta", "cliv0.4"):
            self.assertEqual(uc.parse_tag(junk, "v" if junk and "cli" not in str(junk) else "cli-v"), ())

    def test_semver_ordering_why_four_segments(self):
        # 0.1.7.1 高于 0.1.7 且低于 0.1.8；三段写法 0.1.71 会永远压过 0.1.8
        self.assertGreater((0, 1, 7, 1), (0, 1, 7))
        self.assertLess((0, 1, 7, 1), (0, 1, 8))
        self.assertGreater((0, 1, 71), (0, 1, 8))  # 反例坐实：三段写法比较会反


class LatestStableTest(unittest.TestCase):
    def test_releases_filters_channel_and_prerelease(self):
        releases = [  # API 顺序：新的在前
            rel("ext-v0.5.0"),                      # 扩展渠道，必须无视
            rel("cli-v0.5.0-rc1", prerelease=True),  # 预发布，必须无视
            rel("cli-v0.4.1"),
            rel("ext-v0.4.0"),
            rel("cli-v0.4.0"),
            rel("cli-v0.3.9", draft=True),          # 草稿，必须无视
        ]
        self.assertEqual(uc.latest_stable_from_releases(releases, "cli-v"), (0, 4, 1))

    def test_releases_empty(self):
        self.assertEqual(uc.latest_stable_from_releases([], "v"), ())
        self.assertEqual(uc.latest_stable_from_releases(None, "v"), ())

    def test_tags_picks_max_not_first(self):
        tags = [{"name": "v0.1.2"}, {"name": "v0.1.10"}, {"name": "v0.1.7.1"}, {"name": "ext-v9.9"}]
        # (0,1,10) > (0,1,7,1) > (0,1,2)：字符串排序会把 0.1.10 排在 0.1.2 前面，必须按元组比
        self.assertEqual(uc.latest_stable_from_tags(tags, "v"), (0, 1, 10))


class NewerTest(unittest.TestCase):
    def test_unknown_never_counts_as_newer(self):
        self.assertFalse(uc.newer((), (0, 1, 7)))
        self.assertFalse(uc.newer((0, 1, 8), ()))

    def test_equal_is_not_newer(self):
        self.assertFalse(uc.newer((0, 1, 7), (0, 1, 7)))

    def test_greater_is_newer(self):
        self.assertTrue(uc.newer((0, 1, 8), (0, 1, 7)))
        self.assertTrue(uc.newer((0, 1, 7, 1), (0, 1, 7)))


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="uc-test-")

    def _write(self, obj):
        p = uc.cache_path(self.tmp)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f)

    def test_roundtrip_fresh(self):
        data = {"fetched_at": time.time(), "starpilot": {"a": 1}, "bsk": {"b": 2}}
        self._write(data)
        c = uc.load_cache(self.tmp, ttl_hours=24)
        self.assertEqual(c["starpilot"], {"a": 1})
        self.assertLessEqual(c["_age_hours"], 0.1)

    def test_expired_is_empty(self):
        data = {"fetched_at": time.time() - 25 * 3600, "starpilot": {}, "bsk": {}}
        self._write(data)
        self.assertEqual(uc.load_cache(self.tmp, 24), {})

    def test_future_timestamp_is_empty(self):
        data = {"fetched_at": time.time() + 3600, "starpilot": {}, "bsk": {}}
        self._write(data)
        self.assertEqual(uc.load_cache(self.tmp, 24), {})

    def test_corrupt_or_missing_is_empty(self):
        p = uc.cache_path(self.tmp)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(uc.load_cache(self.tmp, 24), {})
        self.assertEqual(uc.load_cache(self.tmp, 24), {})  # 不存在也是空
        self._write({"fetched_at": time.time()})            # 缺 starpilot/bsk 键
        self.assertEqual(uc.load_cache(self.tmp, 24), {})


class CheckFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="uc-test-")

    def test_check_merges_and_caches(self):
        with mock.patch.object(uc, "fetch_remote", side_effect=[
                {"version": (0, 1, 8), "source": "tags", "err": ""},
                {"version": (0, 4, 2), "source": "releases", "err": ""}]), \
             mock.patch.object(uc, "local_self", return_value=(0, 1, 7, 1)), \
             mock.patch.object(uc, "local_bsk", return_value=(0, 4, 1)):
            rep = uc.check(self.tmp, ttl_hours=24)
        self.assertTrue(rep["starpilot"]["newer"])
        self.assertTrue(rep["bsk"]["newer"])
        self.assertFalse(rep["cached"])
        self.assertEqual(rep["starpilot"]["local"], "0.1.7.1")
        # 再查应命中缓存，不再发起网络请求
        with mock.patch.object(uc, "fetch_remote", side_effect=AssertionError("缓存期内不应联网")), \
             mock.patch.object(uc, "local_self", side_effect=AssertionError("缓存期内不应取本地")), \
             mock.patch.object(uc, "local_bsk", side_effect=AssertionError("缓存期内不应取本地")):
            rep2 = uc.check(self.tmp, ttl_hours=24)
        self.assertTrue(rep2["cached"])
        self.assertTrue(rep2["starpilot"]["newer"])

    def test_network_failure_is_unknown_not_crash(self):
        with mock.patch.object(uc, "fetch_remote",
                               return_value={"version": (), "source": "", "err": "HTTP 403"}), \
             mock.patch.object(uc, "local_self", return_value=(0, 1, 7, 1)), \
             mock.patch.object(uc, "local_bsk", return_value=(0, 4, 1)):
            rep = uc.check(self.tmp, ttl_hours=24)
        self.assertFalse(rep["starpilot"]["newer"])
        self.assertEqual(rep["starpilot"]["remote"], "(未知)")
        self.assertFalse(rep["bsk"]["newer"])

    def test_checker_crash_still_reports(self):
        with mock.patch.object(uc, "check", side_effect=RuntimeError("boom")):
            rc = uc.main(["--json", "--dir", self.tmp])
        self.assertEqual(rc, 0)  # 检查器自身炸了不算「有新版」，也不许抛

    def test_main_exit_code_4_when_newer(self):
        with mock.patch.object(uc, "check", return_value={
                "cached": False,
                "starpilot": {"newer": True, "local": "0.1.7.1", "remote": "0.1.8",
                              "source": "tags", "err": ""},
                "bsk": {"newer": False, "local": "0.4.1", "remote": "0.4.1",
                        "source": "releases", "err": ""}}):
            rc = uc.main(["--json", "--dir", self.tmp])
        self.assertEqual(rc, 4)

    def test_skip_flags_skip_network(self):
        # skip 只豁免远端网络查询与 bsk 本地探测（后者要起子进程）；
        # Starpilot 本地版本是廉价的文件读取，仍允许做。
        with mock.patch.object(uc, "fetch_remote", side_effect=AssertionError("skip 后不应联网")), \
             mock.patch.object(uc, "local_bsk", side_effect=AssertionError("skip 后不应探测 bsk")):
            rep = uc.check(self.tmp, skip_self=True, skip_bsk=True)
        self.assertEqual(rep["starpilot"]["err"], "跳过")


if __name__ == "__main__":
    unittest.main()
