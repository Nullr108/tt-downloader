# -*- coding: utf-8 -*-
"""
Генерация анимированного GIF-заглушки "Здесь могла быть ваша реклама".
Запускается автоматически из bot.py, если файла ad_placeholder.gif нет.
Никаких эмодзи в самом GIF (Pillow их не рисует) — эмодзи живут в подписи.
"""
import os
import math
from PIL import Image, ImageDraw, ImageFont

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ad_placeholder.gif")

W, H = 640, 360
FRAMES = 24
LINE1 = "ЗДЕСЬ МОГЛА БЫТЬ"
LINE2 = "ВАША РЕКЛАМА"


def _font(size):
    # Пытаемся взять шрифт с поддержкой кириллицы
    for path in (
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _centered(draw, y, text, font, fill):
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    draw.text(((W - tw) / 2, y), text, font=font, fill=fill)


def build(out=OUT):
    big = _font(56)
    small = _font(28)
    frames = []
    for i in range(FRAMES):
        t = i / FRAMES
        img = Image.new("RGB", (W, H), (12, 12, 20))
        d = ImageDraw.Draw(img)

        # бегущие цветные полосы сверху и снизу
        for x in range(0, W, 40):
            phase = (x / W + t) % 1.0
            r = int(127 + 127 * math.sin(2 * math.pi * (phase)))
            g = int(127 + 127 * math.sin(2 * math.pi * (phase + 0.33)))
            b = int(127 + 127 * math.sin(2 * math.pi * (phase + 0.66)))
            d.rectangle([x, 0, x + 38, 10], fill=(r, g, b))
            d.rectangle([x, H - 10, x + 38, H], fill=(b, r, g))

        # пульсация текста (яркость)
        pulse = int(180 + 75 * math.sin(2 * math.pi * t))
        col = (pulse, pulse, 255)
        _centered(d, 120, LINE1, big, col)
        _centered(d, 190, LINE2, big, col)

        # бегущие точки-загрузка
        dots = "•" * (1 + (i % 4))
        _centered(d, 275, dots, small, (255, 210, 90))

        frames.append(img)

    frames[0].save(
        out,
        save_all=True,
        append_images=frames[1:],
        duration=80,
        loop=0,
        disposal=2,
    )
    return out


if __name__ == "__main__":
    p = build()
    print("saved:", p)
