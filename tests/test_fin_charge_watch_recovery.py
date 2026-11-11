# поток: fin
"""Recovery must avoid a historical flood and preserve failed notifications."""
import contextlib
import datetime as dt
import io
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from ops import fin_charge_watch as watch


class ChargeWatchRecovery(unittest.TestCase):
    def seen(self, day=dt.date(2026, 10, 20)):
        result = {}
        watch._add(result, 'ozon', 'начисление', '99999', 'Новая услуга',
                   'oz_acc1', day, Decimal('123.45'))
        return result

    def test_empty_recovered_state_seeds_history_without_sending(self):
        seen = self.seen()
        with patch.object(watch, 'scan', return_value=seen), \
                patch.object(watch.db, 'query', return_value=[]), \
                patch.object(watch, '_send') as send, \
                patch.object(watch, '_save') as save, \
                contextlib.redirect_stdout(io.StringIO()):
            watch.run(send=True, today=dt.date(2026, 11, 6))
        send.assert_not_called()
        save.assert_called_once_with(list(seen), seen)

    def test_failed_delivery_does_not_acknowledge_new_type(self):
        known = [{'platform': 'ozon', 'kind': 'начисление', 'code': '52'}]
        with patch.object(watch, 'scan', return_value=self.seen()), \
                patch.object(watch.db, 'query', return_value=known), \
                patch.object(watch, 'source_health', return_value=[]), \
                patch.object(watch, '_send', side_effect=RuntimeError('delivery failed')), \
                patch.object(watch, '_save') as save:
            with self.assertRaises(RuntimeError):
                watch.run(send=True, today=dt.date(2026, 11, 6))
        save.assert_not_called()

    def test_current_week_waits_until_completed_week(self):
        seen = self.seen(dt.date(2026, 11, 6))
        known = [{'platform': 'ozon', 'kind': 'начисление', 'code': '52'}]
        with patch.object(watch, 'scan', return_value=seen), \
                patch.object(watch.db, 'query', return_value=known), \
                patch.object(watch, 'source_health', return_value=[]), \
                patch.object(watch, '_send') as send, \
                patch.object(watch, '_save') as save, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(watch.run(send=True, today=dt.date(2026, 11, 6)), 0)
            send.assert_not_called()
            self.assertEqual(watch.run(send=True, today=dt.date(2026, 11, 10)), 1)
        send.assert_called_once()
        save.assert_called_with(list(seen), seen)

    def test_history_uses_lazy_server_cursor(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        consumed = []

        def rows():
            for value in range(3):
                consumed.append(value)
                yield {'value': value}

        cur.__iter__.side_effect = rows
        with patch.object(watch.db, 'get_conn') as get_conn:
            get_conn.return_value.__enter__.return_value = conn
            stream = watch._iter_rows('SELECT payload FROM raw_ozon_accrual')
            self.assertEqual(consumed, [])
            self.assertEqual(next(stream), {'value': 0})
            self.assertEqual(consumed, [0])
            self.assertEqual(list(stream), [{'value': 1}, {'value': 2}])
        conn.set_session.assert_called_once_with(readonly=True)
        self.assertEqual(conn.cursor.call_args.kwargs['name'], 'fin_charge_scan')
        self.assertEqual(cur.itersize, 500)
        cur.fetchall.assert_not_called()


if __name__ == '__main__':
    unittest.main()
