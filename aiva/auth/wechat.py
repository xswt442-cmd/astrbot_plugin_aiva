"""掌瓦微信扫码的协议细节：签名、出码、轮询状态。

与 QQ 扫码的区别：微信全程走 HTTP，不需要浏览器。掌瓦先发一张 SDK 票据，
微信官方接口拿它换二维码，之后轮询同一个 uuid 取授权码。

微信这套流程要求 `sdk_ticket` 参与 SHA-1 签名，签的串里字段顺序是协议规定
（票据在时间戳之前），不要按 query 参数顺序写。

这里只有纯函数，不碰网络，本地 pytest 直接跑。
"""

from __future__ import annotations

import base64
import hashlib
import random
import time
from typing import Any

__all__ = [
    "WECHAT_APPID",
    "WECHAT_MCODE",
    "WECHAT_POLL_URL",
    "WECHAT_QR_URL",
    "build_poll_query",
    "build_qr_query",
    "build_qr_sign_text",
    "decode_qr",
    "image_mime",
    "parse_poll_status",
    "parse_qr_response",
    "sign",
]

# 掌上无畏契约的微信 AppID。公开客户端里的值，不是 AppSecret。
WECHAT_APPID = "wxcbb49f1f39656c2a"

# 微信渠道的 mcode。和 QQ 扫码那条（`132f0a77…`）不是一个值：拿 QQ 的来用，
# 后端会走错分支，换票回 10202。
WECHAT_MCODE = "69028af6dca2c107f4f58290100011b1a303"

WECHAT_QR_URL = "https://open.weixin.qq.com/connect/sdk/qrconnect"
WECHAT_POLL_URL = "https://long.open.weixin.qq.com/connect/l/qrconnect"

_SCOPE = "snsapi_userinfo"
_NONCE_DIGITS = 8

_IMAGE_MAGIC = (
    ("/9j/", "image/jpeg"),
    ("iVBOR", "image/png"),
    ("R0lGOD", "image/gif"),
    ("UklGR", "image/webp"),
)
_DEFAULT_MIME = "image/jpeg"

# 微信扫码的终态与非终态。终态意味着继续等也不会有结果。
_STATUS_MAP = {
    405: "已确认",
    404: "已扫码",
    408: "等待",
    402: "过期",
    403: "已取消",
}
_TERMINAL = {"过期", "已取消"}


def _random_digits(digits: int = _NONCE_DIGITS) -> str:
    return "".join(str(random.randint(0, 9)) for _ in range(digits))


def build_qr_sign_text(appid: str, nonce: str, ticket: str, timestamp: str) -> str:
    """构造签名串。票据在时间戳之前，这是协议要求。"""
    return f"appid={appid}&noncestr={nonce}&sdk_ticket={ticket}&timestamp={timestamp}"


def sign(sign_text: str) -> str:
    """SHA-1，取小写十六进制。不是 base64。"""
    return hashlib.sha1(sign_text.encode("utf-8")).hexdigest()


def build_qr_query(ticket: str, timestamp: str | None = None) -> dict[str, str]:
    """构造出码请求的查询参数。"""
    nonce = _random_digits()
    timestamp = timestamp or str(int(time.time()))
    return {
        "appid": WECHAT_APPID,
        "noncestr": nonce,
        "timestamp": timestamp,
        "scope": _SCOPE,
        "signature": sign(build_qr_sign_text(WECHAT_APPID, nonce, ticket, timestamp)),
    }


def build_poll_query(uuid: str) -> dict[str, str]:
    """构造轮询请求的查询参数。"""
    return {"f": "json", "uuid": uuid}


def image_mime(image_base64: str) -> str:
    """按 base64 前缀的魔数判断图片类型。微信返回的通常是 JPEG。"""
    for magic, mime in _IMAGE_MAGIC:
        if image_base64.startswith(magic):
            return mime
    return _DEFAULT_MIME


def parse_qr_response(payload: dict[str, Any]) -> dict[str, Any]:
    """取出 uuid 与二维码字节。"""
    if payload.get("errcode") != 0:
        raise RuntimeError(f"微信出码失败：{payload.get('errmsg') or '未知原因'}")

    image = str((payload.get("qrcode") or {}).get("qrcodebase64") or "")
    uuid = str(payload.get("uuid") or "")
    if not image or not uuid:
        raise RuntimeError("微信出码响应里没有二维码")

    return {"uuid": uuid, "二维码": decode_qr(image), "图片类型": image_mime(image)}


def parse_poll_status(payload: dict[str, Any]) -> dict[str, Any]:
    """把轮询响应解析成状态。"""
    raw_code = payload.get("wx_errcode")
    try:
        code = int(raw_code)
    except (TypeError, ValueError):
        return {"状态": "异常", "终态": False, "授权码": None}

    status = _STATUS_MAP.get(code, "异常")
    result: dict[str, Any] = {"状态": status, "终态": status in _TERMINAL, "授权码": None}

    # 已确认必须带授权码，缺了说明响应不完整，按可重试处理。
    if status == "已确认":
        auth_code = str(payload.get("wx_code") or "")
        if not auth_code:
            result["状态"] = "异常"
        else:
            result["授权码"] = auth_code

    return result


def decode_qr(image_base64: str) -> bytes:
    return base64.b64decode(image_base64)
