# поток: mkt
"""Explicit report links behind the existing WB dashboard authentication."""
from pathlib import Path
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

REPORTS = Path(__file__).resolve().parents[1] / 'docs' / 'reports'
PREFIX = '/api/wb-bids/reports'
FILES = {
    'glue-24h-2026-10-11': 'mkt_wb_glue_24h_2026-10-11.html',
    'glue-24h-2026-10-11-data': 'mkt_wb_glue_24h_2026-10-11.json',
    'glue-regroup-2026-10-10': 'mkt_wb_bundle_regroup_2026-10-10_acc1/index.html',
    'glue-core-2026-10-10': 'mkt_wb_core_glue_2026-10-10_acc1/index.html',
    'glue-chips-2026-10-10': 'mkt_wb_chip_progress_2026-10-10.html',
    'glue-manual-2026-10-12': 'mkt_wb_glue_manual_2026-10-12.html',
    'glue-core-manual-2026-10-12': 'mkt_wb_core_manual_2026-10-12.html',
    'glue-examples-2026-10-12': 'mkt_wb_glue_examples_2026-10-12.html',
    'glue-missing-parents-2026-10-12': 'mkt_wb_missing_parent_cards_2026-10-12.html',
    'glue-reviewed-2026-10-12': 'mkt_wb_review_outcomes_2026-10-12.html',
    'glue-colour-chip-review-2026-10-12': 'mkt_wb_colour_chip_review_2026-10-12_acc1/index.html',
    'glue-colour-applied-2026-10-12': 'mkt_wb_colour_chip_review_2026-10-12_acc1/approved_apply/index.html',
    'glue-colour-set-details-2026-10-12': 'mkt_wb_colour_set_explanations_2026-10-12_acc1/index.html',
    'glue-orphan-stock-2026-10-12': 'mkt_wb_orphan_families_2026-10-12_acc1/index.html',
    'wb-budgets-2026-10-12': 'mkt_wb_budgets_2026-10-12.html',
    'wb-budget-topup-2026-10-12': 'mkt_wb_budget_topup_2026-10-12.html',
}
router = APIRouter()


@router.get(PREFIX + '/{key}')
def report(key: str):
    relative = FILES.get(key)
    if relative is None:
        raise HTTPException(status_code=404, detail='Report not found')
    path = REPORTS / relative
    if not path.is_file():
        raise HTTPException(status_code=404, detail='Report not found')
    text = path.read_text(encoding='utf-8')
    headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
    if path.suffix == '.json':
        data = json.loads(text)
        # Downloadable summary does not need the full card-content evidence.
        if isinstance(data.get('stopped_operation'), dict):
            data['stopped_operation'].pop('groups', None)
        return JSONResponse(data, headers=headers)
    for report_key, file_name in FILES.items():
        text = text.replace('href="' + file_name + '"',
                            'href="' + PREFIX + '/' + report_key + '"')
    return HTMLResponse(text, headers=headers)
