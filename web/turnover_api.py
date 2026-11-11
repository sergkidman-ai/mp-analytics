# поток: fin
"""Роутер раздела «Оборот для премии» — деньги покупателя по месяцам, площадкам и фирмам.

База премии = ровно те показатели, которые Сергей читает в кабинетах (решение 28.09.2026):
- ВБ     «Продажа» = Σ retail_amount по строкам «Продажа», возвраты — по строкам «Возврат».
         Месяц берём по дате продажи (rr_dt), НЕ по дате формирования отчёта: недельные пачки
         сдвигают границы и месяц выглядит то вдвое больше, то вдвое меньше.
         Сверено: отчёт 771274605 → 382 949 − 24 402 = 358 547 ₽, цифра из ЛК.
- Ozon   «Реализовано на сумму» = Σ rows[].delivery_commission.amount отчёта о реализации,
         возвраты = Σ rows[].return_commission.amount. Сверено: июль oz_acc1 = 5 998 736,68.
         ВАЖНО: это деньги ПОКУПАТЕЛЯ; баллы Ozon сюда не входят (их платит площадка,
         за июль это ещё 4,53 млн ₽ сверху). Отчёт выходит 8–10 числа → текущего месяца нет.
- Маркет «Получено от потребителей» и «Возвращено потребителям» из детализации закрывающих
         документов (raw_yandex_closure, collectors/yandex_closure.py). Июнь сверен с ЛК
         с точностью 1 388 ₽ (одна транзакция по заказу 58253045507).

Комиссия, логистика, реклама и штрафы базу НЕ уменьшают — это уже не выручка.
"""
import sys
import pathlib
import datetime as dt
from collections import defaultdict
from threading import Lock

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core import db  # noqa: E402
from cf.mp_forecast import ACC_ORG  # noqa: E402

router = APIRouter(tags=["Оборот"])
STATIC = BASE_DIR / "web" / "static"
ORGS = {"7807355364": "Цифровой Квадрат", "7811803918": "Дисквэр"}
PLATFORMS = {"wb": "Wildberries", "ozon": "Ozon", "yandex": "Яндекс Маркет"}


def _wb(since):
    return db.query("""select account, to_char((payload->>'rr_dt')::date, 'YYYY-MM') ym,
                  coalesce(sum((payload->>'retail_amount')::numeric)
                           filter (where payload->>'doc_type_name' = 'Продажа'), 0)::float sales,
                  coalesce(sum((payload->>'retail_amount')::numeric)
                           filter (where payload->>'doc_type_name' = 'Возврат'), 0)::float rets
                from raw_wb_report
               where (payload->>'rr_dt')::date >= %s
               group by 1, 2""", (since,))


def _ozon(since):
    return db.query("""select account, to_char(make_date(year, month, 1), 'YYYY-MM') ym,
                  coalesce(sum((row->'delivery_commission'->>'amount')::numeric), 0)::float sales,
                  coalesce(sum((row->'return_commission'->>'amount')::numeric), 0)::float rets
                from raw_ozon_realization,
                     lateral jsonb_array_elements(payload->'rows') row
               where make_date(year, month, 1) >= %s
               group by 1, 2""", (since,))


def _yandex(since):
    return db.query("""select account, ym,
                  coalesce(sum(amount) filter (where category = 'revenue'), 0)::float sales,
                  coalesce(-sum(amount) filter (where category = 'returns'), 0)::float rets
                from raw_yandex_closure
               where ym >= to_char(%s::date, 'YYYY-MM')
               group by 1, 2""", (since,))


