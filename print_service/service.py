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
from print_service.pdf import build_pair, extract_order_pair
from print_service.queue import PrintQueue
from print_service.storage import save_json


ERRORS = {
    'ozon_order_cancelled': 'Заказ отменён в Ozon. Исключён из печати.',
    'ms_http_429': 'МойСклад пока ограничивает частоту запросов. Пара ещё не готова; после успешной проверки заказ вернётся в счётчик.',
    'ms_rate_limit_deferred': 'Подготовка отложена из-за общего лимита МойСклад. После успешной проверки заказ вернётся в счётчик.',
    'ms_export_http_429': 'МойСклад ограничил частоту выгрузки ТЧ. Подождите минуту и повторите печать заказа.',
    'download_transport_error': 'Скачивание PDF прервалось из-за ошибки связи. Повторите получение документа.',
    'pdf_download_failed': 'Не удалось скачать PDF по ссылке площадки или МС. Повторите получение документа.',
    'market_order_identity_mismatch': 'Маркет не вернул единственный подходящий заказ. Проверьте номер и кабинет заказа.',
    'artifact_demand_identity_changed': 'В МС изменилась связанная отгрузка. Нажмите «Синхронизация с МС», затем обновите этикетку.',
    'order_demand_ambiguous': 'В МС у заказа несколько связанных отгрузок. Проверьте связь и нажмите «Синхронизация с МС».',
    'label_refresh_internal_error': 'Не удалось обновить этикетку. Повторите запрос позже.',
    'order_outside_print_scope': 'Заказ больше не входит в сегодняшний список «Собирается». Обновите данные МС.',
    'counter_already_reserved': 'Для этого счётчика уже формируются файлы. Дождитесь завершения.',
    'selected_orders_changed': 'Этот заказ уже включён в файлы или больше не входит в текущий список. Обновите страницу.',
    'counter_empty': 'В счётчике нет новых заказов. Обновите страницу.',
    'receipt_quantity_cell_too_small': 'Не удалось безопасно выделить количество в ТЧ. Заказ оставлен в счётчике.',
    'counter_has_blocked_orders': 'У части заказов нет необходимых данных. Проверьте красные строки ниже.',
    'sample_requires_previously_printed_demand': 'Тестовый режим: новая выгрузка этого чека из МС пока отключена.',
    'order_printed_externally': 'Заказ уже распечатан отдельно и исключён из счётчиков.',
    'pair_not_ready': 'Оба файла ещё не готовы.',
    'linked_demand_missing': 'В МС нет связанной отгрузки.',
    'linked_demand_ambiguous': 'В МС нет единственной связанной отгрузки.',
    'ozon_warehouse_color_unmapped': 'Для этого склада Ozon не задано буквенное обозначение.',
    'ozon_express_outside_fbs_scope': 'Заказ Ozon Express не входит в печать FBS.',
    'ozon_express_label_identity_mismatch': 'Номер заказа на этикетке Ozon Express не совпадает с выбранным заказом.',
    'ozon_express_label_code_ambiguous': 'На этикетке Ozon Express не удалось однозначно определить выделенные цифры.',
    'ozon_posting_not_ready_for_label': 'Ozon ещё не разрешает получение этикетки этого отправления.',
    'order_changed_since_counter_click': 'Данные или статус заказа изменились после нажатия на счётчик.',
    'ms_export_result_uncertain': 'Результат выгрузки МС не подтверждён. Повторный экспорт автоматически не выполняется.',
    'ozon_label_still_generating': 'Ozon ещё формирует этикетку. Повторите подготовку позже.',
    'ozon_label_retry_pending': 'Не удалось связаться с Ozon. Повторное получение этикетки доступно через минуту.',
    'native_label_format_not_58x40': 'Площадка вернула другой размер этикетки. Масштабирование отключено.',
}


def error_message(error):
    code = str(error)
    if code in ERRORS: return ERRORS[code]
    if '_http_' in code:
        name = {'ms': 'МойСклад', 'ozon': 'Ozon', 'wb': 'WB', 'yandex': 'Маркет'}.get(code.split('_',1)[0], 'Площадка')
        return name + ': не удалось получить документ или данные. Повторите позже.'
    if 'checksum' in code or 'manifest' in code:
        return 'Сохранённые файлы не прошли проверку целостности. Они не будут выданы как готовые.'
    if 'quantity' in code or 'article' in code or 'layout' in code:
        return 'Не удалось безопасно обработать разметку товарного чека. Заказ остаётся в очереди.'
    return 'Не удалось подготовить все файлы. Ошибки показаны в списке заказов; полученные документы сохранены.'


