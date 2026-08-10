# поток: prc
# -*- coding: utf-8 -*-
"""
Журнал загрузок прайсов в Postgres (prc_price_load / prc_price_row).

Пишем снимок КАЖДОЙ строки прайса с решением загрузчика, а не только итоги: без этого
нельзя ни объяснить остаток задним числом, ни собрать список новинок для шага 2.
Сухой прогон тоже журналируется — с dry_run=true.
"""
from decimal import Decimal

import psycopg2.extras

from core.db import get_conn

LOAD_SQL = """
INSERT INTO prc_price_load (
    supplier_key, load_date, moment, source_file, source_kind, currency, rate, rate_date,
    rows_total, rows_loaded, rows_skipped, docs, stale_docs, card_updates, sum_rub,
    dry_run, status, error
) VALUES (
    %(supplier_key)s, %(load_date)s, %(moment)s, %(source_file)s, %(source_kind)s,
    %(currency)s, %(rate)s, %(rate_date)s, %(rows_total)s, %(rows_loaded)s, %(rows_skipped)s,
    %(docs)s, %(stale_docs)s, %(card_updates)s, %(sum_rub)s, %(dry_run)s, %(status)s, %(error)s
) RETURNING id
"""

ROW_SQL = """
INSERT INTO prc_price_row (
    load_id, row_no, article, name, stock_raw, qty, price_src, price_rub,
    ms_id, ms_name, status, reason
) VALUES %s
ON CONFLICT (load_id, row_no) DO NOTHING
"""


def _num(value):
    return None if value in (None, "") else Decimal(str(value))


def save(head, ready, skipped):
    """Записать прогон. Возвращает id записи в prc_price_load."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(LOAD_SQL, head)
            load_id = cur.fetchone()[0]

            rows = []
            for item in ready:
                rows.append((
                    load_id, item["row"], item["article"], item["name"], item["stock_raw"],
                    _num(item["qty"]), _num(item["price_raw"]),
                    Decimal(item["price_kop"]) / 100,
                    item["card"]["id"], item.get("ms_name"), "loaded", None,
                ))
            for item in skipped:
                rows.append((
                    load_id, item["row"], item["article"], item["name"], item["stock_raw"],
                    _num(item["qty"]), _num(item["price_raw"]), None,
                    None, item.get("ms_name"), "skipped", item["reason"],
                ))
            if rows:
                psycopg2.extras.execute_values(cur, ROW_SQL, rows, page_size=500)
    return load_id
