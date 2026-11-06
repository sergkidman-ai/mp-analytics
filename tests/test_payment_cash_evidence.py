# поток: inv
import copy
import unittest
from urllib.parse import unquote
from invoice_bot import payment_integrity as guard, ms

def ref(kind, name):
    return {'meta':{'href':ms.MS+'/entity/'+kind+'/'+name,'type':kind}}

class RecordedCashTests(unittest.TestCase):
    def setUp(self):
        self.org={**ref('organization','org'),'id':'org','inn':'7807355364'}
        self.agent=ref('counterparty','agent')
        self.store={**ref('store','store'),'id':'store','name':'Звездный'}
        self.po={**ref('purchaseorder','po'),'id':'po','name':'7199-Кактус','moment':'2026-10-30 10:00:00',
                 'organization':ref('organization','org'),'agent':self.agent,'store':ref('store','store'),
                 'sum':407473,'shippedSum':407473,'payedSum':0,'supplies':[]}
        self.payments=[];self.queries=[]
        self.draft={'org_inn':'7807355364','kind':'prepayment_order','covers_po_ids':['po'],'amount':'4074.73'}

    def get(self,path):
        if path.startswith('/entity/paymentout?'):
            self.queries.append(unquote(path))
            return {'rows':copy.deepcopy(self.payments)}
        docs={'/entity/organization/org':self.org,'/entity/store/store':self.store,'/entity/purchaseorder/po':self.po}
        if hasattr(self,'extra_po'):docs['/entity/purchaseorder/po2']=self.extra_po
        return copy.deepcopy(docs[path.split('?')[0]])

    def payment(self,amount=407473,purpose='Предоплата по счёту №7199-Кактус от 30.10.2026. В т.ч. НДС 22% - 734.79 руб.',operations=None):
        return {'id':'payment','name':'60197','organization':ref('organization','org'),'agent':self.agent,
                'sum':amount,'applicable':True,'paymentPurpose':purpose,'operations':operations or []}

    def test_unlinked_previous_payment_blocks_same_invoice(self):
        self.payments=[self.payment()]
        with self.assertRaisesRegex(guard.PaymentReconciliationError,'60197'):
            guard.validate_draft(self.draft,self.get)

    def test_partial_unlinked_payment_only_allows_actual_remainder(self):
        self.payments=[self.payment(amount=400000)]
        with self.assertRaises(guard.PaymentReconciliationError):guard.validate_draft(self.draft,self.get)
        self.draft['amount']='74.73';guard.validate_draft(self.draft,self.get)

    def test_one_kopeck_after_linked_partial_payment_remains_payable(self):
        self.po.update(name='7200',sum=149029,shippedSum=149029,payedSum=149028)
        operation={**ref('supply','receipt'),'purchaseOrder':ref('purchaseorder','po'),'linkedSum':149028}
        self.payments=[self.payment(amount=149028,purpose='Предоплата по счёту №7200. Без НДС.',operations=[operation])]
        self.draft['amount']='0.01';guard.validate_draft(self.draft,self.get)

    def test_receipt_payment_is_not_subtracted_twice(self):
        self.po['payedSum']=400000
        self.payments=[self.payment(amount=400000,operations=[{**ref('supply','receipt'),'purchaseOrder':ref('purchaseorder','po'),'linkedSum':400000}])]
        self.draft['amount']='74.73';guard.validate_draft(self.draft,self.get)

    def test_penny_combined_with_another_invoice_is_allowed(self):
        self.po.update(name='7200',sum=149029,shippedSum=149029,payedSum=149028)
        self.extra_po=copy.deepcopy(self.po)
        self.extra_po.update(**ref('purchaseorder','po2'),id='po2',name='7230',sum=455787,shippedSum=455787,payedSum=0)
        operation={**ref('supply','receipt'),'purchaseOrder':ref('purchaseorder','po'),'linkedSum':149028}
        self.payments=[self.payment(amount=149028,purpose='Предоплата по счёту №7200. Без НДС.',operations=[operation])]
        self.draft.update(covers_po_ids=['po','po2'],amount='4557.88')
        guard.validate_draft(self.draft,self.get)

    def test_receipt_allocation_without_expanded_order_reference_is_recognized(self):
        self.po['supplies']=[ref('supply','receipt')]
        receipt={**ref('supply','receipt'),'id':'receipt','sum':407473,'payedSum':300000,
                 'organization':ref('organization','org'),'agent':self.agent,'store':ref('store','store'),'applicable':True}
        base_get=self.get
        def getter(path):return copy.deepcopy(receipt) if path=='/entity/supply/receipt' else base_get(path)
        self.payments=[self.payment(amount=300000,purpose='Оплата по счетам №7199,7201. Без НДС.',
                                    operations=[{**ref('supply','receipt'),'linkedSum':300000}])]
        self.po['payedSum']=300000;self.draft['amount']='1074.73'
        guard.validate_draft(self.draft,getter)

    def test_unreconciled_old_batch_requires_review(self):
        self.payments=[self.payment(purpose='Оплата по счетам №7199,7201. Без НДС.',operations=[{**ref('supply','other'),'purchaseOrder':ref('purchaseorder','other'),'linkedSum':407473}])]
        with self.assertRaisesRegex(guard.PaymentReconciliationError,'привязки'):
            guard.validate_draft(self.draft,self.get)

    def test_correctly_allocated_partial_batch_allows_remainder(self):
        self.payments=[self.payment(amount=400000,purpose='Оплата по счетам №7199,7201. Без НДС.',operations=[{**ref('supply','receipt'),'purchaseOrder':ref('purchaseorder','po'),'linkedSum':300000},{**ref('supply','other'),'purchaseOrder':ref('purchaseorder','other'),'linkedSum':100000}])]
        self.po['payedSum']=300000;self.draft['amount']='1074.73'
        guard.validate_draft(self.draft,self.get)

    def test_same_amount_for_another_invoice_does_not_block(self):
        self.payments=[self.payment(purpose='Оплата по счёту №8001. Без НДС.')]
        guard.validate_draft(self.draft,self.get)

    def test_unposted_document_is_not_payment_evidence(self):
        doc=self.payment();doc['applicable']=False;self.payments=[doc]
        guard.validate_draft(self.draft,self.get)

    def test_payment_query_is_scoped_to_both_organization_and_agent(self):
        guard.validate_draft(self.draft,self.get)
        self.assertIn('organization='+self.org['meta']['href'],self.queries[0])
        self.assertIn('agent='+self.agent['meta']['href'],self.queries[0])

    def test_wrong_organization_response_fails_closed(self):
        doc=self.payment();doc['organization']=ref('organization','other');self.payments=[doc]
        with self.assertRaises(guard.PaymentReconciliationError):guard.validate_draft(self.draft,self.get)

    def test_advance_without_invoice_bases_does_not_query_invoice_payments(self):
        result=guard.validate_draft({'kind':'advance','covers_po_ids':[]},self.get)
        self.assertEqual(result,[]);self.assertEqual(self.queries,[])
