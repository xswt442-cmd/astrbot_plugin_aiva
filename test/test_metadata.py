"""metadata.yaml 的约定。

AstrBot 插件市场从这份文件读插件信息，版本号也在里面。字段填错不影响安装，
但插件详情页会显示成 `[MISSING: ...]`、版本更新推不出去。
"""

from __future__ import annotations

import re
from pathlib import Path

仓库根 = Path(__file__).resolve().parent.parent
元数据路径 = 仓库根 / "metadata.yaml"
声明路径 = 仓库根 / "pyproject.toml"

# 项目声明支持 3.10，tomllib 要 3.11，所以用正则读那一行，不引 tomllib。
_版本行 = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)
_lock版本行 = re.compile(r'name = "astrbot-plugin-aiva"\nversion = "([^"]+)"', re.MULTILINE)


def _元数据() -> str:
    return 元数据路径.read_text(encoding="utf-8")


def _元数据版本() -> str:
    匹配 = re.search(r"^version:\s*(\S+)", _元数据(), re.MULTILINE)
    assert 匹配, "metadata.yaml 里找不到 version"
    return 匹配.group(1)


def _声明版本() -> str:
    匹配 = _版本行.search(声明路径.read_text(encoding="utf-8"))
    assert 匹配, "pyproject.toml 里找不到 version"
    return 匹配.group(1)


def _lock版本() -> str:
    文本 = (仓库根 / "uv.lock").read_text(encoding="utf-8")
    匹配 = _lock版本行.search(文本)
    assert 匹配, "uv.lock 里找不到本项目的版本"
    return 匹配.group(1)


def test_三处版本号一致():
    """metadata 是市场读的，pyproject 与 uv.lock 是本地与打包用的。

    对不上时市场会拒绝更新，或者本地构建出与市场不同的版本。
    """
    声明 = _声明版本()
    assert _元数据版本() == f"v{声明}"
    assert _lock版本() == 声明


def test_市场要求的字段都在():
    """少一个字段，市场详情页会显示成 `[MISSING: ...]`。"""
    文本 = _元数据()
    for 字段 in ("name:", "display_name:", "short_desc:", "desc:", "version:", "author:"):
        assert 字段 in 文本, f"metadata.yaml 缺 {字段}"


def test_分类是市场自己定的那几个():
    """填错不影响安装，只是详情页的分类标签不对。"""
    匹配 = re.search(r"^category:\s*(\S+)", _元数据(), re.MULTILINE)
    assert 匹配, "metadata.yaml 里找不到 category"
    assert 匹配.group(1) == "娱乐"


def test_声明的AstrBot版本区间合理():
    匹配 = re.search(r'astrbot_version:\s*"?([^"\n]+)"?', _元数据())
    assert 匹配, "metadata.yaml 里找不到 astrbot_version"

    区间 = 匹配.group(1).strip()
    assert 区间.startswith(">="), f"下界写成了 {区间}"
    assert "<" in 区间, f"少了上界，升级 AstrBot 后会静默失效：{区间}"


def test_图标在根目录且体积不大():
    """`logo.png` 由 `test/做图标.py` 从 `icon/aiva-icon.png` 生成。

    文件名与位置是 AstrBot 硬编码的。WebUI 每次刷新都重新拉这张图，
    太大就是纯浪费——超过 200KB 说明有人把源图直接放进了根目录。
    """
    图标 = 仓库根 / "logo.png"
    assert 图标.exists(), "缺 logo.png，跑 python test/做图标.py 生成"
    assert 图标.stat().st_size < 200 * 1024, "logo.png 超过 200KB，检查是不是放错图了"


def test_图标尺寸与色彩模式():
    """只读 PNG 头，不引 Pillow（它不是运行时依赖）。"""
    头 = (仓库根 / "logo.png").read_bytes()[:26]

    assert 头[:8] == b"\x89PNG\r\n\x1a\n", "不是 PNG 文件"
    assert 头[12:16] == b"IHDR", "第一个块不是 IHDR"

    宽 = int.from_bytes(头[16:20], "big")
    高 = int.from_bytes(头[20:24], "big")
    assert (宽, 高) == (256, 256), f"logo.png 是 {宽}×{高}，应为 256×256"

    色彩类型 = 头[25]
    # 2 = RGB 无 alpha，6 = RGBA。带 alpha 的图在深色背景上会露出方角。
    assert 色彩类型 == 2, f"logo.png 的色彩类型是 {色彩类型}，应为 2（RGB 无 alpha）"


def test_仓库里不放图标源图():
    """`logo.png` 是仓库里唯一要发的图，源图不进版本库。

    AstrBot 只认根目录的 `logo.png`（`star_manager` 里的 `logo_fname`），
    换图标直接替换这个文件；`test/做图标.py` 留着生成用，需要时把源图
    放回 `icon/` 再跑。
    """
    assert (仓库根 / "logo.png").exists()
    assert not (仓库根 / "icon").exists(), "源图目录不该再进仓库"
