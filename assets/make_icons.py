"""Генерирует иконки приложения: assets/icon.png, icon.ico (Windows), icon.icns (macOS).

Запуск (нужен Pillow и шрифт PT Serif Caption из macOS):
    uv run --with pillow python assets/make_icons.py
"""
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
FONT = "/System/Library/Fonts/Supplemental/PTSerifCaption.ttc"
S = 1024


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def draw() -> Image.Image:
    # фон: тёплый графит с мягким светом сверху
    bg = Image.new("RGB", (S, S))
    px = bg.load()
    for y in range(S):
        for x in range(S):
            d = ((x - S * 0.35) ** 2 + (y + S * 0.1) ** 2) ** 0.5 / (S * 1.25)
            px[x, y] = lerp((52, 47, 39), (20, 18, 15), min(d, 1))
    img = bg.convert("RGBA")
    d = ImageDraw.Draw(img)
    # буква «Т» — PT Serif Caption, льняной цвет
    font = ImageFont.truetype(FONT, 640, index=0)
    text = "Т"
    box = d.textbbox((0, 0), text, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    x, y = (S - w) / 2 - box[0], S * 0.44 - h / 2 - box[1]
    # тень буквы
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).text((x, y + 14), text, font=font, fill=(0, 0, 0, 140))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(18)))
    d.text((x, y), text, font=font, fill=(233, 225, 208, 255))
    # терракотовая черта-«строка»
    bar_w, bar_h, bar_y = 430, 44, int(S * 0.79)
    d.rounded_rectangle([(S - bar_w) / 2, bar_y, (S + bar_w) / 2, bar_y + bar_h], radius=22, fill=(212, 112, 90, 255))
    # скруглённый квадрат (как иконки macOS)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.225), fill=255)
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def main() -> None:
    icon = draw()
    icon.save(HERE / "icon.png")
    icon.save(HERE / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    if sys.platform == "darwin":
        with tempfile.TemporaryDirectory() as tmp:
            iconset = Path(tmp) / "icon.iconset"
            iconset.mkdir()
            for size in (16, 32, 128, 256, 512):
                icon.resize((size, size), Image.LANCZOS).save(iconset / f"icon_{size}x{size}.png")
                icon.resize((size * 2, size * 2), Image.LANCZOS).save(iconset / f"icon_{size}x{size}@2x.png")
            subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(HERE / "icon.icns")], check=True)
    print("Готово:", ", ".join(p.name for p in HERE.glob("icon.*")))


if __name__ == "__main__":
    main()
