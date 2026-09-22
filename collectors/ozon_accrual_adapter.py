"""collectors/ozon_accrual_adapter.py — raw_ozon_accrual → raw_ozon_transaction (старый формат).

Зачем: Ozon отключил /v3/finance/transaction/list, а raw_ozon_transaction читают ~10 потребителей
(categorize_operation, ozon_mp_report, margin_ozon_sku, web/app, ozon_margin_control, ...).
Вместо переписывания каждого собираем из начисления payload старой формы — потребители не меняются.

Факт: accrual_id == operation_id и сумма совпадают у 100 % пар (6986 шт., 01.08–07.09.2026).
Таблицы соответствия — collectors/ozon_accrual_map.json (вывод и проверка:
docs/reference/ozon_accrual_to_txn_mapping.md). Баланс ЛК (_balance_range) — 100 % до копейки.

Что НЕ восстанавливается точно (помечено в payload `_source` = "accrual"):
- posting.order_date — в начислении нет. Берём raw_ozon_posting.in_process_at (МСК): тот же день
  у 91 % отправлений, остальные ≈ +1 сутки (заказ ушёл в обработку на следующий день).
  Нужен только как опорная дата фолбэка FIFO в margin_ozon_sku.
- type 71 (вывоз со склада / сбор возвратов), 25 (потеря по вине Ozon / начисление по спору),
  16 (Drop-off ПВЗ / СЦ) — признака различия нет; итог операции при этом верен.

Настоящие строки transaction/list (до 08.09) адаптер НЕ перезаписывает: ON CONFLICT обновляет
только строки с `_source` = "accrual".

Запуск:  ./venv/bin/python collectors/ozon_accrual_adapter.py 2026-09-08 2026-09-21 [oz_acc1] [--verify]
         --verify: ничего не пишет, сравнивает сборку с настоящими строками (для дней до 08.09).
"""
import sys
import json
import pathlib
import datetime as dt
from collections import Counter, defaultdict

import psycopg2.extras

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

MAP = json.loads((BASE_DIR / "collectors" / "ozon_accrual_map.json").read_text())
TYPES = {t["id"]: t for t in json.loads(
    (BASE_DIR / "docs" / "reference" / "ozon_accrual_types.json").read_text())["accrual_types"]}
MSK = dt.timezone(dt.timedelta(hours=3))


def _m(v):
    if isinstance(v, dict):
        v = v.get("amount")
    return float(v or 0)


def _fees(a):
    """[(type_id, сумма)] видов начислений операции."""
    cat = a.get("accrued_category")
    if cat == "NON_ITEM":
        f = a.get("non_item_fee") or {}
        return [(f.get("type_id"), _m(f.get("accrued")))] if f else []
    out = []
    if cat == "ITEM":
        for it in (a.get("item_fees") or {}).get("fees") or []:
            out += [(f.get("type_id"), _m(f.get("accrued"))) for f in it.get("fees") or []]
    else:
        for p in (a.get("posting") or {}).get("products") or []:
            out += [(s.get("type_id"), _m(s.get("accrued")))
                    for s in (p.get("delivery") or {}).get("services") or []]
    return out


def _commission(a):
    sale = comm = 0.0
    has = False
    for p in (a.get("posting") or {}).get("products") or []:
        c = p.get("commission")
        if c:
            has = True
            sale += _m(c.get("sale_amount"))
            comm += _m(c.get("sale_commission"))
    return has, sale, comm


def operation_type(a, fees, has_comm, sale):
    cat, amount = a.get("accrued_category"), _m(a.get("total_amount"))
    tids = sorted({t for t, _ in fees if t is not None})
    sign = ("c+" if sale > 0 else "c-") if has_comm else ""
    if cat == "NON_ITEM" and tids == [94] and amount > 0:
        return "DefectFineShipmentDelayRatedCancelled"
    op = MAP["key_to_operation_type"].get(f"{cat}|{','.join(map(str, tids))}|{sign}")
    if op:
        return op
    if cat == "POSTING":   # правила из анализа — для наборов, которых не было в выборке
        if has_comm and sale > 0:
            return "OperationAgentDeliveredToCustomer"
        if has_comm:
            return "OperationAgentStornoDeliveredToCustomer" if tids else "ClientReturnAgentOperation"
        return ("OperationAgentDeliveredToCustomer" if 32 in tids and len(tids) >= 2
                else "OperationReturnGoodsFBSofRMS")
    return f"Accrual_{'_'.join(map(str, tids)) or cat}"


def _op_name_type(op, fees):
    known = MAP["operation_type_to_name_type"].get(op)
    if known:
        return known["operation_type_name"], known["type"]
    tid = fees[0][0] if fees else None
    return (TYPES.get(tid) or {}).get("description") or op, "other"


def _service_name(tid, amount):
    names = MAP["service_name_by_type_id"].get(str(tid))
    if not names:
        return f"Accrual_{tid}_{(TYPES.get(tid) or {}).get('name', '')}"
    if tid == 51 and amount > 0:
        return "PremiumMembershipCommissionCancelled"
    if tid == 71:
        return "MarketplaceServiceProductMovementFromWarehouse"   # «Вывоз товара со склада» (справочник)
    return names[0]


