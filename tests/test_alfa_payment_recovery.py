# поток: inv
import unittest
from unittest.mock import patch

from invoice_bot import alfa_payment_draft as bank


class AlfaRecoveryTests(unittest.TestCase):
    def build(self, rate, purpose):
        with patch.object(bank, '_payer_block', return_value={}), \
             patch.object(bank.pd, 'base_payment', return_value={'externalId':'fixture','purpose':purpose}):
            return bank.build_payment({'org_inn':'7807355364','vat_rate':rate}, {'env':'prod'})

    def test_included_tax_is_structured_and_draft_unsigned(self):
        payload = self.build(22, 'Аванс. В т.ч. НДС 22% - 9016.39 руб.')
        self.assertEqual(payload['vat'], {'rate':'22','type':'INCLUDED'})
        self.assertNotIn('digestSignatures', payload)
        self.assertNotIn('amount', payload['vat'])

    def test_no_vat_uses_required_bank_wording(self):
        payload = self.build(0, 'Оплата по счетам №123. Без НДС.')
        self.assertEqual(payload['vat'], {'rate':'0','type':'NO_VAT'})
        self.assertIn('НДС не облагается',payload['purpose'])

    def test_no_vat_cannot_overflow_bank_purpose_limit(self):
        with self.assertRaisesRegex(ValueError,'лимит'):
            self.build(0,'x'*202+'Без НДС.')

    def test_recovery_hold_blocks_http_even_with_prod_permission(self):
        with patch.dict('os.environ',{'ALFA_PAYMENT_PROD_READY':'1'}), patch.object(bank,'_session') as session:
            with self.assertRaisesRegex(RuntimeError,'сверки'):
                bank.send_one({'recovery_hold':True},dry_run=False,cfg={'env':'prod'})
            session.assert_not_called()

    def test_unknown_supplier_tax_blocks_live_request(self):
        with patch.dict('os.environ',{'ALFA_PAYMENT_PROD_READY':'1'}), patch.object(bank,'_session') as session:
            with self.assertRaisesRegex(RuntimeError,'НДС'):
                bank.send_one({'org_inn':'7807355364','kind':'advance'},dry_run=False,cfg={'env':'prod'})
            session.assert_not_called()
