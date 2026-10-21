# поток: fin
"""One versioned Ozon account expense basis, built only from existing finance raw.

Composition matches EXP_SVC: signed net service/commission costs, not sum(abs).
Returns, compensation and other income remain separate. No posting-price revenue
split, SKU/scheme allocation, marketplace writes, MS reads or scheduled messages.
"""
import calendar
import hashlib
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext, ROUND_HALF_EVEN
from pathlib import Path

from ops.ozon_lines_registry import SERVICES, RESID_OPS

D=Decimal
POLICY='ozon-account-exp-svc-signed-v1'
EXPENSE_LINES=('commission','delivery','partners','fbo','promo','penalty','unclassified')
PRECISION=38


class BasisUnavailable(RuntimeError):
    pass


def decimal(value):
    if value is None or isinstance(value,bool):raise ValueError('Missing basis number')
    n=D(str(value))
    if not n.is_finite():raise ValueError('Invalid basis number')
    return n


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()


def month_bounds(month):
    first=date.fromisoformat(month[:7]+'-01')
    return first,first.replace(day=calendar.monthrange(first.year,first.month)[1])


def latest_closed(as_of=None):
    today=as_of or date.today()
    return (today.replace(day=1)-timedelta(days=1)).replace(day=1)


def calculate(account, month, transactions, completeness, source_digest):
    """Pure, exact arithmetic; no changes to financial reports or target profit."""
    if account not in ('oz_acc1','oz_acc2'):raise ValueError('Unknown Ozon basis account')
    first,last=month_bounds(month);lines=defaultdict(lambda:D(0));unallocated=D(0)
    unknown=set();net=D(0);debits=defaultdict(lambda:D(0));credits=defaultdict(lambda:D(0));fixed=D(0)
    def expense_flow(line,value):
        lines[line]+=value
        if line in EXPENSE_LINES:
            if value>=0:debits[line]+=value
            else:credits[line]-=value
    for p in transactions:
        acc=decimal(p.get('accruals_for_sale') or 0);comm=decimal(p.get('sale_commission') or 0)
        amount=decimal(p.get('amount') or 0);net+=amount
        lines['sales' if acc>0 else 'returns']+=acc if acc>0 else -acc
        expense_flow('commission',-comm);services=D(0)
        for svc in p.get('services') or []:
            value=decimal(svc.get('price') or 0);services+=value;code=svc.get('name','')
            hit=SERVICES.get(code);line=hit[0] if hit else 'unclassified'
            if hit is None and value:unknown.add('service:'+code)
            expense_flow(line,-value)
        residual=amount-acc-comm-services
        if abs(residual)>D('.005'):
            code=p.get('operation_type','');hit=RESID_OPS.get(code);line=hit[0] if hit else 'unclassified'
            if hit is None:unknown.add('operation:'+code)
            # Income lines retain incoming positive sign; expenses retain cost sign.
            if line in ('compensation','other'):lines[line]+=residual
            else:expense_flow(line,-residual)
            if code.startswith('OperationSubscription'):fixed-=residual
        else:unallocated+=residual
    sales=lines['sales'];expenses=sum((lines[k] for k in EXPENSE_LINES),D(0))
    represented=sales-lines['returns']-expenses+lines['compensation']+lines['other']
    reasons=list(completeness.get('reasons',[]))
    if not completeness.get('complete'):reasons.append('sources_not_confirmed_complete')
    if sales<=0:reasons.append('empty_or_nonpositive_sales')
    if expenses<0 or expenses>=sales:reasons.append('invalid_expense_ratio')
    if abs(net-represented)>D('.01'):reasons.append('net_reconciliation_failed')
    with localcontext() as ctx:
        ctx.prec=PRECISION
        retention=expenses/sales if sales>0 else None
        keep=1-retention if retention is not None else None
    body=dict(account=account,period_from=str(first),period_to=str(last),policy=POLICY,
       expense_lines=list(EXPENSE_LINES),precision=PRECISION,rounding='ROUND_HALF_EVEN at final kopeck only',
       sales=str(sales),expenses=str(expenses),retention=str(retention) if retention is not None else None,
       keep=str(keep) if keep is not None else None,lines={k:str(v) for k,v in sorted(lines.items())},
       source_sha256=source_digest,completeness=completeness,status='complete' if not reasons else 'incomplete',
       reasons=sorted(set(reasons)),unknown_codes=sorted(unknown),net=str(net),net_reconciliation_delta=str(net-represented),
       component_rounding_residual=str(unallocated),gross_debits={k:str(v) for k,v in debits.items()},
       gross_credits={k:str(v) for k,v in credits.items()},fixed_subscription_expense=str(fixed),
       scope='account-wide; includes FBO, advertising, fixed subscriptions; not actual order/FBS-only fees')
    body['version']='ozexp-v1:'+digest(body)
    return body


