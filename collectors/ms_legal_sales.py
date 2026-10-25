# поток: fin
"""Продажи юрлицам Цифрового квадрата: отгрузки, FIFO и связанные оплаты МС.

scan MONTH сохраняет узкую выборку из всех отгрузок месяца (проект может стоять
только в заказе). build собирает проверенный снимок для раздела Пульта.
"""
import argparse
import json
from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from collectors.mp_cogs_common import Reader, MsReadError
from collectors import ms_demand_cogs as MDC

BASE = Path(__file__).resolve().parents[1] / "reports/data/ms_legal_sales"
RAW = BASE / "raw"
OUT = BASE / "report.json"
COSTS = BASE / "costs.json"
PAYMENTS = BASE / "payments.json"
AGENTS = BASE / "agents.json"
ORG = "a5e7ee84-8d63-41f2-9fae-0be2ef90462b"
PROJECTS = {
    "919ca5eb-61e5-11eb-0a80-07d50002b5bb": "Digitalsquare",
    "04d8b84e-8785-11e6-7a69-971100007175": "Kartridge.org",
    "6dd11126-16c6-11f0-0a80-166b000c5745": "Вручную",
    "a7df7ff3-eb3e-11ee-0a80-0ee50006c1db": "Заправка",
}
# Отгрузка может иметь технический проект Авито, а заказ — целевой проект.
OTHER_DEMAND_PROJECTS = ("d29c1d47-713e-11ed-0a80-056c0005d5bf",)
ORG_HREF = f"{MDC.MS}/entity/organization/{ORG}"
EXCHANGE_STATUS = "Претензия решена обмен"
REPORT_STATES = {"Доставлен", EXCHANGE_STATUS}
SHORT_SHIPMENT = {
    "primary_demand_id": "92688d2c-97d1-11f1-0a80-18d100134ec9",  # 56856, оформлено 3 шт.
    "primary_order_id": "56565c8a-97ae-11f1-0a80-1cf0001c4e42",  # фактически 1 шт.
    "completion_demand_id": "ade8cba4-9d27-11f1-0a80-18160004d309",  # 56856.1, досылка 2 шт.
    "completion_order_id": "71d788cb-9d27-11f1-0a80-01070004e1e8",
}


def _id(obj):
    return ((obj or {}).get("meta") or {}).get("href", "").split("/")[-1].split("?")[0]


def _money(kopecks):
    return str((Decimal(str(kopecks)) / 100).quantize(Decimal("0.01")))


