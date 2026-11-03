#!/usr/bin/env python3
"""Тексты подборки для тестового бота CigarDay — карточки сигар (HTML для
Telegram) и демо-истории.

Тон — подсказка знатока, не продавец. 15-ФЗ: никаких скидок, акций,
«успейте», никакого «заказать» — цена и наличие только как информация.
Тексты перед живым запуском показать юристу CigarDay.
"""
import html

import reco as R

DEMOS = {
    "demo1": ("Кубинская классика, средняя крепость", """\
Partagas Serie D No. 4; 14.10.2026; 5; 2750
Partagas Serie D No. 4; 02.08.2026; 5; 2750
Partagas Serie D No. 4; 11.05.2026; 10; 2700
Montecristo No. 2; 20.09.2026; 3; 3250
Hoyo De Monterrey Epicure No. 2; 05.07.2026; 5; 2750
Romeo y Julieta Wide Churchills; 18.03.2026; 3; 3200
"""),
    "demo2": ("Новый Свет, покрепче", """\
My Father La Opulencia Toro; 21.10.2026; 5; 2970
My Father La Opulencia Toro; 30.08.2026; 5; 2970
Ashton VSG Especial Robusto; 12.09.2026; 3; 3230
Plasencia Alma Fuerte Robustus; 01.07.2026; 4; 2930
Camacho Connecticut Robusto; 15.06.2026; 5; 2570
Arturo Fuente Hemingway Best Seller; 10.04.2026; 2; 3080
"""),
}

NOTE_RU = {
    "coffee": "кофе", "cocoa": "какао", "cedar": "кедр", "wood": "дерево", "nuts": "орехи",
    "leather": "кожа", "spice": "пряности", "pepper": "перец", "earth": "земля",
    "cream": "сливки", "fruit": "фрукты", "sweet": "мёд", "floral": "цветы",
    "herb": "травы", "toast": "поджаренный хлеб",
}


def rub(x):
    return f"{int(round(x)):,}".replace(",", " ") + " ₽"


def dots(n):
    return "●" * n + "○" * (5 - n) if n else ""


def top_notes(it, idf, k=3):
    """Самые характерные ноты: на чём описание делает акцент (число
    упоминаний), с поправкой на редкость — «сладость» есть почти у всех."""
    order = {n: i for i, n in enumerate(it["notes"])}
    ks = sorted(order, key=lambda n: (-it["notes"][n] * (0.5 + idf.get(n, 0)), order[n]))
    return [NOTE_RU.get(n, n) for n in ks[:k]]


def card(it, why, idf, html_mode=True):
    e = html.escape if html_mode else (lambda s: s)
    b = (lambda s: f"<b>{e(s)}</b>") if html_mode else (lambda s: s.upper())
    i = (lambda s: f"<i>{e(s)}</i>") if html_mode else (lambda s: s)
    lines = [b(it["name"])]
    shape = []
    if it.get("format"):
        shape.append(it["format"])
    if it.get("length_mm") and it.get("ring"):
        shape.append(f"{it['length_mm']} мм × {it['ring']}")
    if it.get("smoke_min"):
        shape.append(f"~{it['smoke_min']} мин")
    if shape:
        lines.append(e(" · ".join(shape)))
    if it.get("strength"):
        lines.append(e(f"Крепость: {R.strength_ru(it['strength'])} {dots(it['strength'])}"))
    if it.get("wrapper") and set(it["wrapper"]) != {"Куба"}:
        lines.append(e("Листы: покровный — " + ", ".join(it["wrapper"]) +
                       "; связующий — " + ", ".join(it["binder"]) +
                       "; наполнитель — " + ", ".join(it["filler"])))
    notes = top_notes(it, idf)
    if notes:
        lines.append(e("Ноты: " + ", ".join(notes)))
    lines.append(e(f"{rub(it['price'])} за штуку · есть в CigarDay"))
    if why:
        lines.append(i("Почему: " + why))
    return "\n".join(lines)


def preview(res, catalog, ratings=None):
    """Вся подборка обычным текстом — для проверки до бота."""
    idf = R.note_idf(catalog)
    out = ["── Ваши любимые в наличии ──"]
    if res["favorites"]:
        for f in res["favorites"]:
            out += ["", card(f["item"], f["reason"], idf, html_mode=False)]
    else:
        out.append("(пока нечего показать)")
    if res["favorites_absent"]:
        out += ["", "Сейчас нет в наличии: " + ", ".join(x["name"] for x in res["favorites_absent"])]
    out += ["", "── Вам может понравиться ──"]
    if res.get("price"):
        out.append(f"(ваша обычная цена — около {rub(res['price'])} за штуку; "
                   f"подбираем в пределах ±{int(res['band'] * 100)}%"
                   + (", в узком коридоре нашлось меньше трёх" if res["wide"] else "") + ")")
    for s in res["suggestions"]:
        out += ["", card(s["item"], s["reason"], idf, html_mode=False)]
    return "\n".join(out)
