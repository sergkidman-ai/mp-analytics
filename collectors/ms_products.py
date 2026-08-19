"""collectors/ms_products.py — полный справочник товаров МойСклад в БД.

Тянет /entity/product (≈44k), кладёт в ms_product (buy_price = закупочная/прайс поставщика, ₽;
sale_price; article/code/external_code; archived) и ms_barcode (barcode -> ms_id).
buy_price/value в МС в копейках → ÷100. Это основа для себестоимости, поставщиков, дефицита.

ВАЖНО (19.08.2026): /entity/product БЕЗ фильтра отдаёт только НЕархивные карточки. Пока справочник
собирался одним таким проходом, архивированная человеком карточка навсегда оставалась в ms_product
живой, со старым артикулом и кодом («зомби»): 97 архивных + 158 удалённых на 44.8k живых, из-за них
сверки видели несуществующие дубли (разбор — docs/reports/prc_ms_stale_2026-08-19.md). Поэтому _write
ВСЕГДА добирает второй проход filter=archived=true и помечает архивными тех, кого нет ни в одном
проходе (значит, карточку удалили). Гейт от обрыва выкачки — см. _mark_gone.

Запуск:  ./venv/bin/python collectors/ms_products.py
"""
import os
import sys
import json
import gzip
import time
import urllib.request
import pathlib

from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
import psycopg2.extras  # noqa: E402
from core import db  # noqa: E402
from core.db import get_conn  # noqa: E402

load_dotenv(BASE_DIR / ".env")
MS = "https://api.moysklad.ru/api/remap/1.2"


def _get(path):
    tok = os.getenv("MOYSKLAD_TOKEN")
    req = urllib.request.Request(MS + path, headers={
        "Authorization": f"Bearer {tok}", "Accept-Encoding": "gzip"})
    for _ in range(5):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                d = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    d = gzip.decompress(d)
                return json.loads(d)
        except Exception as e:
            if "429" in str(e):
                time.sleep(3); continue
            raise


def _build(rows, prod_recs, bc_recs):
    """Строки /entity/product → записи ms_product и ms_barcode (аккумулируем в списки)."""
    for r in rows:
        bp = (r.get("buyPrice") or {}).get("value", 0) / 100
        sp = next((p.get("value", 0) / 100 for p in r.get("salePrices", [])), 0)
        msid = r["id"]
        prod_recs.append({
            "ms_id": msid, "name": r.get("name"), "article": r.get("article"),
            "code": r.get("code"), "external_code": r.get("externalCode"),
            "buy_price": round(bp, 2) or None, "sale_price": round(sp, 2) or None,
            "archived": bool(r.get("archived")),
        })
        for b in r.get("barcodes", []):
            for v in b.values():
                bc_recs.append({"barcode": str(v).strip(), "ms_id": msid})


def _archived_pass(prod_recs, bc_recs):
    """Второй проход по АРХИВНЫМ карточкам (в обычной выдаче их нет)."""
    off, n = 0, 0
    while True:
        j = _get(f"/entity/product?limit=1000&offset={off}&filter=archived=true")
        rows = j.get("rows", [])
        _build(rows, prod_recs, bc_recs)
        n += len(rows); off += 1000
        if len(rows) < 1000:
            break
    return n


def _mark_gone(seen_ids, n_live):
    """Кого нет ни в живой, ни в архивной выдаче — тот удалён из МС: помечаем archived.

    Гейт: если живых карточек пришло заметно меньше, чем уже числится в справочнике (обрыв
    выкачки, таймаут, урезанный список извне) — ничего не помечаем, иначе разом «похороним»
    живой каталог. Следующий полный прогон доберёт.
    """
    have = db.query("SELECT count(*) AS n FROM ms_product WHERE NOT archived")[0]["n"]
    if have and n_live < have * 0.9:
        print(f"  [ms] пометка удалённых ПРОПУЩЕНА: пришло {n_live} живых против {have} в справочнике",
              flush=True)
        return 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE TEMP TABLE _ms_seen (ms_id text PRIMARY KEY) ON COMMIT DROP")
            psycopg2.extras.execute_values(
                cur, "INSERT INTO _ms_seen (ms_id) VALUES %s ON CONFLICT DO NOTHING",
                [(i,) for i in seen_ids], page_size=5000)
            cur.execute("""UPDATE ms_product p SET archived = true
                            WHERE NOT p.archived
                              AND NOT EXISTS (SELECT 1 FROM _ms_seen s WHERE s.ms_id = p.ms_id)""")
            return cur.rowcount


def _write(prod_recs, bc_recs):
    n_live = len(prod_recs)
    n_arch = _archived_pass(prod_recs, bc_recs)
    db.upsert("ms_product", prod_recs, conflict_cols=["ms_id"],
              update_cols=["name", "article", "code", "external_code", "buy_price", "sale_price", "archived"])
    # дедуп баркодов (PK barcode)
    seen, ded = set(), []
    for b in bc_recs:
        if b["barcode"] and b["barcode"] not in seen:
            seen.add(b["barcode"]); ded.append(b)
    db.upsert("ms_barcode", ded, conflict_cols=["barcode"], update_cols=["ms_id"])
    n_gone = _mark_gone([r["ms_id"] for r in prod_recs], n_live)
    withbuy = sum(1 for r in prod_recs if r["buy_price"])
    print(f"Записано: {len(prod_recs)} товаров (живых {n_live}, архивных {n_arch}, "
          f"с закупочной {withbuy}) | баркодов {len(ded)} | помечено удалённых {n_gone}", flush=True)


def main(products=None):
    """products=None → тянем /entity/product сами (автономный запуск); иначе используем
    уже выгруженный список из moysklad.fetch_all_products (дедуп в run_daily — один запрос
    каталога на оба коллектора)."""
    prod_recs, bc_recs = [], []
    if products is not None:
        _build(products, prod_recs, bc_recs)
    else:
        off, n = 0, 0
        while True:
            j = _get(f"/entity/product?limit=1000&offset={off}")
            rows = j.get("rows", [])
            _build(rows, prod_recs, bc_recs)
            n += len(rows); off += 1000
            if n % 5000 < 1000:
                print(f"  [ms] {n} товаров…", flush=True)
            if len(rows) < 1000:
                break
    _write(prod_recs, bc_recs)


if __name__ == "__main__":
    main()
