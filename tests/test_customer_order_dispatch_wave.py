# поток: inv
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import requests

from tools import customer_order_dispatch_wave as d
from shipment_guard.config import ORGS, AGENTS


def sample():
    def ref(kind, uid):return {'meta': {'href': 'https://example/entity/'+kind+'/'+uid}}
    return {'name': '123', 'id': 'order-id', 'applicable': True,
        'state': {'name': 'Подтвержден'}, 'deliveryPlannedMoment': '2026-11-03 00:00:00.000',
        'organization': ref('organization', next(iter(ORGS))),
        'agent': ref('counterparty', next(k for k,v in AGENTS.items() if v=='wb')),
        'store': ref('store', 's'), 'sum': 100,
        'positions': {'meta': {'size': 1}, 'rows': [
            {'assortment': {'meta': {'type':'product','href':'https://example/entity/product/p'}},
             'quantity': 2, 'price': 50}]}}


class DispatchWaveTests(TestCase):
    def test_batch_limit_stops_before_api_reads_or_writes(self):
        with patch.object(d.ms_api, 'get') as read:
            with self.assertRaisesRegex(ValueError, '1_to_24'):
                d.prepare_batch(list(range(25)), 1, {}, {})
        read.assert_not_called()

    def test_future_date_and_processed_state_are_rejected(self):
        order=sample();order['deliveryPlannedMoment']='2026-11-05 00:00:00'
        with self.assertRaisesRegex(ValueError,'wrong_planned_date'):d.basic(order)
        order=sample();order['state']['name']='Собирается'
        with self.assertRaisesRegex(ValueError,'already_processed'):d.basic(order)

    def test_multiple_real_components_are_accepted(self):
        order=sample();order['positions']['rows'].append(deepcopy(order['positions']['rows'][0]))
        order['positions']['meta']['size']=2
        d.basic(order)

    def test_existing_demand_blocks_status_write(self):
        order=sample();order['demands']=[{'meta':{'href':'https://example/entity/demand/d'}}]
        with self.assertRaisesRegex(ValueError,'linked_demand_exists'):d.basic(order)

    def test_supply_cargo_mismatch_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'cargo_type_mismatch'):
            d.compatible({'cargoType':2,'crossBorderType':0},{'cargoType':1,'crossBorderType':0})
        d.compatible({'cargoType':0,'crossBorderType':None},{'cargoType':1,'crossBorderType':0})

    def test_timeout_assignment_is_reconciled_without_second_patch(self):
        before={'platform':'wb','account':'wb_acc1','supply_id':'WB-GI-303580630',
                'included':False,'status':{'supplierStatus':'new'}}
        after={**before,'included':True,'status':{'supplierStatus':'confirm'}}
        with TemporaryDirectory() as tmp, patch.object(d,'market',side_effect=[before,after]), \
             patch.object(d.requests,'patch',side_effect=requests.Timeout) as write, \
             patch.dict(d.os.environ,{'WB_TOKEN_ACC1':'local-test'}):
            result=d.wb_assign(sample(),Path(tmp))
        self.assertTrue(result['included']);self.assertEqual(write.call_count,1)

    def test_market_and_ozon_have_no_artificial_status_write_or_repeated_read(self):
        for platform in ('yandex','ozon'):
            before={'platform':platform,'action':'already_at_assembly_stage_no_transition'}
            with TemporaryDirectory() as tmp, patch.object(d,'market') as read, \
                 patch.object(d.requests,'patch') as write:
                self.assertEqual(d.wb_assign(sample(),Path(tmp),before),before)
            read.assert_not_called();write.assert_not_called()

    def test_changed_composition_is_blocked(self):
        original=sample();current=deepcopy(original)
        current['positions']['rows'][0]['quantity']=3
        with self.assertRaisesRegex(AssertionError,'positions changed'):
            d.fixed(current,original)
