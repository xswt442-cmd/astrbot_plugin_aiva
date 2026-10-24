"""微信扫码的完整流程：掌瓦发票据 → 微信换二维码 → 轮询拿授权码。

全程 HTTP，不需要浏览器侧车。出码与等待分成两步：二维码要立刻发给用户，
扫码在后台等。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import aiohttp

from .wechat import (
    WECHAT_APPID,
    WECHAT_MCODE,
    WECHAT_POLL_URL,
    WECHAT_QR_URL,
    build_poll_query,
    build_qr_query,
    解析出码响应,
    解析扫码状态,
)

__all__ = ["微信出码会话"]

SDK票据接口 = "https://app.mval.qq.com/go/auth/get_sdk_ticket"

_请求头 = {"Content-Type": "application/json", "User-Agent": "okhttp/4.9.3"}
_设备名 = "22041216C"
_轮询间隔秒 = 2
_单次请求秒 = 35
# 轮询是长连接，服务端会挂住一段时间再回；给它单独的短超时，别把整轮预算吃掉。
_轮询超时秒 = 35


class 微信出码会话:
    """一次微信扫码会话。出码后可以等用户扫码。"""

    def __init__(self) -> None:
        self._http: Any = None
        self._uuid = ""

    async def 出码(self) -> bytes:
        """出二维码，返回图片字节。"""
        self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=_单次请求秒))
        票据 = await self._取票据()
        async with self._http.get(WECHAT_QR_URL, params=build_qr_query(票据)) as 响应:
            结果 = 解析出码响应(json.loads(await 响应.text()))

        self._uuid = 结果["uuid"]
        return 结果["二维码"]

    async def _取票据(self) -> str:
        """向掌瓦要一张微信 SDK 票据。不需要登录态。

        请求体里的 `sdk_appid` 与 `mcode` 都是微信渠道那套：mcode 换成 QQ 的
        会走到另一条分支（换票回 10202）。
        """
        assert self._http is not None
        体 = {
            "clienttype": 9,
            "config_params": {"client_dev_name": _设备名, "lang_type": 0},
            "mappid": 10200,
            "mcode": WECHAT_MCODE,
            "sdk_appid": WECHAT_APPID,
            "source_game_zone": "agame",
            "game_zone": "agame",
        }
        async with self._http.post(SDK票据接口, headers=_请求头, json=体) as 响应:
            载荷 = json.loads(await 响应.text())

        if 载荷.get("result") != 0:
            raise RuntimeError(
                f"掌瓦出票失败：{载荷.get('err_msg') or 载荷.get('msg') or '未知原因'}"
            )

        票据 = str((载荷.get("data") or {}).get("ticket") or "")
        if not 票据:
            raise RuntimeError("掌瓦返回的票据为空")
        return 票据

    async def 等授权码(self, 超时秒: float) -> str:
        """轮询等用户扫码，返回微信授权码。"""
        if self._http is None or not self._uuid:
            raise RuntimeError("还没出码就等授权")

        截止 = time.monotonic() + 超时秒
        while time.monotonic() < 截止:
            async with self._http.get(
                WECHAT_POLL_URL,
                params=build_poll_query(self._uuid),
                timeout=aiohttp.ClientTimeout(total=_轮询超时秒),
            ) as 响应:
                状态 = 解析扫码状态(json.loads(await 响应.text()))

            if 状态["授权码"]:
                return str(状态["授权码"])
            if 状态["终态"]:
                raise RuntimeError("二维码已过期" if 状态["状态"] == "过期" else "扫码被取消")
            await asyncio.sleep(_轮询间隔秒)

        raise TimeoutError("扫码超时")

    async def 关闭(self) -> None:
        if self._http is not None and not self._http.closed:
            await self._http.close()
