#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""执行纪律文档的验收单测（2026-11-02，未发布改动）。

四组改动各盯一件事：
- 裁定申请单：先裁后动的骨架字段必须齐全（少一段 = agent 又会先斩后奏）；
- confirm-gate 白话硬标准：通俗化规则在场（给人看的文件不许退化成机器腔）；
- SKILL 新章节：4.0 分阶段门 + 4.10 先裁定后动手，引用的文件必须真实存在；
- requirement-card / SKILL 2.2：白话对照与原样传话并存。
"""
from __future__ import annotations

import io
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts) -> str:
    with io.open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


class TestDecisionTemplate(unittest.TestCase):
    def setUp(self):
        self.text = read("templates", "web-ai-decision.md")

    def test_exists_with_core_sections(self):
        for needle in ("## 裁定申请：执行中发现一个岔路", "**正在执行**", "**现场情况**",
                       "**岔路是什么**", "**选项**", "**我的建议**",
                       "**在你裁定前，我什么都不动。**"):
            self.assertIn(needle, self.text, "缺骨架段：%s" % needle)

    def test_three_disciplines_present(self):
        for needle in ("先裁后动", "选项必须带影响", "不许夹带"):
            self.assertIn(needle, self.text)

    def test_counterexamples_present(self):
        self.assertIn("## 反例", self.text)
        self.assertIn("先斩后奏", self.text)

    def test_exemptions_listed(self):
        # 不触发条件要写清，否则 agent 拿裁定申请浪费轮次
        for needle in ("失败两次", "references/protocol.md", "§4.5"):
            self.assertIn(needle, self.text)


class TestConfirmGatePlainLanguage(unittest.TestCase):
    def setUp(self):
        self.text = read("templates", "confirm-gate.md")

    def test_plain_language_standard_present(self):
        self.assertIn("## 白话硬标准", self.text)
        for needle in ("术语必须带白话解释", "说后果，不说机制", "四年级自测"):
            self.assertIn(needle, self.text)

    def test_rewrite_examples_present(self):
        self.assertIn("反例 → 改写", self.text)
        # 反例必须真的比正例「更专业」：至少一组对照
        self.assertGreaterEqual(self.text.count("✅ 白话写法"), 1)


class TestSkillWiring(unittest.TestCase):
    def setUp(self):
        self.skill = read("SKILL.md")

    def test_staged_execution_section(self):
        self.assertIn("### 4.0 大任务先拆阶段", self.skill)
        for needle in ("预计超过 **6 轮**", "验收没过不进下一阶段", "templates/phase-report.md"):
            self.assertIn(needle, self.skill)

    def test_decision_before_action_section(self):
        self.assertIn("### 4.10 agent 有想法：先裁定，后动手", self.skill)
        self.assertIn("templates/web-ai-decision.md", self.skill)

    def test_plain_language_relay_rule(self):
        self.assertIn("白话对照", self.skill)
        self.assertIn("翻译仅供理解", self.skill)

    def test_referenced_docs_exist(self):
        for rel in ("references/browser-backends.md", "templates/web-ai-decision.md",
                    "templates/confirm-gate.md", "templates/phase-report.md",
                    "templates/requirement-card.md"):
            self.assertTrue(os.path.exists(os.path.join(ROOT, rel)),
                            "SKILL 引用的文件不存在：%s" % rel)

    def test_reference_list_updated(self):
        self.assertIn("references/browser-backends.md", self.skill)
        self.assertIn("裁定申请单", self.skill)


class TestPlainLanguageSemantics(unittest.TestCase):
    """D06/D07（GPT 2026-11-02 审计 P1）：翻译不得遗漏风险、不得替用户选择。"""

    def setUp(self):
        # 去粗体标记再断言：语义在，格式不该左右测试
        self.card = read("templates", "requirement-card.md").replace("**", "")
        self.gate = read("templates", "confirm-gate.md").replace("**", "")

    def test_d06_no_omitted_risk_rules_present(self):
        for text, name in ((self.card, "requirement-card"), (self.gate, "confirm-gate")):
            for needle in ("遗漏", "增加", "改变选项", "弱化风险"):
                self.assertIn(needle, text, "%s 缺语义自查项：%s" % (name, needle))
            self.assertIn("语义自查五问", text, "%s 缺五问清单" % name)

    def test_d07_translation_cannot_choose_for_user(self):
        for text, name in ((self.card, "requirement-card"), (self.gate, "confirm-gate")):
            self.assertIn("不得替用户选择", text, "%s 未写明不得代选" % name)
            self.assertIn("代替用户确认", text, "%s 未写明不得代确认" % name)

    def test_card_covers_four_stages(self):
        for stage in ("需求确认", "方案确认", "风险确认", "最终验收"):
            self.assertIn(stage, self.card, "白话标准未覆盖：%s" % stage)

    def test_translation_scope_unchanged(self):
        self.assertIn("范围、选项、风险、约束", self.card)
        self.assertIn("以原文为准", self.card)


class TestRequirementCardPlainLanguage(unittest.TestCase):
    def test_plain_language_note_in_unclear_section(self):
        text = read("templates", "requirement-card.md")
        self.assertIn("白话对照（2026-11-02）", text)
        self.assertIn("只记原话", text)

    def test_backend_compare_doc_conclusion(self):
        text = read("references", "browser-backends.md")
        for needle in ("Playwright", "登录态", "150–300MB", "playwright install"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
