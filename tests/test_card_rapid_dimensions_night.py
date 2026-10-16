import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch as mockpatch, Mock
from copy import deepcopy
from tools.card_rapid_dimensions_night import patch
from tools import card_rapid_dimensions_night as worker


class RapidDimensionsPatchTests(unittest.TestCase):
    def setUp(self):
        self.row = {'id':'p', 'article':'SF-ABC', 'external_code':'0123', 'supplier_id':'s',
                    'dimensions':[{'id':n, 'meta':{'href':'attribute/'+n}, 'value':v}
                                  for n,v in [('length','31.5'),('width','10'),('height','8.5')]]}
        self.live = {'id':'p', 'article':'SF-ABC', 'externalCode':'0123',
                     'supplier':{'meta':{'href':'https://api.moysklad.ru/api/remap/1.2/entity/counterparty/s'}},
                     'attributes':[{'id':'other', 'value':'keep'}, {'id':'length','value':''}],
                     'barcodes':[{'ean8':'20000000'}], 'description':'keep description'}

    def test_preserves_unrelated_attributes_and_does_not_mutate_input(self):
        original = deepcopy(self.live)
        body = patch(self.live, self.row)
        self.assertEqual(set(body), {'id','attributes'})
        self.assertEqual(body['attributes'][0], self.live['attributes'][0])
        self.assertEqual({a['id']:a['value'] for a in body['attributes']},
                         {'other':'keep','length':'31.5','width':'10','height':'8.5'})
        self.assertEqual(self.live, original)

    def test_nonempty_conflicting_field_is_never_overwritten(self):
        self.live['attributes'][1]['value'] = '99'
        with self.assertRaises(RuntimeError):
            patch(self.live, self.row)

    def test_existing_desired_values_are_noop(self):
        self.live['attributes'] = deepcopy(self.row['dimensions'])
        self.live['attributes'][0]['value'] = '31,500'
        self.assertIsNone(patch(self.live, self.row))

    def test_partial_already_desired_value_is_preserved_verbatim(self):
        self.live['attributes'][1]['value'] = '31.500'
        result = patch(self.live, self.row)
        self.assertEqual(result['attributes'][1]['value'], '31.500')

    def test_identity_drift_and_archive_are_blocked(self):
        for field,value in [('id','other'),('article','different'),('externalCode','9999'),('archived',True)]:
            with self.subTest(field=field):
                changed=deepcopy(self.live); changed[field]=value
                with self.assertRaises(RuntimeError):
                    patch(changed,self.row)
        self.live['supplier']['meta']['href'] += 'other'
        with self.assertRaises(RuntimeError):
            patch(self.live,self.row)

    def test_duplicate_dimension_attribute_is_blocked(self):
        self.live['attributes'].append({'id':'length','value':''})
        with self.assertRaises(RuntimeError):
            patch(self.live,self.row)

    def test_new_damaged_or_stop_list_flags_block_writes(self):
        self.live['name'] = 'Картридж Брак'
        with self.assertRaises(RuntimeError):
            patch(self.live,self.row)
        self.live['name'] = 'Картридж'
        self.live['attributes'].append({'id':'stop','name':'Стоп лист','value':True})
        with self.assertRaises(RuntimeError):
            patch(self.live,self.row)

    def test_all_active_scope_includes_zero_stock_without_receipt_filter(self):
        row=deepcopy(self.row);row['source_id']='123'
        data={'error':{'code':0},'price':[{'ID':123,'CodeID':'SF-ABC','UnitsFree':0,
               'GabarityDlina':'0.315','GabarityShirina':'0.1','GabarityVisota':'0.085'}]}
        response=Mock(status_code=200);response.json.return_value=data
        with mockpatch.object(worker.requests,'get',return_value=response), mockpatch.object(worker,'save'), \
             mockpatch.dict(worker.os.environ,{'RAPID_API_AUTHKEY':'test'}), \
             mockpatch('prices.blacklist.load_set',return_value=set()), \
             mockpatch.object(worker.loader,'existing_docs') as receipts:
            eligible,skipped=worker.source_gate([row],'all_active_rapid')
            self.assertEqual(eligible,[row]);self.assertEqual(skipped,[])
            receipts.assert_not_called()

    def test_restore_only_owned_change_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory, mockpatch.object(worker,'OUT',Path(directory)), \
             mockpatch.object(worker,'checked_webhook',return_value={'enabled':False}), \
             mockpatch.object(worker,'set_webhook') as update:
            worker.save('webhook_restore_state.json',{'owned_change_pending':True,'id':worker.WEBHOOK_ID,
                'original_enabled':True,'url_sha256':worker.WEBHOOK_URL_SHA})
            worker.restore_webhook()
            update.assert_called_once_with(True)
            self.assertFalse(json.loads((Path(directory)/'webhook_restore_state.json').read_text())['owned_change_pending'])
            worker.restore_webhook()
            update.assert_called_once()


if __name__ == '__main__':
    unittest.main()
