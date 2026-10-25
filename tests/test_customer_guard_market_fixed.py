# поток: fin
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import Mock
import pytest
from core import db
from customer_guard import domain, store, notifications, rules
from tests.test_customer_guard import pg, posting, ingest, NOW


def test_fixed_market_rule_independent_of_finance_and_calendar(monkeypatch):
    monkeypatch.setattr(rules.db,'query',Mock(side_effect=AssertionError('no financial DB read')))
    for day in (date(2026,10,22),date(2027,1,1)):
        r=rules.get_rule('ya_acc1',as_of=day)
        assert r['retention']==Decimal('.72') and r['valid_until']>=day
        assert r['base_from'] is None and r['base_to'] is None
    p=posting();p.update(account='ya_acc1',platform='yandex')
    value=domain.economics(p,Decimal(150),'actual',r,NOW,price_override='1000')
    assert value['floor']=='1250.00' and value['estimated_net']=='130.00'
    assert value['expense_basis_kind']=='fixed' and value['expense_retention']=='0.72'


@pytest.mark.parametrize('superseded',[False,True])
def test_saved_replay_keeps_revision_queue_and_source_times(pg,superseded):
    p=posting();p.update(account='ya_acc1',platform='yandex');ingest([p])
    at=NOW+timedelta(minutes=10);job=store.claim(at,account='ya_acc1')
    old=dict(cost='100',price='900',ms_price='900',units='1',cost_kind='actual',
             rule_version='old-monthly',estimated_net='200',floor='900',gap='0',signal='ok',
             allocation='single_market_line',positions=[dict(code='0001wb',quantity=1)],price_source='ms')
    assert store.finish(job,old,at)
    if superseded:
        p['products'][0]['quantity']=2
        with store.transaction() as cur:store.observe(cur,p,at+timedelta(minutes=1),at+timedelta(minutes=1))
    else:db.execute("UPDATE customer_guard_jobs SET state='pending',kind='hourly_ms_check'")
    before=db.query('SELECT * FROM customer_guard_jobs')[0]
    order=db.query('SELECT * FROM customer_guard_orders')[0]
    version=db.query('SELECT generation FROM customer_guard_checks')[0]['generation']
    r=rules.get_rule('ya_acc1')
    assert store.reprice_saved(r,at+timedelta(minutes=2))==1
    assert store.reprice_saved(r,at+timedelta(minutes=3))==0
    assert db.query('SELECT * FROM customer_guard_jobs')[0]==before
    after=db.query('SELECT * FROM customer_guard_orders')[0]
    assert all(after[k]==v for k,v in order.items() if k!='latest_check_id')
    c=db.query('SELECT * FROM customer_guard_checks WHERE id=%s',(after['latest_check_id'],))[0]
    assert c['generation']==version
    assert (c['generation']!=after['generation'])==superseded
    result=c['result'];assert result['floor']=='1071.43' and result['estimated_net']=='152.00'
    for k in ('cost','price','ms_price','units','positions','price_source'):assert result[k]==old[k]
    assert result['cost_observed_at']==order['ms_checked_at'].isoformat()
    assert db.query('SELECT count(*) n FROM customer_guard_incidents')[0]['n']==0


def test_notification_explains_fixed_expenses():
    text=notifications.message(dict(account='ya_acc1',platform='yandex',kind='non_positive_profit',
        posting_number='123',ms_status='Собирается',result=dict(price='100',cost='100',floor='1000',gap='-900',
        estimated_net='-72',rule_version='fixed72',signal='below_floor',cost_kind='actual',expense_basis_kind='fixed',expense_retention='0.72')))
    assert 'по фиксированной доле 72%' in text and 'по средней доле' not in text
