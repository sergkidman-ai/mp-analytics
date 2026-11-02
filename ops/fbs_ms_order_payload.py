# поток: inv
"""Build one FBS customerorder payload from verified identities and amounts.

No HTTP calls here. The caller must reread marketplace status, exact MS name,
product identity, stock and current purchase prices immediately before POST.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from core.ms_api import BASE, ref


ORG = {'acc1': 'a5e7ee84-8d63-41f2-9fae-0be2ef90462b',
       'acc2': 'f5bea6e0-c2ac-11ef-0a80-0221000adcc4'}
STORE = {'acc1': 'e0d74d6f-db92-11ee-0a80-0cda000a40b5',
         'acc2': 'd9f78430-0879-11f0-0a80-08bb001470db'}
STATE = {'available': '477d431f-32c5-487b-ba8d-c6e452a4a1d4',
         'waiting': '7e4cf8b8-210a-45c4-84d7-6d50d0de9ea3',
         'payment_pending': 'd0befb68-f468-11e2-9e9a-7054d21a8d1e'}
FIELDS = {
    'Доставка': '5f2ecbc0-469e-11e3-b841-7054d21a8d1e',
    'Способ оплаты': '79b148fb-461e-11e3-1b39-7054d21a8d1e',
    'Ответственный': 'a50ba521-726d-11ea-0a80-03900024991a',
    'Источник заказа': '258bd719-af8e-11ea-0a80-05ce00013582',
}
PAYMENT = 'c66481e9-3676-11e4-6518-002590a28eca'
RESPONSIBLE = 'd5807479-ac23-11e8-9ff4-3150000cb7e3'
PLATFORM = {
    'ozon': ('b7e22828-0b7c-11ee-0a80-11480012ed47',
             'cd7ae93e-713e-11ed-0a80-0ba000059203',
             '0f48d15c-0b83-11ee-0a80-036700138667',
             'c831a6f6-5023-11ed-0a80-011f00192b76'),
    'wb': ('de89f3ba-757f-11ee-0a80-0f8300075ceb',
           'ce772bad-76f0-11ee-0a80-096e00196e79',
           '2a45ff51-469e-11e3-b4d1-7054d21a8d1e',
           'daac1392-af8d-11ea-0a80-02fb0000fda1'),
    'yandex': ('f125b891-28a5-11ee-0a80-0d3a001d1aa6',
               '0e023df8-e631-11eb-0a80-082b000d2a5a',
               '5c58a1c0-f485-11e8-9ff4-34e80003fe44',
               '2b46e175-d3f3-11eb-0a80-00c00003b123'),
}


@dataclass(frozen=True)
class Line:
    product_id: str
    quantity: Decimal
    price_kopeks: int
    discount_percent: Decimal = Decimal(0)


def line_total(line: Line) -> int:
    if line.quantity <= 0 or line.price_kopeks < 0 or not 0 <= line.discount_percent < 100:
        raise ValueError('invalid_line_amount')
    return int((line.quantity * line.price_kopeks *
                (1 - line.discount_percent / 100)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _attribute(name: str, value_type: str, value_id: str) -> dict:
    return {'meta': {'href': f'{BASE}/entity/customerorder/metadata/attributes/{FIELDS[name]}',
                     'type': 'attributemetadata', 'mediaType': 'application/json'},
            'value': ref(value_type, value_id)}


def _state(available: bool, payment_pending: bool = False) -> dict:
    state_id = STATE['payment_pending' if payment_pending else 'available' if available else 'waiting']
    return {'meta': {'href': f'{BASE}/entity/customerorder/metadata/states/{state_id}',
                     'type': 'state', 'mediaType': 'application/json'}}


def build_payload(*, platform: str, account: str, external_id: str,
                  lines: list[Line], expected_kopeks: int, available: bool,
                  now: datetime, market_created_at: datetime | None = None,
                  payment_pending: bool = False) -> dict:
    if platform not in PLATFORM or account not in ('acc1', 'acc2'):
        raise ValueError('unknown_platform_or_account')
    if platform == 'yandex' and account != 'acc1':
        raise ValueError('yandex_acc2_does_not_exist')
    if payment_pending and (platform != 'yandex' or available):
        raise ValueError('payment_pending_only_for_unpaid_yandex')
    if not external_id or not lines or now.tzinfo is None:
        raise ValueError('missing_identity_or_lines_or_time_zone')
    if sum(line_total(line) for line in lines) != expected_kopeks:
        raise ValueError('marketplace_ms_total_mismatch')
    agent, project, delivery, source = PLATFORM[platform]
    # MS reserves new orders automatically; the import must not try to control
    # reservation. Read the resulting reserve back after creation.
    positions = [{'assortment': ref('product', line.product_id),
                  'quantity': float(line.quantity), 'price': line.price_kopeks,
                  'discount': float(line.discount_percent), 'vat': 5}
                 for line in lines]
    payload = {'name': external_id, 'organization': ref('organization', ORG[account]),
               'store': ref('store', STORE[account]), 'agent': ref('counterparty', agent),
               'project': ref('project', project),
               'state': _state(available, payment_pending),
               'vatEnabled': True, 'vatIncluded': True, 'positions': positions,
               'attributes': [_attribute('Доставка', 'customentity', delivery),
                              _attribute('Способ оплаты', 'customentity', PAYMENT),
                              _attribute('Ответственный', 'employee', RESPONSIBLE),
                              _attribute('Источник заказа', 'customentity', source)]}
    if market_created_at is not None:
        if market_created_at.tzinfo is None:
            raise ValueError('market_creation_requires_time_zone')
        source_time = market_created_at.astimezone(ZoneInfo('Europe/Moscow'))
        payload['moment'] = source_time.strftime('%Y-%m-%d %H:%M:00')
    if available:
        local = now.astimezone(ZoneInfo('Europe/Moscow'))
        day = local.date() + timedelta(days=local.hour >= 16)
        payload['deliveryPlannedMoment'] = day.strftime('%Y-%m-%d 00:00:00')
    return payload
