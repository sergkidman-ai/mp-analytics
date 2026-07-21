# -*- coding: utf-8 -*-
# поток: fin
"""reports/yandex_mp_report.py — живой ТЕКУЩИЙ месяц + прогноз для вкладки «Отчёты МП · Яндекс».

Один аккаунт (ya_acc1) — у Яндекс.Маркета одно юрлицо, поэтому страница = ОДНА таблица (в
отличие от Ozon/WB с двумя). Источник и закрытых, и живого месяца — витрина
`yandex_finance_monthly` (её пересобирает `collectors.yandex_monthly` в run_daily: заказы+услуги
из Партнёр-API + order-based COGS из МойСклад). Закрытые месяцы уходят в статику
(reports/data/mp_yandex_hist.json) заморозкой; живой месяц отдаёт /api/yandex/mp-current.

⚠ ОСОБЕННОСТЬ ЖИВОГО МЕСЯЦА. COGS у ЯМ order-based (списывается в МЕСЯЦ ЗАКАЗА целиком, по
позициям МС), а выручка/субсидия реализуются по мере доставки. В неполном месяце часть заказов
ещё «в пути» (DELIVERY/PICKUP/PROCESSING) — их себест уже учтён, а выручка добирается позже →
маржа MTD СТРУКТУРНО ЗАНИЖЕНА (напр. июль-2026 ≈27% против ≈44% закрытого июня). Поэтому:
  • столбец «тек.» = факт MTD как есть (честный «пока набрано»), помечен как заниженный;
  • «прогноз» НЕ масштабирует занижённую MTD-маржу, а проецирует ПОЛНЫЙ месяц по юнит-экономике
    ЗАКРЫТЫХ месяцев × ожидаемое число заказов (заказы прогнозируются дневной ставкой из
    raw_yandex_stats_order — надёжный дневной сигнал). Так прогноз-маржа сходится к норме.

Форматирование/подсветка — те же функции и та же логика ±0.5σ, что в Ozon/WB. DB-only,
вызывается на запросе из web/app.py (/api/yandex/mp-current).
"""
import json
import calendar
import datetime as dt
import pathlib

from core import db

HIST_PATH = pathlib.Path(__file__).resolve().parent / "data" / "mp_yandex_hist.json"

# hist JSON — RUNTIME STATE: run_daily/заморозка переписывают его без рестарта uvicorn.
_HIST_CACHE = {"mtime": None, "data": None}


def _hist():
    try:
        mtime = HIST_PATH.stat().st_mtime
    except OSError:
        return _HIST_CACHE["data"] or {}
    if _HIST_CACHE["mtime"] != mtime:
        _HIST_CACHE["data"] = json.loads(HIST_PATH.read_text(encoding="utf-8"))
        _HIST_CACHE["mtime"] = mtime
    return _HIST_CACHE["data"]


ACCOUNTS = ("ya_acc1",)
# сырые строки витрины yandex_finance_monthly (положительные величины)
_BAL_KEYS = ["revenue", "subsidy", "fee", "delivery", "transfer", "promotion",
             "agency", "other_fee", "subscription_cost", "reviews_cost",
             "boost_sales", "boost_shows", "shelf"]
# расходы площадки, из которых складывается «Итого расходы Маркета» (= mp_cost в _ya_business).
# promotion — родительская строка (= boost_sales+boost_shows+shelf), в сумму берём ЕЁ, а бусты
# показываем под-строками (не суммируем дважды).
MP_EXP = ["fee", "delivery", "transfer", "promotion", "agency", "other_fee",
          "subscription_cost", "reviews_cost"]
WINDOW_DAYS = 14
MONTHS_RU = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн",
             "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]

KIND = {
    "revenue": "inflow", "subsidy": "inflow", "own": "inflow",
    "orders": "count_up", "returns_cnt": "count_dn", "check": "check",
    "fee": "expense", "delivery": "expense", "transfer": "expense", "promotion": "expense",
    "boost_sales": "expense", "boost_shows": "expense", "shelf": "expense",
    "agency": "expense", "other_fee": "expense", "subscription_cost": "expense",
    "reviews_cost": "expense", "itog": "expense", "cogs": "expense",
    "payout": "inflow", "net": "inflow", "margin": "margin",
}


# ---------- чтение витрины ----------
def _month_key(y, m):
    return f"{y}-{m:02d}"


def _row(y, m):
    """Строка yandex_finance_monthly за месяц (month = дата YYYY-MM-01) или None."""
    r = db.query(
        """SELECT revenue::float revenue, subsidy::float subsidy, orders,
                  returns_orders, fee::float fee, delivery::float delivery,
                  transfer::float transfer, promotion::float promotion, agency::float agency,
                  other_fee::float other_fee, subscription_cost::float subscription_cost,
                  reviews_cost::float reviews_cost, cogs::float cogs,
                  boost_sales::float boost_sales, boost_shows::float boost_shows,
                  shelf::float shelf
             FROM yandex_finance_monthly
             WHERE account='ya_acc1' AND month=make_date(%s,%s,1)""",
        (y, m))
    return r[0] if r else None


