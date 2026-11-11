# поток: fin
"""Resume regular MP report refresh, preserving closed months and unrelated jobs."""
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo
import tempfile
import psycopg2
from psycopg2 import sql
from psycopg2.extensions import parse_dsn,make_dsn

BASE=Path(__file__).resolve().parents[1]
RUNTIME=Path('/opt/mp-analytics/.private/mp_reports_runtime')


def sync_inputs():
    sys.path.insert(0,str(BASE))
    from core import db
    from tools.mp_reports_publish import upsert_sql
    params=parse_dsn(db.DATABASE_URL)
    tables={'ms_product':['ms_id'],'products':['ms_id'],'set_cost':['external_code'],
            'prc_tc_link':['external_code','ref_code']}
    manual=('cogs_manual','ya_cogs_manual','ya_cogs_frozen','yandex_ad_contract')
    with psycopg2.connect(db.DATABASE_URL)as source,psycopg2.connect(make_dsn(**{**params,'dbname':'bi_history_20261104'}))as stage:
        source.set_session(readonly=True)
        with source.cursor()as q,stage.cursor()as w:
            w.execute('SELECT current_database()');assert w.fetchone()[0]=='bi_history_20261104'
            for table in list(tables)+list(manual):
                q.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",(table,))
                columns=[r[0]for r in q.fetchall()]
                incoming='_input_'+table
                w.execute(sql.SQL('CREATE TEMP TABLE {}(LIKE {} INCLUDING DEFAULTS)ON COMMIT DROP').format(sql.Identifier(incoming),sql.Identifier(table)))
                with tempfile.TemporaryFile()as f:
                    q.copy_expert(sql.SQL('COPY {} TO STDOUT WITH BINARY').format(sql.Identifier(table)).as_string(source),f);f.seek(0)
                    w.copy_expert(sql.SQL('COPY {} FROM STDIN WITH BINARY').format(sql.Identifier(incoming)).as_string(stage),f)
                if table in manual:
                    w.execute(sql.SQL('DELETE FROM {}').format(sql.Identifier(table)))
                    w.execute(sql.SQL('INSERT INTO {} SELECT * FROM {}').format(sql.Identifier(table),sql.Identifier(incoming)))
                else:
                    w.execute(upsert_sql(table,incoming,columns,tables[table]))
        stage.commit()


def main():
    os.umask(0o077);RUNTIME.mkdir(parents=True,exist_ok=True)
    with (RUNTIME/'lock').open('a')as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            print('MP financial refresh already running');return
        now=dt.datetime.now(ZoneInfo('Europe/Moscow'))
        folder=RUNTIME/now.strftime('%Y%m%d_%H%M%S');folder.mkdir()
        env=os.environ.copy();env.update(MP_REPORTS_RUN_DIR=str(folder),MP_REPORTS_AS_OF=now.date().isoformat())
        state={'status':'running','started_at':now.isoformat(),'folder':str(folder),'steps':{}}
        def save():
            state['updated_at']=dt.datetime.now(dt.timezone.utc).isoformat()
            for p in (folder/'pipeline.json',RUNTIME/'latest.json'):
                temp=p.with_suffix('.tmp');temp.write_text(json.dumps(state,ensure_ascii=False,indent=2));temp.replace(p)
        save()
        sync_inputs()
        state['steps']['manual_inputs']='done';save()
        commands=[('sources','mp_reports_sources_refresh.py',[]),('recent_costs','mp_reports_recent_costs.py',[]),
                  ('market_costs','mp_reports_market_costs.py',[]),('build_open','mp_reports_build_open.py',[]),
                  ('build_market','mp_reports_build_market.py',[]),('preview','mp_reports_publish.py',[]),
                  ('publish','mp_reports_publish.py',['--apply']),
                  ('turnover_brands','turnover_brands_refresh.py',[]),('status','mp_reports_status.py',[])]
        try:
            for key,script,args in commands:
                state['current_step']=key;save()
                with (folder/(key+'.log')).open('w')as output:
                    proc=subprocess.run([sys.executable,str(BASE/'tools'/script),*args],cwd=BASE,env=env,stdout=output,stderr=subprocess.STDOUT,timeout=5400)
                if proc.returncode:raise RuntimeError(key+'_failed')
                if key=='sources':
                    source=json.loads((folder/'sources/checkpoint.json').read_text())
                    failed=[k for k,v in source['tasks'].items()if v.get('status')=='failed']
                    if failed:raise RuntimeError('source_tasks_failed:'+','.join(failed[:4]))
                    for month in {k.rsplit('/',1)[-1] for k in source['tasks'] if k.startswith('wb/')}:
                        if not any(k.startswith('yandex/') and k.endswith('/'+month) and v.get('status')=='done' for k,v in source['tasks'].items()):
                            raise RuntimeError('Market_order_source_unavailable:'+month)
                if key=='recent_costs':
                    source=json.loads((folder/'recent_costs/checkpoint.json').read_text())
                    if any(v.get('status')=='failed'for v in source['tasks'].values()):raise RuntimeError('recent_fifo_tasks_failed')
                state['steps'][key]='done';save()
                print(key+': done',flush=True)
            # Clear the web worker's cached monthly figures after the atomic publication.
            subprocess.run(['systemctl','restart','mp-dashboard.service'],check=True,timeout=60)
            state['status']='complete';state['finished_at']=dt.datetime.now(dt.timezone.utc).isoformat();save()
        except Exception as error:
            state.update(status='failed',error=type(error).__name__,reason=str(error)[:220]);save()
            print('MP refresh failed; see '+str(folder),flush=True)
            raise SystemExit(1)from None


if __name__=='__main__':main()
