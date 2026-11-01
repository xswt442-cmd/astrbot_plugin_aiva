"""`_display_name` 必须是同步函数——它曾经是 async，调用点漏 await 后：

1. 拿到的是协程对象，**不是错误**；
2. 协程对象是真值，`or "你"` 兜底不触发；
3. f-string 把它渲染成 `<coroutine object Main.名字 at 0x7fa...>`；
4. 这串东西被塞进 PK 提示词，模型把它当成玩家名字，一本正经地
   点评「`<coroutine object>`的9.4场均死亡就是铁证」。

线上实拍，2026-10-14 00:40。模型不会对荒谬输入说不——段位缺了它编段位，
名字是协程对象它就把协程对象当名字。堵法不是求模型机灵，是让这类值
根本产生不出来：函数没有 await 就不该是 async。
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MAIN_SOURCE = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
TREE = ast.parse(MAIN_SOURCE)


def _func(name: str):
    for n in ast.walk(TREE):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    raise AssertionError(f"main.py 里找不到 {name}")


class TestDisplayNameIsSync:
    def test_not_a_coroutine_function(self):
        node = _func("_display_name")
        assert isinstance(node, ast.FunctionDef), (
            "_display_name 必须是普通同步函数。它体内没有任何 await，做成协程只会"
            "引来「调用点漏 await → 拿到协程对象 → 真值 → 塞进提示词」"
            "这条静默故障链。"
        )

    def test_no_await_inside(self):
        """如果未来真加了 await（要 async 化），上面那条测试的注释就得改，
        调用点也全部要 await——这两件事必须一起做。"""
        node = _func("_display_name")
        assert not any(isinstance(n, ast.Await) for n in ast.walk(node))

    def test_call_sites_do_not_await(self):
        """`self._display_name(event)` 不许被 await。

        同步函数被 await 是运行时 TypeError；这里按 AST 找所有调用点，
        看有没有哪一处把它当协程用。
        """
        for n in ast.walk(TREE):
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "_display_name"
            ):
                parent = None
                for outer in ast.walk(TREE):
                    if isinstance(outer, ast.Await) and outer.value is n:
                        parent = outer
                assert parent is None, (
                    f"main.py 第 {n.lineno} 行 await 了同步函数 _display_name，会 TypeError"
                )


class TestPromptGuard:
    def test_name_fallback_still_in_pk(self):
        """`or "你"` 这个兜底要留着，但它是防「平台没给名字」，
        不是防协程对象——协程对象的防线在上面那条测试里。"""
        assert 'or "你"' in MAIN_SOURCE


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
