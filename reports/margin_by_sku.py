"""reports/margin_by_sku.py — Этап 3. Витрина маржи: WB-деньги − реальный COGS из МС.

COGS WB-продажи тремя слоями (покрытие → ~100%):
  1) ТОЧНО по заказу: WB `assembly_id` = МС `name` → Σ buy_price компонентов отгрузки (FBS).
  2) ИМПУТАЦИЯ: для непокрытых единиц nm, у которого есть матч — COGS/шт из его матч-заказов.
  3) FALLBACK (FBO, продажи со склада WB без отгрузки в МС): `sa_name`(vendorCode) = `external_code`
     МС → цена группы (для набора/комплекта — ненулевой максимум; для одиночных — минимум,
     «система выбирает наименьший»).

net_profit = to_pay − logistics − storage − acceptance − other − COGS. Деньги — якорь (не штуки).
Запуск:  ./venv/bin/python reports/margin_by_sku.py
"""
import os
import sys
import time
import pathlib
import datetime
from collections import defaultdict

import requests
from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

load_dotenv(BASE_DIR / ".env")
MS_TOK = os.getenv("MOYSKLAD_TOKEN")
MS = "https://api.moysklad.ru/api/remap/1.2"
H = {"Authorization": f"Bearer {MS_TOK}", "Accept-Encoding": "gzip",
     "Accept": "application/json;charset=utf-8"}


def _ms(path, params=None):
    r = requests.get(f"{MS}/{path}", headers=H, params=params, timeout=60)
    time.sleep(0.25)
    return r.json()


def _href_id(href):
    return href.rstrip("/").split("/")[-1]


def demand_cogs_by_order(date_from, date_to, ms_agent="Покупатель ВБ",
                         ms_org='ООО "ЦИФРОВОЙ КВАДРАТ"'):
    """{order_name(assembly_id): cogs} — Σ buy_price компонентов отгрузки МС (по ms_id)."""
    # COGS = себестоимость из report/stock (из приёмок), НЕ buyPrice (тот — кривая справка).
    prod = {r["ms_id"]: float(r["cost_seb"] or 0)
            for r in db.query("SELECT ms_id, cost_seb FROM products")}
    ag = _ms("entity/counterparty", {"filter": f"name={ms_agent}", "limit": 1})["rows"][0]["meta"]["href"]
    org = _ms("entity/organization", {"filter": f"name={ms_org}", "limit": 1})["rows"][0]["meta"]["href"]
    flt = (f"agent={ag};organization={org};"
           f"moment>={date_from} 00:00:00;moment<={date_to} 23:59:59")
    out, offset = {}, 0
    while True:
        j = _ms("entity/demand", {"limit": 100, "offset": offset,
                                  "filter": flt, "expand": "positions.assortment"})
        rows = j.get("rows", [])
        for d in rows:
            cogs = 0.0
            for p in d.get("positions", {}).get("rows", []):
                ms_id = _href_id(p.get("assortment", {}).get("meta", {}).get("href", ""))
                cogs += prod.get(ms_id, 0.0) * (p.get("quantity", 0) or 0)
            out[d.get("name")] = cogs
        offset += 100
        if not rows or offset >= j.get("meta", {}).get("size", 0):
            break
    return out


def _group_price_map():
    """{external_code: (min_nonzero, max_nonzero, is_set)} для fallback-COGS."""
    rows = db.query("""SELECT external_code,
        min(cost_seb) FILTER (WHERE cost_seb>0) mn,
        max(cost_seb) FILTER (WHERE cost_seb>0) mx,
        bool_or(title ILIKE '%%набор%%' OR title ILIKE '%%комплект%%') is_set
        FROM products WHERE external_code IS NOT NULL GROUP BY external_code""")
    return {r["external_code"]: (r["mn"], r["mx"], r["is_set"]) for r in rows}


