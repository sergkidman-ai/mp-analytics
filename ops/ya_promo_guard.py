"""ops/ya_promo_guard.py — поток: mkt
Сторож акций Яндекс Маркета. Третий в семье: ops/wb_promo_guard.py, ops/ozon_promo_guard.py.

Зачем: у Маркета есть автоучастие («Бестселлеры» добавляют товар сами при подходящей цене и
переносят его в следующую волну), поэтому в акции может оказаться товар, который мы продаём
в минус.

Решения Сергея 17.09.2026:
  пол   = (себест + max(300 ₽, 10 % себеста)) / доля выручки после удержаний. Себестоимость —
          та же цепочка, что у сторожей ВБ и Ozon (наличие СЕЙЧАС), через ozon_promo_guard.cost_map;
  доля  = ПЛОСКАЯ за последний закрытый месяц (price_quarantine.retention()['ya_acc1'], авг-2026
          удержания 66,65 % → нам остаётся 33,35 %), не по диапазонам цены;
  ход   = promoPrice ниже пола → поднять promoPrice до пола, если пол ≤ maxPromoPrice;
          иначе (и когда себестоимости нет) — убрать товар из акции;
  цены каталога НЕ трогаем: minimumForBestseller не ставим (решение «только сторож»).

Механики: правим только DIRECT_DISCOUNT и BLUE_FLASH — update других механик API не принимает.
MARKET_PROMOCODE не трогаем вовсе (схема оплаты скидки непрозрачна), такие акции идут в отчёт.

С полом сравниваем promoPrice: это цена, от которой считается наша выручка. Софинансирование
Маркета (буст, ценовая стратегия) в API не видно и наших денег не уменьшает.

Маркет применяет и update, и delete ЧЕРЕЗ 4–6 ЧАСОВ, поэтому сверки сразу после отправки нет:
строки остаются в статусе sent, а сверяет их следующий прогон (mode='verify' в журнале).

Запуск (по умолчанию расчёт, в кабинет ничего не уходит):
    ./venv/bin/python -m ops.ya_promo_guard                          # dry-run
    ./venv/bin/python -m ops.ya_promo_guard --csv /tmp/ya.csv        # + построчно
    ./venv/bin/python -m ops.ya_promo_guard --apply --wave morning   # исполнение
Журнал: ya_promo_guard_log (migrations/125_ya_promo_guard.sql).
"""
import argparse
import math
import os
import pathlib
import re
import sys
import time

import requests

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from dotenv import load_dotenv                                   # noqa: E402
load_dotenv(BASE_DIR / ".env")
from core import db                                              # noqa: E402
from ops import ozon_promo_guard as g                            # noqa: E402
from ops import ozon_stock_action as oz                          # noqa: E402
from ops import price_quarantine as q                            # noqa: E402

ACCOUNT = "ya_acc1"                  # кабинет один, businessId 879051
API = "https://api.partner.market.yandex.ru"
EDITABLE = ("DIRECT_DISCOUNT", "BLUE_FLASH")   # механики, где update разрешён
SKIP_MECHANICS = ("MARKET_PROMOCODE",)         # не трогаем вовсе
# Статусы участия, которые нас касаются. NOT_PARTICIPATING — товар лишь кандидат, его не трогаем.
LIVE_STATUSES = ("AUTO", "PARTIALLY_AUTO", "MANUAL", "RENEWED", "MINIMUM_FOR_PROMOS")
PAGE = 500                           # потолок страницы у Маркета
BATCH = 500                          # потолок товаров в update/delete
PACE = 0.8                           # лимиты: 1000 запросов/ч на список акций, 5000/ч на товары
PRICE_EPS = 0.5
MAX_DISCOUNT_SHARE = 0.95            # promoPrice не выше 95 % зачёркнутой цены — правило Маркета
COGS_MIN_SHARE = 0.5                 # себест известен меньше чем у половины — цепочка сломана
COGS_MIN_ROWS = 20


