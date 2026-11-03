#!/usr/bin/env python3
"""Подбор сигар для тестового бота CigarDay: «Ваши любимые в наличии» и
«Вам может понравиться», с учётом оценок покупок (1–5 звёзд).

Чистая логика: без сети, без scrape.py. На вход —
  catalog  {cid: item} из build_catalog(prices, profiles) — только то, что
           знает CigarIndex о сигаре и о предложении CigarDay;
  history  [{"cid", "date" (YYYY-MM-DD), "qty", "price_per_stick"}] —
           уже сопоставленные с каталогом покупки (см. reco_match.py);
  ratings  {cid: 1..5}.

Почему похожесть по нескольким признакам. Cigardome даёт только СТРАНУ
листа, не сорт (Connecticut / Maduro / Habano). У кубинских все три листа
«Куба», а это больше половины того, что CigarDay держит в наличии, — по
одному «составу» их не различить. Поэтому сравниваем крепость, формат,
размер, страны листов и вкусовые ноты, а цену держим коридором.

Вкусовые ноты взвешиваются по редкости (IDF): «дерево» и «сладость»
встречаются в описании почти каждой сигары и ничего не различают, а «кожа»
или «перец» — различают.

Оценки:
  5, 4 — сигара «любимая», её признаки весят больше (вес якоря 3 и 2);
  3    — нейтрально (вес 0.5: известно, что «так себе», но не плохо);
  2, 1 — в «любимых» не показываем, похожие на неё штрафуем;
  без оценки — вес по частоте и свежести покупок (до 1).
"""
import datetime
import math

RETAILER = "cigarday.ru"
PRICE_BAND = 0.30            # коридор цены: ±30% от обычной цены клиента
PRICE_BAND_WIDE = 0.50       # если в узком коридоре пусто — шире, и честно об этом
HALF_LIFE_DAYS = 180         # свежесть покупки: вес вдвое меньше через полгода
RATING_WEIGHT = {5: 3.0, 4: 2.0, 3: 0.5}
DISLIKE_WEIGHT = {2: 0.6, 1: 1.0}
DISLIKE_PENALTY = 1.2        # насколько сильно тянет вниз сходство с нелюбимой
SIM_FLOOR = 0.35             # ниже этого сходство считаем «случайным», не похожестью

FEATURE_W = {"strength": 2.0, "format": 1.2, "size": 1.3, "leaves": 1.5, "notes": 2.5}

NOTE_GEN = {   # родительный падеж для «ноты кофе и кедра»
    "coffee": "кофе", "cocoa": "какао", "cedar": "кедра", "wood": "дерева",
    "nuts": "орехов", "leather": "кожи", "spice": "пряностей", "pepper": "перца",
    "earth": "земли", "cream": "сливок", "fruit": "фруктов", "sweet": "мёда",
    "floral": "цветов", "herb": "трав", "toast": "поджаренного хлеба",
}
STRENGTH_RU = {1: "мягкая", 2: "мягкая-средняя", 3: "средняя", 4: "средняя-полная", 5: "полная"}
COUNTRY_GEN = {"Куба": "Кубы", "Доминикана": "Доминиканы", "Никарагуа": "Никарагуа",
               "Гондурас": "Гондураса", "Эквадор": "Эквадора", "США": "США",
               "Индонезия": "Индонезии", "Бразилия": "Бразилии", "Мексика": "Мексики",
               "Камерун": "Камеруна", "Коста-Рика": "Коста-Рики", "Панама": "Панамы",
               "Перу": "Перу", "Колумбия": "Колумбии"}
MONTH_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
             "августа", "сентября", "октября", "ноября", "декабря"]


# ── каталог ────────────────────────────────────────────────────────────

def build_catalog(prices, profiles, retailer=RETAILER):
    """{cid: item}. Предложение магазина берётся одно (у CigarDay так и есть);
    «в наличии» — только если in_stock is True и есть цена за штуку: у части
    строк есть лишь цена коробки, и показывать её как цену штуки нельзя."""
    cat = {}
    for cid, c in prices["cigars"].items():
        ref = c.get("reference") or {}
        prof = (profiles.get("cigars") or profiles).get(cid) or {}
        offer = next((o for o in c.get("offers") or [] if o.get("retailer") == retailer), None)
        pps = (offer or {}).get("price_per_stick")
        cat[cid] = {
            "cid": cid,
            "name": c.get("name") or prof.get("name") or cid,
            "brand": ref.get("brand") or prof.get("brand") or "",
            "image": ref.get("image"),
            "price": pps,
            "url": (offer or {}).get("url"),
            "in_stock": bool(offer and offer.get("in_stock") is True and pps),
            "format": prof.get("format"),
            "length_mm": prof.get("length_mm"),
            "ring": prof.get("ring"),
            "smoke_min": prof.get("smoke_min"),
            "strength": prof.get("strength"),
            "filler": prof.get("filler") or [],
            "binder": prof.get("binder") or [],
            "wrapper": prof.get("wrapper") or [],
            "notes": prof.get("notes") or {},
        }
    return cat


