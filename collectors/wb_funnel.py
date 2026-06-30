"""collectors/wb_funnel.py — воронка продаж WB (трафик, клики, конверсии, рейтинг карточки).

POST https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products
Раздел «Аналитика» (Воронка продаж). Отдаёт за период по каждой карточке:
  openCount  — переходы в карточку (трафик/клики),
  cartCount  — добавления в корзину,
  orderCount/orderSum   — заказы,
  buyoutCount/buyoutSum — выкупы,
  conversions (корзина%, корзина→заказ%, выкуп%),
  feedbackRating — рейтинг карточки по отзывам, productRating — внутренний рейтинг,
  + блок past (прошлый сопоставимый период) для динамики.

Пагинация: page инкрементом, страница = 50, стоп при <50. Лимитер WB жёсткий → backoff на 429.
Ключ period = первое число месяца (как в margin_by_sku); end текущего месяца = сегодня.

Запуск:  ./venv/bin/python collectors/wb_funnel.py [oz->wb_acc1] [YYYY-MM-01]
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
URL = "https://seller-analytics-api.wildberries.ru/api/analytics/v3/sales-funnel/products"
TOKEN_ENV = {"wb_acc1": "WB_TOKEN_ACC1", "wb_acc2": "WB_TOKEN_ACC2"}
MIN_OPENS = 10   # порог значимого трафика: ниже — длинный хвост 14k SKU, для воронки не нужен


def _token(account):
    t = os.getenv(TOKEN_ENV[account])
    if not t:
        raise RuntimeError(f"{TOKEN_ENV[account]} не задан в .env")
    return t


def _month_bounds(period_first):
    """period_first = date первого числа → (start, end). end текущего месяца = сегодня."""
    nxt = (period_first + datetime.timedelta(days=32)).replace(day=1)
    last = nxt - datetime.timedelta(days=1)
    today = datetime.date.today()
    if last > today:
        last = today
    return period_first.isoformat(), last.isoformat()


def _num(x):
    return x if isinstance(x, (int, float)) else 0


def _record(account, period_iso, p):
    pr = p.get("product", {})
    sel = p.get("statistic", {}).get("selected", {})
    past = p.get("statistic", {}).get("past", {})
    conv = sel.get("conversions", {})
    stk = pr.get("stocks", {})
    return {
        "account": account, "period": period_iso, "nm_id": pr.get("nmId"),
        "title": pr.get("title"), "vendor_code": pr.get("vendorCode"),
        "brand": pr.get("brandName"), "subject_name": pr.get("subjectName"),
        "product_rating": _num(pr.get("productRating")), "feedback_rating": _num(pr.get("feedbackRating")),
        "open_count": _num(sel.get("openCount")), "cart_count": _num(sel.get("cartCount")),
        "order_count": _num(sel.get("orderCount")), "order_sum": _num(sel.get("orderSum")),
        "buyout_count": _num(sel.get("buyoutCount")), "buyout_sum": _num(sel.get("buyoutSum")),
        "cancel_count": _num(sel.get("cancelCount")), "cancel_sum": _num(sel.get("cancelSum")),
        "add_to_cart_pct": _num(conv.get("addToCartPercent")),
        "cart_to_order_pct": _num(conv.get("cartToOrderPercent")),
        "buyout_pct": _num(conv.get("buyoutPercent")),
        "share_order_pct": _num(sel.get("shareOrderPercent")),
        "stock_wb": _num(stk.get("wb")), "stock_mp": _num(stk.get("mp")),
        "past_open_count": _num(past.get("openCount")), "past_order_sum": _num(past.get("orderSum")),
    }


def main(account="wb_acc1", period=None):
    if period is None:
        period = datetime.date.today().replace(day=1).isoformat()
    period_first = datetime.date.fromisoformat(period)
    piso = period_first.isoformat()
    start, end = _month_bounds(period_first)
    print(f"WB воронка {account} {start}..{end}", flush=True)
    H = {"Authorization": _token(account), "Content-Type": "application/json"}
    offset, total, low, LIM = 0, 0, 0, 1000
    # Пагинация WB v3 — через limit/offset (параметр page игнорируется!). Сорт openCard desc →
    # трафик по убыванию; пишем инкрементально (upsert), стоп когда хвост порции < MIN_OPENS показов
    # (дальше длинный хвост из 14k SKU, для воронки не нужен).
    while True:
        body = {"nmIDs": [], "brandNames": [], "subjectIDs": [], "tagIDs": [],
                "selectedPeriod": {"start": start, "end": end},
                "orderBy": {"field": "openCard", "mode": "desc"}, "limit": LIM, "offset": offset}
        r = requests.post(URL, headers=H, json=body, timeout=120)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "20")) + 2)
            continue
        r.raise_for_status()
        prods = (r.json().get("data") or {}).get("products", [])
        recs = [x for x in (_record(account, piso, p) for p in prods) if x["nm_id"]]
        if recs:
            db.upsert("wb_funnel", recs, conflict_cols=["account", "period", "nm_id"])
            total += len(recs)
            low += sum(1 for x in recs if x["feedback_rating"] and x["feedback_rating"] < 4.3)
        print(f"  [wb funnel] offset {offset}: +{len(prods)} (записано всего {total})", flush=True)
        if len(prods) < LIM:
            break
        tail_open = (prods[-1].get("statistic", {}).get("selected", {}).get("openCount") or 0)
        if tail_open < MIN_OPENS:
            print(f"  [wb funnel] трафик ниже {MIN_OPENS} показов — стоп (длинный хвост не нужен)", flush=True)
            break
        offset += LIM
        time.sleep(3)
    print(f"Записано: {total} карточек | рейтинг <4.3: {low}", flush=True)


if __name__ == "__main__":
    acc = sys.argv[1] if len(sys.argv) > 1 else "wb_acc1"
    per = sys.argv[2] if len(sys.argv) > 2 else None
    main(acc, per)