def _order_counts(since):
    """Delivered/paid sales, not placed orders. WB identifies bought item via srid."""
    rows = []
    for platform, sql in (
        ("wb", """select account,to_char((payload->>'rr_dt')::date,'YYYY-MM') ym,
            count(distinct nullif(payload->>'srid','')) orders,
            count(*) filter (where nullif(payload->>'srid','') is null) missing
            from raw_wb_report where (payload->>'rr_dt')::date >= %s
            and payload->>'supplier_oper_name'='Продажа'
            and payload->>'doc_type_name'='Продажа'
            and (payload->>'retail_amount')::numeric > 0 group by 1,2"""),
        ("ozon", """select account,to_char((payload->>'operation_date')::date,'YYYY-MM') ym,
            count(distinct nullif(split_part(payload->'posting'->>'posting_number','-',1)
              ||'-'||split_part(payload->'posting'->>'posting_number','-',2),'')) orders,
            count(*) filter (where nullif(payload->'posting'->>'posting_number','') is null) missing
            from raw_ozon_transaction where (payload->>'operation_date')::date >= %s
            and (payload->>'accruals_for_sale')::numeric > 0 group by 1,2"""),
        ("yandex", """select account,ym,count(distinct nullif(order_id::text,'')) orders,
            count(*) filter (where nullif(order_id::text,'') is null) missing
            from raw_yandex_closure where ym >= to_char(%s::date,'YYYY-MM')
            and category='revenue' and amount>0 group by 1,2"""),
    ):
        for r in db.query(sql, (since,)):
            rows.append({**r, "platform": platform})
    return {(r["ym"], r["platform"], ACC_ORG.get(r["account"])):
            None if r["missing"] else r["orders"] for r in rows}


@router.get("/turnover")
def page():
    return FileResponse(STATIC / "turnover.html", headers={"Cache-Control": "no-cache"})


@router.get("/turnover/royalty")
def royalty_page():
    # Dedicated address opens the royalty panel even before JavaScript runs.
    html = (STATIC / "turnover.html").read_text(encoding="utf-8")
    html = html.replace('<title>Обороты</title>', '<title>Обороты по торговым маркам</title>')
    html = html.replace('<h1>🧮 Обороты</h1>', '<h1>Обороты по торговым маркам</h1>')
    html = html.replace('id="premium" role="tabpanel"', 'id="premium" hidden role="tabpanel"')
    html = html.replace('id="premium-note"', 'id="premium-note" hidden')
    html = html.replace('aria-labelledby="royalty-tab" hidden', 'aria-labelledby="royalty-tab"')
    html = html.replace('<a href="/turnover" class="cur">', '<a href="/turnover">')
    html = html.replace('<a href="/turnover/royalty">', '<a href="/turnover/royalty" class="cur">')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


_TURNOVER_CACHE = {}
_TURNOVER_CACHE_LOCK = Lock()


