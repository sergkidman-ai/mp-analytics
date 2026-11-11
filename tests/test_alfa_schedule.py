# поток: inv
import unittest
from unittest.mock import patch

import run_inv
from ops import diskver_bank_schedule as schedule


class AlfaScheduleTests(unittest.TestCase):
    def test_alfa_only_never_calls_sber_or_global_relink(self):
        with patch.object(run_inv,'apply_mode',return_value=(True,'fixture')), \
             patch.object(run_inv,'accounts',return_value=['owned-account']), \
             patch.object(run_inv,'dates',return_value=['2026-11-06']), \
             patch.object(run_inv,'run_day',return_value=(3,{'errors':0})) as day, \
             patch.object(run_inv,'run_sber') as sber,patch.object(run_inv,'relink') as relink:
            self.assertEqual(run_inv.main(['--alfa-only','--today']),0)
            day.assert_called_once_with('owned-account','2026-11-06',True)
            sber.assert_not_called();relink.assert_not_called()

    def test_alfa_errors_fail_the_service(self):
        with patch.object(run_inv,'apply_mode',return_value=(True,'fixture')), \
             patch.object(run_inv,'accounts',return_value=['owned-account']), \
             patch.object(run_inv,'dates',return_value=['2026-11-06']), \
             patch.object(run_inv,'run_day',return_value=(1,{'errors':1})):
            self.assertEqual(run_inv.main(['--alfa-only','--today']),1)

    def test_conflicting_bank_flags_cannot_run(self):
        with patch.object(run_inv,'run_day') as day,patch.object(run_inv,'run_sber') as sber:
            self.assertEqual(run_inv.main(['--alfa-only','--sber-only']),2)
            day.assert_not_called();sber.assert_not_called()

    def test_all_alfa_commands_are_scoped(self):
        for mode in ('sync','prepare','morning','evening'):
            for command in schedule.commands(mode,'alfa'):
                if command[1].endswith('run_inv.py'):
                    self.assertIn('--alfa-only',command)
                    self.assertNotIn('--sber-only',command)
                else:
                    self.assertEqual(command[command.index('--org')+1],'7807355364')
                if command[1].endswith('payment_autosend.py'):
                    self.assertNotIn('--no-tg',command)
        watch=schedule.commands('sync','alfa')[-1]
        self.assertIn('--close-only',watch)

    def test_default_sber_scope_is_preserved(self):
        for mode in ('sync','prepare','morning','evening'):
            for command in schedule.commands(mode):
                if command[1].endswith('run_inv.py'):self.assertIn('--sber-only',command)
                else:self.assertEqual(command[command.index('--org')+1],'7811803918')

    def test_alfa_never_uses_sber_secret_rotator(self):
        with self.assertRaises(ValueError):schedule.main('secret','alfa')
