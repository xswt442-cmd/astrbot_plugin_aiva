"""「开一局」的对局设定。

这一局是**推演出来的，不是真实战绩**。所有数字都由这里用随机数生成，不让模型
自己编——模型编的数字既不服从概率，看起来也不像真人。

按无畏契约的真实赛制来，别拿别的 FPS 套：

- 竞技模式先到 13 回合；12:12 进加时，加时要净胜 2 回合。
- 所以合法比分只有「13:x（x≤12）」和加时的「14:12、15:13……」。
- 一局总回合数就是比分之和，K/D/A 大致随回合数走。

设定字典的键保持中文：它是直接喂给提示词的字段名，`prompts.py` 和测试都按
这些键取值。
"""

from __future__ import annotations

import random
from typing import Any

__all__ = [
    "AGENT_POOL",
    "MAP_POOL",
    "build_match_setup",
    "overtime_score",
    "pick_agent",
    "pick_map",
    "regular_score",
    "setup_text",
]

# 竞技模式当前图池（国服名）。这份名单是从 Riot 自己的
# `valorant-api.com/v1/maps?language=zh-CN` 取的，**不是凭记忆写的** ——
# 凭记忆写错过四个（「亚述」「霜寒城谣」「莲华」「日落城」都是错的，
# 「禁地龛境」干脆是编出来的名字）。接口自己也会返回 used_map.name，
# 有数据时优先用他打过的图（见 `pick_map`）。
MAP_POOL = (
    "亚海悬城",  # Ascent
    "霓虹町",  # Split
    "裂变峡谷",  # Fracture
    "源工重镇",  # Bind
    "微风岛屿",  # Breeze
    "盐海矿镇",  # Haven
    "深海明珠",  # Pearl
    "森寒冬港",  # Icebox
    "莲华古城",  # Lotus
    "日落之城",  # Sunset
    "幽邃地窟",  # Abyss
)

# 四大定位与常选特工，名字同样取自有数据的 `valorant-api.com/v1/agents`。
# 推演里写定位，模型才好接着说：写「决斗」它能推进点节奏，
# 写「哨卫」它能推守点与蹲守。
AGENT_POOL = {
    "决斗": ("捷风", "雷兹", "不死鸟", "霓虹", "芮娜", "夜露", "幻棱"),
    "先锋": ("铁臂", "猎枭", "钛狐", "盖可", "黑梦", "斯凯", "K/O"),
    "控场": ("蝰蛇", "海神", "星礈", "迷核", "幽影", "暮蝶", "炼狱"),
    "哨卫": ("尚勃勒", "奇乐", "钢锁", "维斯", "零", "贤者", "禁灭"),
}

_ROLE_NOTES = {
    "决斗": "负责进点 opening，站在队伍第一个身位，也是第一枪大概率由他开",
    "先锋": "负责闪光与侦查，走在队伍第二个身位，进了点先把交叉火力点清掉",
    "控场": "负责切线压烟，进点前把关键枪线封住，决定队友能站在哪儿",
    "哨卫": "看住侧翼与回防，进点后留在后场听 rotation、拖住绕后的人",
}


def pick_map(rng: random.Random, preferred: tuple[str, ...] = ()) -> str:
    """挑一张图。

    `preferred` 传进来就用它——那是他真打过的图（`stats.地图一览`），
    名字也是接口给的，比这张常量表可靠。为空才退回常量表。
    """
    if preferred:
        return rng.choice(preferred)
    return rng.choice(MAP_POOL)


def pick_agent(rng: random.Random) -> tuple[str, str]:
    """返回一个 `(定位, 特工名)`。"""
    role = rng.choice(tuple(AGENT_POOL))
    return role, rng.choice(AGENT_POOL[role])


def regular_score(rng: random.Random) -> tuple[int, int]:
    """一个合法的常规时间比分：先到 13 且落差合理。"""
    if rng.random() < 0.5:
        loser = rng.choice((5, 7, 8, 9, 10, 11))
        return 13, loser
    loser = rng.choice((5, 7, 8, 9, 10, 11))
    return loser, 13


def overtime_score(rng: random.Random) -> tuple[int, int]:
    """加时：12:12 之后净胜 2 回合，先到 14 才算赢，之后每多一轮再加 1 分。

    这里直接给终值，形式上是 `n+2 : n`。
    """
    base = 12 + rng.choice((0, 1, 2, 3))
    winner = base + 2
    return (winner, base) if rng.random() < 0.5 else (base, winner)


