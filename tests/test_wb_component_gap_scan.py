"""No foreign moves or duplicate creations when resolving laser gaps."""
import copy
import unittest
from ops.wb_component_gap_scan import scan


def fixture():
    colors=['Черный','Голубой','Пурпурный','Желтый'];suffix=['K','C','M','Y']
    cards=[];models=[]
    for i in range(5):
        code=f'{i+1:04d}';model='ABC100'+suffix[i] if i<4 else 'ABC100'
        typ='Картридж' if i<4 else 'Комплект картриджей'
        c={'nmID':i+1,'imtID':i+1,'vendorCode':code,'title':model,'subjectID':1820,'description':'',
           'characteristics':[{'id':5023,'value':[model]},{'id':58854,'value':[colors[i] if i<4 else 'Набор CMYK']},
                              {'id':179078,'value':['Brand Printer1']},{'id':179792,'value':[1 if i<4 else 4]}]}
        ref={'external_code':code,'title':model,'united_title':'ABC100 DS','consumable_type':typ,'color':suffix[i] if i<4 else None,'chip':'chip','gone_at':None,
             'raw':{'assignment':'для лазерного принтера','printer_models':[{'brand':'Brand','title':'Printer1'}],'colors_count':4}}
        cards.append(c);models.append(ref)
    return {'cards':cards,'read_at':'test'},{'references':{r['external_code']:r for r in models},'marks':{}},models


class Safety(unittest.TestCase):
    def test_complete_inventory_joins_without_creating_duplicates(self):
        snap,source,models=fixture();plan=scan(snap,source,models)
        self.assertEqual(len(plan['operations']),1);self.assertEqual(plan['create'],[])
        self.assertEqual(plan['operations'][0]['partitions'],[[1,2,3,4,5]])

    def test_printers_must_match_fully(self):
        snap,source,models=fixture();snap['cards'][1]['characteristics'][2]['value'].append('Brand Printer2')
        self.assertEqual(scan(snap,source,models)['operations'],[])

    def test_foreign_member_blocks_moving_whole_source(self):
        snap,source,models=fixture();foreign=copy.deepcopy(snap['cards'][0]);foreign.update(nmID=100,vendorCode='9999',imtID=1);snap['cards'].append(foreign)
        self.assertEqual(scan(snap,source,models)['operations'],[])

    def test_missing_colour_has_exact_create_code(self):
        snap,source,models=fixture();snap['cards']=[c for c in snap['cards'] if c['nmID']!=4]
        p=scan(snap,source,models);self.assertTrue(any(m['code']=='0004' for r in p['create'] for m in r['missing_colors']))

    def test_unknown_existing_variant_never_becomes_absent_inventory(self):
        snap,source,models=fixture();models[0]['chip']=None;source['marks']['0002']=['чип без счётчика']
        p=scan(snap,source,models)
        self.assertEqual(p['create'],[]);self.assertEqual(p['operations'],[])


if __name__=='__main__':unittest.main()
