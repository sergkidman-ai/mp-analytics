# поток: card — self-check SQL-путей дожимателя на синтетической карточке (без площадки)
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/opt/mp-analytics")
sys.path.insert(0, ".")
from core.db import execute, query               # noqa: E402
import tools.ozon_card_push as P                 # noqa: E402

ACC = "oz_acc1"
OID, OID_W = "__selftest__", "__selftest_w__"
CLS = ("A", "W")
try:
    execute("INSERT INTO card_status (platform, account, offer_id, product_id, err_class, "
            "status_failed, is_selling) VALUES ('ozon', %s, %s, 1, 'A', 'imported', false) "
            "ON CONFLICT (platform, account, offer_id) DO NOTHING", (ACC, OID))
    execute("INSERT INTO card_status (platform, account, offer_id, product_id, err_class, "
            "err_codes, is_selling) VALUES ('ozon', %s, %s, 2, 'W', 'pics_http_error', true) "
            "ON CONFLICT (platform, account, offer_id) DO NOTHING", (ACC, OID_W))

    pool = P._pool(ACC, 5000, CLS)
    assert any(c["offer_id"] == OID for c in pool), "синтетическая A не попала в пул"
    assert any(c["offer_id"] == OID_W for c in pool), "синтетическая W не попала в пул"
    # порядок классов: сломанные (A) идут раньше торгующих с замечанием (W)
    order = [c["err_class"] for c in pool]
    assert order == sorted(order), "пул отдаёт классы не по порядку A → W"
    # ограничение классов на входе работает
    assert not any(c["offer_id"] == OID_W for c in P._pool(ACC, 5000, ("A",))), \
        "класс W просочился в пул при --classes A"

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
    assert not any(c["offer_id"] == OID for c in P._pool(ACC, 5000, CLS)), \
        "вылеченная A осталась в пуле"

    # W закрывается иначе: строка остаётся открытой (на карточке могут висеть другие
    # замечания, которые повтором не лечатся), но класс меняется и из пула она уходит.
    P._mark_warn_cleared(ACC, OID_W)
    row = query("SELECT is_open, err_class, err_codes, healed_at FROM card_status "
                "WHERE offer_id = %s", (OID_W,))[0]
    assert row["is_open"] is True, "строку W закрывать нельзя: другие замечания могли остаться"
    assert row["err_class"] == "O" and row["err_codes"] == "" and row["healed_at"] is not None
    assert not any(c["offer_id"] == OID_W for c in P._pool(ACC, 5000, CLS)), \
        "вылеченная W осталась в пуле"

    now = datetime.now(timezone.utc)
    assert P._too_fresh({"status_updated_at": (now - timedelta(minutes=1)).isoformat()}) is True
    assert P._too_fresh({"status_updated_at": (now - timedelta(days=30)).isoformat()}) is False
    assert P._too_fresh({}) is False
    print("SQL-пути дожимателя: OK (пул A+W → журнал → попытка → лечение → выход из пула)")
finally:
    execute("DELETE FROM card_push_log WHERE offer_id = ANY(%s)", ([OID, OID_W],))
    execute("DELETE FROM card_status WHERE offer_id = ANY(%s)", ([OID, OID_W],))
    print("синтетические строки убраны")
