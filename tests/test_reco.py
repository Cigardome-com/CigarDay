#!/usr/bin/env python3
"""Тесты подбора для тестового бота CigarDay (cigarday/reco.py).

Запуск: python3.13 tests/test_reco.py   (без сети, на выдуманном каталоге —
живой prices.json меняет наличие трижды в день, тест на нём мигал бы)
"""
import datetime
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "cigarday"))

import reco as R

failed = 0
TODAY = datetime.date(2026, 11, 3)


def check(label, cond):
    global failed
    print(("OK  " if cond else "FAIL"), label)
    failed += 0 if cond else 1


def cig(cid, brand, price, fmt, length, ring, strength, leaves, notes, in_stock=True):
    w, b, f = leaves
    return {"cid": cid, "name": cid.replace("-", " ").title(), "brand": brand, "image": None,
            "price": price, "url": None, "in_stock": in_stock, "format": fmt,
            "length_mm": length, "ring": ring, "smoke_min": 45, "strength": strength,
            "wrapper": [w], "binder": [b], "filler": [f], "notes": dict.fromkeys(notes, 1)}


CU = ("Куба", "Куба", "Куба")
NI = ("Никарагуа", "Никарагуа", "Никарагуа")
EC = ("Эквадор", "Никарагуа", "Никарагуа")
COMMON = ["wood", "sweet"]          # есть почти у всех — по ним не различаем

CAT = {c["cid"]: c for c in [
    # Купленные
    cig("cu-robusto", "Alpha", 3000, "Robusto", 124, 50, 3, CU, COMMON + ["coffee", "cedar"]),
    cig("ni-full", "Beta", 3000, "Toro", 152, 52, 5, NI, COMMON + ["pepper", "leather", "earth"]),
    # Похожие на кубинскую робусту
    cig("cu-robusto-2", "Gamma", 3200, "Robusto", 124, 50, 3, CU, COMMON + ["coffee", "cedar"]),
    cig("cu-robusto-3", "Gamma", 3100, "Robusto", 127, 50, 3, CU, COMMON + ["coffee", "cedar", "cream"]),
    cig("cu-robusto-pricey", "Delta", 9000, "Robusto", 124, 50, 3, CU, COMMON + ["coffee", "cedar"]),
    cig("cu-robusto-gone", "Epsilon", 3000, "Robusto", 124, 50, 3, CU, COMMON + ["coffee", "cedar"],
        in_stock=False),
    cig("cu-corona", "Zeta", 2800, "Corona", 142, 42, 3, CU, COMMON + ["coffee", "cream"]),
    # Похожие на никарагуанский полнотелый
    cig("ni-full-2", "Eta", 3100, "Toro", 152, 52, 5, NI, COMMON + ["pepper", "leather", "earth"]),
    cig("ni-full-3", "Theta", 2900, "Toro", 150, 54, 4, EC, COMMON + ["pepper", "leather"]),
    # Прочее
    cig("mild-cream", "Iota", 3000, "Corona", 140, 42, 1, EC, COMMON + ["cream", "floral"]),
    cig("fruit-mid", "Kappa", 2700, "Panatela", 170, 38, 2, ("Доминикана",) * 3, COMMON + ["fruit"]),
]}

H_ONE = [{"cid": "cu-robusto", "date": "2026-10-01", "qty": 5, "price_per_stick": 3000}]
H_TWO = H_ONE + [{"cid": "ni-full", "date": "2026-09-15", "qty": 5, "price_per_stick": 3000}]


def sug_ids(res):
    return [s["item"]["cid"] for s in res["suggestions"]]


def rank(history, ratings, cid):
    idf = R.note_idf(CAT)
    anc = R.anchors(history, ratings, CAT, TODAY)
    return R.score_candidate(CAT[cid], anc, CAT, idf)[0]


# 1. Цена в коридоре
res = R.recommend(H_ONE, {}, CAT, TODAY)
ok = all(abs(s["item"]["price"] / res["price"] - 1) <= res["band"] + 1e-9 for s in res["suggestions"])
check("цена каждой «похожей» в коридоре от обычной цены клиента", ok and res["price"] == 3000)
check("дорогая копия по вкусу (9 000 ₽ при обычных 3 000) не предлагается",
      "cu-robusto-pricey" not in sug_ids(res))

# 2. Купленное не повторяется в «похожих»
res = R.recommend(H_TWO, {}, CAT, TODAY)
check("купленное не попадает в «Вам может понравиться»",
      not ({"cu-robusto", "ni-full"} & set(sug_ids(res))))
check("и не дублируется между разделами",
      not ({f["item"]["cid"] for f in res["favorites"]} & set(sug_ids(res))))