def _atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def scan(month, reader=None, through=None):
    """Сканировать месяц по организации, затем отобрать проект заказа/отгрузки."""
    reader = reader or Reader(budget_seconds=1800, page_size=20)
    year, mon = map(int, month.split("-"))
    start = f"{month}-01 00:00:00"
    end_date = date(year + (mon == 12), mon % 12 + 1, 1)
    if through:
        end_date = min(end_date, through + timedelta(days=1))
    end = f"{end_date:%Y-%m-%d} 00:00:00"
    found = []
    params = {"filter": f"organization={ORG_HREF};moment>={start};moment<{end}",
              "expand": "customerOrder.state"}
    for demand in reader.rows("/entity/demand", params):
        order = demand.get("customerOrder") or {}
        demand_project, order_project = _id(demand.get("project")), _id(order.get("project"))
        if demand_project not in PROJECTS and order_project not in PROJECTS:
            continue
        order_status = (order.get("state") or {}).get("name")
        if order_status not in REPORT_STATES:
            continue
        order_name = order.get("name") or demand.get("name") or ""
        # Замены по браку не создают второй продажи и не добавляют FIFO к
        # себестоимости исходной отгрузки (включая обмены 56742 и 56809).
        if order_name.endswith((".0", ".1")):
            continue
        if not demand.get("applicable"):
            continue
        found.append({"id": demand["id"], "name": demand.get("name"),
                      "moment": demand.get("moment"), "month": month,
                      "sum_kopecks": demand.get("sum"), "order_id": _id(order),
                      "order_name": order_name, "agent_id": _id(demand.get("agent")),
                      "order_agent_id": _id(order.get("agent")),
                      "order_status": order_status,
                      "demand_project_id": demand_project, "order_project_id": order_project})
    # В МС часть отгрузок создана отдельно от заказа: customerOrder пуст,
    # хотя заказ с тем же номером, контрагентом и статусом существует.
    # Запрашиваем только узкие проекты и сверяем пару, не полагаясь на имя одно.
    seen = {x["id"] for x in found}
    order_cache = {}
    for project_id in (*PROJECTS, *OTHER_DEMAND_PROJECTS):
        project_href = f"{MDC.MS}/entity/project/{project_id}"
        project_params = {"filter": f"organization={ORG_HREF};project={project_href};"
                                    f"moment>={start};moment<{end}"}
        for demand in reader.rows("/entity/demand", project_params):
            if demand["id"] in seen or _id(demand.get("customerOrder")):
                continue
            name = demand.get("name") or ""
            if not name or name.endswith((".0", ".1")) or not demand.get("applicable"):
                continue
            if name not in order_cache:
                order_cache[name] = list(reader.rows("/entity/customerorder",
                                                     {"filter": "name=" + name,
                                                      "expand": "state"}))
            matches = [order for order in order_cache[name]
                       if _id(order.get("agent")) == _id(demand.get("agent"))
                       and _id(order.get("organization")) == ORG
                       and (order.get("state") or {}).get("name") in REPORT_STATES
                       and int(order.get("sum") or 0) == int(demand.get("sum") or 0)]
            if len(matches) != 1:
                continue
            order = matches[0]
            order_project = _id(order.get("project"))
            if project_id not in PROJECTS and order_project not in PROJECTS:
                continue
            found.append({"id": demand["id"], "name": name,
                          "moment": demand.get("moment"), "month": month,
                          "sum_kopecks": demand.get("sum"),
                          "order_id": order["id"], "order_name": order["name"],
                          "agent_id": _id(demand.get("agent")),
                          "order_agent_id": _id(order.get("agent")),
                          "order_status": (order.get("state") or {}).get("name"),
                          "demand_project_id": project_id,
                          "order_project_id": order_project,
                          "matched_by_name": True})
            seen.add(demand["id"])
    # Если у отгрузки проект вообще не указан, ищем её от целевых заказов.
    # Окно захватывает заказы прошлого месяца, отгруженные в текущем.
    order_start = date(year, mon, 1) - timedelta(days=31)
    checked_agents = {}
    for project_id in PROJECTS:
        project_href = f"{MDC.MS}/entity/project/{project_id}"
        order_params = {"filter": f"organization={ORG_HREF};project={project_href};"
                                  f"moment>={order_start:%Y-%m-%d} 00:00:00;moment<{end}",
                        "expand": "state"}
        for order in reader.rows("/entity/customerorder", order_params):
            name = order.get("name") or ""
            if (not name or name.endswith((".0", ".1")) or
                    (order.get("state") or {}).get("name") not in REPORT_STATES):
                continue
            if any(x["order_id"] == order["id"] for x in found):
                continue
            agent_id = _id(order.get("agent"))
            if agent_id not in checked_agents:
                checked_agents[agent_id] = reader.get("/entity/counterparty/" + agent_id).get("companyType")
            if checked_agents[agent_id] not in ("legal", "entrepreneur"):
                continue
            for demand in reader.rows("/entity/demand", {"filter": "name=" + name}):
                if demand["id"] in seen or _id(demand.get("customerOrder")):
                    continue
                if (_id(demand.get("agent")) != agent_id or
                        _id(demand.get("organization")) != ORG or
                        not (start <= (demand.get("moment") or "") < end) or
                        not demand.get("applicable") or
                        int(demand.get("sum") or 0) != int(order.get("sum") or 0)):
                    continue
                found.append({"id": demand["id"], "name": name,
                              "moment": demand["moment"], "month": month,
                              "sum_kopecks": demand["sum"], "order_id": order["id"],
                              "order_name": name, "agent_id": agent_id,
                              "order_agent_id": agent_id,
                              "order_status": (order.get("state") or {}).get("name"),
                              "demand_project_id": _id(demand.get("project")),
                              "order_project_id": project_id, "matched_by_name": True})
                seen.add(demand["id"])
    # Документы 56856/56856.1 — один оплаченный заказ на 3 шт. Недовложенные
    # 2 шт. досланы отдельным документом; второй оборот прибавлять нельзя.
    if month == "2026-08" and SHORT_SHIPMENT["primary_demand_id"] not in seen:
        details = _short_shipment_details(reader)
        found.append({"id": SHORT_SHIPMENT["primary_demand_id"], "name": "56856",
                      "moment": details["primary_moment"], "month": month,
                      "sum_kopecks": details["sale_kopecks"],
                      "order_id": SHORT_SHIPMENT["primary_order_id"], "order_name": "56856",
                      "agent_id": details["agent_id"], "order_agent_id": details["agent_id"],
                      "order_status": "Доставлен",
                      "demand_project_id": details["project_id"],
                      "order_project_id": details["project_id"],
                      "short_shipment_completion_id": SHORT_SHIPMENT["completion_demand_id"]})
    _atomic(RAW / f"{month}.json", found)
    return len(found)


