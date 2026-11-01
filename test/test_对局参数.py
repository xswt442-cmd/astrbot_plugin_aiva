"""对局列表必须带 scene 与 game_role_id。

线上事故（2026-10-13 19:0x）：绑定成功、登录态正常，但查战绩返回
「登录态失效」。用真凭据打接口才发现：

- `get_main_role_raw` → `result:0`，返回里带着 `scene`(219 字)与 `game_role_id`
- `career/record/list`（不带这两个）→ `result:1003 / "wrong scene: "`
- 同样端点带上这两个 → `result:0`，返回真实对局

而 `_is_login_issue` 把 1003 判成登录过期，于是让用户重新扫码——
重扫一百次也没用，锅还背错。

这些测试不碰网络，只钉住「参数必须带」和「1003 不能当登录过期」两件事。
"""

from __future__ import annotations

import ast
from pathlib import Path

from aiva.transport import _is_login_issue, _pick_role, _translate_error

SOURCE = Path(__file__).resolve().parent.parent / "aiva" / "transport.py"
MAIN_SOURCE = Path(__file__).resolve().parent.parent / "main.py"


class TestMatchListNeedsScene:
    def test_requires_both_params(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "match_list":
                params = [a.arg for a in node.args.args]
                assert "scene" in params and "role_id" in params, f"签名缺参数：{params}"
                return
        raise AssertionError("找不到 match_list")

    def test_body_carries_scene_and_role_id(self):
        text = SOURCE.read_text(encoding="utf-8")
        assert '"game_role_id": role_id' in text
        assert '"scene": scene' in text

    def test_fetch_matches_resolves_scene_itself(self):
        """调用方不该自己拼这个请求体——少传一个就全盘皆输。"""
        text = SOURCE.read_text(encoding="utf-8")
        assert "role_context" in text
        assert "async def fetch_matches(" in text

    def test_callers_go_through_fetch_matches(self):
        """main.py 里不该再有裸调 match_list 的地方。

        正常路径都过 `_fetch_tracked`：它在 `fetch_matches` 外面记一次成败，
        状态和绑定靠这个记录判断，不能只看单次请求。
        """
        text = MAIN_SOURCE.read_text(encoding="utf-8")
        assert "传输.match_list(" not in text, "调用方必须用 fetch_matches()，它会自己取 scene"
        direct = text.count("await fetch_matches(")
        tracked = text.count("_fetch_tracked(")
        assert direct + tracked >= 5, "四个业务调用点都该走 fetch_matches/_取对局记账"
        assert direct == 1, "只有 _取对局记账 内部该裸调 fetch_matches，别处一律走记账版"


class Test1003IsNotLoginExpiry:
    def test_wrong_scene_is_not_login_issue(self):
        raw = {"result": 1003, "err_msg": "wrong scene: "}
        assert not _is_login_issue(raw), "参数错被当成登录过期，会让用户白扫一百次码"

    def test_wrong_scene_has_own_text(self):
        text = _translate_error({"result": 1003, "err_msg": "wrong scene: "})
        assert "重扫码" not in text
        assert "bug" in text or "缺参数" in text

    def test_real_expiry_still_detected(self):
        raw = {"result": 999999, "err_msg": "auth web ticket fail DecodeTgpTicket"}
        assert _is_login_issue(raw)

    def test_bare_1003_is_suspicious(self):
        """没有消息可依据时宁可怀疑登录态，也不要直接放行。"""
        assert _is_login_issue({"result": 1003})

    def test_login_keyword_counts(self):
        assert _is_login_issue({"result": -1, "err_msg": "please login again"})


class TestPickPcRole:
    """同一账号下「无畏契约」和「无畏契约手游」两个角色都会返回，顺序不固定。

    实测（2026-10-19 00:44）：拿手游角色去查战绩，接口回
    `result=1003 err_msg='invalid scene game_id'`，被当成登录失效，
    用户白重绑了三次。端游角色才是要查的那个。
    """

    PC = {"gameName": "无畏契约", "game_role_id": "651cca5e", "tier_text": "白银II"}
    MOBILE = {"gameName": "无畏契约手游", "game_role_id": "24824969", "tier_text": "青铜II"}

    def test_picks_pc_even_when_mobile_first(self):
        assert _pick_role([self.MOBILE, self.PC]) is self.PC

    def test_picks_pc_when_pc_first(self):
        assert _pick_role([self.PC, self.MOBILE]) is self.PC

    def test_falls_back_to_first_without_pc(self):
        assert _pick_role([self.MOBILE]) is self.MOBILE

    def test_empty_returns_empty(self):
        assert _pick_role([]) == {}

    def test_role_context_does_not_pin_first(self):
        source = SOURCE.read_text(encoding="utf-8")
        body = source[source.index("async def role_context") :]
        body = body[: body.index("async def match_list")]
        assert "_pick_role(" in body, "取 list[0] 会随机撞上手游角色"

    def test_invalid_scene_is_not_login_expiry(self):
        """查错了角色（手游 vs 端游）也会回 1003，这个不能让用户重扫码。"""
        raw = {"result": 1003, "err_msg": "invalid scene game_id"}
        assert not _is_login_issue(raw)
        assert "角色" in _translate_error(raw)


class TestErrorEvidenceKept:
    """2025 这种没见过的错误码，只记 result= 等于什么都没记。

    2026-10-25 实测：微信换票回 `result=2025`，日志里只有这个数字，
    腾讯原话（`MlolLoginByWechat fail ret=2025`）没留下来，只能让用户再扫一次。
    """

    def test_short_error_keeps_quote(self):
        from aiva.transport import _short_error

        text = _short_error({"result": 2025, "err_msg": "MlolLoginByWechat fail ret=2025"})
        assert "MlolLoginByWechat" in text

    def test_long_tokens_scrubbed(self):
        from aiva.transport import _short_error

        candidate = "auth fail ticket=abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
        assert "abcdefghijklmnopqrstuvwxyz" not in _short_error({"result": 1, "err_msg": candidate})
        assert "<长串>" in _short_error({"result": 1, "err_msg": candidate})

    def test_raw_summary_has_code_and_message(self):
        from aiva.transport import _raw_summary

        summary = _raw_summary({"result": 2025, "err_msg": "MlolLoginByWechat fail ret=2025"})
        assert "2025" in summary and "MlolLoginByWechat" in summary

    def test_ticket_exchange_failure_is_not_login_expiry(self):
        """换票这一步还没有登录态，任何失败都是业务错误。"""
        source = SOURCE.read_text(encoding="utf-8")
        chunk = source[source.index("if not check:") :]
        chunk = chunk[: chunk.index('if raw.get("result") != 0:', 40)]
        assert "ApiError" in chunk
        assert "LoginExpired" not in chunk