def note_idf(catalog):
    n = max(1, len(catalog))
    df = {}
    for it in catalog.values():
        for k in it["notes"]:
            df[k] = df.get(k, 0) + 1
    return {k: math.log((n + 1) / (v + 1)) + 0.1 for k, v in df.items()}


def _note_vec(it, idf):
    # Число упоминаний — слабый признак (длинное описание ≠ сильная нота),
    # поэтому логарифм, а основной вес — редкость ноты.
    return {k: idf.get(k, 0.1) * (1 + math.log(c)) for k, c in it["notes"].items()}


def _cos(a, b):
    num = sum(v * b.get(k, 0) for k, v in a.items())
    den = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
    return num / den if den else 0.0


def _jacc(a, b):
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a and b else None


# ── похожесть ──────────────────────────────────────────────────────────

def similarity(a, b, idf):
    """0..1. Признак, которого нет у одной из сигар, в сравнение не входит
    (а не считается за «непохоже»)."""
    parts = {}
    if a["strength"] and b["strength"]:
        parts["strength"] = 1 - abs(a["strength"] - b["strength"]) / 4
    if a["format"] and b["format"]:
        parts["format"] = 1.0 if a["format"].lower() == b["format"].lower() else 0.0
    if a["length_mm"] and b["length_mm"] and a["ring"] and b["ring"]:
        parts["size"] = math.exp(-abs(a["length_mm"] - b["length_mm"]) / 25) * \
                        math.exp(-abs(a["ring"] - b["ring"]) / 6)
    leaves = [(w, _jacc(a[p], b[p])) for p, w in (("wrapper", 0.5), ("binder", 0.2), ("filler", 0.3))]
    leaves = [(w, j) for w, j in leaves if j is not None]
    if leaves:
        parts["leaves"] = sum(w * j for w, j in leaves) / sum(w for w, _ in leaves)
    if a["notes"] and b["notes"]:
        parts["notes"] = _cos(_note_vec(a, idf), _note_vec(b, idf))
    if not parts:
        return 0.0
    return sum(FEATURE_W[k] * v for k, v in parts.items()) / sum(FEATURE_W[k] for k in parts)


def _sharp(s):
    """Сходство выше «фона»: почти любые две сигары похожи на 0.3–0.4 хотя бы
    крепостью, и без этой поправки штраф за нелюбимую задевал бы всех."""
    return max(0.0, (s - SIM_FLOOR) / (1 - SIM_FLOOR))


# ── вкус клиента ───────────────────────────────────────────────────────

def _date(s):
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def purchase_stats(history):
    """{cid: {"times", "qty", "last"}}."""
    st = {}
    for h in history:
        cid = h.get("cid")
        if not cid:
            continue
        x = st.setdefault(cid, {"times": 0, "qty": 0, "last": None})
        x["times"] += 1
        x["qty"] += int(h.get("qty") or 1)
        d = _date(h.get("date"))
        if d and (x["last"] is None or d > x["last"]):
            x["last"] = d
    return st


def habit_weight(stat, today):
    """Вес покупки без оценки: частота × свежесть, не больше 1."""
    freq = min(1.0, 0.45 + 0.25 * math.log(1 + stat["times"]) + 0.1 * math.log(1 + stat["qty"]))
    if stat["last"]:
        age = max(0, (today - stat["last"]).days)
        fresh = 0.5 ** (age / HALF_LIFE_DAYS)
    else:
        fresh = 0.5
    return freq * (0.4 + 0.6 * fresh)


def anchors(history, ratings, catalog, today):
    """Якоря вкуса: [(cid, вес, оценка или None)]. Вес < 0 — нелюбимая."""
    out = []
    for cid, stat in purchase_stats(history).items():
        if cid not in catalog:
            continue
        r = ratings.get(cid)
        if r in DISLIKE_WEIGHT:
            out.append((cid, -DISLIKE_WEIGHT[r], r))
        elif r in RATING_WEIGHT:
            out.append((cid, RATING_WEIGHT[r], r))
        else:
            out.append((cid, habit_weight(stat, today), None))
    return out


