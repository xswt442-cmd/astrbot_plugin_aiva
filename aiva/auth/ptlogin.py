"""QQ 扫码登录，拿到掌瓦要的 openid 与 access_token。

掌瓦这条链路比营地简单：全流程纯 HTTP，**不需要浏览器侧车**，也不需要
XXTEA / YSDK 签名。唯一的「签名」是轮询时的 `ptqrtoken`，纯算术。

实测记录（2026-10-13，协议日期要写进注释而不是靠记忆）：

- 出码必须打 `https://xui.ptlogin2.qq.com/ssl/ptqrshow`。换成 `ssl.ptlogin2.qq.com`
  域名会返回一段 HTML 而不是 PNG——同一个 `/ssl/` 路径，只有放在 `xui.` 子域下才出图。
- `xlogin` 会下发 `pt_login_sig`、`pt_clientip`、`pt_serverip` 等 Cookie。后续请求
  必须用**同一个 cookie jar**，重开 Session 会丢 `pt_login_sig`，轮询必然失败。
- 不带 cookie 打业务接口时，服务端返回 `result:999999` +
  `errMsg:"auth web ticket fail ... DecodeTgpTicket"`，这是「活着但要登录」的
  标准答案，不要当成接口挂了。
"""

from __future__ import annotations

import re
import time
import urllib.parse
from typing import Any

import aiohttp

__all__ = ["QrSession", "ScanResult", "NotReady", "redact"]

# 日志脱敏。跳转链 URL 里带 keystr / ptsigx / code，换票体里带 access_token，
# 这些一旦进日志就等于把登录态留在磁盘上。市场审核把「片段」也算泄漏。
_SENSITIVE_FIELD = re.compile(
    r"(?i)(access_token|refresh_token|openid|sig|keystr|ptsigx|code|skey|p_skey"
    r"|clientkey|pt2gguin|uin|token|password|passwd)"
    r"([\"']?\s*[:=]\s*[\"']?)"
    r"([^\"'&\s,<>}]+)"
)


def redact(text: str) -> str:
    """把 `key=value` / `"key":"value"` 形式的敏感值替换成 `<redacted>`。"""
    if not text:
        return ""
    return _SENSITIVE_FIELD.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", text)


# 手机 QQ 扫码不可逆着来：这组参数是 QQ 互联的移动侧参数，换成别的 unionid
# 会拿到一个能被后续接口拒掉的 token。
XLOGIN_URL = (
    "https://xui.ptlogin2.qq.com/cgi-bin/xlogin?"
    "pt_enable_pwd=1&appid=716027609&pt_3rd_aid=102061775&daid=381"
    "&pt_skey_valid=0&style=35&force_qr=1&autorefresh=1"
    "&s_url=https%3A%2F%2Fconnect.qq.com&refer_cgi=m_authorize"
    "&ucheck=1&fall_to_wv=1&status_os=12"
    "&redirect_uri=auth%3A%2F%2Ftauth.qq.com%2F"
    "&client_id=102061775&pf=openmobile_android&response_type=token"
    "&scope=all&sdkp=a&sdkv=3.5.17.lite"
    "&sign=a6479455d3e49b597350f13f776a6288"
    "&status_machine=MjMxMTdSSzY2Qw%3D%3D&switch=1"
    "&time=1763280194&show_download_ui=true"
    "&h5sig=trobryxo8IPM0GaSQH12mowKG-CY65brFzkK7_-9EW4"
    "&loginty=6"
)
QR_SHOW_URL = "https://xui.ptlogin2.qq.com/ssl/ptqrshow"
QR_POLL_URL = "https://xui.ptlogin2.qq.com/ssl/ptqrlogin"
# 拿 auth:// 回调的真正入口。`check_sig` 跟到底通常只能拿到一个 keystr，
# 必须拿这个 keystr 再打一次这里，才能换成带 openid/access_token 的回调。
# 域名是 `openmobile.qq.com`（不带 4），这个能正常解析。
EXCHANGE_REDIRECT_URL = "https://openmobile.qq.com/oauth2.0/m_get_redirect_url"
# keystr 可能藏在这些名字里。腾讯这几个名字是同义替换，遍历一遍最省事。
KEY_FIELD_NAMES = (
    "redirect_uri_key",
    "keystr",
    "key",
    "uikey",
    "superkey",
    "supertoken",
)
# 收集 cookie 时要扫的域。腾讯在多个子域间来回跳，cookie 落在哪都有可能。
KEY_COOKIE_DOMAINS = (
    "https://xui.ptlogin2.qq.com",
    "https://ssl.ptlogin2.qq.com",
    "https://ptlogin4.openmobile.qq.com",
    "https://openmobile.qq.com",
    "https://connect.qq.com",
)
# 跳转链只允许落在腾讯自己的域里。腾讯会换子域（ptlogin2 / ptlogin4 / xui…），
# 枚举具体主机名跟不上，但后缀稳定。跑到别的域就断链——会话里有 cookie，
# 跟着任意 Location 走等于把登录态交给对方。
ALLOWED_REDIRECT_DOMAIN = ".qq.com"


