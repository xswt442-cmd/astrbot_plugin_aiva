"""metadata.yaml 的约定。

AstrBot 插件市场从这份文件读插件信息，版本号也在里面。字段填错不影响安装，
但插件详情页会显示成 `[MISSING: ...]`、版本更新推不出去。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
METADATA_PATH = REPO_ROOT / "metadata.yaml"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"

# 项目声明支持 3.10，tomllib 要 3.11，所以用正则读那一行，不引 tomllib。
_VERSION_LINE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)
_LOCK_VERSION_LINE = re.compile(r'name = "astrbot-plugin-aiva"\nversion = "([^"]+)"', re.MULTILINE)


def _metadata() -> str:
    return METADATA_PATH.read_text(encoding="utf-8")


def _metadata_version() -> str:
    match = re.search(r"^version:\s*(\S+)", _metadata(), re.MULTILINE)
    assert match, "metadata.yaml 里找不到 version"
    return match.group(1)


def _declared_version() -> str:
    match = _VERSION_LINE.search(PYPROJECT_PATH.read_text(encoding="utf-8"))
    assert match, "pyproject.toml 里找不到 version"
    return match.group(1)


def _lock_version() -> str:
    text = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    match = _LOCK_VERSION_LINE.search(text)
    assert match, "uv.lock 里找不到本项目的版本"
    return match.group(1)


def test_three_versions_agree():
    """metadata 是市场读的，pyproject 与 uv.lock 是本地与打包用的。

    对不上时市场会拒绝更新，或者本地构建出与市场不同的版本。
    """
    declared = _declared_version()
    assert _metadata_version() == f"v{declared}"
    assert _lock_version() == declared


def test_market_fields_present():
    """少一个字段，市场详情页会显示成 `[MISSING: ...]`。"""
    text = _metadata()
    for field in ("name:", "display_name:", "short_desc:", "desc:", "version:", "author:"):
        assert field in text, f"metadata.yaml 缺 {field}"


def test_category_from_market_list():
    """填错不影响安装，只是详情页的分类标签不对。"""
    match = re.search(r"^category:\s*(\S+)", _metadata(), re.MULTILINE)
    assert match, "metadata.yaml 里找不到 category"
    assert match.group(1) == "娱乐"


def test_astrbot_range_sane():
    match = re.search(r'astrbot_version:\s*"?([^"\n]+)"?', _metadata())
    assert match, "metadata.yaml 里找不到 astrbot_version"

    span = match.group(1).strip()
    assert span.startswith(">="), f"下界写成了 {span}"
    assert "<" in span, f"少了上界，升级 AstrBot 后会静默失效：{span}"


def test_icon_at_root_and_small():
    """`logo.png` 由 `test/做图标.py` 从 `icon/aiva-icon.png` 生成。

    文件名与位置是 AstrBot 硬编码的。WebUI 每次刷新都重新拉这张图，
    太大就是纯浪费——超过 200KB 说明有人把源图直接放进了根目录。
    """
    icon = REPO_ROOT / "logo.png"
    assert icon.exists(), "缺 logo.png，跑 python test/做图标.py 生成"
    assert icon.stat().st_size < 200 * 1024, "logo.png 超过 200KB，检查是不是放错图了"


def test_icon_size_and_mode():
    """只读 PNG 头，不引 Pillow（它不是运行时依赖）。"""
    header = (REPO_ROOT / "logo.png").read_bytes()[:26]

    assert header[:8] == b"\x89PNG\r\n\x1a\n", "不是 PNG 文件"
    assert header[12:16] == b"IHDR", "第一个块不是 IHDR"

    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    assert (width, height) == (256, 256), f"logo.png 是 {width}×{height}，应为 256×256"

    color_type = header[25]
    # 2 = RGB 无 alpha，6 = RGBA。带 alpha 的图在深色背景上会露出方角。
    assert color_type == 2, f"logo.png 的色彩类型是 {color_type}，应为 2（RGB 无 alpha）"


def test_no_icon_source_in_repo():
    """`logo.png` 是仓库里唯一要发的图，源图不进版本库。

    AstrBot 只认根目录的 `logo.png`（`star_manager` 里的 `logo_fname`），
    换图标直接替换这个文件；`test/做图标.py` 留着生成用，需要时把源图
    放回 `icon/` 再跑。
    """
    assert (REPO_ROOT / "logo.png").exists()
    assert not (REPO_ROOT / "icon").exists(), "源图目录不该再进仓库"
