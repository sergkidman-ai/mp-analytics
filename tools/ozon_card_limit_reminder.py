"""Поток card: разовое напоминание Наталии о лимите совместимости Ozon."""
import os
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE = Path('/opt/mp-analytics')
DUE = datetime(2026, 10, 6, 8, 14, tzinfo=timezone.utc)
MARKER = BASE / 'backups/ozon_card_limit_reminder_sent.json'
MESSAGE = (
    'Напоминание: пересмотреть лимит исправления совместимых принтеров на Ozon.\n\n'
    'С 3 октября автомат работает с лимитом 20 карточек на кабинет за ежедневный запуск '
    '(Цифровой и Дисквер). Это стартовый лимит тестирования, не ограничение API.\n\n'
    'Нужно проверить журнал card_push_log (repair_models): сколько карточек исправлено, '
    'сохранились ли модели принтеров после обработки Ozon, не вернулись ли предупреждения '
    'и не ухудшился ли статус продажи. По результатам решить с Сергеем, увеличивать ли лимит. '
    'Если отправок ещё не было — сообщить об этом и продолжить наблюдение. '
    'Обычный дожим сохраняет лимит 500 карточек.'
)


def send_text(text):
    load_dotenv(BASE / '.env')
    token = os.getenv('DROPBOX_BOT_TOKEN', '').strip()
    recipient = os.getenv('TG_PRC_NATALIA_ID', '1231747786').strip()
    allowed = {s.strip() for s in os.getenv('DROPBOX_ALLOWED_IDS', '').split(',')}
    if not token or recipient not in allowed:
        raise RuntimeError('нет токена Dropbox или Наталии в allow-list')
    response = requests.post('https://api.telegram.org/bot' + token + '/sendMessage',
                             json={'chat_id': recipient, 'text': text,
                                   'disable_notification': False}, timeout=30)
    # Исключения requests содержат URL с токеном: не выводить их в журнал.
    if response.status_code != 200:
        raise RuntimeError('Telegram HTTP ' + str(response.status_code))
    result = response.json()
    if not result.get('ok'):
        raise RuntimeError('Telegram не подтвердил отправку')
    return result['result']['message_id']


def send_due(now=None, marker=MARKER):
    now = now or datetime.now(timezone.utc)
    if now < DUE or marker.exists():
        return False
    message_id = send_text(MESSAGE)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"message_id": ' + str(message_id) + ', "sent_at": "' + now.isoformat() + '"}')
    print('Разовое напоминание о лимите Ozon отправлено Наталии в Dropbox')
    return True
