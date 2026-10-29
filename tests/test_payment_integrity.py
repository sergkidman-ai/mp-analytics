# поток: inv
import copy
import unittest
from unittest.mock import patch, MagicMock
import invoice_bot
from pathlib import Path
# Legacy standalone bank modules modify sys.path; bind namespace imports to this checkout.
invoice_bot.__path__ = [str(Path(__file__).resolve().parents[1] / "invoice_bot")]
from invoice_bot import ms, payment_integrity as integrity, supplier_credit as credit

ORG='7807355364';OTHER='7811803918';SUPPLIER='fixture-supplier'
def ref(kind, ident):return {'meta':{'href':ms.MS+'/entity/'+kind+'/'+ident,'type':kind,'mediaType':'application/json'}}

def fixture():
    org={**ref('organization','org'), 'id':'org','inn':ORG}
    other={**ref('organization','other'), 'id':'other','inn':OTHER}
    store={**ref('store','store'),'id':'store','name':'Звездный'}
    wrong_store={**ref('store','wrong-store'),'id':'wrong-store','name':'Дисквер'}
    agent={**ref('counterparty','agent'),'id':'agent','inn':SUPPLIER}
    po={**ref('purchaseorder','po'),'id':'po','name':'fixture-order','organization':ref('organization','org'),'store':ref('store','store'),'agent':ref('counterparty','agent'), 'sum':100000,'shippedSum':100000,'payedSum':0,'supplies':[ref('supply','supply')]}
    supply={**ref('supply','supply'),'id':'supply','organization':ref('organization','org'),'store':ref('store','store'),'agent':ref('counterparty','agent'),'purchaseOrder':ref('purchaseorder','po'),'sum':100000,'payedSum':0,'applicable':True,'moment':'2026-10-29 10:00:00'}
    data={x['meta']['href'][len(ms.MS):]:x for x in [org,other,store,wrong_store,agent,po,supply]}
    def get(path):return copy.deepcopy(data[path.split('?')[0]])
    return data,get

class IntegrityTests(unittest.TestCase):
    def setUp(self):self.data,self.get=fixture()
    def test_matching_order_and_receipt_pass(self):
        self.assertEqual(len(integrity.inspect_orders(['po'],ORG,self.get)),1)
    def test_wrong_order_organization_or_warehouse_blocks(self):
        for field,value in [('organization',ref('organization','other')),('store',ref('store','wrong-store'))]:
            with self.subTest(field=field):
                data,get=fixture();data['/entity/purchaseorder/po'][field]=value
                with self.assertRaises(integrity.PaymentIntegrityError):integrity.inspect_orders(['po'],ORG,get)
    def test_receipt_org_warehouse_or_supplier_must_match_order(self):
        for field,value in [('organization',ref('organization','other')),('store',ref('store','wrong-store')),('agent',ref('counterparty','different'))]:
            with self.subTest(field=field):
                data,get=fixture();data['/entity/supply/supply'][field]=value
                with self.assertRaises(integrity.PaymentIntegrityError):integrity.inspect_orders(['po'],ORG,get)
    def test_prepayment_without_receipt_and_advance_without_orders(self):
        self.data['/entity/purchaseorder/po']['supplies']=[]
        self.assertEqual(len(integrity.inspect_orders(['po'],ORG,self.get)),1)
        self.assertEqual(integrity.inspect_orders([],None,self.get),[])
    def test_batch_checks_every_order_not_only_first(self):
        second=copy.deepcopy(self.data['/entity/purchaseorder/po']);second['organization']=ref('organization','other')
        self.data['/entity/purchaseorder/second']=second
        with self.assertRaises(integrity.PaymentIntegrityError):integrity.inspect_orders(['po','second'],ORG,self.get)
    def test_stale_amount_after_credit_is_blocked(self):
        self.data['/entity/purchaseorder/po']['payedSum']=53278
        draft={'org_inn':ORG,'kind':'deferred_batch','covers_po_ids':['po'],'amount':'1000.00'}
        with self.assertRaises(integrity.PaymentIntegrityError):integrity.validate_draft(draft,self.get)
        draft['amount']='467.22';integrity.validate_draft(draft,self.get)
    def test_no_receipt_does_not_allow_deferred_payment(self):
        self.data['/entity/purchaseorder/po']['shippedSum']=0
        with self.assertRaises(integrity.PaymentIntegrityError):integrity.validate_draft({'org_inn':ORG,'kind':'deferred_batch','covers_po_ids':['po'],'amount':1},self.get)
    def test_common_bank_builder_guard_precedes_building_payload(self):
        from invoice_bot import payment_draft as pd
        with patch.object(integrity,'validate_draft',side_effect=integrity.PaymentIntegrityError('blocked')),patch.object(pd,'po_docs') as docs:
            with self.assertRaises(integrity.PaymentIntegrityError):pd.base_payment({'covers_po_ids':['po']},None)
        docs.assert_not_called()
    def test_queue_does_not_insert_invalid_prepayment(self):
        from invoice_bot import po_payment_watch as watch
        with patch.object(watch,'_sync_pending',return_value=['po']),patch.object(watch.db,'query',return_value=[{'amount':100}]),patch.object(integrity,'inspect_orders',side_effect=integrity.PaymentIntegrityError('blocked')),patch.object(watch.db,'get_conn') as conn:
            with self.assertRaises(integrity.PaymentIntegrityError):watch.process_prepayment_per_order(SUPPLIER)
        conn.assert_not_called()

