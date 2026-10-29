# поток: fin
from datetime import datetime, timedelta, timezone

from customer_guard.cancellation import classify, UNKNOWN, HANDED_OFF


def test_market_handoff_evidence_is_distinct_from_seller_preparation():
    t = datetime(2026, 10, 28, 13, 0, tzinfo=timezone.utc)
    cases = (
        ('ozon', 'posting_transferring_to_delivery', 'posting_acceptance_in_progress'),
        ('wb', 'complete:waiting', 'complete:sorted'),
        ('yandex', 'READY_TO_SHIP', 'DELIVERY_SERVICE_RECEIVED'),
    )
    for platform, preparation, receipt in cases:
        before = [(t, 'awaiting_deliver', preparation),
                  (t + timedelta(minutes=10), 'cancelled', 'cancelled')]
        result = classify(platform, before)
        assert result[0] == UNKNOWN
        assert result[2] is None
        with_handoff = [before[0],
                        (t + timedelta(minutes=5), 'delivering', receipt), before[1]]
        result = classify(platform, with_handoff)
        assert result[0] == HANDED_OFF
        assert result[2] == t + timedelta(minutes=5)


def test_no_cancellation_does_not_create_a_review():
    t = datetime(2026, 10, 28, 13, 0, tzinfo=timezone.utc)
    assert classify('wb', [(t, 'delivering', 'complete:sorted')]) is None


def test_extra_handoff_poll_is_limited_to_1600_1900_moscow():
    from customer_guard.service import observe_handoff_window
    assert observe_handoff_window(['oz_acc1'],
        datetime(2026, 10, 28, 12, 59, tzinfo=timezone.utc))['outside_window']
    assert observe_handoff_window(['oz_acc1'],
        datetime(2026, 10, 28, 16, 0, tzinfo=timezone.utc))['outside_window']
