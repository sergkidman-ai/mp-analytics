# поток: cf — раздел «Кэшфлоу» (понедельный ДДС по юрлицам)
"""Роутер раздела «Кэшфлоу». Отдельным файлом, как дневник: web/app.py общий, сюда — только cf.
Включается в app.py одной строкой include_router.

Витрину cf_line собирает cf/build.py (ежедневно по cron); здесь — чтение, остатки и правка
плана. Правка плана/остатка сразу пересобирает витрину (секунды).
Решение Сергея 04.08/22.09: фирмы строго раздельно, сводной страницы нет.
"""
import sys
import pathlib
import datetime as dt
from collections import defaultdict

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core import db  # noqa: E402

router = APIRouter(tags=["Кэшфлоу"])
STATIC = BASE_DIR / "web" / "static"

ORGS = {"7807355364": "Цифровой Квадрат", "7811803918": "Дисквэр"}
FACT_WEEKS = 8
PLAN_WEEKS = 8

# строки таблицы: (ключ, подпись, раздел, категории cf_line)
ROWS = [
    ("mp_wb", "Wildberries", "in", ["mp_wb"]),
    ("mp_ozon", "Ozon", "in", ["mp_ozon"]),
    ("mp_yandex", "Яндекс Маркет", "in", ["mp_yandex"]),
    ("b2b", "Продажи юрлицам", "in", ["b2b"]),
    ("interest", "Проценты на остаток", "in", ["interest"]),
    ("refund", "Возвраты", "in", ["refund"]),
    ("other_in", "Прочие поступления", "in", ["other_in"]),
    ("suppliers", "Поставщики", "out", ["suppliers"]),
    ("salary", "Зарплата", "out", ["salary"]),
    ("selfemp", "Самозанятые", "out", ["selfemp"]),
    ("ndfl", "НДФЛ и взносы", "out", ["ndfl"]),
    ("taxes", "Налоги (НДС, УСН)", "out", ["vat", "usn", "taxes"]),
    ("programmer", "Программист", "out", ["programmer"]),
    ("packaging", "Упаковка", "out", ["packaging"]),
    ("rent", "Аренда", "out", ["rent"]),
    ("ads", "Реклама", "out", ["ads"]),
    ("delivery", "Доставка", "out", ["delivery"]),
    ("other_out", "Прочие расходы", "out", ["other_out"]),
]
CAT_TO_ROW = {c: key for key, _, _, cats in ROWS for c in cats}
BANK_NAMES = {"alfa": "Альфа", "sber": "Сбер", "ozon": "Озон Банк"}


def monday(d):
    return d - dt.timedelta(days=d.weekday())


def _org(org):
    return org if org in ORGS else next(iter(ORGS))


# ── остатки ──────────────────────────────────────────────────────────────────
class Balances:
    """Остаток счёта на конец дня. API-банки — из cf_balance; Озон Банк — опорный ручной
    остаток (или ноль до первой операции) плюс операции выписки."""

    def __init__(self, org):
        self.org = org
        self.api = defaultdict(list)       # (bank, acc) → [(date, bal)] по возрастанию
        self.manual = defaultdict(list)
        for r in db.query("""select bank, account, bal_date, balance, source from cf_balance
                             where org_inn=%s order by bal_date""", (org,)):
            (self.manual if r["source"] == "manual" else self.api)[(r["bank"], r["account"])] \
                .append((r["bal_date"], float(r["balance"])))
        self.txn = defaultdict(list)
        self.last_txn = {}
        self.first_txn = {}
        for r in db.query("""select bank, account, operation_date::date d,
                   sum(case when direction='CREDIT' then amount else -amount end) s
                   from bank_txn where org_inn=%s group by 1,2,3 order by 3""", (org,)):
            k = (r["bank"], r["account"])
            self.txn[k].append((r["d"], float(r["s"])))
            self.first_txn.setdefault(k, r["d"])
            self.last_txn[k] = r["d"]

    def kind(self, k):
        return "api" if self.api.get(k) else "roll"

    def at(self, k, d):
        """(остаток, точный?) на конец дня d; None — неизвестен."""
        if self.api.get(k):
            prev = [b for bd, b in self.api[k] if bd <= d]
            return (prev[-1], True) if prev else (None, False)
        anchors = [(ad, b) for ad, b in self.manual.get(k, []) if ad <= d]
        if anchors:
            a_date, bal = anchors[-1]
        elif self.manual.get(k):
            # дата раньше первого опорного остатка — считаем НАЗАД от него
            a_date, bal = self.manual[k][0]
            bal -= sum(s for td, s in self.txn.get(k, []) if d < td <= a_date)
            return bal, d <= self.last_txn.get(k, d)
        elif k in self.first_txn:
            a_date, bal = self.first_txn[k] - dt.timedelta(days=1), 0.0
        else:
            return None, False
        if d < a_date:
            return None, False
        bal += sum(s for td, s in self.txn.get(k, []) if a_date < td <= d)
        return bal, d <= self.last_txn.get(k, a_date)

    def total(self, d):
        s, exact = 0.0, True
        for k in self.accounts():
            b, ok = self.at(k, d)
            if b is not None:
                s += b
            exact = exact and ok
        return s, exact

    def accounts(self):
        return set(self.api) | set(self.manual) | set(self.txn)


