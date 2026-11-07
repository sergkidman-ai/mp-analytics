# поток: print
"""Only MS reads/export and MP reads/labels. No fulfilment/status methods."""
from io import BytesIO
import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4
import base64
from hashlib import sha256
from datetime import datetime, time as daytime

from dotenv import load_dotenv
from fpdf import FPDF
from PIL import Image
import requests

from print_service.domain import ACCOUNTS, MOSCOW, PrintBlocked, guard_receipt_link, guard_sample_export, selected_order, ozon_label_available
from print_service.pdf import pdf_reader
from print_service.storage import atomic_bytes, save_json

MS = 'https://api.moysklad.ru/api/remap/1.2'
TEMPLATE_ID = 'ad4d0e39-201c-40cc-9d63-d805944fbe4c'
TEMPLATE_NAME = 'МСК Товарный чек new'


class APIRejected(PrintBlocked):
    def __init__(self, platform, status):
        self.status = status
        super().__init__(platform + '_http_' + str(status))


class PDFDownloadRejected(PrintBlocked):
    def __init__(self, status):
        self.status = status
        super().__init__('pdf_download_failed')


def reference_id(reference, entity):
    href = (reference or {}).get('meta', {}).get('href', '')
    value = href.rsplit('/', 1)[-1]
    try: canonical = str(UUID(value))
    except (ValueError, TypeError): raise PrintBlocked('ms_reference_invalid') from None
    if href != f'{MS}/entity/{entity}/{canonical}':
        raise PrintBlocked('ms_reference_invalid')
    return canonical


