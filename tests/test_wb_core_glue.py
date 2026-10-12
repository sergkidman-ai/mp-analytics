"""Safety checks for new groups found outside existing bundle groups."""
import unittest
from unittest.mock import patch
from pathlib import Path
import tempfile,json
from ops import wb_core_glue as module
from ops.wb_core_glue import components_for, model_matches, prepare, printers, tokens


def rec(nm,model,kind,col='черный',chip='чип не указан',printer='Printer1'):
    card={'nmID':nm,'imtID':nm,'vendorCode':f'{nm:04d}','subjectID':1820,'title':model,'description':'',
          'characteristics':[{'id':5023,'value':[model]},{'id':58854,'value':[col]},
                             {'id':179078,'value':[printer]},{'id':179792,'value':[1]}]}
    ref={'title':model,'consumable_type':kind,'raw':{},'chip':None}
    return {'card':card,'ref':ref,'chip':chip,'printers':printers(card,ref)}


class Safety(unittest.TestCase):
    def test_tc_structured_printers_use_brand_and_title_not_dict_repr(self):
        c={'characteristics':[]}
        ref={'raw':{'printer_models':[{'brand':'Canon','title':'i-SENSYS LBP611'},'Canon LBP612']}}
        self.assertEqual(printers(c,ref),('tc',('CANONISENSYSLBP611','CANONLBP612')))

    def test_restart_gap_releases_lock_and_keeps_waiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);state=p/'apply_results.json'
            state.write_text(json.dumps({'finished':False}))
            sleeps=[]
            def advance(seconds):
                sleeps.append(seconds)
                state.write_text(json.dumps({'finished':True}))
            with patch.object(module,'BUNDLE_OUT',p),patch.object(module.time,'sleep',advance):
                lock=module.wait_previous()
                self.assertEqual(sleeps,[5]);lock.close()

    def test_stopped_write_never_starts_following_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);(p/'apply_results.json').write_text(json.dumps({'finished':False,'stopped_at':10}))
            with patch.object(module,'BUNDLE_OUT',p):
                with self.assertRaises(RuntimeError):module.wait_previous()

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
        self.assertIsNone(components_for(kit,cc)[1])
        self.assertIsNotNone(components_for(kit,cc,allow_colour_gaps=False)[1])

    def test_missing_set_and_missing_colours_inherit_consistent_chips(self):
        kit=rec(10,'CF530A-CF533A','Комплект картриджей')
        cc=[rec(i+1,f'CF53{i}A','Картридж',c,chip='с чипом' if i<3 else 'чип не указан')
            for i,c in enumerate(['черный','голубой','желтый','пурпурный'])]
        found,reason=components_for(kit,cc)
        self.assertIsNone(reason);self.assertEqual(len(found),4)
        self.assertEqual(module.colour_chip_resolution(kit,found),'с чипом')
        self.assertEqual(kit['chip'],'чип не указан')

    def test_majority_never_overrules_explicit_opposing_chip(self):
        kit=rec(10,'CF530A-CF533A','Комплект картриджей')
        cc=[rec(i+1,f'CF53{i}A','Картридж',c,chip='с чипом')
            for i,c in enumerate(['черный','голубой','желтый','пурпурный'])]
        for opposite in ['без чипа','чип без счётчика','противоречие']:
            cc[-1]['chip']=opposite
            self.assertIsNotNone(components_for(kit,cc)[1])

    def test_unknown_set_cannot_choose_between_known_chip_variants(self):
        kit=rec(10,'CF530A-CF533A','Комплект картриджей')
        cc=[rec(i+1,f'CF53{i}A','Картридж',c,chip='с чипом')
            for i,c in enumerate(['черный','голубой','желтый','пурпурный'])]
        cc.append(rec(20,'CF530A','Картридж',chip='без чипа'))
        self.assertIsNotNone(components_for(kit,cc)[1])

    def test_existing_colour_group_keeps_unknown_set_in_same_partition(self):
        from ops import wb_bundle_regroup as regroup
        rr=[rec(10,'CF530A-CF533A','Комплект картриджей')]+[
            rec(i+1,f'CF53{i}A','Картридж',c)
            for i,c in enumerate(['черный','голубой','желтый','пурпурный'])]
        for r in rr:r['card']['imtID']=100
        parent=rr[1]['card'];bb=[]
        for n in [2,6,10]:
            b=json.loads(json.dumps(parent));b['nmID']=1000+n;b['imtID']=b['nmID']
            b['vendorCode']=parent['vendorCode']+'X'+str(n)
            b['characteristics'][-1]['value']=[n];bb.append(b)
        source={'references':{r['card']['vendorCode']:r['ref'] for r in rr},'marks':{'0001':['с чипом']}}
        snapshot={'read_at':'test','cards':[r['card'] for r in rr]+bb}
        plan=regroup.prepare(snapshot,source)
        self.assertEqual(len(plan['operations']),1)
        self.assertIn([1,2,3,4,10,1002],plan['operations'][0]['partitions'])
        self.assertIn([1006,1010],plan['operations'][0]['partitions'])
        self.assertEqual(regroup.prepare(snapshot,source,allow_colour_gaps=False)['operations'],[])
        source['marks']['0004']=['без чипа']
        self.assertEqual(regroup.prepare(snapshot,source)['operations'],[])
        source['marks'].pop('0004');source['references'].pop('0004')
        self.assertEqual(regroup.prepare(snapshot,source)['operations'],[])

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
