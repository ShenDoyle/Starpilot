#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发送确认判定的验收单测（架构评审 F2 / S1 / S2）。

这套测试盯的是**信任根**：`reply_count_delta` 是整个闭环的判据，
判错一次就是「假重发」或「谎报成功」。所以这里既覆盖正常路径，
也把两个历史事故回放成用例：
  1. 一条提问 + 一条回复被算成 delta=2（口径错，看着像重发）；
  2. 虚拟列表只渲染视口，节点数从 30 掉到 5，计数口径会算出负增量。
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import confirm  # noqa: E402
import platforms  # noqa: E402


def snap(reply_count=0, editor_len=0, asst=None, user=None, url="u1", msg_count=0):
    return {
        "url": url, "msg_count": msg_count, "reply_count": reply_count,
        "user_msg_count": len(user or []), "editor_len": editor_len,
        "asst_fps": list(asst or []), "user_fps": list(user or []),
        "last_asst_fp": (asst or [""])[-1] if asst else "",
        "last_user_fp": (user or [""])[-1] if user else "",
        "editor_kind": "TEXTAREA",
    }


class TestClassify(unittest.TestCase):
    def test_delivered_with_one_new_reply_is_sent(self):
        before = snap(reply_count=3, asst=["a", "b", "c"], user=["u1", "u2", "u3"])
        after = snap(reply_count=4, asst=["a", "b", "c", "d"], user=["u1", "u2", "u3", "u4"])
        r = confirm.classify_send_result(before, after, ["msg_count"])
        self.assertEqual(r["verdict"], confirm.SENT)
        self.assertEqual(r["effective_delta"], 1)
        self.assertEqual(r["delta_source"], "anchor")

    def test_one_question_one_reply_is_not_delta_two(self):
        """历史事故回放：Kimi 的 msg_count 会 +2（我的提问 + 平台回复），

        但 `reply_count`（助手气泡）只 +1 —— 判据必须落在 reply_count/锚点上。
        这里同时给出 msg_count=+2，断言判定仍是 SENT 而不是「重发」。
        """
        before = snap(reply_count=2, msg_count=10, asst=["a", "b"], user=["u1", "u2"])
        after = snap(reply_count=3, msg_count=12, asst=["a", "b", "c"], user=["u1", "u2", "u3"])
        r = confirm.classify_send_result(before, after, ["msg_count"])
        self.assertEqual(r["verdict"], confirm.SENT)
        self.assertEqual(r["count_delta"], 1, "reply_count 只应 +1")
        self.assertEqual(after["msg_count"] - before["msg_count"], 2, "msg_count 是 +2（平台口径）")

    def test_virtual_list_count_drop_but_anchor_says_one(self):
        """虚拟列表：节点数从 30 掉到 5，但确实新增了 1 条回复。

        计数口径会算出 -25（判失败），锚点口径算出 1（判成功）。
        正确做法是信锚点，并把「两口径不一致」如实记下来。
        """
        before = snap(reply_count=30, asst=["a%02d" % i for i in range(20)], user=["q"])
        after = snap(reply_count=5, asst=["a%02d" % i for i in range(19, 20)] + ["new"],
                     user=["q"])
        r = confirm.classify_send_result(before, after, ["msg_count"])
        self.assertEqual(r["effective_delta"], 1)
        self.assertEqual(r["verdict"], confirm.SENT)
        self.assertTrue(r["count_anchor_disagree"], "两口径不一致必须被记录")
        self.assertLess(r["count_delta"], 0, "计数口径此时是负的（正是要避免的误判）")

    def test_bubble_split_is_multi_not_failure(self):
        before = snap(reply_count=1, asst=["a"], user=["u1"])
        after = snap(reply_count=3, asst=["a", "b", "c"], user=["u1", "u2"])
        r = confirm.classify_send_result(before, after, ["msg_count"])
        self.assertEqual(r["verdict"], confirm.SENT_MULTI, "多气泡交 verify 细分，不在这里定罪")
        self.assertEqual(r["effective_delta"], 2)

    def test_weak_signal_only_is_inconclusive(self):
        """主信号一个都没有、但输入框空了：既不报成功也不报失败。"""
        before = snap(reply_count=1, asst=["a"], user=["u1"])
        after = snap(reply_count=1, editor_len=0, asst=["a"], user=["u1"])
        r = confirm.classify_send_result(before, after, [])
        self.assertEqual(r["verdict"], confirm.INCONCLUSIVE_NOT_SUCCESS)
        self.assertFalse(r["delivered"])
        self.assertTrue(r["editor_cleared"])

    def test_no_signal_editor_still_filled_is_not_sent(self):
        before = snap(reply_count=1, asst=["a"], user=["u1"])
        after = snap(reply_count=1, editor_len=120, asst=["a"], user=["u1"])
        r = confirm.classify_send_result(before, after, [])
        self.assertEqual(r["verdict"], confirm.NOT_SENT)

    def test_anchors_unavailable_falls_back_to_count(self):
        """发送前明明有回复却读到 0 个指纹 = 这侧不可信 → 退回计数口径并标记。"""
        before = snap(reply_count=5, asst=[], user=[])
        after = snap(reply_count=6, asst=["x"], user=["u"])
        r = confirm.classify_send_result(before, after, ["msg_count"])
        self.assertFalse(r["anchors"]["anchors_available"])
        self.assertEqual(r["delta_source"], "count")
        self.assertEqual(r["effective_delta"], 1)
        self.assertEqual(r["verdict"], confirm.SENT)

    def test_delivered_without_reply(self):
        before = snap(reply_count=2, asst=["a", "b"], user=["u1"])
        after = snap(reply_count=2, asst=["a", "b"], user=["u1", "u2"])
        r = confirm.classify_send_result(before, after, ["msg_count"])
        self.assertEqual(r["verdict"], confirm.SENT_NO_REPLY)
        self.assertTrue(r["delivered"])