class PrintClients:
    _mutex = threading.Lock()
    _next = {}

    def __init__(self, *, audit_root, request=None, lease_check=lambda: None, delays=True):
        load_dotenv(Path(__file__).resolve().parents[1] / '.env')
        self.audit_root = Path(audit_root)
        self.request = request or requests.request
        self.lease_check, self.delays = lease_check, delays

    def _headers(self, platform, account):
        if platform == 'ms':
            token = os.getenv('MOYSKLAD_TOKEN', '').strip()
            if not token: raise PrintBlocked('ms_token_missing')
            return {'Authorization': 'Bearer ' + token}
        if account not in ACCOUNTS:
            raise PrintBlocked('unsupported_account')
        suffix = account[-4:].upper()
        if platform == 'ozon':
            return {'Client-Id': os.environ['OZON_CLIENT_ID_' + suffix],
                    'Api-Key': os.environ['OZON_API_KEY_' + suffix]}
        if platform == 'wb':
            reference = os.getenv('SHIPMENT_GUARD_WB_TOKEN_ENV_' + suffix, 'WB_TOKEN_' + suffix)
            if not re.fullmatch(r'WB_TOKEN_[A-Z_]*' + suffix, reference):
                raise PrintBlocked('wb_token_reference_invalid')
            token = os.getenv('SHIPMENT_GUARD_WB_TOKEN_' + suffix) or os.getenv(reference)
            if not token: raise PrintBlocked('wb_token_missing')
            return {'Authorization': token}
        if platform == 'yandex' and account == 'ya_acc1':
            return {'Api-Key': os.environ['YANDEX_API_KEY_ACC1']}
        raise PrintBlocked('unsupported_platform_account')

    def _call(self, platform, account, method, url, **kwargs):
        self.lease_check()
        if self.delays:
            interval = {'ms': .15, 'ozon': 1, 'wb': .3, 'yandex': 1, 'download': 0}[platform]
            # Bounded concurrency is for exact read-only posting lookups only.
            # Label creation/polling keep the original one-second pacing.
            if platform == 'ozon' and method == 'POST' and url == 'https://api-seller.ozon.ru/v3/posting/fbs/get':
                interval = .25
            key = platform, account
            with self._mutex:
                pause = max(0, self._next.get(key, 0) - time.monotonic())
                self._next[key] = time.monotonic() + pause + interval
            if pause: time.sleep(pause)
        self.lease_check()
        try:
            return self.request(method, url, timeout=(5, 30), allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise PrintBlocked(platform + '_transport_error') from None

    def _json(self, platform, account, method, url, *, body=None, params=None):
        response = self._call(platform, account, method, url, headers=self._headers(platform, account),
                              json=body, params=params)
        try: value = response.json()
        except ValueError: value = {'invalid_json': True}
        evidence = {'platform': platform, 'account': account, 'http': response.status_code, 'response': value}
        digest = sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        evidence_path = self.audit_root / platform / (account or 'ms') / (digest + '.json')
        if not evidence_path.exists(): save_json(evidence_path, evidence)
        if response.status_code != 200:
            raise APIRejected(platform, response.status_code)
        if not isinstance(value, dict) or value.get('invalid_json'):
            raise PrintBlocked(platform + '_invalid_response')
        return value

    def _read_json(self, platform, account, method, url, **kwargs):
        """Retry only explicitly read-only methods, never export/task creation."""
        for attempt in range(2):
            try:
                return self._json(platform, account, method, url, **kwargs)
            except APIRejected as exc:
                if attempt or exc.status not in (429,500,502,503,504): raise
            except PrintBlocked as exc:
                if attempt or str(exc) != platform + '_transport_error': raise
            if self.delays: time.sleep(1)

    def ms_get(self, path, params=None):
        if path.startswith(MS + '/'):
            path = path[len(MS):]
        if not path.startswith('/entity/') or '?' in path or '..' in path:
            raise PrintBlocked('ms_read_path_invalid')
        if re.fullmatch(r'/entity/customerorder/[0-9a-f-]{36}', path):
            # A single-document GET otherwise returns state.meta only. Both
            # preparation and export must validate the fresh state's name.
            params = dict(params or {})
            expand = [field for field in str(params.get('expand') or '').split(',') if field]
            if 'state' not in expand: expand.append('state')
            params['expand'] = ','.join(expand)
        return self._read_json('ms', '', 'GET', MS + path, params=params)

    def ozon_posting(self, account, number):
        if account not in ('oz_acc1', 'oz_acc2') or not re.fullmatch(r'\d+-\d+-\d+', number):
            raise PrintBlocked('ozon_full_posting_number_required')
        data = self._read_json('ozon', account, 'POST', 'https://api-seller.ozon.ru/v3/posting/fbs/get',
                          body={'posting_number': number, 'with': {'barcodes': True}})
        post = data.get('result', {})
        if post.get('posting_number') != number:
            raise PrintBlocked('ozon_posting_identity_mismatch')
        # Some native QR labels have no scanit. Both barcode fields carry
        # the same actual QR payload; never guess between conflicting codes.
        if post.get('scanit') in (None, ''):
            barcodes = post.get('barcodes') or {}
            code = barcodes.get('lower_barcode')
            if (isinstance(code, str) and code == barcodes.get('upper_barcode')
                    and re.fullmatch(r'[A-Za-z0-9]{1,76}\d{4}', code)):
                return dict(post, scanit=code, scanit_source='barcodes.lower_barcode')
        return post

    def wb_status(self, account, number):
        if account not in ('wb_acc1', 'wb_acc2') or not number.isdigit():
            raise PrintBlocked('wb_assembly_number_required')
        body = {'orders': [int(number)]}
        status = self._read_json('wb', account, 'POST', 'https://marketplace-api.wildberries.ru/api/v3/orders/status', body=body)
        rows = status.get('orders', [])
        if len(rows) != 1 or rows[0].get('id') != int(number) or rows[0].get('supplierStatus') not in ('confirm', 'complete'):
            raise PrintBlocked('wb_order_not_ready_for_label')
        return rows[0]

    def wb_sticker(self, account, number, folder):
        self.wb_status(account, number)
        body = {'orders': [int(number)]}
        data = self._json('wb', account, 'POST', 'https://marketplace-api.wildberries.ru/api/v3/orders/stickers',
                          body=body, params={'type': 'png', 'width': 58, 'height': 40})
        rows = data.get('stickers', [])
        if len(rows) != 1 or rows[0].get('orderId') != int(number):
            raise PrintBlocked('wb_sticker_identity_mismatch')
        sticker = rows[0]
        if not str(sticker.get('partA', '')).isdigit() or not re.fullmatch(r'\d{4}', str(sticker.get('partB', ''))):
            raise PrintBlocked('wb_sticker_code_missing')
        try:
            raw = base64.b64decode(sticker['file'], validate=True)
            image = Image.open(BytesIO(raw)); image.verify()
            image = Image.open(BytesIO(raw))
            if image.size != (580, 400): raise ValueError()
        except (KeyError, ValueError, OSError):
            raise PrintBlocked('wb_sticker_image_invalid') from None
        pdf = FPDF(unit='mm', format=(58, 40))
        pdf.set_auto_page_break(False); pdf.set_margins(0, 0, 0); pdf.add_page()
        pdf.image(BytesIO(raw), x=0, y=0, w=58, h=40)
        atomic_bytes(folder / 'label.pdf', bytes(pdf.output()))
        save_json(folder / 'label_source.json', sticker)
        return sticker

    def _download(self, url, *, allowed):
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.username or parsed.password
                or not any(parsed.hostname == host or (parsed.hostname or '').endswith('.' + host) for host in allowed)):
            raise PrintBlocked('label_download_origin_invalid')
        for attempt in range(2):
            try:
                response = self._call('download', '', 'GET', url)
            except PrintBlocked as exc:
                if attempt or str(exc) != 'download_transport_error': raise
            else:
                if attempt or response.status_code not in (429, 500, 502, 503, 504): break
            if self.delays: time.sleep(1)
        if response.status_code != 200:
            raise PDFDownloadRejected(response.status_code)
        if len(response.content) > 20_000_000:
            raise PrintBlocked('pdf_download_failed')
        pdf_reader(response.content)
        return response.content

    def ozon_label(self, account, posting, folder):
        task_file = folder / 'label_task.json'
        if task_file.exists():
            task = json.loads(task_file.read_text())
        else:
            intent = folder / 'label_task_intent.json'
            journal = json.loads(intent.read_text()) if intent.exists() else {}
            if journal and journal.get('posting_number') != posting['posting_number']:
                raise PrintBlocked('ozon_label_task_identity_mismatch')
            if journal.get('state') == 'retryable_transport_error':
                if time.time() < journal.get('retry_after', 0):
                    raise PrintBlocked('ozon_label_retry_pending')
                posting = self.ozon_posting(account, posting['posting_number'])
                if not ozon_label_available(posting):
                    raise PrintBlocked('ozon_posting_not_ready_for_label')
            elif journal and journal.get('state') != 'rejected':
                raise PrintBlocked('ozon_label_task_result_uncertain')
            # Only PDF-label task creation may be retried after a known transport
            # failure. It neither ships an order nor creates an MS document.
            # Legacy uncertain intents and MS exports keep their strict guards.
            for attempt in range(2):
                journal.update(posting_number=posting['posting_number'], state='sent',
                               attempts=journal.get('attempts', 0)+1, attempted_at=time.time())
                save_json(intent, journal)
                try:
                    data = self._json('ozon', account, 'POST', 'https://api-seller.ozon.ru/v3/posting/fbs/package-label/create',
                                      body={'posting_numbers': [posting['posting_number']]})
                    break
                except APIRejected as exc:
                    if 400 <= exc.status < 500:
                        journal['state'] = 'rejected'; save_json(intent, journal)
                    raise
                except PrintBlocked as exc:
                    if str(exc) != 'ozon_transport_error' or not ozon_label_available(posting):
                        raise
                    journal.update(state='retryable_transport_error', retry_after=time.time()+60,
                                   last_error=str(exc))
                    journal.setdefault('transport_errors', []).append(
                        {'at':time.time(), 'attempt':journal['attempts']})
                    save_json(intent, journal)
                    if attempt: raise
                    posting = self.ozon_posting(account, posting['posting_number'])
                    if not ozon_label_available(posting):
                        raise PrintBlocked('ozon_posting_not_ready_for_label')
            tasks = data.get('tasks', [])
            if len(tasks) != 1 or not tasks[0].get('task_id'):
                raise PrintBlocked('ozon_label_tasks_ambiguous')
            task = tasks[0]; save_json(task_file, task)
            journal.update(state='task_received', task_id=task['task_id'])
            save_json(intent, journal)
        deadline = time.monotonic() + 90
        while True:
            data = self._read_json('ozon', account, 'POST', 'https://api-seller.ozon.ru/v2/posting/fbs/package-label/get',
                              body={'task_id': task['task_id']})
            save_json(folder / 'label_status.json', data)
            status = data.get('status', {})
            if status.get('code') == 'completed':
                if status.get('unprinted_postings') or status.get('printed_postings_count') != 1:
                    raise PrintBlocked('ozon_label_incomplete')
                raw = self._download(data.get('file_url', ''), allowed=('ozone.ru', 'ozon.ru', 'ozonusercontent.com'))
                expected = posting.get('multi_box_qty')
                if isinstance(expected, int) and expected > 1 and len(pdf_reader(raw).pages) != expected:
                    raise PrintBlocked('ozon_box_count_mismatch')
                atomic_bytes(folder / 'label.pdf', raw)
                return
            if status.get('code') in ('error', 'failed') or data.get('error'):
                raise PrintBlocked('ozon_label_generation_failed')
            if time.monotonic() >= deadline:
                raise PrintBlocked('ozon_label_still_generating')
            time.sleep(2)

    def yandex_order(self, number):
        if not number.isdigit(): raise PrintBlocked('market_order_number_required')
        business = os.environ['YANDEX_BUSINESS_ID_ACC1']
        if not business.isdigit(): raise PrintBlocked('market_business_id_invalid')
        data = self._read_json('yandex', 'ya_acc1', 'POST', f'https://api.partner.market.yandex.ru/v1/businesses/{business}/orders',
                          body={'orderIds': [int(number)], 'programTypes': ['FBS', 'EXPRESS'], 'fake': False}, params={'limit': 20})
        orders = data.get('orders', [])
        if (len(orders) != 1 or orders[0].get('orderId') != int(number)
                or orders[0].get('programType') not in ('FBS', 'EXPRESS') or orders[0].get('fake') is not False):
            raise PrintBlocked('market_order_identity_mismatch')
        return orders[0]

    def yandex_label(self, order, folder):
        campaign, number = order.get('campaignId'), order['orderId']
        if type(campaign) is not int or campaign <= 0: raise PrintBlocked('market_campaign_missing')
        base = f'https://api.partner.market.yandex.ru/v2/campaigns/{campaign}/orders/{number}/delivery/labels'
        info = self._read_json('yandex', 'ya_acc1', 'GET', base + '/data').get('result', {})
        boxes = info.get('parcelBoxLabels', [])
        count = info.get('placesNumber')
        if type(count) is not int or count < 1 or len(boxes) != count or len({b.get('boxId') for b in boxes}) != count:
            raise PrintBlocked('market_box_list_incomplete')
        response = self._call('yandex', 'ya_acc1', 'GET', base, headers=self._headers('yandex', 'ya_acc1'),
                              params={'format': 'A9_HORIZONTALLY'})
        if response.status_code != 200: raise PrintBlocked('market_label_http_' + str(response.status_code))
        if len(pdf_reader(response.content).pages) != count: raise PrintBlocked('market_label_box_count_mismatch')
        atomic_bytes(folder / 'label.pdf', response.content); save_json(folder / 'label_source.json', info)

    def export_receipt(self, order_id, demand_id, folder, *, expected_account, expected_number, expected_day=None, allow_unprinted=False):
        result = folder / 'receipt_ms.pdf'
        if result.exists(): pdf_reader(result.read_bytes()); return result.read_bytes()
        intent = folder / 'ms_export_intent.json'
        response_file = folder / 'ms_export_response.json'
        expired_download = None
        if intent.exists():
            previous = json.loads(intent.read_text())
            if previous.get('order_id') != order_id or previous.get('demand_id') != demand_id:
                raise PrintBlocked('ms_export_cache_identity_mismatch')
            if response_file.exists():
                response = json.loads(response_file.read_text())
                if response.get('http') == 303 and response.get('location'):
                    try:
                        raw = self._download(response['location'], allowed=('moysklad.ru',))
                    except PDFDownloadRejected as exc:
                        if exc.status not in (404,410): raise
                        expired_download = exc.status
                    else:
                        atomic_bytes(result, raw)
                        return raw
            if expired_download is None and previous.get('state') != 'rejected':
                raise PrintBlocked('ms_export_result_uncertain')
        template = self.ms_get('/entity/demand/metadata/customtemplate/' + TEMPLATE_ID)
        if template.get('name') != TEMPLATE_NAME or template.get('meta', {}).get('type') != 'customtemplate':
            raise PrintBlocked('ms_receipt_template_changed')
        order = self.ms_get('/entity/customerorder/' + str(UUID(order_id)))
        demand = self.ms_get('/entity/demand/' + str(UUID(demand_id)))
        from shipment_guard.config import identity
        if identity(order)[1] != expected_account or order.get('name') != expected_number:
            raise PrintBlocked('ms_export_order_identity_mismatch')
        if expected_day is not None and not selected_order(order, expected_account,
                datetime.combine(expected_day, daytime(12), tzinfo=MOSCOW)):
            raise PrintBlocked('order_changed_since_counter_click')
        if allow_unprinted:
            guard_receipt_link(order, demand, linked_order_id=order_id)
        else:
            guard_sample_export(order, demand, linked_order_id=order_id)
        if expired_download is not None:
            # Only a confirmed old export and an already printed shipment may
            # regenerate an expired PDF. Archive its response BEFORE a new POST:
            # a lost new response must never be mistaken for the old 303.
            guard_sample_export(order, demand, linked_order_id=order_id)
            save_json(folder / ('expired_download_' + uuid4().hex + '.json'),
                      {'intent':previous, 'response':response, 'download_http':expired_download})
            response_file.unlink()
        save_json(folder / 'ms_before.json', {'order': order, 'demand': demand})
        payload = {'template': {'meta': template['meta']}, 'extension': 'pdf'}
        save_json(intent, {'order_id': order_id, 'demand_id': demand_id, 'request': payload, 'state': 'sent'})
        try:
            response = self._call('ms', '', 'POST', MS + '/entity/demand/' + demand_id + '/export',
                                  headers=self._headers('ms', ''), json=payload)
            save_json(folder / 'ms_export_response.json', {'http': response.status_code, 'location': response.headers.get('Location')})
            if response.status_code == 303:
                raw = self._download(response.headers.get('Location', ''), allowed=('moysklad.ru',))
            elif response.status_code == 200:
                raw = response.content; pdf_reader(raw)
            else:
                if 400 <= response.status_code < 500:
                    save_json(intent, {'order_id': order_id, 'demand_id': demand_id, 'request': payload, 'state': 'rejected'})
                raise PrintBlocked('ms_export_http_' + str(response.status_code))
            atomic_bytes(result, raw)
        finally:
            after_order = self.ms_get('/entity/customerorder/' + order_id)
            after_demand = self.ms_get('/entity/demand/' + demand_id)
            save_json(folder / 'ms_after.json', {'order': after_order, 'demand': after_demand})
        return raw
