"""collectors/wb_ads.py — реклама WB «Продвижение» по кампаниям (расход + выручка → ДРР).

advert-api.wildberries.ru (скоуп «Продвижение» в WB-токене):
- GET  /adv/v1/promotion/count → кампании по type+status (+ их id). Берём активные (9) и
  на паузе (11) — у них есть статистика за период.
- POST /adv/v1/promotion/adverts (список id) → детали кампаний (name, type).
- GET  /adv/v3/fullstats?ids=&beginDate=&endDate= → статистика: sum (расход), views, clicks,
  orders, sum_price (выручка). ОГРАНИЧЕНИЯ: ≤50 id и ≤31 день за запрос; за «сегодня» не
  отдаёт (endDate = вчера). На практике ответ тяжёлый → батчим по 15.

Только аккаунты с «Продвижением» в токене. Кладём за месяц в wb_ads.

Запуск:  ./venv/bin/python collectors/wb_ads.py [wb_acc1] [YYYY-MM-01]
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
B = "https://advert-api.wildberries.ru"
TOKEN_ENV = {"wb_acc1": "WB_TOKEN_ACC1", "wb_acc2": "WB_TOKEN_ACC2"}
STATUSES = (9, 11)   # активные + на паузе (у них есть статистика)
BATCH = 15


def _token(account):
    t = os.getenv(TOKEN_ENV[account])
    if not t:
        raise RuntimeError(f"{TOKEN_ENV[account]} не задан")
    return t


def _month_bounds(pf):
    nxt = (pf + datetime.timedelta(days=32)).replace(day=1)
    last = nxt - datetime.timedelta(days=1)
    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    return pf, min(last, yesterday)


def _get(H, path, **params):
    while True:
        r = requests.get(B + path, headers=H, params=params, timeout=180)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("X-Ratelimit-Retry", r.headers.get("Retry-After", "20"))) + 1)
            continue
        return r


def main(account="wb_acc1", period=None):
    if period is None:
        period = datetime.date.today().replace(day=1).isoformat()
    pf = datetime.date.fromisoformat(period)
    df, dt = _month_bounds(pf)
    if dt < df:
        print(f"WB реклама {account}: период ещё не начался — пропуск", flush=True)
        return
    H = {"Authorization": _token(account)}
    cnt = _get(H, "/adv/v1/promotion/count")
    if cnt.status_code != 200:
        print(f"WB реклама {account}: promotion/count HTTP {cnt.status_code} — пропуск", flush=True)
        return
    meta = {}   # advertId -> (type, status)
    for grp in cnt.json().get("adverts") or []:
        if grp.get("status") in STATUSES:
            for a in grp.get("advert_list") or []:
                meta[a["advertId"]] = (grp.get("type"), grp.get("status"))
    ids = list(meta)
    print(f"WB реклама {account} {df}..{dt}: кампаний к опросу {len(ids)}", flush=True)
    # Имена кампаний: метод деталей у WB переехал (старые /adv/v1|v2/promotion/adverts → 404).
    # Пока без имён — показываем #advertId + тип. TODO: найти актуальный путь деталей кампаний.
    names = {}
    # статистика батчами по 15
    recs, daily = [], {}
    nm_agg = {}   # (advert_id, nm_id) -> агрегат по товару внутри кампании (из days[].apps[].nms[])
    nm_daily = {} # (dt, advert_id, nm_id) -> ДНЕВНОЙ агрегат по товару (реакция на смену ставки)
    booster = {}  # (dt, advert_id, nm_id) -> средняя позиция рекламного бустера за день
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        r = _get(H, "/adv/v3/fullstats", ids=",".join(str(x) for x in chunk),
                 beginDate=df.isoformat(), endDate=dt.isoformat())
        if r.status_code != 200 or not r.text.strip():
            print(f"  [wb ads] батч {i//BATCH}: HTTP {r.status_code} {r.text[:80]}", flush=True)
            time.sleep(2)
            continue
        for c in r.json() or []:
            aid = c.get("advertId")
            ty, st = meta.get(aid, (None, None))
            recs.append({"account": account, "period": pf.isoformat(), "advert_id": aid,
                         "name": names.get(aid), "adv_type": ty, "status": st,
                         "spend": round(c.get("sum", 0) or 0, 2), "views": c.get("views", 0) or 0,
                         "clicks": c.get("clicks", 0) or 0, "orders": c.get("orders", 0) or 0,
                         "revenue": round(c.get("sum_price", 0) or 0, 2),
                         "ctr": c.get("ctr", 0) or 0, "cpc": c.get("cpc", 0) or 0})
            for day in c.get("days") or []:           # дневной расход + товарный (nm) уровень
                dd = (day.get("date") or "")[:10]
                if dd:
                    daily[dd] = daily.get(dd, 0.0) + (day.get("sum") or 0)
                for app in day.get("apps") or []:
                    for nm in app.get("nms") or []:
                        nmid = nm.get("nmId")
                        if not nmid:
                            continue
                        vw = nm.get("views", 0) or 0; cl = nm.get("clicks", 0) or 0
                        atb = nm.get("atbs", 0) or 0; od = nm.get("orders", 0) or 0
                        sp = nm.get("sum", 0) or 0; rv = nm.get("sum_price", 0) or 0
                        a = nm_agg.setdefault((aid, nmid), {
                            "adv_type": ty, "status": st, "name": nm.get("name"),
                            "clicks": 0, "views": 0, "atbs": 0, "orders": 0, "spend": 0.0, "revenue": 0.0})
                        a["clicks"] += cl; a["views"] += vw; a["atbs"] += atb
                        a["orders"] += od; a["spend"] += sp; a["revenue"] += rv
                        if dd:                              # дневной срез по товару (для трека реакции)
                            da = nm_daily.setdefault((dd, aid, nmid), {
                                "clicks": 0, "views": 0, "atbs": 0, "orders": 0, "spend": 0.0, "revenue": 0.0})
                            da["clicks"] += cl; da["views"] += vw; da["atbs"] += atb
                            da["orders"] += od; da["spend"] += sp; da["revenue"] += rv
            for b in c.get("boosterStats") or []:           # позиция рекламного бустера per-nm/день
                bdd = (b.get("date") or "")[:10]; bnm = b.get("nm")
                if bdd and bnm:
                    booster[(bdd, aid, bnm)] = b.get("avg_position")
        print(f"  [wb ads] батч {i//BATCH+1}/{(len(ids)+BATCH-1)//BATCH}: +{len(chunk)} (всего {len(recs)})", flush=True)
        time.sleep(2)
    n = db.upsert("wb_ads", recs, conflict_cols=["account", "period", "advert_id"],
                  update_cols=["name", "adv_type", "status", "spend", "views", "clicks",
                               "orders", "revenue", "ctr", "cpc"])
    # товарный уровень: cpc = расход/клики (факт. ставка по nmId в кампании), ДРР, CR
    nm_recs = []
    for (aid, nmid), a in nm_agg.items():
        cl, sp, rv = a["clicks"], a["spend"], a["revenue"]
        nm_recs.append({
            "account": account, "period": pf.isoformat(), "advert_id": aid, "nm_id": nmid,
            "adv_type": a["adv_type"], "status": a["status"], "name": a["name"],
            "clicks": cl, "views": a["views"], "atbs": a["atbs"], "orders": a["orders"],
            "spend": round(sp, 2), "revenue": round(rv, 2),
            "cpc": round(sp / cl, 2) if cl else 0,
            "ctr": round(cl / a["views"] * 100, 2) if a["views"] else 0,
            "cr": round(a["orders"] / cl * 100, 1) if cl else 0,
            "drr": round(sp / rv * 100, 1) if rv else None,
        })
    if nm_recs:
        db.upsert("wb_ad_nm", nm_recs, conflict_cols=["account", "period", "advert_id", "nm_id"],
                  update_cols=["adv_type", "status", "name", "clicks", "views", "atbs", "orders",
                               "spend", "revenue", "cpc", "ctr", "cr", "drr"])
    print(f"  [wb ads] товарный уровень (nm): {len(nm_recs)} строк (товар×кампания)", flush=True)
    # дневной трек per-nm (показы/клики/заказы/расход + позиция бустера) — для реакции на смену ставки
    daily_recs = []
    for (dd, aid, nmid), da in nm_daily.items():
        cl, sp = da["clicks"], da["spend"]
        daily_recs.append({
            "account": account, "dt": dd, "advert_id": aid, "nm_id": nmid,
            "views": da["views"], "clicks": cl, "atbs": da["atbs"], "orders": da["orders"],
            "spend": round(sp, 2), "revenue": round(da["revenue"], 2),
            "cpc": round(sp / cl, 2) if cl else None,
            "booster_pos": booster.get((dd, aid, nmid)),
            "updated_at": datetime.datetime.now()})
    for (dd, aid, nmid), pos in booster.items():             # позиция есть, а рекл.показов нет — добить строкой
        if (dd, aid, nmid) not in nm_daily:
            daily_recs.append({"account": account, "dt": dd, "advert_id": aid, "nm_id": nmid,
                               "views": 0, "clicks": 0, "atbs": 0, "orders": 0, "spend": 0, "revenue": 0,
                               "cpc": None, "booster_pos": pos, "updated_at": datetime.datetime.now()})
    if daily_recs:
        db.upsert("wb_ad_nm_daily", daily_recs,
                  conflict_cols=["account", "dt", "advert_id", "nm_id"],
                  update_cols=["views", "clicks", "atbs", "orders", "spend", "revenue",
                               "cpc", "booster_pos", "updated_at"])
    print(f"  [wb ads] дневной трек (nm×день): {len(daily_recs)} строк", flush=True)
    db.upsert("ad_spend_daily",
              [{"account": account, "platform": "wb", "date": d, "spend": round(s, 2)}
               for d, s in daily.items()],
              conflict_cols=["account", "platform", "date"], update_cols=["spend"])
    ts = sum(r["spend"] for r in recs)
    tr = sum(r["revenue"] for r in recs)
    print(f"Записано кампаний: {n} | расход {ts:,.0f} ₽ | выручка {tr:,.0f} ₽ | "
          f"ДРР {round(ts/tr*100,1) if tr else '—'}%", flush=True)


if __name__ == "__main__":
    acc = sys.argv[1] if len(sys.argv) > 1 else "wb_acc1"
    per = sys.argv[2] if len(sys.argv) > 2 else None
    main(acc, per)
