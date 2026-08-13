#!/usr/bin/env python3
# поток: mkt
"""ops/wb_bid_lower.py — откат ставки на пол по кор-SKU, которые жгут бюджет без отдачи.

ЗАЧЕМ. Лестница (`wb_bid_ladder`) умеет только повышать: её гейты «замораживают» плохой SKU,
но уже поднятую ставку назад не отдают. Замер 13.08.2026 показал, куда это привело: из 5 956 ₽
расхода кора за 09–12.08 84 % (4 993 ₽) ушло в 303 товара с НУЛЁМ рекламных заказов, а средняя
цена клика по кору выросла с 8.37 до 11.56 ₽. Этот скрипт — недостающая половина механизма.

ПОЧЕМУ НЕ ПРОСТО «НЕТ ЗАКАЗОВ = ВНИЗ». Реклама и органика связаны: поднятая ставка тянет вверх
видимость карточки, а вместе с ней открытия и заказы, которые ВБ рекламе не припишет. Поэтому
отбор смотрит на ОБА источника и снимает ставку только там, где мёртвы оба:
  * есть заказы в рекламе (wb_ad_nm_daily)            → не трогаем;
  * есть заказы в органике (wb_search_report, Джем)   → не трогаем;
  * заказов нет, но товар кладут в корзину            → не трогаем, спрос живой,
                                                        вопрос к цене/карточке, не к ставке;
  * ни заказов, ни корзины при потраченных деньгах    → ставка на пол.
Окно рекламы и окно Джема берутся одинаковой длины и с одинаковым концом, иначе сравниваем
разные недели.

По умолчанию — DRY-RUN: считает, пишет CSV и печатает сводку, в ВБ НЕ отправляет ничего.
Живая запись только с --apply (данные ≠ разрешение писать; --apply даётся под конкретный прогон).

Запуск:
  ./venv/bin/python -m ops.wb_bid_lower                    # список и сводка, dry-run
  ./venv/bin/python -m ops.wb_bid_lower --apply            # живая запись
  ./venv/bin/python -m ops.wb_bid_lower --days 7 --apply   # другое окно
"""
import os
import sys
import csv
import io
import time
import argparse
import datetime
import pathlib

import requests
from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from ops.wb_bid_ladder import WB_ADS_HOST, TOKEN_ENV, FLOOR, _patch_group, _log  # noqa: E402

load_dotenv(BASE_DIR / ".env")


def pick(account, days, end):
    """Кор-SKU, мёртвые и в рекламе, и в органике за окно. Возвращает строки плана."""
    start = end - datetime.timedelta(days=days - 1)
    core = {r["nm_id"] for r in db.query(
        "select distinct nm_id from wb_bid_log where account=%s", (account,))}
    ads = {r["nm_id"]: r for r in db.query("""
        select nm_id, sum(views) v, sum(clicks) c, sum(orders) o, sum(spend) s
          from wb_ad_nm_daily where account=%s and dt between %s and %s
         group by 1""", (account, start, end))}
    # Джем отдаёт скользящее окно 7 дней с меткой начала: берём то, что кончается на end.
    jam_start = end - datetime.timedelta(days=6)
    org = {r["nm_id"]: r for r in db.query("""
        select nm_id, open_card, add_to_cart, orders, avg_position
          from wb_search_report where account=%s and period_start=%s""", (account, jam_start))}
    if not org:
        raise RuntimeError(f"нет отчёта Джема с окном от {jam_start} — без органики не отбираю")
    bids = {r["nm_id"]: r for r in db.query(
        "select nm_id, cpc, advert_id from wb_bid_override where account=%s", (account,))}

    plan, skip = [], {"есть заказы в рекламе": 0, "есть заказы в органике": 0,
                      "кладут в корзину": 0, "уже на полу": 0, "нет кампании": 0}
    for nm in core:
        a = ads.get(nm)
        if not a or float(a["s"] or 0) <= 0:
            continue
        if (a["o"] or 0) > 0:
            skip["есть заказы в рекламе"] += 1
            continue
        o = org.get(nm) or {}
        if (o.get("orders") or 0) > 0:
            skip["есть заказы в органике"] += 1
            continue
        if (o.get("add_to_cart") or 0) > 0:
            skip["кладут в корзину"] += 1
            continue
        b = bids.get(nm) or {}
        cpc = float(b["cpc"]) if b.get("cpc") is not None else None
        if cpc is None or cpc <= FLOOR:
            skip["уже на полу"] += 1
            continue
        if not b.get("advert_id"):
            skip["нет кампании"] += 1
            continue
        plan.append({"nm_id": nm, "advert_id": b["advert_id"], "old_cpc": cpc, "new_cpc": FLOOR,
                     "views": a["v"] or 0, "clicks": a["c"] or 0, "spend": float(a["s"] or 0),
                     "org_open": o.get("open_card") or 0,
                     "org_pos": float(o["avg_position"]) if o.get("avg_position") else None})
    return plan, skip, start, jam_start