def warnings(org, bal, today):
    out = []
    for k in sorted(bal.accounts()):
        bank, acc = k
        name = f"{BANK_NAMES.get(bank, bank)} …{acc[-4:]}"
        last = bal.last_txn.get(k)
        if bal.kind(k) == "roll":
            if last and (today - last).days > 5:
                out.append(f"{name}: выписка загружена по {last:%d.%m} — операции после этой "
                           f"даты (в т.ч. выплаты Ozon) в факте не видны.")
            if not bal.manual.get(k):
                run, low = 0.0, 0.0
                for _, s in bal.txn.get(k, []):
                    run += s
                    low = min(low, run)
                if low < -1000:
                    out.append(f"{name}: остаток не задан — от нуля с первой операции уходит в "
                               f"минус ({low:,.0f} ₽). Введите остаток из выписки."
                               .replace(",", " "))
        elif k in bal.api:
            ld = bal.api[k][-1][0]
            if (today - ld).days > 2:
                out.append(f"{name}: остаток по API последний на {ld:%d.%m}.")
    if org == "7807355364":
        r = db.query("""select max(operation_date)::date d from bank_txn where org_inn=%s
                        and direction='CREDIT' and (cp_inn='9704254424'
                        or purpose ~* 'яндекс\\s*маркет|1936049/21')""", (org,))
        d = r[0]["d"] if r else None
        # в сентябре Маркет перевели с ежедневных выплат на еженедельные с отсрочкой 4 недели —
        # пауза в месяц ожидаема; тревога, только если тишина дольше 5 недель
        if d and (today - d).days > 35:
            out.append(f"Яндекс Маркет: последнее поступление {d:%d.%m} — выплат нет "
                       f"{(today - d).days} дн. Сверить с ЛК: Финансы → Предстоящие выплаты.")
    n = db.query("""select count(*) n, coalesce(sum(amount),0) s from cf_line
                    where org_inn=%s and note like '%%просрочено%%'""", (org,))[0]
    if n["n"]:
        out.append(f"WB: {n['n']} нед. на {float(n['s']):,.0f} ₽ — срок выплаты прошёл, платежа в "
                   f"банке не нашли; стоят в текущей неделе.".replace(",", " "))
    return out


# ── страница и данные ────────────────────────────────────────────────────────
@router.get("/cashflow")
def page():
    return FileResponse(STATIC / "cashflow.html")


