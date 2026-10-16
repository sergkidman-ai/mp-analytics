# поток: prc
"""Повторы подключения к IMAP до чтения писем; сеть и задержки замоканы."""
import imaplib
import ssl
import unittest
from unittest.mock import Mock, patch

from prices import mailbox


class MailboxRetryTests(unittest.TestCase):
    def setUp(self):
        self.factory = patch.object(mailbox.imaplib, 'IMAP4_SSL').start()
        self.sleep = patch.object(mailbox.time, 'sleep').start()
        self.output = patch('builtins.print').start()
        self.addCleanup(patch.stopall)

    def test_capability_abort_recovers_on_fresh_connection(self):
        box = Mock()
        self.factory.side_effect = [imaplib.IMAP4.abort('CAPABILITY connection failed'), box]
        self.assertIs(mailbox.connect(), box)
        self.assertEqual(self.factory.call_count, 2)
        self.sleep.assert_called_once_with(2)
        self.output.assert_called_once_with('IMAP: соединение восстановлено с попытки 2')
        self.assertEqual(self.factory.call_args.kwargs['timeout'], 30)

    def test_login_disconnect_closes_old_connection(self):
        broken, good = Mock(), Mock()
        broken.login.side_effect = ConnectionResetError('connection reset')
        self.factory.side_effect = [broken, good]
        self.assertIs(mailbox.connect(), good)
        broken.shutdown.assert_called_once_with()
        good.login.assert_called_once()

    def test_persistent_network_failure_stops_after_three_attempts(self):
        error = OSError('network unreachable')
        self.factory.side_effect = error
        with self.assertRaises(OSError) as caught:
            mailbox.connect()
        self.assertIs(caught.exception, error)
        self.assertEqual(self.factory.call_count, 3)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [2, 5])
        self.output.assert_not_called()

    def test_authentication_error_is_not_retried(self):
        box = self.factory.return_value
        box.login.side_effect = imaplib.IMAP4.error('authentication failed')
        with self.assertRaises(imaplib.IMAP4.error):
            mailbox.connect()
        self.factory.assert_called_once()
        box.shutdown.assert_called_once_with()
        self.sleep.assert_not_called()

    def test_certificate_error_is_not_retried(self):
        self.factory.side_effect = ssl.SSLCertVerificationError('certificate verification failed')
        with self.assertRaises(ssl.SSLCertVerificationError):
            mailbox.connect()
        self.factory.assert_called_once()
        self.sleep.assert_not_called()

    def test_first_attempt_success_has_no_delay_or_recovery_message(self):
        self.assertIs(mailbox.connect(), self.factory.return_value)
        self.sleep.assert_not_called()
        self.output.assert_not_called()


if __name__ == '__main__':
    unittest.main()