def balance(y, m):
    """{line: magnitude} сырых строк за месяц (0 если строки нет)."""
    r = _row(y, m)
    return {k: float((r[k] if r else 0) or 0) for k in _BAL_KEYS}


def op_counts(y, m):
    r = _row(y, m)
    if not r:
        return 0, 0
    return int(r["orders"] or 0), int(r["returns_orders"] or 0)


def _cogs(y, m):
    r = _row(y, m)
    return float((r["cogs"] if r else 0) or 0)


# ---------- деривативы (та же формула, что _ya_business) ----------
def _derive(m, orders, retc, cogs):
    """own(оборот=выручка+субсидия) → Итого расходы Маркета → К перечислению → минус COGS = Чистая."""
    own = m["revenue"] + m["subsidy"]
    itog = sum(m[k] for k in MP_EXP)
    payout = own - itog
    net = payout - cogs
    return {**m, "own": own, "itog": itog, "payout": payout, "cogs": cogs,
            "orders": orders, "returns_cnt": retc, "net": net,
            "margin": (net / own * 100 if own else 0),
            "check": (own / orders if orders else 0)}


# ---------- живой месяц / прогноз ----------
def _live_month():
    keys = _hist().get("period_keys", [])
    if not keys:
        return None
    ym = max(keys)
    y, m = int(ym[:4]), int(ym[5:7])
    return (y + 1, 1) if m == 12 else (y, m + 1)


def _last_order_date(y, m):
    """Последняя дата заказа (creationDate) в живом месяце — по ней меряем «прошло дней»."""
    r = db.query(
        """SELECT max(payload->>'creationDate') mx FROM raw_yandex_stats_order
             WHERE payload->>'creationDate' LIKE %s""",
        (f"{y}-{m:02d}-%",))
    mx = r[0]["mx"] if r else None
    return dt.date.fromisoformat(mx) if mx else None


def _raw_count(d1, d2):
    """Число созданных заказов в [d1, d2] включительно (строки 'YYYY-MM-DD')."""
    r = db.query(
        """SELECT count(*) c FROM raw_yandex_stats_order
             WHERE payload->>'creationDate' BETWEEN %s AND %s""",
        (d1, d2))
    return int(r[0]["c"] or 0)