def _can_follow(url: str) -> bool:
    """这个跳转目标是不是腾讯自己的域。"""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host == "qq.com" or host.endswith(ALLOWED_REDIRECT_DOMAIN)


PT_APPID = "716027609"
DAID = "381"
THIRD_AID = "102061775"

# 跳转链每一跳单独给 5 秒。会话的 total=20 是单条请求预算，而 _follow_redirects 最多走
# 5 跳，沿用值得100 秒——用户在群里发一句话就能把sock_connect 打断。
REDIRECT_TIMEOUT = 12.0

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 12; 23117RK66C Build/V417IR; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
    "Chrome/101.0.4951.61 Mobile Safari/537.36"
)
COMMON_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "zh-CN,zh;q=0.9",
    "X-Requested-With": "com.tencent.apps.valorant",
    "Referer": "https://openmobile.qq.com/",
}


# 轮询一次拿不到结果时抛这个，调用方继续下一个周期。
class NotReady(Exception):
    pass


class ScanResult:
    """一次成功扫码拿到的东西。够换掌瓦凭证就行，多一样都不存。"""

    __slots__ = ("openid", "access_token")

    def __init__(self, openid: str, access_token: str) -> None:
        self.openid = openid
        self.access_token = access_token

    def __repr__(self) -> str:  # pragma: no cover - 只在调试日志里出现
        # 凭据本体不进 repr。绑定失败时日志里常见 repr，别在这里漏。
        return f"扫码结果(openid长度={len(self.openid)})"


def qr_token(qrsig: str) -> int:
    """QQ 那个 hash33：e = (e * 33 + ord(c)) & 0x7FFFFFFF。

    不是加密，是他 JS 里的一段累加。写在这里免得后人以为要引 crypto 库。
    """
    acc = 0
    for ch in qrsig:
        acc = (acc * 33 + ord(ch)) & 0x7FFFFFFF
    return acc


