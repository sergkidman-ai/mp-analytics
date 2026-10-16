import unittest
from copy import deepcopy
from tools.card_supplier_ean13_apply import patch

class SupplierBarcodePatchTests(unittest.TestCase):
    def setUp(self):
        self.row={'id':'card','article':'article','external_code':'0123','supplier_id':'supplier','ean13':['4006381333931']}
        self.live={'id':'card','article':'article','externalCode':'0123','supplier':{'meta':{'href':'https://api.moysklad.ru/api/remap/1.2/entity/counterparty/supplier'}},
                   'barcodes':[{'ean8':'20468606'},{'code128':'DSGOS0000123'},{'ean13':'4627199113975'}]}
    def test_preserves_every_existing_barcode_and_order(self):
        original=deepcopy(self.live);r=patch(self.live,self.row)
        self.assertEqual(r['barcodes'],self.live['barcodes']+[{'ean13':'4006381333931'}])
        self.assertEqual(self.live,original)
        self.assertEqual(set(r),{'id','barcodes'})
    def test_already_present_does_not_write(self):
        self.live['barcodes'].append({'ean13':'4006381333931'})
        self.assertIsNone(patch(self.live,self.row))
    def test_code_under_other_type_needs_review(self):
        self.live['barcodes'].append({'code128':'4006381333931'})
        with self.assertRaises(RuntimeError):patch(self.live,self.row)
    def test_identity_changes_stop_before_write(self):
        for field,value in [('article','other'),('externalCode','9999'),('archived',True)]:
            with self.subTest(field=field):
                changed=deepcopy(self.live);changed[field]=value
                with self.assertRaises(RuntimeError):patch(changed,self.row)
        self.live['supplier']['meta']['href']='https://api.moysklad.ru/api/remap/1.2/entity/counterparty/other'
        with self.assertRaises(RuntimeError):patch(self.live,self.row)
    def test_no_own_barcode_is_invented(self):
        self.live['barcodes']=[]
        self.assertEqual(patch(self.live,self.row)['barcodes'],[{'ean13':'4006381333931'}])

if __name__=='__main__':unittest.main()
