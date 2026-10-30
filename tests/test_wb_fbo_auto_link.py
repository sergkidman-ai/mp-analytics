# поток: fin
"""Only a unique one-unit order may consume an observed one-unit stock movement."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from collectors.wb_fbo_auto_link import choose_matches

START = datetime(2026, 10, 30, 14, 5, tzinfo=timezone.utc)
END = START + timedelta(hours=2)
MOVE = dict(nm_id=192228701, previous_at=START, current_at=END,
            previous_quantity=Decimal(1), current_quantity=Decimal(0),
            previous_in_way_to_client=Decimal(0), current_in_way_to_client=Decimal(1))
ORDER = dict(id=1, created_at=START + timedelta(minutes=20),
             warehouse="Склад WB РФ", products=[{"sku": "192228701", "quantity": 1}])


def test_unique_one_unit_order_matches():
    assert choose_matches([MOVE], [ORDER]) == [(MOVE, ORDER)]


def test_another_order_for_same_sku_makes_interval_ambiguous():
    other = dict(ORDER, id=2, created_at=START + timedelta(minutes=40))
    assert choose_matches([MOVE], [ORDER, other]) == []


def test_invalid_or_multi_unit_competing_order_does_not_disappear():
    malformed = dict(ORDER, id=2, products=[{"sku": "192228701", "quantity": "?"}])
    assert choose_matches([MOVE], [ORDER, malformed]) == []
    multi = dict(ORDER, products=[{"sku": "192228701", "quantity": 2}])
    assert choose_matches([MOVE], [multi]) == []


def test_order_outside_interval_or_wrong_warehouse_is_not_linked():
    assert choose_matches([MOVE], [dict(ORDER, created_at=START)]) == []
    assert choose_matches([MOVE], [dict(ORDER, created_at=END + timedelta(seconds=1))]) == []
    assert choose_matches([MOVE], [dict(ORDER, warehouse="Другой склад")]) == []
