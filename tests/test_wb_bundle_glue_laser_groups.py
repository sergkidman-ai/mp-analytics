# поток: mkt
"""Guard against changing existing groups when adding own laser bundles."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from ops.wb_bundle_glue_laser_groups import build_plan, check_before, validate, apply_plan


def card(nm, code, qty=1, imt=None):
    return {'nmID': nm, 'vendorCode': code, 'imtID': imt or nm * 10, 'subjectID': 1820,
            'title': f'Картридж TEST {qty} шт', 'description': 'Чип не оговорён',
            'characteristics': [{'id': 179792, 'value': [str(qty)]},
                {'id': 5023, 'value': ['TEST']}, {'id': 58854, 'value': ['Черный']},
                {'id': 88963, 'value': ['1000']}, {'id': 179078, 'value': ['Printer A', 'Printer B']}]}


class LaserGroupSafetyTests(unittest.TestCase):
    def setUp(self):
        self.parent = card(1, '3351', imt=10)
        self.neighbor = card(9, '9999', imt=10)
        self.bundles = [card(2, '3351X2', 2), card(3, '3351X6', 6), card(4, '3351X10', 10)]
        self.ref = {'title': 'TEST', 'consumable_type': 'Картридж', 'chip': None,
                    'raw': {'assignment': 'для лазерного принтера'}}

    def plan(self, extra=()):
        return build_plan({'read_at': '2026-10-07', 'cards': [self.parent, self.neighbor, *self.bundles, *extra]},
                          {'3351': self.ref}, {'3351': set()})

    def test_existing_target_members_are_preserved(self):
        op = self.plan()['operations'][0]
        self.assertEqual(op['expected_ids'], [1, 2, 3, 4, 9])
        self.assertEqual(op['body']['nmIDs'], [2, 3, 4])
        validate(op)

    def test_ink_cartridges_are_outside_scope(self):
        self.ref['raw']['assignment'] = 'для струйного принтера'
        self.assertFalse(self.plan()['operations'])

    def test_group_limit_blocks_whole_group(self):
        extra = [card(n, str(4000+n), imt=10) for n in range(100, 128)]
        plan = self.plan(extra)
        self.assertFalse(plan['operations'])
        self.assertIn('30', plan['review'][0]['reason'])

    def test_foreign_source_blocks_whole_family(self):
        plan = self.plan([card(8, '8888', imt=self.bundles[0]['imtID'])])
        self.assertFalse(plan['operations'])
        self.assertIn('чужие', plan['review'][0]['reason'])

    def test_bundle_model_difference_blocks_family(self):
        self.bundles[0]['characteristics'][1]['value'] = ['OTHER']
        self.assertFalse(self.plan()['operations'])

    def test_chip_contradiction_blocks_family(self):
        self.parent['description'] = 'С чипом'
        self.bundles[1]['description'] = 'Без чипа'
        self.assertFalse(self.plan()['operations'])

    def test_live_neighbor_changes_rejected(self):
        op = self.plan()['operations'][0]
        groups = {imt: [copy.deepcopy(c) for c in op['before_cards'] if c['imtID'] == int(imt)]
                  for imt in op['before_groups']}
        groups['10'][1]['title'] = 'Новые поля соседа'
        self.assertFalse(check_before(op, groups)[0])
        groups['10'].append(card(7, '7777', imt=10))
        self.assertFalse(check_before(op, groups)[0])

    def test_tampered_request_cannot_move_neighbor(self):
        op = self.plan()['operations'][0]
        op['body']['nmIDs'] = [2, 3, 4, 9]
        with self.assertRaises(RuntimeError):
            validate(op)

    def test_ambiguous_write_is_never_retried(self):
        plan = self.plan(); op = plan['operations'][0]
        client = Mock()
        client.group.side_effect = lambda imt: [c for c in op['before_cards'] if c['imtID'] == imt]
        client.move.side_effect = requests.Timeout()
        with tempfile.TemporaryDirectory() as directory, patch('ops.wb_bundle_glue_laser_groups.OUT', Path(directory)):
            with self.assertRaises(requests.Timeout):
                apply_plan(plan, client)
        self.assertEqual(client.move.call_count, 1)

    def test_accepted_move_preserves_full_target(self):
        plan = self.plan(); op = plan['operations'][0]
        client = Mock(); written = [False]
        def group(imt):
            if written[0] and imt == 10:
                return [{**c, 'imtID': 10} for c in op['before_cards']]
            return [c for c in op['before_cards'] if c['imtID'] == imt]
        def move(body):
            written[0] = True
            response = Mock(); response.status_code = 200; response.text = '{}'; response.content = b'{}'; response.json.return_value = {}
            return response
        client.group.side_effect = group; client.move.side_effect = move
        with tempfile.TemporaryDirectory() as directory, patch('ops.wb_bundle_glue_laser_groups.OUT', Path(directory)):
            result = apply_plan(plan, client)
        self.assertEqual(result[0]['group_size'], 5)


if __name__ == '__main__':
    unittest.main()