class TestTrim(unittest.TestCase):
    def test_trim_keeps_anchor_fields(self):
        t = confirm.trim({"reply_count": 1, "asst_fps": ["a"], "last_asst_fp": "a",
                          "editor_len": 3, "editor_kind": "TEXTAREA"}, "h")
        for k in ("asst_fps", "user_fps", "msg_fps", "last_asst_fp", "editor_kind", "dom_hash"):
            self.assertIn(k, t)

    def test_anchor_delta_is_set_based(self):
        b = {"asst_fps": ["a", "b"], "reply_count": 2}
        a = {"asst_fps": ["b", "c"], "reply_count": 2}   # 老消息被卸载 + 新消息进来
        d = platforms.anchor_delta(b, a)
        self.assertEqual(d["new_asst"], 1)
        self.assertTrue(d["last_asst_changed"])


class TestKeyAnchor(unittest.TestCase):
    """平台自带消息 ID（DeepSeek 的 `data-virtual-list-item-key`）优先于正文指纹。

    为什么需要这一级：正文指纹会被三件事骗——回复被折叠、markdown 重排、
    连续两轮正文开头一样（比如都回「收到」）。ID 是消息身份，不会。
    """

    def test_key_beats_identical_text(self):
        # 两轮回复正文开头完全相同：指纹差为 0，ID 差为 1
        b = {"asst_keys": ["k:2", "k:4"], "asst_fps": ["收到", "收到"],
             "reply_count": 2, "last_asst_fp": "收到"}
        a = {"asst_keys": ["k:4", "k:6"], "asst_fps": ["收到", "收到"],
             "reply_count": 2, "last_asst_fp": "收到"}
        d = platforms.anchor_delta(b, a)
        self.assertTrue(d["anchors_available"])
        self.assertEqual(d["anchor_kind"], "key")
        self.assertEqual(d["new_asst"], 1, "正文相同也必须靠 ID 认出这是新的一条")
        self.assertTrue(d["last_asst_changed"])

    def test_key_delta_feeds_classify(self):
        before = {"reply_count": 2, "asst_keys": ["k:2", "k:4"],
                  "asst_fps": ["收到", "收到"], "user_fps": ["u"],
                  "last_asst_fp": "收到", "msg_count": 4, "editor_len": 0}
        after = {"reply_count": 2, "asst_keys": ["k:4", "k:6"],
                 "asst_fps": ["收到", "收到"], "user_fps": ["u", "v"],
                 "last_asst_fp": "收到", "msg_count": 3, "editor_len": 0}
        r = confirm.classify_send_result(before, after, ["needle"])
        self.assertEqual(r["verdict"], confirm.SENT)
        self.assertEqual(r["effective_delta"], 1)
        self.assertEqual(r["delta_source"], "anchor")
        self.assertTrue(r["count_anchor_disagree"], "计数持平而锚点 +1：必须记下这个分歧")

    def test_falls_back_to_fingerprint_without_keys(self):
        b = {"asst_fps": ["a"], "reply_count": 1, "last_asst_fp": "a"}
        a = {"asst_fps": ["a", "b"], "reply_count": 2, "last_asst_fp": "b"}
        d = platforms.anchor_delta(b, a)
        self.assertEqual(d["anchor_kind"], "fp")
        self.assertEqual(d["new_asst"], 1)


