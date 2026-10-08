# поток: fin
import copy
from datetime import date, timedelta
from decimal import Decimal as D
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from collectors import yandex_finance_basis as b
from ops import price_quarantine as q, ya_promo_guard as guard
from tools.yandex_guard_finance_preview import classify


class FinanceBasisTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{'YANDEX_CAMPAIGN_ID_ACC1':'1,2'});self.env.start();self.addCleanup(self.env.stop)
        self.orders={'a':{'creationDate':'2026-08-15','payments':[
            {'id':'1','date':'2026-09-02','type':'PAYMENT','total':'1000.01'},
            {'id':'2','date':'2026-09-30','type':'REFUND','total':'100.00'},
            {'id':'3','date':'2026-10-01','type':'PAYMENT','total':'99'}]},
                     'b':{'creationDate':'2026-09-01','payments':[],
                          'commissions':[{'type':'AGENCY','actual':'.72'}]}}
        self.meta={'dateFrom':'2025-11-01','dateTo':'2026-10-08','complete':True,'campaigns':['1','2'],
                   'pages':{'1':{'complete':True},'2':{'complete':True}},'received_at':'2026-10-08T08:00:00+00:00'}
        self.services=[{'category':'commission','cost':'200.00','netting':'50.00'},
                       {'category':'boost_sales','cost':'10.00','netting':'20.00'},
                       {'category':'reviews','cost':'5.00','netting':'0.00'}]
        self.closure=[{'order_id':'a','transaction_date':'2026-09-02','category':'revenue','amount':'1000.01'}]

    def build(self,**kwargs):
        args=dict(month='2026-09',orders=self.orders,metadata=self.meta,services=self.services,
                  service_meta={'status':'DONE'},closure=self.closure,closure_meta={'status':'PENDING'},cogs='300')
        args.update(kwargs);return b.build(**args)[0]

    def test_partial_closure_complete_payments(self):
        x=self.build();self.assertTrue(x['price_usable']);self.assertTrue(x['accounting_provisional'])
        self.assertEqual(x['source'],'stats/orders_calendar_payments');self.assertEqual(x['lines']['revenue'],D('1000.01'))
        self.assertEqual(x['lines']['returns_sum'],D(100));self.assertEqual(x['reconciliation']['additional_payments'],1)

    def test_accrual_parent_promotion_agency_precision(self):
        x=self.build();self.assertEqual(x['expenses'],D('285.72'))
        self.assertEqual(x['lines']['promotion'],D(35));self.assertEqual(x['lines']['agency'],D('.72'))
        self.assertEqual(x['turnover'],D('1070.01'));self.assertEqual(x['retention'],D('285.72')/D('1070.01'))
        self.assertEqual(x['monthly_record']['promotion'],D(15))

    def test_payment_month_boundary_creation_date_irrelevant(self):
        r=b.payments(self.orders,'2026-09');self.assertEqual(len(r),2)
        self.assertEqual(sum(x['amount'] for x in r if x['type']=='PAYMENT'),D('1000.01'))

    def test_duplicate_payment_and_conflict(self):
        o=copy.deepcopy(self.orders);o['a']['payments'].append(o['a']['payments'][0].copy())
        self.assertEqual(len(b.payments(o,'2026-09')),2)
        o['a']['payments'][-1]['total']='10'
        with self.assertRaisesRegex(RuntimeError,'Conflicting'):b.payments(o,'2026-09')
        o=copy.deepcopy(self.orders);o['b']['payments']=[o['a']['payments'][0]]
        with self.assertRaisesRegex(RuntimeError,'reused'):b.payments(o,'2026-09')

    def test_incomplete_page_campaign_services_or_reconciliation(self):
        for key,value in [('complete',False),('campaigns',['1']),('missing_historical_orders',['old'])]:
            meta=dict(self.meta,**{key:value});self.assertFalse(self.build(metadata=meta)['price_usable'])
        self.assertFalse(self.build(service_meta={'status':'PENDING'})['price_usable'])
        closure=self.closure+[dict(self.closure[0],amount='5')]
        self.assertFalse(self.build(closure=closure)['price_usable'])

    def test_zero_revenue_is_distinct_from_missing(self):
        x=self.build(orders={},services=[],closure=[]);self.assertEqual(x['lines']['revenue'],D(0))
        self.assertIsNone(x['retention']);self.assertFalse(x['price_usable'])
        self.assertFalse(self.build(metadata={})['price_usable'])

    def test_closure_final_only_when_exact_and_request_known(self):
        closure=self.closure+[dict(self.closure[0],category='returns',amount='-100',transaction_date='2026-09-30')]
        x=self.build(closure=closure,closure_meta={'status':'DONE'});self.assertEqual(x['status'],'verified_closure')
        self.assertFalse(x['accounting_provisional'])
        x=self.build(closure=closure,closure_meta={'status':'DONE','seed_request_unverified':True})
        self.assertTrue(x['accounting_provisional'])

    def test_all_provisional_no_silent_fallback(self):
        with self.assertRaisesRegex(RuntimeError,'provisional'):
            q._last_closed({'months':['Aug','Sep'],'period_keys':['2026-08','2026-09'],'provisional':['2026-08','2026-09']})

    def test_active_explicit_yandex_month_and_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'active.json'
            with self.assertRaisesRegex(RuntimeError,'не опубликована'):b.active_basis(path)
            x=self.build();x['month']=(date.today().replace(day=1)-timedelta(days=1)).strftime('%Y-%m')
            b.save(path,x);self.assertEqual(b.active_basis(path)['account'],'ya_acc1')
            x['month']='2026-08';b.save(path,x)
            with self.assertRaisesRegex(RuntimeError,'месяцу'):b.active_basis(path)

    def test_repeat_build_and_atomic_backup(self):
        first=self.build();second=self.build();self.assertEqual(first,second)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'basis.json';b.save(path,first);b.save(path,second)
            self.assertEqual(b.load(path),b.load(path.with_suffix('.json.previous')))

    def test_pending_report_resumes_saved_job_without_generate(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory);b.save(folder/'test_job.json',{'reportId':'job','request':{},'requested_at':b.stamp()})
            with patch.object(b,'request',return_value={'status':'PENDING'}) as req:
                data,meta=b.report('test',{},folder,polls=1)
            self.assertIsNone(data);self.assertEqual(meta['status'],'PENDING');req.assert_called_once_with('GET','/reports/info/job')

    def test_catalog_entry_requires_real_price_cap_and_levels(self):
        r={'offer_id':'34749','cogs':'1138.50','status':'NOT_PARTICIPATING','mechanics':'DIRECT_DISCOUNT',
           'price':5559,'promo_price':None,'max_promo_price':4451,'levels':{'lightBestLevel':4451}}
        x=classify(r,D('.3299429237'));self.assertEqual(x['action'],'add');self.assertEqual(x['target'],D(4451))
        self.assertEqual(classify(dict(r,price=None),D('.3299429237'))['category'],'no_data')
        self.assertEqual(classify(dict(r,levels={}),D('.3299429237'))['action'],'none')
        self.assertEqual(classify(dict(r,offer_id='7200'),D('.3299429237'))['action'],'none')

    def test_targeted_monthly_preview_keeps_verified_payments_over_old_closure(self):
        from collections import defaultdict
        from collectors import yandex_monthly as monthly
        fin={'2026-08-01':defaultdict(float), '2026-09-01':defaultdict(float)}
        evidence=self.build()
        def query(sql,params=None):
            if 'SELECT month,cogs' in sql:
                return [{'month':date(2026,9,1),'cogs':D(300)}]
            return []
        with patch.object(monthly.db,'query',side_effect=query),patch.object(monthly.db,'upsert',side_effect=AssertionError('write')),\
             patch.object(monthly.db,'execute',side_effect=AssertionError('write')),\
             patch.object(monthly.yandex_closure,'closure_monthly',return_value={'2026-09':{'revenue':1,'returns':0}}),\
             patch.object(monthly.yandex_ad_contract,'monthly',return_value={}),\
             patch.object(b,'verified_month',return_value=evidence):
            a=monthly._write_finance(fin,{},'2026-09-01',until='2026-09-30',preview=True)
            c=monthly._write_finance(fin,{},'2026-09-01',until='2026-09-30',preview=True)
        self.assertEqual(a,c);self.assertEqual(len(a),1)
        self.assertEqual(a[0]['revenue'],D('1000.01'));self.assertEqual(a[0]['returns_sum'],D(100))
        self.assertEqual(a[0]['cogs'],D(300));self.assertEqual(fin['2026-08-01']['revenue'],0)

    def test_guard_exact_floor_and_locked_sku(self):
        r={'offer_id':'x','cogs':1000,'keep_ratio':D('.3299429237'),'promo_price':0,'price':5000,
           'max_promo_price':4500,'mechanics':'DIRECT_DISCOUNT'}
        x=guard.decide(r);self.assertEqual(x['mode'],'raise');self.assertEqual(x['new_price'],3941)
        r['offer_id']='7200';self.assertEqual(guard.decide(r)['mode'],'ok')

    def test_other_platforms_use_their_existing_lines_when_yandex_inactive(self):
        oz={'months':['Sep'],'period_keys':['2026-09'],'accounts':{'oz_acc1':{'lines':dict.fromkeys(
            ['commission','delivery','partners','fbo','promo','penalty','unclassified'],[10])|{'sales':[1000]}}}}
        wb={'months':['Aug'],'period_keys':['2026-08'],'accounts':{'wb_acc1':{'lines':dict.fromkeys(
            ['delivery','storage','acceptance','ads','points','penalty','other'],[0])|{'own_price':[1000],'to_pay':[800]}}}}
        with patch.object(q.json,'loads',side_effect=[oz,wb]),patch.object(Path,'read_text',return_value='{}'),\
             patch.object(b,'active_basis',side_effect=RuntimeError('Yandex inactive')):
            out,month=q.retention()
        self.assertEqual(out,{'oz_acc1':.07,'wb_acc1':.2});self.assertEqual(month,'Sep')

if __name__=='__main__':unittest.main()
