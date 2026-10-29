# поток: inv
"""Reuse explicitly registered MS overpayments before planning another supplier payment.

The MS payment remains the source of truth. Baseline links identify credit already
consumed, so a crash after a successful PUT cannot spend it twice on the next run.
"""
from invoice_bot import ms
from invoice_bot.payment_integrity import inspect_orders, PaymentIntegrityError
from core import db


def operations_map(operations):
    return {op['meta']['href'].split('?', 1)[0]: int(op.get('linkedSum') or 0) for op in operations or []}


def available_credit(payment, row):
    current = operations_map(payment.get('operations'))
    baseline = operations_map(row['baseline_operations'])
    consumed = sum(max(0, value - baseline.get(href, 0)) for href, value in current.items())
    return max(0, min(int(row['credit_kop']) - consumed,
                      int(payment.get('sum') or 0) - sum(current.values())))


def settle(org_inn, inn, po_ids):
    rows = db.query('SELECT * FROM supplier_payment_credit WHERE org_inn=%s AND inn=%s',
                    (org_inn, inn))
    if not rows or not po_ids:
        return 0
    from collectors.alfa_link import linkable
    settled = 0
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ('supplier-credit:'+org_inn+':'+inn,))
            for row in rows:
                path = '/entity/paymentout/' + str(row['ms_payment_id'])
                payment = ms.get(path+'?expand=organization,agent,operations')
                if str(payment.get('organization', {}).get('inn')) != str(org_inn) or str(payment.get('agent', {}).get('inn')) != str(inn):
                    raise PaymentIntegrityError('Переплата относится к другой организации или поставщику')
                left = available_credit(payment, row)
                if left <= 0:
                    cur.execute('UPDATE supplier_payment_credit SET checked_at=now(),remaining_kop=0 WHERE ms_payment_id=%s', (row['ms_payment_id'],))
                    continue
                targets = {}
                for po, supplies in inspect_orders(po_ids, org_inn):
                    if (po.get('agent') or {}).get('meta', {}).get('href') != payment['agent']['meta']['href']:
                        raise PaymentIntegrityError('Поставщик заказа отличается от источника переплаты')
                    for supply in supplies:
                        if linkable(supply):
                            targets[supply['id']] = supply
                operations = operations_map(payment.get('operations'))
                used = 0
                for supply in sorted(targets.values(), key=lambda s: (s.get('moment') or '', s['id'])):
                    # Reread the debt immediately before allocating real money.
                    fresh = ms.get('/entity/supply/'+supply['id'])
                    for field in ('organization','store','agent','purchaseOrder'):
                        if fresh.get(field, {}).get('meta', {}).get('href') != supply.get(field, {}).get('meta', {}).get('href'):
                            raise PaymentIntegrityError('Приёмка изменилась во время зачёта переплаты')
                    take = min(left, max(0, int(fresh.get('sum') or 0) - int(fresh.get('payedSum') or 0)))
                    if take <= 0:
                        continue
                    href = fresh['meta']['href']
                    operations[href] = operations.get(href, 0) + take
                    used += take; left -= take
                    if left == 0:
                        break
                if used:
                    current = ms.get(path+'?expand=organization,agent,operations')
                    if (operations_map(current.get('operations')) != operations_map(payment.get('operations'))
                            or current.get('sum') != payment.get('sum')
                            or current.get('organization', {}).get('id') != payment.get('organization', {}).get('id')
                            or current.get('agent', {}).get('id') != payment.get('agent', {}).get('id')):
                        raise PaymentIntegrityError('Платёж изменился во время зачёта переплаты')
                    payload = {'operations': [{'meta': {'href': href, 'type': href.split('/')[-2],
                                                        'mediaType': 'application/json'}, 'linkedSum': value}
                                               for href,value in operations.items()]}
                    status, _ = ms.put(path, payload)
                    if status != 200:
                        raise RuntimeError('МС не подтвердил зачёт переплаты')
                    confirmed = ms.get(path+'?expand=operations')
                    if operations_map(confirmed.get('operations')) != operations:
                        raise RuntimeError('Привязки переплаты не прошли повторную проверку')
                    left = available_credit(confirmed, row)
                    settled += used
                cur.execute('UPDATE supplier_payment_credit SET checked_at=now(),remaining_kop=%s WHERE ms_payment_id=%s',
                            (left, row['ms_payment_id']))
    return settled
