# поток: fin
"""Pure calculations; missing evidence never becomes a successful check."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json

from shipment_guard.economics import number

ACTIVE = frozenset(("awaiting_packaging", "awaiting_deliver", "delivering"))
TERMINAL = frozenset(("cancelled", "delivered"))
READY = frozenset(("Неукомплектован", "Подтвержден", "Собирается"))


def utcnow():
    return datetime.now(timezone.utc)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def initial_due(first_seen):
    return first_seen + timedelta(minutes=10)


def posting_snapshot(post):
    # Store calculation inputs, never recipient/address/phone data.
    keys = ("posting_number", "order_id", "order_number", "status", "substatus",
            "in_process_at", "created_at", "shipment_date", "products")
    result = {key: post[key] for key in keys if key in post}
    if not isinstance(result.get("posting_number"), str) or not result["posting_number"]:
        raise ValueError("invalid_posting_number")
    if not isinstance(result.get("status"), str):
        raise ValueError("invalid_posting_status")
    return result


def sale(post):
    # Shared normalization owned by the financial collector, including v3/v4.
    from core.ozon_posting import product_price
    total = Decimal(0)
    units = Decimal(0)
    if not post.get("products"):
        raise ValueError("no_market_products")
    for product in post["products"]:
        qty, price = number(product.get("quantity")), product_price(product)
        if qty <= 0 or qty != qty.to_integral_value() or price <= 0:
            raise ValueError("invalid_market_price_quantity")
        total += price * qty
        units += qty
    return total, units


def posting_hash(post):
    from core.ozon_posting import product_price
    # Equal v3 scalar and v4 Money values describe the same business revision.
    return digest(dict(status=post["status"], substatus=post.get("substatus") or "",
        products=sorted([(str(p.get("offer_id")), str(p.get("sku")),
                          str(number(p["quantity"]).normalize()),
                          str(product_price(p).normalize()))
                         for p in post["products"]])))


def product_id(position):
    return position.get("assortment", {}).get("meta", {}).get("href", "").rsplit("/", 1)[-1]


def quantities(positions):
    result = defaultdict(Decimal)
    for p in positions:
        pid, qty = product_id(p), number(p.get("quantity"))
        if not pid or qty <= 0:
            raise ValueError("invalid_ms_quantity")
        result[pid] += qty
    if not result:
        raise ValueError("no_ms_positions")
    return dict(result)


def fifo_cost(positions, rows):
    expected = quantities(positions)
    costs = defaultdict(Decimal)
    for row in rows:
        for p in row.get("positions", []):
            pid = p.get("meta", {}).get("href", "").rsplit("/", 1)[-1]
            value = number(p.get("cost")) / 100
            if not pid or value <= 0:
                raise ValueError("incomplete_fifo")
            costs[pid] += value
    if set(costs) != set(expected):
        raise ValueError("incomplete_fifo")
    return sum(costs.values(), Decimal(0))


def composition(post, positions, resolver):
    """Explain TK's selected goods using cached BOMs; never choose replacements."""
    expected, selected = defaultdict(Decimal), defaultdict(Decimal)
    proof = []
    for side, rows, target in (("ozon", post["products"], expected), ("ms", positions, selected)):
        for row in rows:
            code = row.get("offer_id") if side == "ozon" else row.get("assortment", {}).get("externalCode")
            if not code:
                return dict(verified=False, note="Нет внешнего кода для проверки состава")
            bom, source = resolver(str(code))
            for component, count in bom.items():
                n = number(count)
                if n <= 0:
                    raise ValueError("invalid_bom_quantity")
                target[component] += number(row["quantity"]) * n
            proof.append(dict(side=side, code=code, source=source))
    return dict(verified=bool(expected) and dict(expected) == dict(selected), sources=proof,
                expected={k:str(v) for k,v in expected.items()}, selected={k:str(v) for k,v in selected.items()})


def economics(post, cost, kind, rule, now, allocation_verified=False):
    price, units = sale(post)
    result = dict(price=str(price), units=str(units), cost=str(cost), cost_kind=kind,
                  floor=None, gap=None, signal="unknown", allocation="order_total",
                  note="Проверен итог заказа; распределение по товарам не подтверждено")
    if not rule or not rule.get("enabled") or rule.get("account") != "oz_acc2":
        result["note"] = "Нет действующего правила пола"
        return result
    if (rule["valid_until"] < now.date() or rule["platform"] != "ozon"
            or str(rule["organization_id"]) != "f5bea6e0-c2ac-11ef-0a80-0221000adcc4"):
        result["note"] = "Правило пола просрочено или относится к другому кабинету"
        return result
    retention = number(rule["retention"])
    if not 0 <= retention < 1 or cost <= 0:
        raise ValueError("invalid_economics")
    floor = (cost + max(Decimal(300) * units, Decimal(".1") * cost)) / (1 - retention)
    result.update(floor=str(floor.quantize(Decimal(".01"))),
                  gap=str((price - floor).quantize(Decimal(".01"))),
                  rule_version=rule["version"],
                  signal="below_floor" if price < floor else "aggregate_only")
    if allocation_verified:
        result.update(allocation="single_market_line", note="Состав и количество подтверждены по справочнику ТК")
        if price >= floor:
            result["signal"] = "ok"
    return result
