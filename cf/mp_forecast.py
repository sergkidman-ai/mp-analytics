# поток: cf
"""cf/mp_forecast.py — плановые выплаты площадок по их графикам (разбор 22.09.2026,
docs/cf_recon.md).

WB     — выплата за неделю W (по date_from отчёта) приходит в ПОНЕДЕЛЬНИК W+5,
         сумма = «Итого к перечислению» × 0.999 (формула reports/wb_mp_report.py).
         Лаг гуляет 24–38 дней, поэтому недели, чей срок уже прошёл, сверяем с банком
         по сумме: не нашли платёж — неделя «просрочена» и встаёт в ближайшие дни.
Ozon   — выплата в СРЕДУ W+2 = Σ транзакций недели W (сходится до рубля).
Яндекс — только ЦК; еженедельно с отсрочкой 4 недели: база месяца (выручка − возвраты −
         услуги) / дни месяца × 7, в среду. Точность только помесячная — всё оценка.
Недели без данных (отчёт ещё не пришёл, неделя не кончилась) — среднее 4 полных недель.
"""
import datetime as dt
import pathlib
import sys
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import db  # noqa: E402

ACC_ORG = {"wb_acc1": "7807355364", "wb_acc2": "7811803918",
           "oz_acc1": "7807355364", "oz_acc2": "7811803918", "ya_acc1": "7807355364"}
WB_INN, OZON_INN, YA_INN = "9714053621", "7704217370", "9704254424"
WB_RATIO = 0.999
AVG_WEEKS = 4
OVERDUE_LOOKBACK = dt.timedelta(weeks=3)


def monday(d):
    return d - dt.timedelta(days=d.weekday())


def _line(org, day, cat, amount, ref, note, estimate):
    return dict(org_inn=org, day=day, kind="plan", category=cat, amount=round(amount, 2),
                source="mp_forecast", ref=ref, note=note, estimate=estimate)


def _fmt(d):
    return d.strftime("%d.%m")


def wb_weeks():
    rows = db.query("""
        select account, date_trunc('week', (payload->>'date_from')::date)::date as w,
          sum(case when payload->>'supplier_oper_name'='Возврат'
                   then -coalesce((payload->>'ppvz_for_pay')::numeric,0)
                   else coalesce((payload->>'ppvz_for_pay')::numeric,0) end
              - coalesce((payload->>'delivery_rub')::numeric,0)
              - coalesce((payload->>'storage_fee')::numeric,0)
              - coalesce((payload->>'acceptance')::numeric,0)
              - coalesce((payload->>'cashback_amount')::numeric,0)
              - coalesce((payload->>'penalty')::numeric,0)
              - coalesce((payload->>'deduction')::numeric,0)) as itog
        from raw_wb_report
        where (payload->>'date_from')::date >= current_date - 120
        group by 1, 2""")
    out = defaultdict(dict)
    for r in rows:
        out[r["account"]][r["w"]] = float(r["itog"])
    return out


def bank_credits(org, cp_inns, since):
    return [dict(r, amount=float(r["amount"])) for r in db.query("""
        select id, operation_date::date as day, amount from bank_txn
        where org_inn=%s and direction='CREDIT' and cp_inn = any(%s) and operation_date::date >= %s
        order by operation_date""", (org, cp_inns, since))]


def wb_forecast(today, horizon_end):
    out = []
    for acc, weeks in wb_weeks().items():
        org = ACC_ORG[acc]
        if not weeks:
            continue
        last_w = max(weeks)
        full = sorted(weeks)[-AVG_WEEKS:]
        avg = sum(weeks[w] for w in full) / len(full)
        credits = bank_credits(org, [WB_INN], today - OVERDUE_LOOKBACK - dt.timedelta(weeks=6))
        used = set()
        w = monday(today) - dt.timedelta(weeks=5) - OVERDUE_LOOKBACK
        while w + dt.timedelta(weeks=5) <= horizon_end:
            pay_day = w + dt.timedelta(weeks=5)
            estimate = w not in weeks
            if w > last_w:
                amount = avg * WB_RATIO
            elif w in weeks:
                amount = weeks[w] * WB_RATIO
            else:
                w += dt.timedelta(weeks=1)
                continue
            if pay_day < today:
                # срок прошёл: ищем платёж близкой суммы после конца недели
                hit = next((c for c in credits if c["id"] not in used
                            and c["day"] > w + dt.timedelta(days=6)
                            and abs(c["amount"] - amount) <= 0.02 * abs(amount) + 500), None)
                if hit:
                    used.add(hit["id"])
                    w += dt.timedelta(weeks=1)
                    continue
                if amount <= 0:
                    w += dt.timedelta(weeks=1)
                    continue
                out.append(_line(org, today, "mp_wb", amount, f"{acc}:{w}",
                                 f"WB неделя {_fmt(w)}–{_fmt(w + dt.timedelta(days=6))} · "
                                 f"просрочено (срок {_fmt(pay_day)})", True))
            elif pay_day <= horizon_end and amount > 0:
                note = f"WB неделя {_fmt(w)}–{_fmt(w + dt.timedelta(days=6))}"
                if estimate:
                    note += " · оценка, отчёта ещё нет"
                out.append(_line(org, pay_day, "mp_wb", amount, f"{acc}:{w}", note, estimate))
            w += dt.timedelta(weeks=1)
    return out


