#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 SwissFTP 的程序图标（现代扁平：蓝底 + SD 卡 + 上传箭头）。

运行：python tools/make_icon.py
输出：tools/assets/swissftp.ico、tools/assets/swissftp_preview.png
仅构建时需要 Pillow，不参与 EXE 运行时依赖。
"""

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
OUT = HERE / "assets"
BLUE = (47, 111, 235, 255)
WHITE = (255, 255, 255, 255)
PALE = (205, 220, 245, 255)
S = 4  # 超采样倍数，画布 256*4

SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
         (128, 128), (256, 256)]


def k(v: int) -> int:
    return v * S


def draw_icon() -> Image.Image:
    im = Image.new("RGBA", (k(256), k(256)), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    # 圆角方形底
    d.rounded_rectangle((k(8), k(8), k(248), k(248)), radius=k(54), fill=BLUE)

    # SD 卡：右上切角，白色卡身
    card = [(k(72), k(64)), (k(166), k(64)), (k(192), k(90)),
            (k(192), k(200)), (k(72), k(200))]
    d.polygon(card, fill=WHITE)
    d.line(card + [card[0]], fill=WHITE, width=k(7), joint="curve")

    # 顶部触点
    for x in (88, 108, 128, 148):
        d.rounded_rectangle((k(x), k(52), k(x + 12), k(70)),
                            radius=k(3), fill=PALE)

    # 上传箭头（蓝色，画在白卡上）
    d.rounded_rectangle((k(120), k(126), k(144), k(174)),
                        radius=k(10), fill=BLUE)
    d.polygon([(k(104), k(140)), (k(160), k(140)), (k(132), k(106))],
              fill=BLUE)
    d.line([(k(104), k(140)), (k(132), k(106)), (k(160), k(140))],
           fill=BLUE, width=k(6), joint="curve")
    return im


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    big = draw_icon()
    ico = OUT / "swissftp.ico"
    big.resize((256, 256), Image.Resampling.LANCZOS).save(
        ico, format="ICO", sizes=SIZES)
    preview = OUT / "swissftp_preview.png"
    big.resize((256, 256), Image.Resampling.LANCZOS).save(preview)
    print("ICO :", ico, ico.stat().st_size, "bytes")
    print("PNG :", preview)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
