"""掌上无畏契约（掌瓦）接口客户端。

国服没有玩家可读的公开 API：Riot 官方 API 只覆盖国际服，`api.riotgames.cn`、
`pd.cn.a.pvp.net` 实测 DNS 无记录。能拿到国服战绩的只有腾讯官方 App
「掌上无畏契约」的私有接口。所以：

- 一切走 `app.mval.qq.com`，请求体是明文 JSON，**没有 XXTEA、没有 YSDK 签名**；
- 身份信息全挂在 Cookie 上（openid / access_token / userId / tid）；
- `mcode` 是硬编码的渠道串，腾讯轮换就失效——届时这里会集体报错，
  报错的文案写在 `_translate_error` 里，别让它显示成一串数字。

响应约定：`result == 0` 才是成功，数据在 `data` 里。其余一律走错误翻译。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any

import aiohttp

from .auth.wechat import WECHAT_APPID, WECHAT_MCODE

__all__ = [
    "MvalClient",
    "ApiError",
    "LoginExpired",
    "fetch_matches",
    "tier_of",
    "wechat_login_body",
]

BASE_URL = "https://app.mval.qq.com"
COMMON_QUERY = "source_game_zone=agame&game_zone=agame"

# 换了凭证（openid+token）后的三大入口。返回 999999 就是「活着但要登录」。
LOGIN_BY_QQ = "/go/auth/login_by_qq"
LOGIN_BY_WECHAT = "/go/auth/login_by_wechat"
MAIN_ROLE = "/go/account/get_main_role_raw"
MATCH_LIST = "/go/agame/career/record/list"
SCOREBOARD = "/go/agame/career/record/scoreboard"

APPID = 102061775
MAPPID = 10200
MCODE = "132f0a77d34402abc8463d60100011d19b0e"
DEVICE_NAME = "22041216C"

USER_AGENT = (
    "mval/2.4.0.10053 Channel/10068 Manufacturer/Xiaomi "
    "Mozilla/5.0 (Linux; Android 14; 23117RK66C Build/V417IR; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
    "Chrome/101.0.4951.61 Mobile Safari/537.36"
)


class ApiError(Exception):
    """接口给了非 0 的 result。消息已经翻成人话，可以直接发给用户。"""


class LoginExpired(ApiError):
    """登录态失效，需要重新扫码。"""


def wechat_login_body(code: str) -> dict[str, Any]:
    """微信换票的请求体。

    `login_info` 里四个字段一个都不能省：`appid` 告诉后端用哪个微信应用去换
    openid，`check_third_type` / `wx_info_type` 决定走第三方登录分支，
    外面还要用**微信渠道**的 mcode（和 QQ 那条不是一个值）。
    缺任何一个，后端都会回 `result=2025` + 内层 `code:10002`。
    """
    return {
        "clienttype": 9,
        "config_params": {"client_dev_name": DEVICE_NAME, "lang_type": 0},
        "login_info": {
            "appid": WECHAT_APPID,
            "check_third_type": 1,
            "code": code,
            "wx_info_type": 1,
        },
        "mappid": MAPPID,
        "mcode": WECHAT_MCODE,
        "source_game_zone": "agame",
        "game_zone": "agame",
    }


def build_cookie(credential: dict[str, Any]) -> str:
    """把存下来的凭据拼成掌瓦要的 Cookie 字符串。

    字段顺序照第三方实现来的：腾讯的网关按字符串取值，顺序本身不重要，
    但照抄能少一次「我到底漏了哪个」的排查。
    """
    parts = [
        "clientType=9",
        f"uin=o{credential.get('uin', 0)}",
        f"appid={APPID}",
        f"acctype={credential.get('acctype') or 'qc'}",
        f"openid={credential.get('openid', '')}",
        f"access_token={credential.get('access_token', '') or 'null'}",
        f"userId={credential.get('userId', '')}",
        "accountType=5",
        f"tid={credential.get('tid', '')}",
    ]
    return "; ".join(parts)


PC_GAME_NAME = "无畏契约"


def _pick_role(roles: list[dict[str, Any]]) -> dict[str, Any]:
    """从角色列表里挑端游角色。

    掌瓦会把同一账号下的「无畏契约」与「无畏契约手游」一起返回，**顺序不固定**。
    拿手游那个去查战绩，接口回 `result:1003 err_msg='invalid scene game_id'`，
    看着像登录态失效，其实是查错了角色。没有端游角色就退回第一个。
    """
    for role in roles:
        if str(role.get("gameName") or "").strip() == PC_GAME_NAME:
            return role
    return roles[0] if roles else {}


async def fetch_matches(
    credential: dict[str, Any], page_size: int = 20
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """取这个用户的近期对局列表，外加角色信息。

    **一个都不能少**：这个函数会自己先打一次主角色接口拿 `scene` 和
    `game_role_id`，再拿它们去要对局列表。这两个参数缺任何一个，腾讯都回
    `result:1003 / "wrong scene: "`——而 1003 长得像登录过期，会让人以为
    要重新扫码，其实是自己的锅，重扫一百次也没用。

    所以调用方只用管传凭据，别自己去拼这个请求体。

    角色信息（`tier_text` 就是段位，如「钻石II」）一起返回：锐评的提示词
    要求称号里带段位，但段位**不在对局列表里**，只在这里。以前没返回，
    模型就自己编了一个——线上真出过钻石玩家被评成「铂金」。
    """
    client = MvalClient()
    role, scene, role_id = await client.role_context(credential)
    data = await client.match_list(credential, scene, role_id, page_size=page_size)
    return (data.get("battle_list") or data.get("list") or []), role


def tier_of(role: dict[str, Any]) -> str:
    """从主角色返回里取段位文字，没有就返回空串。

    只认 `tier_text`（「钻石II」这种带小段的）。`tier` 是数字档位，
    映射表得自己维护，猜错就是又一句编的段位。
    """
    return str(role.get("tier_text") or "").strip()


_log_hook: Callable[[str], None] | None = None


def set_log_hook(hook: Callable[[str], None] | None) -> None:
    """挂一个日志函数进来。

    这个模块刻意不 import astrbot —— 本地 pytest 要能直接跑它。
    代价是拿不到框架 logger，而「用户说没反应」这类问题唯一的出路就是日志，
    所以由 main.py 在启动时把 logger 挂进来。
    """
    global _log_hook
    _log_hook = hook


def _log(line: str) -> None:
    if _log_hook is not None:
        _log_hook(line)


class MvalClient:
    """一次性的请求器。拿到凭据后调用 `fetch...` / `get...` 系列。"""

    def __init__(self, timeout: float = 20.0) -> None:
        self._timeout = timeout

    async def login_by_qq(self, openid: str, access_token: str) -> dict[str, Any]:
        """用扫码拿到的 openid + access_token 换掌瓦的 userId / tid。

        换回来的是**完整凭据**：后面查战绩必须带 userId 与 tid，只有
        openid 会被网关判为无效。
        """
        body = {
            "clienttype": 9,
            "config_params": {"client_dev_name": "23117RK66C", "lang_type": 0},
            "login_info": {
                "appid": APPID,
                "openid": openid,
                "qq_info_type": 5,
                "sig": access_token,
                "uin": 0,
            },
            "mappid": MAPPID,
            "mcode": MCODE,
            "source_game_zone": "agame",
            "game_zone": "agame",
        }
        headers = {
            "Cookie": "clientType=9; openid=null; access_token=null;",
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        }
        raw = await self._request("POST", LOGIN_BY_QQ, headers=headers, json_body=body, check=False)
        data = raw.get("data") or {}
        login = data.get("login_info") or {}
        return {
            "openid": openid,
            "access_token": access_token,
            "userId": str(login.get("user_id", "")),
            "tid": str(login.get("wt", "")),
            "uin": int(login.get("uin") or 0),
            "acctype": "qc",
        }

    async def login_by_wechat(self, code: str) -> dict[str, Any]:
        """用微信授权码换掌瓦的 userId / tid。

        请求体不能省字段：`login_info` 里除了 `code` 还要 `appid`、
        `check_third_type`、`wx_info_type`，外面要用微信通道的 `mcode`。
        少任何一个，后端拿去微信换 openid 都会失败，回 `result=2025` +
        内层 `code:10002`——看着像「码无效」，其实是我们少传了参数。
        """
        headers = {
            "Cookie": "clientType=9; openid=null; access_token=null;",
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        }
        raw = await self._request(
            "POST", LOGIN_BY_WECHAT, headers=headers, json_body=wechat_login_body(code), check=False
        )
        data = raw.get("data") or {}
        login = data.get("login_info") or {}
        return {
            "openid": str(login.get("openid") or data.get("openid") or ""),
            "access_token": str(login.get("access_token") or data.get("access_token") or ""),
            "userId": str(login.get("user_id", "")),
            "tid": str(login.get("wt", "")),
            "uin": int(login.get("uin") or 0),
            "acctype": "wx",
        }

    async def main_role(self, credential: dict[str, Any]) -> dict[str, Any]:
        """取默认游戏角色。

        返回里有两个**后面每个接口都要用**的字段，别丢：
        - `scene`：219 长的临时令牌，对局列表/详情全都必带，缺了接口回
          `result:1003 wrong scene`（而且这个 1003 **不是**登录过期，
          翻译错了会让人以为要重新扫码）。
        - `game_role_id`：形如 `651cca5e-...`，同样必带。
        """
        raw = await self._request("POST", MAIN_ROLE, credential, json_body={"_t": int(time.time())})
        return raw.get("data") or {}

    async def role_context(self, credential: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
        """一次性拿全：角色信息 + `scene` + `game_role_id`。

        每次查战绩都要这三个东西，而 `scene` 只在主角色返回里、且**每轮都会变**，
        所以不能存起来复用——必须每次现取。封在这里，免得每个调用点各写一遍
        然后漏传 scene（漏了的表现是 `result:1003 wrong scene`，很像登录过期，
        但让用户重扫码也没用）。

        角色顺序不固定，**不能取 `list[0]`**：同一账号下「无畏契约」和
        「无畏契约手游」两个角色都会返回，拿手游那个去查战绩，接口回
        `result:1003 err_msg='invalid scene game_id'`——看着像登录失效，
        其实是查错了角色。
        """
        data = await self.main_role(credential)
        role = _pick_role(data.get("list") or [])
        if not role:
            raise ApiError("掌瓦没返回游戏角色，去 App 里确认已绑定，再重发 /爱瓦 绑定")
        return role, str(role.get("scene") or ""), str(role.get("game_role_id") or "")

    async def match_list(
        self,
        credential: dict[str, Any],
        scene: str,
        role_id: str,
        page_no: int = 1,
        page_size: int = 20,
        baton: str = "",
    ) -> dict[str, Any]:
        """取近期对局。

        `scene` 与 `role_id` 来自 `main_role()`。这两个参数不是可选的：
        少了任何一个，腾讯都回 `result:1003 / "wrong scene: "`。

        翻页用 `page_no` + `baton`：第一次 baton 留空，之后把上一次返回的
        `next_baton` 原样传回来。
        """
        body: dict[str, Any] = {
            "game_role_id": role_id,
            "scene": scene,
            "page_no": page_no,
            "page_size": page_size,
            "_t": int(time.time()),
        }
        if baton:
            body["baton"] = baton
        raw = await self._request("POST", MATCH_LIST, credential, json_body=body)
        return raw.get("data") or {}

    async def scoreboard(
        self,
        credential: dict[str, Any],
        scene: str,
        battle_id: str,
        match_id: str = "",
        role_id: str = "",
    ) -> dict[str, Any]:
        """取单局的十人记分板：K/D/A、ACS、爆头率、经济、特工。

        `battle_id` 与 `match_id` 都是列表里那条记录自带的，别混：前者叫
        `battle_id`，后者叫 `match_id`，两个不同的 UUID。
        """
        body: dict[str, Any] = {
            "battle_id": battle_id,
            "match_id": match_id,
            "scene": scene,
            "_t": int(time.time()),
        }
        if role_id:
            body["game_role_id"] = role_id
        raw = await self._request("POST", SCOREBOARD, credential, json_body=body)
        return raw.get("data") or {}

    async def _request(
        self,
        method: str,
        path: str,
        credential: dict[str, Any] | None = None,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        check: bool = True,
    ) -> dict[str, Any]:
        final_headers = (
            dict(headers)
            if headers
            else {
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                "Connection": "keep-alive",
                "Cookie": build_cookie(credential or {}),
            }
        )
        url = f"{BASE_URL}{path}"
        if COMMON_QUERY:
            url = f"{url}?{COMMON_QUERY}"

        _log(f"→ {method} {path} body={_body_summary(json_body)}")
        # 用 aiohttp 默认的 TLS：校验证书链与主机名。请求里带 Cookie
        # （openid / access_token / tid），关校验等于把这几个交给中间人。
        # 容器 CA 缺失请装 ca-certificates，不要全局降级。
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                method, url, headers=final_headers, json=json_body
            ) as response:
                text = await response.text(errors="ignore")
            try:
                raw = json.loads(text) if text.strip().startswith("{") else {}
            except json.JSONDecodeError:
                raise ApiError(f"接口没返回 JSON（HTTP {response.status}）") from None
        _log(
            f"← {path} HTTP {response.status} result={raw.get('result')!r}"
            + ("" if raw.get("result") == 0 else f" {_short_error(raw)}")
        )

        if not check:
            if raw.get("result") != 0:
                # 换票这一步还没有登录态可言（换票就是来拿登录态的），
                # 所以任何失败都算业务错误，别翻译成「重新扫码」。
                #
                # 整个响应体都记下来：这里的错误码没人见过（2025），
                # err_msg 又常常只是把码复述一遍，只有 data 里可能带线索。
                _log(
                    f"换票失败原文 {_scrub_long_tokens(json.dumps(raw, ensure_ascii=False))[:400]}"
                )
                raise ApiError(f"{_translate_error(raw)}（{_raw_summary(raw)}）")
            return raw
        if raw.get("result") != 0:
            # 登录态过期跟其他错误不是一个性质：前者要用户重新扫码，
            # 后者多半是接口变了。上层用不同类型接。
            #
            # **两种都要带原文**：只翻译成人话的话，用户看到「登录态失效」
            # 但我们完全不知道腾讯到底说了什么，这种黑箱已经浪费过一整天。
            # 消息文本里带上 result 与 err_msg，排查时直接能对上。
            translated = _translate_error(raw)
            detail = _raw_summary(raw)
            if _is_login_issue(raw):
                raise LoginExpired(f"{translated}（{detail}）") from None
            raise ApiError(f"{translated}（{detail}）") from None
        return raw


def _body_summary(body: dict[str, Any] | None) -> str:
    """请求体的日志摘要：只列字段名，一个值都不打。

    换票体的 `login_info` 里就是 openid 与 sig（sig 就是 access_token）。
    早先这里对 dict 直接 `{值!r}`，等于把凭据原文写进 INFO 日志；
    市场审核把「token 进日志」判为泄漏，**连片段都不行**。
    """
    if not body:
        return "{}"

    def item(key: str, value: Any) -> str:
        if isinstance(value, dict):
            return f"{key}={{{', '.join(map(str, value))}}}"
        return f"{key}=<{type(value).__name__}>"

    return "{" + ", ".join(item(k, v) for k, v in body.items()) + "}"


def _is_login_issue(raw: dict[str, Any]) -> bool:
    """判断是不是「登录态过期，要用户重新扫码」。

    **别只看错误码**：`result:1003` 线上实测过两种完全不同的含义——
    参数错（`wrong scene:`，我们自己的锅）和真登录过期（腾讯的锅）。
    把参数错翻译成「请重新扫码」会让用户白扫一次码，锅还背错。

    所以先看消息文本里有没有登录态相关的关键词，再看码。
    """
    message = str(raw.get("err_msg") or raw.get("errMsg") or raw.get("msg") or "").lower()
    if any(
        word in message for word in ("wrong scene", "invalid scene", "wrong param", "bad request")
    ):
        # 参数/角色错，别赖到登录态头上。
        return False
    code = raw.get("result", -1)
    if "ticket" in message or "login" in message or "登录" in message:
        return True
    # 999999 配 DecodeTgpTicket 才是腾讯的登录态问题；
    # 1001/1003 没有明确消息时按可疑处理。
    return code in (1001, 1003, 999999)


def _server_message(raw: dict[str, Any]) -> str:
    return str(raw.get("err_msg") or raw.get("errMsg") or raw.get("msg") or "")


def _scrub_long_tokens(text: str) -> str:
    """把票据/令牌这类长串抹掉。日志是 INFO，会被贴进 issue。"""
    return re.sub(r"[A-Za-z0-9_\-]{40,}", "<长串>", text)


def _short_error(raw: dict[str, Any]) -> str:
    """失败时的日志尾巴：带上腾讯的原话。

    **必须带**：1003 有好几种含义，2025 这种没见过的码更是只能看原话。
    只记 `result=` 的话，每次都得拿真凭据重放一遍才能判断——已经被这个
    盲区绕过两回。
    """
    return f"err={_scrub_long_tokens(_server_message(raw))[:160]!r}"


def _raw_summary(raw: dict[str, Any]) -> str:
    """跟在错误文案后面给用户看的那一截。"""
    return (
        f"result={raw.get('result')!r} err_msg={_scrub_long_tokens(_server_message(raw))[:160]!r}"
    )


def _translate_error(raw: dict[str, Any]) -> str:
    """把 result / errMsg 翻成人能读的一句话。

    别把 errMsg 原样抛给用户——那是给抓包的人看的。
    """
    code = raw.get("result", -1)
    message = str(raw.get("err_msg") or raw.get("errMsg") or "")
    if _is_login_issue(raw):
        return "登录态失效，重新发 /爱瓦 绑定"
    if "wrong scene" in message.lower():
        # 自己少传了 scene/game_role_id，跟用户的登录态无关。
        return "内部取数据缺参数（scene），这是插件的 bug，已记日志"
    if "invalid scene" in message.lower():
        # scene 与 game_role_id 不配对：通常是挑错了游戏角色（手游 vs 端游）。
        return "查错了游戏角色，这是插件的 bug，已记日志"
    if code == 1018:
        return "查得太快了，稍等一下再试"
    if code == 1203:
        return "对方关掉了战绩展示，查不到"
    return f"接口返回错误码 {code}"