def _fifo(reader, demand_id):
    report = reader.get("/report/stock/byoperation?operation.id=" + demand_id)
    rows = report.get("rows")
    if not isinstance(rows, list) or len(rows) > 1:
        raise MsReadError(f"FIFO {demand_id}: invalid operation report")
    report_positions = rows[0].get("positions") or [] if rows else []
    stock_ids = {_id(x) for x in report_positions}
    doc_positions = list(reader.rows(f"/entity/demand/{demand_id}/positions"))
    missing = [x for x in doc_positions if (x.get("assortment") or {}).get("meta", {}).get("type") != "service"
               and _id(x.get("assortment")) not in stock_ids]
    if missing:
        raise MsReadError(f"FIFO {demand_id}: {len(missing)} stock positions absent")
    return _money(sum(Decimal(str(x.get("cost") or 0)) for x in report_positions))


def _short_shipment_details(reader):
    """Проверить 56856 + досылку 56856.1 и получить FIFO трёх реальных штук."""
    ids = SHORT_SHIPMENT
    primary = reader.get("/entity/demand/" + ids["primary_demand_id"])
    completion = reader.get("/entity/demand/" + ids["completion_demand_id"])
    original_order = reader.get("/entity/customerorder/" + ids["primary_order_id"] + "?expand=state")
    completion_order = reader.get("/entity/customerorder/" + ids["completion_order_id"] + "?expand=state")
    if (primary.get("name") != "56856" or completion.get("name") != "56856.1"
            or original_order.get("name") != "56856" or completion_order.get("name") != "56856.1"
            or not primary.get("applicable") or not completion.get("applicable")
            or any(_id(x.get("organization")) != ORG for x in
                   (primary, completion, original_order, completion_order))
            or len({_id(x.get("agent")) for x in
                    (primary, completion, original_order, completion_order)}) != 1
            or any((x.get("state") or {}).get("name") != "Доставлен"
                   for x in (original_order, completion_order))
            or _id(completion.get("customerOrder")) != ids["completion_order_id"]
            or _id(primary.get("project")) not in PROJECTS):
        raise MsReadError("56856: документы или статус досылки изменились")
    positions = [list(reader.rows(f"/entity/demand/{did}/positions")) for did in
                 (ids["primary_demand_id"], ids["completion_demand_id"])]
    order_positions = [list(reader.rows(f"/entity/customerorder/{oid}/positions")) for oid in
                       (ids["primary_order_id"], ids["completion_order_id"])]
    if any(len(p) != 1 for p in (*positions, *order_positions)):
        raise MsReadError("56856: ожидалась одна товарная позиция в каждом документе")
    assortment_ids = {_id(p[0].get("assortment")) for p in (*positions, *order_positions)}
    primary_qty, completion_qty = (Decimal(str(p[0].get("quantity") or 0)) for p in positions)
    original_order_qty, completion_order_qty = (Decimal(str(p[0].get("quantity") or 0))
                                                for p in order_positions)
    sale = Decimal(str(primary.get("sum") or 0))
    if (len(assortment_ids) != 1 or primary_qty != 3 or completion_qty != 2
            or original_order_qty != 1 or completion_order_qty != 2
            or original_order_qty + completion_order_qty != primary_qty
            or Decimal(str(original_order.get("sum") or 0))
               + Decimal(str(completion_order.get("sum") or 0)) != sale
            or Decimal(str(completion.get("sum") or 0))
               != Decimal(str(completion_order.get("sum") or 0))):
        raise MsReadError("56856: количество, товар или сумма досылки изменились")
    primary_fifo = Decimal(_fifo(reader, ids["primary_demand_id"]))
    completion_fifo = Decimal(_fifo(reader, ids["completion_demand_id"]))
    cogs = (primary_fifo * original_order_qty / primary_qty + completion_fifo).quantize(Decimal("0.01"))
    return {"sale_kopecks": primary.get("sum"), "cogs": str(cogs),
            "primary_moment": primary.get("moment"), "completion_date": completion["moment"][:10],
            "agent_id": _id(primary.get("agent")), "project_id": _id(primary.get("project"))}


