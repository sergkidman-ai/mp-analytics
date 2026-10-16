"""Full inventory and write-safety regressions for colour laser completion."""
import copy
import unittest

from ops.wb_colour_laser_complete import build, colour_template
from tests.test_wb_component_gap_scan import fixture


class CompleteColourInventory(unittest.TestCase):
    def test_tc_printer_lists_do_not_hide_existing_colors(self):
        snap, source, models = fixture()
        models[0]['raw']['printer_models'].append({'brand': 'Brand', 'title': 'ExtraPrinter'})
        p = build(snap, source, models)
        self.assertEqual(len(p['rows']), 1)
        self.assertEqual(p['rows'][0]['missing_colors'], [])
        self.assertFalse(p['rows'][0]['missing_kit'])
        self.assertEqual(len(p['operations']), 1)

    def test_replacement_under_another_code_is_not_missing_inventory(self):
        snap, source, models = fixture()
        snap['cards'][1]['vendorCode'] = '9001'
        p = build(snap, source, models)
        self.assertEqual(p['rows'][0]['missing_colors'], [])
        self.assertEqual(p['rows'][0]['replacements'][0]['actual']['code'], '9001')
        self.assertEqual(len(p['operations']), 1)

    def test_same_model_other_resource_is_not_a_replacement(self):
        snap, source, models = fixture()
        for c in snap['cards'][:4]:
            c['characteristics'].append({'id': 88963, 'value': [1000]})
        for r in models[:4]:
            r['resource'] = 1000
        other = copy.deepcopy(snap['cards'][1]); other.update(nmID=100, imtID=100, vendorCode='9001')
        other['characteristics'][-1]['value'] = [5000]; snap['cards'].append(other)
        p = build(snap, source, models)
        self.assertFalse(any(c['card']['nm_id'] == 100 for row in p['rows'] for c in row['components']))

    def test_existing_primary_is_not_replaced_by_an_additional_offer(self):
        snap, source, models = fixture()
        extra = copy.deepcopy(snap['cards'][1]); extra.update(nmID=100, imtID=100, vendorCode='9001')
        snap['cards'].append(extra)
        p = build(snap, source, models)
        self.assertEqual(p['rows'][0]['replacements'], [])
        self.assertFalse(any(r['card']['nm_id'] == 100 for r in p['rows'][0]['components']))

    def test_zero_stock_does_not_remove_cards(self):
        snap, source, models = fixture()
        for c in snap['cards']:
            c['stock'] = 0
        self.assertEqual(len(build(snap, source, models)['operations']), 1)

    def test_existing_set_with_wrong_quantity_is_not_proposed_for_creation(self):
        snap, source, models = fixture()
        snap['cards'][-1]['characteristics'][3]['value'] = [1]
        p = build(snap, source, models)
        self.assertFalse(p['rows'][0]['missing_kit'])
        self.assertEqual(len(p['rows'][0]['kits']), 1)
        self.assertEqual(p['operations'], [])

    def test_mistyped_existing_four_item_offer_blocks_duplicate_creation(self):
        snap, source, models = fixture()
        models[-1].update(consumable_type='Картридж', color='K', title='Unverified pack')
        models[-1]['raw']['colors_count'] = 1
        snap['cards'][-1]['imtID'] = snap['cards'][0]['imtID']
        p = build(snap, source, models)
        self.assertFalse(p['rows'][0]['missing_kit'])
        self.assertEqual(len(p['rows'][0]['unverified_set_candidates']), 1)

    def test_unequal_wb_printers_still_block_a_move(self):
        snap, source, models = fixture()
        snap['cards'][1]['characteristics'][2]['value'].append('Other Printer')
        p = build(snap, source, models)
        self.assertEqual(p['operations'], [])
        self.assertTrue(any('списки принтеров' in reason for reason in p['rows'][0]['reasons']))

    def test_opposite_chips_are_never_in_one_operation(self):
        snap, source, models = fixture()
        models[1]['chip'] = 'nochip'
        p = build(snap, source, models)
        for op in p['operations']:
            self.assertFalse(1 in op['partitions'][0] and 2 in op['partitions'][0])

    def test_unknown_components_are_not_dropped_because_set_is_no_chip(self):
        snap, source, models = fixture()
        for r in models[:4]: r['chip'] = None
        models[-1]['chip'] = 'nochip'
        p = build(snap, source, models)
        self.assertEqual(p['covered_color_parent_skus'], 4)
        self.assertEqual(p['rows'][0]['chip'], 'чип не указан')
        self.assertEqual(p['rows'][0]['kits'], [])

    def test_foreign_group_member_is_preserved_without_blind_transfer(self):
        snap, source, models = fixture()
        foreign = copy.deepcopy(snap['cards'][0]); foreign.update(nmID=100, vendorCode='9999')
        snap['cards'].append(foreign)
        self.assertEqual(build(snap, source, models)['operations'], [])

    def test_unknown_model_is_not_an_instruction_to_create_a_product(self):
        snap, source, models = fixture()
        snap['cards'] = snap['cards'][1:]
        models = models[1:]
        p = build(snap, source, models)
        missing = p['rows'][0]['missing_colors'][0]
        self.assertEqual(missing['status'], 'model_unmapped')
        self.assertEqual(missing['expected'], [])

    def test_a_set_under_another_family_is_found_globally(self):
        snap, source, models = fixture()
        models[-1]['united_title'] = 'Another explicit set family'
        p = build(snap, source, models)
        self.assertFalse(p['rows'][0]['missing_kit'])
        self.assertEqual(len(p['operations']), 1)

    def test_same_set_model_for_another_printer_is_not_a_counterpart(self):
        snap, source, models = fixture()
        models[-1]['united_title'] = 'Different printer manufacturer'
        snap['cards'][-1]['characteristics'][2]['value'] = ['OtherBrand Printer9']
        p = build(snap, source, models)
        self.assertTrue(p['rows'][0]['missing_kit'])
        self.assertEqual(p['rows'][0]['kits'], [])

    def test_already_complete_is_not_repeated(self):
        snap, source, models = fixture()
        for c in snap['cards']:
            c['imtID'] = 42
        p = build(snap, source, models)
        self.assertEqual(p['operations'], [])
        self.assertEqual(p['rows'][0]['status'], 'complete')

    def test_missing_family_field_is_resolved_from_existing_exact_kit(self):
        snap, source, models = fixture()
        for c in snap['cards']:
            c['imtID'] = 42
        models[2]['united_title'] = None
        p = build(snap, source, models)
        self.assertEqual(p['unresolved_family'], [])
        self.assertEqual(p['rows'][0]['status'], 'complete')
        self.assertEqual(p['derived_families'][0]['code'], '0003')

    def test_deleted_primaries_with_living_bundles_are_not_omitted(self):
        snap, source, models = fixture()
        b = copy.deepcopy(snap['cards'][0]); b.update(nmID=20, imtID=20, vendorCode='0001X2')
        b['characteristics'][3]['value'] = [2]
        snap['cards'] = [b]
        p = build(snap, source, models)
        self.assertEqual(p['rows'][0]['status'], 'primary_cards_absent')
        self.assertEqual(p['rows'][0]['expected_black'][0]['code'], '0001')
        self.assertEqual(len(p['rows'][0]['missing_colors']), 4)

    def test_exact_partial_tc_family_is_unified_without_losing_colors(self):
        snap, source, models = fixture()
        models[2]['united_title'] = 'ABC100 partial MY'
        models[3]['united_title'] = 'ABC100 partial MY'
        # A second explicit family entry proves the subset's two models.
        for i in [2, 3]:
            extra = copy.deepcopy(models[i]); extra['external_code'] = '8' + extra['external_code'][1:]; extra['united_title'] = 'ABC100 DS'
            models.append(extra)
        p = build(snap, source, models)
        self.assertEqual(len(p['rows']), 1)
        self.assertEqual(p['rows'][0]['missing_colors'], [])

    def test_colour_template_preserves_xl_suffix(self):
        _, _, models = fixture()
        for r, co in zip(models[1:4], ['C', 'M', 'Y']): r['title'] = 'TK-' + co + '100XL'
        self.assertEqual(colour_template(models[1:4]), ('TK', '100XL'))

    def test_exact_kit_model_finds_black_in_separate_tc_family(self):
        snap, source, models = fixture()
        models[0]['united_title'] = 'ABC100K mono reference'
        p = build(snap, source, models)
        rows = [r for r in p['rows'] if r['components']]
        self.assertEqual(rows[0]['missing_colors'], [])
        self.assertEqual(len(p['operations']), 1)

    def test_explicit_single_cartridge_alias_is_not_an_unknown_family(self):
        snap, source, models = fixture()
        extra = copy.deepcopy(snap['cards'][1]); extra.update(nmID=100, imtID=100, vendorCode='9001')
        extra['characteristics'][0]['value'] = ['ABC100C/EquivalentC']
        ref = copy.deepcopy(models[1]); ref.update(external_code='9001', title='ABC100C/EquivalentC', united_title=None)
        snap['cards'].append(extra); models.append(ref)
        p = build(snap, source, models)
        self.assertEqual(p['unresolved_family'], [])
        self.assertEqual(len(p['operations']), 1)


if __name__ == '__main__':
    unittest.main()
