#!/usr/bin/env python3
"""Профили сигар для подбора (тестовый бот CigarDay): формат, страны трёх
листов, время курения, крепость 1–5 и вкусовые ноты.

Разовый сбор с cigardome.com по ссылкам из data/targets.json. Источник на
странице — window.__SSG_CIGAR_DATA__ (объект cigar внутри — строгий JSON):
type, length, ringGauge, smokeTime, strength, countryKeys{filler,binder,
wrapper}, flavorProfile. Запасной путь — текст «Type … Smoke time N min».

Ноты берём словарём из reference.description_en (prices.json). flavorProfile
Cigardome сохраняем рядом вместе с числом отзывов, но подбор на него при
1–2 отзывах не опирается: это одно чужое мнение, а не профиль.

Вежливость: robots.txt разрешает /cigar/ (проверено 03.11.2026), запросы
по одному с паузой, сырые страницы кэшируются вне репозитория — повторный
разбор сеть не трогает.

  python3.13 cigarday/cigardome_profile.py --fetch      # скачать недостающие страницы
  python3.13 cigarday/cigardome_profile.py              # только разобрать кэш → data/cigar_profile.json

prices.json не меняется. Результат — data/cigar_profile.json.
"""
import argparse
import collections
import datetime
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import html as htmlmod

import paths

TARGETS = paths.TARGETS
PRICES = paths.PRICES
OUT = paths.PROFILES
CACHE = pathlib.Path(os.environ.get("CIGARDOME_CACHE", pathlib.Path.home() / ".cache" / "cigarindex" / "cigardome"))
UA = "CigarIndex-profile/1.0 (+https://cigarindex.ru)"
PAUSE = 3.0

COUNTRY = {
    "CU": "Куба", "DO": "Доминикана", "NI": "Никарагуа", "HN": "Гондурас",
    "CR": "Коста-Рика", "EC": "Эквадор", "US": "США", "ID": "Индонезия",
    "BR": "Бразилия", "MX": "Мексика", "CM": "Камерун", "PA": "Панама",
    "PE": "Перу", "CO": "Колумбия", "PH": "Филиппины", "CN": "Китай",
    "IT": "Италия", "ES": "Испания", "PY": "Парагвай", "GT": "Гватемала",
    "JM": "Ямайка", "PR": "Пуэрто-Рико", "CF": "ЦАР", "SV": "Сальвадор",
}

STRENGTH_TEXT = {"mild": 1, "mild-medium": 2, "medium": 3, "medium-full": 4, "full": 5}

# Каноническая нота → (русская подпись, слова-признаки). Слова ищем целиком
# (границы слова), во множественном числе тоже.
NOTES = {
    "coffee":  ("кофе",            ["coffee", "espresso", "mocha", "café"]),
    "cocoa":   ("какао",           ["cocoa", "cacao", "chocolate", "chocolatey", "mocha", "fudge"]),
    "cedar":   ("кедр",            ["cedar", "cedary"]),
    "wood":    ("дерево",          ["wood", "woody", "woodsy", "oak", "oaky", "hickory"]),
    "nuts":    ("орехи",           ["nut", "nuts", "nutty", "almond", "almonds", "walnut", "walnuts", "peanut",
                                    "peanuts", "cashew", "cashews", "hazelnut", "hazelnuts", "pecan", "pecans",
                                    "chestnut", "marzipan"]),
    "leather": ("кожа",            ["leather", "leathery"]),
    "spice":   ("пряности",        ["spice", "spices", "spicy", "cinnamon", "clove", "cloves", "nutmeg",
                                    "anise", "licorice", "ginger", "cardamom"]),
    "pepper":  ("перец",           ["pepper", "peppery", "peppercorn"]),
    "earth":   ("земля",           ["earth", "earthy", "earthiness", "soil", "mineral", "minerals", "musty", "loam"]),
    "cream":   ("сливки",          ["cream", "creamy", "creaminess", "butter", "buttery", "milk", "milky"]),
    "fruit":   ("фрукты",          ["fruit", "fruity", "fruits", "citrus", "berry", "berries", "cherry", "cherries",
                                    "raisin", "raisins", "fig", "figs", "orange", "apple", "plum", "plums",
                                    "lemon", "apricot", "dates"]),
    "sweet":   ("сладость, мёд",   ["honey", "caramel", "vanilla", "sweet", "sweetness", "sugar", "sugary",
                                    "molasses", "toffee", "syrup", "maple"]),
    "floral":  ("цветы",           ["floral", "flower", "flowers", "flowery"]),
    "herb":    ("травы, сено",     ["hay", "grass", "grassy", "herbal", "herb", "herbs", "vegetal", "tea"]),
    "toast":   ("тосты, хлеб",     ["toast", "toasty", "toasted", "bread", "bready", "graham", "cracker", "biscuit"]),
}