class QrSession:
    """一次扫码的完整生命周期：出码 → 轮询 → 抽 openid。

    用完必须 `await close()`，cookie jar 里装着 `pt_login_sig` 与用户的扫码态。
    """

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._qrsig = ""
        self._login_sig = ""
        self._jsver = "28d22679"
        self._aegis = ""
        # 轮询必带。不带它腾讯会回 code=0（看着像成功）但拿不到 token。
        self._openlogin_data = ""
        # 最近一次轮询拿回来的腾讯原始响应，只给调用方看（打日志 / 排查）。
        self.last_raw = ""
        # 跳转链的逐跳轨迹（URL / 状态 / 下一跳 / 正文摘要）。
        # 只给调用方看：这个模块刻意不 import astrbot，好让本地 pytest 能跑。
        self.trace: list[str] = []

    async def create_qr(self) -> bytes:
        """取一张二维码 PNG，同时把会话内部状态准备好。

        抛 `RuntimeError` 表示腾讯侧没给图，直接让用户重试，不要往下走。
        """
        if self._session is None:
            # `unsafe=True` 只管「允不允许给 IP 主机存 cookie」，跟域无关；
            # 真正的域限制在 `_follow_redirects`：只跟 `*.qq.com`，别的一律断链。
            # 严格策略会把腾讯来回跳时回的 Cookie 丢掉，pt_login_sig 就断了。
            self._session = aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=True),
                timeout=aiohttp.ClientTimeout(total=20),
            )
        assert self._session is not None

        headers = {
            **COMMON_HEADERS,
            "Cookie": "accountType=5; clientType=9",
            "Accept": "text/html,*/*",
        }
        async with self._session.get(XLOGIN_URL, headers=headers) as response:
            page = await response.text(errors="ignore")

        self._login_sig = _extract_login_sig(page, self)
        self._aegis = self._get_cookie("__aegis_uid")
        self._openlogin_data = _build_openlogin_data(self)
        m = re.search(r"/monorepo/([0-9A-Za-z]+)/ptlogin/js/", page)
        if m:
            self._jsver = m.group(1)

        query = urllib.parse.urlencode(
            {
                "s": "8",
                "e": "0",
                "appid": PT_APPID,
                "type": "0",
                "t": str(time.time()),
                "u1": "https://connect.qq.com",
                "daid": DAID,
                "pt_3rd_aid": THIRD_AID,
            }
        )
        async with self._session.get(
            f"{QR_SHOW_URL}?{query}",
            headers={
                **COMMON_HEADERS,
                "Referer": XLOGIN_URL,
                "Accept": "image/webp,image/*,*/*;q=0.8",
            },
        ) as response:
            image = await response.read()

        if image[:8] != b"\x89PNG\r\n\x1a\n":
            # 服务端给了一张 HTML（多半是风控页或被跳走了）。这时候继续轮询
            # 只会拿到一串无意义的 code，不如早点失败。
            raise RuntimeError(f"出码没拿到 PNG（{len(image)} 字节）")

        self._qrsig = self._get_cookie("qrsig")
        if not self._qrsig:
            raise RuntimeError("出码了但没拿到 qrsig")
        return image

    async def poll_once(self) -> ScanResult | None:
        """探一次。返回 None 表示还没扫，调用方再等一轮。

        抛 `RuntimeError` 表示这次扫码已经废了（超时/过期），要重新出码。
        """
        if self._session is None or not self._qrsig:
            raise RuntimeError("还没出码就轮询")

        params = {
            "u1": "https://connect.qq.com",
            "from_ui": "1",
            "type": "1",
            "ptlang": "2052",
            "ptqrtoken": str(qr_token(self._qrsig)),
            "daid": DAID,
            "aid": PT_APPID,
            "pt_3rd_aid": THIRD_AID,
            "pt_openlogin_data": self._openlogin_data,
            "pt_uistyle": "35",
            "jsver": self._jsver,
            "r": str(time.time()),
            "device": "2",
            "ptopt": "1",
        }
        if self._login_sig:
            params["login_sig"] = self._login_sig
        if self._aegis:
            params["aegis_uid"] = self._aegis

        async with self._session.get(
            QR_POLL_URL,
            params=params,
            headers={
                **COMMON_HEADERS,
                "User-Agent": f"{USER_AGENT} tencent_game_emulator",
                "Referer": XLOGIN_URL,
                "Accept": "*/*",
            },
        ) as response:
            text = await response.text(errors="ignore")

        # 原样留一份给调用方打日志。这个模块刻意不 import astrbot
        # （本地 pytest 要能直接跑），所以日志由 main.py 负责。
        self.last_raw = text
        return await self._parse_callback(text)

    async def _parse_callback(self, text: str) -> ScanResult | None:
        """把 ptuiCB(...) 里的一段话拆成登录结果。"""
        m = re.search(r"ptuiCB\('([^']*)','([^']*)','([^']*)','([^']*)','([^']*)'", text)
        if not m:
            return None
        code = m.group(1)
        if code in ("65",):  # 二维码失效
            raise RuntimeError("二维码已失效")
        if code in ("66", "67"):  # 未失效/等待扫码，都不是结果
            return None
        if code != "0":
            return None

        target = m.group(3).replace("\\/", "/").replace("\\x26", "&")
        result = await self._follow_redirects(target)
        if result and result.openid and result.access_token:
            return result

        # 走到这里说明「跟跳转」没拿到 token。但**这不代表失败**——
        # 腾讯的 check_sig 通常只产出一个 keystr，得拿它去换。
        # 这是整条链路最后也是最容易被漏掉的一步。
        result = await self._exchange_with_key(target)
        if result and result.openid and result.access_token:
            self._trace("用密钥换到了凭据")
            return result
        # code=0 表示腾讯那边判定「扫了并确认了」，但我们没从跳转链里捞到
        # openid/access_token。这是**我们**的解析问题，不是用户没扫——
        # 必须跟「还没扫」区分开，否则用户只会看到一句「超时了」。
        raise RuntimeError("腾讯已确认扫码，但没取到 openid/access_token")

    async def _follow_redirects(self, url: str, max_hops: int = 12) -> ScanResult | None:
        """跟着跳转链走，从 auth:// 回调里捞出 openid 与 access_token。

        每一跳都套自己的短超时：会话的 `total=20` 是**整条请求**的预算，
        而这里要走的跳数不少，用会话默认值最坏能拖到一百多秒——用户在群里
        发一句话就能把 `sock_connect` 打断，冒出来的 `asyncio.TimeoutError`
        会一路穿透到 handler，把整条指令炸成「调用插件时出现异常」。

        腾讯这套登录链的形状不固定，实测见过三种「跳到下一跳」的方式：

        1. HTTP 302，用响应头 `Location`；
        2. 正文里的 ``location.href="..."`` / ``location.replace("...")``；
        3. ``<meta http-equiv="refresh" content="0;url=...">``。

        只认第1 种会在这里直接断链——`check_sig` 就是返回第 2 种的。
        每跳都记进 `self.trace`，出问题时能直接看清卡在哪。
        """
        if self._session is None:
            return None
        self.trace = []
        current = url
        visited: set[str] = set()
        for _ in range(max_hops):
            if not current or current in visited:
                break
            visited.add(current)
            result = _extract_credentials(current)
            if result:
                self._trace(f"第{len(self.trace) + 1}跳 地址里直接有凭据")
                return result
            if current.startswith("auth://"):
                break
            body = ""
            next_url = ""
            try:
                async with self._session.get(
                    current,
                    allow_redirects=False,
                    headers={**COMMON_HEADERS, "Referer": XLOGIN_URL, "Accept": "*/*"},
                    timeout=aiohttp.ClientTimeout(total=REDIRECT_TIMEOUT),
                ) as response:
                    status = response.status
                    body = await response.text(errors="ignore")
                    next_url = (response.headers.get("Location", "") or "").strip()
            except (TimeoutError, aiohttp.ClientError, OSError) as exc:
                # 单跳失败不是登录失败，继续试下一跳
                self._trace(f"{_shorten(current)} -> 网络异常 {type(exc).__name__}: {exc}")
                break

            if not next_url:
                # 没有 302。那就看看正文里是用 JS 还是 meta 指向下一跳。
                next_url = _next_hop_in_body(body)
            hop = _shorten(next_url) if next_url else "(无下一跳)"
            self._trace(
                f"{_shorten(current)} -> {status} {hop} 正文{len(body)}字 {_shorten(body, 120)!r}"
            )

            if next_url:
                # 相对路径要先补成绝对地址，否则拿不到 host、也没法判域。
                next_url = urllib.parse.urljoin(current, next_url)
                if not _can_follow(next_url):
                    self._trace(f"跳向非腾讯域，断链：{_shorten(next_url, 60)}")
                    break
                current = next_url
                continue
            m = re.search(r"(auth://tauth\.qq\.com/[^\s\"'<>]+)", body.replace("\\/", "/"))
            if m:
                result = _extract_credentials(m.group(1))
                if result:
                    return result
            result = _extract_credentials(body)
            if result:
                return result
            break
        return None

    async def _exchange_with_key(self, url: str) -> ScanResult | None:
        """拿 keystr 去 `m_get_redirect_url` 换一个带 token 的 auth:// 回调。

        腾讯这套里 `check_sig`（`ptlogin4.openmobile.qq.com`）只负责验签，
        跟到底拿到的往往是一个 keystr 而不是 token。真正的 token 要拿这个
        keystr 再打一次 `openmobile.qq.com/oauth2.0/m_get_redirect_url` 才拿得到。

        keystr 可能藏在跳转 URL 的查询串/fragment 里，也可能落在 cookie 里
        （腾讯跨多个子域跳，cookie 落在哪个域都有可能），所以两个地方都扫，
        并且六个同义字段名都试一遍。
        """
        if self._session is None:
            return None
        candidates = _collect_key_candidates(self, url)
        if not candidates:
            self._trace("跳转链与 cookie 里都没找到 keystr，无法换取回调")
            return None

        self._trace(f"找到 {len(candidates)} 个密钥候选，逐个换回调")
        for key, source in candidates:
            try:
                async with self._session.get(
                    EXCHANGE_REDIRECT_URL,
                    params={"keystr": key},
                    headers={
                        **COMMON_HEADERS,
                        "Referer": "https://imgcache.qq.com/",
                    },
                    timeout=aiohttp.ClientTimeout(total=REDIRECT_TIMEOUT),
                ) as response:
                    body = await response.text(errors="ignore")
                    status = response.status
            except (TimeoutError, aiohttp.ClientError, OSError) as exc:
                self._trace(f"密钥({source})换取异常 {type(exc).__name__}: {exc}")
                continue
            callback = _extract_auth_callback(body)
            result = _extract_credentials(callback)
            if result:
                self._trace(f"密钥({source})换取成功，状态{status} 正文{len(body)}字")
                return result
        self._trace(f"{len(candidates)} 个密钥候选都没换出凭据")
        return None

    def _trace(self, line: str) -> None:
        """记一跳。轨迹要留着给调用方打日志，登录态相关的值不能进日志。"""
        self.trace.append(redact(line))

    def _get_cookie(self, name: str, domain: str = "") -> str:
        """按名字取 cookie。

        指定 `domain` 时只在这个域下找——腾讯跨多个子域跳，同名的 cookie 可能
        落在不同域上，不限定就会拿到错的那份。不指定则全域取第一个。
        """
        if self._session is None:
            return ""
        host = urllib.parse.urlparse(domain).netloc if domain else ""
        for cookie in self._session.cookie_jar:
            if cookie.key != name:
                continue
            if host and host not in (cookie.get("domain") or "").lstrip("."):
                continue
            return cookie.value or ""
        return ""

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None