def _closed_unit_econ(live_key, n=3):
    """Средняя юнит-экономика последних n ЗАКРЫТЫХ месяцев (period_keys < live_key): per-order по
    каждой сырой строке + cogs, и медиана фикс-подписки. Живой месяц Яндекса структурно занижен —
    прогноз опираем на стабильную экономику закрытых месяцев, а не на MTD."""
    H = _hist()
    keys = [k for k in H.get("period_keys", []) if k < live_key]
    keys = sorted(keys)[-n:]
    if not keys:
        return None
    a = H["accounts"]["ya_acc1"]; L = a["lines"]
    idx = [H["period_keys"].index(k) for k in keys]
    tot_ord = sum(a["orders"][i] for i in idx) or 1
    per = {k: sum(L[k][i] for i in idx) / tot_ord for k in _BAL_KEYS}
    per["cogs"] = sum(a["cogs"][i] for i in idx) / tot_ord
    per["_orders_idx"] = idx
    subs = sorted(L["subscription_cost"][i] for i in idx)
    per["_sub_month"] = subs[len(subs) // 2]         # медиана месячной подписки (фикс)
    per["_ret_rate"] = sum(a["returns_cnt"][i] for i in idx) / tot_ord
    return per


def current_report():
    """{"month": {...}|None, "accounts": {"ya_acc1": {line_key: {"cur":{txt,cls},"fc":{txt,cls}}}}}"""
    lm = _live_month()
    if not lm:
        return {"month": None, "accounts": {}}
    y, m = lm
    if not _row(y, m):                       # живого месяца ещё нет в витрине — пустой столбец не рисуем
        return {"month": None, "accounts": {}}
    last = _last_order_date(y, m)
    if not last:
        return {"month": None, "accounts": {}}
    days_in = calendar.monthrange(y, m)[1]
    elapsed = last.day
    remaining = days_in - elapsed
    live_key = _month_key(y, m)

    mags = balance(y, m)
    orders, retc = op_counts(y, m)
    cogs = _cogs(y, m)
    actual = _derive(mags, orders, retc, cogs)

    # --- прогноз: проекция полного месяца по юнит-экономике закрытых × ожидаемое число заказов ---
    per = _closed_unit_econ(live_key)
    d1 = f"{y}-{m:02d}-01"
    w1 = (last - dt.timedelta(days=WINDOW_DAYS - 1)).isoformat()
    raw_mtd = _raw_count(d1, last.isoformat())
    raw_win = _raw_count(w1, last.isoformat())
    daily = raw_win / WINDOW_DAYS
    # множитель роста заказов от дневной ставки скользящего окна (надёжный дневной сигнал);
    # уровень (число заказов витрины) масштабируем этим множителем.
    growth = ((raw_mtd + daily * remaining) / raw_mtd) if raw_mtd else 1.0
    proj_orders = orders * growth

    if per and proj_orders > 0:
        fc_mags = {k: per[k] * proj_orders for k in _BAL_KEYS}
        fc_mags["subscription_cost"] = per["_sub_month"]     # фикс-подписка, не масштабируем
        # promotion держим согласованной с бустами (родитель = сумма частей)
        fc_mags["promotion"] = fc_mags["boost_sales"] + fc_mags["boost_shows"] + fc_mags["shelf"]
        fc_cogs = per["cogs"] * proj_orders
        fc_retc = per["_ret_rate"] * proj_orders
        forecast = _derive(fc_mags, proj_orders, fc_retc, fc_cogs)
    else:
        forecast = {k: None for k in KIND}

    cells = {}
    for key in KIND:
        cv, fv = actual.get(key), forecast.get(key)
        # «тек.» (неполный месяц): подсвечиваем только относительные статьи (доли/маржа/чек),
        # абсолютные MTD-суммы заведомо ниже среднего → ложная подсветка.
        cur_cls = _band(key, cv, actual["own"]) if KIND[key] in ("expense", "margin", "check") else ""
        cells[key] = {
            "cur": {"txt": _fmt(key, cv), "cls": cur_cls},
            "fc": {"txt": _fmt(key, fv), "cls": _band(key, fv, forecast.get("own"))},
        }

    return {
        "month": {"label": MONTHS_RU[m - 1], "month_key": live_key,
                  "elapsed_days": elapsed, "days_in_month": days_in,
                  "remaining_days": remaining, "window_days": WINDOW_DAYS,
                  "last_date": last.isoformat(), "estimate": True},
        "accounts": {"ya_acc1": cells},
    }


# ---------- форматирование / подсветка ----------
def _money(v, neg=False):
    v = round(v); s = f"{abs(v):,}".replace(",", " ")
    return ("−" if (neg or v < 0) else "") + s


def _fmt(key, v):
    if v is None:
        return "—"
    k = KIND[key]
    if k == "margin":
        return f"{v:.1f}%"
    if k in ("count_up", "count_dn"):
        return f"{round(v):,}".replace(",", " ")
    return _money(v, neg=(k == "expense"))


def _basis(key, v, oborot):
    return (v / oborot * 100 if oborot else 0) if KIND[key] == "expense" else v


def _good_up(key):
    return KIND[key] not in ("expense", "count_dn")


def _hist_series(key):
    """Ряд значений (через _basis) закрытых месяцев для эталона подсветки."""
    H = _hist()
    a = H["accounts"]["ya_acc1"]; L = a["lines"]
    ob = [L["revenue"][i] + L["subsidy"][i] for i in range(len(a["orders"]))]
    n = len(ob)
    if key == "own":
        vals = ob
    elif key in _BAL_KEYS:
        vals = L[key]
    elif key == "itog":
        vals = [sum(L[x][i] for x in MP_EXP) for i in range(n)]
    elif key == "payout":
        vals = [ob[i] - sum(L[x][i] for x in MP_EXP) for i in range(n)]
    elif key in ("cogs", "net", "margin", "orders", "returns_cnt"):
        vals = a[key]
    elif key == "check":
        vals = [ob[i] / a["orders"][i] if a["orders"][i] else 0 for i in range(n)]
    else:
        vals = [0] * n
    return [_basis(key, vals[i], ob[i]) for i in range(n)]


def _band(key, v, oborot_cur):
    if v is None:
        return ""
    hs = _hist_series(key)
    if not hs:
        return ""
    mean = sum(hs) / len(hs)
    std = (sum((x - mean) ** 2 for x in hs) / len(hs)) ** 0.5
    if std == 0:
        return ""
    b = _basis(key, v, oborot_cur)
    d = b - mean
    if abs(d) <= 0.5 * std:
        return ""
    gu = _good_up(key)
    qual = ("g" if d > 0 else "a") if gu else ("a" if d > 0 else "g")
    return f"{qual} {'up' if d > 0 else 'dn'}"


if __name__ == "__main__":
    r = current_report()
    mo = r["month"]
    print("месяц:", mo)
    if mo:
        c = r["accounts"]["ya_acc1"]
        for k in ("orders", "own", "revenue", "subsidy", "itog", "payout", "cogs", "net", "margin"):
            print(f"  {k:10} тек={c[k]['cur']['txt']:>12}  прогноз={c[k]['fc']['txt']:>12}")
