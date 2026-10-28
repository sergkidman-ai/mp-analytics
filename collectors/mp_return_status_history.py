# поток: fin
"""Append observed return states from the existing return feed; no marketplace requests."""
import hashlib
import json
from datetime import datetime, timezone

from psycopg2.extras import Json, execute_values

from core import db


def status_fields(raw):
    """Keep every status/state field, including nested marketplace-specific substatuses."""
    found = {}

    def visit(value, path="", selected=False):
        if isinstance(value, dict):
            for key, item in value.items():
                name = str(key)
                child = f"{path}.{name}" if path else name
                key_status = any(word in name.lower() for word in ("status", "state"))
                if key_status and isinstance(item, (str, int, float, bool, type(None))):
                    found[child] = item
                elif isinstance(item, (dict, list)):
                    visit(item, child, selected or key_status)
                elif selected and isinstance(item, (str, int, float, bool, type(None))):
                    found[child] = item
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{path}[{index}]", selected)

    visit(raw)
    return found


def event(head, raw, observed_at, *, baseline=False):
    visual = raw.get("visual") or {}
    change = visual.get("change_moment")
    if not change:
        state = raw.get("state") or {}
        change = state.get("changed_at") if isinstance(state, dict) else None
    change = change or raw.get("updateDate") or raw.get("updatedAt")
    evidence = status_fields(raw)
    place = raw.get("place") or {}
    target = raw.get("target_place") or {}
    value = dict(
        platform=head["platform"], account=head["account"], return_id=str(head["return_id"]),
        source=head.get("source") or head["platform"], order_number=head.get("order_number"),
        return_type=head.get("return_type"), scheme=head.get("scheme"),
        status_raw=head.get("status_raw"), status_name=head.get("status_name"),
        stage=head.get("stage"), status_evidence=evidence,
        place_name=(place.get("name") if isinstance(place, dict) else None) or head.get("where_now"),
        target_name=(target.get("name") if isinstance(target, dict) else None) or head.get("pvz_name"),
        source_changed_at=str(change) if change else None,
        observed_at=observed_at, is_baseline=baseline,
    )
    fingerprint = {k: value[k] for k in (
        "source", "return_type", "scheme", "status_raw", "status_name", "stage",
        "status_evidence", "place_name", "target_name")}
    value["status_hash"] = hashlib.sha256(json.dumps(fingerprint, ensure_ascii=False,
        sort_keys=True, default=str).encode()).hexdigest()
    return value


def record(rows, *, observed_at=None, baseline=False):
    """Append only changed states. A later return to an earlier state is preserved."""
    if not rows:
        return 0
    observed_at = observed_at or datetime.now(timezone.utc)
    events = [event(head, raw, head.get('_observed_at') or observed_at, baseline=baseline)
              for head, _items, raw in rows]
    columns = tuple(events[0])
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('mp_return_status_events'))")
            cur.execute("""SELECT DISTINCT ON (platform,account,return_id)
              platform,account,return_id,status_hash FROM mp_return_status_events
              ORDER BY platform,account,return_id,id DESC""")
            latest = {(p,a,r): h for p,a,r,h in cur.fetchall()}
            changed = [x for x in events if latest.get((x['platform'],x['account'],x['return_id'])) != x['status_hash']]
            if changed:
                values = [tuple(Json(x[c]) if c == 'status_evidence' else x[c] for c in columns) for x in changed]
                execute_values(cur, "INSERT INTO mp_return_status_events (" + ",".join(columns) + ") VALUES %s ON CONFLICT DO NOTHING", values, page_size=500)
            return len(changed)


def backfill():
    """Seed one observed baseline per known return; history before this is unavailable."""
    rows = db.query("""SELECT r.*, raw.payload, raw.loaded_at FROM mp_returns r
      JOIN raw_mp_returns raw USING(platform,account,return_id)
      ORDER BY r.platform,r.account,r.return_id""")
    data = []
    for row in rows:
        head = {k:v for k,v in row.items() if k not in ('payload','loaded_at')}
        head['_observed_at'] = row['loaded_at']
        data.append((head, [], row['payload']))
    # The old raw table holds only the latest response. Never label it a historical transition.
    return record(data, observed_at=datetime.now(timezone.utc), baseline=True)


if __name__ == '__main__':
    print({'baseline_events': backfill()})