def build(account="wb_acc1", date_from="2026-05-01", date_to="2026-05-31"):
    print("Считаю COGS заказов из МС…", flush=True)
    # Широкое окно МС: WB-выкуп идёт через ~6 дней после отгрузки → отгрузка под майский
    # выкуп часто в апреле. Берём -45 дней от начала периода, иначе FBS примут за FBO.
    ms_from = (datetime.date.fromisoformat(date_from) - datetime.timedelta(days=45)).isoformat()
    cogs_order = demand_cogs_by_order(ms_from, date_to)
    gmap = _group_price_map()

    # WB-продажи по assembly: nm, units, sa_name (vendorCode).
    # ВАЖНО: фильтруем по периоду отчёта — иначе при нескольких загруженных месяцах
    # tu/COGS суммируются по всем периодам и весь итог садится на одну строку (×N завышение).
    asm = db.query("""SELECT payload->>'assembly_id' a, payload->>'nm_id' nm,
        sum((payload->>'quantity')::numeric) u, max(payload->>'sa_name') sa
        FROM raw_wb_report WHERE account=%s AND payload->>'supplier_oper_name'='Продажа'
          AND period_from=%s AND period_to=%s
          AND coalesce(payload->>'assembly_id','')<>'' GROUP BY 1,2""",
                   (account, date_from, date_to))
    nm = defaultdict(lambda: {"mc": 0.0, "mu": 0.0, "tu": 0.0, "sa": None})
    for r in asm:
        u = float(r["u"] or 0)
        info = nm[r["nm"]]
        info["tu"] += u
        info["sa"] = info["sa"] or r["sa"]
        if r["a"] in cogs_order:
            info["mc"] += cogs_order[r["a"]]
            info["mu"] += u

    def fallback_cpu(sa):
        g = gmap.get(sa)
        if not g:
            return None
        mn, mx, is_set = g
        if is_set and mx:
            return float(mx)        # набор — полная цена набора
        if mn:
            return float(mn)        # одиночные — минимум (система берёт наименьший)
        return None

    cov = {"exact": 0.0, "impute": 0.0, "fallback": 0.0, "none": 0.0}
    cogs_nm = {}
    for n, info in nm.items():
        if info["mu"] > 0:
            cpu = info["mc"] / info["mu"]
            cogs_nm[n] = info["mc"] + cpu * (info["tu"] - info["mu"])
            cov["exact"] += info["mu"]
            cov["impute"] += info["tu"] - info["mu"]
        else:
            cpu = fallback_cpu(info["sa"])
            if cpu is not None:
                cogs_nm[n] = cpu * info["tu"]
                cov["fallback"] += info["tu"]
            else:
                cogs_nm[n] = 0.0
                cov["none"] += info["tu"]

    # деньги по nm_id из sales + COGS → margin_by_sku
    sales = db.query("""SELECT * FROM sales WHERE platform='wb' AND account=%s
                        AND period_from=%s AND period_to=%s""", (account, date_from, date_to))
    recs = []
    for s in sales:
        rev = float(s["revenue_buyer"] or 0)
        cogs = cogs_nm.get(s["article"], 0.0)
        net = float(s["to_pay"] or 0) - float(s["logistics"] or 0) - float(s["storage"] or 0) \
            - float(s["acceptance"] or 0) - float(s["other"] or 0) - cogs
        recs.append({
            "article": s["article"], "platform": "wb", "account": account,
            "period_from": date_from, "period_to": date_to,
            "qty": s["qty"], "revenue_buyer": rev, "cogs": cogs,
            "commission": s["commission"], "logistics": s["logistics"],
            "returns_sum": s["returns_sum"], "storage": s["storage"],
            "acceptance": s["acceptance"], "other": s["other"],
            "net_profit": net, "margin_pct": (net / rev * 100) if rev else None,
            "commission_pct": (float(s["commission"] or 0) / rev * 100) if rev else None,
        })
    db.upsert("margin_by_sku", recs, conflict_cols=[
        "article", "platform", "account", "period_from", "period_to"])

    tot = sum(cov.values()) or 1
    print(f"  COGS-покрытие по штукам: точно {cov['exact']:.0f}, импутация {cov['impute']:.0f}, "
          f"fallback {cov['fallback']:.0f}, нет {cov['none']:.0f} "
          f"→ покрыто {(tot-cov['none'])/tot*100:.0f}%", flush=True)
    print(f"  записано в margin_by_sku: {len(recs)} nm_id", flush=True)


if __name__ == "__main__":
    build()
