# поток: print
"""Prepare one frozen counter, caching per-order artifacts before publication."""
from collections import defaultdict
from datetime import datetime, time
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re

from shipment_guard.config import identity
from print_service.clients import reference_id
from print_service.diskver_design import apply_diskver_design
from print_service.domain import MOSCOW, OrderPrintFiles, PrintBlocked, guard_receipt_link, selected_order
from print_service.pdf import build_pair, pdf_reader
from print_service.receipt import annotate_receipt
from print_service.storage import atomic_bytes, file_lock, save_json
from print_service.warehouse_colors import warehouse_color


def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def profile_key():
    root = Path(__file__).parent
    files = ['receipt.py', 'diskver_design.py', 'ozon_warehouse_colors.json', 'assets/diskver_support_qr.png', 'preparer.py']
    return sha256(b''.join((root / name).read_bytes() for name in files)).hexdigest()


def validate_native_label(path):
    for page in pdf_reader(Path(path).read_bytes()).pages:
        width, height = float(page.mediabox.width) * 25.4 / 72, float(page.mediabox.height) * 25.4 / 72
        # Yandex's native landscape PDF uses a portrait MediaBox and /Rotate
        # 90. Check its displayed dimensions without changing either property.
        if page.rotation in (90, 270): width, height = height, width
        if abs(width - 58) > .5 or abs(height - 40) > .5 or page.rotation not in (0, 90, 180, 270):
            raise PrintBlocked('native_label_format_not_58x40')


class Preparer:
    def __init__(self, queue, clients, *, root, allow_unprinted=False):
        self.queue, self.clients, self.root = queue, clients, Path(root)
        self.allow_unprinted = allow_unprinted

    def _positions(self, demand_id):
        result, expected, offset = [], None, 0
        for _ in range(100):
            page = self.clients.ms_get('/entity/demand/' + demand_id + '/positions',
                                       params={'limit': 20, 'offset': offset, 'expand': 'assortment'})
            size, rows = page.get('meta', {}).get('size'), page.get('rows', [])
            if type(size) is not int or (expected is not None and expected != size) or (not rows and offset < size):
                raise PrintBlocked('shipment_positions_incomplete')
            expected = size
            result.extend(rows); offset += len(rows)
            if offset >= size:
                if not result: raise PrintBlocked('shipment_empty')
                return result
        raise PrintBlocked('shipment_positions_page_limit')

    def _one(self, member, day):
        account, number = member['account'], member['external_id']
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', number): raise PrintBlocked('order_number_invalid')
        oid, did = member['ms_order_id'], member['ms_demand_id']
        folder = self.root / 'orders' / account / fingerprint([oid, did])
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        with file_lock(folder / '.lock'):
            order = self.clients.ms_get('/entity/customerorder/' + oid)
            demand = self.clients.ms_get('/entity/demand/' + did)
            platform, actual_account, _, _ = identity(order)
            clock = datetime.combine(day, time(12), tzinfo=MOSCOW)
            if actual_account != account or order.get('name') != number or not selected_order(order, account, clock):
                raise PrintBlocked('order_changed_since_counter_click')
            guard_receipt_link(order, demand, linked_order_id=oid)
            positions = self._positions(did)
            quantities = defaultdict(Decimal)
            lines = []
            for row in positions:
                assortment = row.get('assortment', {})
                article, quantity = assortment.get('code'), Decimal(str(row.get('quantity', 0)))
                if not article or not quantity.is_finite() or quantity <= 0:
                    raise PrintBlocked('shipment_article_or_quantity_missing')
                quantities[article] += quantity
                lines.append({k: row.get(k) for k in ('id','quantity','price','discount','vat','pack','gtd')} |
                             {'assortment': {k: assortment.get(k) for k in ('id','code','name')}})
            code, last_four, color = None, None, None
            if platform == 'ozon':
                post = self.clients.ozon_posting(account, number)
                if post.get('status') != 'awaiting_deliver': raise PrintBlocked('ozon_posting_not_ready_for_label')
                code = post.get('scanit')
                if not isinstance(code, str) or not code: raise PrintBlocked('ozon_scanit_missing')
                method = post.get('delivery_method', {})
                color = warehouse_color(account, str(method.get('warehouse_id', '')))
                if account == 'oz_acc1' and color is None: raise PrintBlocked('ozon_warehouse_color_unmapped')
                label_identity = dict(account=account, number=number, scanit=code, boxes=post.get('multi_box_qty'))
            elif platform == 'wb':
                label_identity = dict(account=account, number=number)
            elif platform == 'yandex':
                post = self.clients.yandex_order(number)
                if post.get('status') not in ('PROCESSING', 'DELIVERY', 'PICKUP'):
                    raise PrintBlocked('market_order_not_ready_for_label')
                label_identity = dict(account=account, number=number, campaign=post.get('campaignId'))
            else: raise PrintBlocked('unsupported_account')
            label_dir = folder / 'labels' / fingerprint(label_identity)
            label_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            label = label_dir / 'label.pdf'
            if not label.exists():
                if platform == 'ozon': self.clients.ozon_label(account, post, label_dir)
                elif platform == 'wb': self.clients.wb_sticker(account, number, label_dir)
                else: self.clients.yandex_label(post, label_dir)
            elif platform == 'wb':
                self.clients.wb_status(account, number)
            validate_native_label(label)
            if platform == 'wb':
                sticker = json.loads((label_dir / 'label_source.json').read_text())
                if sticker.get('orderId') != int(number): raise PrintBlocked('wb_cached_label_identity_mismatch')
                code, last_four = sticker['partA'], sticker['partB']
            source_key = fingerprint(dict(profile=profile_key(), order={k: order.get(k) for k in
                ('id','name','moment','deliveryPlannedMoment','organization','store','attributes')},
                demand={k: demand.get(k) for k in ('id','name','moment','sum','organization','agent','store','vatEnabled','vatIncluded')},
                positions=lines, code=code, last_four=last_four, color=color, label=label_identity))
            self.queue.mark_artifact(oid, demand_id=did, kind='label', path=label, source_key=source_key)
            receipt_dir = folder / 'receipts' / source_key
            receipt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            receipt = receipt_dir / 'receipt_ready.pdf'
            if not receipt.exists():
                raw = self.clients.export_receipt(oid, did, receipt_dir, expected_account=account,
                                                  expected_number=number, expected_day=day, allow_unprinted=self.allow_unprinted)
                modified = annotate_receipt(raw, diskver=account.endswith('2'), code=code,
                                            last_four=last_four, color=color, quantities=quantities)
                if account.endswith('2'):
                    modified = apply_diskver_design(modified)
                atomic_bytes(receipt, modified)
                save_json(receipt_dir / 'identity.json', dict(member=member, source_key=source_key))
            self.queue.mark_artifact(oid, demand_id=did, kind='receipt', path=receipt, source_key=source_key)
            return OrderPrintFiles(account, number, oid, did, (str(receipt.resolve()),), (str(label.resolve()),))

    def prepare(self, batch):
        files = tuple(self._one(member, batch['day']) for member in batch['orders'])
        path = build_pair(files, shipment_date=batch['day'], output_root=self.root / 'pairs')
        self.clients.lease_check()
        self.queue.publish_pair(batch['id'], generation=batch['generation'], pair_dir=path)
        return path
