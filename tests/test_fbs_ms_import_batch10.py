# поток: inv
from decimal import Decimal
import unittest

from ops.fbs_ms_import_batch10 import SCOPE, base_code
from ops.fbs_ms_order_payload import Line, line_total


class BatchScopeTests(unittest.TestCase):
    def test_exact_batch_limit_and_family_code_rule(self):
        self.assertEqual(len(SCOPE), 10)
        self.assertEqual(len(set(SCOPE)), 10)
        for source, base in [('0019', '0019'), ('00191', '0019'),
                             ('34261', '3426'), ('58453', '5845'), ('239330', '2393')]:
            self.assertEqual(base_code(source), base)

    def test_ozon_percentage_discount_keeps_ms_kopeks(self):
        self.assertEqual(line_total(Line('x', Decimal(1), 836900, Decimal('5.66'))), 789531)
        self.assertEqual(line_total(Line('x', Decimal(1), 424300, Decimal('10.06'))), 381615)


if __name__ == '__main__':
    unittest.main()
