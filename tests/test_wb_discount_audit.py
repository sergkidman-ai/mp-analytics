# поток: mkt
import unittest
import contextlib
import io
import json
import pathlib
import tempfile
from contextlib import ExitStack
from unittest.mock import patch
from ops import wb_promo_discount_audit as audit
from ops import wb_promo_guard as guard


def evidence(sets=None, rows=None, tc=None, links=None):
    return dict(sets=sets or {}, stock=rows or [],offers=[],tc=tc or [],links=links or {},cards={})


def offer(code,price,qty=4):
    return dict(external_code=code,stock=qty,buy_price=price,cost_seb=price,
                store='Удаленный склад',supplier='test',article='test',captured_at='2026-10-11')


class AuditTests(unittest.TestCase):
    def test_5213_accepts_confirmed_universal_offer_at_four(self):
        e=evidence(rows=[offer('5213',2896,6),offer('0614',928,4)],links={'5213':{'0614'}})
        r=audit.reconstruct('5213',1,e)
        self.assertEqual(r['cost'],928)
        self.assertEqual(r['parts'][0]['rows'][0]['resolved_code'],'0614')
        self.assertFalse(r['parts'][0]['quantity_short'])

    def test_three_units_are_excluded(self):
        e=evidence(rows=[offer('5213',2896,6),offer('0614',928,3)],links={'5213':{'0614'}})
        self.assertEqual(audit.reconstruct('5213',1,e)['cost'],2896)

    def test_bundle_supplier_shortage_is_flagged(self):
        e=evidence(rows=[offer('5213',928,4)])
        self.assertTrue(audit.reconstruct('5213',6,e)['parts'][0]['quantity_short'])

    def test_whole_set_price_protects_4297(self):
        e=evidence(sets={'4297':{'2860':1,'2861':1}},
                   rows=[offer('2860',1690),offer('2861',1357)],
                   tc=[dict(external_code='4297',buy_price=53689)])
        r=audit.reconstruct('4297',1,e)
        self.assertEqual(r['cost'],53689)
        self.assertEqual(r['component_total'],3047)

    def test_missing_component_does_not_create_partial_cost(self):
        e=evidence(sets={'4297':{'2860':1,'2861':1}},rows=[offer('2860',1690)])
        self.assertIsNone(audit.reconstruct('4297',1,e)['cost'])

    def test_quantity_validation_is_independent_and_conservative(self):
        self.assertEqual(audit.quantity('5213X6ABC','Комплект картриджей 6 шт'),6)
        self.assertEqual(audit.quantity('5213X6ABC','Картридж'),1)
        self.assertEqual(audit.quantity('5213X0',''),1)
        self.assertEqual(audit.quantity('5213X10',''),10)
        self.assertEqual(audit.quantity('5213X6ABC','Комплектность картриджей 6 шт'),1)
        self.assertEqual(audit.quantity('5213X6ABC','Комплект картриджей 6 штук'),1)

    def test_strictly_over_fifty_and_cost_discrepancy(self):
        e=evidence(rows=[offer('5213',928)],links={})
        e['cards']={1:dict(vendor_code='5213',title='Картридж')}
        r=audit.audit_account('wb_acc1',{1:(10000,75,2500,'5213'),2:(10000,50,5000,'5213')},
            {1:dict(cogs=100,external_code='5213',bundle=1)},[],{},query_evidence=lambda *args:e)
        self.assertEqual(r['checked'],1)
        self.assertIn('cost_changed_or_mismatch',r['cards'][0]['findings'])
        self.assertIn('below_floor',r['cards'][0]['findings'])

    def test_zero_mp_stock_has_no_sale_floor(self):
        e=evidence(rows=[offer('5213',928)])
        r=audit.audit_account('wb_acc1',{1:(10000,75,2500,'5213')},
            {1:dict(cogs=None,external_code='5213',bundle=1)},[],{1:0},query_evidence=lambda *args:e)
        self.assertEqual(r['cards'][0]['findings'],['no_mp_stock'])
        self.assertIsNone(r['cards'][0]['floor'])

    def test_no_high_discount_avoids_evidence_queries(self):
        with patch.object(audit,'load_evidence',side_effect=AssertionError):
            self.assertEqual(audit.audit_account('wb_acc1',{}, {},[],{})['checked'],0)

    def test_single_card_scope_requires_actual_evidence(self):
        goods={1:(10000,75,2500,'5213'),2:(10000,75,2500,'5213')}
        costs={nm:dict(cogs=1200,bundle=1) for nm in goods}
        with patch.object(guard.db,'query',side_effect=[
             [dict(nm_id=1,plan_price=2500,promo_name='active')],[]]):
            scope=guard.floor_controlled_bundles('wb_acc1',goods,costs,active_promo_names=['active'])
        rows=guard.decide('wb_acc1',goods,costs,{1:2500,2:2500},.569,floor_controlled=scope)
        self.assertEqual(rows[0]['status'],'todo')
        self.assertEqual(rows[1]['reason'],'акция цену не трогала')


