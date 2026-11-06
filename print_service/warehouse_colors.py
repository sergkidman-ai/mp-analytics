# поток: print
"""Custom Ozon colors confirmed by the user on 2026-11-03.

IDs and current names: /v2/warehouse/list (both accounts, has_next=false).
ACC1 colors: image1.png from the original task and the user's confirmation;
Изи + Спринт = violet explicitly confirmed separately.
ACC2 colors: Dropbox 20261103_151116_Natalie_Pro_natalya_photo.jpg.
The gray/pink ZVZ warehouses are distinct from AQUA+SF / IZI+SP without ZVZ.
"""
import json
from pathlib import Path

from print_service.domain import OZON_COLORS, ozon_warehouse_color


def load_config(path=None):
    path = Path(path) if path is not None else Path(__file__).with_name('ozon_warehouse_colors.json')
    config = json.loads(path.read_text())
    if set(config) != {'oz_acc1', 'oz_acc2'}:
        raise ValueError('warehouse_color_accounts_invalid')
    for warehouses in config.values():
        if not isinstance(warehouses, dict):
            raise ValueError('warehouse_colors_invalid')
        for warehouse_id, entry in warehouses.items():
            if (not warehouse_id.isdigit() or int(warehouse_id) <= 0
                    or not isinstance(entry, dict) or entry.get('color') not in OZON_COLORS
                    or not isinstance(entry.get('name'), str) or not entry['name'].strip()):
                raise ValueError('warehouse_color_entry_invalid')
    return config


def warehouse_color(account, warehouse_id, *, config=None, is_express=False):
    if isinstance(warehouse_id, bool) or not isinstance(warehouse_id, (str, int)):
        raise ValueError('ozon_warehouse_identity_required')
    if account not in ('oz_acc1', 'oz_acc2'):
        raise ValueError('ozon_warehouse_identity_required')
    if is_express is True:
        # A delivery-type label, not an inferred custom warehouse color.
        return {'key': 'express', 'short': 'ЭКС', 'css': '#000000'}
    config = load_config() if config is None else config
    mapping = {a: {wid: entry['color'] for wid, entry in rows.items()}
               for a, rows in config.items()}
    return ozon_warehouse_color(mapping, account, str(warehouse_id))
