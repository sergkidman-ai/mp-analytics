#!/usr/bin/env python
# поток: fin
"""ops/ms_qty_backfill.py — починка ШТУК в кэше себеста отгрузок МойСклад.

Зачем: до 2026-08-12 `ms_demand_cogs.qty` / `ms_demand_pos.qty` заполнялись полем `quantity`
отчёта `report/stock/byoperation`, а это не количество в документе, а `stock − reserve + inTransit`
(доступный остаток товара на момент запроса). Отсюда отрицательные и произвольные штуки, а через
импутацию `cost_seb × qty` — ещё и отрицательный себест. Себест (`cost` отчёта) не затронут.

Скрипт переписывает штуки из фактических позиций документов отгрузки и заодно заводит в кэш
отгрузки, которых там не было (им себест берётся штатным `byoperation`).

Запуск (DRY-RUN по умолчанию, запись — `--apply`):
    ./venv/bin/python -m ops.ms_qty_backfill --platform ozon --since 2026-01-01 --until 2026-05-31
    ./venv/bin/python -m ops.ms_qty_backfill --platform wb   --since 2026-01-01 --until 2026-08-12 --apply
"""
import sys
import pathlib
import argparse
import urllib.parse
from collections import defaultdict

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                                   # noqa: E402
import collectors.ms_demand_cogs as MDC               # noqa: E402


def _demands_with_positions(org_href, agent_href, since, until):
    """[{id, name, moment, org, per_item}] — отгрузки org+agent за период с фактическими штуками."""
    flt = urllib.parse.quote(f"organization={org_href};agent={agent_href};"
                             f"moment>={since} 00:00:00;moment<={until} 23:59:59")
    out, offset = [], 0
    while True:
        j = MDC.get(f"/entity/demand?limit=100&offset={offset}&expand=positions&filter={flt}")
        rows = j.get("rows", [])
        for d in rows:
            pos = d.get("positions") or {}
            prows = pos.get("rows", [])
            if (pos.get("meta") or {}).get("size", 0) > len(prows):    # expand не всё развернул
                prows = MDC.get(
                    f"/entity/demand/{d['id']}/positions?limit=1000").get("rows", [])
            per = defaultdict(float)
            for p in prows:
                per[MDC._hid(((p.get("assortment") or {}).get("meta") or {}).get("href"))] += \
                    float(p.get("quantity") or 0)
            out.append({"id": d["id"], "name": d.get("name"), "moment": d.get("moment"),
                        "per_item": dict(per)})
        offset += 100
        if not rows or offset >= j.get("meta", {}).get("size", 0):
            break
    return out


def backfill(platform="ozon", since="2026-01-01", until="2026-05-31", apply=False):
    config = MDC.PLATFORM[platform]
    docs = {}
    for account, org_name in config["org_map"].items():
        org_href = MDC._resolve_href("organization", org_name)
        org_id = MDC._hid(org_href)
        for agent in config["agents"]:
            agent_href = MDC._resolve_href("counterparty", agent)
            got = _demands_with_positions(org_href, agent_href, since, until)
            print(f"[{account}/{agent}] отгрузок в МС: {len(got)}", flush=True)
            for d in got:
                docs[d["id"]] = {**d, "org": org_id, "agent": agent}

    cached = {r["demand_id"]: float(r["qty"] or 0) for r in db.query(
        "SELECT demand_id, qty FROM ms_demand_cogs WHERE demand_id = ANY(%s)", (list(docs),))}
    wrong = [d for d, q in cached.items()
             if abs(q - sum(docs[d]["per_item"].values())) > 0.001]
    missing = [d for d in docs if d not in cached]
    print(f"итого отгрузок {len(docs)}: в кэше {len(cached)} (штуки врут у {len(wrong)}), "
          f"нет в кэше {len(missing)}")
    if not apply:
        print("DRY-RUN — ничего не записано (повторить с --apply)")
        return 0, 0

    for did in wrong:                                   # себест не трогаем, только штуки
        info = docs[did]
        db.execute("UPDATE ms_demand_cogs SET qty=%s WHERE demand_id=%s",
                   (sum(info["per_item"].values()), did))
        for ms_id, q in info["per_item"].items():
            db.execute("UPDATE ms_demand_pos SET qty=%s WHERE demand_id=%s AND ms_id=%s",
                       (q, did, ms_id))

    crecs, precs = [], []
    for n, did in enumerate(missing, 1):
        info = docs[did]
        cogs, _qty, pos = MDC.byoperation_cogs(did)     # уже с исправленными штуками
        crecs.append({"demand_id": did, "demand_name": info["name"], "org": info["org"],
                      "agent": info["agent"], "moment": info["moment"],
                      "cogs": round(cogs, 2), "qty": sum(info["per_item"].values()),
                      "npos": len(pos)})
        precs += [{"demand_id": did, "ms_id": p["ms_id"], "cost": round(p["cost"], 2),
                   "qty": p["qty"]} for p in pos if p["ms_id"]]
        if n % 200 == 0:
            print(f"  заведено {n}/{len(missing)}…", flush=True)
    if crecs:
        db.upsert("ms_demand_cogs", crecs, conflict_cols=["demand_id"])
    if precs:
        db.upsert("ms_demand_pos", precs, conflict_cols=["demand_id", "ms_id"])
    print(f"ГОТОВО: штуки переписаны у {len(wrong)}, заведено новых отгрузок {len(crecs)}")
    return len(wrong), len(crecs)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", default="ozon", choices=sorted(MDC.PLATFORM))
    ap.add_argument("--since", default="2026-01-01")
    ap.add_argument("--until", default="2026-05-31")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    backfill(a.platform, a.since, a.until, a.apply)
