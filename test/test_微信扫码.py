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
    sign,
    检测图片类型,
    解析出码响应,
    解析扫码状态,
)


class Test签名:
    def test_签的是_sha1_不是_base64(self):
        串 = "appid=wx123&noncestr=1&sdk_ticket=t&timestamp=100"
        assert sign(串) == hashlib.sha1(串.encode()).hexdigest()
        assert len(sign(串)) == 40

    def test_票据在时间戳之前(self):
        串 = build_qr_sign_text("wx1", "nonce", "TICKET", "1700000000")

        assert 串.index("sdk_ticket=") < 串.index("timestamp=")
        assert "TICKET" in 串

    def test_出码参数带齐五个(self):
        参数 = build_qr_query("TICKET", 时间戳="1700000000")

        assert 参数["appid"] == WECHAT_APPID
        assert 参数["scope"] == "snsapi_userinfo"
        assert 参数["timestamp"] == "1700000000"
        assert 参数["signature"] == sign(
            build_qr_sign_text(WECHAT_APPID, 参数["noncestr"], "TICKET", "1700000000")
        )

    def test_每轮随机串不同(self):
        assert build_qr_query("T")["noncestr"] != build_qr_query("T")["noncestr"]

    def test_轮询参数(self):
        assert build_poll_query("u1") == {"f": "json", "uuid": "u1"}


class Test解析出码:
    def test_正常(self):
        结果 = 解析出码响应(
            {
                "errcode": 0,
                "uuid": "021abc",
                "appname": "掌上无畏契约",
                "qrcode": {"qrcodebase64": "iVBORw0KGgo="},
            }
        )

        assert 结果["uuid"] == "021abc"
        assert 结果["二维码"].startswith(b"\x89PNG")
        assert 结果["图片类型"] == "image/png"

    def test_错误码要抛(self):
        with pytest.raises(RuntimeError, match="签名|出码"):
            解析出码响应({"errcode": -11, "errmsg": "signature mismatch"})

    def test_缺二维码要抛(self):
        with pytest.raises(RuntimeError, match="没有二维码"):
            解析出码响应({"errcode": 0, "uuid": "x", "qrcode": {}})

    def test_缺uuid要抛(self):
        with pytest.raises(RuntimeError, match="没有二维码"):
            解析出码响应({"errcode": 0, "qrcode": {"qrcodebase64": "iVBORw=="}})


class Test解析扫码状态:
    @pytest.mark.parametrize(
        ("码", "状态", "终态"),
        [(408, "等待", False), (404, "已扫码", False), (402, "过期", True), (403, "已取消", True)],
    )
    def test_各种码(self, 码, 状态, 终态):
        结果 = 解析扫码状态({"wx_errcode": 码, "wx_code": ""})

        assert 结果["状态"] == 状态
        assert 结果["终态"] is 终态
        assert 结果["授权码"] is None

    def test_已确认要带授权码(self):
        结果 = 解析扫码状态({"wx_errcode": 405, "wx_code": "CODE123"})

        assert 结果["状态"] == "已确认"
        assert 结果["授权码"] == "CODE123"

    def test_已确认但没授权码按异常(self):
        """响应不完整时继续等，别把空码送去换票。"""
        结果 = 解析扫码状态({"wx_errcode": 405, "wx_code": ""})

        assert 结果["状态"] == "异常"
        assert 结果["授权码"] is None

    def test_脏数据不抛(self):
        assert 解析扫码状态({"wx_errcode": "abc"})["状态"] == "异常"
        assert 解析扫码状态({})["终态"] is False


class Test图片类型:
    def test_jpeg(self):
        assert 检测图片类型("/9j/4AAQ") == "image/jpeg"

    def test_未知前缀退回_jpeg(self):
        assert 检测图片类型("unknown") == "image/jpeg"


class Test换票请求体:
    """少传字段的代价：后端回 2025 + 内层 10002，看起来像「码无效」。

    2026-10-25 实测走了两轮弯路：只传 `login_info.code` 时，扫码成功后换票
    必然失败。补齐 `appid` / `check_third_type` / `wx_info_type` 与微信渠道的
    mcode 才对。
    """

    def test_四个字段都在(self):
        from aiva.transport import 微信换票体

        信息 = 微信换票体("CODE")["login_info"]
        assert 信息["code"] == "CODE"
        assert 信息["appid"] == "wxcbb49f1f39656c2a"
        assert 信息["check_third_type"] == 1
        assert 信息["wx_info_type"] == 1

    def test_用微信渠道的_mcode(self):
        from aiva.transport import 微信换票体

        体 = 微信换票体("CODE")
        assert 体["mcode"] == "69028af6dca2c107f4f58290100011b1a303"
        assert 体["mcode"] != "132f0a77d34402abc8463d60100011d19b0e", "那是 QQ 渠道的"

    def test_带渠道与区服(self):
        from aiva.transport import 微信换票体

        体 = 微信换票体("CODE")
        assert 体["source_game_zone"] == "agame"
        assert 体["game_zone"] == "agame"
        assert 体["mappid"] == 10200
