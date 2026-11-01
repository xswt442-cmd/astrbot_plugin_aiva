"""扫码链路的超时与网络异常不许穿透到 handler。

线上事故：用户在扫码等待期间发了句别的消息，`aiohttp` 的 `sock_connect`
被打断抛 `asyncio.TimeoutError`，它穿过 `_follow_redirects` → `poll_once` → `绑定`，
最后框架在群里弹出「在调用插件 astrbot_plugin_aiva 的处理函数 绑定 时出现
异常」加一大段 traceback。用户只看到一句「在吗」把绑定搞挂了。

这些测试不import `main.py`（顶层 import astrbot，本地跑不了），
只测 `aiva.auth.ptlogin` 里的纯逻辑与异常边界。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import aiohttp
import pytest

from aiva.auth.ptlogin import QrSession

SOURCE = (Path(__file__).resolve().parent.parent / "aiva" / "auth" / "ptlogin.py").read_text(
    encoding="utf-8"
)


class FakeResponse:
    """够用的最小 aiohttp 响应替身。"""

    def __init__(self, body: str = "", headers: dict[str, str] | None = None):
        self._body = body
        self.headers = headers or {}
        self.status = 200

    async def text(self, errors: str = "strict") -> str:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class FakeSession:
    """按 URL 决定抛什么或返回什么。"""

    def __init__(self, behavior: dict[str, object]):
        self._behavior = behavior
        self.requested: list[str] = []

    def get(self, url: str, **kwargs):
        self.requested.append(url)
        result = self._behavior.get(url, FakeResponse(""))
        if isinstance(result, BaseException):
            raise result
        if callable(result):
            return result(url, **kwargs)
        return result

    async def close(self) -> None:
        return None


class TestFollowRedirectsSwallowsErrors:
    @pytest.mark.parametrize(
        "error",
        [
            TimeoutError(),
            asyncio.TimeoutError(),
            aiohttp.ClientError("boom"),
            ConnectionResetError("reset"),
            OSError("network unreachable"),
        ],
    )
    async def test_single_hop_error_returns_none(self, error):
        """任何网络异常都按「这一跳没拿到」处理，返回 None 等下一轮。"""
        session = QrSession()
        session._session = FakeSession({"https://x.example/1": error})

        result = await session._follow_redirects("https://x.example/1")

        assert result is None, f"{type(error).__name__} 穿透了 _follow_redirects"

    async def test_stops_after_first_failure(self):
        """失败就停，别拿同一个坏连接连打 5 次。"""
        session = QrSession()
        fake = FakeSession({"https://a": TimeoutError(), "https://b": FakeResponse("")})
        session._session = fake

        assert await session._follow_redirects("https://a") is None
        assert fake.requested == ["https://a"], f"不该继续请求：{fake.requested}"

    async def test_error_while_reading_body_swallowed(self):
        """异常发生在 `async with` 内部（读正文时）同样不能穿透。"""

        class ExplodingResponse(FakeResponse):
            async def __aenter__(self):
                return self

            async def text(self, errors: str = "strict") -> str:
                raise asyncio.TimeoutError()

        session = QrSession()
        session._session = FakeSession({"https://x.example/1": ExplodingResponse()})

        assert await session._follow_redirects("https://x.example/1") is None

    async def test_none_session_returns_directly(self):
        session = QrSession()
        assert await session._follow_redirects("https://x.example/1") is None


class TestRedirectTimeoutBudget:
    def test_each_hop_has_own_timeout(self):
        """会话的 total=20 是单条请求预算，跳转链最多 5 跳，不单独设就是 100 秒。

        实测正是这里出的事：等100 秒期间用户发消息打断连接。
        """
        assert "REDIRECT_TIMEOUT" in SOURCE
        assert "timeout=aiohttp.ClientTimeout(total=REDIRECT_TIMEOUT)" in SOURCE

    def test_hop_timeout_is_enough(self):
        """腾讯登录单跳实测约 3 秒，`m_get_redirect_url` 也在 12 秒内，
        5 秒太紧会把本来能成的跳转掐死。"""
        from aiva.auth.ptlogin import REDIRECT_TIMEOUT

        assert REDIRECT_TIMEOUT >= 10, f"单跳 {REDIRECT_TIMEOUT} 秒不够腾讯登录接口用"

    def test_catch_list_includes_timeout(self):
        assert "except (TimeoutError, aiohttp.ClientError, OSError)" in SOURCE, (
            "网络异常必须被吃掉，不许穿透到 handler"
        )


class TestPollErrorClassification:
    def test_bind_handles_timeout(self):
        """handler 里必须有 `except asyncio.TimeoutError`。"""
        main = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
        assert "except asyncio.TimeoutError:" in main, "绑定没接住超时，会炸成插件异常"

    def test_qr_and_ticket_have_fallbacks(self):
        """任何异常都要翻译成人话，不能冒到框架手上弹 traceback。"""
        main = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
        bind_block = main.split("async def bind")[1].split("@aiva_group.command")[0]
        # 出码、换票各一个宽泛兜底；轮询处是专门的 asyncio.TimeoutError
        assert bind_block.count("except Exception") >= 2, (
            f"出码 / 换票 两处都该有兜底，实际 {bind_block.count('except Exception')} 处"
        )
        assert "except asyncio.TimeoutError:" in bind_block, "轮询处没接住超时"
