# поток: rev
"""Polling only queues questions, including on a repeated poll before drafting completes."""
from unittest.mock import patch
from feedback_bot import tg_moderation
from ops import rev_question_fast_poll as fast


def test_poll_queues_new_questions_without_delivering_empty_cards():
    with patch.object(fast.collect, 'report', []), patch.object(fast.collect, 'stage'), \
         patch.object(fast.db, 'execute') as enqueue, patch.object(tg_moderation, 'send') as send:
        for _ in range(2):
            assert fast.run()['new_question_cards'] == 0
        assert enqueue.call_count == 2
        assert 'ON CONFLICT(platform,account,kind,ext_id) DO NOTHING' in enqueue.call_args.args[0]
        send.assert_not_called()