def _req(path, body=None, tries=4):
    """POST к Маркету с обработкой 420/429 (превышен лимит)."""
    H = {"Api-Key": os.getenv("YANDEX_API_KEY_ACC1"), "Content-Type": "application/json"}
    for _ in range(tries):
        r = requests.post(API + path, headers=H, json=body or {}, timeout=90)
        if r.status_code in (420, 429):
            time.sleep(int(r.headers.get("Retry-After", "10")) + 1)
            continue
        r.raise_for_status()
        time.sleep(PACE)
        return r.json()
    raise RuntimeError(f"{path}: не удалось за {tries} попыток")


def _biz():
    return os.getenv("YANDEX_BUSINESS_ID_ACC1")


def promos():
    """Акции, в которых кабинет участвует сейчас."""
    res = _req(f"/v2/businesses/{_biz()}/promos", {"participation": "PARTICIPATING_NOW"})
    return (res.get("result") or {}).get("promos", []) or []


def promo_offers(promo_id):
    """Товары акции (постранично). Фильтр `statuses` в запросе НЕ используем: его значения
    (MANUALLY_ADDED, NOT_MANUALLY_ADDED…) — другой словарь, чем статусы в ответе, и любой из
    наших даёт 400 «Illegal input at statuses[0]». Проще отобрать по ответу — см. LIVE_STATUSES."""
    out, token = [], None
    while True:
        body = {"promoId": promo_id}
        path = f"/v2/businesses/{_biz()}/promos/offers?limit={PAGE}" + (f"&page_token={token}" if token else "")
        res = _req(path, body).get("result") or {}
        out += [o for o in (res.get("offers", []) or []) if o.get("status") in LIVE_STATUSES]
        token = (res.get("paging") or {}).get("nextPageToken")
        if not token:
            return out


def snapshot():
    """Строки «товар × акция» с себестоимостью и полом. Возвращает (rows, promos, keep)."""
    ret, month = q.retention()
    keep = 1 - float(ret[ACCOUNT])
    acts = promos()
    rows, skipped = [], []
    for a in acts:
        mi = a.get("mechanicsInfo") or {}
        mech = [m.get("type") for m in mi] if isinstance(mi, list) else [mi.get("type")]
        if any(m in SKIP_MECHANICS for m in mech if m):
            skipped.append(a)
            continue
        for o in promo_offers(a.get("id")):
            d = (o.get("params") or {}).get("discountParams") or o.get("discountParams") or {}
            rows.append({"account": ACCOUNT, "promo_id": a.get("id"), "promo_name": a.get("name"),
                         "mechanics": ",".join(m for m in mech if m),
                         "offer_id": o.get("offerId"), "status": o.get("status"),
                         "price": float(d.get("price") or 0),
                         "promo_price": float(d.get("promoPrice") or 0),
                         "max_promo_price": float(d.get("maxPromoPrice") or 0),
                         "keep_ratio": keep})
    cogs = g.cost_map(sorted({r["offer_id"] for r in rows if r["offer_id"]}))
    for r in rows:
        r["cogs"], r["cogs_source"] = cogs.get(r["offer_id"], (None, "НЕТ"))
    return rows, acts, skipped, keep, month


def decide(r):
    """mode: ok | raise | remove | frozen (механику править нельзя)."""
    r["floor_price"] = r["new_price"] = None
    if r["cogs"] is None:
        r["mode"], r["reason"] = "remove", f"себестоимость не найдена ({r['cogs_source']})"
        return r
    floor = oz.floor_price(float(r["cogs"]), r["keep_ratio"])
    r["floor_price"] = round(floor, 2)
    if r["promo_price"] > 0 and r["promo_price"] >= floor - PRICE_EPS:
        r["mode"], r["reason"] = "ok", "проходит по полу"
        return r
    target = float(math.ceil(floor))
    editable = any(m in EDITABLE for m in (r["mechanics"] or "").split(","))
    cap = min(r["max_promo_price"] or 0, math.floor(r["price"] * MAX_DISCOUNT_SHARE) if r["price"] else 0)
    if editable and cap > 0 and target <= cap:
        r["mode"], r["new_price"] = "raise", target
        r["reason"] = f"цена {r['promo_price']:.0f} < пол {floor:.0f}, поднимаем"
    else:
        r["mode"] = "remove"
        r["reason"] = (f"пол {floor:.0f} > потолок {cap:.0f}" if editable
                       else f"механику {r['mechanics']} править нельзя, пол {floor:.0f}")
    return r


