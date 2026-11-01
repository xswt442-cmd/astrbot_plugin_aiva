"""提示词的措辞。

这些不是「文案好不好」的审美测试，而是**格式、反端水与反编造约束**：称号那
一行要以「你最近是个」开头，PK 不许推翻算好的对比，开一局必须写明是推演，
段位只能照抄数据。

模型很爱把这些约束自由发挥掉，改了提示就靠这几条兜住。
"""

from __future__ import annotations

from aiva.prompts import (
    FIELD_ALLOWLIST,
    FLUFF_WORDS,
    SIMULATION_NOTICE,
    TITLE_PREFIX,
    match_prompt,
    pk_prompt,
    review_prompt,
)

DATA = "当前段位 铂金3 ｜ 近20场 11胜9负 ｜ 胜率 55% ｜ KDA 1.42 ｜ ACS 231"

_CONTRAST = "ACS：甲占优（甲 231 / 乙 198）\nKDA：甲占优（甲 1.42 / 乙 1.10）"
_ADVANTAGE = "甲 2 项、乙 0 项"


class TestReviewPrompt:
    prompt = review_prompt(DATA)

    def test_carries_data(self):
        assert DATA in self.prompt

    def test_title_line_has_hard_prefix(self):
        assert TITLE_PREFIX in self.prompt
        assert TITLE_PREFIX == "你最近是个"

    def test_three_headings_present(self):
        for part in ("打得好：", "问题：", "建议："):
            assert part in self.prompt, f"缺小标题 {part}"

    def test_no_preamble(self):
        assert "不要前言" in self.prompt

    def test_requires_specific_numbers(self):
        assert "至少出现 3 个具体数字" in self.prompt

    def test_tier_must_be_copied(self):
        """编一个段位比不写段位恶劣得多：玩家会以为自己真是那一个段。"""
        assert "段位只能照抄数据里的「当前段位」那一句" in self.prompt
        assert "编一个段位比不写段位恶劣得多" in self.prompt

    def test_fluff_words_banned(self):
        for word in FLUFF_WORDS:
            assert word in self.prompt, f"端水词「{word}」应该写进禁令里"

    def test_forbids_making_things_up(self):
        assert "只说数据支撑得住的话" in self.prompt
        assert "宁可少说一句，也不要编一句" in self.prompt

    def test_allowlist_in_prompt(self):
        """掌瓦没有 ADR、KAST、经济，不写清楚模型会照 CS2 那套指标编。"""
        for part in FIELD_ALLOWLIST.split("。"):
            if part.strip():
                assert part.strip() in self.prompt

    def test_allowlist_names_missing_dimensions(self):
        for dimension in ("ADR", "KAST", "经济"):
            assert dimension in FIELD_ALLOWLIST

    def test_has_length_range(self):
        assert "180 到 260 字" in self.prompt


class TestMatchPrompt:
    prompt = match_prompt("设定")

    def test_states_it_is_simulated(self):
        """用户看到一串 K/D/A 看不出真假，第一行那句话是唯一的提示。"""
        assert self.prompt.count(SIMULATION_NOTICE) >= 2
        assert "不是真实战绩" in self.prompt

    def test_hard_rule_says_cannot_omit(self):
        assert f"第一行必须是「{SIMULATION_NOTICE}」" in self.prompt
        assert "不许改写、不许省略" in self.prompt

    def test_four_headings_present(self):
        for part in ("经过：", "亮点：", "问题：", "复盘："):
            assert part in self.prompt, f"缺小标题 {part}"

    def test_highlight_and_problem_need_numbers(self):
        assert "各必须出现设定里的至少一个数字" in self.prompt

    def test_forbids_making_things_up(self):
        assert "只说数据支撑得住的话" in self.prompt


class TestPkPrompt:
    def _prompt(self) -> str:
        return pk_prompt("老王", "小明", _CONTRAST, _ADVANTAGE, "老王数据", "小明数据")

    def test_cannot_overturn_computed_contrast(self):
        """让模型自己比，它最省事的答案就是「各有千秋」。"""
        prompt = self._prompt()

        assert "不要推翻对比结论" in prompt
        assert "已经替你算好了" in prompt

    def test_asks_for_both_numbers(self):
        assert "必须写出项名与双方数字" in self._prompt()

    def test_advantage_is_reference_only(self):
        prompt = self._prompt()

        assert "占优项更多的一方不一定分数更高" in prompt
        assert "仅供参考" in prompt

    def test_score_gap_matches_reality(self):
        """分差不该为了制造对立硬拉开，两人本来就接近时就该接近。"""
        prompt = self._prompt()

        assert "分差要跟两人的实际差距相符" in prompt
        assert "敢于给大的分数差距" not in prompt

    def test_winner_matches_score(self):
        """分数与胜者由模型各写各的，不钉住就会出现 88 分输给 70 分。"""
        prompt = self._prompt()

        assert "胜者那一行写分数更高的那个人" in prompt
        assert "老王 或 小明" in prompt

    def test_both_sides_data_included(self):
        prompt = self._prompt()

        assert "【老王】原始数据\n老王数据" in prompt
        assert "【小明】原始数据\n小明数据" in prompt

    def test_fluff_words_banned(self):
        prompt = self._prompt()

        for word in FLUFF_WORDS:
            assert word in prompt

    def test_forbids_making_things_up(self):
        assert "只说数据支撑得住的话" in self._prompt()

    def test_roast_has_min_length(self):
        assert "至少 90 字" in self._prompt()

    def test_roast_targets_loser(self):
        """两边都重点攻击等于没有倾向，反而像端水。"""
        prompt = self._prompt()

        assert "主要攻击输的一方最难看的那一项" in prompt
        assert "赢的一方顺带攻击一下" in prompt
        assert "不要两边都重点攻击" in prompt
