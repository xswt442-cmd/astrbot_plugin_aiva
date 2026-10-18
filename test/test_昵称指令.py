"""`/爱瓦 昵称` 指令。

**为什么要有独立的改名字指令**：QQ 官方机器人收不到 @别人（实测
`raw_message.mentions` 是空数组），所以 PK 只能按名字认人。名字默认取绑定时的
平台显示名，而**改个名字不该逼人重扫一次码**——重绑要走完整套扫码流程，比改
名字重得多。

爱完美（aiwm）有 `/爱完美 昵称`，爱瓦照抄这一条。
"""

from __future__ import annotations

import ast
from pathlib import Path

from aiva.storage import 净化昵称

仓库根 = Path(__file__).resolve().parent.parent
主源码 = (仓库根 / "main.py").read_text(encoding="utf-8")
树 = ast.parse(主源码)


def _函数体(名: str) -> str:
    节点 = None
    for n in ast.walk(树):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == 名:
            节点 = n
    assert 节点 is not None, f"main.py 里找不到 {名}"
    片段 = ast.get_source_segment(主源码, 节点) or ""
    文档 = ast.get_docstring(节点, clean=False)
    return 片段.replace(文档, "") if 文档 else 片段


class Test指令存在:
    def test_有昵称指令(self):
        for n in ast.walk(树):
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "昵称":
                return
        raise AssertionError("main.py 里没有 /爱瓦 昵称 指令")

    def test_子命令表里有昵称(self):
        """漏了这条，`/爱瓦 昵称` 会被当成落空消息，插件自己回提示。"""
        from aiva.兜底 import 子命令名

        assert "昵称" in 子命令名

    def test_用GreedyStr接参数(self):
        """名字里可能有空格，普通参数只吃第一个词。"""
        体 = _函数体("昵称")
        assert "GreedyStr" in 体


class Test改名字行为:
    def test_不带参数看当前值(self):
        体 = _函数体("昵称")
        assert "还没设" in 体, "没设过名字时不带参数也该有话说"

    def test_太长要拒绝(self):
        """名字要念进 PK 提示词，太长会把版面挤掉。"""
        体 = _函数体("昵称")
        assert "_昵称上限" in 体
        assert "太长了" in 体

    def test_改名字不要求重新绑定(self):
        体 = _函数体("昵称")
        assert "记昵称(" in 体, "直接改凭据里的昵称，不要走绑定流程"

    def test_撞名要提醒(self):
        """别人第一反应是「我是不是被占了」。顺手告诉他有几个人同名。"""
        体 = _函数体("昵称")
        assert "找同名的人(" in 体
        assert "个人叫这个" in 体

    def test_没绑定时挡住(self):
        体 = _函数体("昵称")
        assert "凭据是否齐全" in 体


class Test帮助文本提到:
    def test_列表里有昵称(self):
        assert "/爱瓦 昵称" in 主源码

    def test_绑定成功时告知可改(self):
        """不然用户根本不知道有这个指令。"""
        体 = _函数体("绑定")
        assert "昵称" in 体


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])


class Test净化昵称:
    """昵称会拼进 PK 提示词：换行能伪造指令，零宽字符能让两个名字看着一样。"""

    def test_换行与制表符被去掉(self):
        assert 净化昵称("老\n王\t") == "老王"

    def test_零宽字符被去掉(self):
        assert 净化昵称("老\u200b王") == "老王"

    def test_首尾空白被压掉(self):
        assert 净化昵称("  老王  ") == "老王"

    def test_空值不抛(self):
        assert 净化昵称("") == ""
        assert 净化昵称(None) == ""
