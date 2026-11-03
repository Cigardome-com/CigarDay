#!/usr/bin/env python3
"""Картинки для карточек сигар в боте: фото с Cigardome → JPEG 1200×600.

Фото у Cigardome горизонтальные (~6:1) и на прозрачном фоне. Отправленные
в Telegram как есть, они превращаются в тонкую полоску на чёрном. Поэтому
кладём сигару на тёплый светлый фон с мягкой тенью — как предметную
съёмку — и сохраняем в data/cards/<cid>.jpg (репозиторий; на сервере нет
Pillow, а картинки нужны одинаковые всегда).

Исходники качаем по reference.image из prices.json CigarIndex, по одному,
с паузой; скачанное кэшируется вне репозитория.

  CIGARINDEX_ROOT=../cigar_scrapper python3.13 cigarday/card_images.py
"""
import json
import os
import pathlib
import subprocess
import sys
import time

from PIL import Image, ImageDraw, ImageFilter

import paths

OUT = paths.REPO / "data" / "cards"
CACHE = pathlib.Path(os.environ.get("CIGARDOME_IMG_CACHE",
                                    pathlib.Path.home() / ".cache" / "cigarindex" / "cigardome-img"))
UA = "CigarIndex-profile/1.0 (+https://cigarindex.ru)"
W, H = 1200, 600
BG_CENTER = (247, 241, 231)     # светлый крем
BG_EDGE = (226, 212, 190)       # тёплый беж к краям


def background():
    """Мягкая радиальная заливка: светлее в центре, теплее к краям."""
    small = Image.new("RGB", (120, 60))
    px = small.load()
    for y in range(60):
        for x in range(120):
            dx, dy = (x - 60) / 60, (y - 30) / 30
            t = min(1.0, (dx * dx * 0.8 + dy * dy) ** 0.5 * 0.75)
            px[x, y] = tuple(int(c + (e - c) * t) for c, e in zip(BG_CENTER, BG_EDGE))
    return small.resize((W, H), Image.BICUBIC)


def compose(src):
    im = Image.open(src).convert("RGBA")
    bbox = im.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
    if bbox:
        im = im.crop(bbox)
    # Сигара занимает до 86% ширины и не больше 46% высоты.
    k = min(W * 0.86 / im.width, H * 0.46 / im.height)
    im = im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))), Image.LANCZOS)
    x, y = (W - im.width) // 2, (H - im.height) // 2 - 10
    card = background()
    # Тень: силуэт сигары, сдвинутый вниз, размытый и полупрозрачный.
    shadow = Image.new("L", (W, H), 0)
    sil = im.getchannel("A").point(lambda a: int(a * 0.42))
    shadow.paste(sil, (x + 6, y + int(im.height * 0.22) + 8))
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))
    card.paste((92, 64, 38), (0, 0), shadow)
    card.paste(im, (x, y), im)
    # Тонкая рамка-подложка, чтобы карточка аккуратно смотрелась и в тёмной теме.
    d = ImageDraw.Draw(card)
    d.rectangle([0, 0, W - 1, H - 1], outline=(214, 198, 172), width=2)
    return card


def fetch(url, dest):
    r = subprocess.run(["curl", "-s", "-f", "-m", "40", "-A", UA, "-o", str(dest), url])
    return r.returncode == 0 and dest.exists() and dest.stat().st_size > 500


def main():
    prices = json.loads(paths.PRICES.read_text(encoding="utf-8"))["cigars"]
    OUT.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    made, missing, failed = 0, [], []
    first = True
    for cid, c in sorted(prices.items()):
        url = (c.get("reference") or {}).get("image")
        if not url:
            missing.append(cid)
            continue
        src = CACHE / f"{cid}.webp"
        if not src.exists():
            if not first:
                time.sleep(1.0)
            first = False
            if not fetch(url, src):
                failed.append(cid)
                src.unlink(missing_ok=True)
                continue
        try:
            compose(src).save(OUT / f"{cid}.jpg", "JPEG", quality=84, optimize=True, progressive=True)
            made += 1
        except Exception as e:                      # noqa: BLE001
            failed.append(f"{cid} ({type(e).__name__})")
    # Пустая карточка — для вкладки, где показать нечего (сообщение-карусель
    # должно оставаться фото, иначе его не перелистнуть обратно).
    empty = background()
    ImageDraw.Draw(empty).rectangle([0, 0, W - 1, H - 1], outline=(214, 198, 172), width=2)
    empty.save(OUT / "_empty.jpg", "JPEG", quality=84, optimize=True)
    size = sum(p.stat().st_size for p in OUT.glob("*.jpg"))
    print(f"карточек: {made}, без ссылки на фото: {len(missing)} {missing[:5]}, "
          f"не вышло: {len(failed)} {failed[:5]}; всего {size / 1e6:.1f} МБ")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
