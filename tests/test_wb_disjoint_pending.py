# поток: mkt
"""An explicit disjoint run cannot retry pending products or clear their barrier."""
from contextlib import nullcontext
from unittest.mock import Mock
import json
import pytest
from core.wb_auto_journal import Journal
from ops import wb_auto_entry as runner
import ops.promo_ordinary_scope as scope

@pytest.fixture
def writer(tmp_path,monkeypatch):
 monkeypatch.setattr(runner,'ROOT',tmp_path)
 monkeypatch.setattr(runner,'barrier',lambda account:tmp_path/(account+'.pending'))
 monkeypatch.setattr(runner,'writer_lock',nullcontext)
 monkeypatch.setattr(runner.time,'sleep',lambda *a:None)
 monkeypatch.setattr(scope,'ordinary_codes',lambda:{'0001'})
 monkeypatch.setattr(scope,'verified_set_codes',lambda:set())
 j=Journal(tmp_path/'journal.sqlite3')
 old=j.intent('wb_acc1',[{'nm_id':1,'base':1000,'discount':20,'target':'800','vendor':'0001'}])
 before={1:(2500,60,1000,'0001'),2:(2500,60,1000,'0001')}
 after={**before,2:(2500,56,1100,'0001')}
 c=Mock();c.goods.side_effect=[(before,{}),(after,{})];c.remaining.return_value=120
 c.request.return_value=Mock(status_code=200,json=lambda:{'data':{'id':555}})
 c.get.side_effect=lambda path,params:{'data':{'status':3}} if path.endswith('/history/tasks') else {'data':{'historyGoods':[]}}
 monkeypatch.setattr(runner,'Client',lambda *a:c)
 monkeypatch.setattr(runner.guard,'cogs_map',lambda *a:{n:{'cogs':100,'branch':'remote'} for n in before})
 batch=[{'nm_id':2,'wave':'plan_raise','cap':1100,'send_price':2500,'send_disc':56,'price_before':2500,'target_price':1100}]
 return j,old,c,batch,tmp_path


def test_default_gate_still_blocks_every_pending_upload(writer):
 j,old,c,batch,p=writer
 ok,error=runner.legacy_push('wb_acc1',batch)
 assert not ok and 'Prior WB upload unresolved' in error
 c.request.assert_not_called()
 assert j.pending('wb_acc1')[0]['id']==old and runner.barrier('wb_acc1').exists()


def test_explicit_disjoint_run_verifies_only_new_write_and_preserves_old_barrier(writer):
 j,old,c,batch,p=writer
 original=j.pending('wb_acc1')[0]['payload']
 assert runner.legacy_push('wb_acc1',batch,allow_disjoint_pending=True)==(True,'')
 assert c.request.call_count==1
 assert c.request.call_args.kwargs['json']['data']==[{'nmID':2,'price':2500,'discount':56}]
 pending=j.pending('wb_acc1')
 assert len(pending)==1 and pending[0]['id']==old and pending[0]['payload']==original
 assert pending[0]['state']=='uncertain' and runner.barrier('wb_acc1').exists()
 new=j.conn.execute('SELECT state,terminal FROM uploads WHERE id!=?',(old,)).fetchone()
 assert tuple(new)==('verified',1)


def test_overlapping_product_never_reaches_post(writer):
 j,old,c,batch,p=writer;batch[0]['nm_id']=1
 ok,error=runner.legacy_push('wb_acc1',batch,allow_disjoint_pending=True)
 assert not ok and 'overlap' in error
 c.request.assert_not_called()
 assert runner.barrier('wb_acc1').exists()


@pytest.mark.parametrize('payload',[[],[{}],[{'nm_id':'1'}],[{'nm_id':0}],[{'nm_id':True}]])
def test_unknown_product_scope_fails_closed(writer,payload):
 j,old,c,batch,p=writer
 j.conn.execute('UPDATE uploads SET payload=? WHERE id=?',(json.dumps(payload),old));j.conn.commit()
 ok,error=runner.legacy_push('wb_acc1',batch,allow_disjoint_pending=True)
 assert not ok and ('scope' in error or 'identity' in error)
 c.request.assert_not_called()


def test_new_uncertain_post_is_not_retried_by_a_second_isolated_call(writer):
 j,old,c,batch,p=writer
 before={1:(2500,60,1000,'0001'),2:(2500,60,1000,'0001')}
 c.goods.side_effect=None;c.goods.return_value=(before,{})
 c.request.return_value=Mock(status_code=200,json=lambda:{'data':{}})
 ok,error=runner.legacy_push('wb_acc1',batch,allow_disjoint_pending=True)
 assert not ok and len(j.pending('wb_acc1'))==2 and c.request.call_count==1
 ok,error=runner.legacy_push('wb_acc1',batch,allow_disjoint_pending=True)
 assert not ok and 'overlap' in error and c.request.call_count==1
 assert runner.barrier('wb_acc1').exists()
