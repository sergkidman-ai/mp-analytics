# поток: cf
"""cf/plan_seed.py — автоправила cf_plan_rule из факта банка (source='auto').

Регулярные расходы: платежи за 3 последних полных месяца делим на две половины месяца
(1–15 и 16–31). Каждая половина — отдельный платёж: сумма = среднее в месяц, день = медиана
по сумме. Так ложатся «два раза в месяц» у зарплаты и НДФЛ и разовые крупные у программиста.
Налоги (УСН «доходы минус расходы» + НДС 5 %, решение Сергея 22.09):
  НДС  — ежемесячно 28-го, треть квартального; база — последний регулярный платёж ЕНП,
         на новый квартал та же сумма (оценка, правится вручную);
  УСН  — аванс 28-го апреля/июля/октября; оценка = половина платежа за полугодие.
Ручные правила (source='manual') не трогаем: по такой (фирма, статья) авто не создаём.

Запуск:  ./venv/bin/python -m cf.plan_seed
"""
import datetime as dt
import pathlib
import sys
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import db  # noqa: E402

ORGS = ("7807355364", "7811803918")
# статьи opex → (категория кэшфлоу, имя строки)
REGULAR = {
    "ФОТ": ("salary", "Зарплата"), "Самозанятые": ("selfemp", "Самозанятые"),
    "Налоги ФОТ": ("ndfl", "НДФЛ и взносы"),
    "Программист": ("programmer", "Программист"),
    "Упаковка": ("packaging", "Упаковка"),
    "Бухгалтерия": ("other_out", "Бухгалтерия"),
}
MIN_SHARE = 0.05
# переход на НДС 5 % без истории платежей: первый квартал считаем по обороту (Сергей 22.09:
# «оборот за квартал × 5 % / 3 платежа»). Дисквэр на НДС с 01.08.2026, платит с октября.
VAT_START = {"7811803918": dt.date(2026, 8, 1)}
VAT_RATE = 0.05


def months_back(today, n=3):
    first = today.replace(day=1)
    end = first - dt.timedelta(days=1)
    start = first
    for _ in range(n):
        start = (start - dt.timedelta(days=1)).replace(day=1)
    return start, end


def weighted_median_day(pays):
    pays = sorted(pays)
    half, acc = sum(a for _, a in pays) / 2, 0
    for d, a in pays:
        acc += a
        if acc >= half:
            return d
    return pays[-1][0]


def regular_rules(today):
    start, end = months_back(today)
    rows = db.query("""
        select t.org_inn, c.name as cat, extract(day from t.operation_date)::int as dom, t.amount
        from bank_txn t join bank_txn_opex o on o.txn_id = t.id
        join opex_category c on c.id = o.category_id
        where t.direction='DEBIT' and t.operation_date::date between %s and %s
          and t.org_inn = any(%s) and c.name = any(%s)""",
                    (start, end, list(ORGS), list(REGULAR)))
    buckets = defaultdict(list)
    for r in rows:
        cat, name = REGULAR[r["cat"]]
        buckets[(r["org_inn"], cat, name, 1 if r["dom"] <= 15 else 2)].append(
            (r["dom"], float(r["amount"])))
    totals = defaultdict(float)
    for (org, cat, name, _), pays in buckets.items():
        totals[(org, cat, name)] += sum(a for _, a in pays)
    out = []
    for (org, cat, name), total in totals.items():
        if cat == "salary":   # правило Сергея: 50/50 на 1 и 15 число, сумма — среднее по факту
            out.append(dict(org_inn=org, category=cat, name=name, amount=round(total / 3 / 2, 2),
                            schedule="monthly", days=[1, 15], months=None,
                            note=f"среднее ФОТ {start:%m}–{end:%m.%Y} пополам"))
    for (org, cat, name, half), pays in buckets.items():
        if cat == "salary":
            continue
        s = sum(a for _, a in pays)
        if s < MIN_SHARE * totals[(org, cat, name)]:
            continue
        out.append(dict(org_inn=org, category=cat, name=name, amount=round(s / 3, 2),
                        schedule="monthly", days=[weighted_median_day(pays)], months=None,
                        note=f"среднее {start:%m}–{end:%m.%Y}, {'1-я' if half == 1 else '2-я'} половина"))
    return out


