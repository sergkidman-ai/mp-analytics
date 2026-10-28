# поток: fin
"""Observe linked Market salesreturns in MS with the guard's dedicated token.

This collector reads only salesreturn lists, never order positions or costs.
The first run can bootstrap from a chosen date; routine runs scan recent
document moments. Historical returns predating scan coverage remain unknown.
"""
import argparse
from datetime import date, datetime, timedelta, timezone

from core import db
from shipment_guard import config
from shipment_guard.ms import MSClient

MARKET_AGENTS = tuple(uid for uid, platform in config.AGENTS.items() if platform == "yandex")


class MarketReturnClient(MSClient):
    resource_prefixes = ("/entity/salesreturn",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.interval = max(self.interval, 2.1)


def normalize(doc):
    """Only an explicit demand link can connect a return to a Market order."""
    if config.identity(doc)[0] != "yandex":
        return None
    _, account, _, _ = config.identity(doc)
    demand = doc.get("demand") or {}
    demand_id = (demand.get("meta") or {}).get("href", "").rsplit("/", 1)[-1]
    order_number = demand.get("name")
    agent_id = (doc.get("agent", {}).get("meta") or {}).get("href", "").rsplit("/", 1)[-1]
    store = (doc.get("store") or {}).get("name")
    if not (account and demand_id and order_number and agent_id):
        return None
    return dict(salesreturn_id=doc["id"], demand_id=demand_id,
                order_number=order_number, account=account, agent_id=agent_id,
                store_name=store, applicable=bool(doc.get("applicable")),
                moment=doc.get("moment"), ms_updated_at=doc.get("updated"),
                observed_at=datetime.now(timezone.utc))


def scan_filter(agent, since, last_success):
    href = f"{config.MS}/entity/counterparty/{agent}"
    if last_success:
        # MS timestamps are local and naive. A two-day overlap also absorbs clock skew.
        cutoff = (last_success - timedelta(days=2)).replace(tzinfo=None)
        return f"agent={href};updated>={cutoff.strftime('%Y-%m-%d %H:%M:%S')}"
    return f"agent={href};moment>={since.isoformat()} 00:00:00"


def collect(since=None, client=None):
    if isinstance(since, str):
        since = date.fromisoformat(since)
    client = client or MarketReturnClient()
    result = {}
    for agent in MARKET_AGENTS:
        previous = db.query("SELECT earliest_scanned,last_success_at FROM market_ms_return_scan WHERE agent_id=%s", (agent,))
        last_success = previous[0]['last_success_at'] if previous and since is None else None
        effective_since = since or (date.today() - timedelta(days=120))
        params = {"filter": scan_filter(agent, effective_since, last_success),
                  "expand": "demand,store", "order": "updated,desc"}
        rows = list(client.rows("/entity/salesreturn", params, limit=100))
        valid = [x for doc in rows if (x := normalize(doc)) is not None]
        if valid:
            db.upsert("market_ms_return_receipts", valid, ["salesreturn_id"])
        db.execute("""INSERT INTO market_ms_return_scan
          (agent_id,earliest_scanned,last_success_at,documents_seen)
          VALUES (%s,%s,now(),%s)
          ON CONFLICT(agent_id) DO UPDATE SET
            earliest_scanned=LEAST(market_ms_return_scan.earliest_scanned,EXCLUDED.earliest_scanned),
            last_success_at=EXCLUDED.last_success_at,
            documents_seen=EXCLUDED.documents_seen""", (agent, effective_since, len(rows)))
        result[agent] = dict(api_rows=len(rows), linked=len(valid))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", help="First receipt moment to scan, YYYY-MM-DD")
    args = parser.parse_args()
    result = collect(args.since)
    print({"agents": len(result), "api_rows": sum(x["api_rows"] for x in result.values()),
           "linked": sum(x["linked"] for x in result.values())})


if __name__ == "__main__":
    main()