@router.get("/api/turnover")
def turnover(months: int = 6):
    """Calculate once per UTC calendar day and period, on first request.

    No marketplace API calls; lock prevents duplicate concurrent database scans.
    Restart clears this process-local cache.
    """
    months = max(1, min(months, 24))
    today = dt.datetime.now(dt.timezone.utc).date()
    with _TURNOVER_CACHE_LOCK:
        cached = _TURNOVER_CACHE.get(months)
        if cached and cached[0] == today:
            return cached[1]
        result = _calculate_turnover(months)
        result["calculated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        result["refresh_policy"] = "daily"
        _TURNOVER_CACHE[months] = (today, result)
        return result


def _calculate_turnover(months: int = 6):
    """Деньги покупателя по месяцам: (площадка × фирма) и итоги. Возвраты — отдельной строкой."""
    today = dt.date.today()
    month_index = today.year * 12 + today.month - months
    first = dt.date(month_index // 12, month_index % 12 + 1, 1)
    cells = defaultdict(lambda: {"sales": 0.0, "rets": 0.0})
    for platform, rows in (("wb", _wb(first)), ("ozon", _ozon(first)), ("yandex", _yandex(first))):
        for r in rows:
            org = ACC_ORG.get(r["account"])
            if not org:
                continue
            c = cells[(r["ym"], platform, org)]
            c["sales"] += r["sales"]
            c["rets"] += r["rets"]

    counts = _order_counts(first)
    yms = sorted({k[0] for k in cells})
    out = []
    for ym in yms:
        row = {"ym": ym, "orgs": {}, "total": {"sales": 0.0, "rets": 0.0, "net": 0.0}}
        for org in ORGS:
            o = {"platforms": {}, "sales": 0.0, "rets": 0.0, "net": 0.0}
            for p in PLATFORMS:
                c = cells.get((ym, p, org))
                if not c:
                    continue
                net = round(c["sales"] - c["rets"], 2)
                o["platforms"][p] = {"sales": round(c["sales"], 2),
                                     "rets": round(c["rets"], 2), "net": net,
                                     "orders": counts.get((ym,p,org)),
                                     "avg_check": round(c["sales"] / counts[(ym,p,org)],2)
                                     if counts.get((ym,p,org)) else None}
                o["sales"] += c["sales"]
                o["rets"] += c["rets"]
                o["net"] += net
            if o["platforms"]:
                for k in ("sales", "rets", "net"):
                    o[k] = round(o[k], 2)
                    row["total"][k] = round(row["total"][k] + o[k], 2)
                row["orgs"][org] = o
        out.append(row)

    # чего ещё нет: отчёт о реализации Ozon выходит 8–10 числа, ВБ отдаёт финотчёт понедельно
    have_oz = {k[0] for k in cells if k[1] == "ozon"}
    pending = [ym for ym in yms if ym not in have_oz]
    wb_last = db.query("""select max((payload->>'rr_dt')::date) d from raw_wb_report""")[0]["d"]
    return {"months": out, "orgs": ORGS, "platforms": PLATFORMS,
            "ozon_pending": pending,
            "wb_last_day": wb_last.isoformat() if wb_last else None}


# Royalty scope approved 05.10.2026: all WB/Market, selected trademark on Ozon.
_ROYALTY_START = dt.date(2026, 7, 1)
_ROYALTY_BRANDS = {"oz_acc1": "Цифровой квадрат", "oz_acc2": "Dsquare"}
_ROYALTY_RATES_DIGITAL = [
    {"range": "до 5 000 000 рублей", "percent": "2,25"},
    {"range": "от 5 000 000 до 10 000 000 рублей", "percent": "2,15"},
    {"range": "от 10 000 000 до 20 000 000 рублей", "percent": "2,0"},
    {"range": "от 20 000 000 до 30 000 000 рублей", "percent": "1,75"},
    {"range": "свыше 30 000 000 рублей", "percent": "1,5"},
]
_ROYALTY_CACHE = {}
_ROYALTY_LOCK = Lock()
_ROYALTY_CLOSED_DIR = BASE_DIR / 'reports/data/royalty_turnover/closed'


def _royalty_platforms(inn):
    # Дисквер не продаёт на Маркете; отсутствие кабинета не является потерей данных.
    return ('wb', 'ozon') if inn == '7811803918' else tuple(PLATFORMS)


def _closed_royalty_months():
    import json
    result = {}
    for path in _ROYALTY_CLOSED_DIR.glob('????-??.json'):
        value = json.loads(path.read_text())
        month = value['month']
        if value.get('schema_version') != 1 or month['ym'] != path.stem or month.get('status') != 'closed':
            raise ValueError('Invalid closed royalty snapshot: ' + path.name)
        result[month['ym']] = month
    return result


def _save_closed_royalty(month):
    import fcntl
    import json
    import os
    import tempfile
    _ROYALTY_CLOSED_DIR.mkdir(parents=True, exist_ok=True)
    path = _ROYALTY_CLOSED_DIR / (month['ym'] + '.json')
    with (_ROYALTY_CLOSED_DIR / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            # A closed month is immutable, including across concurrent web workers.
            return json.loads(path.read_text())['month']
        fd, name = tempfile.mkstemp(dir=_ROYALTY_CLOSED_DIR, prefix='.snapshot-')
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump({'schema_version': 1, 'month': month}, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
            directory = os.open(_ROYALTY_CLOSED_DIR, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    return month


def _royalty_totals(months):
    from decimal import Decimal
    totals = {}
    for inn in ORGS:
        calculated = [m for m in months if m['orgs'][inn]['royalty']['amount'] is not None]
        total = float(sum((Decimal(str(m['orgs'][inn]['royalty']['amount']))
                          for m in calculated), Decimal(0))) if calculated else None
        totals[inn] = {'amount': total, 'months': [m['ym'] for m in calculated],
                       'preliminary': any(m['orgs'][inn]['royalty']['preliminary'] for m in calculated)}
    return totals


def _apply_royalty_closures(result, today, closed):
    current = today.strftime('%Y-%m')
    for index, month in enumerate(result['months']):
        ym = month['ym']
        if ym < current and ym in closed:
            result['months'][index] = closed[ym]
            continue
        month['status'] = 'calculated' if ym == current else 'awaiting_close'
        month_end = (dt.date.fromisoformat(ym + '-01').replace(day=28)
                     + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
        ready = all(all(org['platforms'][p] is not None for p in _royalty_platforms(inn))
                    and org.get('wb_last_day') is not None and org['wb_last_day'] >= month_end.isoformat()
                    for inn, org in month['orgs'].items())
        if ym < current and ready:
            month['status'] = 'closed'
            month['closed_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        for org in month['orgs'].values():
            royalty = org['royalty']
            royalty['source_warnings'] = list(royalty.get('reasons', []))
            royalty['preliminary'] = month['status'] != 'closed'
            if month['status'] == 'closed':
                royalty['reasons'] = []
            elif ym == current:
                royalty['reasons'].append('Текущий месяц: расчётные данные')
        if month['status'] == 'closed':
            result['months'][index] = _save_closed_royalty(month)
    result['royalty_totals'] = _royalty_totals(result['months'])
    result['digital_royalty_total'] = result['royalty_totals']['7807355364']
    result['diskver_royalty_total'] = result['royalty_totals']['7811803918']
    return result


def _royalty_ozon(since):
    # One classification per offer prevents multiplying realization rows on joins.
    return db.query("""with brands as (
        select account, offer_id,
          array_agg(distinct v->>'value') filter (
            where nullif(trim(v->>'value'), '') is not null) labels
        from raw_ozon_attributes
        cross join lateral jsonb_array_elements(payload->'attributes') a
        cross join lateral jsonb_array_elements(a->'values') v
        where a->>'id' = '85' group by account, offer_id
      ), classified as (
        select r.account, to_char(make_date(year, month, 1), 'YYYY-MM') ym,
          case when cardinality(b.labels) = 1 then b.labels[1] else null end brand,
          coalesce((item->'delivery_commission'->>'amount')::numeric, 0) sales,
          coalesce((item->'return_commission'->>'amount')::numeric, 0) rets
        from raw_ozon_realization r
        cross join lateral jsonb_array_elements(r.payload->'rows') item
        left join brands b on b.account = r.account
          and b.offer_id = item->'item'->>'offer_id'
        where make_date(year, month, 1) >= %s
      ) select account, ym, brand, sum(sales) sales, sum(rets) rets,
          count(*) row_count from classified group by account, ym, brand""", (since,))


def _royalty_months(today):
    cursor = _ROYALTY_START
    while cursor <= today.replace(day=1):
        yield cursor.strftime("%Y-%m")
        cursor = (cursor.replace(day=28) + dt.timedelta(days=4)).replace(day=1)



def _digital_royalty(base):
    """One rate applies to the whole month's reported gross trademark sales."""
    from decimal import Decimal, ROUND_HALF_UP
    if base is None:
        return {"rate_percent": None, "amount": None}
    value = Decimal(str(base))
    if value < 0:
        raise ValueError("Royalty gross sales cannot be negative")
    if value < Decimal("5000000"):
        rate = Decimal("2.25")
    elif value < Decimal("10000000"):
        rate = Decimal("2.15")
    elif value < Decimal("20000000"):
        rate = Decimal("2.0")
    elif value <= Decimal("30000000"):
        rate = Decimal("1.75")
    else:
        rate = Decimal("1.5")
    amount = (value * rate / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {"rate_percent": float(rate), "amount": float(amount)}


def _assemble_royalty(today, source_rows, coverage):
    from decimal import Decimal

    def cell(sales=0, rets=0):
        sales, rets = Decimal(str(sales)), Decimal(str(rets))
        return {"base": float(round(sales, 2)), "sales": float(round(sales, 2)), "rets": float(round(rets, 2)),
                "net": float(round(sales - rets, 2))}

    cells, excluded, unknown = {}, {}, {}
    for platform, rows in source_rows:
        for r in rows:
            org = ACC_ORG.get(r["account"])
            if org not in ORGS:
                continue
            key = (r["ym"], platform, org)
            if platform == "ozon":
                cells.setdefault(key, cell())
                brand = r["brand"]
                if brand is None:
                    unknown[key] = cell(r["sales"], r["rets"])
                    unknown[key]["rows"] = r["row_count"]
                    continue
                if brand != _ROYALTY_BRANDS.get(r["account"]):
                    bucket = excluded.setdefault(key, {"sales": Decimal(0), "rets": Decimal(0)})
                    for field in ("sales", "rets"):
                        bucket[field] += Decimal(str(r[field]))
                    continue
            cells[key] = cell(r["sales"], r["rets"])

    months = []
    for ym in _royalty_months(today):
        orgs = {}
        for org in ORGS:
            platforms = {p: cells.get((ym, p, org)) for p in PLATFORMS}
            unknown_cell = unknown.get((ym, "ozon", org), cell())
            excluded_cell = excluded.get((ym, "ozon", org), {})
            present = [c for c in platforms.values() if c is not None]
            totals = {field: float(round(sum(Decimal(str(c[field])) for c in present), 2)) if present else None
                      for field in ("sales", "rets", "net")}
            orgs[org] = {"platforms": platforms, "base": totals["sales"], **totals,
                         "wb_last_day": next((c["last_day"] for c in coverage
                            if ACC_ORG.get(c["account"]) == org and c["ym"] == ym), None),
                         "ozon_unclassified": unknown_cell,
                         "ozon_excluded": cell(excluded_cell.get("sales", 0), excluded_cell.get("rets", 0))}
        month_end = (dt.date.fromisoformat(ym + "-01").replace(day=28)
                     + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
        for inn, org in orgs.items():
            royalty = _digital_royalty(org["base"])
            reasons = []
            if any(org["platforms"][p] is None for p in _royalty_platforms(inn)):
                reasons.append("Нет данных одной или нескольких площадок")
            if org["wb_last_day"] is None or org["wb_last_day"] < month_end.isoformat():
                reasons.append("Данные ВБ не доходят до конца месяца")
            if org["ozon_unclassified"].get("rows", 0):
                reasons.append("Есть строки Ozon без подтверждённого бренда")
            royalty.update(preliminary=bool(reasons), reasons=reasons)
            org["royalty"] = royalty
        months.append({"ym": ym, "orgs": orgs})
    totals = _royalty_totals(months)
    return {"months": months, "orgs": ORGS, "platforms": PLATFORMS,
            "trademarks": {"7807355364": "Цифровой квадрат", "7811803918": "Dsquare"},
            "since": _ROYALTY_START.isoformat(), "coverage": coverage,
            "basis": "sales_before_returns",
            "royalty_rates": {inn: _ROYALTY_RATES_DIGITAL for inn in ORGS},
            "royalty_period": "month", "royalty_method": "single_rate_on_whole_base",
            "royalty_totals": totals,
            "digital_royalty_total": totals["7807355364"],
            "diskver_royalty_total": totals["7811803918"],
            "refresh_policy": "daily"}


@router.get("/api/turnover/royalty")
def royalty_turnover():
    from zoneinfo import ZoneInfo
    today = dt.datetime.now(ZoneInfo('Europe/Moscow')).date()
    with _ROYALTY_LOCK:
        if _ROYALTY_CACHE.get("day") == today:
            return _ROYALTY_CACHE["result"]
        closed = _closed_royalty_months()
        # Historical source tables are no longer queried for frozen months.
        since = min(dt.date.fromisoformat(ym + '-01') for ym in _royalty_months(today)
                    if ym not in closed or ym == today.strftime('%Y-%m'))
        coverage = db.query("""select account,
          to_char((payload->>'rr_dt')::date, 'YYYY-MM') ym,
          max((payload->>'rr_dt')::date)::text last_day
          from raw_wb_report where (payload->>'rr_dt')::date >= %s
          group by account, ym""", (since,))
        result = _assemble_royalty(today, [
            ("wb", _wb(since)), ("ozon", _royalty_ozon(since)),
            ("yandex", _yandex(since))], coverage)
        result = _apply_royalty_closures(result, today, closed)
        result["calculated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        _ROYALTY_CACHE.update(day=today, result=result)
        return result
