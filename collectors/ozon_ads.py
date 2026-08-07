# поток: mkt
"""collectors/ozon_ads.py — реклама Ozon Performance по кампаниям (расход + выручка → ДРР).

Performance API (api-performance.ozon.ru), креды OZON_PERF_CLIENT_ID_ACC*/OZON_PERF_SECRET_ACC*:
- POST /api/client/token → Bearer.
- GET  /api/client/campaign → список кампаний (id, title, advObjectType, state).
- GET  /api/client/statistics/daily/json?campaignIds=&dateFrom=&dateTo= → по дням на кампанию:
  views, clicks, moneySpent, orders, ordersMoney. Синхронно, JSON, до 10 кампаний за запрос.

Почему daily/json, а не прежняя пара expense-CSV + асинхронный ZIP (проверено 07.08):
  расход по daily/json сходится с фактически списанным по транзакциям ДО РУБЛЯ
  (июль, «Продвижение с оплатой за заказ»: acc1 541 441 ₽, acc2 97 783 ₽ — совпало),
  тогда как ZIP-отчёт вообще не строится для ALL_SKU_PROMO/SEARCH_PROMO
  («generation of this type of report is forbidden») и требовал фильтра по RUNNING,
  из-за которого кампании, остановленные внутри периода, теряли показы и выручку.

Берутся ВСЕ кампании кабинета, а не только RUNNING: в закрытом месяце важен факт расхода,
а не сегодняшнее состояние. `covered_to` — по какой день период реально закрыт (месяц,
собранный в его середине, покрывает половину и молча выглядит как целый: так июль-2026
до пересбора показывал 410 867 ₽ вместо 711 089 ₽).

ДЫРКА ИЗМЕРЕНИЯ (открыта, шаг 3 плана): у «Оплаты за заказ» (ALL_SKU_PROMO) daily/json
отдаёт orders=0 и ordersMoney=0 при реальном расходе в сотни тысяч ₽ — выручка по этой
модели не отдаётся ни здесь, ни отчётом /api/client/statistic/orders/generate/json
(строит пустой отчёт), ни products/generate/json (там состояние продвижения в поиске).
Поэтому ДРР по ней не считается; в сводках это НЕ ноль, а «нет данных».

Тип оплаты: ALL_SKU_PROMO/SEARCH_PROMO = «Оплата за заказ» (% с заказа), иначе «Трафареты».

Запуск:  ./venv/bin/python collectors/ozon_ads.py [oz_acc1|oz_acc2|all] [YYYY-MM-01]
"""
import os
import sys
import time
import datetime
import pathlib

import requests
from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

load_dotenv(BASE_DIR / ".env")
PERF = "https://api-performance.ozon.ru"
CRED = {"oz_acc1": ("OZON_PERF_CLIENT_ID_ACC1", "OZON_PERF_SECRET_ACC1"),
        "oz_acc2": ("OZON_PERF_CLIENT_ID_ACC2", "OZON_PERF_SECRET_ACC2")}
PAY_ORDER_TYPES = {"ALL_SKU_PROMO", "SEARCH_PROMO"}


def has_creds(account):
    cid, sec = CRED.get(account, ("", ""))
    return bool(os.getenv(cid)) and bool(os.getenv(sec))


def _token(account):
    cid, sec = CRED[account]
    r = requests.post(f"{PERF}/api/client/token", timeout=60, json={
        "client_id": os.getenv(cid), "client_secret": os.getenv(sec),
        "grant_type": "client_credentials"})
    r.raise_for_status()
    return r.json()["access_token"]


def _rub(s):
    s = (s or "").strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _month_bounds(period_first):
    nxt = (period_first + datetime.timedelta(days=32)).replace(day=1)
    last = nxt - datetime.timedelta(days=1)
    today = datetime.date.today()
    return period_first, min(last, today)


