# поток: inv
"""A silent IMAP peer must time out and allow the next mail scan."""
from contextlib import ExitStack
import imaplib
import socket
from threading import Event, Thread
from time import monotonic
from unittest import TestCase
from unittest.mock import Mock, patch

from invoice_bot import mail_poller as poller


class StopPolling(BaseException):
    pass


class MailTimeoutTests(TestCase):
    def settings(self, stack):
        for name, value in {'HOST': 'test.invalid', 'USER': 'test', 'PASS': 'test',
                            'FOLDER': 'invoices', 'FOLDER_UPD': 'upd',
                            'FOLDER_SPRINT': 'sprint', 'FOLDER_RENT': '',
                            'FOLDER_BEGET': ''}.items():
            stack.enter_context(patch.object(poller, name, value))
        return stack.enter_context(patch.object(poller, 'log'))

    def test_silent_socket_times_out_and_next_cycle_processes_all_routes(self):
        release = Event()
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            listener.settimeout(2)
            port = listener.getsockname()[1]

            def stall():
                with listener.accept()[0]:
                    release.wait(2)

            server = Thread(target=stall, daemon=True)
            server.start()
            good = Mock()
            attempts = []

            def connect(host, timeout):
                attempts.append(timeout)
                if len(attempts) == 1:
                    # Exercise the same stdlib socket timeout against a peer
                    # that accepts a connection and never sends its greeting.
                    return imaplib.IMAP4('127.0.0.1', port, timeout=timeout)
                return good

            try:
                with ExitStack() as stack:
                    log = self.settings(stack)
                    stack.enter_context(patch.object(poller, 'IMAP_TIMEOUT', 0.1))
                    stack.enter_context(patch.object(poller.imaplib, 'IMAP4_SSL', side_effect=connect))
                    scan = stack.enter_context(patch.object(poller, 'poll_once'))
                    stack.enter_context(patch.object(poller.time, 'sleep', side_effect=[None, StopPolling()]))
                    start = monotonic()
                    with self.assertRaises(StopPolling):
                        poller.main()
                    self.assertLess(monotonic() - start, 1.5)
                    self.assertEqual(attempts, [0.1, 0.1])
                    self.assertEqual([c.args[1] for c in scan.call_args_list], ['invoices', 'upd', 'sprint'])
                    self.assertTrue(any('IMAP error:' in str(c) for c in log.call_args_list))
                    log.assert_any_call('Проверка почты завершена: папок 3')
                    good.logout.assert_called_once()
            finally:
                release.set()
                server.join(timeout=2)

    def test_scan_timeout_closes_connection_and_retries_same_routes(self):
        first, second = Mock(), Mock()
        with ExitStack() as stack:
            log = self.settings(stack)
            stack.enter_context(patch.object(poller.imaplib, 'IMAP4_SSL', side_effect=[first, second]))
            scan = stack.enter_context(patch.object(poller, 'poll_once',
                side_effect=[TimeoutError('read timed out'), None, None, None]))
            stack.enter_context(patch.object(poller.time, 'sleep', side_effect=[None, StopPolling()]))
            with self.assertRaises(StopPolling):
                poller.main()
            self.assertEqual([c.args[1] for c in scan.call_args_list], ['invoices', 'invoices', 'upd', 'sprint'])
            first.logout.assert_called_once()
            second.logout.assert_called_once()
            log.assert_any_call('Проверка почты завершена: папок 3')
