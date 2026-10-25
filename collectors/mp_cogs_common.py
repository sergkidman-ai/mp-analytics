# поток: fin
"""Bounded, read-only MS access and protected writes for the shipment COGS pages."""
import datetime as dt
import gzip
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo
from contextlib import contextmanager

import psycopg2.extras
from psycopg2 import sql

from core import db
from collectors import ms_demand_cogs as MDC


class MsReadError(RuntimeError):
    pass


@contextmanager
def account_lock(account):
    with db.get_conn() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(hashtext('mp_cogs_demand'),hashtext(%s))", (account,))
            if not cur.fetchone()[0]:
                raise RuntimeError(f"shipment COGS refresh already running for {account}")
        try:
            yield
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(hashtext('mp_cogs_demand'),hashtext(%s))", (account,))


class Reader:
    def __init__(self, budget_seconds=1200, page_size=20, max_pages=2000):
        if not 1 <= page_size <= 20 or max_pages < 1 or budget_seconds <= 0:
            raise ValueError("invalid MS read budget")
        self.deadline = time.monotonic() + budget_seconds
        self.page_size = page_size
        self.max_pages = max_pages
        self.last_request = 0.0
        self.requests = 0

    def _remaining(self):
        value = self.deadline - time.monotonic()
        if value <= 0:
            raise MsReadError("MS shipment read budget exhausted")
        return value

    def get(self, path):
        for attempt in range(5):
            delay = max(0, 0.25 - (time.monotonic() - self.last_request))
            if delay >= self._remaining():
                raise MsReadError("MS shipment read budget exhausted before request")
            time.sleep(delay)
            self.last_request = time.monotonic()
            request = urllib.request.Request(MDC.MS + path, headers={
                "Authorization": "Bearer " + MDC.TOK, "Accept-Encoding": "gzip"})
            self.requests += 1
            try:
                with urllib.request.urlopen(request, timeout=min(30, self._remaining())) as response:
                    body = response.read()
                    if response.headers.get("Content-Encoding") == "gzip":
                        body = gzip.decompress(body)
                    result = json.loads(body)
                self._remaining()
                if not isinstance(result, dict):
                    raise MsReadError("MS returned a non-object response")
                return result
            except urllib.error.HTTPError as error:
                if error.code not in {429, 500, 502, 503, 504} or attempt == 4:
                    raise MsReadError(f"MS shipment GET HTTP {error.code}") from None
                try:
                    wait = float(error.headers.get("X-Lognex-Retry-TimeInterval", "1000")) / 1000
                    wait = max(wait, float(error.headers.get("Retry-After", "0")))
                except (ValueError, TypeError):
                    wait = 2 ** attempt
                wait = max(1, wait, 2 ** attempt)
                if wait >= self._remaining():
                    raise MsReadError("MS retry exceeds shipment read budget") from None
                time.sleep(wait)
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                if attempt == 4:
                    raise MsReadError(f"MS shipment GET failed: {type(error).__name__}") from None
                wait = 2 ** attempt
                if wait >= self._remaining():
                    raise MsReadError("MS retry exceeds shipment read budget") from None
                time.sleep(wait)
        raise MsReadError("MS shipment retries exhausted")

    def rows(self, path, params=None):
        params = dict(params or {})
        offset, seen, expected = 0, set(), None
        for _ in range(self.max_pages):
            query = urllib.parse.urlencode({**params, "limit": self.page_size, "offset": offset})
            page = self.get(path + "?" + query)
            rows = page.get("rows")
            total = (page.get("meta") or {}).get("size")
            if not isinstance(rows, list) or isinstance(total, bool) or not isinstance(total, int):
                raise MsReadError("MS shipment page has no rows/size")
            if expected is None:
                expected = total
            elif expected != total:
                raise MsReadError("MS shipment list changed during pagination; retry next run")
            for row in rows:
                identity = row.get("id") or (row.get("meta") or {}).get("href")
                if not identity or identity in seen:
                    raise MsReadError("MS shipment page contains a missing/repeated identity")
                seen.add(identity)
                yield row
            offset += len(rows)
            if offset == total:
                return
            if not rows or offset > total:
                raise MsReadError("MS shipment pagination ended before advertised size")
        raise MsReadError("MS shipment page budget exhausted")

    def resolve(self, entity, name):
        rows = list(self.rows("/entity/" + entity, {"filter": "name=" + name}))
        if len(rows) != 1:
            raise MsReadError(f"MS {entity} name is missing/ambiguous")
        return rows[0]["meta"]["href"]

    def demands(self, org, agent, since, until):
        filters = f"organization={org};agent={agent};moment>={since};moment<{until}"
        out = []
        lower, upper = dt.datetime.fromisoformat(since), dt.datetime.fromisoformat(until)
        for row in self.rows("/entity/demand", {"filter": filters, "order": "moment,asc;id,asc"}):
            if not row.get("name") or not row.get("id") or not row.get("moment"):
                raise MsReadError("MS shipment has no name/id/moment")
            if not lower <= dt.datetime.fromisoformat(row["moment"]) < upper:
                raise MsReadError("MS shipment is outside the requested window")
            out.append({"name": row["name"], "id": row["id"],
                        "moment": row["moment"][:10], "sum": (row.get("sum") or 0) / 100})
        return out

    def fifo(self, demand_id):
        report = self.get("/report/stock/byoperation?operation.id=" + demand_id)
        positions = list(self.rows("/entity/demand/" + demand_id + "/positions"))
        quantities = {}
        for row in positions:
            identity = MDC._hid(((row.get("assortment") or {}).get("meta") or {}).get("href"))
            if not identity:
                raise MsReadError("MS shipment position has no assortment identity")
            quantities[identity] = quantities.get(identity, 0) + float(row.get("quantity") or 0)
        rows = report.get("rows")
        if not isinstance(rows, list):
            raise MsReadError("MS FIFO report has no rows")
        items = (rows[0].get("positions") or []) if rows else []
        costs = {MDC._hid((p.get("meta") or {}).get("href")): float(p.get("cost") or 0) / 100
                 for p in items}
        parts = [{"ms_id": key, "qty": qty, "cost": costs.get(key, 0)}
                 for key, qty in quantities.items()]
        return sum(costs.values()), sum(quantities.values()), parts