def vat_from_turnover(org, today):
    """НДС прошедшего квартала = оборот (по «Отчётам МП», наша цена) с даты перехода × 5 %,
    тремя равными платежами 28-го в месяцах следующего квартала. Незакрытый месяц — не меньше
    предыдущего (данные площадок отстают на дни-неделю)."""
    from reports import mp_numbers
    q_start = dt.date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
    start = max(VAT_START[org], q_start)
    months, d = [], start.replace(day=1)
    while d <= today:
        months.append(d)
        d = (d + dt.timedelta(days=32)).replace(day=1)
    turn, parts, prev = 0.0, [], 0.0
    for m in months:
        b = mp_numbers.business(m.strftime("%Y-%m"), org) or {}
        v = float((b.get("total") or {}).get("oborot") or 0)
        if m.month == today.month and m.year == today.year:
            v = max(v, prev)
        turn += v
        parts.append(f"{m:%m}: {v / 1e6:.2f} млн")
        prev = v
    if turn <= 0:
        return []
    q_next = [(q_start.month + 2 + i) % 12 + 1 for i in range(3)]
    return [dict(org_inn=org, category="vat", name="НДС (треть квартала)",
                 amount=round(turn * VAT_RATE / 3, 2), schedule="quarterly", days=[28],
                 months=q_next,
                 note=f"оценка: оборот {turn / 1e6:.2f} млн ({', '.join(parts)}) × 5 % / 3")]


def tax_rules(today):
    out = []
    taxes = db.query("""
        select t.org_inn, t.operation_date::date d, t.amount, t.purpose
        from bank_txn t join bank_txn_opex o on o.txn_id = t.id
        join opex_category c on c.id = o.category_id
        where c.name='Налоги и взносы' and t.operation_date >= %s and t.org_inn = any(%s)
        order by d""", (today - dt.timedelta(days=200), list(ORGS)))
    q_start = dt.date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
    for org in ORGS:
        mine = [t for t in taxes if t["org_inn"] == org]
        # НДС: регулярные «пополнения ЕНС» одинаковой суммы раз в месяц
        enp = [t for t in mine if "единого налогового счета" in (t["purpose"] or "").lower()]
        amounts = defaultdict(int)
        for t in enp:
            amounts[round(float(t["amount"]), 2)] += 1
        monthly = [a for a, n in amounts.items() if n >= 2]
        if monthly:
            last = max((t for t in enp if round(float(t["amount"]), 2) in monthly),
                       key=lambda t: t["d"])
            base = float(last["amount"])
            nxt = [m for m in range(1, 13) if m not in range(q_start.month, q_start.month + 3)]
            out.append(dict(org_inn=org, category="vat", name="НДС (треть квартала)",
                            amount=round(base, 2), schedule="quarterly", days=[28],
                            months=list(range(q_start.month, q_start.month + 3)),
                            note=f"как в текущем квартале ({base:,.0f} ₽)".replace(",", " ")))
            out.append(dict(org_inn=org, category="vat", name="НДС (треть квартала)",
                            amount=round(base, 2), schedule="quarterly", days=[28],
                            months=nxt,
                            note="оценка = текущий квартал, уточнить по декларации"))
        elif org in VAT_START:
            out += vat_from_turnover(org, today)
        # УСН: аванс за квартал ≈ половина последнего платежа «за полугодие»/«УСН»
        usn = [t for t in mine if "усн" in (t["purpose"] or "").lower()]
        big_enp = [t for t in enp if round(float(t["amount"]), 2) not in monthly]
        src = (usn or big_enp)
        if src:
            last = src[-1]
            p = (last["purpose"] or "").lower()
            amt = float(last["amount"]) / (2 if ("полугод" in p or not usn) else 1)
            out.append(dict(org_inn=org, category="usn", name="УСН (аванс за квартал)",
                            amount=round(amt, 2), schedule="quarterly", days=[28],
                            months=[4, 7, 10],   # годовой по УСН — 28 марта, в горизонт не попадает
                            note=f"оценка по платежу {last['d']:%d.%m} "
                                 f"({float(last['amount']):,.0f} ₽)".replace(",", " ")))
    return out


def seed(today=None):
    today = today or dt.date.today()
    rules = regular_rules(today) + tax_rules(today)
    manual = {(r["org_inn"], r["category"]) for r in
              db.query("select org_inn, category from cf_plan_rule where source='manual'")}
    rules = [r for r in rules if (r["org_inn"], r["category"]) not in manual]
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute("delete from cf_plan_rule where source='auto'")
        for r in rules:
            cur.execute("""insert into cf_plan_rule
                (org_inn, category, name, amount, schedule, days, months, source, note)
                values (%(org_inn)s, %(category)s, %(name)s, %(amount)s, %(schedule)s,
                        %(days)s, %(months)s, 'auto', %(note)s)""", r)
    print(f"[ok] cf_plan_rule: авто {len(rules)} правил (ручных статей: {len(manual)})")
    return rules


if __name__ == "__main__":
    for r in seed():
        print(f"  {r['org_inn'][-4:]} {r['name']:24} {r['amount']:>12,.0f} дни {r['days']} "
              f"{r['months'] or ''} · {r['note']}".replace(",", " "))
