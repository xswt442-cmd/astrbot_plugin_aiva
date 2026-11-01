"""聚合层的测试。

这批数据都是假的，但**结构照第三方那份字段文档来**——真接上以后如果发现字段名
变了，应该回来改 `stats.py` 并在这里补一个用例，而不是改提示词去迁就。
"""

from __future__ import annotations

import random

import pytest

from aiva.match import MAP_POOL, build_match_setup, overtime_score, regular_score, setup_text
from aiva.stats import 拆KDA, 一场, 占优项数, 对比文本, 对比要点, 看胜负, 聚合


def 一局(
    结果="获胜",
    kda="18/12/5",
    acs=245,
    地图="亚海悬城",
    特工="捷风",
    模式="competitive",
    己方=13,
    对方=9,
    **其余,
):
    项 = {
        "result_title": 结果,
        "kda": kda,
        "score_avg": acs,
        "used_map": {"name": 地图},
        "hero_name": 特工,
        "game_enum": 模式,
        "game_mode": "竞技模式" if 模式 == "competitive" else "普通模式",
        "round_won": 己方,
        "round_fail": 对方,
    }
    项.update(其余)
    return 项


class Test拆KDA:
    def test_正常字符串(self):
        assert 拆KDA("13/18/10") == (13, 18, 10)

    def test_缺字段记零(self):
        assert 拆KDA("13/18") == (0, 0, 0)

    def test_空值记零(self):
        assert 拆KDA(None) == (0, 0, 0)
        assert 拆KDA("") == (0, 0, 0)

    def test_列表也能拆(self):
        assert 拆KDA([5, 6, 7]) == (5, 6, 7)

    def test_带空格也行(self):
        assert 拆KDA(" 4 / 5 / 6 ") == (4, 5, 6)


class Test聚合:
    def test_空列表不给假数据(self):
        摘要 = 聚合([])
        assert 摘要 == {"场数": 0}

    def test_kda拆不出来的局会被丢掉(self):
        # 这种局的 KDA 是 (0,0,0)，混进去会把均值拉到接近 0，
        # 那是个看起来有理、实际是错的数。
        摘要 = 聚合([一局(kda=""), 一局(kda="13/18/10")])
        assert 摘要["场数"] == 1

    def test_不计分模式不进统计(self):
        assert 聚合([一局(模式="deathmatch")])["场数"] == 0
        assert 聚合([一局(模式="teamdeathmatch")])["场数"] == 0

    def test_胜负与胜率(self):
        数据 = [一局(结果="获胜")] * 6 + [一局(结果="失败")] * 4
        摘要 = 聚合(数据)
        assert (摘要["胜"], 摘要["负"]) == (6, 4)
        assert 摘要["胜率"] == 60.0

    def test_平局不计入胜率分母(self):
        摘要 = 聚合([一局(结果="获胜"), 一局(结果="失败"), 一局(结果="平局")])
        assert 摘要["平"] == 1
        assert 摘要["胜率"] == 50.0

    def test_场均值(self):
        摘要 = 聚合([一局(kda="20/10/5"), 一局(kda="10/10/5")])
        assert 摘要["场均击杀"] == 15.0
        assert 摘要["场均助攻"] == 5.0

    def test_ACS缺失就是None不许补零(self):
        摘要 = 聚合([一局(acs=""), 一局(acs="")])
        assert 摘要["场均ACS"] is None

    def test_acs为字符串也能算(self):
        摘要 = 聚合([一局(acs="200"), 一局(acs="300")])
        assert 摘要["场均ACS"] == 250.0

    def test_地图聚合按场次降序(self):
        数据 = [一局(地图="深海明珠")] * 3 + [一局(地图="霓虹町")] * 5
        地图 = 聚合(数据)["地图"]
        assert [r["地图"] for r in 地图] == ["霓虹町", "深海明珠"]
        assert 地图[0]["场次"] == 5

    def test_走势按原顺序最新在前(self):
        数据 = [一局(结果="失败"), 一局(结果="获胜"), 一局(结果="获胜")]
        assert 聚合(数据)["走势"] == ["负", "胜", "胜"]


class Test对比:
    def test_有稀缺就不比(self):
        assert 对比要点({"场数": 10}, {"场数": 0}) == []

    def test_死亡越少越好(self):
        甲 = 聚合([一局(kda="10/5/1")])
        乙 = 聚合([一局(kda="10/20/1")])
        要点 = {条["项"]: 条 for 条 in 对比要点(甲, 乙)}
        assert 要点["场均死亡"]["优"] == "甲"

    def test_文本要说清谁占优(self):
        甲 = 聚合([一局(kda="30/5/1"), 一局(kda="30/5/1")])
        乙 = 聚合([一局(kda="5/30/1"), 一局(kda="5/30/1")])
        文本 = 对比文本(对比要点(甲, 乙), "阿甲", "阿乙")
        assert "阿甲占优" in 文本

    def test_占优项数是事实不是结论(self):
        要点 = [
            {"项": "a", "优": "甲"},
            {"项": "b", "优": "甲"},
            {"项": "c", "优": "乙"},
        ]
        assert 占优项数(要点, "甲", "乙") == "甲 2 项、乙 1 项"


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
        summary = 聚合([一局(kda="25/8/3", acs=320, 结果="获胜")] * 8)
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


class Test一场:
    def test_零死时KDA比值不炸(self):
        局 = 一场(一局(kda="10/0/2"))
        assert 局.KDA比值 == 12.0

    def test_正常比值(self):
        assert 一场(一局(kda="18/12/5")).KDA比值 == pytest.approx((18 + 5) / 12)


class Test胜负归一:
    @pytest.mark.parametrize(
        "原文,期望",
        [
            ("获胜", "胜"),
            ("失败", "负"),
            ("平局", "平"),
            ("", "未知"),
            (None, "未知"),
        ],
    )
    def test_中文结果(self, 原文, 期望):
        assert 看胜负(原文) == 期望


# 这两个符号在测试里被引用但不应该存在——留着就是为了确认它们**没有**被
# 重新引入。stats 里曾经混进过两个拼错的导出（"逐party拼接"），那种东西
# 会让 `from aiva.stats import *` 拿到一堆怪名字。
def test_导出列表干净():
    import aiva.stats as 模块

    for 名 in 模块.__all__:
        assert hasattr(模块, 名), f"__all__ 里写了 {名} 但模块里没有"
