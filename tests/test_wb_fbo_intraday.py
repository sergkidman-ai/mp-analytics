# поток: fin
"""A partial or duplicated WB response must never become a trusted stock snapshot."""
import pytest

from collectors.wb_fbo_intraday import normalize


ROW = dict(nmId=192228701, chrtId=4362195, warehouseId=-999999,
           warehouseName="Склад WB", regionName="Склад WB",
           quantity=0, inWayToClient=1, inWayFromClient=0)


def test_complete_zero_stock_and_transit_are_kept():
    row = normalize([ROW])[0]
    assert row[:8] == (192228701, 4362195, -999999, "Склад WB",
                       "Склад WB", 0, 1, 0)


@pytest.mark.parametrize("bad", [
    {k: v for k, v in ROW.items() if k != "inWayToClient"},
    dict(ROW, quantity=-1),
    dict(ROW, chrtId=None),
])
def test_incomplete_rows_are_rejected(bad):
    with pytest.raises(ValueError):
        normalize([bad])


def test_duplicate_warehouse_size_is_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        normalize([ROW, ROW])