def usual_price(history, catalog):
    """Обычная цена штуки для клиента — медиана по штукам. Если в истории
    цена похожа на сумму строки (в разы выше цены каталога), делим на
    количество."""
    pts = []
    for h in history:
        p, q = h.get("price_per_stick") or h.get("price"), int(h.get("qty") or 1)
        if not p:
            p = (catalog.get(h.get("cid")) or {}).get("price")
            if not p:
                continue
        ref = (catalog.get(h.get("cid")) or {}).get("price")
        if q > 1 and ref and p / q > 0 and abs(math.log(p / q / ref)) < abs(math.log(p / ref)):
            p = p / q
        pts += [p] * max(1, q)
    if not pts:
        return None
    pts.sort()
    m = len(pts) // 2
    return pts[m] if len(pts) % 2 else (pts[m - 1] + pts[m]) / 2


# ── подборка ───────────────────────────────────────────────────────────

def score_candidate(cand, anc, catalog, idf):
    """(балл, лучший положительный якорь, сходство с ним, худший нелюбимый)."""
    pos_w = sum(w for _, w, _ in anc if w > 0)
    pos = 0.0
    best = (None, 0.0, 0.0)            # cid, вклад, сходство
    neg = 0.0
    worst = None
    sims = [(cid, w, similarity(cand, catalog[cid], idf)) for cid, w, _ in anc]
    # Штраф — за то, насколько кандидат ближе к нелюбимой, чем к самой
    # похожей любимой. Иначе «2» у одной кубинской робусты гасила бы все
    # кубинские робусты, даже если соседней такой же человек поставил 5:
    # по признакам они почти одинаковы, и различает их только вкус.
    near_love = max((_sharp(s) for _, w, s in sims if w >= 1), default=0.0)
    for cid, w, s in sims:
        if w > 0:
            pos += w * s
            if w * s > best[1]:
                best = (cid, w * s, s)
        else:
            pen = -w * max(0.0, _sharp(s) - near_love)
            if pen > neg:
                neg, worst = pen, cid
    # Деление на (сумма весов + 1), а не на сумму: иначе у клиента с одной
    # покупкой оценка 5 ничего бы не меняла — среднее из одного якоря равно
    # самому сходству при любом весе.
    score = pos / (pos_w + 1) - DISLIKE_PENALTY * neg
    return score, best[0], best[2], worst


def favorites(history, ratings, catalog, today, limit=3):
    """Любимые в наличии + список любимых, которых сейчас нет."""
    stats = purchase_stats(history)
    rows = []
    for cid, st in stats.items():
        if cid not in catalog or ratings.get(cid) in DISLIKE_WEIGHT:
            continue
        r = ratings.get(cid)
        # 5 и 4 — впереди, затем без оценки по привычке, нейтральные 3 — в конце.
        tier = {5: 0, 4: 1, None: 2, 3: 3}.get(r, 3)
        rows.append((tier, -habit_weight(st, today), cid))
    rows.sort()
    shown, absent = [], []
    for _, _, cid in rows:
        it = catalog[cid]
        if it["in_stock"]:
            if len(shown) < limit:
                shown.append({"item": it, "rating": ratings.get(cid), "stat": stats[cid],
                              "reason": favorite_reason(ratings.get(cid), stats[cid])})
        elif ratings.get(cid) in (4, 5) or ratings.get(cid) is None:
            absent.append(it)
    return shown, absent


def suggestions(history, ratings, catalog, today, limit=3):
    """«Вам может понравиться»: не купленное, в наличии у CigarDay, цена в
    коридоре, не больше одной сигары на бренд."""
    idf = note_idf(catalog)
    anc = anchors(history, ratings, catalog, today)
    if not any(w > 0 for _, w, _ in anc):
        return [], {"price": None, "band": None, "wide": False}
    bought = {h["cid"] for h in history if h.get("cid")}
    price = usual_price(history, catalog)
    stats = purchase_stats(history)
    top_times = max((x["times"] for x in stats.values()), default=0)
    if sum(1 for x in stats.values() if x["times"] == top_times) > 1:
        top_times = -1          # «чаще других» при равенстве — неправда
    scored = []
    for cid, it in catalog.items():
        if cid in bought or not it["in_stock"]:
            continue
        sc, best, bsim, worst = score_candidate(it, anc, catalog, idf)
        scored.append((sc, cid, best, bsim, worst))
    scored.sort(key=lambda x: (-x[0], x[1]))

    def pick(band):
        lo, hi = (price * (1 - band), price * (1 + band)) if price else (0, float("inf"))
        out, brands = [], set()
        for sc, cid, best, bsim, worst in scored:
            it = catalog[cid]
            if not (lo <= it["price"] <= hi):
                continue
            b = (it["brand"] or cid).lower()
            if b in brands:
                continue
            brands.add(b)
            out.append({"item": it, "score": sc, "anchor": best, "anchor_sim": bsim,
                        "reason": suggestion_reason(it, catalog.get(best), ratings.get(best),
                                                    stats.get(best), idf, price,
                                                    top=stats.get(best, {}).get("times") == top_times)})
            if len(out) == limit:
                break
        return out

    out, wide = pick(PRICE_BAND), False
    if len(out) < limit and price:
        out, wide = pick(PRICE_BAND_WIDE), True
    return out, {"price": price, "band": PRICE_BAND_WIDE if wide else PRICE_BAND, "wide": wide}


