"""失效记忆：一次失败不能被下一次「恰好成功」盖过去。

实测（2026-10-18 00:39~00:40）：重绑后的头几分钟，新旧会话在服务端交替生效，
同一个号会几秒内时好时坏——00:39:35 失效、00:40:44 成功、00:40:51 又失效。
用户在这个窗口里发 `/爱瓦 状态`，只要撞上「好」的那一次就会被告知「绑定正常」，
转头再查又失效。所以记下最近一次失效时间，半小时内按「还没好」处理。
"""

from __future__ import annotations

from aiva.storage import clear_expired, mark_expired, recently_expired

NOW = 1_792_256_000


class TestMarkExpired:
    def test_marked_time_counts_as_expired(self):
        credential = mark_expired({}, NOW)

        assert credential["最近失效"] == NOW
        assert recently_expired(credential, NOW)

    def test_expired_within_half_hour(self):
        credential = mark_expired({}, NOW)

        assert recently_expired(credential, NOW + 29 * 60)
        assert not recently_expired(credential, NOW + 31 * 60)

    def test_cleared_counts_as_good(self):
        credential = clear_expired(mark_expired({}, NOW))

        assert "最近失效" not in credential
        assert not recently_expired(credential, NOW)

    def test_no_record_counts_as_good(self):
        assert not recently_expired({}, NOW)
        assert not recently_expired(None, NOW)

    def test_dirty_data_does_not_raise(self):
        assert not recently_expired({"最近失效": "昨天"}, NOW)
        assert not recently_expired({"最近失效": None}, NOW)
        assert not recently_expired({"最近失效": -5}, NOW)

    def test_clock_rollback_is_not_expiry(self):
        """系统时钟回拨时会算出负数，别把它当成刚失效。"""
        credential = mark_expired({}, NOW)

        assert not recently_expired(credential, NOW - 3600)
