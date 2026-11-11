# поток: fin
"""Fixed warehouse meeting report, behind the existing BI access controls."""
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(tags=["Нагрузка склада"])
REPORT_DIR = Path(__file__).resolve().parent / "static/reports/warehouse-load-2026-10"
YEAR_REPORT_DIR = Path(__file__).resolve().parent / "static/reports/warehouse-load-2026"
HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


@router.get("/reports/warehouse-load-2026-10")
def warehouse_workload_report():
    return FileResponse(REPORT_DIR / "index.html", media_type="text/html", headers=HEADERS)


@router.get("/reports/warehouse-load-2026-10.xlsx")
def warehouse_workload_workbook():
    return FileResponse(
        REPORT_DIR / "report.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="warehouse-load-october-2026.xlsx",
        headers=HEADERS,
    )


@router.get("/reports/warehouse-load-2026")
def warehouse_workload_year_report():
    return FileResponse(YEAR_REPORT_DIR / "index.html", media_type="text/html", headers=HEADERS)


@router.get("/reports/warehouse-load-2026.xlsx")
def warehouse_workload_year_workbook():
    return FileResponse(
        YEAR_REPORT_DIR / "report.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="warehouse-load-2026.xlsx",
        headers=HEADERS,
    )


@router.get("/reports/warehouse-load-2026.txt")
def warehouse_workload_procedure():
    return FileResponse(
        YEAR_REPORT_DIR / "procedure.txt",
        media_type="text/plain",
        filename="warehouse-procedure-draft.txt",
        headers=HEADERS,
    )
