"""phase1_cogs.py — Фаза 1: COGS ВБ из отгрузок МС по report/profit себесту, по месяцу формирования.

unit_cost[ms_id] = себест/шт из report/profit/byproduct (реальный FIFO, Цифровой).
demand_cogs[assembly_id] = Σ(позиция.qty × unit_cost[позиция.ms_id]) по отгрузке МС.
WB COGS[месяц формирования] = Σ по distinct FBS assembly_id отчётов месяца × demand_cogs.
FBO (assembly_id=0) = импутация: WB-строка → ms_id (по баркоду) → unit_cost. Сверка с файлом.
"""
import os, sys, json, urllib.request, urllib.parse, gzip
sys.path.insert(0, "/opt/mp-analytics")
from core import db
from dotenv import load_dotenv
load_dotenv("/opt/mp-analytics/.env")
TOK = os.getenv("MOYSKLAD_TOKEN"); MS = "https://api.moysklad.ru/api/remap/1.2"


def get(p):
    req = urllib.request.Request(MS + p, headers={"Authorization": f"Bearer {TOK}", "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=180) as r:
        d = r.read(); d = gzip.decompress(d) if r.headers.get("Content-Encoding") == "gzip" else d
        return json.loads(d)


def href_id(h): return (h or "").split("/")[-1].split("?")[0]


def main():
    org = get("/entity/organization?filter=" + urllib.parse.quote('name=ООО "ЦИФРОВОЙ КВАДРАТ"') + "&limit=1")["rows"][0]["meta"]["href"]
    ag = get("/entity/counterparty?filter=" + urllib.parse.quote("name=Покупатель ВБ") + "&limit=1")["rows"][0]["meta"]["href"]

    # 1) себест/шт по ms_id (report/profit, Цифровой, дек-июн)
    print("report/profit → себест/шт…", flush=True)
    agg = {}; off = 0
    while True:
        qs = urllib.parse.urlencode({"momentFrom": "2025-12-01 00:00:00", "momentTo": "2026-06-30 23:59:59", "limit": 1000})
        j = get(f"/report/profit/byproduct?{qs}&offset={off}&filter=" + urllib.parse.quote(f"organization={org}"))
        rows = j.get("rows", [])
        for r in rows:
            ms = href_id((r.get("assortment", {}).get("meta", {}) or {}).get("href"))
            q = r.get("sellQuantity", 0) or 0; c = r.get("sellCostSum", 0) or 0
            if q > 0:
                s = agg.setdefault(ms, [0.0, 0.0]); s[0] += c; s[1] += q
        off += 1000
        if len(rows) < 1000 or off >= j.get("meta", {}).get("size", 0): break
    ucost = {ms: c / q / 100 for ms, (c, q) in agg.items() if q}
    print(f"  себест/шт по {len(ucost)} ms_id", flush=True)

    # 2) отгрузки МС с позициями (Покупатель ВБ, Цифровой, ноя-июн) → demand_cogs[name]
    print("отгрузки МС с позициями…", flush=True)
    flt = urllib.parse.quote(f"agent={ag};organization={org};moment>=2025-11-01 00:00:00;moment<=2026-06-30 23:59:59")
    demand_cogs = {}; off = 0; nd = 0; pos_cov = pos_tot = 0
    while True:
        j = get(f"/entity/demand?limit=100&offset={off}&filter={flt}&expand=positions.assortment")
        rows = j.get("rows", [])
        for d in rows:
            nd += 1; tot = 0.0
            for p in (d.get("positions", {}) or {}).get("rows", []):
                ms = href_id((p.get("assortment", {}).get("meta", {}) or {}).get("href"))
                q = p.get("quantity", 0) or 0; pos_tot += q
                u = ucost.get(ms)
                if u is not None: tot += q * u; pos_cov += q
            demand_cogs[d.get("name")] = tot
        off += 100
        if nd % 2000 < 100: print(f"  отгрузок {nd}…", flush=True)
        if len(rows) < 100 or off >= j.get("meta", {}).get("size", 0): break
    print(f"  отгрузок {nd} | покрытие позиций себестом {round(pos_cov/pos_tot*100) if pos_tot else 0}%", flush=True)

    # 3) WB COGS по месяцу формирования. FBS: distinct assembly_id → demand_cogs. FBO: импутация по баркоду.
    bc2ms = {r["barcode"]: r["ms_id"] for r in db.query("SELECT barcode,ms_id FROM ms_barcode")}
    file = {"2026-01": 946376, "2026-02": 1284369, "2026-03": 1113371, "2026-04": 852235}
    # FBS — по уникальным отгрузкам в отчётах каждого месяца (продажи; возвраты пока опускаем для сверки)
    fbs = db.query("""SELECT to_char((payload->>'create_dt')::date,'YYYY-MM') ym,
        payload->>'assembly_id' aid FROM raw_wb_report WHERE account='wb_acc1'
        AND payload->>'supplier_oper_name'='Продажа' AND coalesce(payload->>'assembly_id','0')<>'0'
        GROUP BY 1,2""")
    cogs = {}; matched = miss = 0
    for r in fbs:
        c = demand_cogs.get(r["aid"])
        if c is not None: cogs[r["ym"]] = cogs.get(r["ym"], 0) + c; matched += 1
        else: miss += 1
    # FBO — импутация
    fbo = db.query("""SELECT to_char((payload->>'create_dt')::date,'YYYY-MM') ym, payload->>'barcode' bc,
        sum((payload->>'quantity')::numeric) q FROM raw_wb_report WHERE account='wb_acc1'
        AND payload->>'supplier_oper_name'='Продажа' AND coalesce(payload->>'assembly_id','0')='0'
        GROUP BY 1,2""")
    fbo_cogs = {}
    for r in fbo:
        u = ucost.get(bc2ms.get(r["bc"]))
        if u is not None: fbo_cogs[r["ym"]] = fbo_cogs.get(r["ym"], 0) + float(r["q"]) * u

    print(f"\nFBS отгрузок сматчено {matched}, не нашлось {miss}")
    print(f"  {'мес':8}{'FBS COGS':>14}{'FBO COGS':>12}{'ИТОГО':>14}{'файл':>12}{'Δ%':>8}")
    for m in ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]:
        fb = round(cogs.get(m, 0)); fo = round(fbo_cogs.get(m, 0)); tot = fb + fo; f = file.get(m)
        d = f"{(tot-f)/f*100:+.1f}%" if f else ""
        print(f"  {m:8}{fb:>14,}{fo:>12,}{tot:>14,}{(f'{f:,}' if f else '—'):>12}{d:>8}")


if __name__ == "__main__":
    main()
