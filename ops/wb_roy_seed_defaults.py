#!/usr/bin/env python3
# поток: mkt
"""ops/wb_roy_seed_defaults.py — отдать Рою позиции, которые крутятся на цене клика кампании по умолчанию.

ЗАЧЕМ. Рой ведёт только позиции со строкой в `wb_bid_override` (~1 800 acc1, 380 acc2). Остальные
тысячи позиций в cpc-кампаниях стоят на минимальной цене клика, которую ВБ ставит при заведении,
и не двигаются. Замер 14–17.09: у ведомых 16,7 показа на позицию против 4,9 (acc1), 9,4 против 6,1
(acc2) при не худшей цене заказа. НО ведомые когда-то отбирались как продающие — сравнение смещено
отбором. Поэтому волна 1 — ЭКСПЕРИМЕНТ, а не раскатка.

ВОЛНА 1 (одобрена Сергеем 18.09.2026): acc2, позиции в cpc-кампаниях, у которых
  · за 14 дней был хотя бы один рекламный клик (без реакции цена клика покупает пустые показы:
    замер 01–09.08 в ops/wb_bid_seed_converting — CTR 0,018 % у позиций без органики);
  · нет строки в wb_bid_override; маржа ≥ 25 %; живой остаток > 0.
Делим 50/50 детерминированно (blake2b 'seed-w1:<nm>'): рука `seed` получает строку в
wb_bid_override с ТЕКУЩЕЙ ценой клика из состава кампании (ничего не меняется в ВБ сейчас — дальше
её ведёт недельный прогон Роя в понедельник: 🟢 +10 %, 🔴 −10 %, 🟤/⚫ пол). Рука `control` не трогается.
Протокол обеих рук — docs/reports/mkt_roy_seed_w1_<дата>_<acc>.csv. Разбор через 2 недели: показы,
клики, заказы и расход на позицию, seed против control.

В ВБ этот скрипт НЕ пишет — только в нашу БД (--apply). Цена клика в ВБ меняется понедельничным Роем.

  ./venv/bin/python -m ops.wb_roy_seed_defaults --account wb_acc2            # dry-run
  ./venv/bin/python -m ops.wb_roy_seed_defaults --account wb_acc2 --apply
"""
import sys
import csv
import time
import hashlib
import argparse
import datetime
import pathlib
import collections

import requests

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from ops.wb_ad_enroll import WB_ADS_HOST, _token, live_stock  # noqa: E402

MIN_MARGIN = 25.0
SOURCE = "seed_w1"


def arm(nm_id):
    h = hashlib.blake2b(f"seed-w1:{nm_id}".encode(), digest_size=8).digest()
    return "seed" if int.from_bytes(h, "big") % 2 == 0 else "control"


def cpc_positions(account):
    """{nm_id: [(advert_id, search_bid_rub), ...]} по всем cpc-кампаниям в статусах 9/11."""
    H = {"Authorization": _token(account)}
    cnt = requests.get(WB_ADS_HOST + "/adv/v1/promotion/count", headers=H, timeout=60).json()
    ids = [a["advertId"] for g in cnt.get("adverts") or [] if g.get("status") in (9, 11)
           for a in g.get("advert_list") or []]
    out = collections.defaultdict(list)
    for k in range(0, len(ids), 50):
        for _ in range(4):
            r = requests.get(WB_ADS_HOST + "/api/advert/v2/adverts", headers=H,
                             params={"ids": ",".join(map(str, ids[k:k + 50]))}, timeout=60)
            if r.status_code != 429:
                break
            time.sleep(5)
        r.raise_for_status()
        for a in r.json().get("adverts") or []:
            if (a.get("settings") or {}).get("payment_type") != "cpc":
                continue
            for n in a.get("nm_settings") or []:
                out[int(n["nm_id"])].append((int(a["id"]), (n.get("bids_kopecks") or {}).get("search", 0) / 100))
        time.sleep(1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="wb_acc2", choices=["wb_acc1", "wb_acc2"])
    ap.add_argument("--apply", action="store_true", help="записать руку seed в wb_bid_override")
    a = ap.parse_args()
    acc = a.account

    pos = cpc_positions(acc)
    managed = {r["nm_id"] for r in db.query("select nm_id from wb_bid_override where account=%s", (acc,))}
    clicks = {r["nm_id"]: r for r in db.query("""
        select nm_id, sum(clicks) c, sum(views) v, sum(orders) o, max(advert_id) filter (where clicks>0) adv_c
          from wb_ad_nm_daily where account=%s and dt>current_date-15 group by 1 having sum(clicks)>0""", (acc,))}
    mg = {r["nm_id"]: float(r["m"]) for r in db.query("""
        select distinct on (nm_id) nm_id, margin_own_live m from mkt_margin_control
         where account=%s and margin_own_live is not null order by nm_id, captured_date desc""", (acc,))}
    pool = [n for n in pos if n not in managed and n in clicks and mg.get(n, -1) >= MIN_MARGIN]
    stock = live_stock(pool)
    pool = [n for n in pool if stock.get(n)]
    print(f"{acc}: позиций в cpc-кампаниях {len(pos)}; не ведомых Роем {len(set(pos) - managed)}; "
          f"с кликом за 14 дн {sum(1 for n in pos if n not in managed and n in clicks)}; "
          f"+ маржа ≥ {MIN_MARGIN:.0f} % и остаток → пул {len(pool)}")

    rows = []
    for n in sorted(pool):
        camps = pos[n]
        adv_c = clicks[n]["adv_c"]
        adv, bid = next(((c, b) for c, b in camps if c == adv_c), camps[0])
        rows.append({"nm_id": n, "arm": arm(n), "advert_id": adv, "cpc_now": bid,
                     "clicks14": int(clicks[n]["c"]), "views14": int(clicks[n]["v"]),
                     "orders14": int(clicks[n]["o"]), "margin": round(mg[n], 1), "stock": stock[n]})
    seed = [r for r in rows if r["arm"] == "seed"]
    ctl = [r for r in rows if r["arm"] == "control"]
    for lab, g in (("seed", seed), ("control", ctl)):
        print(f"  {lab:7}: {len(g)} позиций · клики {sum(r['clicks14'] for r in g)} · показы "
              f"{sum(r['views14'] for r in g)} · заказы {sum(r['orders14'] for r in g)} · "
              f"цена клика сейчас медиана {sorted(r['cpc_now'] for r in g)[len(g) // 2] if g else '—'} ₽")

    stem = datetime.date.today().isoformat()
    proto = BASE_DIR / "docs" / "reports" / f"mkt_roy_seed_w1_{stem}_{acc}.csv"
    if not a.apply:
        print("DRY-RUN: ничего не записано. Запись руки seed в wb_bid_override: --apply")
        return
    with open(proto, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()), delimiter=";")
        w.writeheader()
        w.writerows(rows)
    for r in seed:
        db.execute("""insert into wb_bid_override (account, nm_id, cpc, source, advert_id, note, author, updated_at)
                      values (%s,%s,%s,%s,%s,%s,%s, now())
                      on conflict do nothing""",
                   (acc, r["nm_id"], max(r["cpc_now"], 7.30), SOURCE, r["advert_id"],
                    "волна 1 рычага 1: рука seed, контроль в протоколе", "claude"))
    got = db.query("select count(*) n from wb_bid_override where account=%s and source=%s", (acc, SOURCE))[0]["n"]
    print(f"записано: рука seed {got} из {len(seed)}; протокол {proto.name}")


if __name__ == "__main__":
    main()
