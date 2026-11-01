# поток: fin
"""A short collision with the shared MS worker must not fail status observation."""
from unittest.mock import patch

import pytest

from customer_guard import ms_status_watch


def test_busy_lock_is_retried_without_extra_ms_reads():
    with patch.object(ms_status_watch, 'due_orders', return_value=[{'id': 1}]), \
         patch.object(ms_status_watch, 'check_rows', side_effect=[
             RuntimeError('shipment_or_customer_worker_busy'),
             {'checked': 1, 'changed': 1},
         ]) as check, \
         patch.object(ms_status_watch.time, 'sleep') as sleep:
        assert ms_status_watch.run()=={'checked': 1, 'changed': 1}
    assert check.call_count==2
    sleep.assert_called_once_with(5)


def test_persistent_busy_lock_skips_run_and_other_errors_surface():
    with patch.object(ms_status_watch, 'due_orders', return_value=[{'id': 1}]), \
         patch.object(ms_status_watch, 'check_rows', side_effect=RuntimeError('shipment_or_customer_worker_busy')) as check, \
         patch.object(ms_status_watch.time, 'sleep') as sleep:
        assert ms_status_watch.run()=={'checked': 0, 'changed': 0, 'busy': True}
    assert check.call_count==7
    assert sleep.call_count==6

    with patch.object(ms_status_watch, 'due_orders', return_value=[{'id': 1}]), \
         patch.object(ms_status_watch, 'check_rows', side_effect=RuntimeError('ms_order_identity_changed')):
        with pytest.raises(RuntimeError, match='ms_order_identity_changed'):
            ms_status_watch.run()
