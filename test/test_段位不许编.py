"""段位不许被编。

线上真事：钻石II 玩家被锐评成「你最近是个铂金捷风尖刀」。

根因是提示词要求「称号里要带上他的段位」，而段位**只存在于主角色接口**
（`tier_text`），对局列表里根本没有。以前喂给模型的数据里没有段位，
模型就自己编了一个——而且编得很合理，所以没人当场发现。

这里锁死三件事：
1. 段位必须真的进到喂给模型的文本里；
2. 取不到段位时必须明说「接口没给」，而不是留空让模型猜；
3. 提示词必须写死「只准照抄」。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

仓库 = Path(__file__).resolve().parent.parent


def _读(名: str) -> str:
    return (仓库 / "aiva" / 名).read_text(encoding="utf-8")


class Test段位取数:
    def test_取段位只认tier_text(self):
        from aiva.transport import 取段位

        assert 取段位({"tier_text": "钻石II"}) == "钻石II"
        assert 取段位({"tier_text": "  铂金I "}) == "铂金I"
        # tier 是数字档位，没有映射表就不能用——猜错就是又一句编的段位
        assert 取段位({"tier": 19}) == ""
        assert 取段位({}) == ""

    def test_取对局要一起返回角色(self):
        源码 = _读("transport.py")
        树 = ast.parse(源码)
        函数 = None
        for 节点 in ast.walk(树):
            if isinstance(节点, ast.AsyncFunctionDef) and 节点.name == "取对局":
                函数 = 节点
        assert 函数 is not None
        片段 = ast.get_source_segment(源码, 函数) or ""
        assert "角色" in 片段, "取对局必须把主角色信息带出来，段位在里面"
        assert "return (" in 片段, "取对局应返回 (对局列表, 角色) 二元组"


class Test段位进文本:
    def _摘要(self) -> dict:
        # 字段名要对齐 `aiva/stats.py` 真实读的：胜负看 `result_title`
        # （不是 `result`），模式过滤看 `game_enum`。写错字段名的话
        # `聚合` 会把所有场次判成「无场次」，测试就会假失败。
        局 = [
            {
                "game_enum": "competitive",
                "result_title": "胜利",
                "used_map": {"name": "日落之城"},
                "kda": [20, 5, 4],
                "score": 250,
            }
            for _ in range(4)
        ]
        from aiva.stats import aggregate

        summary = aggregate(局)
        assert summary.get("场数") == 4, f"测试数据不对，聚合出 {summary.get('场数')} 场"
        return summary

    def test_tier_written_when_present(self):
        from aiva.stats import recent_text

        text = recent_text(self._摘要(), "钻石II")
        assert "钻石II" in text
        assert "当前段位" in text

    def test_tier_goes_first(self):
        """模型先看到段位才会照抄。放末尾容易被长数据挤掉注意力。"""
        from aiva.stats import recent_text

        lines = recent_text(self._摘要(), "钻石II").splitlines()
        assert lines[0].startswith("当前段位"), f"段位应在首行，实际是：{lines[0]}"

    def test_missing_tier_says_so(self):
        """留空 = 模型自己编。必须明说「接口没给」。"""
        from aiva.stats import recent_text

        text = recent_text(self._摘要(), "")
        assert "接口没给" in text
        assert "不许提段位" in text

    def test_no_tier_by_default(self):
        from aiva.stats import recent_text

        assert "接口没给" in recent_text(self._摘要())


class Test提示约束:
    def test_必须写死只准照抄(self):
        源码 = _读("prompts.py")
        assert "只能照抄" in 源码, "提示词必须写死「段位只准照抄数据里的」"
        assert "一个字都不许改" in 源码

    def test_必须禁止推断(self):
        源码 = _读("prompts.py")
        assert "不许推断" in 源码


class Test调用点已更新:
    def test_每个取对局调用都解包二元组(self):
        """`取对局` 现在返回 `(列表, 角色)`。

        只改成「拿列表」会让角色悄悄丢掉——段位又变回没数据，模型接着编。
        用 AST 查解包目标个数，比字符串匹配可靠。
        """
        源码 = (仓库 / "main.py").read_text(encoding="utf-8")
        树 = ast.parse(源码)
        找到 = []
        for 节点 in ast.walk(树):
            if not isinstance(节点, (ast.Assign, ast.AnnAssign)):
                continue
            值 = 节点.value
            if not isinstance(值, ast.Await):
                continue
            调用 = 值.value
            if not (
                isinstance(调用, ast.Call)
                and isinstance(调用.func, ast.Name)
                and 调用.func.id == "取对局"
            ):
                continue
            目标 = 节点.targets if isinstance(节点, ast.Assign) else [节点.target]
            元组 = 目标[0] if len(目标) == 1 else None
            个数 = len(元组.elts) if isinstance(元组, ast.Tuple) else 1
            找到.append((节点.lineno, 个数))

        assert 找到, "main.py 里一个 取对局 调用都没找到，测试本身失效了"

        # 业务调用点必须同时拿角色（段位在里面）。唯一允许单值解包的地方是
        # `_取对局记账`：它只是把 `取对局` 的返回值原样转出去，并在外面记成败。
        单值 = [(行号, 个数) for 行号, 个数 in 找到 if 个数 != 2]
        assert len(单值) <= 1, f"这些调用点丢了角色（段位）：{单值}"
        if 单值:
            行号 = 单值[0][0]
            源码行 = 源码.splitlines()[行号 - 1]
            assert "结果 = await 取对局(" in 源码行, (
                f"main.py:{行号} 的 取对局 只解包了 1 个值，角色（含段位）被丢掉了"
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
