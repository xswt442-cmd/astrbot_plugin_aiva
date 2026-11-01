"""掌瓦接口的存活探测。

不需要登录也能跑的那部分：确认端点还活着，区分「活着但要登录」和「端点没了」。

区分三件事很重要：

- HTTP 200 + `result:999999` + `auth web ticket fail` = **活着，要登录**。
- HTTP 404 = 这个端点没了。
- 连不上 = 网络或 DNS 的问题。

用法：

    python test/verify_mval.py                          # 探所有要用的端点
    python test/verify_mval.py --只看 /go/agame/career/record/list
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import aiohttp

BASE_URL = "https://app.mval.qq.com"
COMMON_QUERY = "source_game_zone=agame&game_zone=agame"

_UA = (
    "mval/2.4.0.10053 Channel/10068 Manufacturer/Xiaomi "
    "Mozilla/5.0 (Linux; Android 14; 23117RK66C Build/V417IR; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
    "Chrome/101.0.4951.61 Mobile Safari/537.36"
)

# 本插件实际用到的：POST。
POST_ENDPOINTS = (
    "/go/auth/login_by_qq",
    "/go/account/get_main_role_raw",
    "/go/agame/career/record/list",
    "/go/agame/career/record/scoreboard",
)

# 本插件**不用**、但第三方实现依赖的。留在这里是为了知道它什么时候真的死。
# 注意它是 GET 接口：拿 POST 打会拿到 404，那是请求方式不匹配，不是端点废弃。
KNOWN_UNUSED = (("/go/agame/career/mode/filter", "GET"),)

EMPTY_COOKIE = (
    "clientType=9; uin=o0; appid=102061775; acctype=qc; openid=null; "
    "access_token=null; userId=0; accountType=5; tid=0"
)


async def probe(method: str, path: str) -> tuple[int, str]:
    headers = {
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Content-Type": "application/json",
        "User-Agent": _UA,
        "Cookie": EMPTY_COOKIE,
    }
    timeout_s = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout_s) as session:
        async with session.request(
            method, f"{BASE_URL}{path}?{COMMON_QUERY}", headers=headers, json={"_t": 0}
        ) as response:
            # 原文回传，只在打印时截断：先截断再 json.loads 会必然失败，
            # 然后被误判成「不是 JSON」。
            return response.status, await response.text(errors="ignore")


def judge(status: int, text: str) -> str:
    if status == 404:
        return "❌ 这个请求方式下没有（404）"
    if status != 200:
        return f"⚠️ HTTP {status}"
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return f"⚠️ 不是 JSON（{text[:60]!r}）"
    code = raw.get("result", -1)
    if code == 0:
        return "✅ 通了（居然不要登录）"
    if code == 999999:
        return "✅ 活着（要求登录，符合预期）"
    return f"✅ 活着（result={code}: {raw.get('errMsg') or raw.get('msg') or ''}）"


async def main(only_check: str | None = None) -> int:
    checks = [
        path for path in POST_ENDPOINTS if not only_check or path == only_check
    ] or POST_ENDPOINTS
    failures = 0
    for path in checks:
        try:
            status, text = await probe("POST", path)
        except Exception as exc:  # noqa: BLE001 - 探测脚本要把异常显示出来
            print(f"⚠️ {path} 连不上：{type(exc).__name__}: {exc}")
            failures += 1
            continue
        verdict = judge(status, text)
        print(f"{verdict}  POST {path}")
        if verdict.startswith(("❌", "⚠️")):
            failures += 1

    if only_check:
        return 1 if failures else 0

    for path, method in KNOWN_UNUSED:
        try:
            status, text = await probe(method, path)
        except Exception as exc:  # noqa: BLE001
            print(f"·  {method} {path} 连不上：{type(exc).__name__}")
            continue
        verdict = judge(status, text)
        print(f"·  {verdict}  {method} {path}（本插件不用，仅供参考）")

    return 1 if failures else 0


if __name__ == "__main__":
    parse = argparse.ArgumentParser(description="探测掌瓦接口是否还活着")
    parse.add_argument("--only", dest="only_check", help="只探某个路径")
    params = parse.parse_args()
    sys.exit(asyncio.run(main(params.only_check)))