def _payments(reader, agents):
    """Связь оплаты с заказом напрямую или через исходящий счёт."""
    by_order, by_demand = defaultdict(list), defaultdict(list)
    invoice_payments = []
    for agent in agents:
        agent_href = f"{MDC.MS}/entity/counterparty/{agent}"
        for pay in reader.rows("/entity/paymentin", {"filter":
                               f"organization={ORG_HREF};agent={agent_href}"}):
            for operation in pay.get("operations") or []:
                meta = operation.get("meta") or {}
                item = {"payment_id": pay["id"], "payment_name": pay.get("name"),
                        "payment_date": (pay.get("moment") or "")[:10],
                        "amount": _money(operation.get("linkedSum") or 0),
                        "agent_id": agent,
                        "organization_account_id": _id(pay.get("organizationAccount"))}
                target = meta.get("href", "").split("/")[-1].split("?")[0]
                if meta.get("type") == "customerorder":
                    by_order[target].append(item)
                elif meta.get("type") == "demand":
                    by_demand[target].append(item)
                elif meta.get("type") == "invoiceout":
                    invoice_payments.append((target, item))
    invoices = {}
    for invoice_id, item in invoice_payments:
        if invoice_id not in invoices:
            invoices[invoice_id] = _id(reader.get("/entity/invoiceout/" + invoice_id)
                                       .get("customerOrder"))
        if invoices[invoice_id]:
            by_order[invoices[invoice_id]].append(item)
    return by_order, by_demand


