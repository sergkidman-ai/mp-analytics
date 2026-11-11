# поток: fin
"""Rebuild only open WB/Ozon months; preserve all closed report snapshots."""
import calendar
import datetime as dt
import json
import os
from pathlib import Path
import sys
from psycopg2.extensions import parse_dsn,make_dsn
BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
from core import db
ROOT=Path(os.getenv('MP_REPORTS_RUN_DIR','/opt/mp-analytics/.private/mp_reports_finish_20261106'))


def main():
    os.umask(0o077)
    params=parse_dsn(db.DATABASE_URL);params['dbname']='bi_history_20261104'
    db.DATABASE_URL=make_dsn(**params);os.environ['DATABASE_URL']=db.DATABASE_URL
    assert db.query('SELECT current_database() name')[0]['name']==params['dbname']
    from reports import margin_by_sku as wb,margin_ozon_sku as oz,wb_sales_formation as sales
    cache={};fetch=oz.fetch_product_offer_map
    def product_map(account):
        if account not in cache:cache[account]=fetch(account)
        return cache[account]
    oz.fetch_product_offer_map=product_map
    state={'tasks':{},'closed_snapshots_preserved':True}
    def save():
        state['updated_at']=dt.datetime.now(dt.timezone.utc).isoformat()
        p=ROOT/'build_open.json';tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(state,ensure_ascii=False,indent=2));tmp.replace(p)
    as_of=dt.date.fromisoformat(os.getenv('MP_REPORTS_AS_OF',dt.date.today().isoformat()))
    current=as_of.replace(day=1);previous=(current-dt.timedelta(days=1)).replace(day=1)
    months=[previous.strftime('%Y-%m'),current.strftime('%Y-%m')]
    for platform in ('wb','ozon'):
        hist=json.loads((Path('/opt/mp-analytics/reports/data')/f'mp_{platform}_hist.json').read_text())
        closed=set(hist.get('period_keys',[]))
        for month in months:
            if month in closed:continue
            y,m=map(int,month.split('-'));start=month+'-01';end=f'{month}-{calendar.monthrange(y,m)[1]:02d}'
            for account in (('wb_acc1','wb_acc2') if platform=='wb' else ('oz_acc1','oz_acc2')):
                key=account+'/'+month
                try:
                    if platform=='wb':sales.build(account,month)
                    result=wb.build(account,start,end,strict_costs=True) if platform=='wb' else oz.build(start,end,account,strict_costs=True)
                    state['tasks'][key]={'status':'done',**(result or{})}
                except Exception as error:
                    state['tasks'][key]={'status':'failed','error':type(error).__name__}
                save();print(json.dumps({'task':key,**state['tasks'][key]},ensure_ascii=False),flush=True)
    state['phase']='open_wb_ozon_rebuilt';save()


if __name__=='__main__':main()