def open_demands(reader, org, agents, since, frozen, until=None):
    """Do not even list closed months; use a fixed upper bound while paging."""
    end = dt.datetime.fromisoformat(until) if until else dt.datetime.now(ZoneInfo("Europe/Moscow")).replace(tzinfo=None, microsecond=0)
    start = dt.datetime.fromisoformat(since)
    out, seen = [], set()
    while start < end:
        next_month = dt.datetime(start.year + (start.month == 12), start.month % 12 + 1, 1)
        stop = min(next_month, end)
        if start.strftime("%Y-%m") not in frozen:
            for agent in agents:
                href = reader.resolve("counterparty", agent)
                for demand in reader.demands(org, href, start.isoformat(" "), stop.isoformat(" ")):
                    if demand["id"] in seen:
                        raise MsReadError("duplicate MS shipment across agents/windows")
                    seen.add(demand["id"])
                    out.append(demand)
        start = stop
    names = [row["name"] for row in out]
    if len(set(names)) != len(names):
        raise MsReadError("duplicate shipment names: refusing lossy account/name upsert")
    return out


def impute_fifo(fallback, positions, day):
    """Use the existing dated item/kit FIFO chain when a supplier MS id has no FIFO."""
    value, method = fallback.impute(positions, day)
    if value:
        return value, method
    codes = {fallback.ms2ec.get(p["ms_id"]) for p in positions}
    codes.discard(None)
    if not codes:
        return None, None
    definitions = db.query("""SELECT external_code,n_components,components FROM set_cost
                              WHERE external_code=ANY(%s)""", (list(codes),))
    # Never accept an incomplete declaration or a parser that silently dropped components.
    valid = {r["external_code"] for r in definitions
             if isinstance(r["components"], list) and isinstance(r["n_components"], int) and r["n_components"] > 0
             and len(r["components"]) == r["n_components"]
             and fallback.sets.get(r["external_code"]) == r["components"]}
    total, sources = 0.0, set()
    for position in positions:
        code = fallback.ms2ec.get(position["ms_id"])
        unit = fallback.unit_ms(position["ms_id"], day)
        source = "tovar_fifo"
        if not unit and code:
            unit, source = fallback.unit(code, day)
            if source == "nabor_fifo" and code not in valid:
                unit = None
        if not unit:
            return None, None
        total += unit * float(position["qty"] or 0)
        sources.add(source)
    if total <= 0:
        return None, None
    return total, ("nabor_fifo" if "nabor_fifo" in sources else
                   "analog_fifo" if "analog_fifo" in sources else "tovar_fifo")


def save_records(table, records):
    """Recheck closure and manual overrides in the same transaction as the upsert."""
    if table not in {"oz_cogs_demand", "wb_cogs_demand"}:
        raise ValueError("unknown shipment COGS table")
    if not records:
        return 0
    frozen, manual = table.replace("_demand", "_frozen"), table.replace("_demand", "_manual")
    cols = ["account", "demand_name", "demand_id", "ym", "demand_date", "our_sum", "qty", "cogs", "method", "status", "status_raw"]
    values = [[row[k] for k in cols] for row in records]
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout='5s'")
            cur.execute("SET LOCAL statement_timeout='60s'")
            # freeze/unfreeze is an ordinary INSERT/DELETE: block it only for this short write.
            cur.execute(sql.SQL("LOCK TABLE {}, {} IN SHARE MODE").format(
                sql.Identifier(frozen), sql.Identifier(manual)))
            query = sql.SQL("""INSERT INTO {table} ({cols}, loaded_at)
                SELECT v.account,v.demand_name,v.demand_id,v.ym::date,v.demand_date::date,
                       v.our_sum::numeric,v.qty::numeric,coalesce(m.cogs,v.cogs::numeric),
                       CASE WHEN m.cogs IS NOT NULL THEN 'manual' ELSE v.method END,
                       v.status,v.status_raw,now()
                FROM (VALUES %s) v({cols})
                LEFT JOIN {manual} m ON m.account=v.account AND m.demand_name=v.demand_name
                WHERE NOT EXISTS (SELECT 1 FROM {frozen} f WHERE f.account=v.account AND f.ym=v.ym::date)
                  AND (v.method<>'manual' OR m.cogs IS NOT NULL)
                ON CONFLICT (account,demand_name) DO UPDATE SET
                demand_id=excluded.demand_id,ym=excluded.ym,demand_date=excluded.demand_date,
                our_sum=excluded.our_sum,qty=excluded.qty,cogs=excluded.cogs,method=excluded.method,
                status=excluded.status,status_raw=excluded.status_raw,loaded_at=excluded.loaded_at
                RETURNING demand_name""").format(table=sql.Identifier(table), manual=sql.Identifier(manual),
                    frozen=sql.Identifier(frozen), cols=sql.SQL(',').join(map(sql.Identifier,cols)))
            written = psycopg2.extras.execute_values(cur, query.as_string(conn), values, page_size=100, fetch=True)
    return len(written)