class OrderTrackingPreparer(Preparer):
    def __init__(self, *args, on_order_error=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_order_error = on_order_error or (lambda exc, oid: None)

    def _one(self, member, day):
        try:
            return super()._one(member, day)
        except Exception as exc:
            exc.print_order_id = member['ms_order_id']
            raise

    def prepare(self, batch):
        files, failures = [], []
        for index,member in enumerate(batch['orders']):
            self.clients.lease_check()
            try:
                blocked = batch['display'].get('blocked_orders', {}).get(member['ms_order_id'])
                if blocked: raise PrintBlocked(blocked)
                files.append(self._one(member, batch['day']))
            except Exception as exc:
                self.clients.lease_check()
                self.on_order_error(exc, member['ms_order_id'])
                failures.append(dict(ms_order_id=member['ms_order_id'],
                                     error=str(exc) if isinstance(exc, PrintBlocked) else 'prepare_internal_error'))
                if isinstance(exc,PrintBlocked) and str(exc)=='ms_http_429':
                    # A shared read limit applies to every following member.
                    # Avoid spending four retries on each one; preserve already
                    # prepared pairs and leave the remainder eligible for recovery.
                    failures.extend(dict(ms_order_id=m['ms_order_id'],error='ms_rate_limit_deferred')
                                    for m in batch['orders'][index+1:])
                    break
        if failures:
            self.clients.lease_check()
            self.queue.exclude_failed_orders(batch['id'], generation=batch['generation'], failures=failures)
        if not files: return None
        path = build_pair(tuple(files), shipment_date=batch['day'], output_root=self.root / 'pairs')
        self.clients.lease_check()
        self.queue.publish_pair(batch['id'], generation=batch['generation'], pair_dir=path)
        return path


class PrintService:
    def __init__(self, *, queue=None, clients=None, root=None, clock=None, allow_unprinted=None, demo=False, on_market_cancel=None):
        load_dotenv(Path(__file__).resolve().parents[1] / '.env')
        self.queue = queue or PrintQueue(schema=os.getenv('PRINT_SCHEMA', 'print_ops'))
        self.root = Path(root or os.getenv('PRINT_DATA_ROOT', '/opt/mp-analytics/.private/print_runtime'))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.clients = clients or PrintClients(audit_root=self.root / 'raw')
        self.cancel_dispatch=on_market_cancel
        if self.cancel_dispatch is None and clients is None and not demo:
            from ops.ms_market_status_urgent import request_cancel
            self.cancel_dispatch=lambda row,post:request_cancel(row['id'],row['account'],row['external_id'],post)
        self.collector = Collector(self.clients,on_market_cancel=self._market_cancelled)
        if allow_unprinted is None: allow_unprinted = os.getenv('PRINT_EXPORT_UNPRINTED', '0') == '1'
        self.preparer = OrderTrackingPreparer(self.queue, self.clients, root=self.root, allow_unprinted=allow_unprinted,
                                             on_order_error=self._record_error,on_market_cancel=self._market_cancelled)
        self.labels = LabelPrefetcher(self.queue, self.clients, root=self.root,on_market_cancel=self._market_cancelled)
        self.demo = demo

    def _market_cancelled(self, row, post):
        self.queue.mark_market_cancelled(row['id'],account=row['account'],number=row['external_id'],now=self.clock())
        if self.cancel_dispatch:
            try:self.cancel_dispatch(row,post)
            except Exception as exc:self._record_error(exc,row['id'])

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
            if row['preparation_error'] and not row['source_error']:
                row['error_message'] = error_message(row['preparation_error'])
            if row.get('label_error') and not row['error_message']:
                row['error_message'] = error_message(row['label_error'])
            refresh = row.get('label_refresh')
            if refresh and refresh['error']:
                refresh['error_message'] = error_message(refresh['error'])
            for field in ('receipt_path','receipt_sha','label_path','label_sha','artifact_demand_id','snapshot_id'):
                row.pop(field, None)
        for job in data['jobs']:
            job['error_message'] = error_message(job['error']) if job['error'] else None
        for batch in data['ready_batches']:
            for field in ('blocked_orders', 'requested_order_ids', 'past_exclusions'):
                batch['display'].pop(field, None)
        if data['sync'] and data['sync']['error']:
            data['sync']['error_message'] = error_message(data['sync']['error'])
        return base | dict(installed=True) | data

    def archive(self, *, day=None, accounts=None):
        data = self.queue.archive(now=self.clock(), day=day, accounts=accounts)
        for batch in data['ready_batches']:
            for field in ('blocked_orders', 'requested_order_ids', 'past_exclusions', 'resolved_orders'):
                batch['display'].pop(field, None)
            if accounts is not None and 'excluded_orders' in batch['display']:
                batch['display']['excluded_orders'] = [order for order in batch['display']['excluded_orders']
                                                      if order.get('account') in accounts]
        return data | dict(archive=True, demo=self.demo, installed=True)

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
        if order.get('printed_externally'):
            raise PrintBlocked('order_printed_externally')
        self.require_worker()
        batch = self.queue.reserve_counter(order['counter_key'], request_key=key,
                                           now=self.clock(), order_ids=[str(order_id)])
        return dict(id=batch['id'], state=batch['state'], count=1)

    def request_label(self, order_id, key):
        self.require_worker()
        return self.queue.enqueue_label_request(order_id, request_key=key, now=self.clock())

    def run_label_request(self, request):
        oid = str(request['order_id'])
        try:
            order = self.queue.order(oid)
            if str(order['demand_id']) != str(request['demand_id']):
                raise PrintBlocked('artifact_demand_identity_changed')
            received = self.labels.one(order, now=self.clock())
            if not received and self.queue.order(oid)['batch_state'] not in ('building','ready'):
                raise PrintBlocked('order_changed_since_counter_click')
            self.clients.lease_check()
            self.queue.finish_label_request(request['id'], now=self.clock())
        except Exception as exc:
            self._record_error(exc, oid)
            self.clients.lease_check()
            self.queue.finish_label_request(request['id'], now=self.clock(),
                error=str(exc) if isinstance(exc, PrintBlocked) else 'label_refresh_internal_error')

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
