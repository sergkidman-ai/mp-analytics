# поток: print
"""Complete narrow MS snapshot. Never requests print forms or MP labels."""
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from uuid import UUID

from shipment_guard.config import ORGS, identity
from print_service.clients import MS, reference_id
from print_service.domain import ACCOUNTS, MOSCOW, PrintBlocked, selected_order
from print_service.warehouse_colors import warehouse_color


class Collector:
    def __init__(self, clients):
        self.clients = clients

    def _ozon_postings(self, found, report):
        selected = {oid: (raw, account) for oid, (raw, platform, account) in found.items() if platform == 'ozon'}
        if not selected: return {}
        report(stage='postings', processed=0, total=len(selected))
        pool, results = ThreadPoolExecutor(max_workers=4), {}
        try:
            pending = {pool.submit(self.clients.ozon_posting, account, raw['name']): oid
                       for oid, (raw, account) in selected.items()}
            for future in as_completed(pending):
                try: results[pending[future]] = future.result()
                except PrintBlocked as error: results[pending[future]] = error
                report(stage='postings', processed=len(results), total=len(selected))
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        return results

    def collect(self, *, now, on_progress=None):
        report = on_progress or (lambda **fields: None)
        report(stage='orders', loaded=0)
        day = now.astimezone(MOSCOW).date()
        metadata = self.clients.ms_get('/entity/customerorder/metadata')
        states = [s for s in metadata.get('states', []) if s.get('name') == 'Собирается']
        if len(states) != 1: raise PrintBlocked('ms_collecting_state_ambiguous')
        state = states[0]['meta']['href']
        reference_id(states[0], 'customerorder/metadata/states')
        found = {}
        for org in ORGS:
            filters = ';'.join(['state=' + state, 'organization=' + MS + '/entity/organization/' + org,
                                'deliveryPlannedMoment>=' + str(day) + ' 00:00:00',
                                'deliveryPlannedMoment<' + str(day + timedelta(days=1)) + ' 00:00:00'])
            offset, expected = 0, None
            for _ in range(1000):
                page = self.clients.ms_get('/entity/customerorder', params={
                    'limit': 20, 'offset': offset, 'filter': filters, 'order': 'id,asc',
                    'expand': 'state,store,organization,agent'})
                rows, size = page.get('rows', []), page.get('meta', {}).get('size')
                if type(size) is not int or (expected is not None and expected != size):
                    raise PrintBlocked('ms_sync_changed_during_pagination')
                expected = size
                if len(rows) > 20 or (not rows and offset < size):
                    raise PrintBlocked('ms_sync_incomplete')
                for row in rows:
                    platform, account, _, _ = identity(row)
                    if account == 'ya_acc2' or account not in ACCOUNTS: continue
                    if not selected_order(row, account, now): raise PrintBlocked('ms_sync_scope_changed')
                    if row['id'] in found: raise PrintBlocked('ms_sync_duplicate_order')
                    found[row['id']] = (row, platform, account)
                report(stage='orders', loaded=len(found))
                offset += len(rows)
                if offset >= size: break
            else: raise PrintBlocked('ms_sync_page_limit')
        postings = self._ozon_postings(found, report)
        output = []
        report(stage='warehouses', processed=0, total=len(found))
        for processed, (raw, platform, account) in enumerate(found.values(), 1):
            error, did, store_id = None, None, None
            try:
                store_id = reference_id(raw.get('store'), 'store')
                demands = raw.get('demands', [])
                if len(demands) != 1: raise PrintBlocked('linked_demand_ambiguous')
                did = reference_id(demands[0], 'demand')
            except PrintBlocked as exc:
                error = str(exc)
            direction_id, direction, color = platform, {'wb': 'WB', 'yandex': 'FBS Маркет', 'ozon': 'Ozon — направление не определено'}[platform], None
            if platform == 'ozon':
                try:
                    posting = postings[raw['id']]
                    if isinstance(posting, PrintBlocked): raise posting
                    method = posting.get('delivery_method', {})
                    warehouse = method.get('warehouse_id')
                    if warehouse is None or not method.get('warehouse'): raise PrintBlocked('ozon_direction_missing')
                    direction_id, direction = str(warehouse), 'Ozon · ' + method['warehouse']
                    color = warehouse_color(account, str(warehouse))
                    if account == 'oz_acc1' and color is None: raise PrintBlocked('ozon_warehouse_color_unmapped')
                    if posting.get('is_express') is True: raise PrintBlocked('ozon_express_outside_fbs_scope')
                except PrintBlocked as exc:
                    error = error or str(exc)
            counter = '|'.join([account, direction_id, store_id or 'unknown'])
            output.append(dict(account=account, raw=raw, demand_id=did, counter_key=counter, source_error=error,
                display=dict(platform=platform, organization='Цифровой квадрат' if account.endswith('1') else 'Дисквер',
                             store=raw.get('store', {}).get('name') or 'Склад не определён', store_id=store_id,
                             direction=direction, direction_id=direction_id, color=color,
                             planned_at=raw['deliveryPlannedMoment'], ms_status=raw.get('state', {}).get('name'))))
            report(stage='warehouses', processed=processed, total=len(found))
        return output