def push(rows):
    """update — поднять promoPrice, delete — убрать товар. Отказ в подъёме → убираем."""
    for pid in sorted({r["promo_id"] for r in rows if r["mode"] in ("raise", "remove")}):
        part = [r for r in rows if r["promo_id"] == pid]
        up = [r for r in part if r["mode"] == "raise"]
        for k in range(0, len(up), BATCH):
            chunk = up[k:k + BATCH]
            res = _req(f"/v2/businesses/{_biz()}/promos/offers/update",
                       {"promoId": pid,
                        "offers": [{"offerId": r["offer_id"],
                                    "params": {"discountParams": {"price": int(r["price"]),
                                                                  "promoPrice": int(r["new_price"])}}}
                                   for r in chunk]}).get("result") or {}
            rej = {x.get("offerId"): x.get("reason") for x in (res.get("rejectedOffers") or [])}
            warn = {x.get("offerId"): ",".join(w.get("code", "") for w in (x.get("warnings") or []))
                    for x in (res.get("warningOffers") or [])}
            for r in chunk:
                if r["offer_id"] in rej:
                    r["note"] = f"подъём отклонён: {rej[r['offer_id']]}"
                    r["mode"], r["new_price"] = "remove", None
                    r["reason"] += " → отказ Маркета, убираем"
                else:
                    r["status_row"], r["note"] = "sent", warn.get(r["offer_id"])
        rm = [r for r in part if r["mode"] == "remove"]
        for k in range(0, len(rm), BATCH):
            chunk = rm[k:k + BATCH]
            res = _req(f"/v2/businesses/{_biz()}/promos/offers/delete",
                       {"promoId": pid, "offerIds": [r["offer_id"] for r in chunk]}).get("result") or {}
            rej = {x.get("offerId"): x.get("reason") for x in (res.get("rejectedOffers") or [])}
            for r in chunk:
                if r["offer_id"] in rej:
                    r["status_row"], r["note"] = "error", f"убрать не вышло: {rej[r['offer_id']]}"
                else:
                    r["status_row"] = "sent"


def verify_previous(rows, wave):
    """Маркет применяет правки 4–6 часов, поэтому прошлый прогон сверяет СЛЕДУЮЩИЙ.

    Берём строки в статусе sent старше 6 часов и смотрим, что с товаром сейчас: убранный
    не должен быть в акции, поднятый — стоять не ниже присланной цены."""
    now = {(r["promo_id"], r["offer_id"]): r for r in rows}
    old = db.query("""select id, promo_id, offer_id, mode, new_price from ya_promo_guard_log
                       where status = 'sent' and ts < now() - interval '6 hours'""")
    done = {"confirmed": 0, "error": 0}
    for o in old:
        cur = now.get((o["promo_id"], o["offer_id"]))
        if o["mode"] == "remove":
            good = cur is None
        else:
            good = cur is not None and cur["promo_price"] >= float(o["new_price"] or 0) - PRICE_EPS
        st = "confirmed" if good else "error"
        done[st] += 1
        db.execute("update ya_promo_guard_log set status = %s, note = coalesce(note, '') || %s where id = %s",
                   (st, f" · сверка {wave}: {'ок' if good else 'не применилось'}", o["id"]))
    if sum(done.values()):
        print(f"  сверка прошлых отправок: подтверждено {done['confirmed']}, не применилось {done['error']}",
              flush=True)
    return done


COLS = ["account", "wave", "promo_id", "promo_name", "mechanics", "offer_id", "status",
        "price", "promo_price", "max_promo_price", "cogs", "cogs_source", "keep_ratio",
        "floor_price", "new_price", "mode", "reason", "status_row", "note"]
