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

from aiva.storage import clean_nickname

REPO_ROOT = Path(__file__).resolve().parent.parent
MAIN_SOURCE = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
TREE = ast.parse(MAIN_SOURCE)


def _body(name: str) -> str:
    node = None
    for n in ast.walk(TREE):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            node = n
    assert node is not None, f"main.py 里找不到 {name}"
    segment = ast.get_source_segment(MAIN_SOURCE, node) or ""
    doc = ast.get_docstring(node, clean=False)
    return segment.replace(doc, "") if doc else segment


class TestCommandExists:
    def test_has_nickname_command(self):
        for n in ast.walk(TREE):
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "set_nick":
                return
        raise AssertionError("main.py 里没有 /爱瓦 昵称 指令")

    def test_subcommand_table_has_nickname(self):
        """漏了这条，`/爱瓦 昵称` 会被当成落空消息，插件自己回提示。"""
        from aiva.fallback import SUBCOMMANDS

        assert "昵称" in SUBCOMMANDS

    def test_uses_greedy_str(self):
        """名字里可能有空格，普通参数只吃第一个词。"""
        body = _body("set_nick")
        assert "GreedyStr" in body


class TestRenameBehavior:
    def test_shows_current_without_arg(self):
        body = _body("set_nick")
        assert "还没设" in body, "没设过名字时不带参数也该有话说"

    def test_rejects_too_long(self):
        """名字要念进 PK 提示词，太长会把版面挤掉。"""
        body = _body("set_nick")
        assert "_NICKNAME_LIMIT" in body
        assert "太长了" in body

    def test_no_rebind_required(self):
        body = _body("set_nick")
        assert "set_nickname(" in body, "直接改凭据里的昵称，不要走绑定流程"

    def test_warns_on_nickname_clash(self):
        """别人第一反应是「我是不是被占了」。顺手告诉他有几个人同名。"""
        body = _body("set_nick")
        assert "find_by_nickname(" in body
        assert "个人叫这个" in body

    def test_blocks_when_unbound(self):
        body = _body("set_nick")
        assert "is_complete(" in body


class TestHelpMentions:
    def test_help_lists_nickname(self):
        assert "/爱瓦 昵称" in MAIN_SOURCE

    def test_bind_mentions_rename(self):
        """不然用户根本不知道有这个指令。"""
        body = _body("bind")
        assert "昵称" in body


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])


class TestCleanNickname:
    """昵称会拼进 PK 提示词：换行能伪造指令，零宽字符能让两个名字看着一样。"""

    def test_strips_newline_and_tab(self):
        assert clean_nickname("老\n王\t") == "老王"

    def test_strips_zero_width(self):
        assert clean_nickname("老\u200b王") == "老王"

    def test_trims_whitespace(self):
        assert clean_nickname("  老王  ") == "老王"

    def test_empty_value_no_raise(self):
        assert clean_nickname("") == ""
        assert clean_nickname(None) == ""
