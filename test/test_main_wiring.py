"""`main.py` 的接线检查。

`main.py` 跑不了单测——它顶层 import astrbot，本地没这个包。所以下面这些
只能查源码。改 `main.py` 时这些检查要跟着一起过。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from aiva.兜底 import 以组名开头, 取落空参数, 子命令名, 是否落空

源码目录 = Path(__file__).resolve().parent.parent
main源 = 源码目录 / "main.py"


def _树() -> ast.Module:
    return ast.parse(main源.read_text(encoding="utf-8"))


def _类() -> ast.ClassDef:
    for 节点 in _树().body:
        if isinstance(节点, ast.ClassDef) and 节点.name == "Main":
            return 节点
    raise AssertionError("main.py 里没有 Main 类")


def _正式指令名() -> set[str]:
    """类体里 `@爱瓦.command("x")` 声明的正式指令名，不含别名。"""
    出: set[str] = set()
    for 节点 in _类().body:
        for 装饰 in 节点.decorator_list:
            if not isinstance(装饰, ast.Call) or "command" not in ast.unparse(装饰.func):
                continue
            if 装饰.args and isinstance(装饰.args[0], ast.Constant):
                出.add(str(装饰.args[0].value))
    return 出


def _指令名() -> set[str]:
    """类体里 `@爱瓦.command("x", alias={...})` 声明的全部名字与别名。"""
    出 = _正式指令名()
    for 节点 in _类().body:
        for 装饰 in 节点.decorator_list:
            if not isinstance(装饰, ast.Call) or "command" not in ast.unparse(装饰.func):
                continue
            for 关键字 in 装饰.keywords:
                if 关键字.arg == "alias" and isinstance(关键字.value, ast.Set):
                    出.update(str(e.value) for e in 关键字.value.elts)
    return 出


class Test组名:
    def test_组名是爱瓦(self):
        常量 = {
            目标.targets[0].id: 目标.value.value
            for 目标 in _树().body
            if isinstance(目标, ast.Assign)
            and isinstance(目标.targets[0], ast.Name)
            and isinstance(目标.value, ast.Constant)
        }
        assert 常量.get("_组名") == "爱瓦"

    def test_兜底过滤器用同一个组名(self):
        文本 = main源.read_text(encoding="utf-8")
        assert "是否落空(文本, _组名)" in 文本

    def test_兜底过滤器恒返回True(self):
        """`CustomFilter.filter` 返回 False 会让整个指令组被跳过。

        AstrBot 的 `CommandGroupFilter.filter` 里是
        `if not self.custom_filter_ok(...): return False`——返回值的语义是
        「这条消息我管不管」，不是「命中没有」。所以兜底过滤器**所有出口
        都必须 return True**：命中就自己 set_result + stop_event，
        不属于我的消息要立刻放行。写成「命中返回 False」时 `/爱瓦 绑定`
        完全没反应。
        """
        方法 = _兜底过滤器()
        返回 = [
            节点.value.value
            for 节点 in ast.walk(方法)
            if isinstance(节点, ast.Return) and isinstance(节点.value, ast.Constant)
        ]
        assert 返回 and set(返回) == {True}, f"兜底 filter 里出现了 {返回}，必须只return True"
        # 命中分支必须 set_result + stop_event，否则消息会漏给模型
        assert "event.stop_event()" in ast.unparse(方法)
        # 必须先判「关不关我事」再动手，否则会掐掉别的插件的指令
        assert "以组名开头" in ast.unparse(方法)


def _帮助块() -> str:
    文本 = main源.read_text(encoding="utf-8")
    起点 = 文本.index('_帮助文本 = """') + len('_帮助文本 = """')
    return 文本[起点 : 文本.index('"""', 起点)]


def _兜底过滤器() -> ast.FunctionDef:
    for 节点 in _树().body:
        if isinstance(节点, ast.ClassDef) and 节点.name == "_落空兜底":
            for 体 in 节点.body:
                if isinstance(体, ast.FunctionDef) and 体.name == "filter":
                    return 体
    raise AssertionError("main.py 里没有 _落空兜底.filter")


