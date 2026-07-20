"""reports/ozon_mp_report.py — живой ТЕКУЩИЙ месяц + прогноз для вкладки «Отчёты МП · Ozon».

Реконструкция Финансы→Баланс Ozon из raw_ozon_transaction (10 строк ЛК), операционные
показатели (заказы/возвраты/средний чек) и COGS из margin_by_sku — для месяца, которого
ещё нет в статическом снапшоте (reports/data/mp_ozon_hist.json). Плюс прогноз на конец
месяца линейным run-rate (factor = дней_в_месяце / прошло_дней).

Закрытые месяцы (есть в hist period_keys) — статика. Живой месяц = календарный месяц
после последнего замороженного (max period_keys); после дозаписи месяца в period_keys
(reports/ozon_mp_freeze) эндпоинт сам перейдёт на следующий. hist читается через _hist()
с перечиткой по mtime (файл — runtime state, переписывается заморозкой без рестарта).

⚠ Продажную сторону текущего месяца берём из транзакционных accruals (Отчёт о реализации
выходит ~8–10 числа следующего месяца) — это ОЦЕНКА; сплит Продаж (Выручка/Баллы/Программы)
только из реализации → для живого месяца недоступен («—»).

Подсветка ячеек (cls) — те же 3 блока относительно среднего янв–июнь, что и статика
(±0.5σ, инверсия для расходов). Форматирование — те же функции, что в генераторе страницы.
DB-only, вызывается на запросе из web/app.py (/api/ozon/mp-current).
"""
import json
import calendar
import datetime as dt
import pathlib
from collections import defaultdict

from core import db

HIST_PATH = pathlib.Path(__file__).resolve().parent / "data" / "mp_ozon_hist.json"

# hist JSON — RUNTIME STATE: run_daily/заморозка переписывают его без рестарта uvicorn.
# Держим кэш с перечиткой по mtime, иначе веб-процесс залипнет на старом period_keys
# (закрытый месяц отрендерился бы дважды: статикой И живым столбцом).
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

ACCOUNTS = ("oz_acc1", "oz_acc2")
EXP = ["returns", "commission", "delivery", "partners", "fbo", "promo", "penalty"]
WINDOW_DAYS = 14  # окно скользящей дневной ставки для прогноза («период в прошлом»)
_BAL_KEYS = ["sales", "returns", "commission", "delivery", "partners", "fbo",
             "promo", "penalty", "compensation", "other"]
MONTHS_RU = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн",
             "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]

# kind по ключу строки — определяет формат и направление подсветки
KIND = {
    "sales": "inflow", "rev": "inflow", "bon": "inflow", "par": "inflow",
    "orders": "count_up", "returns_cnt": "count_dn", "check": "check",
    "returns": "expense", "commission": "expense", "delivery": "expense",
    "partners": "expense", "fbo": "expense", "promo": "expense", "penalty": "expense",
    "itog": "expense", "cogs": "expense",
    "compensation": "inflow", "other": "inflow", "net": "inflow", "margin": "margin",
}


# ---------- реконструкция Баланса (порт scratchpad/oz_balance.py) ----------
def _svc_line(n):
    if "Stars" in n:                return "partners"     # звёзды → услуги партнёров
    if "Membership" in n:           return "promo"        # подписка-процент
    if "Acquiring" in n:            return "partners"     # эквайринг
    if "Redistribution" in n:       return "partners"
    if "PremiumCashback" in n or "IndividualPoints" in n: return "promo"
    if "Storage" in n or "MovementFromWarehouse" in n or "CargoAssortment" in n: return "fbo"
    if "VolumeWeight" in n or "Disposal" in n: return "penalty"
    return "delivery"


def _resid_line(ot):
    o = ot.lower()
    if ("costperclick" in o or "pointsforreviews" in o or "acceleratedproductreviews" in o
            or "promotionwithcostperorder" in o or "subscription" in o or "membership" in o
            or "premiumcashback" in o or "individualpoints" in o):        return "promo"
    if "defectrate" in o or "defectfine" in o:                            return "penalty"
    if ot == "MarketplaceAgencyFeeAggregator3plRFBS":                     return "delivery"
    if "rfbs" in o:                                                       return "partners"
    if ot in ("OperationCourierPickUpDelivery", "OperationCourierArrangement"): return "delivery"
    if "servicestorage" in o:                                             return "fbo"
    if "claim" in o or "compensation" in o or "accrual" in o:             return "compensation"
    if "correction" in o:                                                 return "other"
    if "reexposure" in o:                                                 return "other"
    return "other"


