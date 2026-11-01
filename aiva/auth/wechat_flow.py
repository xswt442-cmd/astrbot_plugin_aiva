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
    parse_poll_status,
    parse_qr_response,
)

__all__ = ["WechatQrSession"]

SDK_TICKET_URL = "https://app.mval.qq.com/go/auth/get_sdk_ticket"

_HEADERS = {"Content-Type": "application/json", "User-Agent": "okhttp/4.9.3"}
_DEVICE_NAME = "22041216C"
_POLL_INTERVAL_SECONDS = 2
_REQUEST_TIMEOUT_SECONDS = 35
# 轮询是长连接，服务端会挂住一段时间再回；给它单独的短超时，别把整轮预算吃掉。
_POLL_TIMEOUT_SECONDS = 35


class WechatQrSession:
    """一次微信扫码会话。出码后可以等用户扫码。"""

    def __init__(self) -> None:
        self._http: Any = None
        self._uuid = ""

    async def create_qr(self) -> bytes:
        """出二维码，返回图片字节。"""
        self._http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS)
        )
        ticket = await self._fetch_ticket()
        async with self._http.get(WECHAT_QR_URL, params=build_qr_query(ticket)) as response:
            parsed = parse_qr_response(json.loads(await response.text()))

        self._uuid = parsed["uuid"]
        return parsed["二维码"]

    async def _fetch_ticket(self) -> str:
        """向掌瓦要一张微信 SDK 票据。不需要登录态。

        请求体里的 `sdk_appid` 与 `mcode` 都是微信渠道那套：mcode 换成 QQ 的
        会走到另一条分支（换票回 10202）。
        """
        assert self._http is not None
        body = {
            "clienttype": 9,
            "config_params": {"client_dev_name": _DEVICE_NAME, "lang_type": 0},
            "mappid": 10200,
            "mcode": WECHAT_MCODE,
            "sdk_appid": WECHAT_APPID,
            "source_game_zone": "agame",
            "game_zone": "agame",
        }
        async with self._http.post(SDK_TICKET_URL, headers=_HEADERS, json=body) as response:
            payload = json.loads(await response.text())

        if payload.get("result") != 0:
            raise RuntimeError(
                f"掌瓦出票失败：{payload.get('err_msg') or payload.get('msg') or '未知原因'}"
            )

        ticket = str((payload.get("data") or {}).get("ticket") or "")
        if not ticket:
            raise RuntimeError("掌瓦返回的票据为空")
        return ticket

    async def wait_for_code(self, timeout_seconds: float) -> str:
        """轮询等用户扫码，返回微信授权码。"""
        if self._http is None or not self._uuid:
            raise RuntimeError("还没出码就等授权")

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            async with self._http.get(
                WECHAT_POLL_URL,
                params=build_poll_query(self._uuid),
                timeout=aiohttp.ClientTimeout(total=_POLL_TIMEOUT_SECONDS),
            ) as response:
                status = parse_poll_status(json.loads(await response.text()))

            if status["授权码"]:
                return str(status["授权码"])
            if status["终态"]:
                raise RuntimeError("二维码已过期" if status["状态"] == "过期" else "扫码被取消")
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)

        raise TimeoutError("扫码超时")

    async def close(self) -> None:
        if self._http is not None and not self._http.closed:
            await self._http.close()
