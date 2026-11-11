# поток: fin
"""Incrementally publish reviewed MP finance; never replace closed monthly results."""
import argparse
import calendar
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import parse_dsn,make_dsn
BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
from core import db
ROOT=Path(os.getenv('MP_REPORTS_RUN_DIR','/opt/mp-analytics/.private/mp_reports_finish_20261106'))
LIVE=Path('/opt/mp-analytics')
KEYS={
 'raw_wb_report':['account','rrd_id'], 'raw_wb_finance_detail':['account','rrd_id'],
 'raw_ozon_accrual':['account','accrual_id'],'raw_ozon_transaction':['account','operation_id'],
 'raw_ozon_realization':['account','year','month'],'raw_yandex_stats_order':['account','order_id'],
 'raw_yandex_services':['account','row_hash'],'raw_yandex_closure':['account','category','transaction_id'],
 'ms_demand_cogs':['demand_id'],'ms_demand_pos':['demand_id','ms_id'],'ms_return_cogs':['return_id'],
 'ya_cogs_demand':['account','demand_name'],'sales':['article','platform','account','period_from','period_to','granularity'],
 'margin_by_sku':['article','platform','account','period_from','period_to'],'yandex_finance_monthly':['account','month']}
REPLACE={'raw_yandex_services','sales','margin_by_sku','yandex_finance_monthly'}


def upsert_sql(table, incoming, columns, keys, predicate='true'):
    cols=list(map(sql.Identifier,columns));changed=[c for c in columns if c not in keys]
    updates=[sql.SQL('{}=EXCLUDED.{}').format(sql.Identifier(c),sql.Identifier(c)) for c in changed]
    if table=='ya_cogs_demand':
        updates=[sql.SQL("{}=CASE WHEN t.method='manual' THEN t.{} ELSE EXCLUDED.{} END").format(sql.Identifier(c),sql.Identifier(c),sql.Identifier(c)) if c in ('cogs','method') else sql.SQL('{}=EXCLUDED.{}').format(sql.Identifier(c),sql.Identifier(c)) for c in changed]
    statement=sql.SQL('INSERT INTO {} AS t ({}) SELECT {} FROM {} WHERE {} ON CONFLICT ({}) DO UPDATE SET {} WHERE ({}) IS DISTINCT FROM ({})').format(
        sql.Identifier(table),sql.SQL(',').join(cols),sql.SQL(',').join(cols),sql.Identifier(incoming),sql.SQL(predicate),
        sql.SQL(',').join(map(sql.Identifier,keys)),sql.SQL(',').join(updates),
        sql.SQL(',').join(sql.SQL('t.{}').format(sql.Identifier(c))for c in changed),
        sql.SQL(',').join(sql.SQL('EXCLUDED.{}').format(sql.Identifier(c))for c in changed))
    return statement


def preserve_closed_hash(cursor,table,open_scope):
    cursor.execute(sql.SQL("SELECT md5(coalesce(string_agg(row_to_json(t)::text,'|' ORDER BY account,period_from,article),'')) FROM {} t WHERE NOT({})").format(sql.Identifier(table),sql.SQL(open_scope)))
    return cursor.fetchone()[0]


