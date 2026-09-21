#!/usr/bin/env python3
# поток: mkt
"""ops/mkt_serp_signals.py — этап 3 промта: сигналы из выдачи ВБ, сводка раз в неделю в PRC-бот.

Три сигнала (docs/prompts/mkt_wb_scraper_start.md, этап 3):
  A. где мы одни или почти одни с товаром в наличии (≤1 конкурента в наличии по запросу/региону);
  B. у кого из конкурентов пропало наличие или упала позиция — сравнение последнего снимка
     с предыдущим ПО ТОМУ ЖЕ запросу/региону (пока раз в сутки — «время суток» из промта не
     меряем, крон один раз в день, см. открытый пункт в BRIEF_MKT);
  C. где наша позиция хуже по региону — сравнение внутри последнего снимка между регионами.
Только item_kind IN ('cartridge','drum','head') (этап 2, wb_search_match) — 'other' и 'chip'
в сигналы не идут, иначе шум (батарейки/чипы под общим кодом запроса).

Данных пока мало (крон почти не успел накопить историю) — сигналы B/C нормально дают пусто,
это не баг. Отправка — тем же tg(), что у sторожа маржи (ops/mkt_margin_watch), бот @ds_prc_bot.

    ./venv/bin/python -m ops.mkt_serp_signals --dry     # печать сводки, без телеграма
    ./venv/bin/python -m ops.mkt_serp_signals           # боевой прогон (крон, раз в неделю)
"""
import argparse
import sys

sys.path.insert(0, "/opt/mp-analytics")
from core.db import query  # noqa: E402
from ops.mkt_margin_watch import tg  # noqa: E402

RELEVANT_KINDS = ("cartridge", "drum", "head")


def signal_we_alone():
    rows = query("""
        WITH latest AS (
            SELECT DISTINCT ON (query, region) query, region, captured_at
            FROM wb_search_snapshot ORDER BY query, region, captured_at DESC
        ),
        comp AS (
            SELECT s.query, s.region, s.is_our_flag, s.in_stock
            FROM (
                SELECT sn.query, sn.region, sn.captured_at, sn.in_stock, m.is_our AS is_our_flag
                FROM wb_search_snapshot sn
                JOIN wb_search_match m ON m.snapshot_id = sn.id
                WHERE m.item_kind = ANY(%s)
            ) s
            JOIN latest l ON l.query = s.query AND l.region = s.region AND l.captured_at = s.captured_at
        )
        SELECT query, region,
               count(*) FILTER (WHERE NOT is_our_flag AND in_stock) AS competitors_in_stock
        FROM comp
        GROUP BY query, region
        HAVING bool_or(is_our_flag AND in_stock)
           AND count(*) FILTER (WHERE NOT is_our_flag AND in_stock) <= 1
        ORDER BY query, region
    """, (list(RELEVANT_KINDS),))
    return rows


def _two_latest_runs():
    return query("""
        SELECT query, region, captured_at, rn FROM (
            SELECT query, region, captured_at,
                   ROW_NUMBER() OVER (PARTITION BY query, region ORDER BY captured_at DESC) AS rn
            FROM (SELECT DISTINCT query, region, captured_at FROM wb_search_snapshot) t
        ) x WHERE rn <= 2
    """)


def signal_competitor_drops():
    runs = _two_latest_runs()
    by_qr = {}
    for r in runs:
        by_qr.setdefault((r["query"], r["region"]), {})[r["rn"]] = r["captured_at"]
    out = []
    for (q, region), ts in by_qr.items():
        if 1 not in ts or 2 not in ts:
            continue        # только один снимок пока — сравнивать не с чем
        cur = {row["supplier_id"]: row for row in query(
            "SELECT supplier_id, supplier, min(position) AS pos, bool_or(in_stock) AS in_stock "
            "FROM wb_search_snapshot s JOIN wb_search_match m ON m.snapshot_id=s.id "
            "WHERE s.query=%s AND s.region=%s AND s.captured_at=%s AND m.item_kind = ANY(%s) "
            "AND NOT m.is_our GROUP BY supplier_id, supplier",
            (q, region, ts[1], list(RELEVANT_KINDS)))}
        prev = {row["supplier_id"]: row for row in query(
            "SELECT supplier_id, supplier, min(position) AS pos, bool_or(in_stock) AS in_stock "
            "FROM wb_search_snapshot s JOIN wb_search_match m ON m.snapshot_id=s.id "
            "WHERE s.query=%s AND s.region=%s AND s.captured_at=%s AND m.item_kind = ANY(%s) "
            "AND NOT m.is_our GROUP BY supplier_id, supplier",
            (q, region, ts[2], list(RELEVANT_KINDS)))}
        for sid, p in prev.items():
            c = cur.get(sid)
            if p["in_stock"] and (c is None or not c["in_stock"]):
                out.append((q, region, p["supplier"], "пропало наличие"))
            elif c and p["pos"] is not None and c["pos"] is not None and c["pos"] - p["pos"] >= 10:
                out.append((q, region, p["supplier"], f"позиция {p['pos']}→{c['pos']}"))
    return out


def signal_worse_region():
    rows = query("""
        SELECT s.query, s.region, min(s.position) AS best_pos
        FROM wb_search_snapshot s JOIN wb_search_match m ON m.snapshot_id = s.id
        WHERE m.is_our AND m.item_kind = ANY(%s)
          AND s.captured_at = (SELECT max(captured_at) FROM wb_search_snapshot WHERE query=s.query AND region=s.region)
        GROUP BY s.query, s.region
    """, (list(RELEVANT_KINDS),))
    by_q = {}
    for r in rows:
        by_q.setdefault(r["query"], []).append((r["region"], r["best_pos"]))
    out = []
    for q, regs in by_q.items():
        if len(regs) < 2:
            continue
        best = min(p for _, p in regs)
        for region, pos in regs:
            if pos - best >= 15:
                out.append((q, region, pos, best))
    return out


def build_summary():
    alone = signal_we_alone()
    drops = signal_competitor_drops()
    worse = signal_worse_region()
    lines = ["📡 Выдача ВБ, недельная сводка (этап 3, msk/spb/ekb)"]
    lines.append(f"\nA. Мы одни/почти одни в наличии: {len(alone)}")
    for r in alone[:15]:
        lines.append(f"  «{r['query'][:40]}» ({r['region']}) — конкурентов в наличии {r['competitors_in_stock']}")
    lines.append(f"\nB. У конкурентов пропало наличие/упала позиция: {len(drops)}")
    for q, region, supplier, what in drops[:15]:
        lines.append(f"  «{q[:35]}» ({region}) {supplier or '?'} — {what}")
    lines.append(f"\nC. Наша позиция заметно хуже в другом регионе: {len(worse)}")
    for q, region, pos, best in worse[:15]:
        lines.append(f"  «{q[:35]}» ({region}) {pos} против {best} в лучшем регионе")
    if len(alone) + len(drops) + len(worse) == 0:
        lines.append("\n(пусто — вероятно, данных мало: крон только начал копить историю)")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="только печать, без телеграма")
    a = ap.parse_args()
    text = build_summary()
    print(text)
    if not a.dry:
        print("\nTG:", tg(text))


if __name__ == "__main__":
    main()
