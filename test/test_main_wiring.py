"""`main.py` 的接线检查。

`main.py` 跑不了单测——它顶层 import astrbot，本地没这个包。所以下面这些
只能查源码。改 `main.py` 时这些检查要跟着一起过。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from aiva.fallback import (
    SUBCOMMANDS,
    is_bare_command,
    is_command_miss,
    leftover_arg,
    starts_with_group,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
MAIN_SOURCE = REPO_ROOT / "main.py"


def _tree() -> ast.Module:
    return ast.parse(MAIN_SOURCE.read_text(encoding="utf-8"))


def _main_class() -> ast.ClassDef:
    for node in _tree().body:
        if isinstance(node, ast.ClassDef) and node.name == "Main":
            return node
    raise AssertionError("main.py 里没有 Main 类")


def _formal_command_names() -> set[str]:
    """类体里 `@aiva_group.command("x")` 声明的正式指令名，不含别名。"""
    names: set[str] = set()
    for node in _main_class().body:
        for deco in node.decorator_list:
            if not isinstance(deco, ast.Call) or "command" not in ast.unparse(deco.func):
                continue
            if deco.args and isinstance(deco.args[0], ast.Constant):
                names.add(str(deco.args[0].value))
    return names


def _all_command_names() -> set[str]:
    """类体里 `@aiva_group.command("x", alias={...})` 声明的全部名字与别名。"""
    names = _formal_command_names()
    for node in _main_class().body:
        for deco in node.decorator_list:
            if not isinstance(deco, ast.Call) or "command" not in ast.unparse(deco.func):
                continue
            for keyword in deco.keywords:
                if keyword.arg == "alias" and isinstance(keyword.value, ast.Set):
                    names.update(str(e.value) for e in keyword.value.elts)
    return names


class TestGroupName:
    def test_group_name_is_aiva(self):
        constants = {
            assign.targets[0].id: assign.value.value
            for assign in _tree().body
            if isinstance(assign, ast.Assign)
            and isinstance(assign.targets[0], ast.Name)
            and isinstance(assign.value, ast.Constant)
        }
        assert constants.get("_GROUP") == "爱瓦"

    def test_fallback_filter_uses_same_group(self):
        text = MAIN_SOURCE.read_text(encoding="utf-8")
        assert "is_command_miss(text, _GROUP)" in text

    def test_fallback_filter_always_returns_true(self):
        """`CustomFilter.filter` 返回 False 会让整个指令组被跳过。

        AstrBot 的 `CommandGroupFilter.filter` 里是
        `if not self.custom_filter_ok(...): return False`——返回值的语义是
        「这条消息我管不管」，不是「命中没有」。所以兜底过滤器**所有出口
        都必须 return True**：命中就自己 set_result + stop_event，
        不属于我的消息要立刻放行。写成「命中返回 False」时 `/爱瓦 绑定`
        完全没反应。
        """
        method = _fallback_filter()
        returns = [
            node.value.value
            for node in ast.walk(method)
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant)
        ]
        assert returns and set(returns) == {True}, (
            f"兜底 filter 里出现了 {returns}，必须只return True"
        )
        # 命中分支必须 set_result + stop_event，否则消息会漏给模型
        assert "event.stop_event()" in ast.unparse(method)
        # 必须先判「关不关我事」再动手，否则会掐掉别的插件的指令
        assert "starts_with_group" in ast.unparse(method)


def _help_block() -> str:
    text = MAIN_SOURCE.read_text(encoding="utf-8")
    start = text.index('_HELP_TEXT = """') + len('_HELP_TEXT = """')
    return text[start : text.index('"""', start)]


def _fallback_filter() -> ast.FunctionDef:
    for node in _tree().body:
        if isinstance(node, ast.ClassDef) and node.name == "_MissFallbackFilter":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "filter":
                    return item
    raise AssertionError("main.py 里没有 _MissFallbackFilter.filter")


