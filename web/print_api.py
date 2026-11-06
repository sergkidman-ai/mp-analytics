# поток: print
from functools import lru_cache
from datetime import date
import json
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from print_service.domain import PrintBlocked
from print_service.access import PrintAccess, access_for_user
from print_service.service import PrintService, error_message


class CounterRequest(BaseModel):
    counter_key: str = Field(min_length=1, max_length=300)
    request_key: UUID


class OrderRequest(BaseModel):
    request_key: UUID


@lru_cache(maxsize=1)
def production_service():
    return PrintService()


def make_router(get_service=production_service, *, standalone=False):
    router = APIRouter(tags=['Печать заказов'])

    def current_access(request: Request):
        try:
            return access_for_user(request.headers.get('X-Pult-User'), dashboard=not standalone)
        except PrintBlocked:
            raise HTTPException(403, 'Нет доступа к странице печати.') from None

    @router.get('/warehouse/print', response_class=HTMLResponse)
    @router.get('/warehouse/print/archive', response_class=HTMLResponse)
    def page(request: Request, access: PrintAccess = Depends(current_access)):
        from re import sub
        html = Path(__file__).with_name('static').joinpath('print_orders.html').read_text()
        organizations = json.dumps(access.organizations, ensure_ascii=False).replace('<', '\\u003c')
        html = sub(r'(<script id="print-organizations" type="application/json">).*?(</script>)',
                   lambda match: match[1] + organizations + match[2], html, count=1)
        if access.restricted:
            organization = access.organizations[0]['name']
            html = html.replace('<body>', '<body class="single-organization">', 1)
            html = html.replace('<h1>Печать заказов</h1>', f'<h1>Печать заказов · {organization}</h1>', 1)
            html = html.replace('<title>Печать заказов · Пульт бизнеса</title>', f'<title>Печать заказов · {organization}</title>', 1)
        if standalone:
            # This host serves printing alone; links to other Pult sections
            # stay in the dashboard version of the same page.
            aside = '<aside><a class="brand" href="/warehouse/print">Пульт бизнеса</a><a class="current" href="/warehouse/print">Печать заказов</a><a href="/warehouse/print/archive">Архив</a></aside>'
            tabs = '<nav class="tabs"><a class="current" href="/warehouse/print">Печать заказов</a><a href="/warehouse/print/archive">Архив</a></nav>'
            html = sub(r'<aside>.*?</aside>', lambda _: aside, html, count=1)
            html = sub(r'<nav class="tabs">.*?</nav>', lambda _: tabs, html, count=1)
        if request.url.path.endswith('/archive'):
            html = html.replace('class="current" href="/warehouse/print"', 'href="/warehouse/print"')
            html = html.replace('href="/warehouse/print/archive"', 'class="current" href="/warehouse/print/archive"')
        return HTMLResponse(html, headers={'Cache-Control': 'no-store'})

    @router.get('/api/print')
    def data(access: PrintAccess = Depends(current_access)):
        return access.status(get_service().status())

    @router.get('/api/print/archive')
    def archive(day: date | None = None, access: PrintAccess = Depends(current_access)):
        try:
            return get_service().archive(day=day, accounts=access.accounts)
        except ValueError:
            raise HTTPException(422, 'В архиве доступны только прошедшие дни по Москве.') from None

    @router.post('/api/print/sync')
    def synchronize(access: PrintAccess = Depends(current_access)):
        try: return dict(id=get_service().request_sync())
        except PrintBlocked as exc:
            raise HTTPException(409, 'Обработчик пока недоступен.' if str(exc) == 'worker_unavailable' else error_message(exc)) from None

    @router.post('/api/print/counter')
    def counter(request: CounterRequest, access: PrintAccess = Depends(current_access)):
        if not access.allows_counter(request.counter_key):
            raise HTTPException(403, 'Нет доступа к заказам этой организации.')
        try:
            job = get_service().request_counter(request.counter_key, str(request.request_key))
            return dict(id=str(job['id']), state=job['state'], count=len(job['orders']))
        except PrintBlocked as exc:
            raise HTTPException(409, 'Обработчик пока недоступен.' if str(exc) == 'worker_unavailable' else error_message(exc)) from None

    @router.get('/api/print/batches/{batch_id}/files/{kind}')
    def file(batch_id: UUID, kind: Literal['receipts', 'labels'], access: PrintAccess = Depends(current_access)):
        service = get_service()
        if access.restricted:
            try:
                batch = service.queue.batch(str(batch_id))
            except PrintBlocked:
                raise HTTPException(404, 'Файл не найден.') from None
            if not access.allows_batch(batch):
                raise HTTPException(404, 'Файл не найден.')
        try:
            path = service.queue.ready_files(str(batch_id))[kind]
        except PrintBlocked as exc:
            raise HTTPException(409, error_message(exc)) from None
        return FileResponse(path, media_type='application/pdf',
                            filename=f'{kind}_{str(batch_id)[:8]}.pdf', content_disposition_type='inline',
                            headers={'Cache-Control': 'private, no-cache'})

    def checked_order(service, order_id, access):
        try: order = service.queue.order(str(order_id))
        except PrintBlocked: raise HTTPException(404, 'Заказ не найден.') from None
        if not access.allows(order['account']): raise HTTPException(404, 'Заказ не найден.')
        if access.restricted and order['batch_state'] == 'ready':
            if not access.allows_batch(service.queue.batch(str(order['batch_id']))):
                raise HTTPException(404, 'Заказ не найден.')
        return order

    @router.post('/api/print/orders/{order_id}/pair')
    def single_order(order_id: UUID, request: OrderRequest, access: PrintAccess = Depends(current_access)):
        service = get_service(); checked_order(service, order_id, access)
        try: return service.request_order(str(order_id), str(request.request_key))
        except PrintBlocked as exc:
            raise HTTPException(409, error_message(exc)) from None

    @router.get('/api/print/orders/{order_id}/files/{kind}')
    def single_file(order_id: UUID, kind: Literal['receipts', 'labels'], access: PrintAccess = Depends(current_access)):
        service = get_service(); checked_order(service, order_id, access)
        try: path = service.order_files(str(order_id))[kind]
        except PrintBlocked as exc: raise HTTPException(409, error_message(exc)) from None
        return FileResponse(path, media_type='application/pdf', filename=f'{kind}_{str(order_id)[:8]}.pdf',
                            content_disposition_type='inline', headers={'Cache-Control':'private, no-cache'})

    return router


router = make_router()
