# поток: cf
"""cf/build.py — пересборка витрины cf_line (понедельный кэшфлоу, факт + план).

Факт — bank_txn по operation_date (все банки фирмы). Переводы между своими счетами
исключаются: внутри фирмы они гасят друг друга. Расходы берут статью из bank_txn_opex
(помесячное размазывание opex_fact_* сюда НЕ идёт — нужна дата денег), поступления —
cf_income_rule по ИНН/назначению.

План (с сегодняшнего дня):
  поставщики  — po_payment_status (pending/queued/waiting_receipt) на ~2 недели, дальше неделя
                добирается до среднего недельного факта оплат поставщикам за 8 недель;
  аренда      — rent_plan (последний рабочий день месяца);
  повторяющиеся — cf_plan_rule (зарплата, НДФЛ, налоги, программист, упаковка...);
  площадки    — cf.mp_forecast (выплаты WB / Ozon / Яндекс по графикам площадок).

Запуск:  ./venv/bin/python -m cf.build [--snapshot]
"""
import argparse
import datetime as dt
import pathlib
import re
import sys
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import db  # noqa: E402
from invoice_bot import workcal  # noqa: E402

ORGS = {"7807355364": "Цифровой Квадрат", "7811803918": "Дисквэр"}
FACT_WEEKS = 8
PLAN_WEEKS = 8
SUPPLIER_RUNRATE_WEEKS = 8

# статья opex_category → строка кэшфлоу
OPEX_TO_CF = {
    "ФОТ": "salary", "Самозанятые": "salary", "Налоги ФОТ": "ndfl", "Налоги и взносы": "taxes",
    "Аренда": "rent", "Программист": "programmer", "Упаковка": "packaging",
    "Реклама и продвижение": "ads", "Доставка": "delivery",
    "Закупка товара у поставщиков": "suppliers", "Перевод между своими счетами": "transfer",
}
TRANSFER = "transfer"


def monday(d):
    return d - dt.timedelta(days=d.weekday())


def prev_working(d):
    """Платёж, выпавший на выходной, уходит на ближайший рабочий день назад."""
    for _ in range(10):
        if workcal.is_working(d):
            return d
        d -= dt.timedelta(days=1)
    return d


def tax_category(purpose):
    """Налоги и взносы → НДС / УСН / прочие налоги по назначению платежа."""
    p = (purpose or "").lower()
    if "усн" in p or "упрощ" in p:
        return "usn"
    p = re.sub(r"без\s+ндс|ндс\s+не\s+облага\w*", "", p)
    if "ндс" in p:
        return "vat"
    return "taxes"   # «пополнение ЕНС» без расшифровки


# ── факт ─────────────────────────────────────────────────────────────────────
def income_rules():
    rules = db.query("select * from cf_income_rule order by prio, id")
    for r in rules:
        r["_re"] = re.compile(r["purpose_re"], re.I) if r["purpose_re"] else None
    return rules


def classify_income(t, rules):
    for r in rules:
        if r["cp_inn"] and r["cp_inn"] != (t["cp_inn"] or ""):
            continue
        if r["_re"] and not r["_re"].search(t["purpose"] or ""):
            continue
        if not r["cp_inn"] and not r["_re"]:
            continue
        return r["category"]
    return "other_in"


def fact_lines(date_from, date_to):
    rows = db.query("""
        select t.id, t.org_inn, t.bank, t.direction, t.amount, t.operation_date::date as day,
               t.purpose, t.cp_name, t.cp_inn, c.name as opex_cat
        from bank_txn t
        left join bank_txn_opex o on o.txn_id = t.id
        left join opex_category c on c.id = o.category_id
        where t.operation_date::date between %s and %s and t.org_inn = any(%s)
    """, (date_from, date_to, list(ORGS)))
    rules = income_rules()
    own_inns = set(ORGS)
    out = []
    for t in rows:
        if t["direction"] == "CREDIT":
            cat = classify_income(t, rules)
            if cat == "other_in" and (t["cp_inn"] or "") == t["org_inn"]:
                cat = TRANSFER
            sign = 1
        else:
            cat = OPEX_TO_CF.get(t["opex_cat"] or "", "other_out")
            if cat == "taxes":
                cat = tax_category(t["purpose"])
            if cat == "other_out" and (t["cp_inn"] or "") == t["org_inn"]:
                cat = TRANSFER
            sign = -1
        if cat == TRANSFER:
            continue
        # перевод в другую СВОЮ фирму — реальный отток/приток для каждой, помечаем
        note = "между фирмами" if (t["cp_inn"] or "") in own_inns - {t["org_inn"]} else None
        out.append(dict(org_inn=t["org_inn"], day=t["day"], kind="fact", category=cat,
                        amount=sign * float(t["amount"]), source="bank:" + t["bank"],
                        ref=str(t["id"]), note=note or (t["cp_name"] or "")[:120],
                        estimate=False))
    return out