def floor_from_keep(cost, units, keep):
    from core.profit_floor import minimum_profit
    cost,units,keep=map(decimal,(cost,units,keep))
    if cost<=0 or units<=0 or not 0<keep<=1:raise ValueError('Invalid floor input')
    with localcontext() as ctx:
        ctx.prec=PRECISION
        return ((cost+minimum_profit(cost,units))/keep).quantize(D('.01'),rounding=ROUND_HALF_EVEN)


def floor_price(cost, units, basis):
    if basis.get('status')!='complete':raise BasisUnavailable('Expense basis is not complete')
    return floor_from_keep(cost,units,basis['keep'])


def floor_from_retention(cost, units, retention):
    with localcontext() as ctx:
        ctx.prec=PRECISION
        return floor_from_keep(cost,units,1-decimal(retention))


def successful_log_runs(log_path):
    """Evidence of completed full-calendar by-day collectors, including zero days.

    Presence of raw rows alone is NOT completeness. The logged successful final
    total must match all daily records and the current raw/adapter snapshot.
    """
    runs={};current=None;chunk=[]
    for line in Path(log_path).read_text(errors='replace').splitlines():
        m=re.search(r'Ozon начисления (oz_acc[12]) (\d{4}-\d\d-01)\.\.(\d{4}-\d\d-\d\d)',line)
        if m:
            account,first,last=m.groups();a,b=month_bounds(first)
            current={'account':account,'first':first,'last':last,'days':{}} if str(b)==last else None
            chunk=[]
        if current is None:continue
        chunk.append(line)
        m=re.search(r'\[oz accrual\] (\d{4}-\d\d-\d\d) записей (\d+).*итог ([+-]?[0-9,]+\.\d+)',line)
        if m:current['days'][m[1]]={'count':int(m[2]),'amount':D(m[3].replace(',',''))}
        m=re.search(r'^Итого: записей (\d+), сумма ([+-]?[0-9,]+\.\d+)',line)
        if m:
            n=int(m[1]);amount=D(m[2].replace(',',''))
            if sum(v['count'] for v in current['days'].values())==n and abs(sum((v['amount'] for v in current['days'].values()),D(0))-amount)<=D('.01'):
                current['log_sha256']=hashlib.sha256('\n'.join(chunk).encode()).hexdigest()
                runs[(current['account'],current['first'])]=current
            current=None
    return runs


def build(account, month, *, log_path='/opt/mp-analytics/run_daily.log', as_of=None):
    from core import db
    first,last=month_bounds(month);today=as_of or date.today();reasons=[]
    rows=db.query("SELECT operation_id,payload FROM raw_ozon_transaction WHERE account=%s AND (payload->>'operation_date')::date BETWEEN %s AND %s ORDER BY operation_id",(account,first,last))
    accruals=db.query('SELECT accrual_id,accrual_date,total_amount,payload FROM raw_ozon_accrual WHERE account=%s AND accrual_date BETWEEN %s AND %s ORDER BY accrual_id',(account,first,last))
    realization=db.query('SELECT payload FROM raw_ozon_realization WHERE account=%s AND year=%s AND month=%s',(account,first.year,first.month))
    source_digest=digest({'transactions':rows,'accruals':accruals,'realization':realization,
                          'registry':{'services':SERVICES,'residuals':RESID_OPS}})
    if last>=today:reasons.append('month_not_closed')
    runs=successful_log_runs(log_path) if Path(log_path).exists() else {}
    run=runs.get((account,str(first)))
    if not run:reasons.append('no_successful_full_period_collection_evidence')
    a_days=defaultdict(lambda:[0,D(0)]);t_days=defaultdict(lambda:[0,D(0)])
    for a in accruals:a_days[str(a['accrual_date'])][0]+=1;a_days[str(a['accrual_date'])][1]+=decimal(a['total_amount'])
    for t in rows:
        stamp=str(t['payload'].get('operation_date',''))[:10]
        t_days[stamp][0]+=1;t_days[stamp][1]+=decimal(t['payload'].get('amount') or 0)
    if run:
        expected={str(first+timedelta(days=i)) for i in range((last-first).days+1)}
        if set(run['days'])!=expected:reasons.append('incomplete_collector_days')
        for day in expected:
            proof=run['days'].get(day)
            if proof is None or a_days[day][0]!=proof['count'] or abs(a_days[day][1]-proof['amount'])>D('.01'):
                reasons.append('raw_does_not_match_successful_collection')
            if t_days[day][0]!=a_days[day][0] or abs(t_days[day][1]-a_days[day][1])>D('.01'):
                reasons.append('accrual_transaction_daily_mismatch')
    ids={r['operation_id']:decimal(r['payload'].get('amount') or 0) for r in rows}
    if len(rows)!=len(accruals) or any(a['accrual_id'] not in ids or abs(ids[a['accrual_id']]-decimal(a['total_amount']))>D('.01') for a in accruals):
        reasons.append('accrual_transaction_identity_mismatch')
    report=realization[0]['payload'] if len(realization)==1 else {};header=report.get('header',{})
    if not (isinstance(report.get('rows'),list) and report['rows'] and header.get('start_date')==str(first) and header.get('stop_date')==str(last) and header.get('currency_sys_name')=='RUB'):
        reasons.append('closed_realization_report_missing_or_invalid')
    proof=dict(complete=not reasons,reasons=sorted(set(reasons)),accrual_rows=len(accruals),transaction_rows=len(rows),
       collector_days=len(run['days']) if run else 0,collector_log_sha256=run.get('log_sha256') if run else None,
       collector_log_path=str(log_path),realization_rows=len(report.get('rows') or []),
       note='Successful full by-day collection log + all daily counts/sums and identities in existing raw + closed RUB realization; posting failures do not imply expense-source failure')
    return calculate(account,str(first),[r['payload'] for r in rows],proof,source_digest)


