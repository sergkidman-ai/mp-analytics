#!/usr/bin/env python3
# поток: mkt
"""ops/wb_daily_report.py — ежедневная сводка ВБ acc1 Сергею в телеграм.

Задача (Сергей, 2026-08-07): пока ведём лестницу ставок +10 % через день, каждый день видеть,
что происходит с ОБЩЕЙ видимостью и органикой, а не с рекламной атрибуцией.

Что внутри:
  1. Воронка за вчера (sales-funnel/v3) — сравнение с ТЕМ ЖЕ ДНЁМ НЕДЕЛИ неделю назад.
     Сравнивать соседние дни нельзя: у картриджей выходные проваливаются (см. память
     feedback_weekday_comparability).
  2. Кор (wb_bid_override) против остального каталога — растёт ли хвост следом за кором (гало).
  3. Реклама за вчера + ДРР за 7 дней (граница Сергея — 10 %).
  4. Лестница ставок: средняя ставка кора, когда был последний шаг.
  5. Контроль маржи: сколько SKU ниже порога и в минусе (mkt_margin_control).

Отчёт уходит в @Pro_Dropbox_bot на ЯВНЫЙ chat_id Сергея (см. память project_mp_telegram_channels:
id из TG_NOTIFY_ID/DROPBOX_ALLOWED_IDS принадлежат другим людям — брать их нельзя).

Запуск:  ./venv/bin/python -m ops.wb_daily_report [--dry]   # --dry: напечатать, не отправлять
"""
import os
import sys
import time
import argparse
import datetime
import pathlib

import requests
from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

load_dotenv(BASE_DIR / ".env")

FUNNEL_URL = "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products"
ACCOUNT = "wb_acc1"
SERGEY_CHAT_ID = "1031321444"
DRR_LIMIT = 10.0


def _funnel_day(day, core):
    """Итоги воронки за один день + разрез кор/не-кор. Возвращает dict или None при отказе API."""
    token = os.getenv("WB_TOKEN_ACC1", "")
    h = {"Authorization": token, "Content-Type": "application/json"}
    agg = {"sku": 0, "open": 0, "cart": 0, "ord": 0, "c_open": 0, "c_ord": 0, "n_open": 0}
    offset = 0
    while True:
        body = {"nmIDs": [], "brandNames": [], "subjectIDs": [], "tagIDs": [],
                "selectedPeriod": {"start": day, "end": day},
                "orderBy": {"field": "openCard", "mode": "desc"}, "limit": 1000, "offset": offset}
        r = requests.post(FUNNEL_URL, headers=h, json=body, timeout=180)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "20")) + 2)
            continue
        if r.status_code != 200:
            print(f"[воронка {day}] HTTP {r.status_code}", flush=True)
            return None
        prods = (r.json().get("data") or {}).get("products", [])
        tail = 0
        for p in prods:
            s = (p.get("statistic") or {}).get("selected") or {}
            oc = s.get("openCount") or 0
            tail = oc
            if oc <= 0:
                continue
            nm = (p.get("product") or {}).get("nmId")
            o = s.get("ordersCount") or s.get("orderCount") or 0
            agg["sku"] += 1
            agg["open"] += oc
            agg["cart"] += s.get("addToCartCount") or s.get("cartCount") or 0
            agg["ord"] += o
            if nm in core:
                agg["c_open"] += oc
                agg["c_ord"] += o
            else:
                agg["n_open"] += oc
        if len(prods) < 1000 or tail <= 0:
            break
        offset += 1000
        time.sleep(3)
    return agg


def _pct(now, was):
    if not was:
        return "—"
    return f"{100*(now-was)/was:+.0f}%"


