# поток: fin
"""Update report freshness metadata after a verified finance publication."""
import calendar
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
from core import db
LIVE=Path('/opt/mp-analytics')


def update(publication):
    if publication.get('status')!='published' or not publication.get('closed_months_unchanged'):
        raise RuntimeError('No verified report publication')
    path=LIVE/'reports/data/bi_recovery.json'
    status=json.loads(path.read_text())if path.exists()else{}
    day=publication['as_of'];cost_gaps=publication['unknown_open_sku_costs']+publication['unknown_open_market_fifo']
    slices=db.query("""SELECT DISTINCT 'wb' platform,account,date_trunc('month',(payload->>'create_dt')::date)::date::text period_from FROM raw_wb_report WHERE payload->>'create_dt' IS NOT NULL
      UNION SELECT DISTINCT 'ozon',account,date_trunc('month',(payload->>'operation_date')::date)::date::text FROM raw_ozon_transaction
      UNION SELECT 'yandex',account,month::text FROM yandex_finance_monthly""")
    for row in slices:
        y,m=map(int,row['period_from'][:7].split('-'));row['period_to']=f'{y}-{m:02d}-{calendar.monthrange(y,m)[1]:02d}';row['source']='recovered_api_reports'
    tables={}
    for name in publication['tables']:
        tables[name]=db.query('SELECT count(*) n FROM '+name)[0]['n']
    status.update(phase='reports_refreshed_opex_pending',reports_ready=True,source_until=day,
                  published_at=publication['published_at'],slices=slices,tables=tables,
                  open_cost_gaps=cost_gaps,closed_months_unchanged=True,
                  verified_open_months=publication['open_months'],
                  publication_audit=str(Path(publication['backup']).parent/'publication.json'))
    status['known_gaps']=[s for s in status.get('known_gaps',[])if 'WB Diskver shipment costs' not in s]
    status['report_note']='Отчёты МП обновлены по '+dt.date.fromisoformat(day).strftime('%d.%m.%Y')+'. Незакрытые месяцы — предварительные. Себестоимость рассчитана по FIFO МойСклада; закрытые месяцы сохранены.'
    if cost_gaps:status['report_note']+=f' Для {cost_gaps} позиций себестоимость ещё не подтверждена.'
    status['report_note']+=' Операционные расходы и прибыль после них пока не восстановлены.'
    fd,temp=tempfile.mkstemp(dir=path.parent,prefix='.bi_recovery_',suffix='.tmp')
    try:
        with os.fdopen(fd,'w')as file:json.dump(status,file,ensure_ascii=False,indent=2)
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)
    return {'reports_ready':True,'source_until':day,'open_cost_gaps':cost_gaps}


if __name__=='__main__':
    p=Path(os.environ.get('MP_REPORTS_RUN_DIR','/opt/mp-analytics/.private/mp_reports_finish_20261106'))/'publication.json'
    print(json.dumps(update(json.loads(p.read_text())),ensure_ascii=False))