def publish(basis):
    """One transaction: append immutable version, preserve old rule, publish pointer."""
    from core import db
    from psycopg2.extras import Json
    if basis['account'] not in ('oz_acc1','oz_acc2') or basis['policy']!=POLICY:
        raise ValueError('Unknown Ozon basis account/policy')
    if basis['version']!='ozexp-v1:'+digest({k:v for k,v in basis.items() if k!='version'}):
        raise ValueError('Expense basis version/content mismatch')
    with db.get_conn() as conn,conn.cursor() as cur:
        cur.execute('SELECT pg_advisory_xact_lock(%s,%s)',(0x4F5A4558,1 if basis['account']=='oz_acc1' else 2))
        cur.execute('INSERT INTO fin_ozon_expense_basis(version,account,period_from,period_to,status,payload) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
          (basis['version'],basis['account'],basis['period_from'],basis['period_to'],basis['status'],Json(basis)))
        cur.execute('INSERT INTO fin_ozon_expense_current(account,version) VALUES(%s,%s) ON CONFLICT(account) DO UPDATE SET version=excluded.version,checked_at=now()',
          (basis['account'],basis['version']))
        if basis['status']=='complete':
            # Expire at the end of the next calendar month, not a static +60 days.
            next_first=date.fromisoformat(basis['period_to'])+timedelta(days=1)
            valid_until=next_first.replace(day=calendar.monthrange(next_first.year,next_first.month)[1])
            cur.execute('''UPDATE ms_shipment_rules SET retention=%s,base_from=%s,base_to=%s,
              valid_until=%s,source=%s,source_sha256=%s,version=%s,loaded_at=now()
              WHERE account=%s AND platform='ozon' AND
              (version IS DISTINCT FROM %s OR valid_until IS DISTINCT FROM %s)''',
              (basis['retention'],basis['period_from'],basis['period_to'],valid_until,POLICY,basis['source_sha256'],basis['version'],basis['account'],basis['version'],valid_until))
        else:
            cur.execute("UPDATE ms_shipment_rules SET valid_until=least(valid_until,current_date-1) WHERE account=%s AND platform='ozon' AND valid_until>=current_date",(basis['account'],))
    return basis


def refresh(account, *, as_of=None, log_path='/opt/mp-analytics/run_daily.log', apply=False):
    basis=build(account,str(latest_closed(as_of)),as_of=as_of,log_path=log_path)
    return publish(basis) if apply else basis


def get_basis(account, *, as_of=None, require_current=True):
    from core import db
    rows=db.query('SELECT b.payload FROM fin_ozon_expense_current c JOIN fin_ozon_expense_basis b ON b.version=c.version WHERE c.account=%s',(account,))
    if not rows:raise BasisUnavailable(f'{account}: no confirmed expense basis; run explicit refresh')
    basis=rows[0]['payload']
    if basis['status']!='complete':raise BasisUnavailable(f"{account}: expense sources incomplete: {','.join(basis['reasons'])}; version={basis['version']}")
    if require_current and basis['period_from']!=str(latest_closed(as_of)):
        raise BasisUnavailable(f"{account}: last confirmed period {basis['period_from']} is stale; latest closed month not confirmed")
    return basis


def basis_status(account, *, as_of=None):
    """Explicit current state plus dated last usable version; never a silent fallback."""
    from core import db
    current=db.query('SELECT b.payload FROM fin_ozon_expense_current c JOIN fin_ozon_expense_basis b ON b.version=c.version WHERE c.account=%s',(account,))
    usable=db.query("SELECT payload FROM fin_ozon_expense_basis WHERE account=%s AND status='complete' AND period_to<%s ORDER BY period_from DESC,created_at DESC LIMIT 1",(account,as_of or date.today()))
    value=current[0]['payload'] if current else None
    state='ready' if value and value['status']=='complete' and value['period_from']==str(latest_closed(as_of)) else 'unavailable_or_stale'
    return dict(status=state,current=value,last_usable=usable[0]['payload'] if usable else None)


def shipment_rule(account, *, as_of=None):
    """Compatibility interface for pilot/shipment consumers, with the SAME version."""
    from core import db
    basis=get_basis(account,as_of=as_of)
    rows=db.query("SELECT * FROM ms_shipment_rules WHERE account=%s AND platform='ozon'",(account,))
    if len(rows)!=1 or rows[0]['version']!=basis['version']:raise BasisUnavailable('Published shipment rule version mismatch')
    return rows[0]