def _collect_key_candidates(session: QrSession, url: str) -> list[tuple[str, str]]:
    """收集能用来换 auth:// 回调的 keystr。

    三个来源，按可靠性排：跳转 URL 的查询串/fragment → cookie。
    腾讯这几个字段名是同义替换，一个不行换下一个，所以全收。
    """
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(value: str, source: str) -> None:
        k = (value or "").strip()
        if not k or k in seen:
            return
        seen.add(k)
        candidates.append((k, source))

    restored = (url or "").replace("\\/", "/").replace("\\x26", "&")
    parsed = urllib.parse.urlparse(restored)
    chunks = [parsed.query, parsed.fragment]
    if not parsed.query and not parsed.fragment:
        chunks.append(restored)
    for raw in chunks:
        if not raw:
            continue
        try:
            fields = urllib.parse.parse_qs(raw.replace("#&", "&"), keep_blank_values=True)
        except Exception:  # noqa: BLE001 - 格式不是 query 就没东西可取
            continue
        for name in KEY_FIELD_NAMES:
            values = fields.get(name)
            if values:
                add(values[0], f"url:{name}")

    for domain in KEY_COOKIE_DOMAINS:
        for name in KEY_FIELD_NAMES:
            add(
                session._get_cookie(name, domain),
                f"cookie:{urllib.parse.urlparse(domain).netloc}:{name}",
            )

    return candidates


