# поток: fin
"""October-only Ozon recovery using the deployed collectors and existing COGS rules.

No run_daily, freezing, notifications, prices or marketplace writes. Source failures
stop the affected account before the margin build. Posting transport uses the
deployed per-account advisory lock; raw imports retain their natural-key upserts.
"""
import contextlib
import datetime as dt
import io
import json
from pathlib import Path

from collectors import ozon_products, ozon_postings, ozon_accrual, ozon_accrual_adapter
from collectors import ms_demand_cogs
from reports import margin_ozon_sku

OUT = Path(__file__).resolve().parent


def main():
    start = dt.datetime.now(dt.timezone.utc)
    begin, end = '2026-10-01', '2026-10-20'
    since = (start.date()-dt.timedelta(days=80)).isoformat()
    results=[]
    for account in ('oz_acc1','oz_acc2'):
        print(f'[scoped] {account} start {dt.datetime.now(dt.timezone.utc).isoformat()}',flush=True)
        steps=[]
        try:
            ozon_products.main(account)
            steps.append('catalog')
            ozon_postings.main(begin,end,account)
            steps.append('postings_both_schemes')
            ozon_accrual.main(begin,end,account)
            ozon_accrual_adapter.main(begin,end,account)
            steps.append('accruals_and_adapter')
            output=io.StringIO()
            with contextlib.redirect_stdout(output):
                collected=ms_demand_cogs.collect(account,platform='ozon',moment_from=since)
            print(output.getvalue(),flush=True)
            if 'ОШИБКА byoperation' in output.getvalue():
                raise RuntimeError('Shipment cost reads failed; margin build skipped')
            steps.append('shipment_costs')
            margin_ozon_sku.build('2026-10-01','2026-10-31',account)
            steps.append('october_margin')
            results.append(dict(account=account,status='ok',steps=steps,
                                shipment_cost_collection=collected))
            print(f'[scoped] {account} October margin OK',flush=True)
        except Exception as error:
            results.append(dict(account=account,status='failed',steps=steps,
                                error_type=type(error).__name__,error=str(error)[:300]))
            print(f'[scoped] {account} failed {type(error).__name__}',flush=True)
        (OUT/'scoped_result.json').write_text(json.dumps(dict(
            started_utc=start.isoformat(),results=results),ensure_ascii=False,indent=2))
    status='both_october_calculations_succeeded' if all(x['status']=='ok' for x in results) else 'scoped_calculation_failed'
    (OUT/'monitor_result.json').write_text(json.dumps(dict(status=status,
        completed=[x['account'] for x in results if x['status']=='ok'],
        source='october_only_recovery_after_wb4c_deployment'),indent=2))
    assert status=='both_october_calculations_succeeded',results


if __name__=='__main__':
    main()
