"""掌上无畏契约（掌瓦）接口客户端。

国服没有玩家可读的公开 API：Riot 官方 API 只覆盖国际服，`api.riotgames.cn`、
`pd.cn.a.pvp.net` 实测 DNS 无记录。能拿到国服战绩的只有腾讯官方 App
「掌上无畏契约」的私有接口。所以：

- 一切走 `app.mval.qq.com`，请求体是明文 JSON，**没有 XXTEA、没有 YSDK 签名**；
- 身份信息全挂在 Cookie 上（openid / access_token / userId / tid）；
- `mcode` 是硬编码的渠道串，腾讯轮换就失效——届时这里会集体报错，
  报错的文案写在 `_翻译错误` 里，别让它显示成一串数字。

响应约定：`result == 0` 才是成功，数据在 `data` 里。其余一律走错误翻译。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import aiohttp

__all__ = [
    "掌瓦传输",
    "业务错误",
    "登录过期",
    "取对局",
    "取段位",
]

_接口根 = "https://app.mval.qq.com"
_通用查询 = "source_game_zone=agame&game_zone=agame"

# 换了凭证（openid+token）后的三大入口。返回 999999 就是「活着但要登录」。
_登录换票 = "/go/auth/login_by_qq"
_主角色 = "/go/account/get_main_role_raw"
_对局列表 = "/go/agame/career/record/list"
_对局详情 = "/go/agame/career/record/scoreboard"

_APPID = 102061775
_MAPPID = 10200
_MCODE = "132f0a77d34402abc8463d60100011d19b0e"

_UA = (
    "mval/2.4.0.10053 Channel/10068 Manufacturer/Xiaomi "
    "Mozilla/5.0 (Linux; Android 14; 23117RK66C Build/V417IR; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
    "Chrome/101.0.4951.61 Mobile Safari/537.36"
)


class 业务错误(Exception):
    """接口给了非 0 的 result。消息已经翻成人话，可以直接发给用户。"""


class 登录过期(业务错误):
    """登录态失效，需要重新扫码。"""


def 拼Cookie(凭据: dict[str, Any]) -> str:
    """把存下来的凭据拼成掌瓦要的 Cookie 字符串。

    字段顺序照第三方实现来的：腾讯的网关按字符串取值，顺序本身不重要，
    但照抄能少一次「我到底漏了哪个」的排查。
    """
    段 = [
        "clientType=9",
        f"uin=o{凭据.get('uin', 0)}",
        f"appid={_APPID}",
        "acctype=qc",
        f"openid={凭据.get('openid', '')}",
        f"access_token={凭据.get('access_token', '') or 'null'}",
        f"userId={凭据.get('userId', '')}",
        "accountType=5",
        f"tid={凭据.get('tid', '')}",
    ]
    return "; ".join(段)


_端游名 = "无畏契约"


def _选角色(列表: list[dict[str, Any]]) -> dict[str, Any]:
    """从角色列表里挑端游角色。

    掌瓦会把同一账号下的「无畏契约」与「无畏契约手游」一起返回，**顺序不固定**。
    拿手游那个去查战绩，接口回 `result:1003 err_msg='invalid scene game_id'`，
    看着像登录态失效，其实是查错了角色。没有端游角色就退回第一个。
    """
    for 角色 in 列表:
        if str(角色.get("gameName") or "").strip() == _端游名:
            return 角色
    return 列表[0] if 列表 else {}


async def 取对局(
    凭据: dict[str, Any], 每页: int = 20
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
    传输 = 掌瓦传输()
    角色, 场景, 角色ID = await 传输.取角色上下文(凭据)
    数据 = await 传输.取对局列表(凭据, 场景, 角色ID, 每页=每页)
    return (数据.get("battle_list") or 数据.get("list") or []), 角色


def 取段位(角色: dict[str, Any]) -> str:
    """从主角色返回里取段位文字，没有就返回空串。

    只认 `tier_text`（「钻石II」这种带小段的）。`tier` 是数字档位，
    映射表得自己维护，猜错就是又一句编的段位。
    """
    return str(角色.get("tier_text") or "").strip()


_日志钩子: Callable[[str], None] | None = None


def 挂日志(钩子: Callable[[str], None] | None) -> None:
    """挂一个日志函数进来。

    这个模块刻意不 import astrbot —— 本地 pytest 要能直接跑它。
    代价是拿不到框架 logger，而「用户说没反应」这类问题唯一的出路就是日志，
    所以由 main.py 在启动时把 logger 挂进来。
    """
    global _日志钩子
    _日志钩子 = 钩子


def _记(一句: str) -> None:
    if _日志钩子 is not None:
        _日志钩子(一句)


class 掌瓦传输:
    """一次性的请求器。拿到凭据后调用 `取...` 系列。"""

    def __init__(self, 超时: float = 20.0) -> None:
        self._超时 = 超时

    async def 换票(self, openid: str, access_token: str) -> dict[str, Any]:
        """用扫码拿到的 openid + access_token 换掌瓦的 userId / tid。

        换回来的是**完整凭据**：后面查战绩必须带 userId 与 tid，只有
        openid 会被网关判为无效。
        """
        体 = {
            "clienttype": 9,
            "config_params": {"client_dev_name": "23117RK66C", "lang_type": 0},
            "login_info": {
                "appid": _APPID,
                "openid": openid,
                "qq_info_type": 5,
                "sig": access_token,
                "uin": 0,
            },
            "mappid": _MAPPID,
            "mcode": _MCODE,
            "source_game_zone": "agame",
            "game_zone": "agame",
        }
        头 = {
            "Cookie": "clientType=9; openid=null; access_token=null;",
            "User-Agent": _UA,
            "Content-Type": "application/json",
        }
        原始 = await self._请求("POST", _登录换票, headers=头, json体=体, 校验=False)
        数据 = 原始.get("data") or {}
        登录 = 数据.get("login_info") or {}
        return {
            "openid": openid,
            "access_token": access_token,
            "userId": str(登录.get("user_id", "")),
            "tid": str(登录.get("wt", "")),
            "uin": int(登录.get("uin") or 0),
        }

    async def 取主角色(self, 凭据: dict[str, Any]) -> dict[str, Any]:
        """取默认游戏角色。

        返回里有两个**后面每个接口都要用**的字段，别丢：
        - `scene`：219 长的临时令牌，对局列表/详情全都必带，缺了接口回
          `result:1003 wrong scene`（而且这个 1003 **不是**登录过期，
          翻译错了会让人以为要重新扫码）。
        - `game_role_id`：形如 `651cca5e-...`，同样必带。
        """
        原始 = await self._请求("POST", _主角色, 凭据, json体={"_t": int(time.time())})
        return 原始.get("data") or {}

    async def 取角色上下文(self, 凭据: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
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
        数据 = await self.取主角色(凭据)
        角色 = _选角色(数据.get("list") or [])
        if not 角色:
            raise 业务错误("掌瓦没返回游戏角色，去 App 里确认已绑定，再重发 /爱瓦 绑定")
        return 角色, str(角色.get("scene") or ""), str(角色.get("game_role_id") or "")

    async def 取对局列表(
        self,
        凭据: dict[str, Any],
        场景: str,
        角色ID: str,
        页码: int = 1,
        每页: int = 20,
        游标: str = "",
    ) -> dict[str, Any]:
        """取近期对局。

        `场景` 与 `角色ID` 来自 `取主角色()`。这两个参数不是可选的：
        少了任何一个，腾讯都回 `result:1003 / "wrong scene: "`。

        翻页用 `页码` + `游标`：第一次游标留空，之后把上一次返回的
        `next_baton` 原样传回来。
        """
        体: dict[str, Any] = {
            "game_role_id": 角色ID,
            "scene": 场景,
            "page_no": 页码,
            "page_size": 每页,
            "_t": int(time.time()),
        }
        if 游标:
            体["baton"] = 游标
        原始 = await self._请求("POST", _对局列表, 凭据, json体=体)
        return 原始.get("data") or {}

    async def 取记分板(
        self,
        凭据: dict[str, Any],
        场景: str,
        对局ID: str,
        比赛ID: str = "",
        角色ID: str = "",
    ) -> dict[str, Any]:
        """取单局的十人记分板：K/D/A、ACS、爆头率、经济、特工。

        `对局ID` 与 `比赛ID` 都是列表里那条记录自带的，别混：前者叫
        `battle_id`，后者叫 `match_id`，两个不同的 UUID。
        """
        体: dict[str, Any] = {
            "battle_id": 对局ID,
            "match_id": 比赛ID,
            "scene": 场景,
            "_t": int(time.time()),
        }
        if 角色ID:
            体["game_role_id"] = 角色ID
        原始 = await self._请求("POST", _对局详情, 凭据, json体=体)
        return 原始.get("data") or {}

    async def _请求(
        self,
        方法: str,
        路径: str,
        凭据: dict[str, Any] | None = None,
        *,
        json体: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        校验: bool = True,
    ) -> dict[str, Any]:
        头 = (
            dict(headers)
            if headers
            else {
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Content-Type": "application/json",
                "User-Agent": _UA,
                "Connection": "keep-alive",
                "Cookie": 拼Cookie(凭据 or {}),
            }
        )
        地址 = f"{_接口根}{路径}"
        if _通用查询:
            地址 = f"{地址}?{_通用查询}"

        _记(f"→ {方法} {路径} body={_体摘要(json体)}")
        # 用 aiohttp 默认的 TLS：校验证书链与主机名。请求里带 Cookie
        # （openid / access_token / tid），关校验等于把这几个交给中间人。
        # 容器 CA 缺失请装 ca-certificates，不要全局降级。
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self._超时)) as 会话:
            async with 会话.request(方法, 地址, headers=头, json=json体) as 响应:
                文本 = await 响应.text(errors="ignore")
            try:
                原始 = json.loads(文本) if 文本.strip().startswith("{") else {}
            except json.JSONDecodeError:
                raise 业务错误(f"接口没返回 JSON（HTTP {响应.status}）") from None
        _记(f"← {路径} HTTP {响应.status} result={原始.get('result')!r}")

        if not 校验:
            if 原始.get("result") != 0:
                raise 业务错误(_翻译错误(原始))
            return 原始
        if 原始.get("result") != 0:
            # 登录态过期跟其他错误不是一个性质：前者要用户重新扫码，
            # 后者多半是接口变了。上层用不同类型接。
            #
            # **两种都要带原文**：只翻译成人话的话，用户看到「登录态失效」
            # 但我们完全不知道腾讯到底说了什么，这种黑箱已经浪费过一整天。
            # 消息文本里带上 result 与 err_msg，排查时直接能对上。
            译文 = _翻译错误(原始)
            原文 = (
                f"result={原始.get('result')!r} "
                f"err_msg={原始.get('err_msg') or 原始.get('errMsg') or 原始.get('msg')!r}"
            )
            if _是登录问题(原始):
                raise 登录过期(f"{译文}（{原文}）") from None
            raise 业务错误(f"{译文}（{原文}）") from None
        return 原始


def _体摘要(体: dict[str, Any] | None) -> str:
    """请求体的日志摘要：只列字段名，一个值都不打。

    换票体的 `login_info` 里就是 openid 与 sig（sig 就是 access_token）。
    早先这里对 dict 直接 `{值!r}`，等于把凭据原文写进 INFO 日志；
    市场审核把「token 进日志」判为泄漏，**连片段都不行**。
    """
    if not 体:
        return "{}"

    def 一项(键: str, 值: Any) -> str:
        if isinstance(值, dict):
            return f"{键}={{{', '.join(map(str, 值))}}}"
        return f"{键}=<{type(值).__name__}>"

    return "{" + ", ".join(一项(k, v) for k, v in 体.items()) + "}"


def _是登录问题(原始: dict[str, Any]) -> bool:
    """判断是不是「登录态过期，要用户重新扫码」。

    **别只看错误码**：`result:1003` 线上实测过两种完全不同的含义——
    参数错（`wrong scene:`，我们自己的锅）和真登录过期（腾讯的锅）。
    把参数错翻译成「请重新扫码」会让用户白扫一次码，锅还背错。

    所以先看消息文本里有没有登录态相关的关键词，再看码。
    """
    消息 = str(原始.get("err_msg") or 原始.get("errMsg") or 原始.get("msg") or "").lower()
    if any(词 in 消息 for 词 in ("wrong scene", "invalid scene", "wrong param", "bad request")):
        # 参数/角色错，别赖到登录态头上。
        return False
    码 = 原始.get("result", -1)
    if "ticket" in 消息 or "login" in 消息 or "登录" in 消息:
        return True
    # 999999 配 DecodeTgpTicket 才是腾讯的登录态问题；
    # 1001/1003 没有明确消息时按可疑处理。
    return 码 in (1001, 1003, 999999)


def _翻译错误(原始: dict[str, Any]) -> str:
    """把 result / errMsg 翻成人能读的一句话。

    别把 errMsg 原样抛给用户——那是给抓包的人看的。
    """
    码 = 原始.get("result", -1)
    消息 = str(原始.get("err_msg") or 原始.get("errMsg") or "")
    if _是登录问题(原始):
        return "登录态失效，重新发 /爱瓦 绑定"
    if "wrong scene" in 消息.lower():
        # 自己少传了 scene/game_role_id，跟用户的登录态无关。
        return "内部取数据缺参数（scene），这是插件的 bug，已记日志"
    if "invalid scene" in 消息.lower():
        # scene 与 game_role_id 不配对：通常是挑错了游戏角色（手游 vs 端游）。
        return "查错了游戏角色，这是插件的 bug，已记日志"
    if 码 == 1018:
        return "查得太快了，稍等一下再试"
    if 码 == 1203:
        return "对方关掉了战绩展示，查不到"
    return f"接口返回错误码 {码}"
