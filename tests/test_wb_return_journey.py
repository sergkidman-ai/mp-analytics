# поток: fin
"""A return route must remain unresolved when stock movement has competing units."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from collectors.wb_return_journey import (adjacent_pairs, choose_transit_candidates,
                                          choose_wb_receipt)

T0 = datetime(2026, 10, 30, 10, tzinfo=timezone.utc)


def snap(hours, transit, stock, source="intraday"):
    return dict(at=T0+timedelta(hours=hours),transit=Decimal(transit),
                stock=Decimal(stock),source_kind=source,capture=str(hours))


EVENT = dict(account="wb_acc1",sku="123",event_at=T0+timedelta(hours=1),
             order_kind="fbo",id=1,posting_number="one")


def candidate(before=0, after=1):
    return dict(account="wb_acc1",sku="123",source_kind="intraday",
                stage="in_transit_candidate",transit_before=Decimal(before),
                transit_after=Decimal(after),current_observed_at=T0+timedelta(hours=2))


def test_unique_negative_event_links_only_the_transit_rise():
    stock={"wb_acc1":{"123":[snap(0,0,0),snap(2,1,0)]}}
    assert len(choose_transit_candidates([EVENT],stock))==1
    other=dict(EVENT,id=2)
    assert choose_transit_candidates([EVENT,other],stock)==[]
    assert choose_transit_candidates([EVENT],{"wb_acc1":{"123":[snap(0,0,0),snap(2,0,0)]}})==[]


def test_wb_receipt_requires_the_only_transit_unit_and_stock_increase():
    stock=[snap(0,0,0),snap(2,1,0),snap(4,1,0),snap(6,0,1)]
    assert choose_wb_receipt(candidate(),stock,[EVENT],[candidate()])==(stock[2],stock[3])
    assert choose_wb_receipt(candidate(1,2),stock,[EVENT],[candidate(1,2)]) is None
    missing_stock=[snap(0,0,0),snap(2,1,0),snap(4,0,0)]
    assert choose_wb_receipt(candidate(),missing_stock,[EVENT],[candidate()]) is None


def test_second_return_or_intervening_transit_change_blocks_receipt():
    stock=[snap(0,0,0),snap(2,1,0),snap(4,2,0),snap(6,1,1),snap(8,0,2)]
    assert choose_wb_receipt(candidate(),stock,[EVENT],[candidate()]) is None
    clean=[snap(0,0,0),snap(2,1,0),snap(4,0,1)]
    other=dict(EVENT,id=2,event_at=T0+timedelta(hours=3))
    assert choose_wb_receipt(candidate(),clean,[EVENT,other],[candidate()]) is None


def test_different_snapshot_sources_are_not_compared():
    assert adjacent_pairs([snap(0,0,0,"daily"),snap(2,1,0,"intraday")])==[]
