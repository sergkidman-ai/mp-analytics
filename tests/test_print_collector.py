# поток: print
from datetime import datetime,timedelta,timezone
import unittest
from uuid import uuid4

from print_service.clients import MS
from print_service.collector import Collector
from print_service.domain import PrintBlocked
from print_service.worker import sync_due
from shipment_guard.config import ORGS,AGENTS

NOW=datetime(2026,11,3,10,tzinfo=timezone.utc)


class SavedMS:
    def __init__(self, changed=False):
        self.changed,self.calls=changed,[]
        self.state=str(uuid4());self.orders={}
        wb=next(k for k,v in AGENTS.items() if v=='wb')
        for org,(account,_) in ORGS.items():
            self.orders[org]=[dict(id=str(uuid4()),name=str(int(account)*1000+i),
                state={'name':'Собирается'},deliveryPlannedMoment='2026-11-03 12:00:00',
                organization={'meta':{'href':MS+'/entity/organization/'+org}},
                agent={'meta':{'href':MS+'/entity/counterparty/'+wb}},
                store={'name':'Склад '+account,'meta':{'href':MS+'/entity/store/'+str(uuid4())}},
                demands=[{'meta':{'href':MS+'/entity/demand/'+str(uuid4())}}]) for i in range(21)]
    def ms_get(self,path,params=None):
        self.calls.append((path,params))
        if path.endswith('/metadata'):return {'states':[{'name':'Собирается','meta':{'href':MS+'/entity/customerorder/metadata/states/'+self.state}}]}
        org=next(o for o in self.orders if o in params['filter']);offset=params['offset'];rows=self.orders[org]
        return {'meta':{'size':len(rows)+(1 if self.changed and offset else 0)},'rows':rows[offset:offset+20]}


class CollectorTests(unittest.TestCase):
    def test_complete_pagination_and_no_export_or_labels(self):
        source=SavedMS();rows=Collector(source).collect(now=NOW)
        self.assertEqual(len(rows),42)
        self.assertEqual({r['account'] for r in rows},{'wb_acc1','wb_acc2'})
        self.assertTrue(all(r['demand_id'] for r in rows))
        self.assertTrue(all(path in ('/entity/customerorder/metadata','/entity/customerorder') for path,_ in source.calls))
        self.assertTrue(any(params and params['offset']==20 for _,params in source.calls))

    def test_partial_changed_snapshot_is_rejected(self):
        with self.assertRaises(PrintBlocked):Collector(SavedMS(changed=True)).collect(now=NOW)

    def test_missing_demand_stays_in_lower_list_with_explicit_error(self):
        source=SavedMS();next(iter(source.orders.values()))[0]['demands']=[]
        rows=Collector(source).collect(now=NOW)
        self.assertEqual(len(rows),42)
        self.assertTrue(any(r['source_error']=='linked_demand_ambiguous' for r in rows))

    def test_half_hour_deadline_and_moscow_midnight(self):
        state={'started_at':NOW,'day':NOW.date()}
        self.assertFalse(sync_due(state,NOW+timedelta(minutes=29,seconds=59)))
        self.assertTrue(sync_due(state,NOW+timedelta(minutes=30)))
        self.assertTrue(sync_due({'started_at':None,'day':None},NOW))
        before=datetime(2026,11,3,20,59,tzinfo=timezone.utc)
        self.assertTrue(sync_due({'started_at':before,'day':before.date()},before+timedelta(minutes=2)))
