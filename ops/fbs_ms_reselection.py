# поток: inv
"""Preserve existing sale lines while applying a verified shared-importer plan."""
from collections import Counter, defaultdict
from copy import deepcopy
from decimal import Decimal
from core import ms_api
from core.order_composition_guard import assert_automatic_composition
from ops.fbs_ms_post_import_status import provenance, imported_availability, planned_day, STATE
from ops.supplier_shipping_dates import local_created


def replacement_payload(order, proof, now):
    if order.get('state', {}).get('name') != 'Неукомплектован' or order.get('demands'):
        raise ValueError('order_not_uncompleted_or_already_shipped')
    assert_automatic_composition(order)
    original = order['positions']
    if original['meta']['size'] != len(original['rows']) or not original['rows']:
        raise ValueError('positions_incomplete')
    if Decimal(str(order['sum'])) != Decimal(str(proof['total_kopeks'])):
        raise ValueError('market_and_existing_sale_total_differ')
    manual, priced = provenance(proof)
    available, _ = imported_availability(proof)
    if manual or available is None or (not available and not priced):
        raise ValueError('selection_requires_manual_review')
    wanted = defaultdict(lambda: defaultdict(Decimal))
    old_quantities = Counter()
    for row in original['rows']:
        code = row['assortment'].get('externalCode')
        if not code or row.get('pack') or row.get('shipped'):
            raise ValueError('position_identity_pack_or_shipped')
        old_quantities[code] += Decimal(str(row['quantity']))
    for item in proof['selected']:
        wanted[item['base_code']][(item['card_id'], item['available'])] += Decimal(item['quantity'])
    if dict(old_quantities) != {code:sum(choices.values()) for code,choices in wanted.items()}:
        raise ValueError('set_recomposition_requires_review')
    positions = []
    for row in original['rows']:
        code = row['assortment']['externalCode']; quantity = Decimal(str(row['quantity']))
        old_id = ms_api.meta_id(row, 'assortment')
        eligible = [key for key, amount in wanted[code].items() if amount >= quantity]
        if not eligible:
            raise ValueError('sale_line_split_requires_review')
        cid, own = min(eligible, key=lambda x:(x[0] != old_id, not x[1], x[0]))
        wanted[code][(cid,own)] -= quantity
        item = deepcopy(row)
        for field in ('id','meta','accountId','shipped'):
            item.pop(field,None)
        item['assortment'] = ms_api.ref('product',cid)
        # Existing shortages may reserve future receipts on the order warehouse.
        # Keep that reservation policy when moving the sale to another card.
        item['reserve'] = float(quantity) if own else row.get('reserve', 0)
        positions.append(item)
    if any(amount for choices in wanted.values() for amount in choices.values()):
        raise ValueError('selected_quantity_not_fully_applied')
    target = 'Собирается' if available else 'Неукомплектован'
    created = local_created(proof['market']['created_at'])
    day = planned_day(target, now if available else created,
                      [item.get('supplier_id') for item in proof['selected']])
    if available and day > now.astimezone(created.tzinfo).date().isoformat():
        target = 'Подтвержден'
    payload = {'positions':positions,'deliveryPlannedMoment':day+' 00:00:00'}
    # state metadata references differ from ordinary entity references.
    payload['state'] = {'meta':{'href':ms_api.BASE+'/entity/customerorder/metadata/states/'+STATE[target],
                               'type':'state','mediaType':'application/json'}}
    return payload


def sale_signature(order):
    return [(str(p['quantity']),str(p['price']),str(p.get('discount',0)),
             p.get('vat'),p.get('vatEnabled',True)) for p in order['positions']['rows']]


def needs_update(order, payload):
    before = [(ms_api.meta_id(p,'assortment'),Decimal(str(p.get('reserve',0))))
              for p in order['positions']['rows']]
    after = [(ms_api.meta_id(p,'assortment'),Decimal(str(p.get('reserve',0))))
             for p in payload['positions']]
    return (before != after or ms_api.meta_id(order,'state') != ms_api.meta_id(payload,'state')
            or (order.get('deliveryPlannedMoment') or '')[:19] != payload['deliveryPlannedMoment'])