def _balance_range(account, d1, d2):
    """(mags, sub) за полуинтервал дат [d1, d2) — d1/d2 строки 'YYYY-MM-DD'.
    `sub` = фиксированная абонплата подписки (operation_type `OperationSubscription*`),
    сидящая ВНУТРИ строки `promo`: она приходит пачкой (разово ~раз в месяц), в прогнозе
    её держим отдельно, не размазываем ставкой. %-подписка (`PremiumMembershipCommission`)
    сюда НЕ входит — идёт по дням, пропорц. продажам, попадает в общий поток promo."""
    rows = db.query(
        """SELECT payload FROM raw_ozon_transaction WHERE account=%s
             AND (payload->>'operation_date')::date>=%s
             AND (payload->>'operation_date')::date<%s""",
        (account, d1, d2))
    L = defaultdict(float)
    sub = 0.0
    for r in rows:
        p = r["payload"]
        acc = float(p.get("accruals_for_sale") or 0)
        cm = float(p.get("sale_commission") or 0)
        am = float(p.get("amount") or 0)
        if acc > 0:
            L["sales"] += acc
        else:
            L["returns"] += -acc
        L["commission"] += -cm
        ss = 0.0
        for s in (p.get("services") or []):
            pr = float(s.get("price") or 0); ss += pr
            L[_svc_line(s.get("name", ""))] += -pr
        res = am - acc - cm - ss
        ot = p.get("operation_type", "")
        L[_resid_line(ot)] += -res
        if ot.startswith("OperationSubscription"):
            sub += -res
    return {k: abs(L.get(k, 0.0)) for k in _BAL_KEYS}, abs(sub)


def _month_bounds(y, m):
    d1 = f"{y}-{m:02d}-01"
    d2 = f"{y + 1}-01-01" if m == 12 else f"{y}-{m + 1:02d}-01"
    return d1, d2


def _accumulate(account, y, m):
    """(mags, sub) за календарный месяц — обёртка над _balance_range."""
    d1, d2 = _month_bounds(y, m)
    return _balance_range(account, d1, d2)


def balance(account, y, m):
    """{line: magnitude} — 10 строк Финансы→Баланс за месяц (положительные величины,
    как в снапшоте; знак/направление задаёт KIND при рендере)."""
    return _accumulate(account, y, m)[0]


def _sub_range(account, d1, d2):
    """Фикс-абонплата подписки за [d1, d2): −Σ amount по OperationSubscription* (у них
    нет accruals/комиссии/услуг → residual = amount)."""
    r = db.query(
        """SELECT coalesce(sum((payload->>'amount')::float), 0) s
             FROM raw_ozon_transaction WHERE account=%s
             AND payload->>'operation_type' LIKE 'OperationSubscription%%'
             AND (payload->>'operation_date')::date>=%s
             AND (payload->>'operation_date')::date<%s""",
        (account, d1, d2))
    return abs(float(r[0]["s"]))