@router.get("/api/cashflow")
def cashflow(org: str | None = None):
    org = _org(org)
    today = dt.date.today()
    cur_w = monday(today)
    weeks = [cur_w + dt.timedelta(weeks=i) for i in range(-FACT_WEEKS, PLAN_WEEKS + 1)]
    lines = db.query("""select week_start, kind, category, sum(amount) s, bool_or(estimate) est
                        from cf_line where org_inn=%s and week_start between %s and %s
                        group by 1,2,3""", (org, weeks[0], weeks[-1]))
    cells = defaultdict(lambda: {"fact": 0.0, "plan": 0.0, "est": False})
    for r in lines:
        c = cells[(CAT_TO_ROW.get(r["category"], "other_out" if r["s"] < 0 else "other_in"),
                   r["week_start"])]
        c[r["kind"]] += float(r["s"])
        c["est"] = c["est"] or (r["kind"] == "plan" and r["est"])
    # план, замороженный в понедельник той недели — для «план vs факт»
    snap = defaultdict(float)
    for r in db.query("""select week_start, category, amount from cf_plan_snapshot
                         where org_inn=%s and snap_date=week_start""", (org,)):
        snap[(CAT_TO_ROW.get(r["category"], "other_out"), r["week_start"])] += float(r["amount"])

    rows = []
    for key, label, sec, _ in ROWS:
        rc = {}
        for w in weeks:
            c = cells.get((key, w))
            p0 = snap.get((key, w))
            if c or p0:
                c = dict(c or {"fact": 0.0, "plan": 0.0, "est": False})
                if p0 is not None and w < cur_w:
                    c["plan0"] = round(p0, 2)
                rc[w.isoformat()] = {k: (round(v, 2) if isinstance(v, float) else v)
                                     for k, v in c.items()}
        rows.append({"key": key, "label": label, "section": sec, "cells": rc})

    bal = Balances(org)
    opening, closing, diff, exact = {}, {}, {}, {}
    for i, w in enumerate(weeks):
        net_fact = sum(cells[(k, w)]["fact"] for k, *_ in ROWS if (k, w) in cells)
        net_plan = sum(cells[(k, w)]["plan"] for k, *_ in ROWS if (k, w) in cells)
        if w <= cur_w:
            o, ok = bal.total(w - dt.timedelta(days=1))
            opening[w], exact[w] = o, ok
        else:
            opening[w], exact[w] = closing[weeks[i - 1]], exact[weeks[i - 1]]
        if w < cur_w:
            c, ok = bal.total(w + dt.timedelta(days=6))
            closing[w] = c
            exact[w] = exact[w] and ok
            diff[w] = c - opening[w] - net_fact
        else:
            closing[w] = opening[w] + net_fact + net_plan
    fmt = lambda d: {w.isoformat(): round(v, 2) for w, v in d.items()}  # noqa: E731
    acc_list = []
    for k in sorted(bal.accounts()):
        b, ok = bal.at(k, today - dt.timedelta(days=1))
        acc_list.append({"bank": k[0], "bank_name": BANK_NAMES.get(k[0], k[0]),
                         "account": k[1], "balance": None if b is None else round(b, 2),
                         "exact": ok, "source": bal.kind(k),
                         "manual": [{"date": d.isoformat(), "balance": v}
                                    for d, v in bal.manual.get(k, [])][-1:],
                         "last_txn": bal.last_txn[k].isoformat() if k in bal.last_txn else None})
    built = db.query("select max(id) from cf_line")  # есть ли витрина вообще
    return {
        "org": org, "orgs": [{"org_inn": i, "name": n} for i, n in ORGS.items()],
        "today": today.isoformat(), "current_week": cur_w.isoformat(),
        "weeks": [{"week": w.isoformat(), "end": (w + dt.timedelta(days=6)).isoformat(),
                   "kind": "fact" if w < cur_w else ("current" if w == cur_w else "plan")}
                  for w in weeks],
        "rows": rows,
        "balance": {"open": fmt(opening), "close": fmt(closing), "diff": fmt(diff),
                    "exact": {w.isoformat(): v for w, v in exact.items()}},
        "accounts": acc_list,
        "warnings": warnings(org, bal, today),
        "empty": not built or built[0]["max"] is None,
    }


@router.get("/api/cashflow/cell")
def cell(org: str, week: dt.date, row: str):
    cats = next((c for k, _, _, c in ROWS if k == row), None)
    if cats is None:
        raise HTTPException(404, "нет такой строки")
    lines = db.query("""select day, kind, category, amount, source, note, estimate from cf_line
                        where org_inn=%s and week_start=%s and category = any(%s)
                        order by day, amount""", (_org(org), monday(week), cats))
    return {"lines": [dict(r, day=r["day"].isoformat(), amount=float(r["amount"]))
                      for r in lines]}


