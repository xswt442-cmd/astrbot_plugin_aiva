"""聚合层的测试。

这批数据都是假的，但**结构照第三方那份字段文档来**——真接上以后如果发现字段名
变了，应该回来改 `stats.py` 并在这里补一个用例，而不是改提示词去迁就。
"""

from __future__ import annotations

import random

import pytest

from aiva.match import MAP_POOL, build_match_setup, overtime_score, regular_score, setup_text
from aiva.stats import (
    Match,
    advantage_count,
    aggregate,
    contrast_points,
    contrast_text,
    normalize_outcome,
    split_kda,
)


def make_match(
    result="获胜",
    kda="18/12/5",
    acs=245,
    map_name="亚海悬城",
    agent="捷风",
    mode="competitive",
    ours=13,
    theirs=9,
    **extra,
):
    item = {
        "result_title": result,
        "kda": kda,
        "score_avg": acs,
        "used_map": {"name": map_name},
        "hero_name": agent,
        "game_enum": mode,
        "game_mode": "竞技模式" if mode == "competitive" else "普通模式",
        "round_won": ours,
        "round_fail": theirs,
    }
    item.update(extra)
    return item


class TestSplitKda:
    def test_normal_string(self):
        assert split_kda("13/18/10") == (13, 18, 10)

    def test_missing_field_is_zero(self):
        assert split_kda("13/18") == (0, 0, 0)

    def test_empty_is_zero(self):
        assert split_kda(None) == (0, 0, 0)
        assert split_kda("") == (0, 0, 0)

    def test_list_also_works(self):
        assert split_kda([5, 6, 7]) == (5, 6, 7)

    def test_spaces_allowed(self):
        assert split_kda(" 4 / 5 / 6 ") == (4, 5, 6)


class TestAggregate:
    def test_empty_list_returns_no_fake_data(self):
        summary = aggregate([])
        assert summary == {"场数": 0}

    def test_unparsable_kda_is_dropped(self):
        # 这种局的 KDA 是 (0,0,0)，混进去会把均值拉到接近 0，
        # 那是个看起来有理、实际是错的数。
        summary = aggregate([make_match(kda=""), make_match(kda="13/18/10")])
        assert summary["场数"] == 1

    def test_unscored_modes_excluded(self):
        assert aggregate([make_match(mode="deathmatch")])["场数"] == 0
        assert aggregate([make_match(mode="teamdeathmatch")])["场数"] == 0

    def test_wins_and_win_rate(self):
        rows = [make_match(result="获胜")] * 6 + [make_match(result="失败")] * 4
        summary = aggregate(rows)
        assert (summary["胜"], summary["负"]) == (6, 4)
        assert summary["胜率"] == 60.0

    def test_draws_not_in_denominator(self):
        summary = aggregate(
            [make_match(result="获胜"), make_match(result="失败"), make_match(result="平局")]
        )
        assert summary["平"] == 1
        assert summary["胜率"] == 50.0

    def test_per_match_averages(self):
        summary = aggregate([make_match(kda="20/10/5"), make_match(kda="10/10/5")])
        assert summary["场均击杀"] == 15.0
        assert summary["场均助攻"] == 5.0

    def test_missing_acs_is_none(self):
        summary = aggregate([make_match(acs=""), make_match(acs="")])
        assert summary["场均ACS"] is None

    def test_acs_as_string_still_counts(self):
        summary = aggregate([make_match(acs="200"), make_match(acs="300")])
        assert summary["场均ACS"] == 250.0

    def test_map_breakdown_sorted_by_count(self):
        rows = [make_match(map_name="深海明珠")] * 3 + [make_match(map_name="霓虹町")] * 5
        maps = aggregate(rows)["地图"]
        assert [r["地图"] for r in maps] == ["霓虹町", "深海明珠"]
        assert maps[0]["场次"] == 5

    def test_streak_keeps_source_order(self):
        rows = [make_match(result="失败"), make_match(result="获胜"), make_match(result="获胜")]
        assert aggregate(rows)["走势"] == ["负", "胜", "胜"]