_NOTE_RE = {k: re.compile(r"\b(" + "|".join(map(re.escape, words)) + r")\b", re.I)
            for k, (_, words) in NOTES.items()}


def notes_from_text(text):
    """{нота: число упоминаний}. Порядок первого упоминания сохраняем:
    в описаниях Cigardome главное обычно сказано раньше."""
    text = text or ""
    found = []
    for k, rx in _NOTE_RE.items():
        ms = list(rx.finditer(text))
        if ms:
            found.append((ms[0].start(), k, len(ms)))
    found.sort()
    return {k: n for _, k, n in found}


def strength_num(js_val, text_val):
    if isinstance(js_val, (int, float)) and 1 <= js_val <= 5:
        return int(round(js_val))
    return STRENGTH_TEXT.get((text_val or "").strip().lower())


def parse_page(page):
    """Данные Cigardome со страницы сигары: dict или None."""
    i = page.find("__SSG_CIGAR_DATA__")
    if i >= 0:
        j = page.find("cigar:", i)
        if j >= 0:
            j = page.find("{", j)
            try:
                obj, _ = json.JSONDecoder().raw_decode(page, j)
                return {"source": "json", **obj}
            except ValueError:
                pass
    # Запасной путь — видимый текст.
    s = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page, flags=re.S)
    s = re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<[^>]+>", " ", s)))
    m = re.search(r"Type (.+?) Size (\d+) mm × (\d+) Strength (.*?) ?Filler (.+?) Binder (.+?) Wrapper (.+?) Smoke time (\d+) min", s)
    if not m:
        return None
    return {"source": "text", "type": m[1], "length": int(m[2]), "ringGauge": int(m[3]),
            "smokeTime": int(m[8]), "countryNames": {"filler": [m[5]], "binder": [m[6]], "wrapper": [m[7]]}}


def cache_path(url):
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    return CACHE / f"{slug}.html"


def fetch(urls):
    CACHE.mkdir(parents=True, exist_ok=True)
    todo = [u for u in urls if not cache_path(u).exists()]
    print(f"страниц в кэше: {len(urls) - len(todo)}, скачать: {len(todo)}", flush=True)
    fails = []
    for n, u in enumerate(todo, 1):
        if n > 1:
            time.sleep(PAUSE)
        r = subprocess.run(["curl", "-s", "-m", "40", "-A", UA, "-w", "\n%{http_code}", u],
                           capture_output=True, text=True)
        body, _, code = r.stdout.rpartition("\n")
        if code != "200" or "__SSG_CIGAR_DATA__" not in body:
            fails.append((u, code))
            print(f"[{n}/{len(todo)}] {code} {u}", flush=True)
            if code in ("429", "403", "503"):
                print("сайт просит притормозить — останавливаюсь", flush=True)
                break
            continue
        cache_path(u).write_text(body, encoding="utf-8")
        if n % 20 == 0:
            print(f"[{n}/{len(todo)}] ok", flush=True)
    return fails


def countries(raw, part):
    codes = (raw.get("countryKeys") or {}).get(part) or []
    if codes:
        return [COUNTRY.get(c, c) for c in codes]
    names = (raw.get("countryNames") or {}).get(part) or []
    return [{"Cuba": "Куба"}.get(x, x) for x in names]


