"""`main.py` 结构层的约束。

这些检查存在是因为**本地测试看不到它们**：`main.py` 顶层 import astrbot，
本地没有这个包，所以只能查源码。下面两条都是在容器里真加载时才炸出来的东西。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

MAIN_SOURCE = Path(__file__).resolve().parent.parent / "main.py"


def _tree() -> ast.Module:
    return ast.parse(MAIN_SOURCE.read_text(encoding="utf-8"))


def _main_class() -> ast.ClassDef:
    for node in _tree().body:
        if isinstance(node, ast.ClassDef) and node.name == "Main":
            return node
    raise AssertionError("main.py 里没有 Main 类")


def _command_methods() -> list[ast.AsyncFunctionDef | ast.FunctionDef]:
    return [
        node
        for node in _main_class().body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
    ]


def test_async_generator_must_not_return_value():
    """AstrBot 的指令是异步生成器（`yield event.plain_result(...)`）。

    在同一个函数里既要 `yield` 又要 `return <值>`，Python 直接
    `SyntaxError: 'return' with value in async generator`——**而且是在插件导入
    时才报**，本地 `pytest` 一行都跑不到，只能等装进容器看日志才发现。

    第一版就是这么挂的：`_recent_stats` 一边 `yield` 错误提示，一边
    `return (文本, 摘要)`。改成了抛 `UserFacingError`。别改回去。
    """
    for method in _command_methods():
        has_yield = any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(method))
        if not has_yield:
            continue
        for n in ast.walk(method):
            if not isinstance(n, ast.Return) or n.value is None:
                continue
            # `return` 与 `return None` 是合法的提前退出，只有带别的值不行。
            if isinstance(n.value, ast.Constant) and n.value.value is None:
                continue
            pytest.fail(
                f"{method.name} 第 {n.lineno} 行：异步生成器里不能 return 带值，"
                "插件导入时会 SyntaxError。要么抛异常，要么拆成两个函数。"
            )


def test_user_facing_error_exists():
    """这个异常是「不是故障、但要告诉用户」的统一出口，别绕开它自己 return。"""
    class_body = {n.name for n in _main_class().body if isinstance(n, ast.ClassDef)}
    module_body = {n.name for n in _tree().body if isinstance(n, ast.ClassDef)}
    assert "UserFacingError" in class_body or "UserFacingError" in module_body


@pytest.mark.parametrize(
    "name",
    ["matches_cmd", "review_cmd", "pk_cmd", "play_match", "bind", "unbind", "status_cmd"],
)
def test_commands_are_async_generators(name: str):
    """指令要把结果 `yield` 出去，写 `return` 的结果用户收不到。"""
    method = next((m for m in _command_methods() if m.name == name), None)
    assert method is not None, f"没有 {name} 指令"
    assert any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(method)), (
        f"{name} 里没有 yield，结果发不出去"
    )
