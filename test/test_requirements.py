"""依赖清单的约束。

AstrBot 安装插件时把 requirements.txt 喂给 pip，而 pip 装在同一环境里，
装的就是 AstrBot 核心自己的依赖。核心给这些包装了版本护栏：插件若要求一个
与核心不同的精确版本，护栏会拦下 pip，安装整体失败。

`uv export` 恰好会把每个包钉成 `==`，正是触发护栏的写法。所以这个文件
必须手写。这里把三条约束钉住：

1. 不能出现 `==` 精确钉版本
2. 直接依赖要与 pyproject.toml 的 dependencies 对得上
3. 不能是 uv export 的产物
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS_PATH = REPO_ROOT / "requirements.txt"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"

# 只取真正写依赖的行，注释和空行不算。
_DEP_LINE = re.compile(r"^[A-Za-z0-9_.\-]+")

# 项目声明支持 3.10，而 tomllib 要 3.11，所以这里不引 tomllib，
# 只需要 dependencies 那一段的包名，正则读比解析 TOML 省事。
_DEP_BLOCK = re.compile(r"^dependencies\s*=\s*\[(.*?)\]", re.MULTILINE | re.DOTALL)


def _read_requirements() -> list[str]:
    return [
        line.strip()
        for line in REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _read_declared() -> list[str]:
    block = _DEP_BLOCK.search(PYPROJECT_PATH.read_text(encoding="utf-8"))
    assert block, "pyproject.toml 里找不到 dependencies"
    # 一行一项。不能按逗号切：版本范围里自带逗号（>=3.9,<5），切开会断。
    return [
        line.strip().strip(",").strip('"').strip("'")
        for line in block.group(1).splitlines()
        if line.strip().strip(",").strip('"').strip("'")
    ]


def test_no_exact_pins():
    """`==` 会和核心的版本护栏撞车，pip 动不了核心的包就装不上。"""
    pinned = [line for line in _read_requirements() if "==" in line]
    assert not pinned, f"requirements.txt 不能钉死版本，会和核心冲突：{pinned}"


def test_not_uv_export_output():
    """uv export 的文件头会这么写。看到这句就说明有人改回自动生成了。"""
    assert "uv 自动生成" not in REQUIREMENTS_PATH.read_text(encoding="utf-8")


def test_direct_deps_match_pyproject():
    """两个文件各服务一头：pyproject 给本地 uv sync，本清单给 AstrBot 的 pip。"""
    requirements_name = {_DEP_LINE.match(line).group() for line in _read_requirements()}
    declared_name = {_DEP_LINE.match(line).group() for line in _read_declared()}
    assert requirements_name == declared_name, (
        f"两边不一致。requirements.txt: {sorted(requirements_name)}，"
        f"pyproject.toml: {sorted(declared_name)}"
    )


def test_range_constraint_not_empty():
    """每一行都要有版本下界，否则 pip 可能把核心的包换成任意版本。"""
    for line in _read_requirements():
        assert re.search(r"[><=]+[0-9]", line), f"缺少版本约束：{line}"


def test_lower_bound_follows_core():
    """清单的下界对齐 AstrBot 核心的声明，比 pyproject 更严是可以的。

    核心要求 aiohttp>=3.11.18，本地开发按 >=3.9 也行，但装进容器时按核心的
    下界写才不会让 pip 去动核心的包。所以允许清单更严，不允许更松。
    """

    def _lower_bound(constraint: str) -> tuple[int, ...]:
        match = re.search(r">=\s*([0-9.]+)", constraint)
        return tuple(int(x) for x in match.group(1).split(".")) if match else ()

    requirements = {line.split(">")[0].split("<")[0].strip(): line for line in _read_requirements()}
    for declared in _read_declared():
        pkg, _, constraint = declared.partition(">")
        assert _lower_bound(requirements[pkg]) >= _lower_bound(constraint), (
            f"{pkg} 在 requirements.txt 里的下界比 pyproject.toml 松，装插件时 pip 可能去动核心的包"
        )