# 3. Отсутствующее не предлагается
check("нет в наличии — не предлагается", "cu-robusto-gone" not in sug_ids(res))
gone_fav = [{"cid": "cu-robusto-gone", "date": "2026-10-20", "qty": 3, "price_per_stick": 3000}]
r2 = R.recommend(H_ONE + gone_fav, {}, CAT, TODAY)
check("любимая, которой нет в наличии, не показана в «любимых», а названа отдельно",
      "cu-robusto-gone" not in [f["item"]["cid"] for f in r2["favorites"]]
      and "cu-robusto-gone" in [x["cid"] for x in r2["favorites_absent"]])

# 4. Разнообразие брендов
brands = [s["item"]["brand"] for s in res["suggestions"]]
check("не больше одной сигары на бренд", len(brands) == len(set(brands)) and len(brands) == 3)
check("у бренда Gamma две похожие сигары, в подборке — одна",
      sum(1 for b in brands if b == "Gamma") <= 1)

# 5. Оценка 1–2 убирает из «любимых» и понижает похожие
base = R.recommend(H_TWO, {}, CAT, TODAY)
low = R.recommend(H_TWO, {"ni-full": 1}, CAT, TODAY)
check("без оценки ni-full в «любимых»", "ni-full" in [f["item"]["cid"] for f in base["favorites"]])
check("оценка 1 убирает ni-full из «любимых»", "ni-full" not in [f["item"]["cid"] for f in low["favorites"]])
check("оценка 1 понижает похожую на неё ni-full-2",
      rank(H_TWO, {"ni-full": 1}, "ni-full-2") < rank(H_TWO, {}, "ni-full-2"))
check("и после оценки 1 похожие на нелюбимую уходят из подборки",
      "ni-full-2" in sug_ids(base) and "ni-full-2" not in sug_ids(low))
check("оценка 2 тоже убирает из «любимых»",
      "ni-full" not in [f["item"]["cid"] for f in R.recommend(H_TWO, {"ni-full": 2}, CAT, TODAY)["favorites"]])

H_CU = H_ONE + [{"cid": "cu-corona", "date": "2026-09-01", "qty": 2, "price_per_stick": 2800}]
r_mix = R.recommend(H_CU, {"cu-robusto": 5, "cu-corona": 1}, CAT, TODAY)
check("двойка у одной кубинской не гасит соседнюю, если она ближе к любимой (5)",
      "cu-robusto-2" in sug_ids(r_mix) or "cu-robusto-3" in sug_ids(r_mix))

# 6. Оценка 5 поднимает похожие
check("оценка 5 поднимает похожую ni-full-2 (один якорь)",
      rank(H_TWO[1:], {"ni-full": 5}, "ni-full-2") > rank(H_TWO[1:], {}, "ni-full-2"))
check("оценка 5 у ni-full при двух покупках поднимает её похожих выше похожих на робусту",
      sug_ids(R.recommend(H_TWO, {"ni-full": 5}, CAT, TODAY))[0] in ("ni-full-2", "ni-full-3"))
r5 = R.recommend(H_TWO, {"ni-full": 5}, CAT, TODAY)
check("причина ссылается на оценку: «…, которой вы поставили 5»",
      "которой вы поставили 5" in r5["suggestions"][0]["reason"])
check("оценённая на 5 стоит первой в «любимых»", r5["favorites"][0]["item"]["cid"] == "ni-full")

# 7. Нейтральная 3 — не убирает и не штрафует
r3 = R.recommend(H_TWO, {"ni-full": 3}, CAT, TODAY)
check("оценка 3 оставляет сигару в «любимых»", "ni-full" in [f["item"]["cid"] for f in r3["favorites"]])

# 8. Только нелюбимые — подборки нет, а не случайные сигары
r0 = R.recommend(H_ONE, {"cu-robusto": 1}, CAT, TODAY)
check("если всё куплено оценено на 1 — подборка пустая", r0["suggestions"] == [] and r0["favorites"] == [])

# 9. Причина человеческим языком
r = R.recommend(H_ONE, {"cu-robusto": 5}, CAT, TODAY)
reason = r["suggestions"][0]["reason"]
check(f"причина: «{reason}»", "тот же формат Robusto" in reason and "ноты" in reason)
check("у кубинских не пишем «покровный лист из Кубы, как у неё» — это у всех кубинских",
      all("покровный" not in s["reason"] for s in r["suggestions"] if s["item"]["wrapper"] == ["Куба"]))

# 10. Цена в истории как сумма строки
hist_sum = [{"cid": "cu-robusto", "date": "2026-10-01", "qty": 5, "price_per_stick": 15000}]
check("цена строки 15 000 за 5 штук понята как 3 000 за штуку",
      R.usual_price(hist_sum, CAT) == 3000)

# 11. Похожесть: признак, которого нет, не считается «непохоже»
a = dict(CAT["cu-robusto"], format=None)
check("без формата у одной сигары сходство не падает до нуля",
      R.similarity(a, CAT["cu-robusto-2"], R.note_idf(CAT)) > 0.9)

print(f"\n{'ВСЁ ОК' if not failed else f'ОШИБОК: {failed}'}")
sys.exit(1 if failed else 0)
