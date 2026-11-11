# поток: mkt
"""Explicit report links behind the existing WB dashboard authentication."""
from pathlib import Path
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, PlainTextResponse

REPORTS = Path(__file__).resolve().parents[1] / 'docs' / 'reports'
PREFIX = '/api/wb-bids/reports'
FILES = {
    'ozon-fixed20-margin-2026-11-11': 'ozon_recovery_20261103/daily_20261111/margin_review/index.html',
    'ozon-fixed20-margin-2026-11-11-data': 'ozon_recovery_20261103/daily_20261111/margin_review/analysis.json',
    'ozon-fixed20-margin-2026-11-11-daily': 'ozon_recovery_20261103/daily_20261111/margin_review/5378_daily.csv',
    'ozon-fixed20-2026-11-11': 'ozon_recovery_20261103/daily_20261111/index.html',
    'ozon-fixed20-2026-11-11-periods': 'ozon_recovery_20261103/daily_20261111/equal11_periods.csv',
    'ozon-fixed20-2026-11-11-daily': 'ozon_recovery_20261103/daily_20261111/monitor20_daily.csv',
    'ozon-fixed20-2026-11-11-skus': 'ozon_recovery_20261103/daily_20261111/by_sku_20.csv',
    'ozon-fixed20-2026-11-11-data': 'ozon_recovery_20261103/daily_20261111/status.json',
    'ozon-wave1-profile-2026-11-11': 'ozon_wave1_profile_20261111/index.html',
    'ozon-wave1-profile-2026-11-11-campaigns': 'ozon_wave1_profile_20261111/campaigns.csv',
    'ozon-wave1-profile-2026-11-11-profile': 'ozon_wave1_profile_20261111/profile_comparison.csv',
    'ozon-wave1-profile-2026-11-11-daily': 'ozon_wave1_profile_20261111/account_daily.csv',
    'ozon-wave1-profile-2026-11-11-data': 'ozon_wave1_profile_20261111/analysis.json',
    'ozon-wave1-status-2026-11-10': 'ozon_wave1_status_20261110/index.html',
    'ozon-wave1-status-2026-11-10-acc1': 'ozon_wave1_status_20261110/oz_acc1_campaigns.csv',
    'ozon-wave1-status-2026-11-10-acc2': 'ozon_wave1_status_20261110/oz_acc2_campaigns.csv',
    'ozon-wave1-status-2026-11-10-data': 'ozon_wave1_status_20261110/summary.json',
    'wb-budget-topup-low50-2026-11-09': 'wb_budget_topup_low50_20261109/index.html',
    'wb-budget-topup-low50-2026-11-09-csv': 'wb_budget_topup_low50_20261109/verified_result.csv',
    'wb-budget-topup-low50-2026-11-09-data': 'wb_budget_topup_low50_20261109/verification.json',
    'wb-budget-guard-recovery-2026-11-09': 'wb_budget_guard_recovery_20261109/index.html',
    'wb-budget-guard-recovery-2026-11-09-status': 'mkt_ad_budget_guard_latest.json',
    'wb-budget-guard-recovery-2026-11-09-text': 'mkt_ad_budget_guard_latest.txt',
    'wb-capacity-budgets-2026-11-09': 'wb_capacity_budgets_20261109/index.html',
    'wb-capacity-budgets-2026-11-09-all': 'wb_capacity_budgets_20261109/all_campaigns.csv',
    'wb-capacity-budgets-2026-11-09-low': 'wb_capacity_budgets_20261109/budget_le_50.csv',
    'wb-capacity-budgets-2026-11-09-running-low': 'wb_capacity_budgets_20261109/running_budget_le_50.csv',
    'wb-capacity-budgets-2026-11-09-data': 'wb_capacity_budgets_20261109/summary.json',
    'wb-acc1-capacity-probe-2026-11-09': 'wb_acc1_capacity_probe_20261109/index.html',
    'wb-acc1-capacity-probe-2026-11-09-skus': 'wb_acc1_capacity_probe_20261109/test_campaign_50.csv',
    'wb-acc1-capacity-probe-2026-11-09-data': 'wb_acc1_capacity_probe_20261109/summary.json',
    **{f'wb-acc1-capacity-probe-2026-11-09-packing-{n}': f'wb_acc1_capacity_probe_20261109/packing_remaining_{n}_DRAFT.csv' for n in (50, 200, 300)},
    'wb-acc1-ad-audit-2026-11-09': 'wb_acc1_ad_audit_20261109/index.html',
    'wb-acc1-ad-audit-2026-11-09-outside': 'wb_acc1_ad_audit_20261109/outside_running_ads_in_stock.csv',
    'wb-acc1-ad-audit-2026-11-09-none': 'wb_acc1_ad_audit_20261109/outside_all_unfinished_ads_in_stock.csv',
    'wb-acc1-ad-audit-2026-11-09-bundles': 'wb_acc1_ad_audit_20261109/outside_bundles.csv',
    'wb-acc1-ad-audit-2026-11-09-children': 'wb_acc1_ad_audit_20261109/outside_children.csv',
    'wb-acc1-ad-audit-2026-11-09-new': 'wb_acc1_ad_audit_20261109/outside_new_6months.csv',
    'wb-acc1-ad-audit-2026-11-09-old': 'wb_acc1_ad_audit_20261109/outside_old.csv',
    'wb-acc1-ad-audit-2026-11-09-candidates': 'wb_acc1_ad_audit_20261109/bid_candidates.csv',
    'wb-acc1-ad-audit-2026-11-09-floor': 'wb_acc1_ad_audit_20261109/already_at_floor.csv',
    'wb-acc1-ad-audit-2026-11-09-tail': 'wb_acc1_ad_audit_20261109/paid_no_own_or_ad_orders.csv',
    'wb-acc1-ad-audit-2026-11-09-data': 'wb_acc1_ad_audit_20261109/summary.json',
    'ozon-plan-summary-2026-11-05': 'ozon_launch_20261106/index.html',
    'ozon-monthly-plan-2026-11-05': 'ozon_launch_20261106/index.html',
    'ozon-monthly-plan-2026-11-05-wave1': 'ozon_wave_revision_20261106/wave_1_DRAFT.csv',
    'ozon-monthly-plan-2026-11-05-wave2': 'ozon_wave_revision_20261106/wave_2_DRAFT.csv',
    'ozon-monthly-plan-2026-11-05-wave3': 'ozon_wave_revision_20261106/wave_3_DRAFT.csv',
    'ozon-monthly-plan-2026-11-05-all': 'ozon_wave_revision_20261106/wave_draft_all_candidates.csv',
    'ozon-monthly-plan-2026-11-05-year-flags': 'ozon_wave_revision_20261106/year_flags_for_wave_draft.csv',
    'ozon-monthly-plan-2026-11-05-data': 'ozon_wave_revision_20261106/wave_draft_manifest.json',
    'ozon-monthly-plan-2026-11-05-cost': 'ozon_wave_revision_20261106/cost_preview.json',
    'ozon-waves-2026-11-06': 'ozon_launch_20261106/index.html',
    'ozon-launch-2026-11-06': 'ozon_launch_20261106/index.html',
    'ozon-fixed20-2026-11-09': 'ozon_recovery_20261103/daily_20261109/index.html',
    'ozon-fixed20-2026-11-09-periods': 'ozon_recovery_20261103/daily_20261109/equal_9day_periods.csv',
    'ozon-fixed20-2026-11-09-daily': 'ozon_recovery_20261103/daily_20261109/monitor20_daily.csv',
    'ozon-fixed20-2026-11-09-skus': 'ozon_recovery_20261103/daily_20261109/monitor20_sku_summary_20.csv',
    'ozon-acc2-monthly-experiment-2026-11-08-deferred': 'ozon_acc2_monthly_experiment_20261108/activation_20261109/deferred_19_for_return.csv',
    'ozon-acc2-monthly-experiment-2026-11-08-ready': 'ozon_acc2_monthly_experiment_20261108/activation_20261109/launched_wave1.csv',
    'ozon-fixed20-2026-11-07': 'ozon_recovery_20261103/daily_20261107/index.html',
    'ozon-fixed20-2026-11-07-skus': 'ozon_recovery_20261103/daily_20261107/weekly_by_sku_20.csv',
    'ozon-bid-candidates-2026-11-08': 'ozon_bid_candidates_20261108/index.html',
    'ozon-bid-candidates-2026-11-08-skus': 'ozon_bid_candidates_20261108/candidate_shortlist.csv',
    'ozon-bid-candidates-2026-11-08-plan': 'ozon_bid_candidates_20261108/draft_bid_plan.csv',
    'ozon-acc2-bid-candidates-2026-11-08': 'ozon_acc2_bid_candidates_20261108/index.html',
    'ozon-acc2-bid-candidates-2026-11-08-skus': 'ozon_acc2_bid_candidates_20261108/candidate_shortlist.csv',
    'ozon-acc2-bid-candidates-2026-11-08-plan': 'ozon_acc2_bid_candidates_20261108/draft_bid_plan.csv',
    'ozon-acc2-bid-candidates-2026-11-08-zero': 'ozon_acc2_bid_candidates_20261108/zero_model_sales_instock.csv',
    'ozon-acc2-bid-candidates-2026-11-08-floor': 'ozon_acc2_bid_candidates_20261108/at_floor_review.csv',
    'ozon-acc2-bid-candidates-2026-11-08-funding': 'ozon_acc2_bid_candidates_20261108/funding_by_campaign.csv',
    'ozon-acc2-inventory-bonus-2026-11-08': 'ozon_acc2_inventory_bonus_20261108/index.html',
    'ozon-acc2-inventory-bonus-2026-11-08-all': 'ozon_acc2_inventory_bonus_20261108/all_outside_cpc_in_stock.csv',
    'ozon-acc2-inventory-bonus-2026-11-08-changes': 'ozon_acc2_inventory_bonus_20261108/classification_changes.csv',
    'ozon-accounts-inventory-2026-11-08': 'ozon_accounts_inventory_compare_20261108/index.html',
    'ozon-accounts-inventory-2026-11-08-table': 'ozon_accounts_inventory_compare_20261108/comparison.csv',
    'ozon-child-gap-2026-11-08': 'ozon_child_gap_audit_20261108/index.html',
    'ozon-autoarchive-dates-2026-11-08': 'ozon_autoarchive_dates_20261108/index.html',
    'ozon-autoarchive-brand-api-2026-11-08': 'ozon_autoarchive_brand_api_20261108/index.html',
    'ozon-autoarchive-brand-api-2026-11-08-skus': 'ozon_autoarchive_brand_api_20261108/autoarchived_brand_by_SKU.csv',
    'ozon-dsprint-restore-2026-11-08': 'ozon_dsprint_restore_20261108/index.html',
    'ozon-dsprint-restore-2026-11-08-skus': 'ozon_dsprint_restore_20261108/queue_status.csv',
    'ozon-acc2-monthly-experiment-2026-11-08': 'ozon_acc2_monthly_experiment_20261108/index.html',
    'ozon-acc2-monthly-experiment-2026-11-08-guard': 'ozon_acc2_monthly_experiment_20261108/guard_status.json',
    'ozon-acc2-monthly-experiment-2026-11-08-limits': 'ozon_acc2_monthly_experiment_20261108/limits_from_acc1/limits_public.json',
    'ozon-acc2-monthly-experiment-2026-11-08-wave1': 'ozon_acc2_monthly_experiment_20261108/activation_20261109/launched_wave1.csv',
    'ozon-acc2-monthly-experiment-2026-11-08-wave2': 'ozon_acc2_monthly_experiment_20261108/wave_2_DRAFT.csv',
    'ozon-acc2-monthly-experiment-2026-11-08-all': 'ozon_acc2_monthly_experiment_20261108/all_waves_DRAFT.csv',
    'ozon-acc2-monthly-experiment-2026-11-08-baseline': 'ozon_acc2_monthly_experiment_20261108/activation_20261109/baseline/launched_sku_model_family_baseline.csv',
    'ozon-model-map-comparison-2026-11-08-children': 'ozon_accounts_inventory_compare_20261108/model_comparison/child_shared_model_anomalies.csv',
    'ozon-acc2-monthly-experiment-2026-11-08-budgets': 'ozon_acc2_monthly_experiment_20261108/create_wave1/ozon_required_budgets.csv',
    'ozon-acc2-monthly-experiment-2026-11-08-uploaded': 'ozon_acc2_monthly_experiment_20261108/activation_20261109/launched_wave1.csv',
    'ozon-model-map-comparison-2026-11-08': 'ozon_accounts_inventory_compare_20261108/model_comparison/index.html',
    'ozon-model-map-comparison-2026-11-08-groups': 'ozon_accounts_inventory_compare_20261108/model_comparison/core_native_model_groups.csv',
    'ozon-model-map-comparison-2026-11-08-members': 'ozon_accounts_inventory_compare_20261108/model_comparison/native_model_members.csv',
    'ozon-model-map-comparison-2026-11-08-families': 'ozon_accounts_inventory_compare_20261108/model_comparison/article_family_overlap.csv',
    'ozon-autoarchive-dates-2026-11-08-acc2': 'ozon_autoarchive_dates_20261108/acc2_autoarchived_children_dates.csv',
    'ozon-autoarchive-dates-2026-11-08-acc1': 'ozon_autoarchive_dates_20261108/acc1_autoarchived_all.csv',
    'ozon-autoarchive-dates-2026-11-08-cohorts': 'ozon_autoarchive_dates_20261108/acc2_creation_cohorts.csv',
    'ozon-child-gap-2026-11-08-all': 'ozon_child_gap_audit_20261108/acc1_stock_children_to_acc2.csv',
    'ozon-child-gap-2026-11-08-edges': 'ozon_child_gap_audit_20261108/matched_counterpart_edges.csv',
    'ozon-child-gap-2026-11-08-families': 'ozon_child_gap_audit_20261108/family_gaps.csv',
    'ozon-child-gap-2026-11-08-archive': 'ozon_child_gap_audit_20261108/acc2_all_children_with_archive.csv',
    **{f'ozon-child-gap-2026-11-08-{status}': f'ozon_child_gap_audit_20261108/{status}.csv'
       for status in ('LIVE_IN_STOCK', 'LIVE_NO_FREE_STOCK', 'AUTOARCHIVED', 'ARCHIVED_OTHER',
                      'NO_CHILD_FAMILY_IN_ACC2', 'NO_EXACT_PRINTER_MATCH_REVIEW')},
    **{f'ozon-accounts-inventory-2026-11-08-{account}-{group}':
       f'ozon_accounts_inventory_compare_20261108/{account}_{group}.csv'
       for account in ('oz_acc1', 'oz_acc2') for group in ('bundle', 'child')},
    **{f'ozon-acc2-inventory-bonus-2026-11-08-{group}': f'ozon_acc2_inventory_bonus_20261108/{group}.csv'
       for group in ('bundles', 'children', 'new_6months', 'old', 'unknown_type', 'unknown_date')},
    **{f'ozon-acc2-inventory-bonus-2026-11-08-x{q}': f'ozon_acc2_inventory_bonus_20261108/bundles_x{q}.csv'
       for q in (3, 6, 9, 12)},
    'ozon-launch-2026-11-06-uploaded': 'ozon_launch_20261106/uploaded_sku_manifest.csv',
    'ozon-launch-2026-11-06-wave1-live': 'ozon_launch_20261106/wave1_live_manifest.csv',
    'ozon-launch-2026-11-06-activation': 'ozon_launch_20261106/activation_status.json',
    'ozon-launch-2026-11-06-excluded': 'ozon_launch_20261106/not_uploaded_own_failures.csv',
    'ozon-launch-2026-11-06-campaigns': 'ozon_launch_20261106/campaign_results.csv',
    'ozon-launch-2026-11-06-budgets': 'ozon_launch_20261106/ozon_required_budgets.json',
    'ozon-launch-2026-11-06-baseline-skus': 'ozon_launch_20261106/baseline/sku_baseline_28days.csv',
    'ozon-launch-2026-11-06-baseline-families': 'ozon_launch_20261106/baseline/general_sales_daily.csv',
    'ozon-launch-2026-11-06-baseline-family-totals': 'ozon_launch_20261106/baseline/family_baseline_28days.csv',
    'ozon-launch-2026-11-06-baseline-account': 'ozon_launch_20261106/baseline/account_daily.csv',
    'ozon-launch-2026-11-06-guard': 'ozon_launch_20261106/guard_status.json',
    'ozon-launch-2026-11-06-policy': 'ozon_launch_20261106/risk_policy.json',
    'ozon-launch-2026-11-06-data': 'ozon_launch_20261106/launch_summary.json',
    'ozon-waves-2026-11-06-all': 'ozon_wave_revision_20261106/wave_draft_all_candidates.csv',
    'ozon-waves-2026-11-06-campaigns': 'ozon_wave_revision_20261106/campaign_plan.csv',
    'ozon-waves-2026-11-06-families': 'ozon_wave_revision_20261106/children_families.csv',
    'ozon-waves-2026-11-06-family-sizes': 'ozon_wave_revision_20261106/children_family_sizes.csv',
    'ozon-waves-2026-11-06-ratings': 'ozon_wave_revision_20261106/children_rating_evidence.csv',
    'ozon-waves-2026-11-06-models': 'ozon_wave_revision_20261106/model_wave_context.csv',
    'ozon-waves-2026-11-06-data': 'ozon_wave_revision_20261106/wave_draft_manifest.json',
    'ozon-waves-2026-11-06-cost': 'ozon_wave_revision_20261106/cost_preview.json',
    **{f'ozon-waves-2026-11-06-wave{w}': f'ozon_wave_revision_20261106/wave_{w}_DRAFT.csv' for w in (1, 2, 3)},
    **{f'ozon-waves-2026-11-06-campaign-wave{w}_{segment}':
       f'ozon_wave_revision_20261106/campaign_wave{w}_{segment}.csv'
       for w, bundle in ((1, 'bundles_x2'), (2, 'bundles_x6'), (3, 'bundles_x10'))
       for segment in ('new_6months', bundle, 'other_outside_cpc', 'children')},
    'ozon-year-review-2026-11-05': 'ozon_year_review_20261105/index.html',
    'ozon-year-review-2026-11-05-skus': 'ozon_year_review_20261105/sku_transition_summary_in_stock.csv',
    'ozon-year-review-2026-11-05-episodes': 'ozon_year_review_20261105/transition_episodes_currently_in_stock.csv',
    'ozon-year-review-2026-11-05-rare': 'ozon_year_review_20261105/rare_both_modes_currently_in_stock.csv',
    'ozon-year-review-2026-11-05-no-cpc-sales': 'ozon_year_review_20261105/selling_without_observed_cpc_in_stock.csv',
    'ozon-year-review-2026-11-05-no-gain-review': 'ozon_year_review_20261105/selling_before_cpc_no_observed_gain_review.csv',
    'ozon-year-review-2026-11-05-profile': 'ozon_year_review_20261105/annual_sku_profile_currently_in_stock.csv',
    'ozon-year-review-2026-11-05-no-comparison': 'ozon_year_review_20261105/no_usable_off_on_comparison_in_stock.csv',
    'ozon-year-review-2026-11-05-demand': 'ozon_year_review_20261105/demand_before_cpc_in_stock.csv',
    'ozon-year-review-2026-11-05-data': 'ozon_year_review_20261105/analysis_summary.json',
    'ozon-year-review-2026-11-05-families': 'ozon_year_review_20261105/family_context_for_stock_episodes.csv',
    'ozon-year-review-2026-11-05-logged': 'ozon_year_review_20261105/historical_logged_restore_matches.csv',
    'ozon-inventory-2026-11-05': 'ozon_full_inventory_20261105/in_stock/index.html',
    'ozon-inventory-2026-11-05-x2': 'ozon_full_inventory_20261105/in_stock/bundles_x2.csv',
    'ozon-inventory-2026-11-05-x6': 'ozon_full_inventory_20261105/in_stock/bundles_x6.csv',
    'ozon-inventory-2026-11-05-x10': 'ozon_full_inventory_20261105/in_stock/bundles_x10.csv',
    'ozon-inventory-2026-11-05-new': 'ozon_full_inventory_20261105/in_stock/new_6months.csv',
    'ozon-inventory-2026-11-05-other': 'ozon_full_inventory_20261105/in_stock/other_outside_cpc.csv',
    'ozon-inventory-2026-11-05-children': 'ozon_full_inventory_20261105/in_stock/children.csv',
    'ozon-inventory-2026-11-05-all': 'ozon_full_inventory_20261105/in_stock/all_outside_cpc.csv',
    'ozon-inventory-2026-11-05-xlsx': 'ozon_full_inventory_20261105/in_stock/inventory.xlsx',
    'ozon-inventory-2026-11-05-data': 'ozon_full_inventory_20261105/in_stock/summary.json',
    'ozon-inventory-2026-11-05-old-bundle-review': 'ozon_full_inventory_20261105/in_stock/previous_bundle_label_without_marker.csv',
    'ozon-wave1-plan-2026-11-05': 'ozon_recovery_20261103/launch_preflight/review_final/PLAN_FOR_OWNER_20261105.html',
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
    'glue-status-2026-10-12': 'mkt_wb_glue_status_2026-10-12_acc1/index.html',
    'glue-laser-gaps-2026-10-12': 'mkt_wb_glue_status_2026-10-12_acc1/laser_gaps/index.html',
    'glue-colour-laser-2026-10-16': 'mkt_wb_colour_laser_complete_2026-10-16_acc1/index.html',
    'glue-colour-laser-sets-2026-10-16': 'mkt_wb_colour_laser_complete_2026-10-16_acc1/sets_to_create.csv',
    'glue-colour-laser-missing-2026-10-16': 'mkt_wb_colour_laser_complete_2026-10-16_acc1/missing.csv',
    'glue-colour-laser-absent-2026-10-16': 'mkt_wb_colour_laser_complete_2026-10-16_acc1/absent_models.csv',
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
    if path.suffix == '.csv':
        return FileResponse(path, media_type='text/csv; charset=utf-8', filename=path.name, headers={'Cache-Control': 'no-store'})
    if path.suffix == '.xlsx':
        return FileResponse(path, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                            filename=path.name, headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
    text = path.read_text(encoding='utf-8')
    headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
    if path.suffix == '.txt':
        return PlainTextResponse(text, headers=headers)
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
