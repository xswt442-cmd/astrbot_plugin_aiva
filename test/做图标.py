"""生成根目录的 logo.png。

源图在 `icon/aiva-icon.png`，是 1254×1254 的方形深底徽章。

与 aiwm 那份脚本不同，这张图**不做亮度抠底**：采样过，
背景亮度 r+g+b 在 40~57，而图案自己的暗部（人物面部阴影 55、
红圈暗部 66）与之重叠——按亮度抠会把人抠没。好在徽章外圈直径
约占画布 87%，AstrBot 的圆形裁切只会切到四角的背景，切不到图案，
所以直接整张缩放即可，方形硬边不会露出来。

规格是 256×256、RGB、无 alpha、PNG 压到几十 KB。WebUI 每次刷新都
重新拉这张图，太大就是纯浪费。源图是插画，重采样用 LANCZOS。

需要 Pillow，它不在 `requirements.txt` 里（那是给 AstrBot 装的运行时清单）：

    python test/做图标.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_PATH = REPO_ROOT / "icon" / "aiva-icon.png"
OUTPUT_PATH = REPO_ROOT / "logo.png"

SIZE = 256

# 背景判定：亮度 r+g+b 低于这个值且 RGB 差异不大（不是彩色图案）就算背景。
# 用来自检「圆形裁切不会切到图案」——如果图案贴边了要人工换源图。
BG_BRIGHTNESS_MAX = 100


def main() -> None:
    source = Image.open(SOURCE_PATH).convert("RGB")
    width, height = source.size
    if width != height:
        sys.exit(f"源图 {width}×{height} 不是正方形，先裁方再放进来")

    image = source.resize((SIZE, SIZE), Image.LANCZOS)
    self_check(image)

    # optimize 让 PNG 用最省的压缩方式
    image.save(OUTPUT_PATH, format="PNG", optimize=True)
    print(f"{OUTPUT_PATH.relative_to(REPO_ROOT)}：{image.size[0]}×{image.size[1]} {image.mode}")
    print(f"{OUTPUT_PATH.stat().st_size} 字节")


def self_check(image: Image.Image) -> None:
    """确认圆形裁切安全：图案最外沿到画布边的距离要够。

    逐行找第一个非背景像素。图案离画布边太近的话，圆形头像裁切时
    会把徽章切掉一角——那种问题肉眼看缩略图注意不到，发出去才发现。
    """
    pixels = image.load()

    def is_bg(x: int, y: int) -> bool:
        r, g, b = pixels[x, y]
        return r + g + b <= BG_BRIGHTNESS_MAX

    # 从四条边往里扫，取图案最先出现的位置
    margin = []
    for x in range(SIZE):
        column = [y for y in range(SIZE) if not is_bg(x, y)]
        if column:
            margin.append(min(column, key=lambda y: min(y, SIZE - 1 - y)))
    if not margin:
        sys.exit("整张图都被判成背景，自检阈值可能不对")
    nearest = min(
        min(margin),
        min(
            min(x for x in range(SIZE) if not is_bg(x, y))
            for y in range(0, SIZE, 4)
            if any(not is_bg(x, y) for x in range(SIZE))
        ),
    )
    # 圆形裁切半径 128，图案必须在半径内：离边至少留 12px 余量
    if nearest < 12:
        sys.exit(f"图案最外沿离画布边只有 {nearest}px，圆形裁切会切到它；把源图缩小些再放进来")


if __name__ == "__main__":
    main()