def build_match_setup(rng: random.Random, summary: dict[str, Any] | None) -> dict[str, Any]:
    """生成一局的完整设定。

    `summary` 是玩家真实的近期统计（`aiva.stats.聚合` 的输出），用它决定这局的
    水平——没有就按中间档来。这样推演出来的表现贴着本人平时的水平，
    而不是凭空一个路人。
    """
    has_data = bool(summary and summary.get("场数"))

    # 自己的水平：默认中间档，不假装知道他什么水平。
    base_kd = summary.get("KDA") or 2.0 if has_data else 2.0
    base_acs = summary.get("场均ACS") or 220.0 if has_data else 220.0
    win_rate = summary.get("胜率") or 50.0 if has_data else 50.0

    # 胜负由他自己的胜率抽。交给模型抽就是连开十局全「超神」，它的随机是假的。
    # 收在 0.1~0.9：满了胜率也照样会输，一成胜率也照样会赢。
    won = rng.random() < max(0.1, min(0.9, win_rate / 100))

    if rng.random() < 0.12:  # 加时
        ours, theirs = overtime_score(rng)
    else:
        ours, theirs = regular_score(rng)
    if won and ours < theirs:
        ours, theirs = theirs, ours
    if not won and ours > theirs:
        ours, theirs = theirs, ours

    rounds = ours + theirs

    # K/D/A 跟着回合数走。一局 20 回合打出 5 杀或 40 杀都不像真的。
    kills_per_round = max(0.25, min(1.35, rng.gauss(base_kd / 3.2, 0.18)))
    kills = max(1, round(rounds * kills_per_round + (2 if won else -1)))
    deaths = max(1, round(rounds * random.uniform(0.45, 0.85)))
    assists = max(0, round(kills * rng.uniform(0.15, 0.5)))

    # ACS 围绕他自己的均值浮动，但**不随胜负大改**——输的局照样能打出高分，
    # 这是回合制的常态，别把「输」和「打得烂」绑定。
    acs = max(40, round(rng.gauss(base_acs, max(25, base_acs * 0.18))))

    # 爆头率手掌 Start：10%~35% 是绝大多数人的区间。
    headshot_rate = round(max(3.0, min(45.0, rng.gauss(19, 7))), 1)
    headshots = min(kills, round(kills * headshot_rate / 100))

    role, agent = pick_agent(rng)
    plants = rng.randint(0, 4) if rng.random() < 0.55 else 0
    defuses = rng.randint(0, 3) if rng.random() < 0.35 else 0

    # 优先打他真打过的图（名字用接口返回的原文），没有历史才用常量表。
    played_maps = tuple(r["地图"] for r in (summary.get("地图") or [])[:6]) if has_data else ()

    return {
        "地图": pick_map(rng, played_maps),
        "特工": agent,
        "定位": role,
        "定位说明": _ROLE_NOTES[role],
        "是否胜利": won,
        "比分": f"{ours}:{theirs}",
        "加时": rounds > 24,
        "回合数": rounds,
        "击杀": kills,
        "死亡": deaths,
        "助攻": assists,
        "ACS": acs,
        "爆头率": headshot_rate,
        "爆头数": headshots,
        "首杀": rng.randint(0, max(1, round(rounds * 0.3))),
        "下包": plants,
        "拆包": defuses,
        "残局": rng.choice(
            (
                "1v2 残局站住了",
                "1v3 被翻",
                "残局白给",
                "残局稳住防守拿下",
                "没遇到残局",
            )
        ),
        "走位": rng.choice(
            (
                "开局抢点激进",
                "守点架枪",
                "被打得退守",
                "绕后拿了个侧翼",
                "跟着队友进点",
            )
        ),
        "水平": (
            f"他近 {summary['场数']} 场自己水平："
            f"KDA {summary.get('KDA')}、ACS {summary.get('场均ACS')}"
            if has_data
            else "无历史数据，取中等水平"
        ),
    }


def setup_text(setup: dict[str, Any]) -> str:
    """把设定折成给模型读的文字。"""
    lines = [
        f"模式：竞技模式{'（打到加时）' if setup['加时'] else ''}",
        f"地图：{setup['地图']}",
        f"使用特工：{setup['特工']}（{setup['定位']}：{setup['定位说明']}）",
        f"比分：{setup['比分']}（{'胜' if setup['是否胜利'] else '负'}），"
        f"共 {setup['回合数']} 回合",
        f"击杀/死亡/助攻：{setup['击杀']}/{setup['死亡']}/{setup['助攻']}",
        f"ACS：{setup['ACS']}",
        f"爆头：{setup['爆头数']} 次，精准击败率 {setup['爆头率']}%",
        f"首杀次数：{setup['首杀']}",
        f"下包/拆包：{setup['下包']}/{setup['拆包']}",
        f"残局：{setup['残局']}",
        f"走位：{setup['走位']}",
        f"玩家水平：{setup['水平']}",
    ]
    return "\n".join(lines)