def run(apply=False):
    os.umask(0o077);ROOT.mkdir(parents=True,exist_ok=True)
    as_of=dt.date.fromisoformat(os.getenv('MP_REPORTS_AS_OF',dt.date.today().isoformat()))
    current=as_of.replace(day=1);start=(current-dt.timedelta(days=1)).replace(day=1).isoformat()
    hist={p:json.loads((LIVE/'reports/data'/f'mp_{p}_hist.json').read_text()) for p in ('wb','ozon','yandex')}
    snapshots={p:hashlib.sha256((LIVE/'reports/data'/f'mp_{p}_hist.json').read_bytes()).hexdigest()for p in hist}
    open_months={p:[d for d in (start,current.isoformat())if d[:7] not in set(h.get('period_keys',[]))]for p,h in hist.items()}
    def values(items):return ','.join("'"+v+"'"for v in items)or"'0001-01-01'"
    mart_scope=' OR '.join("(platform='"+p+"' AND period_from IN ("+values(open_months[p])+'))'for p in ('wb','ozon'))
    scopes={
      'raw_wb_report':f"period_from>='{start}'",'raw_wb_finance_detail':f"report_date>='{start}'",
      'raw_ozon_accrual':f"accrual_date>='{start}'",'raw_ozon_transaction':f"period_from>='{start}'",
      'raw_ozon_realization':f"make_date(year,month,1)>='{start}'",
      'raw_yandex_stats_order':f"payload->>'creationDate'>='{start}'",
      'raw_yandex_services':f"ym>='{start[:7]}'",'raw_yandex_closure':f"ym>='{start[:7]}'",
      'ms_demand_cogs':'true','ms_demand_pos':'true','ms_return_cogs':'true',
      'ya_cogs_demand':'ym IN ('+values(open_months['yandex'])+')',
      'sales':mart_scope,'margin_by_sku':mart_scope,'yandex_finance_monthly':'month IN ('+values(open_months['yandex'])+')'}
    params=parse_dsn(db.DATABASE_URL);source_dsn=make_dsn(**{**params,'dbname':'bi_history_20261104'})
    result={'source_database':'bi_history_20261104','as_of':as_of.isoformat(),'open_months':open_months,'closed_snapshot_hashes':snapshots,'tables':{}}
    with psycopg2.connect(source_dsn)as source,psycopg2.connect(db.DATABASE_URL)as target:
      source.set_session(isolation_level='REPEATABLE READ',readonly=True)
      with source.cursor()as sq,target.cursor()as tq:
        sq.execute('SELECT current_database()');assert sq.fetchone()[0]=='bi_history_20261104'
        tq.execute('SELECT current_database()');assert tq.fetchone()[0]=='mp_analytics'
        sq.execute("SELECT count(*) FROM margin_by_sku WHERE ("+mart_scope+") AND cogs IS NULL")
        result['unknown_open_sku_costs']=sq.fetchone()[0]
        sq.execute("SELECT count(*) FROM ya_cogs_demand WHERE ym IN ("+values(open_months['yandex'])+") AND (cogs<=0 OR method NOT IN ('ms_fifo','manual'))")
        result['unknown_open_market_fifo']=sq.fetchone()[0]
        assert result['unknown_open_sku_costs']==0,'open_cost_gaps_require_review'
        assert result['unknown_open_market_fifo']==0,'Market_cost_gaps_require_review'
        exports={};cols_by_table={}
        for table,predicate in scopes.items():
          sq.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",(table,))
          columns=[r[0]for r in sq.fetchall()if r[0]!='id'];cols_by_table[table]=columns
          sq.execute(sql.SQL('SELECT count(*) FROM {} WHERE {}').format(sql.Identifier(table),sql.SQL(predicate)))
          count=sq.fetchone()[0];result['tables'][table]={'source_rows':count}
          tq.execute(sql.SQL('SELECT count(*) FROM {} WHERE {}').format(sql.Identifier(table),sql.SQL(predicate)))
          result['tables'][table]['target_rows']=tq.fetchone()[0]
          if apply:
            f=tempfile.TemporaryFile(dir=ROOT)
            statement=sql.SQL('COPY (SELECT {} FROM {} WHERE {}) TO STDOUT WITH BINARY').format(sql.SQL(',').join(map(sql.Identifier,columns)),sql.Identifier(table),sql.SQL(predicate))
            sq.copy_expert(statement.as_string(source),f);f.seek(0);exports[table]=f
        (ROOT/'publish_preview.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        if not apply:print(json.dumps(result,ensure_ascii=False));return result
        checks=json.loads((ROOT/'build_open.json').read_text())
        assert checks.get('phase')=='open_wb_ozon_rebuilt' and all(v['status']=='done'for v in checks['tasks'].values())
        env=os.environ.copy()
        for name,key in [('dbname','PGDATABASE'),('host','PGHOST'),('port','PGPORT'),('user','PGUSER'),('password','PGPASSWORD')]:
          if name in params:env[key]=str(params[name])
        backup=ROOT/'before_publish.dump'
        subprocess.run(['pg_dump','-Fc','--no-owner','--no-acl','-f',str(backup)],env=env,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        subprocess.run(['pg_restore','-l',str(backup)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        target.rollback()
        tq.execute("SET LOCAL lock_timeout='10s'")
        tq.execute("SELECT pg_advisory_xact_lock(hashtext('mp_reports_financial_publish'))")
        tq.execute(sql.SQL('LOCK TABLE {} IN SHARE ROW EXCLUSIVE MODE').format(sql.SQL(',').join(map(sql.Identifier,scopes))))
        closed_before={t:preserve_closed_hash(tq,t,mart_scope)for t in ('sales','margin_by_sku')}
        for table,f in exports.items():
          columns=cols_by_table[table];incoming='_mp_incoming_'+table
          tq.execute(sql.SQL('CREATE TEMP TABLE {} (LIKE {} INCLUDING DEFAULTS) ON COMMIT DROP').format(sql.Identifier(incoming),sql.Identifier(table)))
          statement=sql.SQL('COPY {} ({}) FROM STDIN WITH BINARY').format(sql.Identifier(incoming),sql.SQL(',').join(map(sql.Identifier,columns)))
          tq.copy_expert(statement.as_string(target),f)
          tq.execute("SELECT pg_get_serial_sequence(%s,'id')",(table,)) if table in ('raw_wb_report','raw_ozon_accrual','raw_ozon_realization','raw_ozon_transaction','sales') else None
          if table in ('raw_wb_report','raw_ozon_accrual','raw_ozon_realization','raw_ozon_transaction','sales'):
            sequence=tq.fetchone()[0]
            if sequence:
              tq.execute(sql.SQL('SELECT setval(%s,greatest(coalesce(max(id),0),1),true) FROM {}').format(sql.Identifier(table)),(sequence,))
          if table in REPLACE:
            tq.execute(sql.SQL('DELETE FROM {} WHERE {}').format(sql.Identifier(table),sql.SQL(scopes[table])))
          tq.execute(upsert_sql(table,incoming,columns,KEYS[table]))
          result['tables'][table]['written_rows']=tq.rowcount;f.close()
        # SKU Ozon profitability remains withheld; aggregate FIFO COGS is validated independently.
        tq.execute("UPDATE margin_by_sku SET net_profit=NULL,margin_pct=NULL WHERE platform='ozon' AND ("+mart_scope+")")
        assert closed_before=={t:preserve_closed_hash(tq,t,mart_scope)for t in closed_before},'closed_months_changed'
        tq.execute("SELECT net_profit::float FROM margin_by_sku WHERE platform='wb' AND account='wb_acc1' AND article='216421567' AND period_from='2026-05-01'")
        control=tq.fetchone()[0];assert control is not None and abs(control-3741.77)<.01,'control_failed'
        assert all(hashlib.sha256((LIVE/'reports/data'/f'mp_{p}_hist.json').read_bytes()).hexdigest()==value for p,value in snapshots.items()),'closed_snapshot_changed_during_publish'
        target.commit()
    result.update(status='published',published_at=dt.datetime.now(dt.timezone.utc).isoformat(),backup=str(backup),closed_months_unchanged=True)
    (ROOT/'publication.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps({'status':result['status'],'tables':len(result['tables']),'closed_months_unchanged':True,'backup':str(backup)},ensure_ascii=False))
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true');run(p.parse_args().apply)
