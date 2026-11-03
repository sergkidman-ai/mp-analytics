# поток: mkt
from unittest import TestCase
from unittest.mock import patch

from ops import wb_promo_guard, ya_promo_guard


class PromoGuardNotificationTests(TestCase):
    def test_wb_apply_failure_notifies(self):
        with patch.object(wb_promo_guard.sys, 'argv', ['wb', '--apply']), \
             patch('core.wb_price_writer.legacy_main', side_effect=RuntimeError('promo_stock_snapshot_stale:2026-11-02')), \
             patch.object(wb_promo_guard, 'tg', return_value='ok') as tg:
            with self.assertRaisesRegex(RuntimeError, 'promo_stock_snapshot_stale'):
                wb_promo_guard.run_cli()
        self.assertIn('promo_stock_snapshot_stale', tg.call_args.args[0])

    def test_market_apply_failure_notifies(self):
        with patch.object(ya_promo_guard.sys, 'argv', ['market', '--apply']), \
             patch.object(ya_promo_guard, 'main', side_effect=RuntimeError('promo_stock_snapshot_stale:2026-11-02')), \
             patch.object(wb_promo_guard, 'tg', return_value='ok') as tg:
            with self.assertRaisesRegex(RuntimeError, 'promo_stock_snapshot_stale'):
                ya_promo_guard.run_cli()
        self.assertIn('promo_stock_snapshot_stale', tg.call_args.args[0])

    def test_market_no_change_summary_is_accurate(self):
        text = ya_promo_guard.summary([], [], [], 0.72, 'September', True)
        self.assertIn('ценовых изменений нет', text)