def build():
    y = datetime.date.today() - datetime.timedelta(days=1)
    prev = y - datetime.timedelta(days=7)
    core = {r["nm_id"] for r in db.query(
        "SELECT nm_id FROM wb_bid_override WHERE account=%s", (ACCOUNT,))}

    a = _funnel_day(y.isoformat(), core)
    time.sleep(3)
    b = _funnel_day(prev.isoformat(), core)
    L = []
    dow = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"][y.weekday()]
    L.append(f"*ВБ · {y.strftime('%d.%m')} ({dow})* — против {prev.strftime('%d.%m')}, тот же день недели")

    if a and b:
        L.append("")
        L.append(f"Переходы в карточку: *{a['open']}* ({_pct(a['open'], b['open'])})")
        L.append(f"  кор {a['c_open']} ({_pct(a['c_open'], b['c_open'])}) · "
                 f"хвост {a['n_open']} ({_pct(a['n_open'], b['n_open'])})")
        L.append(f"SKU с трафиком: *{a['sku']}* ({_pct(a['sku'], b['sku'])})")
        L.append(f"Корзины {a['cart']} ({_pct(a['cart'], b['cart'])}) · "
                 f"заказы *{a['ord']}* ({_pct(a['ord'], b['ord'])})")
        cr = 100 * a["ord"] / a["open"] if a["open"] else 0
        cr0 = 100 * b["ord"] / b["open"] if b["open"] else 0
        L.append(f"Конверсия переход→заказ: {cr:.1f}% (было {cr0:.1f}%)")
    else:
        L.append("_воронка недоступна (API вернул отказ)_")

    ad = db.query("""
      SELECT round(sum(spend)) sp, sum(clicks) cl, sum(orders) o, round(sum(revenue)) rv
        FROM wb_ad_nm_daily WHERE account=%s AND dt=%s""", (ACCOUNT, y))
    ad7 = db.query("""
      SELECT round(sum(spend)) sp, round(sum(revenue)) rv
        FROM wb_ad_nm_daily WHERE account=%s AND dt > current_date - 7""", (ACCOUNT,))[0]
    L.append("")
    if ad and ad[0]["sp"] is not None:
        r0 = ad[0]
        drr = 100 * float(r0["sp"]) / float(r0["rv"]) if r0["rv"] else None
        L.append(f"Реклама вчера: {r0['sp']:.0f} ₽ · клики {r0['cl']} · заказы {r0['o']}"
                 + (f" · ДРР {drr:.1f}%" if drr is not None else ""))
    else:
        L.append("Реклама вчера: _данных ещё нет (ВБ отдаёт с суточным лагом)_")
    if ad7["rv"]:
        d7 = 100 * float(ad7["sp"]) / float(ad7["rv"])
        flag = "  ⚠️ ВЫШЕ ГРАНИЦЫ" if d7 > DRR_LIMIT else ""
        L.append(f"ДРР за 7 дней: *{d7:.1f}%* (граница {DRR_LIMIT:.0f}%){flag}")

    lad = db.query("""
      SELECT round(avg(cpc),2) cpc, count(*) n, max(updated_at)::date::text d
        FROM wb_bid_override WHERE account=%s""", (ACCOUNT,))[0]
    last = db.query("""
      SELECT ts::date::text d, count(*) n FROM wb_bid_log
       WHERE applied AND author='ladder' GROUP BY 1 ORDER BY 1 DESC LIMIT 1""")
    L.append("")
    L.append(f"Лестница: кор {lad['n']} SKU, средняя ставка {lad['cpc']} ₽"
             + (f"; последний шаг {last[0]['d']} ({last[0]['n']} SKU)" if last else "; шагов ещё не было"))

    mc = db.query("""
      SELECT count(*) n, count(*) FILTER (WHERE below_threshold) below,
             count(*) FILTER (WHERE is_negative) neg, max(captured_date)::text d
        FROM mkt_margin_control
       WHERE account=%s AND captured_date=(SELECT max(captured_date) FROM mkt_margin_control WHERE account=%s)
         AND buy_price_live IS NOT NULL""", (ACCOUNT, ACCOUNT))[0]
    if mc["n"]:
        L.append(f"Маржа-live ({mc['d']}): ниже порога {mc['below']}, в минусе {mc['neg']} из {mc['n']}")
    return "\n".join(L)


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
    ap.add_argument("--dry", action="store_true", help="напечатать, не отправлять")
    args = ap.parse_args()
    msg = build()
    print(msg)
    if not args.dry:
        send(msg)
