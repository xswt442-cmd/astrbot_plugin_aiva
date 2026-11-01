"""扫码轮询必须按墙钟掐表，不能按次数。

线上事故（2026-10-13 17:31）：用户扫码后被告知「二维码已失效」。
日志显示从出码到收工整整 127 秒。原因是原来的写法
`for _ in range(25): await sleep(2); await 轮询一次()`
——服务器到腾讯这一跳实测要 ~3 秒，每轮实际占 5 秒，
25轮 = 125 秒。二维码寿命约 60 秒且从出码那刻起算，
等问到第 20 轮时腾讯早把码作废了。用户扫了没用，不是他的问题。

这些测试不 import `main.py`（顶层 import astrbot，本地跑不了），
只对源码做 AST 断言，把这个语义钉死。
"""

from __future__ import annotations

import ast
from pathlib import Path

主源码路径 = Path(__file__).resolve().parent.parent / "main.py"
主源码 = 主源码路径.read_text(encoding="utf-8")
主树 = ast.parse(主源码)


def _绑定函数() -> ast.FunctionDef | ast.AsyncFunctionDef:
    for 节点 in ast.walk(主树):
        if isinstance(节点, (ast.FunctionDef, ast.AsyncFunctionDef)) and 节点.name == "绑定":
            return 节点
    raise AssertionError("main.py 里找不到 绑定 handler")


def _常量(名: str) -> float:
    for 节点 in 主树.body:
        if isinstance(节点, ast.Assign) and 节点.targets[0].id == 名:
            return 节点.value.value  # type: ignore[attr-defined]
    raise AssertionError(f"main.py 里找不到常量 {名}")


class Test预算按墙钟不按次数:
    def test_不再有次数式预算(self):
        """次数式预算是这次事故的根因，必须彻底消失。"""
        for 已废 in ("_轮询次数", "_轮询间隔"):
            assert 已废 not in 主源码, f"{已废} 是次数式预算，服务器网络慢时会把二维码拖过期"

    def test_节奏与预算是浮点常量(self):
        assert _常量("_轮询节奏") == 2.0
        预算 = _常量("_轮询预算")
        assert isinstance(预算, float)

    def test_预算在二维码寿命之内(self):
        """二维码约60 秒失效，预算必须留出余量，不能等它作废。"""
        预算 = _常量("_轮询预算")
        assert 30 <= 预算 <= 55, f"预算 {预算}s，二维码约60s 失效"

    def test_用monotonic_而不是time_time(self):
        """monotonic 不受系统时间调整影响，这是掐表的正确时钟。"""
        绑定 = ast.get_source_segment(主源码, _绑定函数()) or ""
        assert "time.monotonic()" in 绑定
        assert "time.time()" not in 绑定


class Test轮询循环结构:
    def test_是while循环不是for次数(self):
        绑定 = _绑定函数()
        循环 = [n for n in ast.walk(绑定) if isinstance(n, ast.While)]
        assert 循环, "绑定 handler 里应该有 while 循环按时间推进"

    def test_没有for_else残留(self):
        """原来 for...else 结构已被 while + 显式收工取代。

        注意只能查 for 节点的orelse：except 分支里出现 else: 完全是合法的。
        """
        for 节点 in ast.walk(_绑定函数()):
            if isinstance(节点, ast.For):
                assert 节点.orelse == [], "for...else 结构已废弃，预算必须靠墙钟而不是次数"

    def test_下一拍按节奏累加(self):
        """慢的请求不该把节奏拖长：从节奏点累加，不是 sleep(固定) 起步。"""
        绑定 = ast.get_source_segment(主源码, _绑定函数()) or ""
        assert "下一拍 += _轮询节奏" in 绑定

    def test_补觉前先查预算(self):
        """sleep 之前先看还剩多少预算，否则网络慢时会睡过头。"""
        绑定 = ast.get_source_segment(主源码, _绑定函数()) or ""
        睡 = 绑定.index("await asyncio.sleep")
        assert ">= 截止" in 绑定[:睡], "补觉之前必须先检查预算是否已耗尽"

    def test_耗尽预算与腾讯判废都告知用户(self):
        绑定 = ast.get_source_segment(主源码, _绑定函数()) or ""
        assert 绑定.count("重新发 /爱瓦 绑定") >= 2, "两条失败出口都要给出重试指引"


class Test区分不出码原因:
    def test_码过期要说是码过期了(self):
        绑定 = ast.get_source_segment(主源码, _绑定函数()) or ""
        assert "码已过期" in 绑定

    def test_过期与解析失败分开说(self):
        """「码过期」是我们慢，「解析失败」是我们的 bug，文案不能混。
        混了就会让用户反复扫码，而真正要修的是解析。"""
        绑定 = ast.get_source_segment(主源码, _绑定函数()) or ""
        assert '"过期" in str(exc)' in 绑定, "两种失败必须靠判断分叉，不能共用一句文案"
        assert "码已过期，重新发 /爱瓦 绑定。" in 绑定

    def test_解析失败时把轨迹打进日志(self):
        """下一次能不能定位，全看轨迹有没有记全。"""
        绑定 = ast.get_source_segment(主源码, _绑定函数()) or ""
        assert "trace" in 绑定

    def test_每轮记腾讯原始返回(self):
        绑定 = ast.get_source_segment(主源码, _绑定函数()) or ""
        assert "last_raw" in 绑定, "轮询要留原始返回，排查时能直接看清腾讯说了什么"

    def test_日志记第几轮(self):
        绑定 = ast.get_source_segment(主源码, _绑定函数()) or ""
        assert "问了几轮" in 绑定


class Testptlogin留原始响应:
    def test_会话保存原始响应(self):
        源码 = (Path(__file__).resolve().parent.parent / "aiva" / "auth" / "ptlogin.py").read_text(
            encoding="utf-8"
        )
        assert "last_raw" in 源码

    def test_代码0但没凭据时抛错而非静默返回none(self):
        """code=0 是腾讯说「扫了并确认了」。这时静默返回 None 会让
        用户只看到「超时了」，把我们的解析 bug 误报成用户没扫。"""
        源码 = (Path(__file__).resolve().parent.parent / "aiva" / "auth" / "ptlogin.py").read_text(
            encoding="utf-8"
        )
        assert "腾讯已确认扫码" in 源码
