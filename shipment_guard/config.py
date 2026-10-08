# поток: fin
import os
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')
MS = 'https://api.moysklad.ru/api/remap/1.2'
MS_TZ = ZoneInfo('Europe/Moscow')

def ms_token():
    # No fallback to the admin token shared with TC and other collectors.
    token=os.getenv('SHIPMENT_GUARD_MS_TOKEN','').strip()
    if not token: raise RuntimeError('shipment_guard_ms_token_missing')
    return token

def account_id():
    return str(UUID(os.environ['SHIPMENT_GUARD_ACCOUNT_ID']))

# IDs resolved by exact name during read-only preflight, not inferred from order numbers.
ORGS = {'a5e7ee84-8d63-41f2-9fae-0be2ef90462b': ('1', 'ООО "ЦИФРОВОЙ КВАДРАТ"'),
        'f5bea6e0-c2ac-11ef-0a80-0221000adcc4': ('2', 'ООО "ДИСКВЭР"')}
AGENTS = {'de89f3ba-757f-11ee-0a80-0f8300075ceb': 'wb',
          'b7e22828-0b7c-11ee-0a80-11480012ed47': 'ozon',
          '22240c7b-c88d-11ed-0a80-10e700065d1d': 'ozon',
          'f125b891-28a5-11ee-0a80-0d3a001d1aa6': 'yandex',
          '86cbf18c-30f1-11ec-0a80-09e70012e3e9': 'yandex'}

def identity(document):
    def hid(key):
        return document.get(key, {}).get('meta', {}).get('href', '').rsplit('/', 1)[-1]
    org = ORGS.get(hid('organization'))
    platform = AGENTS.get(hid('agent'), 'direct')
    prefix = {'wb':'wb', 'ozon':'oz', 'yandex':'ya'}.get(platform)
    account = f'{prefix}_acc{org[0]}' if org and prefix else None
    return platform, account, org[1] if org else 'Неизвестное юрлицо', hid('organization')
