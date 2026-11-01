"""在AstrBot 容器里实测爱瓦指令的 filter 链。

单独跑一个进程时 `star_handlers_registry` 是空的（插件是主进程加载的），
所以这里直接 import 插件的 main，让装饰器把 filter 建出来，再拿它跑候选消息。

用法（在容器内）：
    cd /AstrBot/data/plugins/astrbot_plugin_aiva && python test/探针.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, "/AstrBot")
sys.path.insert(0, "/AstrBot/data/plugins/astrbot_plugin_aiva")

import data.plugins.astrbot_plugin_aiva.main as aiva_plugin  # noqa: E402
from astrbot.core.platform.astr_message_event import AstrMessageEvent  # noqa: E402
from astrbot.core.platform.astrbot_message import (  # noqa: E402
    AstrBotMessage,
    MessageType,
)
from astrbot.core.platform.platform_metadata import PlatformMetadata  # noqa: E402
from astrbot.core.star.filter.command import CommandFilter  # noqa: E402
from astrbot.core.star.filter.command_group import (  # noqa: E402
    CommandGroupFilter,
)

candidates = [
    "/爱瓦 绑定",
    "爱瓦 绑定",
    "[At:qq_official] /爱瓦 绑定",
]


def make_event(text: str) -> AstrMessageEvent:
    msg = AstrBotMessage()
    msg.type = MessageType.GROUP_MESSAGE
    e = AstrMessageEvent(
        message_str=text,
        message_obj=msg,
        platform_meta=PlatformMetadata("qq_official", "lg1", "lg1"),
        session_id="ZnNe/2A8FDBA44C38D5B4863B0F49D76E744E",
    )
    e.is_at_or_wake_command = True
    return e


def main():
    # 装饰器建出来的组挂在 RegisteringCommandable 上
    instance = aiva_plugin.Main.__new__(aiva_plugin.Main)
    group = instance.aiva_group.parent_group
    assert isinstance(group, CommandGroupFilter)
    print("指令组完整名：", group.get_complete_command_names())
    print("custom_filter：", [type(c).__name__ for c in group.custom_filter_list])
    child = group.sub_command_filters
    print("子命令：", [s.command_name for s in child])
    print("子命令 custom_filter 数：", [len(s.custom_filter_list) for s in child])
    print()

    # 组上的 filter + 每个子指令的 filter，合成 waking_check 实际跑的链路
    chain = [("组", group)] + [(s.command_name, s) for s in child]

    for text in candidates:
        print(f"===== message_str = {text!r} =====")
        for name, f in chain:
            event = make_event(text)
            try:
                r = f.filter(event, {})
            except Exception as e:  # noqa: BLE001
                print(f"  {name}: 抛错 {type(e).__name__}: {e}")
                continue
            extra = ""
            if isinstance(f, CommandFilter) and event.get_extra(default={}).get("parsed_params"):
                extra = f" params={event.get_extra(default={})['parsed_params']}"
            print(f"  {'✅' if r else '— '} {name}: {r}{extra}")
        print()


if __name__ == "__main__":
    main()
