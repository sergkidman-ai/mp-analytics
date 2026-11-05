# поток: print
from datetime import datetime, timezone, timedelta
from pathlib import Path
import json
import os
import traceback
from uuid import uuid4

from dotenv import load_dotenv

from print_service.clients import PrintClients
from print_service.collector import Collector
from print_service.labels import LabelPrefetcher
from print_service.domain import MOSCOW, PrintBlocked
from print_service.preparer import Preparer
from print_service.pdf import extract_order_pair
from print_service.queue import PrintQueue
from print_service.storage import save_json


ERRORS = {
    'counter_already_reserved': 'Для этого счётчика уже формируются файлы. Дождитесь завершения.',
    'selected_orders_changed': 'Этот заказ уже включён в файлы или больше не входит в текущий список. Обновите страницу.',
    'counter_empty': 'В счётчике нет новых заказов. Обновите страницу.',
    'counter_has_blocked_orders': 'У части заказов нет необходимых данных. Проверьте красные строки ниже.',
    'sample_requires_previously_printed_demand': 'Тестовый режим: новая выгрузка этого чека из МС пока отключена.',
    'pair_not_ready': 'Оба файла ещё не готовы.',
    'linked_demand_missing': 'В МС нет связанной отгрузки.',
    'linked_demand_ambiguous': 'В МС нет единственной связанной отгрузки.',
    'ozon_warehouse_color_unmapped': 'Для этого склада Ozon не задано буквенное обозначение.',
    'ozon_express_outside_fbs_scope': 'Заказ Ozon Express не входит в печать FBS.',
    'ozon_posting_not_ready_for_label': 'Ozon ещё не разрешает получение этикетки этого отправления.',
    'order_changed_since_counter_click': 'Данные или статус заказа изменились после нажатия на счётчик.',
    'ms_export_result_uncertain': 'Результат выгрузки МС не подтверждён. Повторный экспорт автоматически не выполняется.',
    'ozon_label_still_generating': 'Ozon ещё формирует этикетку. Повторите подготовку позже.',
    'native_label_format_not_58x40': 'Площадка вернула другой размер этикетки. Масштабирование отключено.',
}


def error_message(error):
    code = str(error)
    if code in ERRORS: return ERRORS[code]
    if '_http_' in code:
        name = {'ms': 'МойСклад', 'ozon': 'Ozon', 'wb': 'WB', 'yandex': 'Маркет'}.get(code.split('_http_')[0], 'Площадка')
        return name + ': не удалось получить документ или данные. Повторите позже.'
    if 'checksum' in code or 'manifest' in code:
        return 'Сохранённые файлы не прошли проверку целостности. Они не будут выданы как готовые.'
    if 'quantity' in code or 'article' in code or 'layout' in code:
        return 'Не удалось безопасно обработать разметку товарного чека. Заказ остаётся в очереди.'
    return 'Не удалось подготовить все файлы. Заказы остаются в очереди; уже полученные документы сохранены.'


class OrderTrackingPreparer(Preparer):
    def _one(self, member, day):
        try:
            return super()._one(member, day)
        except Exception as exc:
            exc.print_order_id = member['ms_order_id']
            raise