def ozon_forecast(today, horizon_end):
    rows = db.query("""
        select account, date_trunc('week', (payload->>'operation_date')::date)::date as w,
               sum((payload->>'amount')::numeric) as s,
               max((payload->>'operation_date')::date) as last_day
        from raw_ozon_transaction
        where (payload->>'operation_date')::date >= current_date - 70
        group by 1, 2""")
    by = defaultdict(dict)
    last = {}
    for r in rows:
        by[r["account"]][r["w"]] = float(r["s"])
        last[r["account"]] = max(last.get(r["account"], r["last_day"]), r["last_day"])
    out = []
    cur_w = monday(today)
    for acc, weeks in by.items():
        org = ACC_ORG[acc]
        full = [w for w in sorted(weeks) if w < cur_w][-AVG_WEEKS:]
        avg = sum(weeks[w] for w in full) / max(len(full), 1)
        w = cur_w - dt.timedelta(weeks=2)
        while w + dt.timedelta(weeks=2, days=2) <= horizon_end:
            pay_day = w + dt.timedelta(weeks=2, days=2)
            if pay_day >= today:
                if w < cur_w:
                    amount, est, note = weeks.get(w, 0.0), False, ""
                elif w == cur_w:
                    days_in = (last[acc] - w).days + 1 if last[acc] >= w else 0
                    amount = weeks.get(w, 0.0) + avg / 7 * (7 - days_in)
                    est, note = True, f" · начислено {days_in} дн, остаток по среднему"
                else:
                    amount, est, note = avg, True, " · оценка по среднему 4 недель"
                if amount > 0:
                    out.append(_line(org, pay_day, "mp_ozon", amount, f"{acc}:{w}",
                                     f"Ozon неделя {_fmt(w)}–{_fmt(w + dt.timedelta(days=6))}"
                                     + note, est))
            w += dt.timedelta(weeks=1)
    return out


def yandex_forecast(today, horizon_end):
    base = {}
    for r in db.query("""
        select f.month, f.revenue - coalesce(f.returns_sum,0)
               - coalesce((select sum(cost) from raw_yandex_services s
                           where s.account=f.account and s.ym=to_char(f.month,'YYYY-MM')),0) as net
        from yandex_finance_monthly f where f.account='ya_acc1'"""):
        base[str(r["month"])[:7]] = float(r["net"])
    cur_month = today.strftime("%Y-%m")
    full = sorted(m for m in base if m < cur_month)
    if not full:
        return []
    last_full = full[-1]
    out = []
    w = monday(today)
    while w + dt.timedelta(days=2) <= horizon_end:
        src_w = w - dt.timedelta(weeks=4)
        m = src_w.strftime("%Y-%m")
        m_used = m if m in base and m < cur_month else last_full
        dim = 30.4
        amount = base[m_used] / dim * 7
        pay_day = w + dt.timedelta(days=2)
        if pay_day >= today and amount > 0:
            out.append(_line("7807355364", pay_day, "mp_yandex", amount, f"ya_acc1:{src_w}",
                             f"Маркет неделя {_fmt(src_w)} · оценка по базе {m_used}", True))
        w += dt.timedelta(weeks=1)
    return out


def forecast(today, horizon_end):
    return wb_forecast(today, horizon_end) + ozon_forecast(today, horizon_end) \
        + yandex_forecast(today, horizon_end)
