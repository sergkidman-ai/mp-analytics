# поток: print
from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from print_service.domain import PrintBlocked
from print_service.service import PrintService, error_message


class CounterRequest(BaseModel):
    counter_key: str = Field(min_length=1, max_length=300)
    request_key: UUID


@lru_cache(maxsize=1)
def production_service():
    return PrintService()


def make_router(get_service=production_service):
    router = APIRouter(tags=['Печать заказов'])

    @router.get('/warehouse/print', response_class=HTMLResponse)
    def page():
        return HTMLResponse(Path(__file__).with_name('static').joinpath('print_orders.html').read_text(),
                            headers={'Cache-Control': 'no-store'})

    @router.get('/api/print')
    def data():
        return get_service().status()

    @router.post('/api/print/sync')
    def synchronize():
        try: return dict(id=get_service().request_sync())
        except PrintBlocked as exc:
            raise HTTPException(409, 'Обработчик пока недоступен.' if str(exc) == 'worker_unavailable' else error_message(exc)) from None

    @router.post('/api/print/counter')
    def counter(request: CounterRequest):
        try:
            job = get_service().request_counter(request.counter_key, str(request.request_key))
            return dict(id=str(job['id']), state=job['state'], count=len(job['orders']))
        except PrintBlocked as exc:
            raise HTTPException(409, 'Обработчик пока недоступен.' if str(exc) == 'worker_unavailable' else error_message(exc)) from None

    @router.get('/api/print/batches/{batch_id}/files/{kind}')
    def file(batch_id: UUID, kind: Literal['receipts', 'labels']):
        try:
            path = get_service().queue.ready_files(str(batch_id))[kind]
        except PrintBlocked as exc:
            raise HTTPException(409, error_message(exc)) from None
        return FileResponse(path, media_type='application/pdf',
                            filename=f'{kind}_{str(batch_id)[:8]}.pdf', content_disposition_type='inline',
                            headers={'Cache-Control': 'private, no-cache'})

    return router


router = make_router()