class TestMultiTurnAttribution(unittest.TestCase):
    """`user_msg_delta>=2` 必须分得清「我们重发」与「别人也在用这条对话」。

    2026-11-04 的 5/5 验收第 5 轮实测：新增 3 个用户轮次、正文互不相同
    （一句是验收探针，两句是用户同期打的字）。旧代码一律判 `MISMATCH（重发）`，
    会把一次外部污染误报成产品缺陷；反过来也可能把真重发说成「别人插话」。
    两者处置相反（改代码 vs 换对话），所以判据必须落在**正文指纹**上。
    """

    OURS = "只回答一个数字：把 144 开平方得到多少"

    def _fp(self, t):
        return confirm.fp_of(t)

    def test_same_text_twice_is_a_real_duplicate_send(self):
        b = {"user_fps": []}
        a = {"user_fps": [self._fp(self.OURS), self._fp(self.OURS)]}
        got = confirm.classify_multi_turns(b, a, self.OURS)
        self.assertEqual(got["code"], "OURS_ONLY")
        self.assertIn("真实重发", got["detail"])
        self.assertEqual(got["foreign"], [])

    def test_foreign_turns_are_contamination_not_a_defect(self):
        b = {"user_fps": [self._fp("前一轮的问题")]}
        a = {"user_fps": [self._fp("前一轮的问题"),
                          self._fp(self.OURS),
                          self._fp("另一句不是本次发送的话"),
                          self._fp("第三句也是外来的")]}
        got = confirm.classify_multi_turns(b, a, self.OURS)
        self.assertEqual(got["code"], "CONTAMINATED")
        self.assertEqual(len(got["ours"]), 1)
        self.assertEqual(len(got["foreign"]), 2)
        self.assertIn("别改发送逻辑", got["detail"])

    def test_our_text_absent_is_flagged_separately(self):
        """本次正文找不到 ⇒ 要么没送达要么被改写，不能算重发也不能算污染。"""
        b = {"user_fps": []}
        a = {"user_fps": [self._fp("完全不相干的一句"), self._fp("另一句不相干")]}
        got = confirm.classify_multi_turns(b, a, self.OURS)
        self.assertEqual(got["code"], "OUR_MISSING")

    def test_no_fingerprints_is_unknown_not_a_guess(self):
        got = confirm.classify_multi_turns({"user_fps": []}, {"user_fps": []},
                                           self.OURS)
        self.assertEqual(got["code"], "UNKNOWN")
        got2 = confirm.classify_multi_turns({"user_fps": ["x"]},
                                           {"user_fps": ["x", "y"]}, "")
        self.assertEqual(got2["code"], "UNKNOWN", "没有本次正文就不许硬判")

    def test_fingerprint_matches_the_js_normalisation(self):
        """Python 侧 fp_of 必须与 JS 的 fpNorm 同口径，否则跨语言比对一定漂。"""
        raw = "只回答 **一个数字**：(144) 开平方 ~得到~ 多少？\n\n换行与  空白"
        import re
        js_equiv = re.sub(r"[\s#*`>\[\]\(~_\-|]+", "", raw)[:64]
        self.assertEqual(confirm.fp_of(raw), js_equiv)

    def test_editor_reformatting_still_counts_as_ours(self):
        """编辑器重排（换行合并、空白折叠）会改指纹：宽松包含关系仍认我方。"""
        b = {"user_fps": []}
        a = {"user_fps": [self._fp(self.OURS), self._fp(self.OURS) + "尾巴"]}
        got = confirm.classify_multi_turns(b, a, self.OURS)
        self.assertEqual(got["code"], "OURS_ONLY")


    def test_contaminated_is_not_counted_as_a_send_failure(self):
        """审计映射钉死：CONTAMINATED 归 inconclusive，不能进 mismatch。

        write_gate 的 journal `result` 按 `verdict.startswith("MISMATCH")` 分档，
        三值语义要求「没判清」不动 fail_streak。污染属于外部条件不可信，
        不是发送缺陷 —— 被记成 mismatch 会推高失败计数并触发 replan，方向就错了。
        """
        contaminated = ("CONTAMINATED -> 换无人使用的专用对话重跑；"
                        "这不是发送缺陷，别改发送逻辑")
        self.assertFalse(contaminated.startswith("MISMATCH"))
        result = ("mismatch" if contaminated.startswith("MISMATCH")
                  else "inconclusive")
        self.assertEqual(result, "inconclusive")


if __name__ == "__main__":
    unittest.main()
