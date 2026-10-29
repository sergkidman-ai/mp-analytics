# поток: inv
"""Fail closed when a supplier order/receipt belongs to the wrong organization or store."""
from decimal import Decimal
from invoice_bot import ms

STORE_BY_ORG = {'7807355364': 'Звездный', '7811803918': 'Дисквер'}


class PaymentIntegrityError(ValueError):
    pass


def _norm(value):
    return str(value or '').strip().lower().replace('ё', 'е')


def inspect_orders(po_ids, org_inn, getter=None):
    getter = getter or ms.get
    if not po_ids:
        return []  # Advances/rent without order bases have no order/store to validate.
    expected = STORE_BY_ORG.get(str(org_inn))
    if not expected:
        raise PaymentIntegrityError('Неизвестная организация плательщика')
    cache = {}

    def resolve(ref):
        href = (ref.get('meta') or {}).get('href')
        if not href:
            raise PaymentIntegrityError('У документа отсутствует организация или склад')
        if href not in cache:
            if not href.startswith(ms.MS + '/'):
                raise PaymentIntegrityError('Некорректная ссылка МС')
            cache[href] = getter(href[len(ms.MS):])
        return cache[href]

    result = []
    for po_id in dict.fromkeys(po_ids):
        po = getter(f'/entity/purchaseorder/{po_id}?expand=supplies')
        po_org = resolve(po.get('organization') or {})
        po_store = resolve(po.get('store') or {})
        label = po.get('name') or str(po_id)
        if str(po_org.get('inn')) != str(org_inn):
            raise PaymentIntegrityError(f'{label}: организация заказа отличается от плательщика')
        if _norm(po_store.get('name')) != _norm(expected):
            raise PaymentIntegrityError(f'{label}: для организации требуется склад «{expected}»')
        supplies = []
        for ref in po.get('supplies') or []:
            supply = resolve(ref)
            supply_org = resolve(supply.get('organization') or {})
            supply_store = resolve(supply.get('store') or {})
            if supply_org.get('id') != po_org.get('id') or str(supply_org.get('inn')) != str(org_inn):
                raise PaymentIntegrityError(f'{label}: организация приёмки отличается от заказа')
            if supply_store.get('id') != po_store.get('id') or _norm(supply_store.get('name')) != _norm(expected):
                raise PaymentIntegrityError(f'{label}: склад приёмки отличается от заказа/организации')
            if (supply.get('agent') or {}).get('meta', {}).get('href') != (po.get('agent') or {}).get('meta', {}).get('href'):
                raise PaymentIntegrityError(f'{label}: поставщик приёмки отличается от заказа')
            supplies.append(supply)
        result.append((po, supplies))
    return result


def validate_draft(draft, getter=None):
    inspected = inspect_orders(draft.get('covers_po_ids'), draft.get('org_inn'), getter)
    if not inspected:
        return inspected
    # Credits/other payments may have reduced debt since the draft was queued.
    due = 0
    for po, _ in inspected:
        base = int(po.get('sum') or 0)
        if draft.get('kind') == 'deferred_batch':
            base = min(base, int(po.get('shippedSum') or 0))
        due += max(0, base - int(po.get('payedSum') or 0))
    amount = int(Decimal(str(draft['amount'])) * 100)
    if amount <= 0 or amount > due:
        raise PaymentIntegrityError('Сумма черновика превышает текущую задолженность: обновите очередь')
    return inspected
