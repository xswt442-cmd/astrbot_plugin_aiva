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

MAIN_PATH = Path(__file__).resolve().parent.parent / "main.py"
MAIN_SOURCE = MAIN_PATH.read_text(encoding="utf-8")
MAIN_TREE = ast.parse(MAIN_SOURCE)


def _bind_func() -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(MAIN_TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "bind":
            return node
    raise AssertionError("main.py 里找不到 bind handler")


def _const(name: str) -> float:
    for node in MAIN_TREE.body:
        if isinstance(node, ast.Assign) and node.targets[0].id == name:
            return node.value.value  # type: ignore[attr-defined]
    raise AssertionError(f"main.py 里找不到常量 {name}")


class TestBudgetByWallClock:
    def test_no_count_based_budget(self):
        """次数式预算是这次事故的根因，必须彻底消失。"""
        for retired in ("_轮询次数", "_轮询间隔"):
            assert retired not in MAIN_SOURCE, (
                f"{retired} 是次数式预算，服务器网络慢时会把二维码拖过期"
            )

    def test_interval_and_budget_are_floats(self):
        assert _const("_POLL_INTERVAL") == 2.0
        budget = _const("_POLL_BUDGET")
        assert isinstance(budget, float)

    def test_budget_within_qr_lifetime(self):
        """二维码约60 秒失效，预算必须留出余量，不能等它作废。"""
        budget = _const("_POLL_BUDGET")
        assert 30 <= budget <= 55, f"预算 {budget}s，二维码约60s 失效"

    def test_uses_monotonic_not_time_time(self):
        """monotonic 不受系统时间调整影响，这是掐表的正确时钟。"""
        bind = ast.get_source_segment(MAIN_SOURCE, _bind_func()) or ""
        assert "time.monotonic()" in bind
        assert "time.time()" not in bind


class TestPollLoopStructure:
    def test_while_loop_not_for_count(self):
        bind = _bind_func()
        loops = [n for n in ast.walk(bind) if isinstance(n, ast.While)]
        assert loops, "绑定 handler 里应该有 while 循环按时间推进"

    def test_no_for_else_leftover(self):
        """原来 for...else 结构已被 while + 显式收工取代。

        注意只能查 for 节点的orelse：except 分支里出现 else: 完全是合法的。
        """
        for node in ast.walk(_bind_func()):
            if isinstance(node, ast.For):
                assert node.orelse == [], "for...else 结构已废弃，预算必须靠墙钟而不是次数"

    def test_next_tick_advances_by_interval(self):
        """慢的请求不该把节奏拖长：从节奏点累加，不是 sleep(固定) 起步。"""
        bind = ast.get_source_segment(MAIN_SOURCE, _bind_func()) or ""
        assert "next_tick += _POLL_INTERVAL" in bind

    def test_checks_budget_before_sleep(self):
        """sleep 之前先看还剩多少预算，否则网络慢时会睡过头。"""
        bind = ast.get_source_segment(MAIN_SOURCE, _bind_func()) or ""
        sleep_at = bind.index("await asyncio.sleep")
        assert ">= deadline" in bind[:sleep_at], "补觉之前必须先检查预算是否已耗尽"

    def test_both_exits_tell_user_to_retry(self):
        bind = ast.get_source_segment(MAIN_SOURCE, _bind_func()) or ""
        assert bind.count("重新发 /爱瓦 绑定") >= 2, "两条失败出口都要给出重试指引"


class TestDistinguishQrFailure:
    def test_expired_qr_says_expired(self):
        bind = ast.get_source_segment(MAIN_SOURCE, _bind_func()) or ""
        assert "码已过期" in bind

    def test_expired_and_parse_failure_differ(self):
        """「码过期」是我们慢，「解析失败」是我们的 bug，文案不能混。
        混了就会让用户反复扫码，而真正要修的是解析。"""
        bind = ast.get_source_segment(MAIN_SOURCE, _bind_func()) or ""
        assert '"过期" in str(exc)' in bind, "两种失败必须靠判断分叉，不能共用一句文案"
        assert "码已过期，重新发 /爱瓦 绑定。" in bind

    def test_parse_failure_logs_trace(self):
        """下一次能不能定位，全看轨迹有没有记全。"""
        bind = ast.get_source_segment(MAIN_SOURCE, _bind_func()) or ""
        assert "trace" in bind

    def test_each_round_logs_raw_response(self):
        bind = ast.get_source_segment(MAIN_SOURCE, _bind_func()) or ""
        assert "last_raw" in bind, "轮询要留原始返回，排查时能直接看清腾讯说了什么"

    def test_log_records_round_number(self):
        bind = ast.get_source_segment(MAIN_SOURCE, _bind_func()) or ""
        assert "rounds" in bind


class TestPtloginKeepsRawResponse:
    def test_session_saves_raw_response(self):
        source = (
            Path(__file__).resolve().parent.parent / "aiva" / "auth" / "ptlogin.py"
        ).read_text(encoding="utf-8")
        assert "last_raw" in source

    def test_code_zero_without_credentials_raises(self):
        """code=0 是腾讯说「扫了并确认了」。这时静默返回 None 会让
        用户只看到「超时了」，把我们的解析 bug 误报成用户没扫。"""
        source = (
            Path(__file__).resolve().parent.parent / "aiva" / "auth" / "ptlogin.py"
        ).read_text(encoding="utf-8")
        assert "腾讯已确认扫码" in source
