# поток: rev
from unittest import TestCase, main
from unittest.mock import Mock, patch
from reports.rev_archive_fact_recovery import answer_from_native_mapping, public_product_attributes


class ExactArchivedFacts(TestCase):
    def setUp(self):
        self.row = dict(platform='yandex', kind='question', item_id='3489del', body='Это оригинал?')
        self.mapping = {'offer': {'offerId': '3489del', 'archived': True,
            'name': 'Чернила T6735 для принтеров Epson', 'vendor': 'Цифровой квадрат',
            'description': 'Чернила T6735 для принтеров Epson - это совместимый чернила.'},
            'mapping': {'marketModelId': 5397150218}}

    def test_exact_archive_with_historical_identity_proves_originality(self):
        result = answer_from_native_mapping(self.row, self.mapping, [5397150218])
        self.assertEqual(result[2], 'review')
        self.assertIn('не оригинальные Epson', result[1])
        self.assertTrue(result[4]['deterministic_verified'])
        self.assertTrue(result[4]['historical_publication_requires_owner'])

    def test_live_base_offer_never_substitutes_deleted_offer(self):
        self.mapping['offer']['offerId'] = '3489'
        self.assertIsNone(answer_from_native_mapping(self.row, self.mapping, [5397150218]))

    def test_model_identity_must_be_unambiguous_and_match(self):
        for history in ([], [1], [5397150218, 1]):
            with self.subTest(history=history):
                self.assertIsNone(answer_from_native_mapping(self.row, self.mapping, history))

    def test_printer_compatibility_does_not_imply_non_original(self):
        self.mapping['offer']['description'] = 'Чернила совместимы с принтером Epson L800.'
        self.assertIsNone(answer_from_native_mapping(self.row, self.mapping, [5397150218]))

    def test_originality_rule_cannot_answer_manufacturer_or_package_question(self):
        for text in ('Это оригинал? Кто производитель?', 'Что написано на коробке?'):
            self.row['body'] = text
            self.assertIsNone(answer_from_native_mapping(self.row, self.mapping, [5397150218]))

    def test_grounding_whitelists_public_fields(self):
        self.mapping['offer']['purchasePrice'] = {'value': 123}
        ground = answer_from_native_mapping(self.row, self.mapping, [5397150218])[4]
        self.assertNotIn('purchasePrice', str(ground))

    def test_brand_and_country_are_not_factory_manufacturer(self):
        result = public_product_attributes(
            [{'id': 85, 'values': [{'value': 'Цифровой квадрат'}]},
             {'id': 4389, 'values': [{'value': 'Китай'}]}],
            [{'id': 85, 'name': 'Бренд'}, {'id': 4389, 'name': 'Страна-изготовитель'}])
        self.assertEqual(result['brand']['values'], ['Цифровой квадрат'])
        self.assertNotIn('manufacturer', result)

    def test_archive_engine_uses_native_recovery_when_normal_card_missing(self):
        from reports.archive_questions import answer
        expected = answer_from_native_mapping(self.row, self.mapping, [5397150218])
        with patch('reports.rev_archive_fact_recovery.native_archive_answer', return_value=expected) as recover:
            self.assertEqual(answer(self.row, Mock(for_yandex=Mock(return_value=None))), expected)
        recover.assert_called_once_with(self.row)

    def test_archive_engine_keeps_fact_block_when_exact_recovery_fails(self):
        from reports.archive_questions import answer
        with patch('reports.rev_archive_fact_recovery.native_archive_answer', return_value=None):
            result = answer(self.row, Mock(for_yandex=Mock(return_value=None)))
        self.assertEqual(result[2], 'human')
        self.assertIn('3489del', result[4]['fact_block'])
        self.assertFalse(result[4]['deterministic_verified'])


if __name__ == '__main__':
    main()