def _daily(H, ids, df, dt):
    """({campaign_id: агрегат за период}, {дата: расход}) из daily/json. До 10 кампаний за раз."""
    per_camp, per_date = {}, {}
    for i in range(0, len(ids), 10):
        r = requests.get(f"{PERF}/api/client/statistics/daily/json", headers=H, timeout=120,
                         params={"campaignIds": ids[i:i + 10],
                                 "dateFrom": df.isoformat(), "dateTo": dt.isoformat()})
        if r.status_code != 200:
            print(f"  daily/json HTTP {r.status_code}: {r.text[:100]}", flush=True)
            continue
        for x in r.json().get("rows", []) or []:
            cid, day, sp = str(x.get("id")), (x.get("date") or "")[:10], _rub(x.get("moneySpent"))
            a = per_camp.setdefault(cid, {"views": 0, "clicks": 0, "spend": 0.0,
                                          "orders": 0.0, "ad_revenue": 0.0})
            a["views"] += int(_rub(x.get("views")))
            a["clicks"] += int(_rub(x.get("clicks")))
            a["spend"] += sp
            a["orders"] += _rub(x.get("orders"))
            a["ad_revenue"] += _rub(x.get("ordersMoney"))
            if day:
                per_date[day] = per_date.get(day, 0.0) + sp
        time.sleep(0.2)
    return per_camp, per_date


def main(account="oz_acc1", period=None):
    if account == "all":
        for a in ("oz_acc1", "oz_acc2"):
            main(a, period)
        return
    if not has_creds(account):
        print(f"Ozon реклама {account}: нет Performance-кредов — пропуск", flush=True)
        return
    if period is None:
        period = datetime.date.today().replace(day=1).isoformat()
    pf = datetime.date.fromisoformat(period)
    df, dt = _month_bounds(pf)
    print(f"Ozon реклама {account} {df}..{dt}", flush=True)
    H = {"Authorization": f"Bearer {_token(account)}"}
    camps = requests.get(f"{PERF}/api/client/campaign", headers=H, timeout=60).json().get("list", [])
    meta = {str(c["id"]): c for c in camps}
    stats, per_date = _daily(H, list(meta), df, dt)
    daily = [{"account": account, "platform": "ozon", "date": d, "spend": round(s, 2)}
             for d, s in per_date.items()]
    if daily:
        db.upsert("ad_spend_daily", daily, conflict_cols=["account", "platform", "date"],
                  update_cols=["spend"])
    recs = []
    for cid, a in stats.items():
        if not (a["spend"] or a["views"] or a["ad_revenue"]):
            continue                     # кампания без активности за период — не строка отчёта
        c = meta.get(cid, {})
        adv = c.get("advObjectType")
        recs.append({
            "account": account, "period": pf.isoformat(), "campaign_id": cid,
            "title": c.get("title"), "adv_type": adv,
            "pay_model": "Оплата за заказ" if adv in PAY_ORDER_TYPES else "Трафареты",
            "state": c.get("state"), "covered_to": dt.isoformat(),
            "spend": round(a["spend"], 2), "views": a["views"], "clicks": a["clicks"],
            "orders": a["orders"], "sold": a["orders"],
            "ad_revenue": round(a["ad_revenue"], 2)})
    n = db.upsert("ozon_ads", recs, conflict_cols=["account", "period", "campaign_id"],
                  update_cols=["title", "adv_type", "pay_model", "state", "spend", "views",
                               "clicks", "ad_revenue", "sold", "orders", "covered_to"]) if recs else 0
    tot_spend = sum(r["spend"] for r in recs)
    tot_rev = sum(r["ad_revenue"] for r in recs)
    blind = sum(r["spend"] for r in recs if r["pay_model"] == "Оплата за заказ"
                and not r["ad_revenue"])
    print(f"Записано кампаний: {n} | расход {tot_spend:,.0f} ₽ | выручка с рекламы "
          f"{tot_rev:,.0f} ₽ | ДРР {round(tot_spend/tot_rev*100,1) if tot_rev else '—'}%",
          flush=True)
    if blind:
        print(f"  из них вслепую (выручку API не отдаёт): {blind:,.0f} ₽ — "
              f"{100*blind/tot_spend:.0f}% бюджета", flush=True)
    if dt < (pf + datetime.timedelta(days=32)).replace(day=1) - datetime.timedelta(days=1):
        print(f"  ВНИМАНИЕ: месяц закрыт только по {dt} — пересобрать после его окончания",
              flush=True)


if __name__ == "__main__":
    acc = sys.argv[1] if len(sys.argv) > 1 else "oz_acc1"
    per = sys.argv[2] if len(sys.argv) > 2 else None
    main(acc, per)
