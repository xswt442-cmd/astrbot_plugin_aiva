"""在容器里验插件能不能真的加载。

`main.py` 本地跑不了单测（顶层 import astrbot），所以装到容器里才算第一次
真验。爱完美的教训在这儿：它本地全绿、到容器里 `AttributeError` 挂掉，
所以这一步值得单独做。

用法（在 AstrBot 容器内）：

    python test/验加载.py /AstrBot/data/plugins/astrbot_plugin_aiva
"""

import ast
import importlib
import sys
from pathlib import Path

PLUGIN_DIR = Path(sys.argv[1])
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, "/AstrBot")

problems = []

# 1. 语法
try:
    source = (PLUGIN_DIR / "main.py").read_text(encoding="utf-8")
    ast.parse(source)
except Exception as exc:
    print(f"[失败] main.py 语法：{type(exc).__name__}: {exc}")
    raise SystemExit(1) from None
print("[通过] main.py 语法")

# 2. 用到的 astrbot API 真的存在
for name in ("command_group", "custom_filter"):
    module = importlib.import_module("astrbot.api.event.filter")
    if not hasattr(module, name):
        problems.append(f"astrbot.api.event.filter 没有 {name}")
    else:
        print(f"[通过] astrbot.api.event.filter.{name}")

for path, name in (
    ("astrbot.api.star", "Context"),
    ("astrbot.api.star", "Star"),
    ("astrbot.core.star.filter.custom_filter", "CustomFilter"),
    ("astrbot.core.platform.astr_message_event", "MessageEventResult"),
    ("astrbot.core.star.filter.command", "GreedyStr"),
):
    try:
        module = importlib.import_module(path)
        getattr(module, name)
        print(f"[通过] {path}.{name}")
    except Exception as exc:
        problems.append(f"{path}.{name} 拿不到：{exc}")

# 3. Model 调用方式：Context 上真有 tool_loop_agent，
#    main.py 也得真的在用它——本地测试碰不到 astrbot，
#    万一写成没有的方法只能到这一步才发现。
try:
    context = importlib.import_module("astrbot.api.star").Context
    if not hasattr(context, "tool_loop_agent"):
        problems.append("Context 上没有 tool_loop_agent")
    elif "tool_loop_agent" not in source:
        problems.append("main.py 没在用 tool_loop_agent")
    else:
        print("[通过] Context.tool_loop_agent 可用且在用")
except Exception as exc:
    problems.append(f"拿不到 Context：{exc}")

# 4. 兜底挂在指令组方法上，且不是那句会 AttributeError 的写法
tree = ast.parse(source)
if "@custom_filter(_落空兜底)" not in source:
    problems.append("兜底没挂在指令组方法上")
# 只扫代码里的属性访问，别扫注释——注释里为了说明坑会提到那个方法名。
used_attrs = [node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)]
if "add_custom_filter" in used_attrs:
    problems.append("还在调 add_custom_filter(...)，那会 AttributeError")
if not problems:
    print("[通过] 兜底的挂法")

# 5. 真的 import 一遍插件里的模块（不加载插件，只验模块级代码）
for module_obj in (
    "aiva.兜底",
    "aiva.pk",
    "aiva.match",
    "aiva.stats",
    "aiva.prompts",
    "aiva.storage",
    "aiva.transport",
    "aiva.auth.ptlogin",
):
    try:
        importlib.import_module(module_obj)
    except Exception as exc:
        problems.append(f"import {module_obj} 失败：{type(exc).__name__}: {exc}")
if not problems:
    print("[通过] aiva 各模块可导入")

if problems:
    print("\n[失败]")
    for entry in problems:
        print(f"  · {entry}")
    raise SystemExit(1)

print("\n[全部通过] 可以在容器里加载")
