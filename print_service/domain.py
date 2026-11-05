# поток: print
"""Pure print rules. No API requests, document exports, or status writes."""
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
import json
from zoneinfo import ZoneInfo


MOSCOW = ZoneInfo('Europe/Moscow')
ACCOUNTS = frozenset({'oz_acc1', 'oz_acc2', 'wb_acc1', 'wb_acc2', 'ya_acc1'})
OZON_COLORS = {
    'orange': ('Орж', '#f59e0b'), 'black': ('Чрн', '#000000'),
    'blue': ('Син', '#0000ff'), 'green': ('Злн', '#008000'),
    'violet': ('Флт', '#800080'), 'gray': ('Сер', '#808080'),
    'pink': ('Роз', '#e83e8c'), 'lightblue': ('Гол', '#00bfff'),
}


class PrintBlocked(ValueError):
    pass


def planned_for_today(value: str | None, now: datetime) -> bool:
    """MS timestamps without an offset are in MS's Moscow timezone."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('clock_requires_timezone')
    try:
        planned = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    if planned.tzinfo is None:
        planned = planned.replace(tzinfo=MOSCOW)
    return planned.astimezone(MOSCOW).date() == now.astimezone(MOSCOW).date()


def selected_order(order: dict, account: str, now: datetime) -> bool:
    return (account in ACCOUNTS
            and order.get('state', {}).get('name') == 'Собирается'
            and planned_for_today(order.get('deliveryPlannedMoment'), now))


def ozon_warehouse_color(mapping: dict, account: str, warehouse_id: str):
    """Custom mapping uses account + Ozon warehouse ID, never the MS store/name.

    Values in mapping[account][warehouse_id] are explicit OZON_COLORS keys.
    An unmapped warehouse stays unknown; no color is inferred from its name.
    """
    if account not in {'oz_acc1', 'oz_acc2'} or not isinstance(warehouse_id, str) or not warehouse_id:
        raise ValueError('ozon_warehouse_identity_required')
    key = mapping.get(account, {}).get(warehouse_id)
    if key is None:
        return None
    if key not in OZON_COLORS:
        raise ValueError('unknown_ozon_color')
    short, css = OZON_COLORS[key]
    return {'key': key, 'short': short, 'css': css}


def guard_receipt_link(order: dict, demand: dict, *, linked_order_id: str):
    """Require freshly read, unique, bidirectional order-to-demand identity."""
    if not linked_order_id or order.get('id') != linked_order_id:
        raise PrintBlocked('order_identity_mismatch')
    meta = demand.get('meta', {})
    if meta.get('type') != 'demand' or not demand.get('id'):
        raise PrintBlocked('demand_identity_missing')
    href = meta.get('href')
    expected = 'https://api.moysklad.ru/api/remap/1.2/entity/demand/' + demand['id']
    if href != expected:
        raise PrintBlocked('demand_reference_invalid')
    links = order.get('demands', [])
    if len(links) != 1 or links[0].get('meta', {}).get('href') != href:
        raise PrintBlocked('order_demand_ambiguous')
    reverse = demand.get('customerOrder', {}).get('meta', {}).get('href')
    if reverse != 'https://api.moysklad.ru/api/remap/1.2/entity/customerorder/' + linked_order_id:
        raise PrintBlocked('demand_order_mismatch')


def guard_sample_export(order: dict, demand: dict, *, linked_order_id: str):
    """Fresh identity and printed=true are mandatory before each TEST export."""
    guard_receipt_link(order, demand, linked_order_id=linked_order_id)
    if demand.get('printed') is not True:
        raise PrintBlocked('sample_requires_previously_printed_demand')


@dataclass(frozen=True)
class OrderPrintFiles:
    """One order may have multiple receipt pages and multiple box labels."""
    account: str
    external_id: str  # Full posting_number / assembly order ID / Market order ID.
    ms_order_id: str
    ms_demand_id: str
    receipt_files: tuple[str, ...]
    label_files: tuple[str, ...]

    def __post_init__(self):
        if self.account not in ACCOUNTS:
            raise PrintBlocked('unsupported_account')
        if not all(isinstance(x, str) and x.strip() for x in
                   (self.external_id, self.ms_order_id, self.ms_demand_id)):
            raise PrintBlocked('order_identity_missing')
        for files in (self.receipt_files, self.label_files):
            if not isinstance(files, tuple) or not files or any(not isinstance(x, str) or not x for x in files):
                raise PrintBlocked('both_documents_required')


def freeze_batch(orders: tuple[OrderPrintFiles, ...], *, shipment_date: date):
    """Use this one immutable order sequence for BOTH PDF merges.

    Input order is the displayed group order. Partial groups must be chosen
    explicitly before this call; missing files never cause silent skipping.
    Paths describe prepared artifacts; PDF validation/merging is a later step.
    """
    if not isinstance(orders, tuple) or not orders:
        raise PrintBlocked('nonempty_fixed_order_list_required')
    for field in ('ms_order_id', 'ms_demand_id'):
        ids = [getattr(o, field) for o in orders]
        if len(set(ids)) != len(ids):
            raise PrintBlocked('duplicate_' + field)
    identities = [(o.account, o.external_id) for o in orders]
    if len(set(identities)) != len(identities):
        raise PrintBlocked('duplicate_marketplace_order')
    manifest = {
        'shipment_date_moscow': shipment_date.isoformat(),
        'orders': [dict(account=o.account, external_id=o.external_id,
                        ms_order_id=o.ms_order_id, ms_demand_id=o.ms_demand_id,
                        receipts=list(o.receipt_files), labels=list(o.label_files)) for o in orders],
    }
    manifest['sequence_id'] = sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return manifest
