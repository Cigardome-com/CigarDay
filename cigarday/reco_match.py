#!/usr/bin/env python3
"""История покупок текстом → строки, привязанные к каталогу CigarIndex.

Отдельно от reco.py, чтобы логика подбора и её тесты не тянули scrape.py.
Сопоставление названий — существующим scrape.match_listing (алиасы из
data/targets.json CigarIndex, алиас = окончание названия). match_judge не вызываем.

Формат строки истории (разделитель ; | или табуляция):
  название; дата; количество; цена
  Partagas Serie D No 4; 12.09.2026; 5; 2970
Дата — ДД.ММ.ГГГГ или ГГГГ-ММ-ДД. Цена — за штуку или за всю строку
(reco.usual_price разберётся по цене каталога). Количество и цена
необязательны.
"""
import datetime
import json
import pathlib
import re
import sys

import paths  # noqa: E402

paths.cigarindex_src_on_path()
import scrape as S  # noqa: E402


def load_aliases(targets_path=paths.TARGETS):
    t = json.loads(pathlib.Path(targets_path).read_text(encoding="utf-8"))["cigars"]
    return {cid: [c["name"]] + list(c.get("aliases") or []) for cid, c in t.items()}


def match_name(name, aliases):
    """cid или None. Подходит несколько сигар (обычная и Tubos, короткое и
    длинное имя) — берём ту, чей совпавший алиас длиннее: он точнее."""
    clean = re.sub(r"\([^)]*\)", " ", name).strip()
    row = [{"name": clean, "price_per_stick": 1, "in_stock": True}]
    norm = S._norm_key(clean)
    norm_nt = S.TUBOS_SUFFIX.sub("", norm)
    best = None
    for cid, al in aliases.items():
        if not S.match_listing(row, al):
            continue
        spec = max((len(S._norm_key(a)) for a in al
                    if S._norm_key(a) and (norm.endswith(S._norm_key(a)) or norm_nt.endswith(S._norm_key(a)))),
                   default=0)
        if best is None or spec > best[0]:
            best = (spec, cid)
    return best[1] if best else None


def _date(s):
    s = s.strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y", "%d/%m/%Y"):
        try:
            return datetime.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _num(s):
    s = re.sub(r"[^\d.,]", "", s or "").replace(",", ".")
    try:
        return float(s) if s else None
    except ValueError:
        return None


def parse_history(text, aliases):
    """(строки, нераспознанные). Нераспознанные возвращаем явно — молча их
    терять нельзя: клиент увидит, что его покупка «пропала»."""
    rows, unknown = [], []
    for line in (text or "").splitlines():
        line = line.strip().strip("•-–— ").strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in re.split(r"\s*[;|\t]\s*", line)]
        name = parts[0]
        date = qty = price = None
        for p in parts[1:]:
            if date is None and _date(p):
                date = _date(p)
            elif qty is None and re.fullmatch(r"\d{1,3}\s*(шт\.?)?", p):
                qty = int(re.match(r"\d+", p)[0])
            elif price is None and _num(p):
                price = _num(p)
        cid = match_name(name, aliases)
        rec = {"name": name, "cid": cid, "date": date, "qty": qty or 1, "price_per_stick": price}
        (rows if cid else unknown).append(rec)
    return rows, unknown
