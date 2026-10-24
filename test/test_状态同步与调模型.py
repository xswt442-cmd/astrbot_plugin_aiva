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

主文件 = Path(__file__).resolve().parent.parent / "main.py"
源码 = 主文件.read_text(encoding="utf-8")
树 = ast.parse(源码)


def _函数(名: str) -> ast.FunctionDef:
    for 节点 in ast.walk(树):
        if isinstance(节点, (ast.FunctionDef, ast.AsyncFunctionDef)) and 节点.name == 名:
            return 节点
    raise AssertionError(f"main.py 里找不到函数 {名}")


def _源码片段(节点: ast.AST) -> str:
    return ast.get_source_segment(源码, 节点) or ""


def _函数体内(名: str) -> str:
    """函数体源码，**去掉 docstring 本身**。

    docstring 里会正常提到 `tool_loop_agent`（解释为什么不用它），
    按字面 grep 会把注释当成代码，测试就假通过了。但只剔 docstring——
    其余字符串（报错文案）还得留着，那也是断言的一部分。
    """
    节点 = _函数(名)
    文档 = ast.get_docstring(节点, clean=False)
    片段 = []
    for 子 in ast.walk(节点):
        if isinstance(子, ast.Expr) and isinstance(子.value, ast.Constant):
            continue  # 裸字符串表达式 = docstring 或无意义语句
        文本 = _源码片段(子)
        if not 文本:
            continue
        if 文档 and 文档 in 文本:
            文本 = 文本.replace(文档, "")
        片段.append(文本)
    return "\n".join(片段)


class Test状态自查:
    def test_状态必须调战绩接口(self):
        体 = _函数体内("状态")
        assert "取对局记账(" in 体, (
            "状态指令只查 get_main_role_raw，那个接口不校验登录态，"
            "会给假绿灯。必须再打一次 record/list（走记账版，顺带记下失效）。"
        )

    def test_状态先看失效记录(self):
        """刚失效过就别打接口碰运气，那会给出一个随时翻车的「绑定正常」。"""
        体 = _函数体内("状态")
        assert "刚失效过(" in 体

    def test_状态不能只报绑定正常(self):
        体 = _函数体内("状态")
        # 取对局失败时必须走单独分支，不能落回「绑定正常」那句。
        assert "战绩可用" in 体, "战绩自查结果必须参与文案，不能被忽略"

    def test_状态要报绑定时长(self):
        体 = _函数体内("状态")
        assert "已绑多久(" in 体, "状态应显示已绑定多久"


class Test调模型:
    def test_不许用工具循环(self):
        体 = _函数体内("_问模型")
        assert "tool_loop_agent" not in 体, (
            "锐评/开局/PK 都不需要工具。tool_loop_agent 会塞全量工具 schema，"
            "线上实测锐评卡死 90 秒以上，直连 provider 只要 12 秒。"
        )

    def test_必须直连provider(self):
        体 = _函数体内("_问模型")
        assert "provider_manager.get_provider_by_id" in 体
        assert ".text_chat(" in 体

    def test_必须设超时(self):
        体 = _函数体内("_问模型")
        assert "asyncio.wait_for(" in 体
        assert "_模型超时" in 体

    def test_超时后必须换新请求重试(self):
        """23:52 的 PK 实测：上游偶发挂起，同提示直连 17 秒能过，
        挂起的那条连接等满 90 秒也不回。挂起的连接不会自己恢复，
        唯一有效动作是重开一条。"""
        体 = _函数体内("_问模型")
        assert 体.count("asyncio.wait_for(") >= 2, (
            "超时后必须用新请求重试一次，不能只失败一次就放弃"
        )
        assert "重试" in 体

    def test_必须压低重试次数(self):
        体 = _函数体内("_问模型")
        assert "request_max_retries=1" in 体, (
            "agent_runner 默认 request_max_retries=5，叠满轻松超过两分钟。"
        )

    def test_空返回要报错(self):
        体 = _函数体内("_问模型")
        assert "返回了空内容" in 体, "模型返回空内容不能当成正常结果发出去"

    def test_provider取不到要报错(self):
        体 = _函数体内("_问模型")
        assert "不存在或没启用" in 体


class Test绑定时间戳:
    def test_绑定时要盖时间戳(self):
        """两条绑定路径都走 `_落库并回话`，时间戳盖在那里。"""
        体 = _函数体内("_落库并回话")
        assert "记绑定时间(" in 体

    def test_两条绑定路径都落库(self):
        源码 = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
        assert 源码.count("_落库并回话(") >= 3, "绑定与绑定微信都要落库"

    def test_旧数据没有时间戳也要能读(self):
        from aiva.storage import 已绑多久, 记绑定时间

        assert 已绑多久({"openid": "x"}) is None
        带戳 = 记绑定时间({"openid": "x"})
        多大 = 已绑多久(带戳)
        assert 多大 is not None and 0 <= 多大 < 5

    def test_提醒阈值(self):
        import time

        from aiva.storage import 记绑定时间, 该提醒重绑

        assert not 该提醒重绑(记绑定时间({}))
        老 = {"绑定于": int(time.time()) - 13 * 3600}
        assert 该提醒重绑(老)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
