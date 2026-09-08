#!/usr/bin/env python3
# поток: inv
"""Сумма закупки по ГРУППЕ поставщика за месяц (витрина supplier_purchase_month).

Зачем: у поставщика за годы менялись ООО/ИП, и закупка размазана по разным юрлицам одной
группы. Реальный объём закупок виден только по группе целиком (таблица групп —
invoice_bot/supplier_groups.py, она же у разбора счетов/УПД).

Источник: МойСклад, проведённые документы по дате документа (moment):
  entity/supply         — приёмки, «сколько закупили»;
  entity/purchasereturn — возвраты поставщику, отдельной колонкой; нетто = приёмки − возвраты.
Суммы документов МС (в копейках) → рубли, с НДС, как в самих документах.

Нагрузка на МС: страницы по 1000 документов БЕЗ expand — весь 2026 год это ~3 запроса,
ночной прогон текущего месяца — 1 запрос. Группу берём по id контрагента из ссылки agent,
имя запрашиваем только для контрагентов ВНЕ групп и кэшируем в ms_agent_name.

Прошлые месяцы статичны: без аргументов пересчитывается ТОЛЬКО текущий месяц.

Запуск:
  ./venv/bin/python -m collectors.supplier_purchase_month              # текущий месяц (ночью)
  ./venv/bin/python -m collectors.supplier_purchase_month --since 2026-01   # разовый backfill
"""
import argparse
import datetime as dt
import sys
from collections import defaultdict

from collectors.moysklad import _get, API
from core import db
from invoice_bot.supplier_groups import ID2GROUP, ID2NAME

PAGE = 1000                 # максимум МойСклада для выдачи без expand
START_MONTH = "2026-01"     # с какого месяца имеет смысл витрина (решение 08.09.2026)


def _month_start(s):
    """'2026-01' | '2026-01-15' → date(2026,1,1)."""
    parts = s.split("-")
    return dt.date(int(parts[0]), int(parts[1]), 1)


def _next_month(d):
    return dt.date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def _fetch(entity, since, until):
    """Проведённые документы entity за период → [(месяц, agent_id, сумма_₽)]."""
    out, offset, total = [], 0, None
    flt = (f"moment>={since:%Y-%m-%d} 00:00:00;"
           f"moment<={until:%Y-%m-%d} 23:59:59;applicable=true")
    while total is None or offset < total:
        r = _get(f"{API}/entity/{entity}", params={"limit": PAGE, "offset": offset, "filter": flt})
        total = r["meta"]["size"]
        rows = r.get("rows", [])
        if not rows:
            break
        for d in rows:
            moment = (d.get("moment") or "")[:10]
            if not moment:
                continue
            agent = (d.get("agent") or {}).get("meta", {}).get("href", "").rsplit("/", 1)[-1]
            out.append((dt.date.fromisoformat(moment).replace(day=1), agent,
                        float(d.get("sum") or 0) / 100.0))
        offset += PAGE
    print(f"  {entity}: {len(out)} документов за {since}…{until}", flush=True)
    return out


def _agent_names(ids):
    """Имена контрагентов вне групп: сначала кэш ms_agent_name, недостающие — из МС."""
    ids = {i for i in ids if i}
    if not ids:
        return {}
    known = {r["agent_id"]: r["name"] for r in db.query(
        "SELECT agent_id, name FROM ms_agent_name WHERE agent_id = ANY(%s)", (list(ids),))}
    miss = ids - set(known)
    fresh = []
    for aid in sorted(miss):
        try:
            name = (_get(f"{API}/entity/counterparty/{aid}").get("name") or "").strip()
        except Exception as e:                       # удалённый контрагент — не роняем прогон
            print(f"  имя контрагента {aid} не получено: {e}", flush=True)
            name = ""
        name = name or f"контрагент {aid[:8]}"
        known[aid] = name
        fresh.append({"agent_id": aid, "name": name})
    if fresh:
        db.upsert("ms_agent_name", fresh, ["agent_id"])
    return known


def collect(since_month):
    """Пересчёт витрины с месяца since_month по текущий включительно."""
    today = dt.date.today()
    cur_month = today.replace(day=1)
    since = max(since_month, _month_start(START_MONTH))
    if since > cur_month:
        print(f"Нечего считать: {since:%Y-%m} позже текущего месяца", flush=True)
        return 0
    print(f"Закупки по группам: месяцы {since:%Y-%m}…{cur_month:%Y-%m}", flush=True)
    supplies = _fetch("supply", since, today)
    returns = _fetch("purchasereturn", since, today)

    agg = defaultdict(lambda: {"supply_sum": 0.0, "supply_docs": 0,
                               "return_sum": 0.0, "return_docs": 0})
    unknown = set()
    for kind, rows in (("supply", supplies), ("return", returns)):
        for month, agent, amount in rows:
            grp = ID2GROUP.get(agent)
            if not grp:
                unknown.add(agent)
            key = (month, grp or agent)
            agg[key][f"{kind}_sum"] += amount
            agg[key][f"{kind}_docs"] += 1
    names = _agent_names(unknown)

    out = []
    for (month, key), v in agg.items():
        in_group = key not in names
        out.append({"month": month, "grp": key if in_group else names[key],
                    "supply_sum": round(v["supply_sum"], 2), "supply_docs": v["supply_docs"],
                    "return_sum": round(v["return_sum"], 2), "return_docs": v["return_docs"],
                    "in_group": in_group, "updated_at": dt.datetime.now()})
    # имена вне групп могут схлопнуться в одну строку с группой — складываем по (месяц, имя)
    merged = {}
    for r in out:
        key = (r["month"], r["grp"])
        m = merged.get(key)
        if m is None:                 # setdefault здесь нельзя: он кладёт КОПИЮ dict(r),
            merged[key] = dict(r)     # и проверка `m is not r` даёт задвоение сумм
            continue
        for f in ("supply_sum", "supply_docs", "return_sum", "return_docs"):
            m[f] += r[f]
        m["in_group"] = m["in_group"] or r["in_group"]
    rows = list(merged.values())

    # перезапись только пересчитанного диапазона: прошлые месяцы остаются нетронутыми
    db.execute("DELETE FROM supplier_purchase_month WHERE month >= %s", (since,))
    if rows:
        db.upsert("supplier_purchase_month", rows, ["month", "grp"])
    tot = sum(r["supply_sum"] for r in rows)
    print(f"  записано строк: {len(rows)}, приёмок на {tot:,.0f} ₽".replace(",", " "), flush=True)
    if unknown:
        print(f"  контрагентов вне таблицы групп: {len(unknown)}", flush=True)
    return len(rows)


def main(since=None):
    return collect(_month_start(since) if since else dt.date.today().replace(day=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Закупки по группам поставщиков помесячно")
    ap.add_argument("--since", help="месяц начала пересчёта, YYYY-MM (по умолчанию текущий)")
    a = ap.parse_args()
    sys.exit(0 if main(a.since) is not None else 1)