class Test兜底同步:
    def test_每个指令都在兜底名单里(self):
        # 落一个别名没同步，用户打那个别名就会被当成废话，
        # 拿到一句「不是子命令」。
        缺 = _指令名() - 子命令名
        assert not 缺, f"这些指令/别名没写进 兜底.子命令名：{sorted(缺)}"

    def test_帮助里列了每个指令(self):
        """帮助漏一条，用户就只能靠猜或问。

        只查正式指令名：别名是给习惯写法兜底的，不必逐条列进帮助。
        """
        缺 = sorted(名 for 名 in _正式指令名() if f"/爱瓦 {名}" not in _帮助块())
        assert not 缺, f"帮助文本里没列这些指令：{缺}"

    def test_帮助说明_PK_写昵称而不是艾特群友(self):
        """QQ 官方机器人收不到 @别人，README 曾在这一点上写错。

        帮助里收成一行一句后不再解释原因，但写法必须还是「写昵称」。
        """
        assert "PK <群友昵称>" in _帮助块()

    def test_帮助首行是产品名加游戏名(self):
        assert _帮助块().lstrip().startswith("爱瓦 · ")

    def test_标题写明数据来自哪里(self):
        """文案收成一行一句后，数据来源只留在标题里。"""
        assert "国服" in _帮助块().splitlines()[0]

    def test_说明自然语言也能用(self):
        assert "自然语言" in _帮助块()


