# поток: ev
import unittest
from ops.wb_loss_cost import allocate

def item(n,qty,orders):
    return dict(document_id='doc'+str(n),warehouse=str(n),nm_id=n,vendor_code='0019',qty=qty,
        evidence=[dict(demand_id=k,moment=date,usable_candidate=True) for k,date in orders])

class AllocationTests(unittest.TestCase):
    def test_latest_shipments(self):
        r=allocate([item(1,2,[('old','2025-01-01T00:00:00+03:00'),('new','2026-07-15T00:00:00+03:00'),('middle','2026-06-01T00:00:00+03:00')])])
        self.assertEqual([e['demand_id'] for e in r[0]['selected']],['new','middle'])
    def test_no_reuse_across_warehouses(self):
        orders=[('one','2026-01-01T00:00:00+03:00')]
        r=allocate([item(1,1,orders),item(2,1,orders)])
        self.assertEqual(sum(len(i['selected']) for i in r),1)
        self.assertEqual(sum(i['unmatched_qty'] for i in r),1)
    def test_reassign_to_avoid_greedy_shortfall(self):
        a=('new','2026-07-15T00:00:00+03:00');b=('old','2026-01-01T00:00:00+03:00')
        r=allocate([item(1,1,[a,b]),item(2,1,[a])])
        self.assertEqual([[e['demand_id'] for e in i['selected']] for i in r],[['old'],['new']])
    def test_cost_availability_does_not_change_selection(self):
        i=item(1,1,[('latest','2026-07-15T00:00:00+03:00'),('old','2026-01-01T00:00:00+03:00')])
        i['evidence'][0]['cost_rub']=None;i['evidence'][1]['cost_rub']='100'
        self.assertEqual(allocate([i])[0]['selected'][0]['demand_id'],'latest')
    def test_rejected_evidence_not_allocated(self):
        i=item(1,1,[('sold','2026-07-15T00:00:00+03:00')]);i['evidence'][0]['usable_candidate']=False
        self.assertEqual(allocate([i])[0]['unmatched_qty'],1)

if __name__=='__main__':unittest.main()
