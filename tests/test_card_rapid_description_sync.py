import unittest

from tools.card_rapid_description_sync import IDS, build_plan, checked_write_payload
from core import ms_api


class RapidDescriptionTests(unittest.TestCase):
    def card(self, **overrides):
        row = {'id': 'test', 'article': 'SKU', 'externalCode': '0123', 'description': 'old',
               'supplier': {'meta': {'href': ms_api.BASE + '/entity/counterparty/' + sorted(IDS)[0]}}}
        return {**row, **overrides}

    def item(self, **overrides):
        return {'CodeID': 'SKU', 'Name': 'new with chip', 'UnitsFree': 0, **overrides}

    def test_zero_stock_is_included_and_existing_equal_skipped(self):
        plan, skipped = build_plan([self.card()], [self.item()])
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]['api_free_stock'], 0)
        self.assertEqual(set(checked_write_payload(self.card(), plan[0])), {'id', 'description'})
        plan, skipped = build_plan([self.card(description='new with chip')], [self.item()])
        self.assertFalse(plan)
        self.assertEqual(skipped['already_equal'], 1)

    def test_archived_foreign_ambiguous_and_empty_names_excluded(self):
        plan, _ = build_plan([self.card(archived=True), self.card(supplier={})], [self.item()])
        self.assertFalse(plan)
        self.assertFalse(build_plan([self.card()], [self.item(), self.item(Name='other')])[0])
        self.assertFalse(build_plan([self.card()], [self.item(Name='')])[0])
        self.assertFalse(build_plan([self.card()], [self.item(CodeID='DIFFERENT')])[0])

    def test_case_and_whitespace_matching_preserves_actual_article(self):
        plan, _ = build_plan([self.card(article='s ku')], [self.item()])
        self.assertEqual(plan[0]['article'], 's ku')

    def test_changed_identity_or_description_blocks_write(self):
        plan, _ = build_plan([self.card()], [self.item()])
        for changes in ({'article': 'OTHER'}, {'externalCode': '9999'}, {'supplier': {}},
                        {'archived': True}, {'description': 'concurrent edit'}):
            with self.assertRaises(RuntimeError):
                checked_write_payload(self.card(**changes), plan[0])


if __name__ == '__main__':
    unittest.main()
