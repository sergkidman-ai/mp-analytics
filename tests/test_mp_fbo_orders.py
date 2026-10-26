# поток: fin
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from core import db
from collectors.mp_fbo_orders import normalized_ozon, normalized_wb, record_orders
from tests.test_customer_guard import pg  # isolated customer-guard schema
from web.customer_guard_api import data as customer_orders_data

NOW = datetime(2026,10,26,12,tzinfo=timezone.utc)


@pytest.fixture
def fbo_schema(pg):
    db.execute((Path(__file__).parents[1]/'migrations/079_mp_fbo_orders.sql').read_text())
    yield


def test_ozon_fbo_preserves_market_price_without_ms_jobs(fbo_schema):
    post=dict(posting_number='FBO-1',order_number='O-1',status='delivered',
              created_at='2026-10-26T09:00:00Z',in_process_at='2026-10-26T09:01:00Z',
              products=[dict(offer_id='0001wb',sku=123,quantity=2,price='125.50')])
    row=normalized_ozon(post,'oz_acc1',NOW)
    assert str(row['mp_price'])=='251.00'
    result=record_orders('ozon/oz_acc1/fbo',[row],NOW,NOW,NOW,NOW,1)
    assert result['changed_rows']==1
    assert record_orders('ozon/oz_acc1/fbo',[row],NOW,NOW,NOW,NOW,1)['changed_rows']==0
    assert db.query('SELECT count(*) n FROM mp_fbo_order_events')[0]['n']==1
    assert db.query('SELECT count(*) n FROM customer_guard_jobs')[0]['n']==0


def test_wb_scheme_requires_statistics_proof_and_status_updates(fbo_schema):
    stat=dict(srid='wb-srid-1',nmId=123,warehouseType='Склад WB',
              date='2026-10-25T10:00:00',supplierArticle='0001wb',
              priceWithDisc=250,warehouseName='Склад WB',isCancel=False)
    feed=dict(srid='wb-srid-1',status='created',updatedAt='2026-10-25T11:00:00+03:00',
              sellerPrice=260,warehouseName='Склад WB')
    row=normalized_wb(stat,feed,'wb_acc1',NOW)
    assert row['mp_status']=='created' and row['source_evidence']['warehouseType']=='Склад WB'
    assert row['mp_price']==260
    record_orders('wb/wb_acc1/fbo',[row],NOW,NOW,NOW,NOW,1)
    feed['status']='return'
    feed['updatedAt']='2026-10-26T12:00:00+03:00'
    row=normalized_wb(stat,feed,'wb_acc1',NOW)
    record_orders('wb/wb_acc1/fbo',[row],NOW,NOW,NOW,NOW,1)
    assert db.query('SELECT mp_status FROM mp_fbo_orders')[0]['mp_status']=='returned'
    assert db.query('SELECT count(*) n FROM mp_fbo_order_events')[0]['n']==2
    with pytest.raises(ValueError,match='not_wb_fbo'):
        normalized_wb(dict(stat,warehouseType='Склад продавца'),feed,'wb_acc1',NOW)
    old_buyout=dict(saleID='S123',date='2026-10-26T13:00:00',warehouseType='Склад WB')
    assert normalized_wb(stat,None,'wb_acc1',NOW,old_buyout)['mp_status']=='delivered'
    later_return=dict(saleID='R123',date='2026-10-27T13:00:00',warehouseType='Склад WB')
    assert normalized_wb(stat,feed,'wb_acc1',NOW,later_return)['mp_status']=='returned'
    assert db.query('SELECT count(*) n FROM customer_guard_jobs')[0]['n']==0


def test_fbo_list_filter_never_classifies_unknown_cost_as_loss(fbo_schema):
    post=dict(posting_number='FBO-2',status='delivered',created_at='2026-10-26T09:00:00Z',
              products=[dict(offer_id='0001wb',sku=123,quantity=1,price='125.50')])
    row=normalized_ozon(post,'oz_acc1',NOW)
    record_orders('ozon/oz_acc1/fbo',[row],NOW,NOW,NOW,NOW,1)
    def listing(**overrides):
        args=dict(scheme='fbo',status='',signal='',search='',product_code='0001',
                  account='',limit=50,offset=0)
        return customer_orders_data(**dict(args,**overrides))
    result=listing()
    assert result['total']==1 and result['rows'][0]['scheme']=='fbo'
    assert result['rows'][0]['result'] is None
    assert listing(signal='non_positive_profit')['total']==0
    assert listing(scheme='fbs')['total']==0
