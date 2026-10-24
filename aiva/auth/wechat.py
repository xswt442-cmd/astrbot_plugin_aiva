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
    "sign",
    "解码二维码",
    "解析出码响应",
    "解析扫码状态",
    "检测图片类型",
]

# 掌上无畏契约的微信 AppID。公开客户端里的值，不是 AppSecret。
WECHAT_APPID = "wxcbb49f1f39656c2a"

# 微信渠道的 mcode。和 QQ 扫码那条（`132f0a77…`）不是一个值：拿 QQ 的来用，
# 后端会走错分支，换票回 10202。
WECHAT_MCODE = "69028af6dca2c107f4f58290100011b1a303"

WECHAT_QR_URL = "https://open.weixin.qq.com/connect/sdk/qrconnect"
WECHAT_POLL_URL = "https://long.open.weixin.qq.com/connect/l/qrconnect"

_范围 = "snsapi_userinfo"
_随机串位数 = 8

_图片魔数 = (
    ("/9j/", "image/jpeg"),
    ("iVBOR", "image/png"),
    ("R0lGOD", "image/gif"),
    ("UklGR", "image/webp"),
)
_默认图片类型 = "image/jpeg"

# 微信扫码的终态与非终态。终态意味着继续等也不会有结果。
_状态映射 = {
    405: "已确认",
    404: "已扫码",
    408: "等待",
    402: "过期",
    403: "已取消",
}
_终态 = {"过期", "已取消"}


def _随机数字串(位数: int = _随机串位数) -> str:
    return "".join(str(random.randint(0, 9)) for _ in range(位数))


def build_qr_sign_text(appid: str, 随机串: str, 票据: str, 时间戳: str) -> str:
    """构造签名串。票据在时间戳之前，这是协议要求。"""
    return f"appid={appid}&noncestr={随机串}&sdk_ticket={票据}&timestamp={时间戳}"


def sign(签名串: str) -> str:
    """SHA-1，取小写十六进制。不是 base64。"""
    return hashlib.sha1(签名串.encode("utf-8")).hexdigest()


def build_qr_query(票据: str, 时间戳: str | None = None) -> dict[str, str]:
    """构造出码请求的查询参数。"""
    随机串 = _随机数字串()
    时间戳 = 时间戳 or str(int(time.time()))
    return {
        "appid": WECHAT_APPID,
        "noncestr": 随机串,
        "timestamp": 时间戳,
        "scope": _范围,
        "signature": sign(build_qr_sign_text(WECHAT_APPID, 随机串, 票据, 时间戳)),
    }


def build_poll_query(uuid: str) -> dict[str, str]:
    """构造轮询请求的查询参数。"""
    return {"f": "json", "uuid": uuid}


def 检测图片类型(图片base64: str) -> str:
    """按 base64 前缀的魔数判断图片类型。微信返回的通常是 JPEG。"""
    for 魔数, 类型 in _图片魔数:
        if 图片base64.startswith(魔数):
            return 类型
    return _默认图片类型


def 解析出码响应(载荷: dict[str, Any]) -> dict[str, Any]:
    """取出 uuid 与二维码字节。"""
    if 载荷.get("errcode") != 0:
        raise RuntimeError(f"微信出码失败：{载荷.get('errmsg') or '未知原因'}")

    图片 = str((载荷.get("qrcode") or {}).get("qrcodebase64") or "")
    uuid = str(载荷.get("uuid") or "")
    if not 图片 or not uuid:
        raise RuntimeError("微信出码响应里没有二维码")

    return {"uuid": uuid, "二维码": 解码二维码(图片), "图片类型": 检测图片类型(图片)}


def 解析扫码状态(载荷: dict[str, Any]) -> dict[str, Any]:
    """把轮询响应解析成状态。"""
    原始码 = 载荷.get("wx_errcode")
    try:
        码 = int(原始码)
    except (TypeError, ValueError):
        return {"状态": "异常", "终态": False, "授权码": None}

    状态 = _状态映射.get(码, "异常")
    结果: dict[str, Any] = {"状态": 状态, "终态": 状态 in _终态, "授权码": None}

    # 已确认必须带授权码，缺了说明响应不完整，按可重试处理。
    if 状态 == "已确认":
        授权码 = str(载荷.get("wx_code") or "")
        if not 授权码:
            结果["状态"] = "异常"
        else:
            结果["授权码"] = 授权码

    return 结果


def 解码二维码(图片base64: str) -> bytes:
    return base64.b64decode(图片base64)