class FinalStageTests(unittest.TestCase):
    def run_guard(self, argv, costs, prices, failure=False):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch('sys.argv', ['guard', '--acc', 'wb_acc1', '--no-tg']+argv))
            stack.enter_context(patch.object(guard, 'REPORTS', directory))
            stack.enter_context(patch.object(guard, 'COGS_MIN_KNOWN', 1))
            stack.enter_context(patch.object(guard, 'active_promos', return_value=([], None)))
            fetch=stack.enter_context(patch.object(guard, 'fetch_goods', side_effect=prices))
            stack.enter_context(patch.object(guard, 'cogs_map', return_value=costs))
            stack.enter_context(patch.object(guard, 'mp_stock', return_value={}))
            stack.enter_context(patch.object(guard, 'prepromo_map', return_value={1:2500}))
            stack.enter_context(patch.object(guard, 'floor_controlled_bundles', return_value={1:'export'}))
            saved=stack.enter_context(patch.object(guard, 'save'))
            pushed=stack.enter_context(patch.object(guard, 'push', return_value=(True,'')))
            stack.enter_context(patch.object(guard.time, 'sleep'))
            checked=stack.enter_context(patch.object(audit, 'audit_account',
                side_effect=RuntimeError('audit unavailable') if failure else None,
                return_value=dict(checked=1,flagged=0,cards=[])))
            output=io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(guard.main(),0)
            files=list(pathlib.Path(directory).glob('wb_discount_audit_*.json'))
            if not failure:
                self.assertEqual(json.loads(files[0].read_text())['checked'],1)
            return fetch.call_count,checked.call_args,pushed.call_count,saved.call_args,output.getvalue()

    def test_after_apply_audit_uses_final_reread_and_confirmed_decision(self):
        initial={1:(10000,75,2500,'5213')}
        after={1:(10000,50,5000,'5213')}
        calls,check,push,save,_=self.run_guard(['--apply'],
            {1:dict(cogs=1200,bundle=1,external_code='5213')},[initial,after,after])
        self.assertEqual(calls,3)
        self.assertEqual(push,1)
        self.assertEqual(check.args[1],after)
        self.assertEqual(check.args[3][0]['status'],'confirmed')
        self.assertEqual(save.args[0][0]['status'],'confirmed')

    def test_dry_run_never_pushes_and_audit_failure_is_visible(self):
        goods={1:(10000,75,2500,'5213')}
        calls,check,push,save,output=self.run_guard(['--dry'],
            {1:dict(cogs=1200,bundle=1,external_code='5213')},[goods,goods],True)
        self.assertEqual(calls,2)
        self.assertEqual(push,0)
        self.assertEqual(save.args[0][0]['status'],'dry')
        self.assertIn('АУДИТ СКИДОК НЕ ЗАВЕРШЁН',output)

    def test_safety_skipped_account_still_audited_without_push(self):
        goods={1:(10000,75,2500,'5213')}
        _,check,push,_,_=self.run_guard(['--apply'],{},[goods,goods])
        self.assertEqual(check.args[3],[])
        self.assertEqual(push,0)
