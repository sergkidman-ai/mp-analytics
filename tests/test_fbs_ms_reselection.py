# поток: inv
from copy import deepcopy
from datetime import datetime
from unittest import TestCase
from unittest.mock import patch
from core import ms_api
from ops.fbs_ms_reselection import replacement_payload,sale_signature,needs_update
from ops.ms_supplier_priorities import POLICY_VERSION
from ops.fbs_ms_order_payload import STORE
from invoice_bot.supplier_groups import GROUPS
from prices.profiles import STORE_REMOTE


class ReselectionTests(TestCase):
    def setUp(self):
        guard=patch('ops.fbs_ms_reselection.assert_automatic_composition')
        self.manual_guard=guard.start()
        self.addCleanup(guard.stop)

    def test_manual_history_blocks_reselection_before_building_payload(self):
        from core.order_composition_guard import ManualCompositionProtected
        old,proof=self.fixtures()
        self.manual_guard.side_effect=ManualCompositionProtected('manual_user_composition_protected')
        with self.assertRaises(ManualCompositionProtected):replacement_payload(old,proof,datetime.now().astimezone())

    def fixtures(self):
        old={'name':'n','sum':90000,'state':{'name':'Неукомплектован'},
             'positions':{'meta':{'size':1},'rows':[
                 {'id':'old-position','assortment':dict(ms_api.ref('product','old'),externalCode='0019'),
                  'quantity':1,'price':100000,'discount':10,'vat':5,'vatEnabled':True,'reserve':0}]}}
        item={'card_id':'new','base_code':'0019','available':False,'quantity':'1',
              'supplier_id':GROUPS['Изипринт'][0][2],'selection_policy':POLICY_VERSION,
              'priority':15,'priority_at':'2026-11-06T12:00:00+03:00','purchase_kopeks':50000,
              'supplier_available':'10','stock_store_id':STORE_REMOTE,'manual_fallback':False}
        proof={'total_kopeks':90000,'market':{'created_at':'2026-11-05T16:00:00+03:00'},
               'selected':[item],'payload':{'store':ms_api.ref('store',STORE['acc1']),
                   'positions':[{'assortment':ms_api.ref('product','new'),'quantity':1}]}}
        return old,proof

    def test_supplier_swap_preserves_every_sale_field_and_uses_original_creation(self):
        old,proof=self.fixtures();snapshot=deepcopy(old)
        payload=replacement_payload(old,proof,datetime.fromisoformat('2026-11-06T12:00:00+03:00'))
        self.assertEqual(old,snapshot)
        self.assertEqual(sale_signature(old),sale_signature({'positions':{'rows':payload['positions']}}))
        self.assertEqual(ms_api.meta_id(payload['positions'][0],'assortment'),'new')
        self.assertEqual(payload['deliveryPlannedMoment'],'2026-11-07 00:00:00')
        self.assertTrue(needs_update(old,payload))

    def test_manual_selection_or_different_composition_never_changes_sale(self):
        old,proof=self.fixtures()
        proof['selected'][0]['manual_fallback']=True
        with self.assertRaisesRegex(ValueError,'manual_review'):
            replacement_payload(old,proof,datetime.now().astimezone())

    def test_shortage_preserves_existing_future_stock_reservation(self):
        old,proof=self.fixtures();old['positions']['rows'][0]['reserve']=1
        payload=replacement_payload(old,proof,datetime.fromisoformat('2026-11-06T12:00:00+03:00'))
        self.assertEqual(payload['positions'][0]['reserve'],1)
        proof['selected'][0]['manual_fallback']=False
        old['positions']['rows'][0]['assortment']['externalCode']='0020'
        with self.assertRaisesRegex(ValueError,'recomposition'):
            replacement_payload(old,proof,datetime.now().astimezone())

    def test_already_shipped_and_amount_changes_fail_closed(self):
        old,proof=self.fixtures();old['demands']=[{'id':'d'}]
        with self.assertRaisesRegex(ValueError,'shipped'):
            replacement_payload(old,proof,datetime.now().astimezone())
        old.pop('demands');old['sum']=90001
        with self.assertRaisesRegex(ValueError,'total_differ'):
            replacement_payload(old,proof,datetime.now().astimezone())
