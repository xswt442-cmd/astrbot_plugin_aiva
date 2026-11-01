"""打印运行中 AstrBot 里 aiva 的 handler 注册情况与 filter 链。

用法（容器内）：
    cd /AstrBot/data/plugins/astrbot_plugin_aiva && python test/注册表.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, "/AstrBot")
sys.path.insert(0, "/AstrBot/data/plugins/astrbot_plugin_aiva")

import data.plugins.astrbot_plugin_aiva.main as 插件  # noqa: E402,F401
from astrbot.core.star.filter.command import CommandFilter  # noqa: E402
from astrbot.core.star.filter.command_group import (  # noqa: E402
    CommandGroupFilter,
)
from astrbot.core.star.star_handler import (  # noqa: E402
    EventType,
    star_handlers_registry,
)


def build(text: str):
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.core.platform.astrbot_message import (
        AstrBotMessage,
        MessageType,
    )
    from astrbot.core.platform.platform_metadata import PlatformMetadata

    msg = AstrBotMessage()
    msg.type = MessageType.GROUP_MESSAGE
    e = AstrMessageEvent(
        message_str=text,
        message_obj=msg,
        platform_meta=PlatformMetadata("qq_official", "lg1", "lg1"),
        session_id="s",
    )
    e.is_at_or_wake_command = True
    e.is_wake = True
    return e


def report() -> None:
    handlers = star_handlers_registry.get_handlers_by_event_type(
        EventType.AdapterMessageEvent,
        plugins_name=None,
    )
    print(f"总 handler 数：{len(handlers)}")

    this_plugin = []
    for h in handlers:
        if "aiva" in str(h.handler_module_path):
            this_plugin.append(h)
            is_group = any(isinstance(f, CommandGroupFilter) for f in h.event_filters)
            print(f"\n== {h.handler_name}  是组handler={is_group}")
            print(f"   extras_configs={getattr(h, 'extras_configs', None)}")
            for f in h.event_filters:
                if isinstance(f, CommandGroupFilter):
                    print(f"   - Group 完整名={f.get_complete_command_names()}")
                    for s in f.sub_command_filters:
                        print(
                            f"       └ {s.command_name!r} 完整名={s.get_complete_command_names()}"
                        )
                elif isinstance(f, CommandFilter):
                    print(f"   - Cmd {f.command_name!r} 完整名={f.get_complete_command_names()}")
                else:
                    print(f"   - {type(f).__name__}")

    print("\n\n===== 模拟 waking_check：对 '爱瓦 绑定' 逐 handler 试filter =====")
    event = build("爱瓦 绑定")
    for h in this_plugin:
        passed = True
        details = []
        for f in h.event_filters:
            try:
                r = f.filter(event, {})
            except Exception as e:  # noqa: BLE001
                details.append(f"{type(f).__name__}=EXC({e})")
                passed = False
                break
            details.append(f"{type(f).__name__}={r}")
            if not r:
                passed = False
                break
        print(f"  {'✅' if passed else '— '} {h.handler_name}: {details}")

    print("\n===== 若某handler 通过，parsed_params =====")
    event2 = build("爱瓦 绑定")
    for h in this_plugin:
        for f in h.event_filters:
            try:
                if f.filter(event2, {}):
                    dump = event2.get_extra(default={})
                    if "parsed_params" in dump:
                        print(f"  {h.handler_name}: {dump['parsed_params']}")
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    report()
