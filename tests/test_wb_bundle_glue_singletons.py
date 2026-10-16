# поток: mkt
"""Safety boundaries for the explicitly authorized singleton bundle wave."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from ops.wb_bundle_glue_singletons import (build_plan, check_before, family_reasons,
                                          apply_plan, validate_operation, Content)


def card(nm, code, qty=1, imt=None, chip_text='Не требуется чип'):
    return {'nmID':nm,'vendorCode':code,'imtID':imt or nm*10,'subjectID':1820,
            'title':f'Картридж TEST {qty} шт','description':chip_text,
            'characteristics':[{'id':179792,'value':[str(qty)]},
                {'id':5023,'value':['TEST']},{'id':58854,'value':['Черный']},
                {'id':88963,'value':['1000']},{'id':179078,'value':['Printer A','Printer B']}]}


class SingletonBundleSafetyTests(unittest.TestCase):
    def setUp(self):
        self.p=card(1,'3351')
        self.bb=[card(2,'3351X2',2),card(3,'3351X6',6),card(4,'3351X10',10)]
        self.ref={'title':'TEST','consumable_type':'Картридж','chip':None,'raw':{'colors_count':1}}

    def plan(self, extra=()):
        return build_plan({'read_at':'2026-10-07','cards':[self.p,*self.bb,*extra]},
                          {'3351':self.ref},{'3351':set()})

    def test_silence_is_inherited_only_with_identical_products(self):
        self.assertEqual(family_reasons(self.p,self.bb,self.ref,set()),[])
        self.bb[0]['characteristics'][-1]['value']=['Printer A']
        self.assertIn('Список совместимых принтеров отличается',family_reasons(self.p,self.bb,self.ref,set()))

    def test_chip_conflict_blocks_entire_family(self):
        self.bb[0]['description']='Картридж с чипом'
        self.bb[1]['description']='Картридж без чипа'
        self.assertEqual(len(self.plan()['operations']),0)
        self.assertTrue(self.plan()['review'])

    def test_color_count_is_not_physical_pack_quantity(self):
        self.ref['raw']['colors_count']=3
        self.assertEqual(len(self.plan()['operations']),1)

    def test_parent_already_grouped_is_out_of_scope(self):
        other=card(7,'9999',imt=self.p['imtID'])
        self.assertEqual(self.plan([other])['operations'],[])

    def test_foreign_card_in_bundle_group_is_not_moved(self):
        other=card(7,'9999',imt=self.bb[0]['imtID'])
        self.assertEqual(self.plan([other])['operations'],[])

    def test_quantity_error_blocks_entire_family(self):
        self.bb[1]['title']='Картридж TEST 2 шт'
        self.assertEqual(self.plan()['operations'],[])

    def test_group_changed_after_preview_is_rejected(self):
        op=self.plan()['operations'][0]
        groups={str(c['imtID']):[c] for c in [self.p,*self.bb]}
        self.assertTrue(check_before(op,groups)[0])
        groups[str(self.p['imtID'])].append(card(99,'9999',imt=self.p['imtID']))
        self.assertFalse(check_before(op,groups)[0])

    def test_tampered_request_cannot_write_unchecked_ids(self):
        op=self.plan()['operations'][0]
        validate_operation(op)
        op['body']['nmIDs']=[999]
        with self.assertRaises(RuntimeError):validate_operation(op)

    def test_uncertain_write_is_not_retried(self):
        plan=self.plan()
        client=Mock()
        groups={c['imtID']:[c] for c in [self.p,*self.bb]}
        client.group.side_effect=lambda imt:groups[imt]
        client.move.side_effect=requests.Timeout('uncertain')
        with tempfile.TemporaryDirectory() as d, patch('ops.wb_bundle_glue_singletons.OUT',Path(d)):
            with self.assertRaises(requests.Timeout):apply_plan(plan,client)
            self.assertEqual(client.move.call_count,1)
            self.assertTrue((Path(d)/'apply_results.json').exists())

    def test_transient_group_index_payload_disagreement_retries_reads(self):
        client=object.__new__(Content)
        first=Mock(status_code=200)
        first.json.return_value={'cards':[card(1,'3351',imt=99)]}
        second=Mock(status_code=200)
        second.json.return_value={'cards':[card(1,'3351',imt=10)]}
        client.request=Mock(side_effect=[first,second])
        with patch('ops.wb_bundle_glue_singletons.time.sleep'):
            self.assertEqual(client.group(10)[0]['imtID'],10)
        self.assertEqual(client.request.call_count,2)


if __name__=='__main__':unittest.main()
