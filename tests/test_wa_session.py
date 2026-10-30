"""实例、会话、开窗纪律：把「读不到回复 + 开一堆 tab」的四条根因钉成测试。

现场（2026-10-30，豆包工作机，Chrome）：
  · 同一个 Chrome 里装了 4 份 BrowserSkill，工具栏并排 4 个图标，bsk 报 4 个实例；
  · ChatGPT 页面有 0×0 的隐形 [role="dialog"]，被发送前的弹窗检测误判 → 永远拒发；
  · bsk 会话空闲 5 分钟回收窗口 → 发送时会话已死；
  · 修完能发，但常常「取不到回复」，一次任务开很多标签页。

四条里三条是代码问题，各自对应下面一组测试：
  1) `_norm_session` 没把 `browser_instance_id` 折进 `browser`
     → enforce_single_session 的过滤条件永远成立 → 旧窗口一个都收不掉 → 窗口堆积。
  2) 实例重复时仍按「就近挑一个」开会话 → 发送在 A、读回在 B → 永远读不到。
  3) wait/read 把「探测失败」当噪声跳过 → 会话没了也继续等满 15 分钟。
  4) 弹窗判据只看节点存在，不看尺寸 → 0×0 容器也能拦下发消息。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import platforms  # noqa: E402
import wa  # noqa: E402


def B(i, name="chrome", label="", **kw):
    d = {"id": i, "name": name, "label": label, "ext": "0.3.3",
         "sessions": 0, "unresponsive": False, "skew": False}
    d.update(kw)
    return d


class TestSessionNormalization(unittest.TestCase):
    def test_browser_id_folded_into_browser(self):
        # bsk 的字段名是 browser_instance_id；不折进来，enforce_single_session 就永远不匹配
        s = wa._norm_session({"session_id": "abcd", "browser_instance_id": "ec39f216",
                              "agent_window_id": 123})
        self.assertEqual(s["id"], "abcd")
        self.assertEqual(s["browser"], "ec39f216")
        self.assertEqual(s["window"], 123)

    def test_explicit_browser_wins(self):
        s = wa._norm_session({"session_id": "abcd", "browser": "x", "browser_instance_id": "y"})
        self.assertEqual(s["browser"], "x")

    def test_missing_browser_is_empty_not_none(self):
        self.assertEqual(wa._norm_session({"session_id": "abcd"})["browser"], "")


class TestInstanceReport(unittest.TestCase):
    def test_single_instance_is_healthy(self):
        rep = wa.instance_report([B("a1", label="工作")])
        self.assertTrue(rep["healthy"])
        self.assertTrue(rep["unique"])
        self.assertEqual(rep["duplicates"], [])

    def test_same_label_twice_is_duplicate(self):
        # 同一 Chrome 装两份扩展：label 相同、浏览器相同 → 重复
        rep = wa.instance_report([B("a1", label="Chrome"), B("a2", label="Chrome")])
        self.assertFalse(rep["healthy"])
        self.assertEqual(len(rep["duplicates"]), 1)
        self.assertEqual(rep["duplicates"][0]["count"], 2)
        self.assertEqual(sorted(rep["duplicates"][0]["ids"]), ["a1", "a2"])

    def test_different_labels_are_not_duplicates(self):
        # 多 profile 各装一份（本机真实形态）：不同 label，是合法配置
        rep = wa.instance_report([B("a1", label="京东佰意佳"), B("a2", label="京东老浦家")])
        self.assertTrue(rep["healthy"])
        self.assertFalse(rep["unique"])

    def test_empty_labels_are_not_duplicates(self):
        # 没起名字的两个 profile 会被误判成重复 —— 只算「无法区分」，不当重复
        rep = wa.instance_report([B("a1"), B("a2")])
        self.assertTrue(rep["healthy"])
        self.assertEqual(rep["duplicates"], [])

    def test_unresponsive_flagged(self):
        rep = wa.instance_report([B("a1", label="工作", unresponsive=True)])
        self.assertEqual(rep["unresponsive"], ["a1"])

    def test_four_instances_all_same_label(self):
        rep = wa.instance_report([B("a%d" % i, label="BrowserSkill") for i in range(4)])
        self.assertEqual(rep["duplicates"][0]["count"], 4)

    def test_hint_tells_where_to_fix(self):
        rep = wa.instance_report([B("a1", label="Chrome"), B("a2", label="Chrome")])
        hint = wa.duplicate_hint(rep)
        self.assertIn("chrome://extensions", hint)
        self.assertIn("只留一个", hint)
        self.assertIn("a1", hint)
        self.assertIn("doctor", hint)


class TestPickInstance(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("WA_BROWSER_PREFER", None)

    def test_single_instance_used(self):
        self.assertEqual(wa._first_browser_instance([B("a1")]), "a1")

    def test_ambiguous_returns_empty(self):
        # 两个实例且没有偏好：返回空让调用方报错，绝不就近挑
        self.assertEqual(wa._first_browser_instance([B("a1", label="x"), B("a2", label="y")]), "")

    def test_prefer_by_label(self):
        os.environ["WA_BROWSER_PREFER"] = "老浦家"
        self.assertEqual(
            wa._first_browser_instance([B("a1", label="京东佰意佳"), B("a2", label="京东老浦家")]),
            "a2")

    def test_prefer_matching_two_is_still_ambiguous(self):
        os.environ["WA_BROWSER_PREFER"] = "京东"
        self.assertEqual(
            wa._first_browser_instance([B("a1", label="京东佰意佳"), B("a2", label="京东老浦家")]),
            "")

    def test_prefer_by_instance_id(self):
        os.environ["WA_BROWSER_PREFER"] = "a2"
        self.assertEqual(wa._first_browser_instance([B("a1"), B("a2")]), "a2")

    def test_unresponsive_skipped(self):
        self.assertEqual(wa._first_browser_instance([B("a1", unresponsive=True)]), "")


class TestStartRateLimit(unittest.TestCase):
    def _st(self, n, age=0.0):
        import time
        return {"session_start_at": [time.time() - age for _ in range(n)]}

    def test_fresh_state_ok(self):
        self.assertFalse(wa._too_many_starts({}))

    def test_four_starts_in_window_blocks(self):
        self.assertTrue(wa._too_many_starts(self._st(wa.WA_START_MAX)))

    def test_old_starts_are_trimmed(self):
        # 一小时前的开窗不该算在「10 分钟内」的密度里
        self.assertFalse(wa._too_many_starts(self._st(10, age=wa.WA_START_WINDOW_S + 60)))

    def test_note_appends_and_trims(self):
        import time
        st = {"session_start_at": [time.time() - wa.WA_START_WINDOW_S - 100]}
        wa._note_session_start(st)
        self.assertEqual(len(st["session_start_at"]), 1)
        self.assertFalse(wa._too_many_starts(st))


class TestModalMustBeVisible(unittest.TestCase):
    """弹窗判据：只有「看得见且占面积」才算，0×0 容器不算。"""

    def setUp(self):
        self.js = platforms.js_probe(platforms.get("chatgpt"))

    def test_uses_geometry_not_existence(self):
        self.assertIn("getBoundingClientRect", self.js)
        self.assertIn("r.width < 24", self.js)
        self.assertIn("r.height < 24", self.js)

    def test_checks_computed_style(self):
        for token in ("display", "visibility", "opacity"):
            self.assertIn(token, self.js)

    def test_reports_rect_for_diagnosis(self):
        self.assertIn("modal_rect", self.js)

    def test_old_node_only_query_is_gone(self):
        # 旧的 `const modal = document.querySelector(...)` 只看节点存在，必须消失
        self.assertNotIn("const modal = document.querySelector", self.js)


class TestEnsureSessionGuard(unittest.TestCase):
    """开窗之前必须体检：重复安装的扩展会在「发送」和「读回」之间劈叉。"""

    def setUp(self):
        import json
        import shutil
        import tempfile
        self.json = json
        self._orig = wa.bsk
        wa.bsk = self._fake
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "state.json")
        self._rm = shutil.rmtree

    def tearDown(self):
        wa.bsk = self._orig
        self._rm(self.tmp, ignore_errors=True)
        os.environ.pop("WA_ALLOW_DUPLICATE_EXT", None)

    def _fake(self, args, timeout=120, check=False):
        if args[:2] == ["browsers", "--json"]:
            dup = [{"instance_id": "a1", "browser_name": "chrome", "label": "Chrome",
                    "extension_version": "0.3.3", "session_count": 0},
                   {"instance_id": "a2", "browser_name": "chrome", "label": "Chrome",
                    "extension_version": "0.3.3", "session_count": 0}]
            return 0, self.json.dumps(dup), ""
        if args[:3] == ["session", "list", "--json"]:
            return 0, "[]", ""
        return 1, "", "unexpected call: %s" % " ".join(args[:3])

    def test_refuses_to_open_window_when_extension_is_duplicated(self):
        with self.assertRaises(SystemExit) as cm:
            wa.ensure_session({"browser": "a1"}, self.path)
        msg = str(cm.exception)
        self.assertIn("chrome://extensions", msg)
        self.assertIn("a1", msg)

    def test_bypass_lets_it_through_to_the_next_failure(self):
        # 绕过体检后应卡在「开窗」本身，而不是卡在体检提示上（证明拦的是体检那一步）
        os.environ["WA_ALLOW_DUPLICATE_EXT"] = "1"
        with self.assertRaises(SystemExit) as cm:
            wa.ensure_session({"browser": "a1"}, self.path)
        self.assertIn("session start 失败", str(cm.exception))
        self.assertNotIn("chrome://extensions", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