def _extract_auth_callback(body: str) -> str:
    """从 `m_get_redirect_url` 的响应里提出 auth:// 回调。

    正常形状是 ``_Callback({... "url": "auth://..." ...})`` 这种 JS 包装的
    JSON。正则没匹配上就退回直接找 auth:// —— 腾讯改过几次格式，
    别把代码绑死在某一种上。
    """
    if not body:
        return ""
    m = re.search(r"_Callback\s*\(\s*(\{.*?\})\s*\)\s*;?\s*$", body, re.DOTALL)
    if m:
        try:
            import json

            payload = json.loads(m.group(1))
            callback = str(payload.get("url", "") or "").strip()
            if callback.startswith("auth://"):
                return callback
        except Exception:  # noqa: BLE001 - JSON 坏了就退回正则
            pass
    m2 = re.search(r"(auth://tauth\.qq\.com/[^\s\"'<>]+)", body)
    return m2.group(1) if m2 else ""


def _extract_login_sig(page: str, session: QrSession) -> str:
    """取 login_sig。

    优先从 xlogin 页面里的 ``g_login_sig=encodeURIComponent("...")``拿——
    这个才是腾讯这轮签发的那一份。cookie 里的 `pt_login_sig` 只作兜底：
    两者在部分场景下不一致，优先用页面里的。
    """
    m = re.search(r'g_login_sig=encodeURIComponent\(\s*"([^"]+)"\s*\)', page or "")
    if m:
        return urllib.parse.unquote(m.group(1))
    return session._get_cookie("pt_login_sig")