def recommend(history, ratings, catalog, today=None):
    today = today or datetime.date.today()
    fav, absent = favorites(history, ratings, catalog, today)
    sug, meta = suggestions(history, ratings, catalog, today)
    return {"favorites": fav, "favorites_absent": absent, "suggestions": sug, **meta}


# ── причины человеческим языком ────────────────────────────────────────

def _when(d):
    return f"{MONTH_GEN[d.month - 1]} {d.year}" if d else ""


def favorite_reason(rating, stat):
    if rating in (4, 5):
        return f"вы поставили {rating}"
    if stat["times"] >= 2:
        s = f"брали {stat['times']} раза" if stat["times"] < 5 else f"брали {stat['times']} раз"
        return s + (f", последний раз — в {_when_prep(stat['last'])}" if stat["last"] else "")
    return f"брали в {_when_prep(stat['last'])}" if stat["last"] else "вы её брали"


MONTH_PREP = ["январе", "феврале", "марте", "апреле", "мае", "июне", "июле",
              "августе", "сентябре", "октябре", "ноябре", "декабре"]


def _when_prep(d):
    return f"{MONTH_PREP[d.month - 1]}" + ("" if d.year == datetime.date.today().year else f" {d.year}")


def _and(xs):
    xs = list(xs)
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " и " + xs[-1]


def suggestion_reason(it, anchor, rating, stat, idf, price, top=False):
    """«похожа на Partagas Serie D No. 4, которой вы поставили 5: тот же
    формат Robusto, чуть крепче, ноты кофе и кедра»."""
    parts = []
    if anchor:
        if anchor["format"] and it["format"]:
            if anchor["format"].lower() == it["format"].lower():
                parts.append(f"тот же формат {it['format']}")
            elif anchor["length_mm"] and it["length_mm"]:
                d = it["length_mm"] - anchor["length_mm"]
                if abs(d) >= 15:
                    parts.append(f"{it['format']}, {'длиннее' if d > 0 else 'короче'}")
        if anchor["strength"] and it["strength"]:
            d = it["strength"] - anchor["strength"]
            parts.append({0: "та же крепость", 1: "чуть крепче", -1: "чуть мягче"}.get(
                d, "заметно крепче" if d > 0 else "заметно мягче"))
        common = [k for k in it["notes"] if k in anchor["notes"]]
        common.sort(key=lambda k: -min(it["notes"][k], anchor["notes"][k]) * (0.5 + idf.get(k, 0)))
        if common:
            parts.append("ноты " + _and(NOTE_GEN.get(k, k) for k in common[:2]))
        wr = set(it["wrapper"]) & set(anchor["wrapper"])
        if wr and not (set(it["wrapper"]) == {"Куба"} and set(anchor["wrapper"]) == {"Куба"}):
            c = next(iter(wr))
            parts.append(f"покровный лист из {COUNTRY_GEN.get(c, c)}, как у неё")
        if rating in (4, 5):
            head = f"похожа на {anchor['name']}, которой вы поставили {rating}"
        elif stat and stat["times"] >= 2 and top:
            head = f"похожа на {anchor['name']}, которую вы берёте чаще других"
        elif stat and stat["times"] >= 2:
            head = f"похожа на {anchor['name']}, которую вы брали {stat['times']} раз" + \
                   ("а" if 2 <= stat["times"] <= 4 else "")
        else:
            head = f"похожа на {anchor['name']} из ваших покупок"
        return head + (": " + ", ".join(parts[:3]) if parts else "")
    return ""


def strength_ru(n):
    return STRENGTH_RU.get(n, "")
