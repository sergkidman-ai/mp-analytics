# поток: print
"""Warm the native label cache without exporting receipts or reserving orders."""
import json
from pathlib import Path
import re

from shipment_guard.config import identity
from print_service.clients import reference_id
from print_service.domain import MOSCOW, PrintBlocked, selected_order, ozon_label_available
from print_service.preparer import fingerprint, validate_native_label
from print_service.queue import checked_file
from print_service.storage import file_lock


class LabelPrefetcher:
    def __init__(self, queue, clients, *, root):
        self.queue, self.clients, self.root = queue, clients, Path(root)

    def candidates(self, now):
        result = []
        for row in self.queue.label_candidates(now=now):
            if row.get('label_retry_needed'):
                result.append(row)
                continue
            try:
                if row['artifact_demand_id'] != row['demand_id'] or not row['label_path']:
                    raise PrintBlocked('label_not_cached')
                checked_file(row['label_path'], row['label_sha'])
                validate_native_label(row['label_path'])
            except (PrintBlocked, OSError):
                result.append(row)
        return result

    def one(self, row, *, now):
        oid, did = str(row['id']), str(row['demand_id'])
        account, number = row['account'], row['external_id']
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', number): raise PrintBlocked('order_number_invalid')
        folder = self.root / 'orders' / account / fingerprint([oid, did])
        with file_lock(folder / '.lock'):
            # Counter clicks take priority. Use the very same lock/cache as
            # Preparer, including after a concurrent foreground preparation.
            if self.queue.order(oid)['batch_id']: return False
            order = self.clients.ms_get('/entity/customerorder/' + oid)
            platform, actual_account, _, _ = identity(order)
            if order.get('id') != oid or actual_account != account or order.get('name') != number or not selected_order(order, account, now):
                raise PrintBlocked('order_changed_since_counter_click')
            demands = order.get('demands', [])
            if len(demands) != 1 or reference_id(demands[0], 'demand') != did:
                raise PrintBlocked('artifact_demand_identity_changed')
            if platform == 'ozon':
                post = self.clients.ozon_posting(account, number)
                if not ozon_label_available(post): raise PrintBlocked('ozon_posting_not_ready_for_label')
                code = post.get('scanit')
                if (not isinstance(code, str) or not code) and post.get('is_express') is not True:
                    raise PrintBlocked('ozon_scanit_missing')
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
            self.clients.lease_check()
            marked = self.queue.mark_prefetched_label(oid, demand_id=did, path=label, now=now)
            if marked: self.queue.resolve_label_errors(oid, demand_id=did, now=now)
            return marked
