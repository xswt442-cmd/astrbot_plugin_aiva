"""指令落空时的兜底。

`/爱瓦 <任意内容>` 而内容没匹配上任何子命令时，`CommandGroupFilter.filter`
返回 False，指令被跳过，**消息原样进模型**。模型收到的是一句普通提问，于是
自己编答案：爱完美实测过，用户发一个链接过去，模型回「打不开，你发链接干嘛」
——它并不在查数据，它在编。

用户没打错指令，是这条指令没接住。所以指令组挂一个兜底过滤器：匹配了组名但
没匹配任何子命令时，自己回一句提示，把消息拦在这里。

只发了组名（`/爱瓦`）也归这里管：框架会给一份「参数不足」加全部子命令的
docstring，又长又不像是说给用户听的，直接回帮助更省事。
"""

from __future__ import annotations

__all__ = [
    "SUBCOMMANDS",
    "is_bare_command",
    "is_command_miss",
    "leftover_arg",
    "miss_hint",
    "starts_with_group",
]

# 全部子命令与别名。少一个别名，用户打那个别名时就落空了。
# 改 main.py 里的指令或别名，这里要跟着改——`test_main_wiring.py` 会核对。
SUBCOMMANDS = frozenset(
    {
        # 子命令
        "战绩",
        "锐评",
        "开一局",
        "PK",
        "解绑",
        "昵称",
        "状态",
        "帮助",
        "绑定",
        "绑定微信",
        # 别名
        "pk",
        "对战",
        "开局",
        "来一局",
        "help",
        "菜单",
        "status",
    }
)


def starts_with_group(message: str, group_name: str) -> bool:
    """这条消息是不是以指令组名开头。

    **必须有这一层判断**。`CommandGroupFilter.filter` 的执行顺序是
    先跑 `custom_filter_ok()`、之后才检查组名是否匹配，所以挂在指令组上的
    过滤器对**每一条消息**都会执行——包括别的插件的指令。
    没有这道闸，`leftover_arg` 对不属于自己的消息会原样返回，于是
    「爱农 状态」被判定成「爱瓦的子命令落空」，`stop_event()` 把整条掐死。
    实测过一次：爱农因此从能用变成完全没反应。
    """
    text = (message or "").strip()
    return text.startswith(f"/{group_name}") or text.startswith(group_name)


def leftover_arg(message: str, group_name: str) -> str:
    """取出组名后面的内容。

    组名可能带 `/` 前缀也可能不带——QQ 官方适配器有时会把 `message_str` 里的
    `/` 去掉，所以两种都得认。

    不以组名开头时返回空串，**不是原样返回**：调用方靠这一点判断「这条消息
    根本不归我管」。
    """
    text = (message or "").strip()
    for prefix in (f"/{group_name}", group_name):
        if text.startswith(prefix):
            return text[len(prefix) :].strip()
    return ""


def is_command_miss(message: str, group_name: str) -> bool:
    """这条消息是不是「匹配了组名但没匹配任何子命令」。

    判据必须与 `CommandGroupFilter.startswith` 一致——**字面前缀匹配**，否则
    会吃掉正常指令。

    消息完全等于组名（`/爱瓦`）不算落空：`filter` 那时会抛错列出全部子
    指令，不归这里管，不然两组提示会叠着发。
    """
    if not starts_with_group(message, group_name):
        # 不是发给我的，放行。别碰别人的指令。
        return False
    arg = leftover_arg(message, group_name)
    if not arg:
        return False
    return not any(arg.startswith(name) for name in SUBCOMMANDS)


def is_bare_command(message: str, group_name: str) -> bool:
    """只发了指令组名，一个参数都没带。

    `/爱瓦` 以前交给 AstrBot 处理，它回的是「参数不足」加全部子命令和各自的
    docstring——用户看到一大段内部说明，还得自己找能发什么。这里改成回帮助。
    """
    return starts_with_group(message, group_name) and not leftover_arg(message, group_name)


def miss_hint(arg: str) -> str:
    """没有子命令匹配时回什么。"""
    cleaned = (arg or "").strip()
    if not cleaned:
        return "没认出要做什么。全部指令发 /爱瓦 帮助。"

    # 参数短就原样带回去让用户看清自己发了什么，太长就截断——
    # 一整段东西回显出来没意义。
    echo = cleaned if len(cleaned) <= 40 else f"{cleaned[:40]}…"
    return f"「{echo}」不是子命令。全部指令发 /爱瓦 帮助。"