def _build_openlogin_data(session: QrSession) -> str:
    """拼轮询必带的 `pt_openlogin_data`。

    **漏了它腾讯会回 code=0** —— 看着像扫码成功了，实际拿不到 token，
    表现就是「code=0 但没有 openid/access_token」。这是这条链路最阴的坑：
    没有任何报错，只是静默地少一半信息。

    字段与取值参照 Shy7777/valshop-daily（MIT）的实现：29 项，
    其中 13 项从 xlogin 的查询串里回读，其余固定。
    """
    query = urllib.parse.parse_qs(urllib.parse.urlparse(XLOGIN_URL).query)

    def q(name: str, default: str = "") -> str:
        values = query.get(name)
        return values[0] if values else default

    tid = session._get_cookie("idt") or str(int(time.time()))
    pairs: list[tuple[str, str]] = [
        ("which", ""),
        ("refer_cgi", q("refer_cgi", "m_authorize")),
        ("response_type", q("response_type", "token")),
        ("client_id", q("client_id", THIRD_AID)),
        ("state", ""),
        ("display", ""),
        ("openapi", "1011"),
        ("switch", q("switch", "1")),
        ("src", "1"),
        ("sdkv", q("sdkv", "3.5.17.lite")),
        ("sdkp", q("sdkp", "a")),
        ("tid", tid),
        ("pf", q("pf", "openmobile_android")),
        ("need_pay", "0"),
        ("browser", "0"),
        ("browser_error", ""),
        ("serial", ""),
        ("token_key", ""),
        ("redirect_uri", q("redirect_uri", "auth://tauth.qq.com/")),
        ("sign", q("sign", "")),
        ("time", q("time", "")),
        ("status_version", ""),
        ("status_os", q("status_os", "12")),
        ("status_machine", q("status_machine", "")),
        ("page_type", "1"),
        ("has_auth", "1"),
        ("update_auth", "1"),
        ("auth_time", str(int(time.time() * 1000))),
        ("loginfrom", ""),
        ("h5sig", q("h5sig", "")),
        ("loginty", q("loginty", "6")),
    ]
    return urllib.parse.urlencode(pairs)


