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

REPO_ROOT = Path(__file__).resolve().parent.parent


def _read(name: str) -> str:
    return (REPO_ROOT / "aiva" / name).read_text(encoding="utf-8")


class TestTierSource:
    def test_tier_only_from_tier_text(self):
        from aiva.transport import tier_of

        assert tier_of({"tier_text": "钻石II"}) == "钻石II"
        assert tier_of({"tier_text": "  铂金I "}) == "铂金I"
        # tier 是数字档位，没有映射表就不能用——猜错就是又一句编的段位
        assert tier_of({"tier": 19}) == ""
        assert tier_of({}) == ""

    def test_fetch_matches_returns_role_too(self):
        source = _read("transport.py")
        tree = ast.parse(source)
        func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "fetch_matches":
                func = node
        assert func is not None
        segment = ast.get_source_segment(source, func) or ""
        assert "role" in segment, "fetch_matches 必须把主角色信息带出来，段位在里面"
        assert "return (" in segment, "fetch_matches 应返回 (对局列表, 角色) 二元组"


class TestTierInText:
    def _summary(self) -> dict:
        # 字段名要对齐 `aiva/stats.py` 真实读的：胜负看 `result_title`
        # （不是 `result`），模式过滤看 `game_enum`。写错字段名的话
        # `聚合` 会把所有场次判成「无场次」，测试就会假失败。
        matches = [
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

        summary = aggregate(matches)
        assert summary.get("场数") == 4, f"测试数据不对，聚合出 {summary.get('场数')} 场"
        return summary

    def test_tier_written_when_present(self):
        from aiva.stats import recent_text

        text = recent_text(self._summary(), "钻石II")
        assert "钻石II" in text
        assert "当前段位" in text

    def test_tier_goes_first(self):
        """模型先看到段位才会照抄。放末尾容易被长数据挤掉注意力。"""
        from aiva.stats import recent_text

        lines = recent_text(self._summary(), "钻石II").splitlines()
        assert lines[0].startswith("当前段位"), f"段位应在首行，实际是：{lines[0]}"

    def test_missing_tier_says_so(self):
        """留空 = 模型自己编。必须明说「接口没给」。"""
        from aiva.stats import recent_text

        text = recent_text(self._summary(), "")
        assert "接口没给" in text
        assert "不许提段位" in text

    def test_no_tier_by_default(self):
        from aiva.stats import recent_text

        assert "接口没给" in recent_text(self._summary())


class TestPromptConstraint:
    def test_must_say_copy_only(self):
        source = _read("prompts.py")
        assert "只能照抄" in source, "提示词必须写死「段位只准照抄数据里的」"
        assert "一个字都不许改" in source

    def test_must_forbid_inference(self):
        source = _read("prompts.py")
        assert "不许推断" in source


class TestCallSitesUpdated:
    def test_every_call_unpacks_pair(self):
        """`fetch_matches` 现在返回 `(列表, 角色)`。

        只改成「拿列表」会让角色悄悄丢掉——段位又变回没数据，模型接着编。
        用 AST 查解包目标个数，比字符串匹配可靠。
        """
        source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if not isinstance(value, ast.Await):
                continue
            call = value.value
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "fetch_matches"
            ):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            pair = targets[0] if len(targets) == 1 else None
            count = len(pair.elts) if isinstance(pair, ast.Tuple) else 1
            found.append((node.lineno, count))

        assert found, "main.py 里一个 fetch_matches 调用都没找到，测试本身失效了"

        # 业务调用点必须同时拿角色（段位在里面）。唯一允许单值解包的地方是
        # `_fetch_tracked`：它只是把 `fetch_matches` 的返回值原样转出去，并在外面记成败。
        singles = [(lineno, count) for lineno, count in found if count != 2]
        assert len(singles) <= 1, f"这些调用点丢了角色（段位）：{singles}"
        if singles:
            lineno = singles[0][0]
            line = source.splitlines()[lineno - 1]
            assert "result = await fetch_matches(" in line, (
                f"main.py:{lineno} 的 fetch_matches 只解包了 1 个值，角色（含段位）被丢掉了"
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
