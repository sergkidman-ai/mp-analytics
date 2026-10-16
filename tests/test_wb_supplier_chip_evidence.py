# поток: mkt
import unittest
from ops.wb_supplier_chip_evidence import recognize


class SupplierChipEvidenceTest(unittest.TestCase):
    def test_existing_supplier_spellings(self):
        cases = [
            ('б/ч', '', 'без чипа'),
            ('w/o chip', '', 'без чипа'),
            ('необходим чип', '', 'без чипа'),
            ('чип отдельно', '', 'без чипа'),
            ('чип безлимит', '', 'чип без счётчика'),
            ('с чипом без счётчика', '', 'чип без счётчика'),
            ('с многоразовым чипом Longlife', '', 'с чипом'),
            ('W/chip', 'blossom', 'без чипа'),
            ('W/chip', 'sakura', 'с чипом'),
        ]
        for text, supplier, expected in cases:
            with self.subTest(text=text, supplier=supplier):
                self.assertEqual(recognize(text, supplier)['classes'], [expected])

    def test_silence_and_template_are_not_evidence(self):
        for text in ('Q2612A', '', 'не указан', 'чип не указан', 'не требуется чип'):
            with self.subTest(text=text):
                self.assertEqual(recognize(text)['classes'], [])

    def test_conflicting_explicit_variants_are_preserved(self):
        self.assertEqual(set(recognize('без чипа; с чипом')['classes']),
                         {'без чипа', 'с чипом'})


if __name__ == '__main__':
    unittest.main()
