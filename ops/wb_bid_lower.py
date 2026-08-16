#!/usr/bin/env python3
# поток: mkt
"""ops/wb_bid_lower.py — понижение ставок WB: красный шаг −10 % и бордовый сброс на пол.

ЗАЧЕМ. Лестница (`wb_bid_ladder`) умеет только повышать: её гейты «замораживают» плохой SKU,
но уже поднятую ставку назад не отдают. Замер 13.08.2026 показал, куда это привело: из 5 956 ₽
расхода кора за 09–12.08 84 % (4 993 ₽) ушло в 303 товара с НУЛЁМ рекламных заказов, а средняя
цена клика по кору выросла с 8.37 до 11.56 ₽. Этот скрипт — недостающая половина механизма.

ДВА ЦВЕТА РОЯ (термины Сергея, 14.08.2026), два разных действия:

  🔴 КРАСНЫЙ → ставка −10 %. Товар живой: его заказывают в рекламе или в органике, кладут
     в корзину. Проблема не в спросе, а в цене клика — реклама съедает больше прибыли, чем
     этот товар может отдать. Сбрасывать такого на пол нельзя, потеряем работающие продажи;
     снижаем мягко и смотрим неделю.

  🟤 БОРДОВЫЙ → ставка на пол. Деньги потрачены, и мёртво ВСЁ: ни рекламного заказа,
     ни органического, ни даже добавления в корзину. Держать за него высокую ставку не за что.

ПОРОГ ДРР ИНДИВИДУАЛЬНЫЙ, а не общий «10 % на всех». KPI Сергея — чистая ≥25 % от нашей цены,
и реклама вычитается ровно из этой маржи. Значит товар с маржой 45 % может отдать в рекламу
20 % выручки и остаться в KPI, а товар с маржой 27 % — только 2 %:

    допустимый ДРР = маржа_live − 25          (WB_MARGIN_GATE из reports/bid_policy.py)

Фактический ДРР выше своего порога → красный. Маржа ниже жёсткого пола 15 % → красный без
разговоров: такой товар реклама не окупит ни при какой ставке.

ПОЧЕМУ НЕ ПРОСТО «НЕТ ЗАКАЗОВ = ВНИЗ». Реклама и органика связаны: поднятая ставка тянет вверх
видимость карточки, а вместе с ней открытия и заказы, которые ВБ рекламе не припишет. Поэтому
отбор смотрит на ОБА источника: заказ в органике или добавление в корзину выводит товар
из бордового в красный (мягкое снижение) или оставляет в покое. Окно рекламы и окно Джема
берутся одинаковой длины и с одинаковым концом, иначе сравниваем разные недели.

По умолчанию — DRY-RUN: считает, пишет CSV и печатает сводку, в ВБ НЕ отправляет ничего.
Живая запись только с --apply (данные ≠ разрешение писать; --apply даётся под конкретный прогон).

Запуск:
  ./venv/bin/python -m ops.wb_bid_lower                     # оба цвета, список и сводка, dry-run
  ./venv/bin/python -m ops.wb_bid_lower --mode red          # только красные −10 %
  ./venv/bin/python -m ops.wb_bid_lower --mode floor        # только бордовые на пол
  ./venv/bin/python -m ops.wb_bid_lower --apply             # живая запись
  ./venv/bin/python -m ops.wb_bid_lower --days 7 --apply    # другое окно
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
from ops.wb_bid_ladder import WB_ADS_HOST, TOKEN_ENV, FLOOR, _margin, _patch_group, _log  # noqa: E402
from reports.bid_policy import WB_MARGIN_GATE, WB_MARGIN_FLOOR  # noqa: E402

load_dotenv(BASE_DIR / ".env")

CUT = 0.90           # красный шаг: −10 % от текущей ставки (зеркало +10 % лестницы)
MIN_SPEND = 1.0      # ₽ за окно: ниже этого расход — не сигнал, а копейки, не трогаем


def allowed_drr(margin_own):
    """Сколько процентов выручки этот товар может отдать рекламе, оставшись в KPI-25 %.

    Реклама вычитается из той же маржи, по которой считается KPI Сергея, поэтому потолок ДРР
    у каждого товара свой: маржа 45 % → можно 20 %, маржа 27 % → только 2 %. Общий порог
    «10 % на всех» одним запрещает зарабатывать, другим разрешает уходить ниже KPI.
    None — маржа неизвестна, решение по ДРР не принимаем вовсе.
    """
    if margin_own is None:
        return None
    return max(0.0, margin_own - WB_MARGIN_GATE)


def pick(account, days, end, mode="all"):
    """Классифицирует SKU со ставкой в красные/бордовые за окно. Возвращает строки плана.

    Красный — живой товар с дорогой рекламой (ДРР выше своего порога или маржа ниже пола 15 %):
    ставка ×0.90. Бордовый — мёртвый по всем трём сигналам при потраченных деньгах: ставка на пол.
    """
    start = end - datetime.timedelta(days=days - 1)
    ads = {r["nm_id"]: r for r in db.query("""
        select nm_id, sum(views) v, sum(clicks) c, sum(orders) o, sum(spend) s, sum(revenue) rev
          from wb_ad_nm_daily where account=%s and dt between %s and %s
         group by 1""", (account, start, end))}
    # Джем отдаёт скользящее окно 7 дней с меткой начала. Идеал — окно, кончающееся на end,
    # но Джем приезжает на день-два позже рекламы, поэтому берём самое свежее доступное
    # и проверяем разрыв: сравнивать рекламу этой недели с органикой позапрошлой нельзя.
    want = end - datetime.timedelta(days=6)
    got = db.query("""select max(period_start) d from wb_search_report
                       where account=%s and period_start <= %s""", (account, want))
    jam_start = got[0]["d"] if got else None
    if jam_start is None:
        raise RuntimeError(f"нет ни одного отчёта Джема до {want} — без органики не отбираю")
    lag = (want - jam_start).days
    if lag > 2:
        raise RuntimeError(
            f"свежий отчёт Джема — окно от {jam_start}, отстаёт от рекламы на {lag} дн. "
            f"Сравнивать разные недели нельзя: сдвинь --end на {end - datetime.timedelta(days=lag)}")
    org = {r["nm_id"]: r for r in db.query("""
        select nm_id, open_card, add_to_cart, orders, avg_position
          from wb_search_report where account=%s and period_start=%s""", (account, jam_start))}
    bids = {r["nm_id"]: r for r in db.query(
        "select nm_id, cpc, advert_id from wb_bid_override where account=%s", (account,))}
    mg, mg_day = _margin(account, list(bids.keys()))

    plan, skip = [], {"расхода нет": 0, "уже на полу": 0, "нет кампании": 0,
                      "маржа неизвестна": 0, "в KPI, не трогаем": 0}
    for nm, b in bids.items():
        cpc = float(b["cpc"]) if b.get("cpc") is not None else None
        if cpc is None or cpc <= FLOOR:
            skip["уже на полу"] += 1
            continue
        if not b.get("advert_id"):
            skip["нет кампании"] += 1
            continue
        a = ads.get(nm) or {}
        spend = float(a.get("s") or 0)
        if spend < MIN_SPEND:
            skip["расхода нет"] += 1
            continue
        o = org.get(nm) or {}
        ad_ord, org_ord = a.get("o") or 0, o.get("orders") or 0
        cart = o.get("add_to_cart") or 0
        rev = float(a.get("rev") or 0)
        margin = (mg.get(nm) or (None, None, None))[1]
        drr = spend / rev * 100 if rev > 0 else None
        cap = allowed_drr(margin)

        # Бордовый проверяем первым: мёртвый по всем трём сигналам — решение не зависит от маржи.
        if ad_ord == 0 and org_ord == 0 and cart == 0:
            color, new = "🟤 бордовый", FLOOR
        elif margin is None:
            skip["маржа неизвестна"] += 1      # вслепую ставку не режем, как и не поднимаем
            continue
        elif margin < WB_MARGIN_FLOOR:
            color, new = "🔴 красный (маржа < пола 15%)", max(FLOOR, round(cpc * CUT, 2))
        elif drr is not None and cap is not None and drr > cap:
            color, new = "🔴 красный (ДРР выше своего порога)", max(FLOOR, round(cpc * CUT, 2))
        else:
            skip["в KPI, не трогаем"] += 1
            continue
        if mode == "red" and not color.startswith("🔴"):
            continue
        if mode == "floor" and not color.startswith("🟤"):
            continue
        if new >= cpc:                          # шаг упёрся в пол и ничего не меняет
            skip["уже на полу"] += 1
            continue
        plan.append({"nm_id": nm, "advert_id": b["advert_id"], "old_cpc": cpc, "new_cpc": new,
                     "color": color, "margin": margin, "drr": drr, "cap": cap,
                     "views": a.get("v") or 0, "clicks": a.get("c") or 0, "spend": spend,
                     "revenue": rev, "ad_orders": ad_ord, "org_orders": org_ord, "cart": cart,
                     "org_open": o.get("open_card") or 0,
                     "org_pos": float(o["avg_position"]) if o.get("avg_position") else None})
    return plan, skip, start, jam_start, mg_day


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
    ap.add_argument("--days", type=int, default=7, help="окно замера, дней (по умолчанию 7 — недельный такт)")
    ap.add_argument("--end", default=None, help="последний день окна ГГГГ-ММ-ДД (по умолчанию максимум в данных)")
    ap.add_argument("--mode", default="all", choices=["all", "red", "floor"],
                    help="all — оба цвета; red — только −10 %%; floor — только сброс на пол")
    ap.add_argument("--apply", action="store_true", help="живая запись в ВБ")
    ap.add_argument("--note", default=None)
    a = ap.parse_args()

    end = (datetime.date.fromisoformat(a.end) if a.end else
           db.query("select max(dt) d from wb_ad_nm_daily where account=%s", (a.account,))[0]["d"])
    plan, skip, start, jam_start, mg_day = pick(a.account, a.days, end, a.mode)
    note = a.note or f"lower {a.mode} {end} окно {a.days}д"

    print(f"окно рекламы {start}…{end} | окно Джема {jam_start}…{jam_start + datetime.timedelta(days=6)}"
          f" | маржа на {mg_day}")
    print("не трогаем: " + ", ".join(f"{k} {v}" for k, v in skip.items() if v))
    if not plan:
        print("под понижение никто не подходит")
        return

    by_color = {}
    for r in plan:
        by_color.setdefault(r["color"].split(" (")[0], []).append(r)
    print(f"\n{'цвет':22}{'SKU':>5}{'расход ₽':>10}{'выручка ₽':>11}{'ставка ср':>11}{'станет':>9}{'экономия ₽':>12}")
    total_saved = 0.0
    for color, g in sorted(by_color.items(), key=lambda kv: -sum(r["spend"] for r in kv[1])):
        saved = sum(r["spend"] * (1 - r["new_cpc"] / r["old_cpc"]) for r in g)
        total_saved += saved
        print(f"{color:22}{len(g):>5}{sum(r['spend'] for r in g):>10,.0f}"
              f"{sum(r['revenue'] for r in g):>11,.0f}"
              f"{sum(r['old_cpc'] for r in g)/len(g):>11.2f}"
              f"{sum(r['new_cpc'] for r in g)/len(g):>9.2f}{saved:>12,.0f}")
    print(f"{'ИТОГО':22}{len(plan):>5}{sum(r['spend'] for r in plan):>10,.0f}"
          f"{sum(r['revenue'] for r in plan):>11,.0f}{'':>20}{total_saved:>12,.0f}")
    print(f"экономия ≈ {total_saved:,.0f} ₽ за {a.days} дн ({total_saved/a.days*30:,.0f} ₽/мес)")

    out = BASE_DIR / "docs" / "reports" / f"mkt_wb_bid_lower_{end}_{a.mode}.csv"
    with io.open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["nm_id", "advert_id", "цвет", "ставка_₽", "новая_₽", "маржа_live_%",
                    "ДРР_факт_%", "ДРР_допустимый_%", "показы", "клики", "расход_₽", "выручка_₽",
                    "рекл_заказы", "орг_заказы", "орг_корзина", "орг_открытий", "орг_позиция"])
        for r in sorted(plan, key=lambda r: -r["spend"]):
            w.writerow([r["nm_id"], r["advert_id"], r["color"], f"{r['old_cpc']:.2f}",
                        f"{r['new_cpc']:.2f}",
                        f"{r['margin']:.1f}" if r["margin"] is not None else "",
                        f"{r['drr']:.1f}" if r["drr"] is not None else "",
                        f"{r['cap']:.1f}" if r["cap"] is not None else "",
                        r["views"], r["clicks"], f"{r['spend']:.2f}", f"{r['revenue']:.2f}",
                        r["ad_orders"], r["org_orders"], r["cart"], r["org_open"],
                        f"{r['org_pos']:.0f}" if r["org_pos"] else ""])
    print(f"список: {out}")

    if not a.apply:
        print("DRY-RUN: в ВБ ничего не отправлено. Живая запись — с --apply")
        return
    ok, bad = apply_step(a.account, plan, note)
    print(f"записано: {ok} | отказов: {bad}")


if __name__ == "__main__":
    main()
