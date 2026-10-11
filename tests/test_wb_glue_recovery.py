"""Safety of recovery after rejected, lost-response and interrupted writes."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ops import wb_glue_recovery as r
from ops import wb_glue_supervisor as supervisor
from ops.wb_bundle_plan import fingerprint


def fixture():
    cards=[{'nmID':1,'imtID':10,'vendorCode':'0001','subjectID':1820,'title':'One',
            'description':'original','characteristics':[]},
           {'nmID':2,'imtID':11,'vendorCode':'0001X2','subjectID':1820,'title':'Two',
            'description':'original','characteristics':[]}]
    op={'id':10,'anchor':1,'before_cards':cards,'before_groups':{'10':[1],'11':[2]},
        'before_fingerprint':fingerprint(cards),'partitions':[[1,2]],'preferred_roots':{'10':1},'rules':['test']}
    attempt={'status':'attempt','body':{'targetIMT':10,'nmIDs':[2]},
             'before_groups':{'10':[1],'11':[2]}}
    return op,[attempt]


class Client:
    def __init__(self,cards):self.cards=cards
    def group(self,root):return copy.deepcopy([c for c in self.cards if c['imtID']==root])


class Safety(unittest.TestCase):
    def fresh(self,cards,op,events):
        with patch.object(r.regroup,'by_nm',lambda client,nm:next(copy.deepcopy(c) for c in client.cards if c['nmID']==nm)),patch.object(r.time,'sleep'):
            return r.make_fresh(op,Client(cards),events)

    def test_rejected_attempt_unchanged_can_resume(self):
        op,events=fixture();fresh,proof=self.fresh(op['before_cards'],op,events)
        self.assertEqual(proof['explanation'],'recorded_attempt_unchanged')
        self.assertEqual(fresh['before_groups'],op['before_groups'])

    def test_lost_response_already_applied_uses_actual_membership(self):
        op,events=fixture();cards=copy.deepcopy(op['before_cards']);cards[1]['imtID']=10
        fresh,proof=self.fresh(cards,op,events)
        self.assertEqual(proof['explanation'],'recorded_attempt_applied')
        self.assertEqual(fresh['before_groups'],{'10':[1,2]})

    def test_accepted_detach_is_discovered_not_repeated(self):
        op,events=fixture();op['before_cards'][1]['imtID']=10
        op['before_groups']={'10':[1,2]};op['before_fingerprint']=fingerprint(op['before_cards'])
        events=[{'status':'attempt','body':{'nmIDs':[2]},'before_groups':{'10':[1,2]}}]
        cards=copy.deepcopy(op['before_cards']);cards[1]['imtID']=12
        fresh,proof=self.fresh(cards,op,events)
        self.assertEqual(proof['explanation'],'recorded_attempt_applied')
        self.assertEqual(fresh['before_groups'],{'10':[1],'12':[2]})

    def test_foreign_member_prevents_recovery(self):
        op,events=fixture();cards=copy.deepcopy(op['before_cards'])
        cards.append({**cards[0],'nmID':3})
        with self.assertRaises(r.UnsafeRecovery):self.fresh(cards,op,events)

    def test_content_change_prevents_recovery(self):
        op,events=fixture();cards=copy.deepcopy(op['before_cards']);cards[0]['title']='external edit'
        with self.assertRaises(r.UnsafeRecovery):self.fresh(cards,op,events)

    def test_unexplained_membership_change_prevents_recovery(self):
        op,events=fixture();cards=copy.deepcopy(op['before_cards']);cards[1]['imtID']=12
        with self.assertRaises(r.UnsafeRecovery):self.fresh(cards,op,events)

    def test_membership_changes_without_any_write_are_not_repaired(self):
        op,_=fixture();cards=copy.deepcopy(op['before_cards']);cards[1]['imtID']=10
        with self.assertRaises(r.UnsafeRecovery):self.fresh(cards,op,[])

    def test_unstable_independent_reads_prevent_writes(self):
        op,events=fixture();later=copy.deepcopy(op['before_cards']);later[1]['imtID']=10
        with patch.object(r,'read_complete',side_effect=[op['before_cards'],later]),patch.object(r.time,'sleep'):
            with self.assertRaises(r.RecoveryNotReady):r.make_fresh(op,None,events)

    def test_retryable_errors_exclude_authorization_and_invalid_requests(self):
        import json
        def response(status,text='Internal server error'):
            return [{'status':'response','http':status,'response':json.dumps({'error':True,'errorText':text})}]
        self.assertTrue(r.retryable({},response(400)))
        self.assertTrue(r.retryable({},response(500)))
        self.assertTrue(r.retryable({},response(429)))
        self.assertFalse(r.retryable({},response(403)))
        self.assertFalse(r.retryable({},response(400,'invalid fields')))

    def test_process_crash_can_be_found_in_durable_journal(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp);(out/'journal.jsonl').write_text(json.dumps({'id':10,'status':'attempt'})+'\n')
            self.assertEqual(supervisor.pending_id(out,{'results':[]}),10)
            self.assertIsNone(supervisor.pending_id(out,{'results':[{'id':10,'status':'confirmed'}]}))

    def test_finished_membership_requires_no_additional_move(self):
        from ops import wb_bundle_regroup as regroup
        op,events=fixture();cards=copy.deepcopy(op['before_cards']);cards[1]['imtID']=10
        fresh,_=self.fresh(cards,op,events)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'docs/reports').mkdir(parents=True)
            with patch.object(regroup,'ROOT',root),patch.object(regroup,'journal'),patch.object(regroup,'perform') as move:
                result=regroup.execute(fresh,Client(cards))
            self.assertEqual(result['status'],'confirmed');move.assert_not_called()

    def test_supervisor_persists_confirmed_recovery_and_clears_stop(self):
        import json
        from ops import wb_bundle_regroup as regroup
        op,events=fixture();events.append({'id':10,'status':'response','http':400,
            'response':json.dumps({'error':True,'errorText':'Internal server error'})})
        for e in events:e['id']=10
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)
            (out/'plan.json').write_text(json.dumps({'operations':[op]}))
            (out/'apply_results.json').write_text(json.dumps({'results':[],'stopped_at':10}))
            (out/'journal.jsonl').write_text('\n'.join(json.dumps(e) for e in events)+'\n')
            with patch.object(supervisor,'Content',lambda:Client(op['before_cards'])),patch.object(supervisor.time,'sleep'),\
                 patch.object(regroup,'by_nm',lambda client,nm:next(copy.deepcopy(c) for c in client.cards if c['nmID']==nm)),\
                 patch.object(regroup,'execute',return_value={'id':10,'status':'confirmed'}),patch.object(regroup,'report'):
                self.assertTrue(supervisor.recover(out,'bundle',3))
            state=json.loads((out/'apply_results.json').read_text())
            self.assertNotIn('stopped_at',state);self.assertNotIn('auto_recovering',state)
            self.assertEqual(state['results'],[{'id':10,'status':'confirmed'}])
            self.assertTrue((out/'auto_recovery_10_1.json').exists())


if __name__=='__main__':unittest.main()
