"""日志里不能出现登录态材料。

市场审核把「token 进日志」判为泄漏，**连片段都算**。换票体里的 `sig` 就是
access_token，跳转链 URL 里带 `keystr` / `ptsigx` / `code`，这些一旦写进日志，
等于在磁盘上留一份可复用的凭据。
"""

from __future__ import annotations

from aiva.auth.ptlogin import redact
from aiva.transport import _body_summary


class TestBodySummary:
    def test_ticket_body_hides_values(self):
        body = {
            "login_info": {
                "appid": "1001",
                "openid": "OPENID_SECRET",
                "sig": "ACCESS_TOKEN_SECRET",
                "uin": 0,
            }
        }
        summary = _body_summary(body)

        assert "OPENID_SECRET" not in summary
        assert "ACCESS_TOKEN_SECRET" not in summary
        assert "login_info" in summary, "字段名要留着，不然排查分不清请求长什么样"

    def test_plain_fields_hide_values_too(self):
        """只保留字段名是硬规则：白名单一漏就是一次泄漏。"""
        summary = _body_summary({"mcode": "MCODE_SECRET", "page": 1})

        assert "MCODE_SECRET" not in summary
        assert "mcode" in summary


class TestTraceSanitize:
    def test_keys_in_jump_chain_scrubbed(self):
        trace = redact(
            "https://ssl.ptlogin2.qq.com/check?uin=123&ptsigx=PTSIGX_SECRET&code=CODE_SECRET"
        )

        assert "PTSIGX_SECRET" not in trace
        assert "CODE_SECRET" not in trace

    def test_json_form_scrubbed(self):
        cleaned = redact('{"access_token": "TOKEN_SECRET", "openid": "OPENID_SECRET"}')

        assert "TOKEN_SECRET" not in cleaned
        assert "OPENID_SECRET" not in cleaned

    def test_callback_url_scrubbed(self):
        cleaned = redact("auth://tauth.qq.com/?#access_token=TOKEN_SECRET&openid=OPENID_SECRET")

        assert "TOKEN_SECRET" not in cleaned
        assert "OPENID_SECRET" not in cleaned

    def test_plain_text_untouched(self):
        assert redact("第2跳 302 无下一跳") == "第2跳 302 无下一跳"
