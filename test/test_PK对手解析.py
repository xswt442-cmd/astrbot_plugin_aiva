"""QQ 官方平台上必须能从 `raw_message.mentions` 认出 @ 的对手。

线上真事：用户 `@老狗 /爱瓦 PK @YJJ`，插件回「没认出要和谁比」。
日志里 `message_str` 只剩 `/爱瓦 PK` —— @YJJ 被适配器吃掉了。

适配器源码（`qqofficial_platform_adapter.py`）：

```python
bot_mentions = [m for m in (message.mentions or []) if m.is_you is True]
for mention_id in bot_mention_ids:      # 只删 @机器人 的
    plain_content_raw = plain_content_raw.replace(f"<@{mention_id}>", "")
```

平台把全部 @ 放在 `message.mentions`，适配器只把 `is_you=True` 的转成
`At` 组件。所以组件链和正文正则在这条路径上永远拿不到别人。
出路是适配器第 769 行的 `abm.raw_message = message`。
"""

from __future__ import annotations

import ast
from pathlib import Path

from aiva.pk import find_opponent, opponent_from_mentions

主文件 = Path(__file__).resolve().parent.parent / "main.py"


class 假提及:
    def __init__(self, id, *, is_you=False, bot=False, username=""):
        self.id = id
        self.is_you = is_you
        self.bot = bot
        self.username = username


class 假原始消息:
    def __init__(self, mentions):
        self.mentions = mentions


class TestOpponentFromMentions:
    def test_认出被at的人(self):
        原始 = 假原始消息([假提及("YJJ_openid_123456")])
        assert opponent_from_mentions(原始) == "YJJ_openid_123456"

    def test_跳过at机器人(self):
        """群里 @机器人 也会进 mentions，不排掉就把机器人当对手了。"""
        原始 = 假原始消息([假提及("机器人_id", is_you=True)])
        assert opponent_from_mentions(原始) is None

    def test_机器人和对手都在时取对手(self):
        原始 = 假原始消息([假提及("机器人_id", is_you=True), 假提及("YJJ_openid_123456")])
        assert opponent_from_mentions(原始) == "YJJ_openid_123456"

    def test_跳过机器人账号(self):
        原始 = 假原始消息([假提及("bot_id_xxx", bot=True)])
        assert opponent_from_mentions(原始) is None

    def test_排除自己(self):
        原始 = 假原始消息([假提及("我自己")])
        assert opponent_from_mentions(原始, me="我自己") is None

    def test_排除机器人标识(self):
        原始 = 假原始消息([假提及("qq_official")])
        assert opponent_from_mentions(原始) is None

    def test_没有mentions不炸(self):
        assert opponent_from_mentions(None) is None
        assert opponent_from_mentions(假原始消息([])) is None

    def test_没有id字段不炸(self):
        class 空:
            is_you = False
            bot = False

        assert opponent_from_mentions(假原始消息([空()])) is None


class Test旧的find_opponent没坏:
    def test_组件链(self):
        class At:
            qq = "C123456"

        assert find_opponent([At()], "") == "C123456"

    def test_正文提及(self):
        assert find_opponent([], "<@!C123456>") == "C123456"

    def test_排掉机器人(self):
        class At:
            qq = "qq_official"

        assert find_opponent([At()], "") is None


class TestMain已接上:
    def test_PK必须先查raw_message(self):
        源码 = 主文件.read_text(encoding="utf-8")
        assert "opponent_from_mentions(" in 源码, "PK 必须先读 raw_message.mentions"
        assert "raw_message" in 源码

    def test_仍保留旧路径兜底(self):
        """别的平台（OneBot）组件链里有 qq，砍掉会退化。"""
        源码 = 主文件.read_text(encoding="utf-8")
        树 = ast.parse(源码)
        函数 = None
        for 节点 in ast.walk(树):
            if isinstance(节点, ast.AsyncFunctionDef) and 节点.name == "pk_cmd":
                函数 = 节点
        assert 函数 is not None
        片段 = ast.get_source_segment(源码, 函数) or ""
        assert "find_opponent(" in 片段, "组件链/正文路径要留着做兜底"
        # 顺序不能反：raw_message 必须先试
        assert 片段.index("opponent_from_mentions") < 片段.index("find_opponent(")


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
