"""Поток card: разовый срок, повтор после сбоя и защита от второй отправки."""
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from tools import ozon_card_limit_reminder as reminder


class LimitReminder(unittest.TestCase):
    def test_waits_until_due(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(reminder, 'send_text') as send:
            self.assertFalse(reminder.send_due(reminder.DUE - timedelta(seconds=1), Path(folder) / 'sent'))
        send.assert_not_called()

    def test_sends_once_after_due(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(reminder, 'send_text', return_value=42) as send:
            marker = Path(folder) / 'sent'
            self.assertTrue(reminder.send_due(reminder.DUE, marker))
            self.assertFalse(reminder.send_due(reminder.DUE + timedelta(days=1), marker))
        send.assert_called_once_with(reminder.MESSAGE)

    def test_failed_delivery_does_not_mark_sent(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(reminder, 'send_text', side_effect=RuntimeError):
            marker = Path(folder) / 'sent'
            with self.assertRaises(RuntimeError):
                reminder.send_due(reminder.DUE, marker)
            self.assertFalse(marker.exists())
