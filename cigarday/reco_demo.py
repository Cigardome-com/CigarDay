#!/usr/bin/env python3
"""Подборка текстом на живых данных — проверить подбор до бота.

  python3.13 cigarday/reco_demo.py demo1                     # демо-история
  python3.13 cigarday/reco_demo.py demo2 --rate "camacho-connecticut-robusto=1,ashton-vsg-especial-robusto=5"
  python3.13 cigarday/reco_demo.py путь/к/истории.txt [--rate ...]
"""
import argparse
import json
import pathlib
import sys

import paths  # noqa: E402
import reco as R  # noqa: E402
import reco_match as M  # noqa: E402
import reco_text as T  # noqa: E402


def load_catalog():
    prices = json.loads(paths.PRICES.read_text(encoding="utf-8"))
    prof = json.loads(paths.PROFILES.read_text(encoding="utf-8"))
    return R.build_catalog(prices, prof)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("history")
    ap.add_argument("--rate", default="", help="cid=оценка через запятую")
    a = ap.parse_args()
    text = T.DEMOS[a.history][1] if a.history in T.DEMOS else pathlib.Path(a.history).read_text(encoding="utf-8")
    cat = load_catalog()
    rows, unknown = M.parse_history(text, M.load_aliases())
    ratings = {k: int(v) for k, v in (x.split("=") for x in a.rate.split(",") if x)}
    print(f"покупок распознано: {len(rows)}; не распознано: {len(unknown)} "
          + "; ".join(u["name"] for u in unknown))
    for r in rows:
        it = cat[r["cid"]]
        print(f"  {r['name']} → {r['cid']} ({'в наличии' if it['in_stock'] else 'нет в наличии'})"
              + (f", оценка {ratings[r['cid']]}" if r["cid"] in ratings else ""))
    print()
    print(T.preview(R.recommend(rows, ratings, cat), cat))


if __name__ == "__main__":
    main()