class TestFallbackSync:
    def test_every_command_in_fallback_list(self):
        # 落一个别名没同步，用户打那个别名就会被当成废话，
        # 拿到一句「不是子命令」。
        missing = _all_command_names() - SUBCOMMANDS
        assert not missing, f"这些指令/别名没写进 fallback.SUBCOMMANDS：{sorted(missing)}"

    def test_help_lists_every_command(self):
        """帮助漏一条，用户就只能靠猜或问。

        只查正式指令名：别名是给习惯写法兜底的，不必逐条列进帮助。
        """
        help_text = _help_block()
        missing = sorted(
            name for name in _formal_command_names() if f"/爱瓦 {name}" not in help_text
        )
        assert not missing, f"帮助文本里没列这些指令：{missing}"

    def test_help_says_pk_by_nickname(self):
        """QQ 官方机器人收不到 @别人，README 曾在这一点上写错。

        帮助里收成一行一句后不再解释原因，但写法必须还是「写昵称」。
        """
        assert "PK <群友昵称>" in _help_block()

    def test_help_first_line_has_product_and_game(self):
        assert _help_block().lstrip().startswith("爱瓦 · ")

    def test_title_states_data_source(self):
        """文案收成一行一句后，数据来源只留在标题里。"""
        assert "国服" in _help_block().splitlines()[0]

    def test_help_mentions_natural_language(self):
        assert "自然语言" in _help_block()


class TestFallbackScope:
    """兜底过滤器绝不能碰别人的消息。

    `CommandGroupFilter.filter` 先跑 `custom_filter_ok()`、之后才检查组名，
    所以挂在指令组上的过滤器对**每一条消息**都执行。原实现里
    `leftover_arg` 对不属于自己的消息原样返回，导致「爱农 状态」被当成
    「爱瓦的子命令落空」而`stop_event()` —— 爱农因此从能用变成完全没反应。
    """

    @pytest.mark.parametrize(
        "message",
        [
            "爱农 状态",
            "/爱农 绑定",
            "爱wm PK 76561198412825279",
            "帮助",
            "",
            "今天天气不错",
            "爱瓦",
        ],
    )
    def test_other_plugin_messages_not_miss(self, message: str):
        assert not is_command_miss(message, "爱瓦"), f"{message!r} 被误判成爱瓦的落空"

    @pytest.mark.parametrize("message", ["爱瓦 瞎写的", "/爱瓦 瞎写的"])
    def test_aiva_garbage_is_miss(self, message: str):
        assert is_command_miss(message, "爱瓦"), f"{message!r} 应该判成落空但没有"

    @pytest.mark.parametrize(
        "message",
        ["爱瓦 绑定", "/爱瓦 绑定", "爱瓦战绩", "爱瓦 PK", "爱瓦 help"],
    )
    def test_aiva_valid_command_not_miss(self, message: str):
        assert not is_command_miss(message, "爱瓦"), f"{message!r} 是正常指令，不该被判成落空"

    def test_leftover_arg_empty_for_foreign_message(self):
        # 返回原样文本是上一版 bug 的根源：调用方无从分辨「不归我管」
        assert leftover_arg("爱农 状态", "爱瓦") == ""
        assert leftover_arg("爱瓦 瞎写的", "爱瓦") == "瞎写的"

    def test_starts_with_group(self):
        assert starts_with_group("爱瓦 绑定", "爱瓦")
        assert starts_with_group("/爱瓦 绑定", "爱瓦")
        assert not starts_with_group("爱农 绑定", "爱瓦")
        assert not starts_with_group("", "爱瓦")


def _log_calls() -> list[str]:
    """把 main.py 里所有 `logger.xxx(...)` 的内容源码抠出来。"""
    calls: list[str] = []
    for node in ast.walk(_tree()):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and getattr(func.value, "id", "") == "logger":
            calls.extend(ast.unparse(arg) for arg in node.args)
    return calls


