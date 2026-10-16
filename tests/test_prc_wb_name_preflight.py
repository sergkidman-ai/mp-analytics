# поток: prc
"""A live candidate must be held when its value, identity or source changed."""
import copy
import unittest

from tools.prc.wb_fill import compose_details
from tools.prc.wb_name_preflight import validate_live


class LivePreflight(unittest.TestCase):
    def setUp(self):
        self.raw = {'consumable_type':'Картридж','title':'Q2612A','additional_title':'',
                    'printer_models':[{'brand':'HP'}],'ink_colors':['черный'],'chip':'С чипом'}
        self.product = {'id':'product-1','externalCode':'1234','code':'1234ep',
                        'name':'Картридж HP Q2612A черный с чипом','article':'A',
                        'description':'Описание','supplier':{'meta':{'href':'supplier'}},
                        'productFolder':{'meta':{'href':'folder'}},'uom':{'meta':{'href':'uom'}},
                        'weight':1,'barcodes':[{'code128':'ABC'}],
                        'attributes':[{'name':'Название WB','value':'Ручное название'}]}
        self.row = {'ms_id':'product-1','external_code':'1234','code':'1234ep',
                    'supplier_name':self.product['name'],'current':'Ручное название',
                    'proposed':compose_details(self.raw,self.product['name']).name}

    def check(self, product=None, raw=None):
        return validate_live(self.row, product or self.product, raw or self.raw,
                             self.raw, ({},set()))[0]

    def test_unchanged_complete_candidate_passes(self):
        self.assertEqual(self.check(), [])

    def test_manual_edit_after_snapshot_held(self):
        p=copy.deepcopy(self.product);p['attributes'][0]['value']='Новая правка человека'
        self.assertIn('изменилось прежнее Название WB', self.check(product=p))

    def test_external_code_changed_held(self):
        p=dict(self.product,externalCode='9999')
        self.assertIn('изменился id/внешний код', self.check(product=p))

    def test_catalog_chip_change_held(self):
        reasons=self.check(raw=dict(self.raw,chip='Без чипа'))
        self.assertIn('изменились/пропали исходные поля ТК',reasons)

    def test_archived_and_incomplete_held(self):
        p=dict(self.product,archived=True,weight=0)
        reasons=self.check(product=p)
        self.assertIn('карточка в архиве',reasons)
        self.assertTrue(any('Вес' in r for r in reasons))

    def test_stop_list_held(self):
        p=copy.deepcopy(self.product);p['attributes'].append({'name':'Стоп лист','value':True})
        self.assertTrue(any('стоп-лист' in r for r in self.check(product=p)))


if __name__=='__main__':
    unittest.main()