def build(a, post, names):
    """payload старого формата из начисления. post — строка raw_ozon_posting или None."""
    fees = _fees(a)
    has_comm, sale, comm = _commission(a)
    op = operation_type(a, fees, has_comm, sale)
    op_name, op_kind = _op_name_type(op, fees)
    cat = a.get("accrued_category")

    services = defaultdict(float)
    if cat != "NON_ITEM":      # у NON_ITEM старый формат держал сумму в остатке, services пусты
        for tid, v in fees:
            services[_service_name(tid, v)] += v

    if cat == "POSTING":
        units = [(p.get("sku"), p.get("quantity") or 1) for p in a["posting"].get("products") or []]
        schema = (a["posting"].get("delivery_schema") or "").upper()
    else:
        units = [(it.get("sku"), it.get("quantity") or 1)
                 for it in (a.get("item_fees") or {}).get("fees") or []]
        schema = ""
    if not schema and post:
        tpl = (post["payload"] or {}).get("tpl_integration_type")
        schema = ("FBO" if post["scheme"] == "fbo"
                  else "RFBS" if tpl in ("aggregator", "hybrid") else "FBS")
    order_date = (post["in_process_at"].astimezone(MSK).strftime("%Y-%m-%d %H:%M:%S")
                  if post and post["in_process_at"] else "")

    return {
        "_source": "accrual",
        "operation_id": int(a["accrual_id"]),
        "operation_date": f"{a['date']} 00:00:00",
        "operation_type": op,
        "operation_type_name": op_name,
        "type": op_kind,
        "amount": round(_m(a.get("total_amount")), 2),
        "accruals_for_sale": round(sale, 2),
        "sale_commission": round(comm, 2),
        "delivery_charge": 0,
        "return_delivery_charge": 0,
        "services": [{"name": n, "price": round(v, 2)} for n, v in services.items()],
        "items": [{"sku": s, "name": names.get(s, "")} for s, q in units for _ in range(q)],
        "posting": {"posting_number": a.get("unit_number") or "", "delivery_schema": schema,
                    "order_date": order_date, "warehouse_id": 0},
    }


def _load(account, date_from, date_to):
    acc = db.query("""select payload from raw_ozon_accrual
                       where account = %s and accrual_date between %s and %s
                       order by accrual_date, accrual_id""", (account, date_from, date_to))
    acc = [r["payload"] for r in acc]
    units = sorted({a.get("unit_number") for a in acc if a.get("unit_number")})
    posts = {r["posting_number"]: r for r in db.query(
        """select distinct on (posting_number) posting_number, scheme, in_process_at, payload
             from raw_ozon_posting where account = %s and posting_number = any(%s)
            order by posting_number, loaded_at desc""", (account, units))} if units else {}
    names = {r["sku"]: r["name"] for r in db.query(
        "select sku, name from ozon_product where account = %s", (account,))}
    return [build(a, posts.get(a.get("unit_number")), names) for a in acc]


def save(account, ops, date_from, date_to):
    sql = """insert into raw_ozon_transaction (account, operation_id, period_from, period_to, payload)
             values %s
             on conflict (account, operation_id) do update
                set payload = excluded.payload, period_from = excluded.period_from,
                    period_to = excluded.period_to, loaded_at = now()
              where raw_ozon_transaction.payload->>'_source' = 'accrual'"""
    rows = [(account, o["operation_id"], date_from, date_to, psycopg2.extras.Json(o)) for o in ops]
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, sql, rows, page_size=500)
    return len(rows)


def verify(account, ops):
    """Сравнить сборку с настоящими строками transaction/list. → Counter расхождений по полю."""
    real = {r["operation_id"]: r["payload"] for r in db.query(
        """select operation_id, payload from raw_ozon_transaction
            where account = %s and operation_id = any(%s)
              and coalesce(payload->>'_source', '') <> 'accrual'""",
        (account, [o["operation_id"] for o in ops]))}
    bad, n = Counter(), 0
    for o in ops:
        r = real.get(o["operation_id"])
        if r is None:
            continue
        n += 1
        for f in ("operation_type", "operation_type_name", "type", "operation_date"):
            bad[f] += o[f] != r.get(f)
        for f in ("amount", "accruals_for_sale", "sale_commission"):
            bad[f] += abs(o[f] - float(r.get(f) or 0)) > 0.005
        so, sr = defaultdict(float), defaultdict(float)
        for s in o["services"]:
            so[s["name"]] += s["price"]
        for s in r.get("services") or []:
            sr[s["name"]] += float(s.get("price") or 0)
        bad["services"] += any(abs(so[k] - sr[k]) > 0.005 for k in set(so) | set(sr))
        bad["items_sku"] += Counter(i["sku"] for i in o["items"]) != Counter(
            i.get("sku") for i in r.get("items") or [])
        rp = r.get("posting") or {}
        bad["posting_number"] += o["posting"]["posting_number"] != (rp.get("posting_number") or "")
        bad["delivery_schema"] += o["posting"]["delivery_schema"] != (rp.get("delivery_schema") or "")
        bad["order_date_day"] += o["posting"]["order_date"][:10] != (rp.get("order_date") or "")[:10]
    return n, bad


def main(date_from, date_to, account="oz_acc1", check=False):
    ops = _load(account, date_from, date_to)
    print(f"Ozon адаптер {account} {date_from}..{date_to}: начислений {len(ops)}, "
          f"сумма {sum(o['amount'] for o in ops):,.2f}", flush=True)
    if check:
        n, bad = verify(account, ops)
        print(f"  [verify] сравнено {n}; расхождения: "
              + (", ".join(f"{k} {v}" for k, v in bad.items() if v) or "нет"), flush=True)
        return n, bad
    print(f"  → raw_ozon_transaction {save(account, ops, date_from, date_to)}", flush=True)


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    main(a[0], a[1] if len(a) > 1 else a[0], a[2] if len(a) > 2 else "oz_acc1",
         check="--verify" in sys.argv)
