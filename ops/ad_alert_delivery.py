# поток: mkt
"""Personal delivery for advertising alerts; no staff PRC recipient list."""
import os

import requests

from ops.wb_daily_report import SERGEY_CHAT_ID


def send_sergey(text):
    """Deliver only to Sergey's verified private chat in the Dropbox bot."""
    token = os.getenv('DROPBOX_BOT_TOKEN', '').strip()
    if not token:
        return {'ok': False, 'error': 'dropbox_token_missing'}
    root = 'https://api.telegram.org/bot' + token
    try:
        bot = requests.get(root + '/getMe', timeout=(5, 10)).json()
        chat = requests.get(root + '/getChat', params={'chat_id': SERGEY_CHAT_ID},
                            timeout=(5, 10)).json()
        if not (bot.get('ok') and bot['result'].get('username', '').lower() == 'pro_dropbox_bot'
                and chat.get('ok') and chat['result'].get('type') == 'private'
                and str(chat['result'].get('id')) == SERGEY_CHAT_ID):
            return {'ok': False, 'error': 'recipient_identity_mismatch'}
        response = requests.post(root + '/sendMessage', json={
            'chat_id': SERGEY_CHAT_ID, 'text': text[:3900], 'disable_web_page_preview': True},
            timeout=(5, 25))
        data = response.json()
        mid = (data.get('result') or {}).get('message_id')
        confirmed = bool(response.status_code == 200 and data.get('ok') and mid)
        return {'ok': confirmed, 'message_id': mid if confirmed else None,
                'error': None if confirmed else 'delivery_not_confirmed'}
    except Exception as exc:
        # Request exception URLs can contain the bot token.
        return {'ok': False, 'error': type(exc).__name__}
