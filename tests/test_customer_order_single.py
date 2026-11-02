# поток: inv
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import customer_order_fresh_fulfillment_20261022 as m

BASE = 'https://api.moysklad.ru/api/remap/1.2/entity/'

def href(kind, ident):
    return {'meta': {'href': BASE + kind + '/' + ident, 'type': kind}}

def order(status='Подтвержден', count=1, reserve=1):
    product=dict(href('product','p1'), externalCode='code', article='SKU')
    pos = dict(href('product','p1'), id='line', quantity=1, reserve=reserve,
               price=10000, assortment=product)
    rows = [copy.deepcopy(pos) for _ in range(count)]
    return dict(id='o1', name='123-1-1', updated='now', applicable=True,
                state={'name':status}, store=href('store','s1'),
                organization=href('organization','org1'), agent=href('counterparty','a1'),
                meta={'href':BASE+'customerorder/o1'}, positions={'meta':{'size':count},'rows':rows},
                sum=10000, vatSum=0)

class SingleOrderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.folder=Path(self.tmp.name)
        self.order=order();self.stock=2;self.total_reserve=1;self.demands=[];self.matches=1
        self.get_patch=patch.object(m.b.ms_api,'get',side_effect=self.get);self.get_patch.start();self.addCleanup(self.get_patch.stop)
        self.bundle=patch('tools.customer_order_remaining_bundles_20261022.composition',return_value=({'code':1},False));self.bundle.start();self.addCleanup(self.bundle.stop)
        self.env=patch.dict('os.environ',{'CARTRIDGE_API_KEY':'fake'});self.env.start();self.addCleanup(self.env.stop)
    def get(self,path,params=None):
        if path=='/entity/customerorder':
            rows=[{'id':'o1','name':'123-1-1'}]*self.matches
            return {'meta':{'size':len(rows)},'rows':rows}
        if path=='/entity/customerorder/o1':return copy.deepcopy(self.order)
        if path=='/entity/demand':return {'meta':{'size':len(self.demands)},'rows':self.demands}
        if path=='/report/stock/all':return {'meta':{'size':1},'rows':[{'meta':{'href':BASE+'product/p1'},'stock':self.stock,'reserve':self.total_reserve}]}
        raise AssertionError(path)
    def test_wrong_status(self):
        self.order['state']['name']='Собирается'
        with self.assertRaisesRegex(m.SingleOrderBlocked,'wrong status'):m.single_preflight('123-1-1',self.folder)
    def test_multiple_sku(self):
        self.order=order(count=2)
        with self.assertRaisesRegex(m.SingleOrderBlocked,'one product'):m.single_preflight('123-1-1',self.folder)
    def test_insufficient_stock(self):
        self.stock=0
        with self.assertRaisesRegex(m.SingleOrderBlocked,'Insufficient'):m.single_preflight('123-1-1',self.folder)
    def test_own_reserve_counts_but_others_do_not(self):
        self.stock=1;self.total_reserve=1
        _, proof=m.single_preflight('123-1-1',self.folder)
        self.assertEqual(proof['available_to_this_order'],'1')
        self.total_reserve=2
        with self.assertRaisesRegex(m.SingleOrderBlocked,'Insufficient'):m.single_preflight('123-1-1',self.folder)
    def test_existing_demand(self):
        self.demands=[{'name':'123-1-1'}]
        with self.assertRaisesRegex(m.SingleOrderBlocked,'demand'):m.single_preflight('123-1-1',self.folder)
    def test_linked_demand(self):
        self.order['demands']=[href('demand','d1')]
        with self.assertRaisesRegex(m.SingleOrderBlocked,'already shipped'):m.single_preflight('123-1-1',self.folder)
    def test_ambiguous_exact_number(self):
        self.matches=2
        with self.assertRaisesRegex(m.SingleOrderBlocked,'count is not one'):m.single_preflight('123-1-1',self.folder)
    def test_change_between_check_and_apply_prevents_write(self):
        with patch.object(m,'single_folder',return_value=self.folder), patch.object(m.b,'one_write') as write:
            m.single_check('123-1-1')
            self.order['updated']='later'
            with self.assertRaisesRegex(m.SingleOrderBlocked,'changed since'):m.single_apply('123-1-1')
            write.assert_not_called()
    def test_change_between_two_apply_preflights_prevents_write(self):
        with patch.object(m,'single_folder',return_value=self.folder), patch.object(m.b,'one_write') as write:
            m.single_check('123-1-1')
            old=copy.deepcopy(self.order)
            new=copy.deepcopy(self.order);new['updated']='changed during preflight'
            with patch.object(m,'single_preflight',side_effect=[(old,{}),(new,{})]):
                with self.assertRaisesRegex(m.SingleOrderBlocked,'changed during preflight'):
                    m.single_apply('123-1-1')
            write.assert_not_called()

if __name__=='__main__':unittest.main()
