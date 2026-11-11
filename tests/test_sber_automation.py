# поток: inv
import contextlib
import datetime as dt
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import run_inv
from invoice_bot import payment_draft as pd, sber_payment_draft as bank
from collectors import sber_auth as auth
from ops import sber_secret_rotate as rotate
from ops.diskver_bank_schedule import commands


class VatContractTests(unittest.TestCase):
    def test_bank_fields_match_purpose_tax(self):
        with patch.object(bank, '_payer_block', return_value={}), \
             patch.object(bank.pd, 'base_payment', return_value={'externalId': 'fixture'}):
            result = bank.build_payment({'amount': 50000, 'vat_rate': 22, 'org_inn': '7811803918'})
        self.assertEqual(result['vat'], {'type': 'INCLUDED', 'rate': '22', 'amount': 9016.39})
        self.assertIn('9016.39', pd.vat_clause(50000, [], 22))
        self.assertEqual(str(pd.vat_amount('25000', 22)), '4508.20')
        self.assertEqual(str(pd.vat_amount('4557.88', 22)), '821.91')

    def test_no_vat_is_explicit(self):
        with patch.object(bank, '_payer_block', return_value={}), \
             patch.object(bank.pd, 'base_payment', return_value={'externalId': 'fixture'}):
            result = bank.build_payment({'amount': 9300, 'vat_rate': 0, 'org_inn': '7811803918'})
        self.assertEqual(result['vat'], {'type': 'NO_VAT', 'rate': '0', 'amount': 0.0})

    def test_unknown_vat_blocks_live_supplier_request(self):
        with patch.dict('os.environ', {'SBER_PAYMENT_PROD_READY': '1'}), \
             patch.object(bank.sber_auth, 'api') as api:
            with self.assertRaisesRegex(RuntimeError, 'НДС'):
                bank.send_one({'id': 1, 'org_inn': '7811803918', 'kind': 'advance'}, dry_run=False)
            api.assert_not_called()


class TokenRefreshTests(unittest.TestCase):
    def test_refresh_rechecks_cache_after_lock(self):
        now = dt.datetime.now(dt.timezone.utc)
        old = {'access_token': 'expired', 'expires_at': (now-dt.timedelta(hours=1)).isoformat()}
        fresh = {'access_token': 'fresh', 'expires_at': (now+dt.timedelta(hours=1)).isoformat()}
        with patch.object(auth, 'load', side_effect=[old, fresh]), \
             patch.object(auth, '_token_lock', side_effect=contextlib.nullcontext), \
             patch.object(auth, '_token_call') as call:
            self.assertEqual(auth.access_token(), 'fresh')
            call.assert_not_called()

    def test_transport_errors_never_expose_secret_query_parameters(self):
        session = Mock()
        session.post.side_effect = auth.requests.ConnectionError('url?client_secret=private-fixture')
        with patch.object(auth, 'cfg', return_value={'client_id': 'x', 'client_secret': 'private-fixture'}), \
             patch.object(auth, 'session', return_value=session):
            with self.assertRaises(RuntimeError) as caught:
                auth._token_call({'grant_type': 'refresh_token'})
        self.assertNotIn('private-fixture', str(caught.exception))

    def test_partial_token_pair_does_not_destroy_stored_refresh(self):
        session = Mock()
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {'access_token': 'only-access'}
        with patch.object(auth, 'cfg', return_value={'client_id': 'x', 'client_secret': 'fixture'}), \
             patch.object(auth, 'session', return_value=session), patch.object(auth, '_save') as save:
            with self.assertRaisesRegex(RuntimeError, 'неполную'):
                auth._token_call({})
            save.assert_not_called()


class SberScheduleTests(unittest.TestCase):
    def test_sber_only_never_loads_alfa_accounts_or_relinks(self):
        with patch.object(run_inv, 'run_sber', return_value=0) as sber, \
             patch.object(run_inv, 'accounts', side_effect=AssertionError('Alfa must not run')), \
             patch.object(run_inv, 'relink', side_effect=AssertionError('Alfa must not relink')):
            self.assertEqual(run_inv.main(['--sber-only', '--today']), 0)
            sber.assert_called_once()

    def test_senders_are_scoped_to_diskver_and_supplier_kinds(self):
        for mode in ('morning', 'evening'):
            for command in commands(mode):
                if 'payment_autosend.py' in ' '.join(command):
                    self.assertEqual(command[command.index('--org')+1], '7811803918')
                    self.assertNotIn('--no-tg', command)
                    self.assertNotIn('all', command)

    def test_rotation_logs_no_secret_characters(self):
        self.assertNotIn('private-fixture', rotate.tail('private-fixture'))


if __name__ == '__main__':
    unittest.main()
