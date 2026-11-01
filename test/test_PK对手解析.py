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

MAIN_FILE = Path(__file__).resolve().parent.parent / "main.py"


class FakeMention:
    def __init__(self, id, *, is_you=False, bot=False, username=""):
        self.id = id
        self.is_you = is_you
        self.bot = bot
        self.username = username


class FakeRawMessage:
    def __init__(self, mentions):
        self.mentions = mentions


class TestOpponentFromMentions:
    def test_finds_mentioned_user(self):
        raw = FakeRawMessage([FakeMention("YJJ_openid_123456")])
        assert opponent_from_mentions(raw) == "YJJ_openid_123456"

    def test_skips_at_bot(self):
        """群里 @机器人 也会进 mentions，不排掉就把机器人当对手了。"""
        raw = FakeRawMessage([FakeMention("机器人_id", is_you=True)])
        assert opponent_from_mentions(raw) is None

    def test_picks_opponent_over_bot(self):
        raw = FakeRawMessage(
            [FakeMention("机器人_id", is_you=True), FakeMention("YJJ_openid_123456")]
        )
        assert opponent_from_mentions(raw) == "YJJ_openid_123456"

    def test_skips_bot_account(self):
        raw = FakeRawMessage([FakeMention("bot_id_xxx", bot=True)])
        assert opponent_from_mentions(raw) is None

    def test_excludes_self(self):
        raw = FakeRawMessage([FakeMention("我自己")])
        assert opponent_from_mentions(raw, me="我自己") is None

    def test_excludes_bot_id(self):
        raw = FakeRawMessage([FakeMention("qq_official")])
        assert opponent_from_mentions(raw) is None

    def test_no_mentions_no_raise(self):
        assert opponent_from_mentions(None) is None
        assert opponent_from_mentions(FakeRawMessage([])) is None

    def test_missing_id_no_raise(self):
        class empty:
            is_you = False
            bot = False

        assert opponent_from_mentions(FakeRawMessage([empty()])) is None


class TestFindOpponentStillWorks:
    def test_component_chain(self):
        class At:
            qq = "C123456"

        assert find_opponent([At()], "") == "C123456"

    def test_mention_in_body(self):
        assert find_opponent([], "<@!C123456>") == "C123456"

    def test_drops_bot(self):
        class At:
            qq = "qq_official"

        assert find_opponent([At()], "") is None


class TestMainWired:
    def test_pk_checks_raw_message_first(self):
        source = MAIN_FILE.read_text(encoding="utf-8")
        assert "opponent_from_mentions(" in source, "PK 必须先读 raw_message.mentions"
        assert "raw_message" in source

    def test_keeps_legacy_path_fallback(self):
        """别的平台（OneBot）组件链里有 qq，砍掉会退化。"""
        source = MAIN_FILE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "pk_cmd":
                func = node
        assert func is not None
        segment = ast.get_source_segment(source, func) or ""
        assert "find_opponent(" in segment, "组件链/正文路径要留着做兜底"
        # 顺序不能反：raw_message 必须先试
        assert segment.index("opponent_from_mentions") < segment.index("find_opponent(")


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
