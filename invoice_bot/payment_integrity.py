# поток: inv
"""Fail closed when a supplier order/receipt belongs to the wrong organization or store."""
from decimal import Decimal
from datetime import date, timedelta
from urllib.parse import quote
from invoice_bot import ms

STORE_BY_ORG = {'7807355364': 'Звездный', '7811803918': 'Дисквер'}


class PaymentIntegrityError(ValueError):
    pass


class PaymentReconciliationError(ValueError):
    """Prior cash payments need reconciliation; this is not a warehouse assignment alert."""


def _href(document, field):
    return ((document.get(field) or {}).get('meta') or {}).get('href')


def _cash_due(draft, inspected, getter):
    """Bound unpaid order totals by recorded payments, including unlinked prepayments.

    Exact receipt links are not guaranteed to exist when goods arrive after cash.
    A multi-invoice payment without a matching allocation is ambiguous: stop for
    reconciliation rather than assume its listed invoice remains unpaid.
    """
    from collectors.alfa_link import purpose_tokens, _numkey
    groups = {}
    receipts = {}
    for po, supplies in inspected:
        key = (_href(po, 'organization'), _href(po, 'agent'))
        groups.setdefault(key, []).append(po)
        receipts[po['id']] = {(s.get('meta') or {}).get('href') for s in supplies}
    due, evidence_names = 0, set()
    for (org_href, agent_href), orders in groups.items():
        numbered = [po for po in orders if _numkey(po.get('name'))]
        payments = []
        if numbered:
            terms = ['organization=' + org_href, 'agent=' + agent_href, 'applicable=true']
            days = [(po.get('moment') or '')[:10] for po in numbered if po.get('moment')]
            if days:
                earliest = date.fromisoformat(min(days)) - timedelta(days=14)
                terms.append('moment>=' + earliest.isoformat() + ' 00:00:00')
            offset, seen = 0, set()
            while True:
                payload = getter('/entity/paymentout?filter=' + quote(';'.join(terms)) +
                                 '&limit=20&offset=' + str(offset) + '&expand=operations')
                if 'rows' not in payload or payload.get('errors'):
                    raise PaymentReconciliationError('Не удалось проверить предыдущие исходящие платежи')
                for payment in payload['rows']:
                    if not payment.get('applicable'):
                        continue
                    if (_href(payment, 'organization'), _href(payment, 'agent')) != (org_href, agent_href):
                        raise PaymentReconciliationError('МС вернул платёж другого юрлица или поставщика')
                    if payment['id'] not in seen:
                        payments.append(payment); seen.add(payment['id'])
                if len(payload['rows']) < 20:
                    break
                offset += 20
        for po in orders:
            base = int(po.get('sum') or 0)
            if draft.get('kind') == 'deferred_batch':
                base = min(base, int(po.get('shippedSum') or 0))
            paid = int(po.get('payedSum') or 0)
            key = _numkey(po.get('name'))
            cash = 0
            for payment in payments:
                tokens = {_numkey(t) for t in purpose_tokens(payment.get('paymentPurpose'))}
                tokens.discard(None)
                if not key or key not in tokens or int(payment.get('sum') or 0) <= 0:
                    continue
                evidence_names.add(str(payment.get('name') or payment['id']))
                if len(tokens) == 1:
                    cash += int(payment['sum'])
                    continue
                allocation = 0
                total_linked = 0
                for operation in payment.get('operations') or []:
                    linked = int(operation.get('linkedSum') or 0)
                    total_linked += linked
                    target = _href(operation, 'purchaseOrder')
                    if (operation.get('meta') or {}).get('type') == 'purchaseorder':
                        target = operation['meta']['href']
                    if (target == (po.get('meta') or {}).get('href') or
                            (operation.get('meta') or {}).get('href') in receipts[po['id']]):
                        allocation += linked
                if base > paid and (not allocation or int(payment['sum']) > total_linked):
                    raise PaymentReconciliationError(
                        f"Счёт {po.get('name')} уже указан в проведённом платеже МС "
                        f"№{payment.get('name')}; сначала сверить его привязки")
                cash += allocation
            # Recorded allocations and invoice evidence can describe the same cash.
            due += max(0, base - max(paid, cash))
    return due, evidence_names


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
    cash_due, payments = _cash_due(draft, inspected, getter or ms.get)
    if amount > cash_due:
        raise PaymentReconciliationError(
            'По счетам уже есть проведённые платежи МС №' + ', №'.join(sorted(payments)) +
            '; сумма нового черновика превышает остаток с учётом этих оплат')
    return inspected
