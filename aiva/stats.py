"""把掌瓦的对局列表聚成一组可以直接念出来的指标。

**原则：这里算出来的数字是最终答案。** 模型只负责照着说，
不许重算、不许「约」「大概」、不许补一个这里没给的数。

字段名来自第三方那份 `API_FIELDS.md`（标称基于掌瓦 2.7.1.10064），
本仓库 **尚未用自己的登录态逐字段核对过**——首次绑定后要用
`test/verify_mval.py` 跑一遍，对不上的地方回来改这里，不要改提示词去迁就。

两处已知的坑，写在最前面：

1. `kda` 是 `"13/18/10"` 这种**字符串**，不是三个整数。
2. `result_title` 是 `"获胜"` / `"失败"` / `"平局"` 的**中文**，别按首字母判断。

摘要字典的键保持中文（`"场数"`、`"胜率"`、`"地图"`……）：它们是喂给提示词和
`match.py` 的字段名，改了要连着 prompts、match 和测试一起动。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "MAX_MATCHES",
    "Match",
    "advantage_count",
    "agent_breakdown",
    "aggregate",
    "contrast_points",
    "contrast_text",
    "is_scored_mode",
    "map_breakdown",
    "normalize_outcome",
    "recent_streak",
    "recent_text",
    "split_kda",
]

# 掌瓦只对自己的一个 account 返回最近若干场，取太多会拖慢，
# 而模型面对 40 场也不会比 20 场说得更好。
MAX_MATCHES = 20

# 只统计这两种模式的对局。死斗/斗牛/爆能快攻的 KDA 与 ACS 量纲不同，
# 混进来会把均值拉成一个没有意义的数字。
_SCORED_MODES = frozenset({"competitive", "unrated", "premier", "swiftplay"})

_WIN_TEXTS = frozenset({"获胜", "胜利", "胜"})
_LOSS_TEXTS = frozenset({"失败", "落败", "负"})
_DRAW_TEXTS = frozenset({"平局", "和"})


class Match:
    """单局数据的一个薄壳。字段缺失一律给 None / 0，**不猜**。"""

    __slots__ = (
        "mode",
        "map_name",
        "outcome",
        "kills",
        "deaths",
        "assists",
        "acs",
        "grade",
        "agent",
        "rounds_won",
        "rounds_lost",
        "time",
    )

    def __init__(self, raw: dict[str, Any]) -> None:
        self.mode = str(raw.get("game_mode") or "")
        self.map_name = ((raw.get("used_map") or {}).get("name") or "").strip()
        self.outcome = normalize_outcome(raw.get("result_title"))
        self.kills, self.deaths, self.assists = split_kda(raw.get("kda"))
        self.acs = _to_number(raw.get("score_avg"))
        grade = raw.get("score_level") or {}
        self.grade = str(grade.get("level") or "").strip()
        agent = str(raw.get("hero_name") or "").strip()
        self.agent = agent or "—"
        self.rounds_won = _to_number(raw.get("round_won"))
        self.rounds_lost = _to_number(raw.get("round_fail"))
        self.time = str(raw.get("time") or "")

    @property
    def kda_ratio(self) -> float:
        if self.deaths <= 0:
            # 0 死不该除出 Infinity，也不能记 0 分。按「击杀+助攻」处理，
            # 写清这是它的上限而不是真实比值。
            return float(self.kills + self.assists)
        return (self.kills + self.assists) / self.deaths


def is_scored_mode(raw: dict[str, Any]) -> bool:
    """只留完整的 5v5 回合制模式。"""
    enum = str(raw.get("game_enum") or "").strip().lower()
    if not enum:
        # 没给枚举时按中文模式名兜底：里面含「死斗」「斗牛」的一律不要。
        mode = str(raw.get("game_mode") or "")
        return not any(word in mode for word in ("死斗", "斗牛", "单人"))
    return enum in _SCORED_MODES


def normalize_outcome(text: Any) -> str:
    """把 result_title 归一成 胜/负/平/未知。"""
    value = str(text or "").strip()
    if value in _WIN_TEXTS:
        return "胜"
    if value in _LOSS_TEXTS:
        return "负"
    if value in _DRAW_TEXTS:
        return "平"
    return "未知"


def split_kda(value: Any) -> tuple[int, int, int]:
    """`"13/18/10"` → (13, 18, 10)。

    拆不动就记 (0, 0, 0)，但上层要把这种局排除掉，避免拉低均值。
    """
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        parts = value[:3]
    else:
        parts = str(value or "").split("/")
    if len(parts) < 3:
        return (0, 0, 0)
    try:
        return (_to_number(parts[0]), _to_number(parts[1]), _to_number(parts[2]))
    except (TypeError, ValueError):
        return (0, 0, 0)


def _to_number(value: Any) -> float:
    """字段类型在 string/int 之间摇摆（同一个字段不同场次可能不同），统一转。"""
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace("%", "").replace(",", "").strip())
    except ValueError:
        return 0.0


def aggregate(raw_list: list[Any]) -> dict[str, Any]:
    """把一页原始数据聚成一份摘要。

    返回的东西**全部是算好的值**，模型照抄即可。缺失的项一律 None，
    让它说「这一项没有」，而不是让它自己估一个。
    """
    matches: list[Match] = []
    for item in raw_list or []:
        if not isinstance(item, dict):
            continue
        if not is_scored_mode(item):
            continue
        record = Match(item)
        if record.kills == 0 and record.deaths == 0 and record.assists == 0:
            # KDA 拆不出来的局直接丢掉：均值会被一堆 0 拉到毫无意义。
            continue
        matches.append(record)

    total = len(matches)
    if total == 0:
        return {"场数": 0}

    wins = sum(1 for g in matches if g.outcome == "胜")
    losses = sum(1 for g in matches if g.outcome == "负")
    draws = sum(1 for g in matches if g.outcome == "平")

    def mean_of(getter) -> float:
        return round(sum(getter(g) for g in matches) / total, 1)

    acs_list = [g.acs for g in matches if g.acs > 0]
    return {
        "场数": total,
        "胜": wins,
        "负": losses,
        "平": draws,
        "胜率": round(wins / max(wins + losses, 1) * 100, 1),
        "场均击杀": mean_of(lambda g: g.kills),
        "场均死亡": mean_of(lambda g: g.deaths),
        "场均助攻": mean_of(lambda g: g.assists),
        "KDA": round(sum(g.kda_ratio for g in matches) / total, 2),
        "场均ACS": round(sum(acs_list) / len(acs_list), 1) if acs_list else None,
        "场均回合差": round(sum(g.rounds_won - g.rounds_lost for g in matches) / total, 1),
        "地图": map_breakdown(matches),
        "特工": agent_breakdown(matches),
        "走势": recent_streak(matches),
    }


def map_breakdown(matches: list[Match]) -> list[dict[str, Any]]:
    """按地图聚合场次与胜率，按场次降序。"""
    table: dict[str, dict[str, Any]] = {}
    for g in matches:
        name = g.map_name or "未知"
        row = table.setdefault(name, {"地图": name, "场次": 0, "胜": 0})
        row["场次"] += 1
        if g.outcome == "胜":
            row["胜"] += 1
    for row in table.values():
        row["胜率"] = round(row["胜"] / row["场次"] * 100, 1)
    return sorted(table.values(), key=lambda r: -r["场次"])


def agent_breakdown(matches: list[Match], top_n: int = 5) -> list[dict[str, Any]]:
    """按特工聚合，取用得最多的几个。"""
    table: dict[str, dict[str, Any]] = {}
    for g in matches:
        row = table.setdefault(g.agent, {"特工": g.agent, "场次": 0, "胜": 0, "_ACS": 0.0, "_n": 0})
        row["场次"] += 1
        if g.outcome == "胜":
            row["胜"] += 1
        if g.acs > 0:
            row["_ACS"] += g.acs
            row["_n"] += 1
    rows: list[dict[str, Any]] = []
    for row in table.values():
        row["胜率"] = round(row["胜"] / row["场次"] * 100, 1)
        row["场均ACS"] = round(row["_ACS"] / row["_n"], 1) if row["_n"] else None
        rows.append({k: v for k, v in row.items() if not k.startswith("_")})
    rows.sort(key=lambda r: -r["场次"])
    return rows[:top_n]


def recent_streak(matches: list[Match], limit: int = 10) -> list[str]:
    """最近若干场的胜负序列，**按原始数据顺序**，最前面是最新的一场。

    掌瓦的列表本来就是倒序（新的在前）。这里不做任何额外排序，
    免得把「最近五连败」排成别的样子。
    """
    return [g.outcome for g in matches[:limit]]


def contrast_points(a: dict[str, Any], b: dict[str, Any]) -> list[dict[str, str]]:
    """逐项比出高低。返回 `[{"项":…, "甲":…, "乙":…, "优":"甲"|"乙"|"持平"}]`。

    对比**由代码算死，不交给模型**：让模型自己比，它最省事的答案就是
    「各有千秋」这类端水话。每项都算清谁占优、双方数字多少，模型的活只剩
    解说，端水就没地方藏。
    """
    points: list[dict[str, str]] = []

    # 有一方没数据就不比。空统计里各项都是 None，逐项比下来是一串
    # 「持平」，看着像「两人一样强」，其实是「没数据」。
    if not a.get("场数") or not b.get("场数"):
        return points

    def add_item(
        title: str,
        a_value: float | None,
        b_value: float | None,
        *,
        lower_is_better: bool = False,
        unit: str = "",
        digits: int = 1,
    ) -> None:
        if a_value is None or b_value is None:
            return
        template = f"{{:.{digits}f}}"
        a_text = template.format(a_value) + unit
        b_text = template.format(b_value) + unit
        if a_value == b_value:
            better = "持平"
        elif (a_value > b_value) != lower_is_better:
            better = "甲"
        else:
            better = "乙"
        points.append({"项": title, "甲": a_text, "乙": b_text, "优": better})

    add_item("胜率", a.get("胜率"), b.get("胜率"), unit="%")
    add_item("KDA", a.get("KDA"), b.get("KDA"), digits=2)
    add_item("场均ACS", a.get("场均ACS"), b.get("场均ACS"))
    add_item("场均击杀", a.get("场均击杀"), b.get("场均击杀"))
    add_item("场均死亡", a.get("场均死亡"), b.get("场均死亡"), lower_is_better=True)
    add_item("场均助攻", a.get("场均助攻"), b.get("场均助攻"))
    add_item("回合净胜", a.get("场均回合差"), b.get("场均回合差"))
    add_item("统计场数", a.get("场数"), b.get("场数"), digits=0, unit=" 场")
    return points


def contrast_text(points: list[dict[str, str]], a_name: str, b_name: str) -> str:
    """把对比要点折成给模型读的文本。每一行都直接说明谁占优。"""
    lines = []
    for point in points:
        if point["优"] == "持平":
            lines.append(f"{point['项']}：持平（{a_name} {point['甲']} / {b_name} {point['乙']}）")
        else:
            better_name = a_name if point["优"] == "甲" else b_name
            lines.append(
                f"{point['项']}：{better_name}占优"
                f"（{a_name} {point['甲']} / {b_name} {point['乙']}）"
            )
    return "\n".join(lines)


def advantage_count(points: list[dict[str, str]], a_name: str, b_name: str) -> str:
    """「甲 5 项、乙 2 项」这样的汇总，写进提示里当参考。

    刻意**不**把它写成「占优多的一方必须赢」：项数是机械数出来的，而实际谁更强
    要看是哪几项——赢面判断留给模型，这里只提供事实。
    """
    a_count = sum(1 for point in points if point["优"] == "甲")
    b_count = sum(1 for point in points if point["优"] == "乙")
    return f"{a_name} {a_count} 项、{b_name} {b_count} 项"


def recent_text(summary: dict[str, Any], tier: str = "") -> str:
    """把摘要拼成一段给模型的文本。

    这一段是模型的**全部事实来源**：这里没有的数字，它就不该说。

    `tier` 从主角色接口单独取（对局列表里没有这个字段）。锐评的提示词要求
    称号里带段位，以前这里不给，模型就自己编——线上真出过钻石玩家被评成
    「铂金」。取不到就明说取不到，别让模型猜。
    """
    if not summary.get("场数"):
        return "（没有可统计的对局）"

    lines = [
        f"近 {summary['场数']} 场：{summary['胜']}胜 {summary['负']}负"
        + (f" {summary['平']}平" if summary.get("平") else ""),
        f"胜率 {summary['胜率']}%",
        f"场均 K/D/A {summary['场均击杀']}/{summary['场均死亡']}/{summary['场均助攻']}"
        f"（KDA {summary['KDA']}）",
    ]
    if summary.get("场均ACS") is not None:
        lines.append(f"场均 ACS {summary['场均ACS']}")
    else:
        lines.append("场均 ACS 未取到")

    lines.append(f"场均回合净胜 {summary['场均回合差']}")

    # 段位放最前面：提示词要求称号里带它，模型先看到才会照抄。
    if tier:
        lines.insert(0, f"当前段位 {tier}")
    else:
        lines.insert(0, "当前段位 接口没给（**称号里不许提段位**，不要猜）")

    if summary.get("走势"):
        marks = {"胜": "W", "负": "L", "平": "D", "未知": "?"}
        lines.append("最近走势 " + " ".join(marks.get(s, "?") for s in summary["走势"]))

    if summary.get("地图"):
        map_part = "、".join(f"{r['地图']} {r['场次']}场/{r['胜率']}%" for r in summary["地图"][:5])
        lines.append(f"地图 {map_part}")

    if summary.get("特工"):
        agent_part = "、".join(
            f"{r['特工']} {r['场次']}场"
            + (f"/{r['场均ACS']} ACS" if r["场均ACS"] is not None else "")
            for r in summary["特工"]
        )
        lines.append(f"常用特工 {agent_part}")

    return "\n".join(lines)
