# поток: fin
"""Bounded, read-only sources. Caller holds the shipment worker's shared lock."""
from collections import defaultdict
from decimal import Decimal
import re
import time

from shipment_guard import config
from shipment_guard.ms import MSClient, SourceError
from shipment_guard.economics import number
from customer_guard.domain import product_id, quantities, fifo_cost

ORG = "f5bea6e0-c2ac-11ef-0a80-0221000adcc4"
OZON_AGENTS = ("b7e22828-0b7c-11ee-0a80-11480012ed47", "22240c7b-c88d-11ed-0a80-10e700065d1d")


class CustomerMS(MSClient):
    resource_prefixes = ("/context/employee", "/entity/customerorder", "/entity/demand",
                         "/entity/product", "/entity/enter", "/entity/store",
                         "/report/stock/byoperation")

    def __init__(self, *args, budget=60, **kwargs):
        super().__init__(*args, **kwargs)
        self.interval = max(2.1, self.interval)
        self.budget = budget
        self.receipts = {}
        self.remote = None
        self.evidence = []
        self.deadline = self.clock() + 240

    def get(self, path, params=None):
        if self.requests >= self.budget or self.clock() >= self.deadline:
            raise SourceError("ms_request_budget", 600)
        value = super().get(path, params)
        self.evidence.append(dict(path=path, params=params, response=value))
        return value

    def verify_identity(self):
        if self.get("/context/employee").get("uid") != "yunusov@dsquare":
            raise SourceError("unexpected_ms_user", 3600)

    def collection(self, path, params=None, max_pages=10):
        result = []
        for page in range(max_pages):
            obj = self.get(path, {**(params or {}), "limit": 20, "offset": page * 20})
            rows, size = obj.get("rows"), obj.get("meta", {}).get("size")
            if not isinstance(rows, list) or not isinstance(size, int):
                raise SourceError("ms_collection_schema")
            result.extend(rows)
            if len(result) == size:
                return result
            if not rows or len(result) > size:
                raise SourceError("ms_collection_changed")
        raise SourceError("ms_page_budget", 600)

    def find_order(self, number_):
        if not re.fullmatch(r"\d+-\d+-\d+", number_):
            raise SourceError("unsupported_posting_number")
        flt = f"organization={config.MS}/entity/organization/{ORG};name={number_}"
        flt += "".join(f";agent={config.MS}/entity/counterparty/{a}" for a in OZON_AGENTS)
        rows = self.collection("/entity/customerorder",
                               {"filter": flt, "expand": "state,store,agent"}, max_pages=1)
        if any(x.get("name") != number_ or config.identity(x)[:2] != ("ozon", "oz_acc2") for x in rows):
            raise SourceError("ms_filter_mismatch")
        if len(rows) > 1:
            raise SourceError("ms_ambiguous_order")
        return rows[0] if rows else None

    def positions(self, entity, uid):
        return self.collection(f"/entity/{entity}/{uid}/positions", {"expand": "assortment"})

    def fifo(self, entity):
        return self.collection("/report/stock/byoperation", {"operation.id": entity})

    def supplier_cost(self, positions):
        if self.remote is None:
            stores = self.collection("/entity/store", {"filter": "name=Удаленный"}, max_pages=1)
            if len(stores) != 1:
                raise SourceError("remote_store_ambiguous")
            self.remote = stores[0]["meta"]["href"]
        cost, sources = Decimal(0), []
        for pid, qty in quantities(positions).items():
            product = self.get(f"/entity/product/{pid}", {"expand": "supplier"})
            if not product.get("article") or not product.get("supplier", {}).get("meta", {}).get("href"):
                raise ValueError("supplier_identity_missing")
            href = f"{config.MS}/entity/product/{pid}"
            enters = self.collection("/entity/enter", {"filter":
                f"store={self.remote};assortment={href};applicable=true",
                "expand": "store,rate.currency"}, max_pages=1)
            if len(enters) != 1:
                raise ValueError("supplier_receipt_not_unique")
            enter = enters[0]
            currency = enter.get("rate", {}).get("currency", {})
            if str(currency.get("code")) != "643":
                raise ValueError("supplier_currency_unverified")
            if enter.get("overhead", {}).get("sum", 0):
                raise ValueError("supplier_overhead_unverified")
            key = (enter["id"], enter.get("updated"))
            if key not in self.receipts:
                self.receipts[key] = self.positions("enter", enter["id"])
            matches = [p for p in self.receipts[key] if product_id(p) == pid]
            if len(matches) != 1:
                raise ValueError("supplier_price_not_unique")
            line = matches[0]
            unit = number(line.get("price")) / 100
            # Explicitly handle VAT; do not silently assume an exclusive price.
            if enter.get("vatEnabled") and not enter.get("vatIncluded") and line.get("vatEnabled") is not False:
                unit *= 1 + number(line.get("vat")) / 100
            if unit <= 0:
                raise ValueError("supplier_price_missing")
            cost += qty * unit
            sources.append(dict(product_id=pid, supplier=product["supplier"]["meta"]["href"],
                article=product["article"], enter_id=enter["id"], position_id=line["id"],
                version=enter.get("updated"), quantity=str(qty), unit=str(unit), currency="RUB"))
        return cost, sources

    def cost(self, order, positions):
        demands = order.get("demands", [])
        if demands:
            actual, covered, refs = Decimal(0), defaultdict(Decimal), []
            if len(demands) > 5:
                raise ValueError("too_many_demands")
            seen = set()
            for ref in demands:
                href = ref.get("meta", {}).get("href", "")
                if not re.fullmatch(re.escape(config.MS) + r"/entity/demand/[0-9a-f-]{36}", href):
                    raise ValueError("invalid_demand_link")
                uid = href.rsplit("/", 1)[-1]
                if uid in seen:
                    raise ValueError("duplicate_demand_link")
                seen.add(uid)
                demand = self.get(f"/entity/demand/{uid}")
                if not demand.get("applicable"):
                    continue
                if config.identity(demand)[:2] != ("ozon", "oz_acc2"):
                    raise ValueError("demand_account_mismatch")
                dp = self.positions("demand", uid)
                for pid, qty in quantities(dp).items():
                    covered[pid] += qty
                actual += fifo_cost(dp, self.fifo(uid))
                refs.append(dict(entity_id=uid, version=demand.get("updated")))
            if dict(covered) != quantities(positions):
                raise ValueError("partial_demand_coverage")
            return actual, "actual", refs
        state = order.get("state", {}).get("name")
        if state == "Неукомплектован":
            cost, sources = self.supplier_cost(positions)
            return cost, "purchase_estimate", sources
        if state == "Подтвержден":
            return fifo_cost(positions, self.fifo(order["id"])), "fifo_estimate", []
        raise ValueError("no_confirmed_cost")


def get_posting(number_):
    from collectors.ozon import _headers
    from collectors.ozon_postings import request_page
    result = request_page("https://api-seller.ozon.ru/v3/posting/fbs/get",
                          _headers("oz_acc2"), {"posting_number": number_},
                          deadline=time.monotonic() + 45)
    post = result.get("result", {})
    if post.get("posting_number") != number_:
        raise ValueError("posting_identity_mismatch")
    return post