def build():
    targets = json.loads(TARGETS.read_text(encoding="utf-8"))["cigars"]
    prices = json.loads(PRICES.read_text(encoding="utf-8"))["cigars"]
    out, missing = {}, []
    for cid, t in targets.items():
        url = t.get("cigardome")
        ref = (prices.get(cid) or {}).get("reference") or {}
        specs = ref.get("specs") or {}
        raw = None
        if url and cache_path(url).exists():
            raw = parse_page(cache_path(url).read_text(encoding="utf-8"))
        if raw is None:
            missing.append(cid)
            raw = {}
        length = raw.get("length")
        if not length:
            m = re.match(r"(\d+)", str(specs.get("Length") or ""))
            length = int(m[1]) if m else None
        ring = raw.get("ringGauge") or specs.get("Ring Gauge")
        fp = raw.get("flavorProfile") or {}
        out[cid] = {
            "name": t["name"],
            "brand": ref.get("brand") or raw.get("brand"),
            "country": ref.get("country"),
            "format": (raw.get("type") or "").strip() or None,
            "length_mm": int(length) if length else None,
            "ring": int(ring) if ring else None,
            "smoke_min": raw.get("smokeTime") or None,
            "strength": strength_num(raw.get("strength"), specs.get("Strength")),
            "strength_text": specs.get("Strength"),
            "filler": countries(raw, "filler"),
            "binder": countries(raw, "binder"),
            "wrapper": countries(raw, "wrapper"),
            "notes": notes_from_text(ref.get("description_en")),
            "community_flavor": fp,
            "community_flavor_reviews": (raw.get("reviewsCount") or {}).get("flavour", 0),
            "source": raw.get("source"),
        }
    doc = {"generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "source": "cigardome.com (__SSG_CIGAR_DATA__) + description_en из prices.json",
           "note_labels": {k: v[0] for k, v in NOTES.items()},
           "cigars": out}
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return doc, missing


def summary(doc, missing):
    cs = doc["cigars"]
    n = len(cs)
    cnt = lambda f: sum(1 for c in cs.values() if f(c))
    print(f"\nсигар: {n}; страница не разобрана: {len(missing)} {missing[:10]}")
    print(f"с форматом: {cnt(lambda c: c['format'])}")
    print(f"с длиной и толщиной: {cnt(lambda c: c['length_mm'] and c['ring'])}")
    print(f"со странами всех трёх листов: {cnt(lambda c: c['filler'] and c['binder'] and c['wrapper'])}")
    print(f"со временем курения: {cnt(lambda c: c['smoke_min'])}")
    print(f"с крепостью: {cnt(lambda c: c['strength'])}")
    print(f"с нотами: {cnt(lambda c: c['notes'])}; в среднем нот: "
          f"{sum(len(c['notes']) for c in cs.values()) / max(1, n):.1f}")
    print(f"с отзывами о вкусе ≥3: {cnt(lambda c: c['community_flavor_reviews'] >= 3)}")
    print("\nформаты:", collections.Counter(c["format"] for c in cs.values()).most_common(15))
    print("крепость:", sorted(collections.Counter(c["strength"] for c in cs.values()).items(), key=str))
    print("покровный лист:", collections.Counter("/".join(c["wrapper"]) for c in cs.values()).most_common(10))
    print("\nчастота нот (доля сигар):")
    nc = collections.Counter(k for c in cs.values() for k in c["notes"])
    for k, v in nc.most_common():
        print(f"  {k:8} {doc['note_labels'][k]:16} {v:4}  {v / n:4.0%}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="скачать недостающие страницы")
    a = ap.parse_args()
    if a.fetch:
        targets = json.loads(TARGETS.read_text(encoding="utf-8"))["cigars"]
        fails = fetch([t["cigardome"] for t in targets.values() if t.get("cigardome")])
        print(f"не скачано: {len(fails)}", fails[:10])
    doc, missing = build()
    summary(doc, missing)


if __name__ == "__main__":
    sys.exit(main())
