# поток: mkt
import unittest
from unittest.mock import Mock

from ops import ozon_autoadd_flag_audit as audit


def item(product_id, flag, *, old_flag=False):
    return {'product_id': product_id, 'offer_id': str(product_id),
            'price': {'auto_action_enabled': old_flag,
                      'auto_add_to_ozon_actions_list_enabled': flag}}


class AuditTests(unittest.TestCase):
    def test_counts_actual_auto_add_field_across_pages(self):
        api = Mock(calls=2)
        api.post.side_effect = [
            (200, {'items': [item(1, True), item(2, False)], 'cursor': 'next'}),
            (200, {'items': [item(3, None)], 'cursor': '', 'total': 3}),
        ]
        result = audit.scan_account('oz_acc1', api)
        self.assertEqual((result['enabled'], result['disabled'], result['unknown']), (1, 1, 1))
        self.assertEqual(result['examples'], ['1'])
        self.assertEqual(api.post.call_count, 2)
        self.assertTrue(all(c.args[0] == audit.PRICES_PATH for c in api.post.call_args_list))

    def test_rejects_incomplete_pagination(self):
        api = Mock(calls=2)
        api.post.side_effect = [
            (200, {'items': [item(1, False)], 'cursor': 'next'}),
            (200, {'items': [item(2, False)], 'cursor': 'next'}),
        ]
        with self.assertRaisesRegex(RuntimeError, 'incomplete pagination'):
            audit.scan_account('oz_acc1', api)

    def test_rejects_total_mismatch(self):
        api = Mock(calls=1)
        api.post.return_value = (200, {'items': [item(1, False)], 'cursor': '', 'total': 2})
        with self.assertRaisesRegex(RuntimeError, 'received 1 of total 2'):
            audit.scan_account('oz_acc1', api)


if __name__ == '__main__':
    unittest.main()