class TestContrast:
    def test_skips_when_one_side_empty(self):
        assert contrast_points({"场数": 10}, {"场数": 0}) == []

    def test_fewer_deaths_is_better(self):
        a = aggregate([make_match(kda="10/5/1")])
        b = aggregate([make_match(kda="10/20/1")])
        points = {point["项"]: point for point in contrast_points(a, b)}
        assert points["场均死亡"]["优"] == "甲"

    def test_text_names_the_better_side(self):
        a = aggregate([make_match(kda="30/5/1"), make_match(kda="30/5/1")])
        b = aggregate([make_match(kda="5/30/1"), make_match(kda="5/30/1")])
        text = contrast_text(contrast_points(a, b), "阿甲", "阿乙")
        assert "阿甲占优" in text

    def test_advantage_count_is_fact(self):
        points = [
            {"项": "a", "优": "甲"},
            {"项": "b", "优": "甲"},
            {"项": "c", "优": "乙"},
        ]
        assert advantage_count(points, "甲", "乙") == "甲 2 项、乙 1 项"


class TestMatchSetup:
    def test_regular_score_is_legal(self):
        for _ in range(200):
            a, b = regular_score(random.Random())
            assert max(a, b) == 13, (a, b)
            assert min(a, b) <= 12

    def test_overtime_wins_by_two(self):
        for _ in range(200):
            a, b = overtime_score(random.Random())
            assert abs(a - b) == 2
            assert max(a, b) >= 14

    def test_score_matches_win_flag(self):
        for rng in (random.Random(i) for i in range(300)):
            setup = build_match_setup(rng, None)
            ours, theirs = (int(x) for x in setup["比分"].split(":"))
            assert (ours > theirs) == setup["是否胜利"]
            if setup["加时"]:
                assert abs(ours - theirs) == 2
            else:
                assert max(ours, theirs) == 13

    def test_rounds_equal_score_sum(self):
        setup = build_match_setup(random.Random(7), None)
        assert setup["回合数"] == sum(int(x) for x in setup["比分"].split(":"))

    def test_deaths_are_sane(self):
        for i in range(200):
            setup = build_match_setup(random.Random(i), None)
            assert setup["死亡"] >= 1
            assert setup["击杀"] >= 1
            assert setup["回合数"] >= 10

    def test_headshots_not_more_than_kills(self):
        for i in range(200):
            setup = build_match_setup(random.Random(i), None)
            assert 0 <= setup["爆头数"] <= setup["击杀"]

    def test_uses_real_stats_when_available(self):
        summary = aggregate([make_match(kda="25/8/3", acs=320, result="获胜")] * 8)
        setup = build_match_setup(random.Random(3), summary)
        # 一个 8 连胜、ACS 320 的人，推演出来的 ACS 不该掉到 100 上下
        assert setup["ACS"] > 200

    def test_setup_text_has_required_fields(self):
        text = setup_text(build_match_setup(random.Random(1), None))
        for field in ("地图：", "使用特工：", "比分：", "ACS：", "精准击败率"):
            assert field in text

    def test_map_name_comes_from_official_pool(self):
        # 这批中文名是从 valorant-api.com 取的，不许手改成「自造名」
        setup = build_match_setup(random.Random(9), None)
        assert setup["地图"] in MAP_POOL


class TestMatchRecord:
    def test_zero_deaths_ratio_does_not_explode(self):
        record = Match(make_match(kda="10/0/2"))
        assert record.kda_ratio == 12.0

    def test_normal_ratio(self):
        assert Match(make_match(kda="18/12/5")).kda_ratio == pytest.approx((18 + 5) / 12)


class TestNormalizeOutcome:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("获胜", "胜"),
            ("失败", "负"),
            ("平局", "平"),
            ("", "未知"),
            (None, "未知"),
        ],
    )
    def test_chinese_results(self, raw, expected):
        assert normalize_outcome(raw) == expected


def test_export_list_is_clean():
    import aiva.stats as module

    for name in module.__all__:
        assert hasattr(module, name), f"__all__ 里写了 {name} 但模块里没有"
