#!/usr/bin/env python3
# поток: mkt
"""Алерт «вышла из сумрака»: у TheCartridge впервые появилась живая закупка по нашей карточке.

Зачем. Позиции, которых нет у TheCartridge, обычно нет и в наличии — считать по ним нечего,
поэтому витрина `mkt_sku_economics` оставляет их БЕЗ себеста и БЕЗ маржи (никаких оценок по
предмету, решение Сергея 07.08.2026). Момент, когда цена появляется, надо не пропустить:
позиция становится закупаемой, и обычная формула юнит-экономики сразу даёт по ней маржу.

Порядок запуска (важен): коллектор TheCartridge → `reports.sku_economics` → ЭТОТ скрипт.
Тогда алерт несёт уже посчитанную маржу, а не «цена появилась, экономика будет завтра».

Идемпотентность: journal `mkt_tc_resurfaced` (миграция 113) — один ряд на карточку, повторно
о той же карточке не сообщаем, даже если цена потом пропадёт и появится снова.

    ./venv/bin/python -m ops.tc_price_alert --dry     # напечатать, не отправлять
"""
import argparse
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import db                                    # noqa: E402
from reports.margin_control import _mapping            # noqa: E402  — общий мост nm → код TheCartridge

ACCOUNT = "wb_acc1"
SERGEY_CHAT_ID = 1031321444        # только явный chat_id, см. память про телеграм-каналы
TOP_N = 15                         # сколько позиций показывать в сообщении построчно


def _find(account):
    """Карточки, по которым цена TheCartridge появилась ВПЕРВЫЕ и о которых мы ещё не сообщали."""
    # Коды с ценой в последнем снимке.
    live = {r["external_code"]: float(r["buy_price"]) for r in db.query("""
        SELECT external_code, buy_price FROM tc_buy_price_latest
         WHERE status = 'ok' AND buy_price IS NOT NULL AND buy_price > 0
    """)}
    if not live:
        return [], None
    day = db.query("SELECT max(captured_date) d FROM tc_buy_price")[0]["d"]

    # Коды, у которых цена была хоть раз РАНЬШЕ последнего снимка → это не «из сумрака».
    seen = {r["external_code"] for r in db.query("""
        SELECT DISTINCT external_code FROM tc_buy_price
         WHERE captured_date < %s AND buy_price IS NOT NULL AND buy_price > 0
    """, (day,))}
    fresh = {c: p for c, p in live.items() if c not in seen}
    if not fresh:
        return [], day

    done = {r["nm_id"] for r in db.query(
        "SELECT nm_id FROM mkt_tc_resurfaced WHERE account=%s", (account,))}
    m = _mapping(account, set(fresh))                  # nm_id → (external_code, ...)
    nms = {nm: e[0] for nm, e in m.items() if e and e[0] in fresh and nm not in done}
    if not nms:
        return [], day

    econ = {r["nm_id"]: r for r in db.query("""
        SELECT nm_id, margin_own AS margin_own, net_u, promo_price, name
          FROM (SELECT e.nm_id, e.margin_pct_own AS margin_own, e.net_u, e.promo_price,
                       c.payload->>'title' AS name
                  FROM mkt_sku_economics e
                  LEFT JOIN raw_wb_card_content c ON c.nm_id = e.nm_id AND c.account = e.account
                 WHERE e.account = %s AND e.nm_id = ANY(%s::bigint[])) t
    """, (account, list(nms)))}

    out = []
    for nm, code in nms.items():
        e = econ.get(nm, {})
        out.append({
            "nm_id": nm, "external_code": code, "buy_price": fresh[code],
            "margin_own": float(e["margin_own"]) if e.get("margin_own") is not None else None,
            "net_u": float(e["net_u"]) if e.get("net_u") is not None else None,
            "promo_price": float(e["promo_price"]) if e.get("promo_price") is not None else None,
            "name": (e.get("name") or "")[:40],
        })
    # Сначала самые интересные: где маржа известна и высокая.
    out.sort(key=lambda r: (r["margin_own"] is None, -(r["margin_own"] or 0)))
    return out, day


def build(rows, day):
    if not rows:
        return None
    L = [f"*Вышли из сумрака* ({day}): {len(rows)} SKU",
         "У TheCartridge впервые появилась закупка — позиция стала считаемой."]
    withm = [r for r in rows if r["margin_own"] is not None]
    if withm:
        good = [r for r in withm if r["margin_own"] >= 25]
        L.append(f"Маржа посчитана по {len(withm)}: с маржой ≥25% — {len(good)}.")
    nom = len(rows) - len(withm)
    if nom:
        L.append(f"Без маржи пока {nom} — нет цены на витрине ВБ.")
    L.append("")
    for r in rows[:TOP_N]:
        m = f"{r['margin_own']:.0f}%" if r["margin_own"] is not None else "—"
        p = f"{r['promo_price']:.0f}₽" if r["promo_price"] else "цены нет"
        L.append(f"{r['nm_id']} {r['name']} · закуп {r['buy_price']:.0f}₽ · цена {p} · маржа {m}")
    if len(rows) > TOP_N:
        L.append(f"…и ещё {len(rows) - TOP_N}")
    return "\n".join(L)


def mark(account, rows, day):
    db.upsert("mkt_tc_resurfaced",
              [{"account": account, "nm_id": r["nm_id"], "external_code": r["external_code"],
                "first_price_on": day, "buy_price": r["buy_price"], "margin_own": r["margin_own"]}
               for r in rows],
              ["account", "nm_id"], update_cols=[])   # уже сообщали → ничего не трогаем


def send(text):
    token = os.getenv("DROPBOX_BOT_TOKEN", "")
    if not token:
        print("нет DROPBOX_BOT_TOKEN — отправка пропущена", flush=True)
        return False
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": SERGEY_CHAT_ID, "text": text, "parse_mode": "Markdown"},
                      timeout=30)
    ok = r.status_code == 200 and r.json().get("ok")
    print(f"телеграм: {'отправлено' if ok else 'ОШИБКА ' + r.text[:200]}", flush=True)
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="напечатать и НЕ отмечать в журнале")
    a = ap.parse_args()
    rows, day = _find(ACCOUNT)
    msg = build(rows, day)
    if not msg:
        print(f"из сумрака никто не вышел (снимок {day})", flush=True)
        sys.exit(0)
    print(msg, flush=True)
    if not a.dry:
        send(msg)
        mark(ACCOUNT, rows, day)     # отмечаем после отправки — сбой связи не съест алерт