class PrintService:
    def __init__(self, *, queue=None, clients=None, root=None, clock=None, allow_unprinted=None, demo=False):
        load_dotenv(Path(__file__).resolve().parents[1] / '.env')
        self.queue = queue or PrintQueue(schema=os.getenv('PRINT_SCHEMA', 'print_ops'))
        self.root = Path(root or os.getenv('PRINT_DATA_ROOT', '/opt/mp-analytics/.private/print_runtime'))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.clients = clients or PrintClients(audit_root=self.root / 'raw')
        self.collector = Collector(self.clients)
        if allow_unprinted is None: allow_unprinted = os.getenv('PRINT_EXPORT_UNPRINTED', '0') == '1'
        self.preparer = OrderTrackingPreparer(self.queue, self.clients, root=self.root, allow_unprinted=allow_unprinted)
        self.labels = LabelPrefetcher(self.queue, self.clients, root=self.root)
        self.demo = demo

    def status(self):
        now = self.clock()
        base = dict(day=str(now.astimezone(MOSCOW).date()), demo=self.demo, interval_minutes=30)
        if not self.queue.installed():
            return base | dict(installed=False, rows=[], counters={}, ready_batches=[], jobs=[],
                               sync=None, last_sync=None, worker_healthy=False)
        data = self.queue.view(now=now)
        if data['sync']:
            try:
                progress = json.loads((self.root / 'sync_progress' / (str(data['sync']['id']) + '.json')).read_text())
                data['sync']['progress'] = {key: progress[key] for key in
                    ('stage', 'loaded', 'processed', 'total', 'updated_at') if key in progress}
            except (OSError, ValueError, TypeError):
                data['sync']['progress'] = {}
        for row in data['rows']:
            # Drop obsolete MS print annotations from persisted old snapshots.
            for field in ('ms_order_printed', 'ms_demand_printed'):
                row['display'].pop(field, None)
            row['error_message'] = error_message(row['source_error']) if row['source_error'] else None
            for field in ('receipt_path','receipt_sha','label_path','label_sha','artifact_demand_id','snapshot_id'):
                row.pop(field, None)
        for job in data['jobs']:
            job['error_message'] = error_message(job['error']) if job['error'] else None
        if data['sync'] and data['sync']['error']:
            data['sync']['error_message'] = error_message(data['sync']['error'])
        return base | dict(installed=True) | data

    def require_worker(self):
        if not self.queue.installed() or not self.queue.worker_healthy(self.clock()):
            raise PrintBlocked('worker_unavailable')

    def request_sync(self):
        self.require_worker()
        return self.queue.enqueue_sync()

    def request_counter(self, counter, key):
        self.require_worker()
        return self.queue.reserve_counter(counter, request_key=key, now=self.clock())

    def request_order(self, order_id, key):
        order = self.queue.order(order_id)
        if order['batch_state'] == 'ready':
            self.order_files(order_id)
            return dict(id=order['batch_id'], state='ready', count=1)
        self.require_worker()
        batch = self.queue.reserve_counter(order['counter_key'], request_key=key,
                                           now=self.clock(), order_ids=[str(order_id)])
        return dict(id=batch['id'], state=batch['state'], count=1)

    def order_files(self, order_id):
        order = self.queue.order(order_id)
        if order['batch_state'] != 'ready': raise PrintBlocked('pair_not_ready')
        self.queue.ready_files(str(order['batch_id']))
        batch = self.queue.batch(str(order['batch_id']))
        path = extract_order_pair(batch['pair_dir'], str(order_id), output_root=self.root / 'individual_pairs')
        return dict(receipts=path / 'receipts.pdf', labels=path / 'labels.pdf')

    def _record_error(self, exc, task_id):
        save_json(self.root / 'errors' / (uuid4().hex + '.json'),
                  {'task_id':str(task_id),'exception':type(exc).__name__,
                   'frames':[{'file':f.filename,'line':f.lineno,'function':f.name}
                             for f in traceback.extract_tb(exc.__traceback__)]})

    def _sync_progress(self, sync_id, **fields):
        try:
            save_json(self.root / 'sync_progress' / (str(sync_id) + '.json'),
                      dict(fields, updated_at=self.clock().isoformat()))
        except OSError:
            pass  # Progress display must not prevent a valid snapshot.

    def run_sync(self, sync_id):
        now = self.clock()
        try:
            rows = self.collector.collect(now=now, on_progress=lambda **fields: self._sync_progress(sync_id, **fields))
            self._sync_progress(sync_id, stage='saving', processed=len(rows), total=len(rows))
            self.clients.lease_check()
            self.queue.sync_snapshot(rows, started_at=now, complete=True)
            self.queue.finish_sync(sync_id)
            self._sync_progress(sync_id, stage='complete', processed=len(rows), total=len(rows))
        except Exception as exc:
            self._record_error(exc, sync_id)
            self.clients.lease_check()
            self.queue.finish_sync(sync_id, error=str(exc) if isinstance(exc, PrintBlocked) else 'sync_internal_error')

    def run_batch(self, batch):
        try:
            self.preparer.prepare(batch)
        except Exception as exc:
            self._record_error(exc, batch['id'])
            self.clients.lease_check()
            try:
                self.queue.fail_batch(batch['id'], generation=batch['generation'],
                                      error=str(exc) if isinstance(exc, PrintBlocked) else 'prepare_internal_error',
                                      order_id=getattr(exc, 'print_order_id', None))
            except PrintBlocked:
                pass  # A newer worker or an already published pair owns the result.

    def run_label_prefetch(self, row):
        try:
            self.labels.one(row, now=self.clock())
        except Exception as exc:
            self._record_error(exc, str(row['id']))
            self.clients.lease_check()
