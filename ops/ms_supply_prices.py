#!/usr/bin/env python3
# поток: ev — выгрузка позиций приёмок МойСклада в ms_supply_pos.
# Зачем: оценка товара, утраченного на складе МП. Себест отгрузок (FIFO) отвечает на другой
# вопрос — по чём списали при ПРОДАЖЕ; для лежавшего на складе нужна цена ПОКУПКИ партии.
import sys, time
from collectors.moysklad import _get, API
from core import db

BATCH = 100   # документов за запрос; expand=positions отдаёт до 1000 позиций внутри

# gtd и country нужны статформе ФТС: номер ГТД и страна происхождения товара берутся
# по FIFO из приёмки. Оба поля — вложенные объекты, без expand приходят только ссылками.
EXPAND = "positions,positions.country"


def collect(since, until):
    total, offset, saved = None, 0, 0
    while total is None or offset < total:
        r = _get(f"{API}/entity/supply", params={
            "limit": BATCH, "offset": offset, "expand": EXPAND,
            "filter": f"moment>={since} 00:00:00;moment<={until} 23:59:59"})
        total = r["meta"]["size"]
        rows = r["rows"]
        if not rows:
            break
        batch = []
        for d in rows:
            pos = d.get("positions", {})
            got, size = pos.get("rows", []), pos.get("meta", {}).get("size", 0)
            if len(got) < size:                     # редкий документ длиннее 1000 позиций
                got = _get(pos["meta"]["href"], params={"limit": 1000})["rows"]
            agent = (d.get("agent") or {}).get("meta", {}).get("href", "").rsplit("/", 1)[-1]
            for p in got:
                a = p.get("assortment", {}).get("meta", {})
                if a.get("type") != "product":
                    continue                        # услуги/комплекты в себест партии не идут
                batch.append({"supply_id": d["id"], "supply_name": d.get("name"), "moment": d["moment"],
                              "ms_id": a["href"].rsplit("/", 1)[-1], "qty": float(p.get("quantity") or 0),
                              "price_rub": float(p.get("price") or 0) / 100.0, "agent": agent,
                              "gtd": (p.get("gtd") or {}).get("name"),
                              "country": (p.get("country") or {}).get("name")})
        if batch:
            db.upsert("ms_supply_pos", batch, ["supply_id", "ms_id"])
            saved += len(batch)
        offset += BATCH
        print(f"  {min(offset, total)}/{total} документов, позиций сохранено {saved}", flush=True)
    return saved


if __name__ == "__main__":
    since = sys.argv[1] if len(sys.argv) > 1 else "2025-08-01"
    until = sys.argv[2] if len(sys.argv) > 2 else "2026-08-21"
    t = time.time()
    n = collect(since, until)
    print(f"приёмки {since}..{until}: позиций {n}, за {time.time()-t:.0f} с")
