"""Safety checks for new groups found outside existing bundle groups."""
import unittest
from ops.wb_core_glue import components_for, model_matches, prepare, printers, tokens


def rec(nm,model,kind,col='черный',chip='чип не указан',printer='Printer1'):
    card={'nmID':nm,'imtID':nm,'vendorCode':f'{nm:04d}','subjectID':1820,'title':model,'description':'',
          'characteristics':[{'id':5023,'value':[model]},{'id':58854,'value':[col]},
                             {'id':179078,'value':[printer]},{'id':179792,'value':[1]}]}
    ref={'title':model,'consumable_type':kind,'raw':{},'chip':None}
    return {'card':card,'ref':ref,'chip':chip,'printers':printers(card,ref)}


class Safety(unittest.TestCase):
    def test_exact_range_and_no_resource_letter_removal(self):
        self.assertEqual(tokens('CF530A-CF533A'),['CF530A','CF531A','CF532A','CF533A'])
        self.assertEqual(tokens('S051161-S051158'),['S051158','S051159','S051160','S051161'])
        self.assertEqual(tokens('CLI-521'),['CLI521'])
        self.assertEqual(tokens('056-056H'),['056056H'])
        self.assertEqual(tokens('DK-7300+TK-7300'),['DK7300','TK7300'])

    def test_color_suffix_matches_only_its_color(self):
        c=rec(1,'GI-490C','Чернила','голубой')
        self.assertTrue(model_matches('GI490',c['card'],c['ref']))
        c['card']['characteristics'][1]['value']=['черный']
        self.assertFalse(model_matches('GI490',c['card'],c['ref']))
        c=rec(1,'CLI-521XL','Картридж')
        self.assertFalse(model_matches('CLI521',c['card'],c['ref']))

    def test_color_kit_full_printers_and_chip(self):
        kit=rec(10,'CF530A-CF533A','Комплект картриджей')
        cc=[rec(i+1,f'CF53{i}A','Картридж',c) for i,c in enumerate(['черный','голубой','желтый','пурпурный'])]
        found,reason=components_for(kit,cc)
        self.assertIsNone(reason);self.assertEqual(len(found),4)
        cc[0]['printers']=('wb',('PRINTER1','PRINTER2'))
        self.assertIsNotNone(components_for(kit,cc)[1])
        cc[0]['printers']=kit['printers'];cc[0]['chip']='с чипом'
        self.assertIsNotNone(components_for(kit,cc)[1])

    def test_ambiguous_same_color_and_missing_component_block(self):
        kit=rec(10,'GI-490','Комплект чернил')
        cc=[rec(i+1,'GI-490'+col,'Чернила',rus) for i,(col,rus) in enumerate([('BK','черный'),('C','голубой'),('M','пурпурный'),('Y','желтый')])]
        self.assertIsNone(components_for(kit,cc)[1])
        self.assertIsNotNone(components_for(kit,cc[:2])[1])
        cc.append(rec(20,'GI-490C','Чернила','голубой'))
        self.assertIsNotNone(components_for(kit,cc)[1])

    def test_drum_kit_must_have_both_exact_roles(self):
        kit=rec(10,'DR-2175+TN-2175','Фотобарабан + картридж')
        drum=rec(1,'DR-2175','Фотобарабан');toner=rec(2,'TN-2175','Тонер-картридж')
        self.assertIsNone(components_for(kit,[drum,toner])[1])
        self.assertIsNotNone(components_for(kit,[toner])[1])

    def test_no_bundles_required_to_create_group(self):
        rr=[rec(10,'DR-2175+TN-2175','Фотобарабан + картридж'),rec(1,'DR-2175','Фотобарабан'),rec(2,'TN-2175','Тонер-картридж')]
        cards=[r['card'] for r in rr];refs={r['card']['vendorCode']:r['ref'] for r in rr}
        plan=prepare({'cards':cards,'read_at':'test'},{'references':refs,'marks':{}})
        self.assertEqual(len(plan['operations']),1)
        self.assertEqual(plan['operations'][0]['partitions'],[[1,2,10]])

    def test_foreign_members_are_never_silently_moved(self):
        rr=[rec(10,'DR-2175+TN-2175','Фотобарабан + картридж'),rec(1,'DR-2175','Фотобарабан'),rec(2,'TN-2175','Тонер-картридж'),rec(3,'Other','Картридж')]
        rr[-1]['card']['imtID']=rr[1]['card']['imtID']
        plan=prepare({'cards':[r['card'] for r in rr],'read_at':'test'},{'references':{r['card']['vendorCode']:r['ref'] for r in rr},'marks':{}})
        self.assertEqual(plan['operations'],[])
        self.assertTrue(any('другие' in r['reason'] for r in plan['review']))


if __name__=='__main__':unittest.main()
