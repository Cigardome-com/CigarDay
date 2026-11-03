"""Где что лежит.

Бот живёт в своём репозитории, а цены, наличие и алиасы названий берёт у
CigarIndex: на сервере-сборщике это /opt/cigarindex — ночной сбор и дневная
перепроверка наличия обновляют там data/prices.json. Бот туда ничего не
пишет. Локально путь задаётся переменной CIGARINDEX_ROOT.
"""
import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CIGARINDEX = pathlib.Path(os.environ.get("CIGARINDEX_ROOT", "/opt/cigarindex"))
PRICES = CIGARINDEX / "data" / "prices.json"
TARGETS = CIGARINDEX / "data" / "targets.json"
PROFILES = REPO / "data" / "cigar_profile.json"


def cigarindex_src_on_path():
    """scrape.match_listing — сопоставление названий по алиасам CigarIndex."""
    p = str(CIGARINDEX / "src")
    if p not in sys.path:
        sys.path.insert(0, p)
