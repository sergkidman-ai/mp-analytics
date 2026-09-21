"""ops/mkt_serp_match.py — этап 2 промта: разбор «того же товара» в wb_search_snapshot (поток mkt).

Правило-эвристика по `name`, не ИИ и не догадка о факте (габарит/себест) — просто текстовый разбор
уже собранной строки, пересчитываемый без похода в ВБ (data-layer.md, принцип 1). Замер 21.09:
в выдаче по общему коду встречаются совсем не картриджи (батарейки, варочные панели, металлопрокат
совпали по числу в названии) — item_kind='other' режет их ДО любого сравнения цены/позиции.

Категории item_kind: cartridge (по умолчанию, если есть маркер расходника) / chip / drum / head /
other (маркеров расходника нет вовсе — скорее всего левый товар, в сигналы не включать).
color — только когда слово явно есть в названии (bk/c/m/y/multi), иначе NULL: не гадаем.

Запуск: ./venv/bin/python ops/mkt_serp_match.py [--limit N]   # разбирает только новые строки
"""
import argparse
import pathlib
import re
import sys

import psycopg2.extras

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

CARTRIDGE_RE = re.compile(
    r"картридж|тонер|чернил|струйн|лазерн|мфу\b|принтер|pixma|мультиупак|запр\w*карт", re.I)
CHIP_RE = re.compile(r"\bчип\w*\b", re.I)
DRUM_RE = re.compile(r"фотобарабан|драм.?юнит|\bdrum\b", re.I)
HEAD_RE = re.compile(r"печатающ\w{0,3}\s+голов", re.I)

BUNDLE_KW_RE = re.compile(r"набор|комплект|\bк-?т\b|мультиупак|_set\b", re.I)
BUNDLE_NUM_RE = [
    re.compile(r"(?:набор|комплект|к-?т)\D{0,15}?(\d{1,2})\b", re.I),
    re.compile(r"(\d{1,2})\s*шт\b", re.I),
    re.compile(r"_set\D{0,3}(\d{1,2})", re.I),
]
COLOR_MAP = [
    (re.compile(r"чёрн\w*|черн\w*|\bbk\b", re.I), "bk"),
    (re.compile(r"голуб\w*|циан\w*|\bcyan\b", re.I), "c"),
    (re.compile(r"пурпур\w*|малинов\w*|\bmagenta\b", re.I), "m"),
    (re.compile(r"жёлт\w*|желт\w*|\byellow\b", re.I), "y"),
    (re.compile(r"цветн\w*|многоцвет\w*|full\s*color", re.I), "multi"),
]
XL_RE = re.compile(r"\bxl\b|повышенн\w+\s+объ|увеличенн\w+\s+(объ|ресурс)|\bx2\b", re.I)


def item_kind_of(name):
    if CHIP_RE.search(name) and not CARTRIDGE_RE.search(name):
        return "chip"
    if DRUM_RE.search(name):
        return "drum"
    if HEAD_RE.search(name):
        return "head"
    if CARTRIDGE_RE.search(name):
        return "cartridge"
    return "other"


def bundle_qty_of(name):
    """(is_bundle, qty) — qty=None значит «бандл есть, число не нашли» (не выдумываем)."""
    is_bundle = bool(BUNDLE_KW_RE.search(name))
    best = None
    for pat in BUNDLE_NUM_RE:
        m = pat.search(name)
        if m and m.group(1):
            try:
                v = int(m.group(1))
            except (TypeError, ValueError):
                continue
            if 2 <= v <= 12:
                best = max(best or 0, v)
                is_bundle = True
    return is_bundle, best


def color_of(name):
    for pat, code in COLOR_MAP:
        if pat.search(name):
            return code
    return None


def parse_row(name):
    name = name or ""
    is_bundle, qty = bundle_qty_of(name)
    return {
        "item_kind": item_kind_of(name),
        "is_bundle": is_bundle,
        "bundle_qty": qty,
        "color": color_of(name),
        "is_xl": bool(XL_RE.search(name)),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50000, help="максимум новых строк за прогон")
    a = ap.parse_args()

    our_nm = {r["nm_id"] for r in db.query("SELECT DISTINCT nm_id FROM wb_price")}
    rows = db.query(
        "SELECT s.id, s.nm_id, s.name FROM wb_search_snapshot s "
        "LEFT JOIN wb_search_match m ON m.snapshot_id = s.id "
        "WHERE m.snapshot_id IS NULL LIMIT %s", (a.limit,))
    if not rows:
        print("новых строк нет")
        return

    out = []
    for r in rows:
        p = parse_row(r["name"])
        out.append((r["id"], p["item_kind"], p["is_bundle"], p["bundle_qty"], p["color"],
                    p["is_xl"], r["nm_id"] in our_nm))
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur, "INSERT INTO wb_search_match (snapshot_id, item_kind, is_bundle, bundle_qty, "
                     "color, is_xl, is_our) VALUES %s", out)

    kinds = {}
    for p in out:
        kinds[p[1]] = kinds.get(p[1], 0) + 1
    print(f"разобрано {len(out)}: " + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))


if __name__ == "__main__":
    main()
