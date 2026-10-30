# поток: mkt
"""WB advertising budget alerts use Sergey's existing Dropbox channel."""
from unittest.mock import Mock

from ops import wb_ad_budget_guard as guard
from ops import ad_alert_delivery as delivery
from ops import ozon_promo_bonus_watch as ozon_bonus


def test_budget_alert_goes_to_sergey_not_general_prc(monkeypatch, tmp_path):
    row = dict(id=35700718, bal=0, cpo=68, o28=8, topup=1000,
               good=True, low=True, runway=0)
    monkeypatch.setattr(guard, 'scan', lambda account: [row] if account == 'wb_acc1' else [])
    send = Mock(return_value={'ok': True, 'message_id': 42})
    monkeypatch.setattr(guard, 'send_sergey', send)
    monkeypatch.setattr(guard, 'REPORT', tmp_path / 'ad-budget.txt')
    monkeypatch.setattr('sys.argv', ['wb_ad_budget_guard'])

    guard.main()

    send.assert_called_once()
    assert 'ВБ реклама: бюджеты' in send.call_args.args[0]
    assert 'Пополнение — только по вашему «да»' in send.call_args.args[0]


def test_sender_rejects_wrong_bot_before_message(monkeypatch):
    monkeypatch.setenv('DROPBOX_BOT_TOKEN', 'test-token')
    get = Mock(side_effect=[Mock(json=lambda: {'ok': True, 'result': {'username': 'wrong_bot'}}),
                            Mock(json=lambda: {'ok': True, 'result': {
                                'id': int(delivery.SERGEY_CHAT_ID), 'type': 'private'}})])
    post = Mock()
    monkeypatch.setattr(delivery.requests, 'get', get)
    monkeypatch.setattr(delivery.requests, 'post', post)

    assert delivery.send_sergey('text')['error'] == 'recipient_identity_mismatch'
    post.assert_not_called()


def test_ozon_ad_bonus_uses_same_sergey_channel(monkeypatch):
    send = Mock(return_value={'ok': True, 'message_id': 43})
    monkeypatch.setattr(ozon_bonus, 'send_sergey', send)
    assert ozon_bonus.tg('Ozon bonus') == 'ok'
    send.assert_called_once_with('Ozon bonus')