def _expected_monthly_sub(account, y, m):
    """Ожидаемая фикс-подписка за месяц = медиана помесячных сумм OperationSubscription*
    за 3 полных месяца до (y,m). Устойчива к разовым доплатам (напр. июнь ЦК: PremiumPlus +
    PremiumPro = 49 980, тогда как обычный месяц = 24 990)."""
    vals = []
    yy, mm = y, m
    for _ in range(3):
        yy, mm = (yy - 1, 12) if mm == 1 else (yy, mm - 1)
        d1, d2 = _month_bounds(yy, mm)
        vals.append(_sub_range(account, d1, d2))
    vals.sort()
    return vals[len(vals) // 2]


def op_counts_range(account, d1, d2):
    """(заказы, возвраты) за [d1, d2) = distinct posting_number: accr>0 продажи /
    accr<0|товарный возврат."""
    rows = db.query(
        """SELECT payload->'posting'->>'posting_number' post,
                  (payload->>'accruals_for_sale')::float accr, payload->>'operation_type' ot
             FROM raw_ozon_transaction WHERE account=%s
             AND (payload->>'operation_date')::date>=%s
             AND (payload->>'operation_date')::date<%s""",
        (account, d1, d2))
    sales_p, ret_p = set(), set()
    for r in rows:
        p = r["post"]
        if not p:
            continue
        a = r["accr"] or 0
        if a > 0:
            sales_p.add(p)
        elif a < 0 or r["ot"] == "OperationReturnGoodsFBSofRMS":
            ret_p.add(p)
    return len(sales_p), len(ret_p)


def op_counts(account, y, m):
    """(заказы, возвраты) за календарный месяц — обёртка над op_counts_range."""
    d1, d2 = _month_bounds(y, m)
    return op_counts_range(account, d1, d2)


def _cogs(account, y, m):
    dt = f"{y}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"
    r = db.query("""SELECT coalesce(sum(cogs),0) c FROM margin_by_sku
        WHERE platform='ozon' AND account=%s AND period_from=%s AND period_to=%s""",
        (account, f"{y}-{m:02d}-01", dt))
    return float(r[0]["c"])


def _month_last_day(y, m):
    r = db.query(
        """SELECT max((payload->>'operation_date')::date) mx FROM raw_ozon_transaction
             WHERE (payload->>'operation_date')::date>=make_date(%s,%s,1)
             AND (payload->>'operation_date')::date<(make_date(%s,%s,1)+interval '1 month')""",
        (y, m, y, m))
    return r[0]["mx"]


def realiz_sales(account, y, m):
    """Продажи/Возвраты/Вознаграждение из ОФИЦИАЛЬНОГО Отчёта о реализации (как в ЛК Баланс).
    (prod, ret, comm) или None если отчёта за месяц ещё нет. Порт scratchpad/oz_balance.py.
    prod = Σ delivery_commission(amount+bonus+bank+pickup+stars) = sales_split.total;
    ret  = Σ return_commission(те же поля); comm = Σ(dc.standard_fee − rc.standard_fee)."""
    r = db.query("SELECT payload FROM raw_ozon_realization WHERE account=%s AND year=%s AND month=%s",
                 (account, y, m))
    if not r:
        return None
    dc = defaultdict(float); rc = defaultdict(float)
    for row in (r[0]["payload"].get("rows") or []):
        for k, v in (row.get("delivery_commission") or {}).items():
            if isinstance(v, (int, float)): dc[k] += v
        for k, v in (row.get("return_commission") or {}).items():
            if isinstance(v, (int, float)): rc[k] += v
    P = lambda d: d["amount"] + d["bonus"] + d["bank_coinvestment"] + d["pick_up_point_coinvestment"] + d["stars"]
    return round(P(dc)), round(P(rc)), round(dc["standard_fee"] - rc["standard_fee"])


def _live_month():
    """Живой месяц = календарный месяц СРАЗУ ПОСЛЕ последнего замороженного (max period_keys).
    Так закрытый, но ещё не замороженный месяц НЕ пропадает со страницы (виден живым, а при
    remaining=0 прогноз=факт), а после дозаписи в period_keys переход бесшовный. None — если
    истории нет вовсе. Реальные данные месяца проверяются в current_report (last==None → пусто)."""
    keys = _hist().get("period_keys", [])
    if not keys:
        return None
    ym = max(keys)
    y, m = int(ym[:4]), int(ym[5:7])
    return (y + 1, 1) if m == 12 else (y, m + 1)


# ---------- форматирование / подсветка (совпадает с gen_reports.py) ----------
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


def _hist_series(acc, key):
    """Ряд значений (уже через _basis) для эталона подсветки — ТОЛЬКО по НЕ-provisional
    (сверённым) месяцам, чтобы неполная оценка не смещала среднее/σ."""
    H = _hist()
    a = H["accounts"][acc]; L = a["lines"]; ob = L["sales"]; n = len(ob)
    keys = H.get("period_keys", [])
    prov = set(H.get("provisional", []))
    idx = [i for i in range(n) if i >= len(keys) or keys[i] not in prov]
    if key in _BAL_KEYS:
        vals = L[key]
    elif key == "itog":
        vals = [sum(L[x][i] for x in EXP) for i in range(n)]
    elif key in ("cogs", "net", "margin", "orders", "returns_cnt"):
        vals = a[key]
    elif key == "check":
        vals = [L["sales"][i] / a["orders"][i] if a["orders"][i] else 0 for i in range(n)]
    elif key in ("rev", "bon", "par"):
        mk = {"rev": "rev", "bon": "bonus", "par": "part"}[key]
        vals = [(s[mk] if s else 0) for s in a["split"]]
    else:
        vals = [0] * n
    return [_basis(key, vals[i], ob[i]) for i in idx]


def _band(acc, key, v, oborot_cur):
    """Класс ячейки vs исторический ряд сверённых месяцев: g/a (цвет) + up/dn (стрелка)."""
    if v is None:
        return ""
    hs = _hist_series(acc, key)
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


def _derive(mags, orders, retc, cogs):
    """itog/payout/net/margin/check из строк Баланса."""
    itog = sum(mags[k] for k in EXP)
    payout = mags["sales"] - itog + mags["compensation"] + mags["other"]
    net = payout - cogs
    sales = mags["sales"]
    return {**mags, "itog": itog, "cogs": cogs, "orders": orders, "returns_cnt": retc,
            "net": net, "margin": (net / sales * 100 if sales else 0),
            "check": (sales / orders if orders else 0),
            "rev": None, "bon": None, "par": None}


def current_report():
    """{"month": {...}|None, "accounts": {acc: {line_key: {"jul":{txt,cls},"fc":{txt,cls}}}}}"""
    lm = _live_month()
    if not lm:
        return {"month": None, "accounts": {}}
    y, m = lm
    last = _month_last_day(y, m)
    if not last:                       # живой месяц ещё без транзакций — не показываем пустой столбец
        return {"month": None, "accounts": {}}
    days_in = calendar.monthrange(y, m)[1]
    elapsed = last.day
    remaining = days_in - elapsed
    # окно скользящей дневной ставки: WINDOW_DAYS дней, кончая последним днём с данными.
    # Оно непрерывно и свободно пересекает границу месяца → «сплошной поток», переход
    # месяца для прогноза незначим (в начале августа окно ещё захватывает июль).
    if last:
        w1 = (last - dt.timedelta(days=WINDOW_DAYS - 1)).isoformat()
        w2 = (last + dt.timedelta(days=1)).isoformat()
    else:
        w1, w2 = _month_bounds(y, m)

    out = {}
    for acc in ACCOUNTS:
        mags, sub = _accumulate(acc, y, m)              # факт с начала месяца (MTD)
        orders, retc = op_counts(acc, y, m)
        cogs = _cogs(acc, y, m)
        actual = _derive(mags, orders, retc, cogs)
        # Прогноз = факт MTD + дневная_ставка(окно) × оставшиеся дни. Ставка берётся из
        # скользящего окна прошлого (WINDOW_DAYS дн), а не из неполного месяца → нет взрыва
        # factor в начале месяца, метод сходится к факту в конце. Пачечную фикс-подписку
        # (`sub` внутри promo) не размазываем: уже списана в этом месяце → берём факт, иначе
        # ждём один платёж (как в прошлом месяце). %-подписка идёт в общем потоке promo.
        win, win_sub = _balance_range(acc, w1, w2)
        rate = {k: win[k] / WINDOW_DAYS for k in win}
        sub_exp = sub if sub > 0 else _expected_monthly_sub(acc, y, m)
        fc_mags = {}
        for k in mags:
            if k == "promo":
                rate_ex = (win[k] - win_sub) / WINDOW_DAYS     # ставка promo без подписки
                fc_mags[k] = (mags[k] - sub) + rate_ex * remaining + sub_exp
            else:
                fc_mags[k] = mags[k] + rate[k] * remaining
        win_o, win_r = op_counts_range(acc, w1, w2)
        fc_orders = orders + win_o / WINDOW_DAYS * remaining
        fc_retc = retc + win_r / WINDOW_DAYS * remaining
        # COGS привязана к продажам (margin_by_sku помесячный, дневного среза нет):
        # forecast_cogs = forecast_sales × (COGS ÷ Продажи текущего месяца).
        cogs_ratio = cogs / mags["sales"] if mags["sales"] else 0
        fc_cogs = fc_mags["sales"] * cogs_ratio
        forecast = _derive(fc_mags, fc_orders, fc_retc, fc_cogs)

        cells = {}
        for key in KIND:
            jv, fv = actual.get(key), forecast.get(key)
            # столбец «тек.» — неполный месяц: подсвечиваем ТОЛЬКО относительные статьи
            # (доли расходов, маржа, средний чек), т.к. абсолютные суммы MTD заведомо ниже
            # среднего полного месяца → подсветка была бы ложной. Прогноз (полный месяц) — весь.
            jul_cls = _band(acc, key, jv, actual["sales"]) if KIND[key] in ("expense", "margin", "check") else ""
            cells[key] = {
                "jul": {"txt": _fmt(key, jv), "cls": jul_cls},
                "fc": {"txt": _fmt(key, fv), "cls": _band(acc, key, fv, forecast["sales"])},
            }
        out[acc] = cells

    return {
        "month": {"label": MONTHS_RU[m - 1], "month_key": f"{y}-{m:02d}",
                  "elapsed_days": elapsed, "days_in_month": days_in,
                  "remaining_days": remaining, "window_days": WINDOW_DAYS,
                  "last_date": last.isoformat() if last else None, "estimate": True},
        "accounts": out,
    }


if __name__ == "__main__":
    r = current_report()
    mo = r["month"]
    print("месяц:", mo)
    for acc in ACCOUNTS:
        c = r["accounts"][acc]
        print(f"\n{acc}: Продажи jul={c['sales']['jul']['txt']} fc={c['sales']['fc']['txt']} "
              f"| заказы {c['orders']['jul']['txt']}→{c['orders']['fc']['txt']} "
              f"| чек {c['check']['jul']['txt']}→{c['check']['fc']['txt']} "
              f"| COGS {c['cogs']['jul']['txt']}→{c['cogs']['fc']['txt']} "
              f"| маржа {c['margin']['jul']['txt']}→{c['margin']['fc']['txt']} "
              f"| чистая {c['net']['jul']['txt']}→{c['net']['fc']['txt']}")
