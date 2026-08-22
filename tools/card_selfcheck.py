# поток: card — self-check SQL-путей дожимателя на синтетической карточке (без площадки)
import sys

sys.path.insert(0, "/opt/mp-analytics")
sys.path.insert(0, ".")
from core.db import execute, query               # noqa: E402
import tools.ozon_card_push as P                 # noqa: E402

OID, ACC = "__selftest__", "oz_acc1"
try:
    execute("INSERT INTO card_status (platform, account, offer_id, product_id, err_class, "
            "status_failed, is_selling) VALUES ('ozon', %s, %s, 1, 'A', 'imported', false) "
            "ON CONFLICT (platform, account, offer_id) DO NOTHING", (ACC, OID))

    pool = P._pool(ACC, 50)
    assert any(c["offer_id"] == OID for c in pool), "синтетическая не попала в пул"

    P._log({"platform": "ozon", "account": ACC, "offer_id": OID, "product_id": 1, "rung": 1,
            "attr_id": 9024, "http_code": 200, "task_id": 123, "task_status": "imported",
            "healed": None, "note": None})
    P._mark_attempt(ACC, OID, needs_human=False)
    assert query("SELECT attempts FROM card_status WHERE offer_id = %s", (OID,))[0]["attempts"] == 1

    execute("UPDATE card_push_log SET healed = %s WHERE id = (SELECT max(id) FROM card_push_log "
            "WHERE platform = %s AND account = %s AND offer_id = %s)", (True, "ozon", ACC, OID))
    assert query("SELECT healed FROM card_push_log WHERE offer_id = %s ORDER BY id DESC LIMIT 1",
                 (OID,))[0]["healed"] is True

    P._mark_healed(ACC, OID)
    row = query("SELECT is_open, healed_at FROM card_status WHERE offer_id = %s", (OID,))[0]
    assert row["is_open"] is False and row["healed_at"] is not None

    assert not any(c["offer_id"] == OID for c in P._pool(ACC, 50)), "вылеченная осталась в пуле"

    assert P._too_fresh({"status_updated_at": "2026-08-22T23:59:00Z"}) is True
    assert P._too_fresh({"status_updated_at": "2026-07-01T00:00:00Z"}) is False
    assert P._too_fresh({}) is False
    print("SQL-пути дожимателя: OK (пул → журнал → попытка → лечение → выход из пула)")
finally:
    execute("DELETE FROM card_push_log WHERE offer_id = %s", (OID,))
    execute("DELETE FROM card_status WHERE offer_id = %s", (OID,))
    print("синтетическая строка убрана")
