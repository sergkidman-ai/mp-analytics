# поток: fin
"""Read-only accountant KUDIR reports and fixed original downloads."""
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from typing import Literal

from reports import kudir_page, kudir_report, kudir_tax

router = APIRouter(tags=["КУДиР"])
PRIVATE_HEADERS = {"Cache-Control": "private, no-store"}


Organisation = Literal["cifrovoi", "diskver"]


def _quarter(org, quarter):
    if quarter is None:
        return 3
    if quarter not in kudir_report.quarters(org):
        raise HTTPException(404, "КУДиР за этот квартал для выбранного юрлица нет")
    return quarter


def _data(quarter, org="cifrovoi"):
    try:
        data = dict(kudir_report.load(quarter, org))
        data['income_confirmation'] = kudir_tax.confirmed_income(data)
        data['tax'] = kudir_tax.load(org, quarter)
        return data
    except (FileNotFoundError, ValueError):
        raise HTTPException(503, "Файл КУДиР недоступен или его формат изменился")


@router.get("/reports/kudir", response_class=HTMLResponse)
def kudir_page_route(quarter: int | None = Query(None, ge=1, le=4), org: Organisation = "cifrovoi"):
    quarter = _quarter(org, quarter)
    _data(quarter, org)
    try:
        return HTMLResponse(kudir_page.html_for(quarter, org), headers=PRIVATE_HEADERS)
    except (FileNotFoundError, ValueError):
        raise HTTPException(503, "Материалы КУДиР недоступны")


@router.get("/api/reports/kudir")
def kudir_data(quarter: int | None = Query(None, ge=1, le=4), org: Organisation = "cifrovoi"):
    return JSONResponse(_data(_quarter(org, quarter), org), headers=PRIVATE_HEADERS)


@router.get("/reports/kudir/file/{quarter}")
def kudir_original(quarter: int, org: Organisation = "cifrovoi"):
    _quarter(org, quarter)
    path = kudir_report.source_path(quarter, org)
    if not path.is_file():
        raise HTTPException(404, "Оригинал КУДиР недоступен")
    if org == "diskver":
        filename, media_type = "KUDIR_Diskver_2026.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        filename, media_type = f"KUDIR_Cifrovoi_2026_Q{quarter}.xls", "application/vnd.ms-excel"
    return FileResponse(path, filename=filename, media_type=media_type, headers=PRIVATE_HEADERS)