def apply_step(account, plan, note):
    """Живая запись: один PATCH на кампанию, лог и override — как у лестницы, но author=lower."""
    token = os.getenv(TOKEN_ENV[account], "")
    if not token:
        raise RuntimeError(f"{TOKEN_ENV[account]} не задан")
    by_adv = {}
    for r in plan:
        by_adv.setdefault(r["advert_id"], []).append((r["nm_id"], r["new_cpc"]))
    ok = bad = 0
    logs = []
    now = datetime.datetime.now()
    for adv, pairs in by_adv.items():
        status, resp, body = _patch_group(token, adv, pairs)
        applied = status == 200
        for nm, cpc in pairs:
            old = next(r["old_cpc"] for r in plan if r["nm_id"] == nm)
            logs.append({"account": account, "nm_id": nm, "advert_id": adv, "action": "api_set",
                         "applied": applied, "old_cpc": old, "new_cpc": cpc,
                         "old_source": "api_set", "author": "lower", "note": note})
            ok, bad = (ok + 1, bad) if applied else (ok, bad + 1)
        if applied:
            db.upsert("wb_bid_override",
                      [{"account": account, "nm_id": nm, "cpc": cpc, "source": "api_set",
                        "advert_id": adv, "note": note, "author": "lower", "updated_at": now}
                       for nm, cpc in pairs],
                      conflict_cols=["account", "nm_id"],
                      update_cols=["cpc", "source", "advert_id", "note", "author", "updated_at"])
        else:
            print(f"  [!] кампания {adv}: HTTP {status} {str(resp)[:160]}", flush=True)
        time.sleep(0.5)
    if logs:
        _log(logs)
    return ok, bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="wb_acc1")
    ap.add_argument("--days", type=int, default=4, help="окно замера, дней (по умолчанию 4)")
    ap.add_argument("--end", default=None, help="последний день окна ГГГГ-ММ-ДД (по умолчанию максимум в данных)")
    ap.add_argument("--apply", action="store_true", help="живая запись в ВБ")
    ap.add_argument("--note", default=None)
    a = ap.parse_args()

    end = (datetime.date.fromisoformat(a.end) if a.end else
           db.query("select max(dt) d from wb_ad_nm_daily where account=%s", (a.account,))[0]["d"])
    plan, skip, start, jam_start = pick(a.account, a.days, end)
    note = a.note or f"lower→пол {end} окно {a.days}д"

    print(f"окно рекламы {start}…{end} | окно Джема {jam_start}…{jam_start + datetime.timedelta(days=6)}")
    print("не трогаем: " + ", ".join(f"{k} {v}" for k, v in skip.items() if v))
    if not plan:
        print("под откат никто не подходит")
        return
    saved = sum(r["spend"] * (1 - FLOOR / r["old_cpc"]) for r in plan)
    cs = sorted(r["old_cpc"] for r in plan)
    print(f"под откат на {FLOOR:.2f} ₽: {len(plan)} SKU | их расход {sum(r['spend'] for r in plan):,.0f} ₽"
          f" за {a.days} дн | кликов {sum(r['clicks'] for r in plan)} | заказов 0")
    print(f"ставка: мин {cs[0]:.2f} медиана {cs[len(cs)//2]:.2f} макс {cs[-1]:.2f} ₽"
          f" | ожидаемая экономия ≈ {saved:,.0f} ₽ за {a.days} дн ({saved/a.days*30:,.0f} ₽/мес)")

    out = BASE_DIR / "docs" / "reports" / f"mkt_wb_bid_lower_{end}.csv"
    with io.open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["nm_id", "advert_id", "ставка_₽", "новая_₽", "показы", "клики",
                    "расход_₽", "орг_открытий", "орг_позиция"])
        for r in sorted(plan, key=lambda r: -r["spend"]):
            w.writerow([r["nm_id"], r["advert_id"], f"{r['old_cpc']:.2f}", f"{FLOOR:.2f}",
                        r["views"], r["clicks"], f"{r['spend']:.2f}", r["org_open"],
                        f"{r['org_pos']:.0f}" if r["org_pos"] else ""])
    print(f"список: {out}")

    if not a.apply:
        print("DRY-RUN: в ВБ ничего не отправлено. Живая запись — с --apply")
        return
    ok, bad = apply_step(a.account, plan, note)
    print(f"записано: {ok} | отказов: {bad}")


if __name__ == "__main__":
    main()
