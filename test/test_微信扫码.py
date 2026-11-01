"""微信扫码的协议细节。

掌瓦的微信登录不是「拿二维码换 token」，而是分四步：掌瓦发票据 → 微信官方
接口用票据换二维码 → 轮询拿授权码 → 授权码交给掌瓦换登录态。这里钉住中间
两段不碰网络的逻辑：签名串、出码响应、轮询状态。

签名串里 `sdk_ticket` 必须在 `timestamp` 之前，顺序错了微信回
`signature mismatch`——实测过。
"""

from __future__ import annotations

import hashlib

import pytest

from aiva.auth.wechat import (
    WECHAT_APPID,
    build_poll_query,
    build_qr_query,
    build_qr_sign_text,
    image_mime,
    parse_poll_status,
    parse_qr_response,
    sign,
)


class TestSignature:
    def test_signs_sha1_not_base64(self):
        raw = "appid=wx123&noncestr=1&sdk_ticket=t&timestamp=100"
        assert sign(raw) == hashlib.sha1(raw.encode()).hexdigest()
        assert len(sign(raw)) == 40

    def test_ticket_before_timestamp(self):
        raw = build_qr_sign_text("wx1", "nonce", "TICKET", "1700000000")

        assert raw.index("sdk_ticket=") < raw.index("timestamp=")
        assert "TICKET" in raw

    def test_qr_query_has_five_params(self):
        params = build_qr_query("TICKET", timestamp="1700000000")

        assert params["appid"] == WECHAT_APPID
        assert params["scope"] == "snsapi_userinfo"
        assert params["timestamp"] == "1700000000"
        assert params["signature"] == sign(
            build_qr_sign_text(WECHAT_APPID, params["noncestr"], "TICKET", "1700000000")
        )

    def test_noncestr_changes_each_round(self):
        assert build_qr_query("T")["noncestr"] != build_qr_query("T")["noncestr"]

    def test_poll_query(self):
        assert build_poll_query("u1") == {"f": "json", "uuid": "u1"}


class TestParseQrResponse:
    def test_normal(self):
        parsed = parse_qr_response(
            {
                "errcode": 0,
                "uuid": "021abc",
                "appname": "掌上无畏契约",
                "qrcode": {"qrcodebase64": "iVBORw0KGgo="},
            }
        )

        assert parsed["uuid"] == "021abc"
        assert parsed["二维码"].startswith(b"\x89PNG")
        assert parsed["图片类型"] == "image/png"

    def test_error_code_raises(self):
        with pytest.raises(RuntimeError, match="签名|出码"):
            parse_qr_response({"errcode": -11, "errmsg": "signature mismatch"})

    def test_missing_qr_raises(self):
        with pytest.raises(RuntimeError, match="没有二维码"):
            parse_qr_response({"errcode": 0, "uuid": "x", "qrcode": {}})

    def test_missing_uuid_raises(self):
        with pytest.raises(RuntimeError, match="没有二维码"):
            parse_qr_response({"errcode": 0, "qrcode": {"qrcodebase64": "iVBORw=="}})


class TestParsePollStatus:
    @pytest.mark.parametrize(
        ("code", "status", "terminal"),
        [(408, "等待", False), (404, "已扫码", False), (402, "过期", True), (403, "已取消", True)],
    )
    def test_each_code(self, code, status, terminal):
        parsed = parse_poll_status({"wx_errcode": code, "wx_code": ""})

        assert parsed["状态"] == status
        assert parsed["终态"] is terminal
        assert parsed["授权码"] is None

    def test_confirmed_carries_code(self):
        parsed = parse_poll_status({"wx_errcode": 405, "wx_code": "CODE123"})

        assert parsed["状态"] == "已确认"
        assert parsed["授权码"] == "CODE123"

    def test_confirmed_without_code_is_error(self):
        """响应不完整时继续等，别把空码送去换票。"""
        parsed = parse_poll_status({"wx_errcode": 405, "wx_code": ""})

        assert parsed["状态"] == "异常"
        assert parsed["授权码"] is None

    def test_dirty_data_does_not_raise(self):
        assert parse_poll_status({"wx_errcode": "abc"})["状态"] == "异常"
        assert parse_poll_status({})["终态"] is False


class TestImageMime:
    def test_jpeg(self):
        assert image_mime("/9j/4AAQ") == "image/jpeg"

    def test_unknown_falls_back_to_jpeg(self):
        assert image_mime("unknown") == "image/jpeg"


class TestLoginBody:
    """少传字段的代价：后端回 2025 + 内层 10002，看起来像「码无效」。

    2026-10-25 实测走了两轮弯路：只传 `login_info.code` 时，扫码成功后换票
    必然失败。补齐 `appid` / `check_third_type` / `wx_info_type` 与微信渠道的
    mcode 才对。
    """

    def test_all_four_fields_present(self):
        from aiva.transport import wechat_login_body

        info = wechat_login_body("CODE")["login_info"]
        assert info["code"] == "CODE"
        assert info["appid"] == "wxcbb49f1f39656c2a"
        assert info["check_third_type"] == 1
        assert info["wx_info_type"] == 1

    def test_uses_wechat_mcode(self):
        from aiva.transport import wechat_login_body

        body = wechat_login_body("CODE")
        assert body["mcode"] == "69028af6dca2c107f4f58290100011b1a303"
        assert body["mcode"] != "132f0a77d34402abc8463d60100011d19b0e", "那是 QQ 渠道的"

    def test_carries_zone(self):
        from aiva.transport import wechat_login_body

        body = wechat_login_body("CODE")
        assert body["source_game_zone"] == "agame"
        assert body["game_zone"] == "agame"
        assert body["mappid"] == 10200
