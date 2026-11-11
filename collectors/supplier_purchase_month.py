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

Нагрузка на МС: страницы по 20 документов БЕЗ expand; история читается один раз,
ночной прогон обновляет только незакрытые месяцы. Группу берём по id контрагента из ссылки agent,
имя запрашиваем только для контрагентов ВНЕ групп и кэшируем в ms_agent_name.

Прошлые месяцы закрываются и архивируются; без аргументов обновляется текущий
и закрываются пропущенные завершённые месяцы. Закрытые месяцы защищены в БД.

Запуск:
  ./venv/bin/python -m collectors.supplier_purchase_month              # текущий месяц (ночью)
  ./venv/bin/python -m collectors.supplier_purchase_month --since 2026-01   # разовый backfill
"""
import argparse
import datetime as dt
import sys
from collections import defaultdict
from decimal import Decimal
from core import supplier_purchase_history as history

from collectors.moysklad import _get, API
from core import db
from invoice_bot.supplier_groups import ID2GROUP, ID2NAME

PAGE = 20                 # максимум МойСклада для выдачи без expand
START_MONTH = "2026-01"     # с какого месяца имеет смысл витрина (решение 08.09.2026)


def _month_start(s):
    """'2026-01' | '2026-01-15' → date(2026,1,1)."""
    parts = s.split("-")
    return dt.date(int(parts[0]), int(parts[1]), 1)


def _next_month(d):
    return dt.date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def _fetch_docs(entity, since, until):
    """Узкие страницы без expand, полная проверка количества и идентификаторов."""
    out, offset, total, seen = [], 0, None, set()
    flt = (f"moment>={since:%Y-%m-%d} 00:00:00;"
           f"moment<={until:%Y-%m-%d} 23:59:59;applicable=true")
    while total is None or offset < total:
        result = _get(f"{API}/entity/{entity}", params={
            'limit': PAGE, 'offset': offset, 'filter': flt, 'order': 'moment,asc'})
        size = result['meta']['size']
        if total is None:
            total = size
        elif size != total:
            raise ValueError('Supplier source changed while paging; retry required')
        rows = result.get('rows', [])
        if not rows and offset < total:
            raise ValueError('Incomplete supplier document page')
        for row in rows:
            identity = row.get('id')
            if not identity or identity in seen or row.get('applicable') is not True:
                raise ValueError('Invalid supplier document identity/state')
            day = dt.date.fromisoformat(row['moment'][:10])
            if not since <= day <= until:
                raise ValueError('Supplier document outside requested period')
            seen.add(identity)
        out.extend(rows)
        offset += len(rows)
        if not rows:
            break
    if len(out) != total:
        raise ValueError('Supplier document count mismatch')
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


def aggregate(month, documents):
    agg = defaultdict(lambda: {'supply_sum': Decimal(0), 'supply_docs': 0,
                               'return_sum': Decimal(0), 'return_docs': 0})
    unknown = set()
    for entity, kind in [('supply', 'supply'), ('purchasereturn', 'return')]:
        seen = set()
        for doc in documents[entity]:
            day = dt.date.fromisoformat(doc['moment'][:10])
            identity = doc.get('id')
            if (day.replace(day=1) != month or not identity or identity in seen
                    or doc.get('applicable') is not True):
                raise ValueError('Invalid supplier source document')
            seen.add(identity)
            agent = doc.get('agent', {}).get('meta', {}).get('href', '').rsplit('/', 1)[-1]
            if not agent:
                raise ValueError('Supplier document has no agent')
            grp = ID2GROUP.get(agent)
            if not grp:
                unknown.add(agent)
            row = agg[(grp or agent)]
            row[kind+'_sum'] += Decimal(str(doc.get('sum') or 0)) / 100
            row[kind+'_docs'] += 1
    names = _agent_names(unknown)
    merged = {}
    for key, values in agg.items():
        group = names.get(key, key)
        row = merged.setdefault(group, dict(month=month, grp=group, supply_sum=Decimal(0),
            supply_docs=0, return_sum=Decimal(0), return_docs=0, in_group=False))
        for field, value in values.items():
            row[field] += value
        row['in_group'] = row['in_group'] or key not in names
    return list(merged.values())


def refresh_last_purchases():
    # Тот же проверенный источник: дополнительных запросов к МС не требуется.
    rows = db.query("""SELECT d #>> '{agent,meta,href}' agent,
        max((d->>'moment')::date) last_supply,
        count(*) FILTER(WHERE (d->>'moment')::date >= %s) count90
        FROM supplier_purchase_period p
        CROSS JOIN LATERAL jsonb_array_elements(p.payload #> '{documents,supply}') d
        GROUP BY 1""", (history.today()-dt.timedelta(days=90),))
    names = dict(ID2NAME)
    names.update({r['agent_id']: r['name'] for r in db.query('SELECT agent_id,name FROM ms_agent_name')})
    merged = {}
    for row in rows:
        agent = (row['agent'] or '').rsplit('/', 1)[-1]
        name = names.get(agent)
        if not name:
            continue
        value = merged.setdefault(name, {'supplier': name, 'last_supply': row['last_supply'],
                                         'supply_count_90d': 0})
        value['last_supply'] = max(value['last_supply'], row['last_supply'])
        value['supply_count_90d'] += row['count90']
    if merged:
        db.upsert('supplier_last_purchase', list(merged.values()), ['supplier'])


def collect(since_month):
    today = history.today()
    current = today.replace(day=1)
    month = max(since_month, _month_start(START_MONTH))
    closed = {r['month'] for r in history.periods() if r['closed_at'] is not None}
    count = 0
    while month <= current:
        if month in closed:
            history.archive(month)
        else:
            end = min(today, _next_month(month)-dt.timedelta(days=1))
            documents = {entity: _fetch_docs(entity, month, end)
                         for entity in ['supply', 'purchasereturn']}
            rows = aggregate(month, documents)
            count += history.save(month, rows, documents)
            print(f"{month:%Y-%m}: {len(documents['supply'])} приёмок, "
                  f"{len(documents['purchasereturn'])} возвратов; "
                  + ('закрыт' if month < current else 'расчётный'), flush=True)
        month = _next_month(month)
    refresh_last_purchases()
    return count


def main(since=None):
    # При смене месяца завершаем предыдущий; уже закрытые месяцы не запрашиваются.
    return collect(_month_start(since or START_MONTH))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Закупки по группам поставщиков помесячно")
    ap.add_argument("--since", help="месяц начала пересчёта, YYYY-MM (по умолчанию текущий)")
    a = ap.parse_args()
    sys.exit(0 if main(a.since) is not None else 1)
