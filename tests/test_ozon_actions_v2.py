# поток: mkt
import unittest
from datetime import date
from unittest.mock import Mock, patch
from ops import ozon_actions as api
from ops import ozon_promo_guard as guard
from ops import ozon_stock_action as stock


def product(pid=1, cap='100', actual='70'):
    return dict(id=pid, price={'amount':'200','currency':'RUB'},
                action_price={'amount':cap,'currency':'RUB'},
                max_action_price={'amount':'100','currency':'RUB'},
                marketplace_seller_price={'amount':actual,'currency':'RUB'})


class ProtocolTests(unittest.TestCase):
    def test_update_batches_and_does_not_infer_unlisted_success(self):
        req=Mock(return_value=dict(active_product_ids=[1],warnings=[]))
        r=api.update(req,'oz_acc1',5,[dict(product_id=i,action_price=90) for i in range(1,102)])
        self.assertEqual(req.call_count,2)
        self.assertEqual([len(c.args[3]['products']) for c in req.call_args_list],[100,1])
        self.assertEqual(r['active'],{1})
    def test_money_preserves_cents_and_rejects_unknown_values(self):
        self.assertEqual(api.money(1234.56),dict(amount='1234.56',currency='RUB'))
        for value in (None,{},'NaN','Infinity',-1):
            with self.subTest(value=value),self.assertRaises(api.ActionProtocolError):api.amount(value)

    def test_actual_price_is_not_action_limit_or_website_price(self):
        p=product();p['website_prices']={'price':{'amount':'10'}}
        result=api.product(p,True)
        self.assertEqual(result['action_price'],70)
        self.assertEqual(result['action_price_limit'],100)

    def test_missing_actual_price_fails_closed(self):
        p=product();p.pop('marketplace_seller_price')
        with self.assertRaises(api.ActionProtocolError):api.product(p,True)

    def test_flat_money_and_cursor_pagination_with_total_items(self):
        request=Mock(side_effect=[dict(products=[product(1)],last_id='cursor',total_items=2),
                                 dict(products=[product(2)],last_id='',total_items=2)])
        self.assertEqual(len(api.products(request,'oz_acc1',5)),2)
        self.assertEqual(request.call_args_list[1].args[3]['last_id'],'cursor')
        self.assertEqual(request.call_args_list[0].args[3]['limit'],100)
        self.assertNotIn('offset',request.call_args_list[0].args[3])

    def test_repeated_page_or_truncated_total_raises(self):
        for response in ([dict(products=[product()],last_id='x',total=2)]*2,
                         [dict(products=[product()],last_id='',total=2)]):
            with self.subTest(response=response),self.assertRaises(api.ActionProtocolError):
                api.products(Mock(side_effect=response),'oz_acc1',5)

    def test_candidates_keep_zero_action_limit(self):
        req=Mock(return_value=dict(products=[product(cap='0')],total=1,last_id=''))
        self.assertEqual(api.products(req,'oz_acc1',5,True)[0]['action_price'],0)
        self.assertEqual(req.call_args.args[2],'/v2/actions/candidates')

    def test_new_update_records_separate_outcomes_and_warnings(self):
        req=Mock(return_value=dict(active_product_ids=[1],deactivated_product_ids=[2],
            rejected=[dict(product_id=3,reason='too low')],warnings=[dict(product_id=1,message='warning')]))
        r=api.update(req,'oz_acc1',5,[dict(product_id=i,action_price=90.25) for i in (1,2,3)])
        self.assertEqual(r['active'],{1});self.assertEqual(r['inactive'],{2})
        self.assertEqual(r['rejected'],{3:'too low'});self.assertTrue(r['warnings'])
        self.assertEqual(req.call_args.args[2],'/v1/actions/products/update')
        self.assertEqual(req.call_args.args[3]['products'][0]['action_price']['amount'],'90.25')

    def test_before_switch_legacy_update_response_is_supported(self):
        req=Mock(return_value=dict(result=dict(product_ids=[1],rejected=[])))
        self.assertEqual(api.update(req,'oz_acc1',5,[dict(product_id=1,action_price=90)])['active'],{1})

    def test_stock_and_elastic_remove_via_price_above_ceiling(self):
        for kind in ('STOCK_DISCOUNT','ELASTIC_BOOSTING'):
            req=Mock(return_value=dict(deactivated_product_ids=[1]))
            r=api.remove(req,'oz_acc1',dict(id=5,action_type=kind),
                         [dict(product_id=1,max_price=100,floor_price=150.2)])
            self.assertEqual(r['inactive'],{1});self.assertEqual(req.call_count,1)
            self.assertEqual(req.call_args.args[3]['products'][0]['action_price']['amount'],'152.00')

    def test_stock_rejection_never_falls_back_to_promo_code_deactivate(self):
        req=Mock(return_value=dict(rejected=[dict(product_id=1,reason='price above ceiling')]))
        r=api.remove(req,'oz_acc1',dict(id=5,action_type='STOCK_DISCOUNT'),[dict(product_id=1,max_price=100)])
        self.assertEqual(r['inactive'],set())
        self.assertEqual(r['rejected'],{1:'price above ceiling'})
        self.assertEqual(req.call_count,1)
        self.assertEqual(req.call_args.args[2],'/v1/actions/products/update')

    def test_elastic_active_response_never_calls_deactivate(self):
        req=Mock(return_value=dict(active_product_ids=[1]))
        r=api.remove(req,'oz_acc1',dict(id=5,action_type='ELASTIC_BOOSTING'),
                     [dict(product_id=1,max_price=100)])
        self.assertEqual(r['active'],{1})
        self.assertEqual(r['inactive'],set())
        self.assertEqual(req.call_count,1)
        self.assertEqual(req.call_args.args[2],'/v1/actions/products/update')

    def test_promo_codes_use_dedicated_removal_unknown_mechanics_fail(self):
        req=Mock(return_value=dict(product_ids=[1]))
        self.assertEqual(api.remove(req,'oz_acc1',dict(id=5,action_type='PROMO_CODE'),
                                    [dict(product_id=1)])['inactive'],{1})
        self.assertEqual(req.call_args.args[2],'/v2/actions/products/deactivate')
        req.reset_mock()
        with self.assertRaises(api.ActionProtocolError):
            api.remove(req,'oz_acc1',dict(id=5,action_type='UNKNOWN'),[dict(product_id=1)])
        req.assert_not_called()


