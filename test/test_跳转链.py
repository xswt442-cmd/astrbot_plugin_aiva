"""跳转链的解析逻辑：腾讯这套登录链的「下一跳」不总是 302。

线上事故（2026-10-13 17:41）：扫码成功了，腾讯回 `code=0`，给出的地址是
`http://ptlogin4.openmobile.qq.com/check_sig?...&ptsigx=...`。我们把这一跳
请求回来之后什么凭据都没捞到，因为 `_follow_redirects` 只认两种形状：
HTTP `Location` 头，和正文里的 `auth://tauth.qq.com/...`。
`check_sig` 返回的既不是 302 也不是 auth://，于是直接断链。

这些测试只测纯函数（`_next_hop_in_body`、`_extract_credentials`），不碰网络。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aiva.auth.ptlogin import QrSession, _can_follow, _extract_credentials, _next_hop_in_body

PTLOGIN_SOURCE = Path(__file__).resolve().parent.parent / "aiva" / "auth" / "ptlogin.py"


class TestNextHopInBody:
    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            # 最常见的两种 JS 跳转
            (
                """<script>location.href="http://ptlogin2.graph.qq.com/ck/x?a=1"</script>""",
                "http://ptlogin2.graph.qq.com/ck/x?a=1",
            ),
            (
                """location.replace('https://xui.ptlogin2.qq.com/next')""",
                "https://xui.ptlogin2.qq.com/next",
            ),
            ('window.location = "http://a.com/b"', "http://a.com/b"),
            ("location.assign('http://a.com/c')", "http://a.com/c"),
            # meta refresh
            ('<meta http-equiv="refresh" content="0;url=http://a.com/d">', "http://a.com/d"),
            # 转义过的 & 和 / 必须还原，否则拼出来的 URL 少参数
            ('location.href="http://a.com/e?x=1\\x26y=2"', "http://a.com/e?x=1&y=2"),
            ('location.href="http:\\/\\/a.com\\/f"', "http://a.com/f"),
        ],
    )
    def test_recognizes_hop_shapes(self, body: str, expected: str):
        assert _next_hop_in_body(body) == expected

    @pytest.mark.parametrize(
        "body",
        [
            "",
            "<html><body>没有跳转</body></html>",
            'location.href="about:blank"',
            'location.href="javascript:void(0)"',
            'location.href="#"',
        ],
    )
    def test_ignores_non_hops(self, body: str):
        assert _next_hop_in_body(body) == ""

    def test_returns_first_hop(self):
        body = """location.href="http://a.com/first";location.href="http://a.com/second";"""
        assert _next_hop_in_body(body) == "http://a.com/first"


class TestExtractCredentials:
    def test_reads_standard_callback(self):
        callback = "auth://tauth.qq.com/?#access_token=AT123&openid=OID456"
        result = _extract_credentials(callback)
        assert result is not None
        assert result.openid == "OID456"
        assert result.access_token == "AT123"

    def test_digs_callback_out_of_body(self):
        body = """<script>location.href="auth://tauth.qq.com/?#access_token=T&openid=O"</script>"""
        result = _extract_credentials(body)
        assert result is not None
        assert result.openid == "O"
        assert result.access_token == "T"

    def test_missing_token_is_not_credential(self):
        assert _extract_credentials("auth://tauth.qq.com/?#openid=OID456") is None

    def test_missing_openid_is_not_credential(self):
        assert _extract_credentials("auth://tauth.qq.com/?#access_token=AT123") is None

    @pytest.mark.parametrize("text", ["", "没有任何凭据的一段文字", "http://a.com/x?y=1"])
    def test_unrelated_text_returns_none(self, text: str):
        assert _extract_credentials(text) is None

    def test_restores_uppercase_escape(self):
        body = 'location.href="auth://tauth.qq.com/?#access_token=T\\u0026openid=O"'
        result = _extract_credentials(body)
        assert result is not None
        assert result.openid == "O"
        assert result.access_token == "T"


class TestSessionTrace:
    def test_trace_starts_empty(self):
        assert QrSession().trace == []

    def test_trace_recorded_per_hop(self):
        """跳数要够。check_sig 之后往往还要再跳两三次。"""
        source = PTLOGIN_SOURCE.read_text(encoding="utf-8")
        assert "max_hops: int = 12" in source
        assert "_trace" in source


class TestBuildOpenloginData:
    """漏了 pt_openlogin_data，腾讯会回 code=0 但拿不到 token。

    这是整条链路最阴的坑：没有任何报错，只是静默少一半信息。
    """

    @pytest.mark.asyncio
    async def test_builds_31_fields(self):
        import aiohttp

        from aiva.auth.ptlogin import _build_openlogin_data

        session = QrSession()
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as raw:
            session._session = raw
            data = _build_openlogin_data(session)
        fields = dict(pair.split("=", 1) for pair in data.split("&"))
        # 31 项：which/refer_cgi/.../loginty。少一项腾讯就少一半信息。
        assert len(fields) == 31, f"openlogin_data 字段数变了：{len(fields)}"
        assert fields["openapi"] == "1011"
        assert fields["has_auth"] == "1"
        assert fields["update_auth"] == "1"
        assert fields["page_type"] == "1"
        assert fields["pf"] == "openmobile_android"
        assert fields["response_type"] == "token"
        assert fields["redirect_uri"].startswith("auth%3A%2F%2Ftauth.qq.com")
        assert fields["client_id"] == "102061775"

    @pytest.mark.asyncio
    async def test_reads_back_from_xlogin_query(self):
        """refer_cgi / sdkv / h5sig 等要跟 xlogin 的参数对齐。"""
        import aiohttp

        from aiva.auth.ptlogin import _build_openlogin_data

        session = QrSession()
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as raw:
            session._session = raw
            fields = dict(p.split("=", 1) for p in _build_openlogin_data(session).split("&"))
        assert fields["refer_cgi"] == "m_authorize"
        assert fields["sdkv"] == "3.5.17.lite"
        assert fields["loginty"] == "6"
        assert fields["status_os"] == "12"

    def test_poll_must_carry_it(self):
        source = PTLOGIN_SOURCE.read_text(encoding="utf-8")
        assert '"pt_openlogin_data": self._openlogin_data' in source


class TestExtractLoginSig:
    def test_prefers_page_value(self):
        from aiva.auth.ptlogin import _extract_login_sig

        page = 'g_login_sig=encodeURIComponent("abc%2Fdef")'
        assert _extract_login_sig(page, QrSession()) == "abc/def"

    def test_falls_back_to_cookie(self):
        from aiva.auth.ptlogin import _extract_login_sig

        session = QrSession()
        session._get_cookie = lambda name, domain="": "from-cookie"  # type: ignore[method-assign]
        assert _extract_login_sig("没有这一行", session) == "from-cookie"

    def test_empty_when_neither(self):
        from aiva.auth.ptlogin import _extract_login_sig

        session = QrSession()
        session._get_cookie = lambda name, domain="": ""  # type: ignore[method-assign]
        assert _extract_login_sig("", session) == ""


class TestCollectKeyCandidates:
    def test_collects_from_query(self):
        from aiva.auth.ptlogin import _collect_key_candidates

        session = QrSession()
        session._get_cookie = lambda name, domain="": ""  # type: ignore[method-assign]
        url = "https://x.com/y?keystr=KEY123&other=1"
        candidates = _collect_key_candidates(session, url)
        assert ("KEY123", "url:keystr") in candidates

    def test_collects_from_fragment(self):
        from aiva.auth.ptlogin import _collect_key_candidates

        session = QrSession()
        session._get_cookie = lambda name, domain="": ""  # type: ignore[method-assign]
        candidates = _collect_key_candidates(session, "https://x.com/y#redirect_uri_key=FRAGKEY")
        assert ("FRAGKEY", "url:redirect_uri_key") in candidates

    def test_all_six_synonyms_recognized(self):
        from aiva.auth.ptlogin import KEY_FIELD_NAMES, _collect_key_candidates

        session = QrSession()
        session._get_cookie = lambda name, domain="": ""  # type: ignore[method-assign]
        for name in KEY_FIELD_NAMES:
            candidates = _collect_key_candidates(session, f"https://x.com/y?{name}=V-{name}")
            assert (f"V-{name}", f"url:{name}") in candidates, f"{name} 没被收进来"

    def test_dedupes(self):
        from aiva.auth.ptlogin import _collect_key_candidates

        session = QrSession()
        session._get_cookie = lambda name, domain="": ""  # type: ignore[method-assign]
        url = "https://x.com/y?keystr=SAME&key=SAME"
        candidates = _collect_key_candidates(session, url)
        assert len([c for c in candidates if c[0] == "SAME"]) == 1

    def test_collects_from_cookie(self):
        from aiva.auth.ptlogin import _collect_key_candidates

        session = QrSession()
        session._get_cookie = lambda name, domain="": "CK" if name == "keystr" else ""  # type: ignore[method-assign]
        candidates = _collect_key_candidates(session, "")
        assert any(v == "CK" for v, _ in candidates)

    def test_empty_when_nothing_found(self):
        from aiva.auth.ptlogin import _collect_key_candidates

        session = QrSession()
        session._get_cookie = lambda name, domain="": ""  # type: ignore[method-assign]
        assert _collect_key_candidates(session, "") == []


class TestExtractAuthCallback:
    def test_reads_callback_wrapped_json(self):
        from aiva.auth.ptlogin import _extract_auth_callback

        body = '_Callback({"url":"auth://tauth.qq.com/?#access_token=T&openid=O"});'
        callback = _extract_auth_callback(body)
        assert callback.startswith("auth://")
        assert "access_token=T" in callback

    def test_broken_json_falls_back_to_regex(self):
        from aiva.auth.ptlogin import _extract_auth_callback

        body = "_Callback({坏掉的 JSON); auth://tauth.qq.com/?#access_token=T&openid=O"
        callback = _extract_auth_callback(body)
        assert callback.startswith("auth://")

    def test_non_auth_url_rejected(self):
        from aiva.auth.ptlogin import _extract_auth_callback

        assert _extract_auth_callback('_Callback({"url":"https://evil.com"});') == ""

    @pytest.mark.parametrize("body", ["", "什么都没有"])
    def test_no_callback_returns_empty(self, body: str):
        from aiva.auth.ptlogin import _extract_auth_callback

        assert _extract_auth_callback(body) == ""


class TestExchangeChain:
    def test_url_is_openmobile_without_4(self):
        """`openmobile.qq.com` 能正常解析；带 4 的那个在内网被屏蔽。"""
        from aiva.auth.ptlogin import EXCHANGE_REDIRECT_URL

        assert EXCHANGE_REDIRECT_URL == "https://openmobile.qq.com/oauth2.0/m_get_redirect_url"

    def test_exchange_is_attempted(self):
        """跟完跳转没有 token 时必须走换回调这条路，不能直接抛错。"""
        source = PTLOGIN_SOURCE.read_text(encoding="utf-8")
        assert "await self._exchange_with_key(target)" in source, "必须在抛错前先尝试换回调"


class TestRedirectOnlyTencent:
    """会话里带着 cookie，跟着任意 Location 走等于把登录态交给对方。

    腾讯会换子域（ptlogin2 / ptlogin4 / xui / graph…），枚举主机名跟不上，
    所以按后缀 `*.qq.com` 判。
    """

    @pytest.mark.parametrize(
        "url",
        [
            "https://xui.ptlogin2.qq.com/cgi-bin/xlogin",
            "http://ptlogin4.openmobile.qq.com/check_sig?ptsigx=1",
            "https://ssl.ptlogin2.qq.com/check?uin=1",
            "https://connect.qq.com/",
            "https://QQ.COM/a",
        ],
    )
    def test_tencent_hosts_allowed(self, url):
        assert _can_follow(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://evil.com/",
            "https://qq.com.evil.com/",  # 后缀伪装
            "https://notqq.com/",
            "http://127.0.0.1:8080/",
            "file:///etc/passwd",
            "",
        ],
    )
    def test_other_hosts_rejected(self, url):
        assert not _can_follow(url)

    def test_follow_redirects_really_blocks(self):
        source = PTLOGIN_SOURCE.read_text(encoding="utf-8")
        assert "_can_follow(next_url)" in source, "拿到了下一跳却没用白名单判，等于没有拦"
