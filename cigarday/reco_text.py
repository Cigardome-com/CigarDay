#!/usr/bin/env python3
"""Тексты подборки для тестового бота CigarDay — карточки сигар (HTML для
Telegram) и демо-истории.

Тон — подсказка знатока, не продавец. 15-ФЗ: никаких скидок, акций,
«успейте», никакого «заказать» — цена и наличие только как информация.
Тексты перед живым запуском показать юристу CigarDay.

Вёрстка карточки (подпись к фото, до 1024 знаков):

  Montecristo Edmundo
  Куба · Robusto · 137×52 мм · ~55 мин

  Крепость  ●●●●○  средняя-полная
  Ноты  какао · фрукты · мёд
  ┃ Похожа на My Father La Opulencia Toro, которую вы берёте чаще
  ┃ других: тот же формат, та же крепость, ноты какао и земли

  2 970 ₽ за штуку · есть в CigarDay
"""
import datetime
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
COUNTRY_RU = {"Cuba": "Куба", "Dominican Republic": "Доминикана", "Nicaragua": "Никарагуа",
              "Honduras": "Гондурас", "Costa Rica": "Коста-Рика", "USA": "США",
              "United States": "США", "Mexico": "Мексика", "Ecuador": "Эквадор",
              "Brazil": "Бразилия", "Panama": "Панама", "Peru": "Перу"}


def rub(x):
    return f"{int(round(x)):,}".replace(",", " ") + " ₽"


def dots(n):
    return "●" * n + "○" * (5 - n) if n else ""


def stars(n):
    return "★" * n + "☆" * (5 - n) if n else ""


def top_notes(it, idf, k=3):
    """Самые характерные ноты: на чём описание делает акцент (число
    упоминаний), с поправкой на редкость — «сладость» есть почти у всех."""
    order = {n: i for i, n in enumerate(it["notes"])}
    ks = sorted(order, key=lambda n: (-it["notes"][n] * (0.5 + idf.get(n, 0)), order[n]))
    return [NOTE_RU.get(n, n) for n in ks[:k]]


def shape_line(it):
    parts = []
    c = COUNTRY_RU.get(it.get("country") or "", it.get("country") or "")
    if c:
        parts.append(c)
    if it.get("format"):
        parts.append(it["format"])
    if it.get("length_mm") and it.get("ring"):
        parts.append(f"{it['length_mm']}\u00a0мм\u00a0×\u00a0{it['ring']}")
    if it.get("smoke_min"):
        parts.append(f"~{it['smoke_min']} мин")
    return " · ".join(parts)


def leaves_line(it):
    """Листы — только когда они что-то говорят: у кубинских все три «Куба»."""
    w, b, f = it.get("wrapper") or [], it.get("binder") or [], it.get("filler") or []
    if not w or set(w + b + f) == {"Куба"}:
        return ""
    inner = list(dict.fromkeys(b + f))
    return f"Покровный лист — {', '.join(w)}" + (f", внутри — {', '.join(inner)}" if inner else "")


def _bold_name(reason, name):
    e = html.escape(reason)
    n = html.escape(name or "")
    return e.replace(n, f"<b>{n}</b>", 1) if n and n in e else e


def card(it, why, idf, html_mode=True, anchor_name=None, rating=None, note=None):
    """Подпись к фото сигары. why — причина (строка из reco), rating —
    оценка клиента для «любимых», note — приписка (например, кого нет)."""
    if not html_mode:
        return _plain(it, why, idf)
    e = html.escape
    out = [f"<b>{e(it['name'])}</b>"]
    sh = shape_line(it)
    if sh:
        out.append(f"<i>{e(sh)}</i>")
    out.append("")
    if it.get("strength"):
        out.append(f"Крепость  {dots(it['strength'])}  {e(R.strength_ru(it['strength']))}")
    notes = top_notes(it, idf)
    if notes:
        out.append("Ноты  " + e(" · ".join(notes)))
    lv = leaves_line(it)
    if lv:
        out.append(e(lv))
    if rating in (4, 5):
        out += ["", f"<blockquote>Ваша оценка  {stars(rating)}</blockquote>"]
    elif why:
        text = why[0].upper() + why[1:]
        out += ["", f"<blockquote>{_bold_name(text, anchor_name)}</blockquote>"]
    out += ["", f"<b>{e(rub(it['price']))}</b> за штуку · есть в CigarDay"]
    if note:
        out.append(f"<i>{e(note)}</i>")
    return "\n".join(out)


def rate_card(it, stat, rating, idx, total):
    """Подпись к фото в пошаговой оценке покупок."""
    e = html.escape
    d = stat["last"].strftime("%d.%m.%Y") if stat.get("last") else ""
    if stat["times"] > 1:
        bought = f"Брали {stat['times']} раз{'а' if 2 <= stat['times'] <= 4 else ''}" + (f", последний — {d}" if d else "")
    else:
        bought = f"Брали {d}" if d else "Брали один раз"
    out = [f"<b>{e(it['name'])}</b>"]
    sh = shape_line(it)
    if sh:
        out.append(f"<i>{e(sh)}</i>")
    out += ["", e(bought)]
    out.append(f"Ваша оценка  {stars(rating)}" if rating else "<i>Как она вам? Нажмите на звезду</i>")
    if not it.get("in_stock"):
        out.append("<i>Сейчас нет в наличии в CigarDay</i>")
    out += ["", f"<i>Покупка {idx} из {total}</i>"]
    return "\n".join(out)


def _plain(it, why, idf):
    lines = [it["name"].upper()]
    sh = shape_line(it)
    if sh:
        lines.append(sh)
    if it.get("strength"):
        lines.append(f"Крепость: {R.strength_ru(it['strength'])} {dots(it['strength'])}")
    lv = leaves_line(it)
    if lv:
        lines.append(lv)
    notes = top_notes(it, idf)
    if notes:
        lines.append("Ноты: " + ", ".join(notes))
    lines.append(f"{rub(it['price'])} за штуку · есть в CigarDay")
    if why:
        lines.append("Почему: " + why)
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


def today_ru():
    return datetime.date.today().strftime("%d.%m.%Y")
