"""reports/margin_ozon_sku.py — витрина маржи Ozon по SKU: деньги Ozon − COGS из МС.

Деньги: raw_ozon_transaction, разложенные categorize_operation (без двойного счёта).
SKU: items[].sku операции (78% операций несут товары; кампанийная реклама/подписка/
эквайринг без items — это ОВЕРХЕД, не привязывается к SKU, показывается отдельно).

COGS по тому же принципу, что у ВБ (margin_by_sku), но ключ озоновский:
  posting_number → первые 2 сегмента `order_id-shipment` → FIFO-кэш ms_demand_cogs
  (report/stock/byoperation на moment отгрузки, агенты «Покупатель Озон» и «Озон Экспресс»).
  FBO (~3.2%) отгрузки в МС НЕ имеет → COGS не находится (как WB-FBO) — в покрытии видно.

Мульти-SKU отправление: деньги и COGS делятся поровну между SKU отправления (большинство
отправлений односоставные; помечено как допущение v1).

net = Σ(категории op) − COGS. Деньги — якорь. Пишет в margin_by_sku (platform='ozon').
Запуск:  ./venv/bin/python reports/margin_ozon_sku.py [2026-06-01] [2026-06-30] [oz_acc1]
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
from collectors.ozon import categorize_operation, CATEGORIES, fetch_product_offer_map  # noqa: E402

load_dotenv(BASE_DIR / ".env")
MS_TOK = os.getenv("MOYSKLAD_TOKEN")
MS = "https://api.moysklad.ru/api/remap/1.2"
H = {"Authorization": f"Bearer {MS_TOK}", "Accept-Encoding": "gzip",
     "Accept": "application/json;charset=utf-8"}
ACC_ORG = {"oz_acc1": 'ООО "ЦИФРОВОЙ КВАДРАТ"', "oz_acc2": 'ООО "ДИСКВЭР"'}


def _ms(path, params=None):
    r = requests.get(f"{MS}/{path}", headers=H, params=params, timeout=60)
    time.sleep(0.2)
    return r.json()


def _href_id(href):
    return href.rstrip("/").split("/")[-1]


def norm_posting(p):
    """Ключ связки = первые 2 сегмента posting_number (order_id-shipment)."""
    return "-".join((p or "").split("-")[:2])


def _agent_href(name="Покупатель Озон"):
    j = _ms("entity/counterparty", {"search": name, "limit": 50})
    for r in j.get("rows", []):
        if r["name"].lower() == name.lower():
            return r["meta"]["href"], r["name"]
    raise RuntimeError(f"агент «{name}» не найден в МС")


def posting_cogs_map(date_from, date_to, ms_org):
    """{norm_posting: cogs} — FIFO-кэш отгрузок FBS+RFBS на их moment в МС."""
    org = _ms("entity/organization", {"filter": f"name={ms_org}", "limit": 1})
    org_id = _href_id(org["rows"][0]["meta"]["href"])
    rows = db.query("""SELECT demand_name, cogs FROM ms_demand_cogs
        WHERE org=%s AND agent IN ('Покупатель Озон','Озон Экспресс')""", (org_id,))
    out = defaultdict(float)
    for r in rows:
        out[norm_posting(r["demand_name"])] += float(r["cogs"] or 0)
    return out


def _group_price_map():
    """{external_code: (min_nonzero, max_nonzero, is_set)} — групповая цена для fallback-COGS
    (как у ВБ): один external_code = группа аналогов с разной себестоимостью."""
    rows = db.query("""SELECT external_code,
        min(cost_seb) FILTER (WHERE cost_seb>0) mn,
        max(cost_seb) FILTER (WHERE cost_seb>0) mx,
        bool_or(title ILIKE '%%набор%%' OR title ILIKE '%%комплект%%') is_set
        FROM products WHERE external_code IS NOT NULL AND external_code<>'' GROUP BY external_code""")
    return {r["external_code"]: (r["mn"], r["mx"], r["is_set"]) for r in rows}


def _fallback_cpu(offer, gmap):
    """COGS/ед. по группе external_code: набор → max, одиночный → min (система берёт наименьший)."""
    g = gmap.get(offer)
    if not g:
        return None
    mn, mx, is_set = g
    if is_set and mx:
        return float(mx)
    return float(mn) if mn else None


def _rows(account, date_from, date_to):
    return [r["payload"] for r in db.query(
        """SELECT payload FROM raw_ozon_transaction
           WHERE account=%s AND (payload->>'operation_date')::date BETWEEN %s AND %s""",
        (account, date_from, date_to))]


def build(date_from="2026-06-01", date_to="2026-06-30", account="oz_acc1"):
    ms_org = ACC_ORG.get(account, ACC_ORG["oz_acc1"])
    ms_from = (datetime.date.fromisoformat(date_from) - datetime.timedelta(days=45)).isoformat()
    print(f"Ozon маржа {account} {date_from}..{date_to}", flush=True)
    cogs_map = posting_cogs_map(ms_from, date_to, ms_org)
    sku2offer = fetch_product_offer_map(account)   # sku → offer_id (=external_code)
    gmap = _group_price_map()

    ops = _rows(account, date_from, date_to)
    sku_fin = defaultdict(lambda: {c: 0.0 for c in CATEGORIES})
    sku_name = {}
    posting_skus = {}                 # norm_posting -> {"skus":[...], "schema":...}
    posting_rev = defaultdict(float)  # norm_posting -> выручка (для покрытия по деньгам)
    overhead = {c: 0.0 for c in CATEGORIES}
    for op in ops:
        cats = categorize_operation(op)
        skus = [str(i.get("sku")) for i in (op.get("items") or []) if i.get("sku")]
        if not skus:
            for c, v in cats.items():
                overhead[c] += v       # кампанийная реклама/подписка/эквайринг — оверхед
            continue
        share = 1.0 / len(skus)
        for it in op.get("items", []):
            if it.get("sku"):
                sku_name[str(it["sku"])] = it.get("name")
        for sku in skus:
            for c, v in cats.items():
                sku_fin[sku][c] += v * share
        post = (op.get("posting") or {}).get("posting_number")
        if post and cats["revenue"] > 0:
            key = norm_posting(post)
            posting_skus[key] = {
                "skus": skus, "schema": (op.get("posting") or {}).get("delivery_schema") or "—"}
            posting_rev[key] += cats["revenue"]

    # COGS на SKU тремя слоями (как у ВБ) → покрытие ~100%:
    #   1) ТОЧНО: отправление сматчилось с МС-заказом → COGS из позиций (делим по ед.).
    #   2) ИМПУТАЦИЯ: непокрытое отправление, но у SKU есть COGS/ед. из его сматченных продаж.
    #   3) ГРУППА: fallback по offer_id=external_code (как WB-FBO), если импутации нет.
    # Единицы: items[] повторяется по штукам (qty 3 = 3 записи) → счёт по записям.
    sku_cogs = defaultdict(float)
    m_cogs, m_units = defaultdict(float), defaultdict(int)
    cov = {"matched": 0, "imputed": 0, "grouped": 0, "missed": 0}
    rev_cov = {"covered": 0.0, "missed": 0.0}
    miss_schema = defaultdict(int)
    miss_detail, unmatched = [], []
    for key, info in posting_skus.items():
        skus = info["skus"]
        rev = posting_rev.get(key, 0.0)
        c = cogs_map.get(key)
        if c is not None:
            per = c / len(skus)
            for s in skus:
                sku_cogs[s] += per
                m_cogs[s] += per
                m_units[s] += 1
            cov["matched"] += 1
            rev_cov["covered"] += rev
        else:
            unmatched.append((key, skus, info["schema"], rev))
    cpu_map = {s: m_cogs[s] / m_units[s] for s in m_units if m_units[s] > 0}
    for key, skus, schema, rev in unmatched:
        used_impute = used_group = 0
        for s in skus:
            if s in cpu_map:
                sku_cogs[s] += cpu_map[s]
                used_impute += 1
            else:
                fc = _fallback_cpu(sku2offer.get(s), gmap)
                if fc is not None:
                    sku_cogs[s] += fc
                    used_group += 1
        if used_impute + used_group == 0:
            cov["missed"] += 1
            rev_cov["missed"] += rev
            miss_schema[schema] += 1
            miss_detail.append((key, schema, rev, len(skus)))
        else:
            cov["imputed" if used_impute >= used_group else "grouped"] += 1
            rev_cov["covered"] += rev

    # сборка строк + запись
    recs, rows_view = [], []
    for sku, f in sku_fin.items():
        rev = f["revenue"]
        cogs = sku_cogs.get(sku, 0.0)
        net = sum(f.values()) - cogs
        rec = {
            "article": sku, "platform": "ozon", "account": account,
            "period_from": date_from, "period_to": date_to, "qty": None,
            "revenue_buyer": rev, "cogs": cogs,
            "commission": -f["commission"], "logistics": -f["logistics"],
            "returns_sum": -f["returns"], "storage": -f["storage"],
            "acceptance": 0.0,
            "other": -(f["penalties"] + f["acquiring"] + f["advertising"]
                       + f["subscription"] + f["other"]),
            "net_profit": net, "margin_pct": (net / rev * 100) if rev else None,
            "commission_pct": (-f["commission"] / rev * 100) if rev else None,
        }
        recs.append(rec)
        rows_view.append((sku, sku_name.get(sku, ""), rev, cogs, net, rec["margin_pct"]))
    db.upsert("margin_by_sku", recs, conflict_cols=[
        "article", "platform", "account", "period_from", "period_to"])

    # --- сводка ---
    tot_payout = sum(sum(f.values()) for f in sku_fin.values()) + sum(overhead.values())
    tot_cogs = sum(sku_cogs.values())
    tot_rev = sum(f["revenue"] for f in sku_fin.values())
    total_p = sum(cov.values()) or 1
    covered = total_p - cov["missed"]
    rev_total = sum(rev_cov.values()) or 1
    print(f"\n  SKU с продажами: {sum(1 for f in sku_fin.values() if f['revenue']>0)}")
    print(f"  COGS-покрытие отправлений: {covered}/{total_p} ({covered/total_p*100:.0f}%) "
          f"= МС {cov['matched']} + импутация {cov['imputed']} + группа {cov['grouped']}; "
          f"без COGS {cov['missed']} {dict(miss_schema)}")
    print(f"  COGS-покрытие ПО ВЫРУЧКЕ: {rev_cov['covered']/rev_total*100:.1f}% "
          f"(без COGS {rev_cov['missed']:,.0f} ₽)".replace(",", " "))
    if miss_detail:
        print("  непокрытые (posting | schema | выручка | #ед.):")
        for key, sch, rev, ns in sorted(miss_detail, key=lambda x: -x[2])[:10]:
            print(f"    {key:<16} {sch:<5} {rev:>8,.0f}  ед={ns}".replace(",", " "))
    print(f"  записано в margin_by_sku: {len(recs)} SKU (platform=ozon)")

    print(f"\n=== ПОРТФЕЛЬ Ozon {account} {date_from}..{date_to} (₽) ===")
    for lbl, v in [("Выручка (SKU)", tot_rev), ("− COGS (из МС)", -tot_cogs),
                   ("Оверхед (реклама-кампании/подписка/эквайринг, вне SKU)",
                    sum(overhead.values()))]:
        print(f"  {lbl:<54}{v:>14,.0f}".replace(",", " "))
    print(f"  {'= ЧИСТАЯ ПРИБЫЛЬ (к перечислению − COGS)':<54}"
          f"{tot_payout - tot_cogs:>14,.0f}".replace(",", " "))

    print(f"\n=== ТОП-15 SKU по выручке ===")
    print(f"{'SKU':<12}{'выручка':>11}{'COGS':>11}{'чист.приб':>11}{'марж%':>7}  товар")
    for sku, name, rev, cogs, net, mp in sorted(rows_view, key=lambda x: -x[2])[:15]:
        mps = f"{mp:5.1f}" if mp is not None else "  —  "
        print(f"{sku:<12}{rev:>11,.0f}{cogs:>11,.0f}{net:>11,.0f}{mps:>7}  {(name or '')[:34]}"
              .replace(",", " "))

    print(f"\n=== 10 SKU-УБИЙЦ (отрицательная маржа, по выручке) ===")
    losers = [r for r in rows_view if r[4] < 0 and r[2] > 0]
    for sku, name, rev, cogs, net, mp in sorted(losers, key=lambda x: -x[2])[:10]:
        mps = f"{mp:5.1f}" if mp is not None else "  —  "
        print(f"{sku:<12}{rev:>11,.0f}{cogs:>11,.0f}{net:>11,.0f}{mps:>7}  {(name or '')[:34]}"
              .replace(",", " "))


if __name__ == "__main__":
    a = sys.argv
    build(a[1] if len(a) > 1 else "2026-06-01",
          a[2] if len(a) > 2 else "2026-06-30",
          a[3] if len(a) > 3 else "oz_acc1")
