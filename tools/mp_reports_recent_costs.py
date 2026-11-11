# поток: fin
"""Collect recent WB/Ozon FIFO and return gates into recovery staging."""
import datetime as dt
import json
import os
from pathlib import Path
import sys
import time
from psycopg2.extensions import parse_dsn,make_dsn
BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
from core import db,ms_api
from tools import bi_history_restore as recovery


def main():
    os.umask(0o077)
    root=Path(os.getenv('MP_REPORTS_RUN_DIR','/opt/mp-analytics/.private/mp_reports_finish_20261106'))/'recent_costs'
    root.mkdir(parents=True,exist_ok=True)
    p=parse_dsn(db.DATABASE_URL);p['dbname']='bi_history_20261104'
    db.DATABASE_URL=make_dsn(**p);os.environ['DATABASE_URL']=db.DATABASE_URL
    assert db.query('SELECT current_database() name')[0]['name']==p['dbname']
    recovery.ROOT=root;recovery.STATE={'tasks':{}}
    recovery.END=dt.date.fromisoformat(os.getenv('MP_REPORTS_AS_OF',dt.date.today().isoformat()));recovery.AS_OF=recovery.END+dt.timedelta(days=1)
    since=(recovery.END.replace(day=1)-dt.timedelta(days=1)).replace(day=1).isoformat()
    ms_api.TIMEOUT=30;ms_api.RETRIES=3
    from collectors import ms_demand_cogs as demand,ms_return_cogs as returns
    last=[0.0]
    def get(path,_tries=0):
        wait=.3-(time.monotonic()-last[0])
        if wait>0:time.sleep(wait)
        last[0]=time.monotonic()
        return recovery.narrow_ms_read(lambda p,t:ms_api.get(p),path,recovery.AS_OF.isoformat())
    demand.get=get;returns.get=get
    for platform,accounts in [('wb',['wb_acc1','wb_acc2']),('ozon',['oz_acc1','oz_acc2'])]:
        for account in accounts:
            try:
                count,missing=demand.collect(account,platform,moment_from=since,batch=20,progress_every=25)
                recovery.mark('demand/'+account,status='done',collected=count,outside_current_window=missing)
                count,total=returns.collect(account,platform,moment_from=since)
                recovery.mark('return/'+account,status='done',rows=count)
            except Exception as e:
                recovery.mark('account/'+account,status='failed',error=type(e).__name__)
    recovery.STATE['phase']='recent_costs_refreshed';recovery.save()


if __name__=='__main__':main()