def build(reader=None, refresh_cost_months=(), refresh_agents=False):
    reader = reader or Reader(budget_seconds=1800, page_size=20)
    today = date.today()
    months = [f"{year}-{month:02d}" for year in range(2026, today.year + 1)
              for month in range(1, 13) if date(year, month, 1) <= today]
    shipments = []
    for month in months:
        path = RAW / f"{month}.json"
        if not path.exists():
            raise FileNotFoundError(f"missing scanned month {month}")
        shipments.extend(json.loads(path.read_text(encoding="utf-8")))
    for shipment in shipments:
        # Expanded customerOrder.meta.href may end with '?expand=state'.
        shipment["order_id"] = shipment["order_id"].split("?")[0]
    if len({x["id"] for x in shipments}) != len(shipments):
        raise ValueError("duplicate demand in monthly raw files")
    agents = json.loads(AGENTS.read_text()) if AGENTS.exists() else {}
    for agent_id in {agent_id for x in shipments
                     for agent_id in (x["agent_id"], x.get("order_agent_id")) if agent_id}:
        if agent_id in agents and not refresh_agents:
            continue
        item = reader.get("/entity/counterparty/" + agent_id)
        agents[agent_id] = {"name": item.get("name"), "type": item.get("companyType")}
    _atomic(AGENTS, agents)
    costs = json.loads(COSTS.read_text()) if COSTS.exists() else {}
    if OUT.exists():
        costs.update({x["demand_id"]: x["cogs"]
                      for x in json.loads(OUT.read_text()).get("rows", [])})
    eligible = [x for x in shipments if agents[x["agent_id"]]["type"] in ("legal", "entrepreneur")
                or x.get("order_agent_id") and agents[x["order_agent_id"]]["type"] in ("legal", "entrepreneur")]
    pay_by_order, pay_by_demand = _payments(reader, {agent_id for x in eligible
                                                    for agent_id in (x["agent_id"], x.get("order_agent_id"))
                                                    if agent_id})
    shipments = [x for x in eligible if agents[x["agent_id"]]["type"] in ("legal", "entrepreneur")
                 or x.get("order_agent_id") and
                    any(p.get("agent_id") == x["order_agent_id"]
                        for p in pay_by_order.get(x["order_id"], []))]
    confirmed = []
    for shipment in shipments:
        if shipment.get("order_status") == EXCHANGE_STATUS:
            order = reader.get("/entity/customerorder/" + shipment["order_id"] + "?expand=state")
            if (order.get("state") or {}).get("name") not in REPORT_STATES:
                continue
            linked = {p["payment_id"]: p for p in
                      (pay_by_demand.get(shipment["id"], []) +
                       pay_by_order.get(shipment["order_id"], []))}
            paid_sum = sum((Decimal(p["amount"]) for p in linked.values()), Decimal(0))
            if paid_sum != Decimal(_money(shipment["sum_kopecks"])):
                continue
        confirmed.append(shipment)
    shipments = confirmed
    order_counts = Counter(x["order_id"] for x in shipments)
    result = []
    short_details = None
    for shipment in shipments:
        did, oid = shipment["id"], shipment["order_id"]
        if did == SHORT_SHIPMENT["primary_demand_id"]:
            short_details = _short_shipment_details(reader)
            if Decimal(str(shipment["sum_kopecks"])) != Decimal(str(short_details["sale_kopecks"])):
                raise MsReadError("56856: сумма в сырье не совпала с МС")
            cogs = short_details["cogs"]
            costs[did] = cogs
            _atomic(COSTS, costs)
        else:
            cogs = None if shipment["month"] in refresh_cost_months else costs.get(did)
        if cogs is None:
            cogs = _fifo(reader, did)
            costs[did] = cogs
            _atomic(COSTS, costs)
        pays = list(pay_by_demand.get(did, []))
        seen_payment_ids = {p["payment_id"] for p in pays}
        pays.extend(p for p in pay_by_order.get(oid, []) if p["payment_id"] not in seen_payment_ids)
        # Один заказ может иметь несколько отгрузок: нельзя приписывать каждой всю оплату.
        paid = (str(sum((Decimal(p["amount"]) for p in pays), Decimal(0)))
                if order_counts[oid] == 1 or not pay_by_order.get(oid) else None)
        result.append({"demand_id": did, "demand_name": shipment["name"],
                       "order_id": oid, "order_name": shipment["order_name"],
                       "date": shipment["moment"][:10], "month": shipment["month"],
                       "project": PROJECTS.get(shipment["demand_project_id"])
                                  or PROJECTS[shipment["order_project_id"]],
                       "agent": agents[shipment["order_agent_id"]]["name"]
                                if agents[shipment["agent_id"]]["type"] == "individual"
                                and shipment.get("order_agent_id") else agents[shipment["agent_id"]]["name"],
                       "agent_type": agents[shipment["order_agent_id"]]["type"]
                                     if agents[shipment["agent_id"]]["type"] == "individual"
                                     and shipment.get("order_agent_id") else agents[shipment["agent_id"]]["type"],
                       "demand_agent_discrepancy": agents[shipment["agent_id"]]["name"]
                                                   if shipment.get("order_agent_id")
                                                   and shipment["order_agent_id"] != shipment["agent_id"]
                                                   else None,
                       "order_status": shipment.get("order_status") or "Доставлен",
                       "sale": _money(shipment["sum_kopecks"]), "paid": paid,
                       "cogs": cogs, "payments": pays,
                       "completion_demand_name": "56856.1" if short_details and
                                                  did == SHORT_SHIPMENT["primary_demand_id"] else None,
                       "completion_demand_id": SHORT_SHIPMENT["completion_demand_id"] if short_details and
                                                 did == SHORT_SHIPMENT["primary_demand_id"] else None,
                       "completion_date": short_details["completion_date"] if short_details and
                                          did == SHORT_SHIPMENT["primary_demand_id"] else None})
    result.sort(key=lambda x: (x["date"], x["order_name"]))
    _atomic(PAYMENTS, {"orders": dict(pay_by_order), "demands": dict(pay_by_demand)})
    _atomic(OUT, {"year": 2026, "updated_at": date.today().isoformat(),
                  "scanned_months": months, "rows": result})
    return len(result)


def refresh():
    today = date.today()
    current = today.strftime("%Y-%m")
    scan(current, through=today)
    refresh_cost_months = {current}
    if today.day <= 5 and (today.year > 2026 or today.month > 1):
        previous = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        scan(previous)
        refresh_cost_months.add(previous)
    return build(refresh_cost_months=refresh_cost_months, refresh_agents=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("scan", "build", "refresh"))
    parser.add_argument("month", nargs="?")
    args = parser.parse_args()
    if args.action == "scan":
        if not args.month:
            parser.error("scan requires YYYY-MM")
        print(args.month, scan(args.month, through=date.today()))
    elif args.action == "build":
        print("legal shipments", build())
    else:
        print("legal shipments", refresh())


if __name__ == "__main__":
    main()
