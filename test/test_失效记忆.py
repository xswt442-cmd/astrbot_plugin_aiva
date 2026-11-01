"""失效记忆：一次失败不能被下一次「恰好成功」盖过去。

实测（2026-10-18 00:39~00:40）：重绑后的头几分钟，新旧会话在服务端交替生效，
同一个号会几秒内时好时坏——00:39:35 失效、00:40:44 成功、00:40:51 又失效。
用户在这个窗口里发 `/爱瓦 状态`，只要撞上「好」的那一次就会被告知「绑定正常」，
转头再查又失效。所以记下最近一次失效时间，半小时内按「还没好」处理。
"""

from __future__ import annotations

from aiva.storage import clear_expired, mark_expired, recently_expired

现在 = 1_792_256_000


class Test记失效:
    def test_记下时间里立刻算失效(self):
        凭据 = mark_expired({}, 现在)

        assert 凭据["最近失效"] == 现在
        assert recently_expired(凭据, 现在)

    def test_半小时内一直算失效(self):
        凭据 = mark_expired({}, 现在)

        assert recently_expired(凭据, 现在 + 29 * 60)
        assert not recently_expired(凭据, 现在 + 31 * 60)

    def test_清掉之后算好(self):
        凭据 = clear_expired(mark_expired({}, 现在))

        assert "最近失效" not in 凭据
        assert not recently_expired(凭据, 现在)

    def test_没记录算好(self):
        assert not recently_expired({}, 现在)
        assert not recently_expired(None, 现在)

    def test_脏数据不抛(self):
        assert not recently_expired({"最近失效": "昨天"}, 现在)
        assert not recently_expired({"最近失效": None}, 现在)
        assert not recently_expired({"最近失效": -5}, 现在)

    def test_时间倒退不算失效(self):
        """系统时钟回拨时会算出负数，别把它当成刚失效。"""
        凭据 = mark_expired({}, 现在)

        assert not recently_expired(凭据, 现在 - 3600)
