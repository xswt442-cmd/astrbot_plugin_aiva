"""工作流里的 shell 片段必须用 ASCII 变量名。

GitHub runner 的 bash 与 awk 都不接受非 ASCII 的标识符：

- `bash -c '版本=v0.1.4'` 直接 `command not found`，`$版本` 展开成空，
  于是 `[[ ! "" =~ ^v... ]]` 判定失败，报出来的错误信息还是那句正确的
  提示，看起来像版本号写错了，其实是变量根本没赋值。
- `awk` 里的中文变量名报 `invalid char`，整步失败。

这个错在 release 工作流上连踩两次：第一次是 awk，改掉后没检查 bash 里的
变量，触发时立刻失败。所以在这里扫全，文本内容（echo 的中文、注释）不受限制。
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOW_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"

# bash 的变量赋值与引用：NAME= / $NAME / ${NAME} / $'...' 之外的普通形式
_ASSIGN = re.compile(r"^\s*(?:export\s+)?([^\s=]+)=")
_REFERENCE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
_BUILTINS = {
    "BASH",
    "PATH",
    "HOME",
    "GITHUB",
    "RUNNER",
    "CI",
    "PWD",
    "USER",
    "TMPDIR",
    "IFS",
    "RANDOM",
    "SECONDS",
    "LINENO",
    "PIPESTATUS",
    "ver",
    "bare",
    "meta",
    "pkgver",
    "latest",
    "bytes",
    "grep",
    "unzip",
    "du",
    "stat",
    "cut",
    "awk",
    "echo",
    "git",
    "gh",
    "sleep",
    "seq",
    "docker",
    "uv",
    "npm",
    "node",
}


def _check_stepwise(name: str, raw: str) -> list[str]:
    """只看 run: 块里的 shell，YAML 结构和 echo 的中文不算。"""
    problems: list[str] = []
    in_script = False
    for lineno, line in enumerate(raw.splitlines(), 1):
        if not in_script:
            if re.match(r"^\s*run:\s*\|", line):
                in_script = True
            continue

        assign = _ASSIGN.match(line)
        if assign and not assign.group(1).isascii():
            problems.append(f"{name}:{lineno} 变量名非 ASCII：{assign.group(1)}")

        for variables in _REFERENCE.findall(line):
            if not variables.isascii():
                problems.append(f"{name}:{lineno} 引用非 ASCII 变量：${variables}")

    return problems


def test_shell_var_names_are_ascii():
    problems: list[str] = []
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        problems += _check_stepwise(path.name, path.read_text(encoding="utf-8"))
    assert not problems, "bash 不接受非 ASCII 变量名，改用 ASCII：\n" + "\n".join(problems)


def test_workflow_dir_not_empty():
    """确认 glob 没写错，否则上面那条会静默通过。"""
    assert list(WORKFLOW_DIR.glob("*.yml")), f"{WORKFLOW_DIR} 下没有找到工作流文件"
