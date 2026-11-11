# поток: inv
import unittest
import uuid
import tempfile
from pathlib import Path
from unittest.mock import patch

import invoice_bot
invoice_bot.__path__ = [str(Path(__file__).resolve().parents[1] / "invoice_bot")]
from invoice_bot import payment_draft as pd, sber_payment_draft as sber
from ops.restore_diskver_payment_data import history_rows
from collectors import sber_auth


class RecoveryIdentityTests(unittest.TestCase):
    def test_legacy_id_is_unchanged(self):
        ns = uuid.uuid4()
        self.assertEqual(pd.external_id({'id': 325}, ns), str(uuid.uuid5(ns, '325')))

    def test_restored_sequence_cannot_reuse_legacy_external_id(self):
        ns = uuid.uuid4()
        row = {'id': 325, 'bank_external_seed': uuid.uuid4()}
        self.assertNotEqual(pd.external_id(row, ns), pd.external_id({'id': 325}, ns))
        self.assertEqual(pd.external_id(row, ns), pd.external_id(dict(row, id=1), ns))

    def test_distinct_seeds_and_banks_remain_distinct(self):
        row = {'id': 1, 'bank_external_seed': uuid.uuid4()}
        ns = uuid.uuid4()
        self.assertNotEqual(pd.external_id(row, ns), pd.external_id(row, uuid.uuid4()))
        self.assertNotEqual(pd.external_id(row, ns), pd.external_id(dict(row, bank_external_seed=uuid.uuid4()), ns))

    def test_hold_precedes_payload_and_bank_calls_even_with_live_gates(self):
        with patch.dict('os.environ', {'SBER_PAYMENT_PROD_READY': '1', 'SBER_PAYMENT_APPLY': '1'}), \
             patch.object(sber, 'build_payment') as build, patch.object(sber.sber_auth, 'api') as api:
            with self.assertRaisesRegex(RuntimeError, 'сверка'):
                sber.send_one({'id': 1, 'org_inn': '7811803918', 'recovery_hold': True}, dry_run=False)
            build.assert_not_called()
            api.assert_not_called()

    def test_hold_allows_readonly_payload_review(self):
        with patch.object(sber, 'build_payment', return_value={'externalId': 'fixture'}) as build, \
             patch.object(sber.sber_auth, 'api') as api:
            result = sber.send_one({'id': 1, 'org_inn': '7811803918', 'recovery_hold': True}, dry_run=True)
            self.assertTrue(result['ok'])
            build.assert_called_once()
            api.assert_not_called()


class SharedTokenStorageTests(unittest.TestCase):
    def test_worktree_and_main_share_one_token_store(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main, worktree = root / 'main', root / 'bank'
            main.mkdir()
            worktree.mkdir()
            (main / '.env').touch()
            (worktree / '.env').symlink_to(main / '.env')
            self.assertEqual(sber_auth._token_root(main), main)
            self.assertEqual(sber_auth._token_root(worktree), main)

    def test_missing_env_uses_canonical_project(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(sber_auth._token_root(Path(directory)), Path('/opt/mp-analytics'))


class RecoveryHistoryTests(unittest.TestCase):
    ORG = 'https://api.moysklad.ru/api/remap/1.2/entity/organization/diskver'

    def payment(self, **changes):
        return dict({'id': 'ms-id', 'sum': 10001, 'moment': '2026-11-01 10:00:00',
                     'applicable': True, 'organization': {'meta': {'href': self.ORG}},
                     'agent': {'inn': 'supplier', 'meta': {'href': 'agent'}}, 'operations': []}, **changes)

    def test_amount_uses_exact_kopecks_and_document_identity(self):
        row = history_rows([self.payment()], {'supplier'}, self.ORG)[0]
        self.assertEqual(row['amount'], '100.01')
        self.assertEqual(row['ms_payment_id'], 'ms-id')

    def test_other_organization_and_cross_org_receipts_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'organization'):
            history_rows([self.payment(organization={'meta': {'href': 'other'}})], {'supplier'}, self.ORG)
        with self.assertRaisesRegex(ValueError, 'organization'):
            history_rows([self.payment(operations=[{'organization': {'meta': {'href': 'other'}}}])], {'supplier'}, self.ORG)

    def test_unposted_and_non_supplier_payments_are_excluded(self):
        self.assertEqual(history_rows([self.payment(applicable=False)], {'supplier'}, self.ORG), [])
        self.assertEqual(history_rows([self.payment()], {'different'}, self.ORG), [])

    def test_duplicate_document_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            history_rows([self.payment(), self.payment()], {'supplier'}, self.ORG)


if __name__ == '__main__':
    unittest.main()