# ── план ─────────────────────────────────────────────────────────────────────
def supplier_plan(today, horizon_end, fact):
    out = []
    rows = db.query("""
        select org_inn, po_id, inn, due_date, amount, status from po_payment_status
        where status in ('pending','queued','waiting_receipt') and org_inn = any(%s)
    """, (list(ORGS),))
    names = {r["inn"]: r["name"] for r in db.query("select inn, name from supplier_payment_terms")}
    known = defaultdict(float)
    for r in rows:
        day = max(r["due_date"], today)   # просроченные — в ближайшие дни
        if day > horizon_end:
            continue
        out.append(dict(org_inn=r["org_inn"], day=day, kind="plan", category="suppliers",
                        amount=-float(r["amount"]), source="po_payment", ref=str(r["po_id"]),
                        note=f"{names.get(r['inn'], r['inn'])} · {r['status']}", estimate=False))
        known[(r["org_inn"], monday(day))] += float(r["amount"])
    # среднее недельное по факту оплат поставщикам
    start = monday(today) - dt.timedelta(weeks=SUPPLIER_RUNRATE_WEEKS)
    for org in ORGS:
        paid = -sum(f["amount"] for f in fact if f["org_inn"] == org and f["category"] == "suppliers"
                    and start <= f["day"] < monday(today))
        weekly = paid / SUPPLIER_RUNRATE_WEEKS
        w = monday(today)
        while w <= horizon_end:
            gap = weekly - known[(org, w)]
            if w > monday(today) and gap > 0:   # текущая неделя — только документы
                out.append(dict(org_inn=org, day=w + dt.timedelta(days=2), kind="plan",
                                category="suppliers", amount=-round(gap, 2), source="runrate",
                                ref=None, note=f"до среднего {weekly:,.0f} ₽/нед".replace(",", " "),
                                estimate=True))
            w += dt.timedelta(weeks=1)
    return out


def last_working_of_month(y, m):
    nxt = dt.date(y + (m == 12), m % 12 + 1, 1)
    return prev_working(nxt - dt.timedelta(days=1))


def month_iter(today, horizon_end):
    y, m = today.year, today.month
    while dt.date(y, m, 1) <= horizon_end:
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def rent_plan(today, horizon_end):
    out = []
    for r in db.query("select * from rent_plan where active and org_inn = any(%s)", (list(ORGS),)):
        for y, m in month_iter(today, horizon_end):
            day = last_working_of_month(y, m)
            if today <= day <= horizon_end:
                out.append(dict(org_inn=r["org_inn"], day=day, kind="plan", category="rent",
                                amount=-float(r["amount"]), source="rent_plan", ref=str(r["id"]),
                                note=r["note"], estimate=False))
    return out


def rule_plan(today, horizon_end):
    out = []
    for r in db.query("select * from cf_plan_rule where active and org_inn = any(%s)", (list(ORGS),)):
        for y, m in month_iter(today, horizon_end):
            if r["schedule"] == "quarterly" and m not in (r["months"] or []):
                continue
            for dd in r["days"]:
                last = (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)).day
                day = prev_working(dt.date(y, m, min(dd, last)))
                if today <= day <= horizon_end:
                    out.append(dict(org_inn=r["org_inn"], day=day, kind="plan",
                                    category=r["category"], amount=-float(r["amount"]),
                                    source="plan_rule", ref=str(r["id"]), note=r["name"],
                                    estimate=r["source"] == "auto"))
    return out


def build(snapshot=None, today=None):
    """snapshot=None — замораживаем план по понедельникам (день недели решает сам)."""
    from cf import mp_forecast
    today = today or dt.date.today()
    if snapshot is None:
        snapshot = today.weekday() == 0
    fact_from = monday(today) - dt.timedelta(weeks=max(FACT_WEEKS, SUPPLIER_RUNRATE_WEEKS))
    horizon_end = monday(today) + dt.timedelta(weeks=PLAN_WEEKS, days=6)
    fact = fact_lines(fact_from, today)
    plan = (supplier_plan(today, horizon_end, fact) + rent_plan(today, horizon_end)
            + rule_plan(today, horizon_end) + mp_forecast.forecast(today, horizon_end))
    lines = fact + plan
    for ln in lines:
        ln["week_start"] = monday(ln["day"])
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute("delete from cf_line")
        cols = ["org_inn", "week_start", "day", "kind", "category", "amount", "source", "ref",
                "note", "estimate"]
        from psycopg2.extras import execute_values
        execute_values(cur, f"insert into cf_line ({','.join(cols)}) values %s",
                       [tuple(ln[c] for c in cols) for ln in lines])
        if snapshot:
            cur.execute("""
                insert into cf_plan_snapshot (snap_date, org_inn, week_start, category, amount)
                select %s, org_inn, week_start, category, sum(amount) from cf_line
                where kind = 'plan' group by 2, 3, 4
                on conflict do nothing""", (today,))
    by = defaultdict(float)
    for ln in lines:
        by[(ORGS[ln["org_inn"]], ln["kind"])] += ln["amount"]
    print(f"[ok] cf_line: {len(lines)} строк (факт {len(fact)}, план {len(plan)})")
    for (org, kind), s in sorted(by.items()):
        print(f"     {org:18} {kind:5} сальдо {s:>14,.0f} ₽".replace(",", " "))
    return len(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", action="store_true", help="заморозить план (по понедельникам)")
    a = ap.parse_args()
    build(snapshot=True if a.snapshot else None)