class CreditTests(unittest.TestCase):
    def setUp(self):
        self.data,self.get=fixture()
        old={'meta':{'href':ms.MS+'/entity/supply/old','type':'supply'},'linkedSum':447846}
        self.row={'ms_payment_id':'payment','credit_kop':53278,'baseline_operations':[old]}
        self.payment={**ref('paymentout','payment'),'id':'payment','organization':self.data['/entity/organization/org'],'agent':self.data['/entity/counterparty/agent'],'sum':501124,'operations':[old]}
        self.data['/entity/paymentout/payment']=self.payment
        self.puts=0
    def put(self,path,payload):
        self.puts+=1;self.data[path]['operations']=copy.deepcopy(payload['operations'])
        return 200,copy.deepcopy(self.data[path])
    def run_settle(self,put=None):
        with patch.object(credit.db,'query',return_value=[self.row]),patch.object(credit.db,'get_conn',return_value=MagicMock()),patch.object(credit.ms,'get',side_effect=self.get),patch.object(credit.ms,'put',side_effect=put or self.put),patch('collectors.alfa_link.linkable',return_value=True):
            return credit.settle(ORG,SUPPLIER,['po'])
    def test_credit_applies_exactly_once_preserving_original_links(self):
        self.assertEqual(self.run_settle(),53278)
        self.assertEqual(self.run_settle(),0);self.assertEqual(self.puts,1)
        self.assertEqual(credit.operations_map(self.payment['operations'])[ms.MS+'/entity/supply/old'],447846)
        self.assertEqual(credit.available_credit(self.payment,self.row),0)
    def test_credit_partially_spent_and_remainder_stays_available(self):
        self.data['/entity/supply/supply']['sum']=10000
        self.assertEqual(self.run_settle(),10000)
        self.assertEqual(credit.available_credit(self.payment,self.row),43278)
    def test_different_organization_source_is_blocked(self):
        self.payment['organization']['inn']=OTHER
        with self.assertRaises(integrity.PaymentIntegrityError):self.run_settle()
        self.assertEqual(self.puts,0)
    def test_crash_after_ms_write_does_not_spend_again(self):
        def uncertain(path,payload):self.put(path,payload);raise RuntimeError('simulated missing response')
        with self.assertRaises(RuntimeError):self.run_settle(uncertain)
        self.assertEqual(self.run_settle(),0);self.assertEqual(self.puts,1)
    def test_expanded_metadata_query_does_not_change_operation_identity(self):
        plain = [{'meta':{'href':ms.MS+'/entity/supply/old'},'linkedSum':447846}]
        expanded = [{'meta':{'href':ms.MS+'/entity/supply/old?expand=organization'},'linkedSum':447846}]
        self.assertEqual(credit.operations_map(plain),credit.operations_map(expanded))
    def test_credit_cannot_exceed_real_unallocated_money(self):
        self.payment['sum']=447946
        self.assertEqual(credit.available_credit(self.payment,self.row),100)

if __name__=='__main__':unittest.main()