class TestNoCredentialLeak:
    def test_logs_have_no_credential_fields(self):
        # 换票时把 openid/access_token 传给 transport 是必需的，不算泄。
        # 泄的是把它写进 logger——日志会被到处收集、分发。
        for arg in _log_calls():
            for secret in ("access_token", "openid", "tid", "credential["):
                assert secret not in arg, f"日志带上了凭据字段：{arg}"

    def test_logs_never_dump_whole_credential(self):
        # 直接把 dict 丢进 f-string 会把四样全打印出来。
        for arg in _log_calls():
            assert not any(f"{{{name}}}" in arg for name in ("credential",))


class TestSourceHygiene:
    """两类只在运行时才炸、本地又跑不到的写法。

    都不是靠读代码能稳定发现的：漏 f 前缀的字符串**语法完全合法**，只有用户
    看到「「{关键词}」匹配到几个人」时才知道出事了；except 里读 try 体内才绑
    定的变量同样合法，只有异常真发生时才 `UnboundLocalError`——而那条分支走的
    是「对方登录态过期」，测试环境根本碰不到。
    """

    def test_no_missing_f_prefix(self):
        """字符串里出现 `{名字}` 形状却不是 f-string，就是漏了前缀。"""
        placeholder = re.compile(r"\{[^{}\n]{1,40}\}")
        leaks = []
        for node in ast.walk(_tree()):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            for hit in placeholder.finditer(node.value):
                leaks.append((node.lineno, hit.group(0)))
        assert not leaks, f"这些字符串里的占位符不会被替换（漏了 f 前缀）：{leaks}"

    def test_except_does_not_read_try_bound_vars(self):
        """`my_list` 那种：`fetch_matches` 抛异常时它还没绑上，except 里一读就炸。

        线上真出过：PK 时对方登录态过期，本该回一句「对方登录态失效」，
        实际抛 `UnboundLocalError`，用户只看到机器人没反应。
        """
        problems = []
        for func in _all_functions():
            for func_name, lineno, names in _unbound_reads(func):
                problems.append(f"{func_name} 第{lineno}行 except 里读了 {sorted(names)}")
        assert not problems, "\n".join(problems)


def _all_functions() -> list[ast.AsyncFunctionDef | ast.FunctionDef]:
    return [
        node
        for node in ast.walk(_tree())
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
    ]


def _bound_names(nodes: list[ast.AST]) -> set[str]:
    names: set[str] = set()
    for node in nodes:
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                names.add(child.id)
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(child.name)
            if isinstance(child, ast.ExceptHandler) and child.name:
                names.add(child.name)
    return names


def _unbound_reads(func: ast.AsyncFunctionDef | ast.FunctionDef) -> list[tuple[str, int, set]]:
    """找出 `except` 里读了「同一个 try 体内才绑定」的变量的地方。"""
    report: list[tuple[str, int, set]] = []
    bound = {arg.arg for arg in func.args.args}

    def scan(nodes: list[ast.stmt]) -> None:
        for node in nodes:
            if isinstance(node, ast.Try):
                body_names = _bound_names(node.body)
                for handler in node.handlers:
                    read = {
                        child.id
                        for child in ast.walk(handler)
                        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
                    }
                    suspect = (read & body_names) - bound
                    if suspect:
                        report.append((func.name, node.lineno, suspect))
                scan(node.body)
                for handler in node.handlers:
                    scan(handler.body)
                    bound.update(_bound_names(handler.body))
                scan(node.orelse)
                scan(node.finalbody)
                bound.update(body_names)
            elif hasattr(node, "body") and isinstance(node.body, list):
                bound.update(_bound_names([node]))
                scan(node.body)

    scan(func.body)
    return report


