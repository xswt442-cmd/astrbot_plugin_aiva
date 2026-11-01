"""锁死两件事：

1. **状态指令必须真的打一次战绩接口。**
   只查 `get_main_role_raw` 会给出假绿灯——那个接口只要 Cookie 形状对就回
   `result=0`，而真正需要登录态的 `record/list` 早就 1003 了。用户被告知
   「绑定正常」，转头发锐评却被告知「登录态失效」，两个都是真话。

2. **调模型不许再走 `tool_loop_agent`。**
   它会把当前会话挂着的全部工具 schema 塞进请求（这台机器十几个插件），
   锐评实测稳定卡死 90 秒以上；直连 provider 只要 12 秒。锐评/开局/PK 都
   不需要工具。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

MAIN_FILE = Path(__file__).resolve().parent.parent / "main.py"
SOURCE = MAIN_FILE.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def _func(name: str) -> ast.FunctionDef:
    for node in ast.walk(TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"main.py 里找不到函数 {name}")


def _segment(node: ast.AST) -> str:
    return ast.get_source_segment(SOURCE, node) or ""


def _body(name: str) -> str:
    """函数体源码，**去掉 docstring 本身**。

    docstring 里会正常提到 `tool_loop_agent`（解释为什么不用它），
    按字面 grep 会把注释当成代码，测试就假通过了。但只剔 docstring——
    其余字符串（报错文案）还得留着，那也是断言的一部分。
    """
    node = _func(name)
    doc = ast.get_docstring(node, clean=False)
    segments = []
    for child in ast.walk(node):
        if isinstance(child, ast.Expr) and isinstance(child.value, ast.Constant):
            continue  # 裸字符串表达式 = docstring 或无意义语句
        text = _segment(child)
        if not text:
            continue
        if doc and doc in text:
            text = text.replace(doc, "")
        segments.append(text)
    return "\n".join(segments)


class TestStatusSelfCheck:
    def test_status_must_hit_match_api(self):
        body = _body("status_cmd")
        assert "_fetch_tracked(" in body, (
            "状态指令只查 get_main_role_raw，那个接口不校验登录态，"
            "会给假绿灯。必须再打一次 record/list（走记账版，顺带记下失效）。"
        )

    def test_status_checks_expiry_record_first(self):
        """刚失效过就别打接口碰运气，那会给出一个随时翻车的「绑定正常」。"""
        body = _body("status_cmd")
        assert "recently_expired(" in body

    def test_status_cannot_only_report_ok(self):
        body = _body("status_cmd")
        # 取对局失败时必须走单独分支，不能落回「绑定正常」那句。
        assert "matches_ok" in body, "战绩自查结果必须参与文案，不能被忽略"

    def test_status_reports_bind_duration(self):
        body = _body("status_cmd")
        assert "seconds_since_bind(" in body, "状态应显示已绑定多久"


class TestAskModel:
    def test_no_tool_loop(self):
        body = _body("_ask_model")
        assert "tool_loop_agent" not in body, (
            "锐评/开局/PK 都不需要工具。tool_loop_agent 会塞全量工具 schema，"
            "线上实测锐评卡死 90 秒以上，直连 provider 只要 12 秒。"
        )

    def test_direct_provider(self):
        body = _body("_ask_model")
        assert "provider_manager.get_provider_by_id" in body
        assert ".text_chat(" in body

    def test_sets_timeout(self):
        body = _body("_ask_model")
        assert "asyncio.wait_for(" in body
        assert "_model_timeout" in body

    def test_retries_with_new_request(self):
        """23:52 的 PK 实测：上游偶发挂起，同提示直连 17 秒能过，
        挂起的那条连接等满 90 秒也不回。挂起的连接不会自己恢复，
        唯一有效动作是重开一条。"""
        body = _body("_ask_model")
        assert body.count("asyncio.wait_for(") >= 2, (
            "超时后必须用新请求重试一次，不能只失败一次就放弃"
        )
        assert "重试" in body

    def test_lowers_retry_count(self):
        body = _body("_ask_model")
        assert "request_max_retries=1" in body, (
            "agent_runner 默认 request_max_retries=5，叠满轻松超过两分钟。"
        )

    def test_empty_result_raises(self):
        body = _body("_ask_model")
        assert "返回了空内容" in body, "模型返回空内容不能当成正常结果发出去"

    def test_missing_provider_raises(self):
        body = _body("_ask_model")
        assert "不存在或没启用" in body


class TestBindTimestamp:
    def test_stamp_on_bind(self):
        """两条绑定路径都走 `_save_and_reply`，时间戳盖在那里。"""
        body = _body("_save_and_reply")
        assert "stamp_bound_at(" in body

    def test_both_bind_paths_persist(self):
        source = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
        assert source.count("_save_and_reply(") >= 3, "绑定与绑定微信都要落库"

    def test_old_data_without_stamp_readable(self):
        from aiva.storage import seconds_since_bind, stamp_bound_at

        assert seconds_since_bind({"openid": "x"}) is None
        stamped = stamp_bound_at({"openid": "x"})
        seconds = seconds_since_bind(stamped)
        assert seconds is not None and 0 <= seconds < 5

    def test_remind_threshold(self):
        import time

        from aiva.storage import should_rebind, stamp_bound_at

        assert not should_rebind(stamp_bound_at({}))
        old = {"绑定于": int(time.time()) - 13 * 3600}
        assert should_rebind(old)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