# ── правка плана и остатков ──────────────────────────────────────────────────
class Rule(BaseModel):
    id: int | None = None
    org_inn: str
    category: str
    name: str
    amount: float
    schedule: str = "monthly"
    days: list[int]
    months: list[int] | None = None
    active: bool = True
    note: str | None = None


PLAN_CATS = {"salary", "selfemp", "ndfl", "vat", "usn", "taxes", "programmer", "packaging", "rent",
             "ads", "delivery", "other_out"}


def _rebuild():
    from cf import build
    build.build()


@router.get("/api/cashflow/rules")
def rules(org: str):
    rs = db.query("""select id, org_inn, category, name, amount, schedule, days, months, active,
                     source, note from cf_plan_rule where org_inn=%s
                     order by category, days""", (_org(org),))
    rent = db.query("select amount, pay_day, note from rent_plan where active and org_inn=%s",
                    (_org(org),))
    return {"rules": [dict(r, amount=float(r["amount"])) for r in rs],
            "rent": [dict(r, amount=float(r["amount"])) for r in rent]}


@router.post("/api/cashflow/rules")
def save_rule(r: Rule):
    if r.org_inn not in ORGS or r.category not in PLAN_CATS:
        raise HTTPException(400, "фирма или статья не из списка")
    if r.schedule not in ("monthly", "quarterly") or not r.days or \
            any(not 1 <= d <= 31 for d in r.days) or r.amount < 0:
        raise HTTPException(400, "проверьте дни (1–31), график и сумму")
    if r.schedule == "quarterly" and not r.months:
        raise HTTPException(400, "для квартального платежа укажите месяцы")
    vals = dict(r.model_dump(), source="manual")
    # первая ручная правка статьи: все её автоправила становятся ручными (иначе ночной
    # plan_seed их пересоздаст и платёж задвоится), правится только выбранная строка
    db.execute("""update cf_plan_rule set source='manual', updated_at=now()
                  where source='auto' and org_inn=%s and category=%s""", (r.org_inn, r.category))
    if r.id:
        n = db.execute("""update cf_plan_rule set category=%(category)s, name=%(name)s,
            amount=%(amount)s, schedule=%(schedule)s, days=%(days)s, months=%(months)s,
            active=%(active)s, note=%(note)s, source='manual', updated_at=now()
            where id=%(id)s and org_inn=%(org_inn)s""", vals)
        if not n:
            raise HTTPException(404, "правило не найдено")
    else:
        db.execute("""insert into cf_plan_rule (org_inn, category, name, amount, schedule, days,
            months, active, source, note) values (%(org_inn)s, %(category)s, %(name)s,
            %(amount)s, %(schedule)s, %(days)s, %(months)s, %(active)s, 'manual', %(note)s)""",
                   vals)
    _rebuild()
    return {"ok": True}


@router.delete("/api/cashflow/rules/{rid}")
def delete_rule(rid: int):
    n = db.execute("delete from cf_plan_rule where id=%s", (rid,))
    if not n:
        raise HTTPException(404, "правило не найдено")
    _rebuild()
    return {"ok": True}


class ManualBalance(BaseModel):
    org_inn: str
    bank: str
    account: str
    bal_date: dt.date
    balance: float


@router.post("/api/cashflow/balance")
def set_balance(b: ManualBalance):
    """Опорный остаток счёта без API (Озон Банк) — из выписки, на конец дня bal_date."""
    if b.org_inn not in ORGS or b.bank != "ozon":
        raise HTTPException(400, "вручную задаётся только остаток счетов Озон Банка")
    known = db.query("select 1 from bank_txn where bank='ozon' and org_inn=%s and account=%s "
                     "limit 1", (b.org_inn, b.account))
    if not known:
        raise HTTPException(400, "такого счёта нет в выписках этой фирмы")
    db.upsert("cf_balance", [dict(bank=b.bank, account=b.account, org_inn=b.org_inn,
                                  bal_date=b.bal_date, balance=b.balance, source="manual",
                                  note="из выписки, введено на странице")],
              ["bank", "account", "bal_date"])
    return {"ok": True}