DB_COLS = [c if c != "status" else "promo_status" for c in COLS]
DB_COLS = [c if c != "status_row" else "status" for c in DB_COLS]


def save(rows, wave):
    if not rows:
        return
    import psycopg2.extras
    with db.get_conn() as conn, conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur, f"insert into ya_promo_guard_log ({', '.join(DB_COLS)}) values %s",
            [tuple({**r, "wave": wave}.get(c) for c in COLS) for r in rows], page_size=500)


def summary(rows, acts, skipped, keep, month, apply):
    m = {}
    for r in rows:
        m[r["mode"]] = m.get(r["mode"], 0) + 1
    lines = [f"*Маркет* · сторож акций · {'исполнение' if apply else 'расчёт'}",
             f"акций {len(acts)}, позиций {len(rows)}, остаётся нам {keep*100:.1f}% ({month})",
             f"  · проходят: {m.get('ok', 0)}  · поднять: {m.get('raise', 0)}  · убрать: {m.get('remove', 0)}"]
    if skipped:
        lines.append("  · не трогаем (промокоды): " + ", ".join((a.get("name") or "")[:25] for a in skipped))
    why = {}
    for r in rows:
        if r["mode"] in ("raise", "remove"):
            k = re.sub(r"\d+", "N", r["reason"].split(" (")[0])
            why[k] = why.get(k, 0) + 1
    for k, n in sorted(why.items(), key=lambda x: -x[1])[:4]:
        lines.append(f"  – {k}: {n}")
    if apply:
        lines.append("отправлено, Маркет применит через 4–6 ч; сверит следующий прогон")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Маркет: сторож акций — поднять или убрать то, что ниже пола")
    ap.add_argument("--apply", action="store_true", help="отправить в кабинет (по умолчанию расчёт)")
    ap.add_argument("--wave", default="manual", help="метка прогона в журнале")
    ap.add_argument("--limit", type=int, help="не больше N правок за прогон")
    ap.add_argument("--csv", help="построчный расчёт в файл")
    ap.add_argument("--no-log", action="store_true", help="не писать журнал")
    ap.add_argument("--no-tg", action="store_true", help="не слать сводку в бот")
    a = ap.parse_args()
    rows, acts, skipped, keep, month = snapshot()
    for r in rows:
        decide(r)
        r["status_row"], r["note"] = "dry", None
    if not a.no_log:
        verify_previous(rows, a.wave)
    known = sum(1 for r in rows if r["cogs"] is not None)
    if len(rows) >= COGS_MIN_ROWS and known < COGS_MIN_SHARE * len(rows):
        msg = (f"⚠️ *Маркет* · сторож акций: себестоимость найдена у {known} из {len(rows)} — "
               f"цепочка себеста сломана, прогон пропущен")
        print(msg.replace("*", ""), flush=True)
        if a.apply and not a.no_tg:
            oz.send(msg)
        return
    g._cut(rows, a.limit)
    if a.apply:
        for r in rows:
            if r["mode"] in ("ok", "ok_deferred"):
                r["status_row"] = "skip"
        push(rows)
    if not a.no_log:
        save(rows, a.wave)
    txt = summary(rows, acts, skipped, keep, month, a.apply)
    print(txt.replace("*", ""), flush=True)
    if a.apply and not a.no_tg and any(r["mode"] in ("raise", "remove") for r in rows):
        oz.send(txt)
    if a.csv and rows:
        head = ["promo_name", "mechanics", "offer_id", "status", "price", "promo_price",
                "max_promo_price", "cogs", "cogs_source", "floor_price", "new_price", "mode",
                "status_row", "reason", "note"]
        pathlib.Path(a.csv).write_text(
            "\n".join([";".join(head)] + [";".join("" if r.get(c) is None else str(r.get(c)) for c in head)
                                          for r in rows]), encoding="utf-8")
        print(f"\nпострочно → {a.csv}", flush=True)


if __name__ == "__main__":
    main()