class GuardIntegrationTests(unittest.TestCase):
    def test_request_failure_does_not_trigger_blind_removal(self):
        row=self.row()
        with patch.object(stock,'update_products',side_effect=RuntimeError('unavailable')), \
             patch.object(stock,'remove_products') as remove:
            guard.execute('oz_acc1',[row])
        self.assertEqual(row['status'],'error');remove.assert_not_called()

    def test_transient_read_500_is_retried(self):
        bad=Mock(status_code=500,headers={})
        good=Mock(status_code=200);good.json.return_value={'result':[]}
        with patch.object(stock,'_headers',return_value={}), \
             patch.object(stock.requests,'get',side_effect=[bad,good]) as get, \
             patch.object(stock.time,'sleep'):
            self.assertEqual(stock.actions('oz_acc1'),[])
        self.assertEqual(get.call_count,2)
    def row(self):
        return dict(action_id=5,action_type='STOCK_DISCOUNT',product_id=1,mode='raise',
                    new_price=150,floor_price=150,max_price=200,stock=0,min_stock=0,
                    status='dry',reason='below floor',note=None)

    def test_actual_below_floor_after_accepted_update_is_removed_and_verified(self):
        row=self.row()
        with patch.object(stock,'update_products',return_value=dict(active={1},inactive=set(),rejected={},warnings=[])), \
             patch.object(stock,'remove_products',return_value=dict(inactive={1},rejected={},warnings=[])) as remove, \
             patch.object(stock,'participants',side_effect=[[dict(id=1,action_price=90)],[]]), \
             patch.object(guard.time,'sleep'):
            guard.execute('oz_acc1',[row])
        self.assertEqual((row['mode'],row['status']),('remove','confirmed'))
        remove.assert_called_once()

    def test_disappearance_during_update_counts_as_verified_removal(self):
        row=self.row()
        with patch.object(stock,'update_products',return_value=dict(active=set(),inactive={1},rejected={},warnings=[])), \
             patch.object(stock,'remove_products') as remove, \
             patch.object(stock,'participants',return_value=[]),patch.object(guard.time,'sleep'):
            guard.execute('oz_acc1',[row])
        self.assertEqual((row['mode'],row['status']),('remove','confirmed'));remove.assert_not_called()

    def test_removal_acceptance_is_not_confirmation_if_product_still_present(self):
        row=self.row();row['mode']='remove'
        with patch.object(stock,'remove_products',return_value=dict(inactive={1},rejected={},warnings=[])), \
             patch.object(stock,'participants',return_value=[dict(id=1,action_price=90)]),patch.object(guard.time,'sleep'):
            guard.execute('oz_acc1',[row])
        self.assertEqual(row['status'],'error')

    def test_stock_ladder_under_floor_update_excludes_product(self):
        row=dict(action_id=5,action_title='Акция для складов. Регион',action_type='STOCK_DISCOUNT',
            product_id=1,offer_id='3933X6',mode='update',price=160,floor=150,cap=200,
            stock=6,min_stock=1,rung=0,cogs=500,cogs_src='supplier')
        with patch.object(stock,'update_products',return_value=dict(active={1},inactive=set(),rejected={})), \
             patch.object(stock,'participants',side_effect=[[dict(id=1,action_price=90,max_action_price=200)],[]]), \
             patch.object(stock,'remove_products',return_value=dict(inactive={1})), \
             patch.object(stock,'_log'),patch.object(stock,'time'),patch.object(stock,'_remember') as remember:
            result=stock.apply_plan('oz_acc1',[row],dry=False,allow_remove=True)
        self.assertEqual(result,dict(add=0,update=0,remove=1));remember.assert_not_called()
        self.assertEqual(row['mode'],'remove')

    def test_stock_update_deactivation_is_not_counted_as_addition(self):
        row=dict(action_id=5,action_title='Распродажа стока',action_type='STOCK_DISCOUNT',
            product_id=1,offer_id='3933X6',mode='add',price=160,floor=150,cap=200,
            stock=6,min_stock=1,rung=0,cogs=500,cogs_src='supplier')
        with patch.object(stock,'update_products',return_value=dict(active=set(),inactive={1},rejected={})), \
             patch.object(stock,'participants',return_value=[]),patch.object(stock.time,'sleep'), \
             patch.object(stock,'remove_products') as remove,patch.object(stock,'_log'), \
             patch.object(stock,'_remember') as remember:
            result=stock.apply_plan('oz_acc1',[row],dry=False,allow_remove=True)
        self.assertEqual(result,dict(add=0,update=0,remove=1))
        remember.assert_not_called();remove.assert_not_called()


class PriceCollectorTests(unittest.TestCase):
    def test_live_v2_response_shape_is_used_for_membership(self):
        from prices import mp_price_promo as collector
        def response(body):
            r=Mock(status_code=200);r.json.return_value=body;return r
        p=product(1)
        get=Mock(side_effect=[
            response(dict(items=[dict(product_id=1,offer_id='0644X3',price=dict(price='200',old_price='300'))],cursor='')),
            response(dict(result=[dict(id=5,title='Акция',is_participating=True)])),
            response(dict(products=[p],total=1,last_id=''))])
        with patch.object(collector,'_oz_headers',return_value={}), \
             patch.object(collector.db,'query',return_value=[dict(offer_id='0644X3',sku=123)]), \
             patch.object(collector,'_get',get),patch.object(collector.time,'sleep'):
            rows,_=collector.ozon_snapshot('oz_acc1',{'0644X3'},date(2026,10,15))
        self.assertTrue(rows[0]['in_promo'])
        self.assertEqual(rows[0]['promo_names'],'Акция')
        self.assertTrue(get.call_args.args[0].endswith('/v2/actions/products'))
        self.assertEqual(get.call_args.kwargs['json']['limit'],100)