class Test兜底不越界:
    """兜底过滤器绝不能碰别人的消息。

    `CommandGroupFilter.filter` 先跑 `custom_filter_ok()`、之后才检查组名，
    所以挂在指令组上的过滤器对**每一条消息**都执行。原实现里
    `取落空参数` 对不属于自己的消息原样返回，导致「爱农 状态」被当成
    「爱瓦的子命令落空」而`stop_event()` —— 爱农因此从能用变成完全没反应。
    """

    @pytest.mark.parametrize(
        "消息",
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
    def test_别人的消息不判定为落空(self, 消息: str):
        assert not 是否落空(消息, "爱瓦"), f"{消息!r} 被误判成爱瓦的落空"

    @pytest.mark.parametrize("消息", ["爱瓦 瞎写的", "/爱瓦 瞎写的"])
    def test_爱瓦的瞎写才算落空(self, 消息: str):
        assert 是否落空(消息, "爱瓦"), f"{消息!r} 应该判成落空但没有"

    @pytest.mark.parametrize(
        "消息",
        ["爱瓦 绑定", "/爱瓦 绑定", "爱瓦战绩", "爱瓦 PK", "爱瓦 help"],
    )
    def test_爱瓦的正常指令不算落空(self, 消息: str):
        assert not 是否落空(消息, "爱瓦"), f"{消息!r} 是正常指令，不该被判成落空"

    def test_取落空参数对陌生消息返回空(self):
        # 返回原样文本是上一版 bug 的根源：调用方无从分辨「不归我管」
        assert 取落空参数("爱农 状态", "爱瓦") == ""
        assert 取落空参数("爱瓦 瞎写的", "爱瓦") == "瞎写的"

    def test_以组名开头(self):
        assert 以组名开头("爱瓦 绑定", "爱瓦")
        assert 以组名开头("/爱瓦 绑定", "爱瓦")
        assert not 以组名开头("爱农 绑定", "爱瓦")
        assert not 以组名开头("", "爱瓦")


def _日志调用() -> list[str]:
    """把 main.py 里所有 `logger.xxx(...)` 的内容源码抠出来。"""
    出: list[str] = []
    for 节点 in ast.walk(_树()):
        if not isinstance(节点, ast.Call):
            continue
        函数 = 节点.func
        if isinstance(函数, ast.Attribute) and getattr(函数.value, "id", "") == "logger":
            出.extend(ast.unparse(参数) for 参数 in 节点.args)
    return 出


class Test凭据不外泄:
    def test_日志里不带凭据字段(self):
        # 换票时把 openid/access_token 传给 transport 是必需的，不算泄。
        # 泄的是把它写进 logger——日志会被到处收集、分发。
        for 参数 in _日志调用():
            for 敏感 in ("access_token", "openid", "tid", "凭据["):
                assert 敏感 not in 参数, f"日志带上了凭据字段：{参数}"

    def test_日志里不会出现整个凭据对象(self):
        # 直接把 dict 丢进 f-string 会把四样全打印出来。
        for 参数 in _日志调用():
            assert not any(f"{{{名}}}" in 参数 for 名 in ("凭据",))


class Test源码卫生:
    """两类只在运行时才炸、本地又跑不到的写法。

    都不是靠读代码能稳定发现的：漏 f 前缀的字符串**语法完全合法**，只有用户
    看到「「{关键词}」匹配到几个人」时才知道出事了；except 里读 try 体内才绑
    定的变量同样合法，只有异常真发生时才 `UnboundLocalError`——而那条分支走的
    是「对方登录态过期」，测试环境根本碰不到。
    """

    def test_没有漏f前缀的字符串(self):
        """字符串里出现 `{名字}` 形状却不是 f-string，就是漏了前缀。"""
        占位 = re.compile(r"\{[^{}\n]{1,40}\}")
        漏 = []
        for 节点 in ast.walk(_树()):
            if not isinstance(节点, ast.Constant) or not isinstance(节点.value, str):
                continue
            for 命中 in 占位.finditer(节点.value):
                漏.append((节点.lineno, 命中.group(0)))
        assert not 漏, f"这些字符串里的占位符不会被替换（漏了 f 前缀）：{漏}"

    def test_except里不读try体内才绑定的变量(self):
        """`甲列表` 那种：`取对局` 抛异常时它还没绑上，except 里一读就炸。

        线上真出过：PK 时对方登录态过期，本该回一句「对方登录态失效」，
        实际抛 `UnboundLocalError`，用户只看到机器人没反应。
        """
        问题 = []
        for 函数 in _顶层函数():
            for 函数名, 行号, 名字 in _未绑定读取(函数):
                问题.append(f"{函数名} 第{行号}行 except 里读了 {sorted(名字)}")
        assert not 问题, "\n".join(问题)


def _顶层函数() -> list[ast.AsyncFunctionDef | ast.FunctionDef]:
    return [
        节点
        for 节点 in ast.walk(_树())
        if isinstance(节点, (ast.AsyncFunctionDef, ast.FunctionDef))
    ]


def _绑定的(节点们: list[ast.AST]) -> set[str]:
    出: set[str] = set()
    for 节点 in 节点们:
        for 子 in ast.walk(节点):
            if isinstance(子, ast.Name) and isinstance(子.ctx, ast.Store):
                出.add(子.id)
            if isinstance(子, (ast.FunctionDef, ast.AsyncFunctionDef)):
                出.add(子.name)
            if isinstance(子, ast.ExceptHandler) and 子.name:
                出.add(子.name)
    return 出


def _未绑定读取(函数: ast.AsyncFunctionDef | ast.FunctionDef) -> list[tuple[str, int, set]]:
    """找出 `except` 里读了「同一个 try 体内才绑定」的变量的地方。"""
    报告: list[tuple[str, int, set]] = []
    已绑 = {参数.arg for 参数 in 函数.args.args}

    def 扫(节点们: list[ast.stmt]) -> None:
        for 节点 in 节点们:
            if isinstance(节点, ast.Try):
                体内 = _绑定的(节点.body)
                for 处理 in 节点.handlers:
                    读到 = {
                        子.id
                        for 子 in ast.walk(处理)
                        if isinstance(子, ast.Name) and isinstance(子.ctx, ast.Load)
                    }
                    可疑 = (读到 & 体内) - 已绑
                    if 可疑:
                        报告.append((函数.name, 节点.lineno, 可疑))
                扫(节点.body)
                for 处理 in 节点.handlers:
                    扫(处理.body)
                    已绑.update(_绑定的(处理.body))
                扫(节点.orelse)
                扫(节点.finalbody)
                已绑.update(体内)
            elif hasattr(节点, "body") and isinstance(节点.body, list):
                已绑.update(_绑定的([节点]))
                扫(节点.body)

    扫(函数.body)
    return 报告


class Test导入齐全:
    @pytest.mark.parametrize(
        "名字",
        [
            "扫码会话",
            "开局设定",
            "设定文本",
            "找对手",
            "PK提示",
            "开局提示",
            "锐评提示",
            "占优项数",
            "对比文本",
            "对比要点",
            "聚合",
            "近况文本",
            "凭据仓库",
            "凭据摘要",
            "凭据是否齐全",
            "业务错误",
            "登录过期",
            "掌瓦传输",
            "取落空参数",
            "是否落空",
            "落空提示",
        ],
    )
    def test_用到的符号在导入段出现过(self, 名字: str):
        文本 = main源.read_text(encoding="utf-8")
        assert 名字 in 文本


class Test日志出口:
    """插件规范：logger 必须且只能从 `astrbot.api` 取，不许用内置 `logging`。

    爱农上架审核第一次就是被这一条驳回的（`ainong/auth/` 下两个模块用了内置
    `logging`，只看 main.py 查不出来）。这里扫整个包，防止同样的写法溜进来。
    """

    def test_包内不用内置logging(self):
        违规 = [
            路径.name
            for 路径 in sorted(main源.parent.joinpath("aiva").rglob("*.py"))
            if re.search(r"^\s*(?:import logging|from logging)\b", 路径.read_text("utf-8"), re.M)
        ]
        assert not 违规, f"这些文件用了内置 logging，要改成从 astrbot.api 取：{违规}"

    def test_logger_从_astrbot_取(self):
        源码 = main源.read_text(encoding="utf-8")
        assert re.search(r"^from astrbot\.api import .*\blogger\b", 源码, re.M)


class Test审核合规:
    """市场审核的硬要求，驳回一次就要重提一个版本号，钉死。"""

    def test_数据目录用插件名(self):
        """规则是 `data/plugin_data/<plugin_name>`，`<plugin_name>` 指目录名。"""
        源码 = main源.read_text(encoding="utf-8")
        assert "StarTools.get_data_dir(_插件名)" in 源码
        assert '_插件名 = "astrbot_plugin_aiva"' in 源码
        assert '"plugin_data" / "aiva"' not in 源码

    def test_旧目录会被迁移(self):
        """升级不能把已有绑定丢掉。"""
        源码 = main源.read_text(encoding="utf-8")
        assert "_迁移旧凭据(目录)" in 源码
        assert '_旧数据目录名 = "aiva"' in 源码

    def test_不关_TLS_校验(self):
        """请求里带 openid/access_token/tid，关掉校验等于交给中间人。"""
        传输 = (源码目录 / "aiva" / "transport.py").read_text(encoding="utf-8")
        assert "CERT_NONE" not in 传输
        assert "check_hostname = False" not in 传输
        assert "verify_mode" not in 传输
        assert "ssl=" not in 传输


class Test空指令给帮助:
    """只发 `/爱瓦` 时回帮助，别甩框架的「参数不足」。

    2026-10-29 实测：`/爱瓦 @某人` 落到框架那层，回的是「参数不足」加全部
    子命令和各自的 docstring——一大段内部说明，用户还是不知道能发什么。
    """

    @pytest.mark.parametrize("消息", ["/爱瓦", "爱瓦", "/爱瓦   ", "爱瓦 "])
    def test_只发组名算空指令(self, 消息: str):
        from aiva.兜底 import 是否空指令

        assert 是否空指令(消息, "爱瓦")

    @pytest.mark.parametrize(
        "消息",
        ["/爱瓦 战绩", "爱瓦战绩", "/爱农 状态", "今天天气不错", ""],
    )
    def test_带参数或别人的消息不算(self, 消息: str):
        from aiva.兜底 import 是否空指令

        assert not 是否空指令(消息, "爱瓦")

    def test_过滤器回帮助而不是落空提示(self):
        源 = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
        体 = 源[源.index("class _落空兜底") :]
        体 = 体[: 体.index("class Main(")]
        assert "是否空指令(" in 体
        assert "_帮助文本" in 体, "空指令要回帮助，不能只说「不是子命令」"
