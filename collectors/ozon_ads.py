"""collectors/ozon_ads.py — реклама Ozon Performance по кампаниям (расход + выручка → ДРР).

Performance API (api-performance.ozon.ru), креды OZON_PERF_CLIENT_ID_ACC*/OZON_PERF_SECRET_ACC*:
- POST /api/client/token → Bearer.
- GET  /api/client/campaign → список кампаний (id, title, advObjectType, state).
- GET  /api/client/statistics/expense (CSV) → расход по кампаниям по дням (надёжно, синхронно).
- POST /api/client/statistics → UUID → poll → GET .../report?UUID (ZIP из CSV по кампаниям) →
  показы/клики/выручка в продвижении для ДРР.

Тип оплаты: ALL_SKU_PROMO/SEARCH_PROMO = «Оплата за заказ» (% с заказа), иначе «Трафареты».
Только для аккаунтов с Performance-кредами (сейчас Цифровой). Кладём в ozon_ads за месяц.

Запуск:  ./venv/bin/python collectors/ozon_ads.py [oz_acc1] [YYYY-MM-01]
"""
import io
import os
import sys
import time
import zipfile
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


def _expense(H, df, dt):
    """({campaign_id: расход}, {дата: расход}) из CSV (ID;Дата;Название;Расход;…)."""
    r = requests.get(f"{PERF}/api/client/statistics/expense", headers=H, timeout=90,
                     params={"dateFrom": df.isoformat(), "dateTo": dt.isoformat()})
    r.raise_for_status()
    per_camp, per_date = {}, {}
    for ln in r.text.splitlines()[1:]:
        p = ln.split(";")
        if len(p) >= 4 and p[0]:
            sp = _rub(p[3])
            per_camp[p[0]] = per_camp.get(p[0], 0.0) + sp
            d = p[1].strip()[:10]
            if d:
                per_date[d] = per_date.get(d, 0.0) + sp
    return per_camp, per_date


def _report(H, ids, df, dt):
    """{campaign_id: {views,clicks,ad_revenue,sold}} из async-отчёта (ZIP с CSV по кампаниям)."""
    if not ids:
        return {}
    u = requests.post(f"{PERF}/api/client/statistics", headers=H, timeout=90, json={
        "campaigns": ids, "from": f"{df.isoformat()}T00:00:00Z",
        "to": f"{dt.isoformat()}T23:59:59Z", "groupBy": "NO_GROUP_BY"})
    u.raise_for_status()
    uuid = u.json().get("UUID")
    for _ in range(30):
        time.sleep(4)
        st = requests.get(f"{PERF}/api/client/statistics/{uuid}", headers=H, timeout=60).json()
        if st.get("state") == "OK":
            break
        if st.get("state") == "ERROR":
            return {}
    rep = requests.get(f"{PERF}/api/client/statistics/report", headers=H, timeout=120,
                       params={"UUID": uuid})
    rep.raise_for_status()
    out = {}
    try:
        z = zipfile.ZipFile(io.BytesIO(rep.content))
    except zipfile.BadZipFile:
        return {}
    for name in z.namelist():
        cid = name.split("_")[0]
        lines = z.read(name).decode("utf-8", errors="replace").splitlines()
        if len(lines) < 3:
            continue
        hdr = [h.strip().lower() for h in lines[1].split(";")]

        def col(sub):
            for i, h in enumerate(hdr):
                if sub in h:
                    return i
            return -1
        iv, ic = col("показ"), col("клик")
        irev, isold = col("продажи в продвижении"), col("продано")
        agg = {"views": 0, "clicks": 0, "ad_revenue": 0.0, "sold": 0.0}
        for ln in lines[2:]:
            p = ln.split(";")
            if iv >= 0 and iv < len(p):
                agg["views"] += int(_rub(p[iv]))
            if ic >= 0 and ic < len(p):
                agg["clicks"] += int(_rub(p[ic]))
            if irev >= 0 and irev < len(p):
                agg["ad_revenue"] += _rub(p[irev])
            if isold >= 0 and isold < len(p):
                agg["sold"] += _rub(p[isold])
        out[cid] = agg
    return out


def main(account="oz_acc1", period=None):
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
    meta = {c["id"]: c for c in camps}
    exp, exp_daily = _expense(H, df, dt)
    daily = [{"account": account, "platform": "ozon", "date": d, "spend": round(s, 2)}
             for d, s in exp_daily.items()]
    db.upsert("ad_spend_daily", daily, conflict_cols=["account", "platform", "date"],
              update_cols=["spend"])
    running = [c["id"] for c in camps if c.get("state") == "CAMPAIGN_STATE_RUNNING"]
    rep = _report(H, running, df, dt)
    # кампании к записи: где есть расход или показы (активность за период)
    ids = set(exp) | set(rep)
    recs = []
    for cid in ids:
        c = meta.get(cid, {})
        adv = c.get("advObjectType")
        rp = rep.get(cid, {})
        recs.append({
            "account": account, "period": pf.isoformat(), "campaign_id": cid,
            "title": c.get("title"), "adv_type": adv,
            "pay_model": "Оплата за заказ" if adv in PAY_ORDER_TYPES else "Трафареты",
            "state": c.get("state"),
            "spend": round(exp.get(cid, 0.0), 2),
            "views": rp.get("views", 0), "clicks": rp.get("clicks", 0),
            "ad_revenue": round(rp.get("ad_revenue", 0.0), 2), "sold": rp.get("sold", 0)})
    n = db.upsert("ozon_ads", recs, conflict_cols=["account", "period", "campaign_id"],
                  update_cols=["title", "adv_type", "pay_model", "state", "spend",
                               "views", "clicks", "ad_revenue", "sold"])
    tot_spend = sum(r["spend"] for r in recs)
    tot_rev = sum(r["ad_revenue"] for r in recs)
    print(f"Записано кампаний: {n} | расход {tot_spend:,.0f} ₽ | выручка с рекламы {tot_rev:,.0f} ₽ "
          f"| ДРР {round(tot_spend/tot_rev*100,1) if tot_rev else '—'}%", flush=True)


if __name__ == "__main__":
    acc = sys.argv[1] if len(sys.argv) > 1 else "oz_acc1"
    per = sys.argv[2] if len(sys.argv) > 2 else None
    main(acc, per)