def _next_hop_in_body(body: str) -> str:
    """从一段正文里找出「下一跳该去哪」。

    腾讯的 `check_sig` 之类接口不返回 302，而是在正文里用 JS 指向下一跳。
    常见三种，按可靠性排序：

    1. ``location.href = "..."`` / ``location.replace("...")`` / ``location.assign``
    2. ``<meta http-equiv="refresh" content="0;url=...">``
    3. ``window.location = "..."``

    正文里的 ``\\x26`` 是被转义过的 ``&``，``\\/`` 是转义过的 ``/``，
    不还原的话拼出来的 URL 会少参数。
    """
    if not body:
        return ""
    restored = body.replace("\\/", "/").replace("\\x26", "&").replace("\\u0026", "&")

    # 排除明显不是「下一跳」的：about:blank、javascript:、data:、纯锚点。
    pattern = (
        r"""(?:window\.)?location(?:\.href)?\s*(?:=|\.replace\(|\.assign\()\s*"""
        r"""["']([^"']+)["']"""
        r"""|http-equiv=["']?refresh["']?[^>]*?content=["'][^"']*?url=([^"']+)["']"""
        r"""|window\.location\s*=\s*["']([^"']+)["']"""
    )
    m = re.search(pattern, restored, re.IGNORECASE)
    if not m:
        return ""
    candidate = next((g for g in m.groups() if g), "")
    candidate = candidate.strip()
    if not candidate or candidate.startswith(("about:", "javascript:", "data:", "#")):
        return ""
    return candidate


def _shorten(text: str, limit: int = 80) -> str:
    """把 URL 或正文压短，便于进日志。"""
    s = (text or "").replace("\n", " ").replace("\r", " ")
    return s if len(s) <= limit else f"{s[:limit]}…"


def _extract_credentials(text: str) -> ScanResult | None:
    """从 auth:// 回调（或含它的一段正文）里取 openid 与 access_token。

    回调格式是 `auth://tauth.qq.com/?#access_token=...&openid=...`，
    注意 `#` 在 query 前头，直接用 urlparse.query 拿不到，要把 `#` 换成 `&`。
    """
    if not text:
        return None
    value = text
    for escape in ("\\/", "\\x26", "\\u0026"):
        value = value.replace(escape, "/" if escape == "\\/" else "&")
    if "#" in value:
        value = value.replace("#", "&", 1)
    m = re.search(r"(auth://[^\s\"'<>]+)", value)
    query = m.group(1) if m else value
    if "?" in query:
        query = query.split("?", 1)[1]
    fields: dict[str, str] = {}
    try:
        from urllib.parse import parse_qs

        for key, values in parse_qs(query, keep_blank_values=True).items():
            if values:
                fields[key] = values[0]
    except Exception:  # noqa: BLE001 - 格式不是 query 就没东西可取，返回 None 即可
        return None
    openid = (fields.get("openid") or "").strip()
    token = (fields.get("access_token") or "").strip()
    if not openid or not token:
        return None
    return ScanResult(openid, token)


def as_credential(scan: ScanResult) -> dict[str, Any]:
    """把扫码结果转成存库用的字典（去掉 openid 之外的所有东西）。"""
    # 只留这两样。上面一层已经有 json 序列化，这里不必再处理。
    return {"openid": scan.openid, "access_token": scan.access_token}


def short_value(value: Any, limit: int = 8) -> str:
    """把凭据裁一小段给人看。日志里判断「有没有」用够了，本体不该出现在日志。"""
    s = str(value)
    return f"{s[:limit]}…（共{len(s)}位）" if len(s) > limit else "（空）"