class TestImports:
    @pytest.mark.parametrize(
        "name",
        [
            "QrSession",
            "build_match_setup",
            "setup_text",
            "find_opponent",
            "opponent_from_raw",
            "opponent_from_mentions",
            "mention_summary",
            "pk_prompt",
            "match_prompt",
            "review_prompt",
            "advantage_count",
            "contrast_text",
            "contrast_points",
            "aggregate",
            "recent_text",
            "CredentialStore",
            "masked_id",
            "is_complete",
            "ApiError",
            "LoginExpired",
            "MvalClient",
            "leftover_arg",
            "is_command_miss",
            "miss_hint",
        ],
    )
    def test_symbols_appear_in_import_block(self, name: str):
        text = MAIN_SOURCE.read_text(encoding="utf-8")
        assert name in text


class TestLoggerSource:
    """插件规范：logger 必须且只能从 `astrbot.api` 取，不许用内置 `logging`。

    爱农上架审核第一次就是被这一条驳回的（`ainong/auth/` 下两个模块用了内置
    `logging`，只看 main.py 查不出来）。这里扫整个包，防止同样的写法溜进来。
    """

    def test_no_builtin_logging_in_package(self):
        offenders = [
            path.name
            for path in sorted(MAIN_SOURCE.parent.joinpath("aiva").rglob("*.py"))
            if re.search(r"^\s*(?:import logging|from logging)\b", path.read_text("utf-8"), re.M)
        ]
        assert not offenders, f"这些文件用了内置 logging，要改成从 astrbot.api 取：{offenders}"

    def test_logger_comes_from_astrbot(self):
        source = MAIN_SOURCE.read_text(encoding="utf-8")
        assert re.search(r"^from astrbot\.api import .*\blogger\b", source, re.M)


class TestMarketplaceRules:
    """市场审核的硬要求，驳回一次就要重提一个版本号，钉死。"""

    def test_data_dir_uses_plugin_name(self):
        """规则是 `data/plugin_data/<plugin_name>`，`<plugin_name>` 指目录名。"""
        source = MAIN_SOURCE.read_text(encoding="utf-8")
        assert "StarTools.get_data_dir(_PLUGIN_NAME)" in source
        assert '_PLUGIN_NAME = "astrbot_plugin_aiva"' in source
        assert '"plugin_data" / "aiva"' not in source

    def test_legacy_dir_migrated(self):
        """升级不能把已有绑定丢掉。"""
        source = MAIN_SOURCE.read_text(encoding="utf-8")
        assert "_migrate_legacy_credentials(data_dir)" in source
        assert '_LEGACY_DATA_DIR = "aiva"' in source

    def test_tls_verification_not_disabled(self):
        """请求里带 openid/access_token/tid，关掉校验等于交给中间人。"""
        transport = (REPO_ROOT / "aiva" / "transport.py").read_text(encoding="utf-8")
        assert "CERT_NONE" not in transport
        assert "check_hostname = False" not in transport
        assert "verify_mode" not in transport
        assert "ssl=" not in transport


class TestBareCommandHelp:
    """只发 `/爱瓦` 时回帮助，别甩框架的「参数不足」。

    2026-10-29 实测：`/爱瓦 @某人` 落到框架那层，回的是「参数不足」加全部
    子命令和各自的 docstring——一大段内部说明，用户还是不知道能发什么。
    """

    @pytest.mark.parametrize("message", ["/爱瓦", "爱瓦", "/爱瓦   ", "爱瓦 "])
    def test_bare_group_is_bare_command(self, message: str):
        assert is_bare_command(message, "爱瓦")

    @pytest.mark.parametrize(
        "message",
        ["/爱瓦 战绩", "爱瓦战绩", "/爱农 状态", "今天天气不错", ""],
    )
    def test_with_arg_or_foreign_not_bare(self, message: str):
        assert not is_bare_command(message, "爱瓦")

    def test_filter_returns_help_not_miss_hint(self):
        source = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
        block = source[source.index("class _MissFallbackFilter") :]
        block = block[: block.index("class Main(")]
        assert "is_bare_command(" in block
        assert "_HELP_TEXT" in block, "空指令要回帮助，不能只说「不是子命令」"
