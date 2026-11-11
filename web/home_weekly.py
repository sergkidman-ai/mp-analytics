# поток: fin
"""Weekly WB/Ozon ledger from persisted operations and dated FIFO facts. No writes/API calls."""
from collections import Counter, defaultdict, deque
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
from threading import Lock
import time

from core import db
from reports import fifo_fallback
from ops.ozon_lines_registry import RESID_OPS, SERVICES

ACCOUNTS = {'7807355364': ('wb_acc1','oz_acc1'), '7811803918': ('wb_acc2','oz_acc2')}
ORG_IDS = {'1':'a5e7ee84-8d63-41f2-9fae-0be2ef90462b', '2':'f5bea6e0-c2ac-11ef-0a80-0221000adcc4'}
EXACT_METHODS = {'shipment_fifo','fbo_unit_fifo','manual'}
_CACHE = {}
_LOCK = Lock()


def dec(value):
    return Decimal(str(value or 0))


def money(value):
    return float(dec(value).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))


def day(value):
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def monday(value):
    return value - dt.timedelta(days=value.weekday())


def bounds(period):
    first = dt.date.fromisoformat(period).replace(day=1)
    last = (first.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
    return monday(first), monday(last) + dt.timedelta(days=6)


def normalize_posting(value):
    return '-'.join(str(value or '').split('-')[:2])


def aggregate(events, start, end, expected):
    """Return COGS consumes original sale lots; it never uses a current average."""
    weeks, lots, known = {}, defaultdict(deque), set()
    for e in sorted(events, key=lambda e: (e['date'], e.get('sort', ''), e['account'])):
        selected = start <= e['date'] <= end
        w = monday(e['date']).isoformat()
        if selected:
            row = weeks.setdefault(w, {'wk':w, 'rev':Decimal(0), 'payout':Decimal(0),
                'ad':Decimal(0), 'cogs':Decimal(0), 'missing_qty':Decimal(0),
                'cost_units':Decimal(0), 'fallback_units':Decimal(0), 'sources':set(),
                'unverified_cost_transactions':0,
                'cost_methods':Counter(), 'cost_gaps':Counter(), 'storno_excess':Decimal(0)})
            for key in ('rev','payout','ad'):
                row[key] += dec(e.get(key))
            row['sources'].add(e['account'])
            row['unverified_cost_transactions'] += int(bool(e.get('unverified_cost')))
        for product in e.get('products', []):
            qty = dec(product['quantity'])
            if qty <= 0:
                continue
            key = (e['account'], e.get('reference'), product['sku'])
            cost, missing, fallback = Decimal(0), Decimal(0), Decimal(0)
            methods = Counter()
            if e['kind'] == 'sale':
                cpu = product.get('unit_cost')
                cpu = dec(cpu) if cpu is not None and dec(cpu) > 0 else None
                method = product.get('cost_method', 'unknown')
                lots[key].append([qty, cpu, method])
                known.add(key)
                if cpu is None:
                    missing = qty
                else:
                    cost = qty * cpu
                    fallback = qty if method not in EXACT_METHODS else Decimal(0)
                    methods[method] += qty
            elif e['kind'] == 'return':
                remaining = qty
                while remaining > 0 and lots[key]:
                    lot = lots[key][0]
                    take = min(remaining, lot[0])
                    if lot[1] is None:
                        missing += take
                    else:
                        cost -= take * lot[1]
                        methods['original_sale_return'] += take
                        if lot[2] not in EXACT_METHODS:
                            fallback += take
                    remaining -= take
                    lot[0] -= take
                    if lot[0] == 0:
                        lots[key].popleft()
                if remaining:
                    if key not in known:
                        missing += remaining
                    elif selected:
                        row['storno_excess'] += remaining
            if selected:
                row['cost_units'] += qty
                row['cogs'] += cost
                row['missing_qty'] += missing
                if missing:
                    row['cost_gaps'][(e['account'],e.get('reference'),product['sku'],e['kind'])] += missing
                row['fallback_units'] += fallback
                row['cost_methods'].update(methods)
    result = []
    for w, r in sorted(weeks.items()):
        missing_accounts = sorted(set(expected) - r['sources'])
        complete = not r['missing_qty'] and not missing_accounts and not r['unverified_cost_transactions']
        net = r['payout'] - r['cogs'] if complete else None
        units = r['cost_units']
        result.append({'wk':w, 'rev':money(r['rev']), 'payout':money(r['payout']),
            'net':money(net) if net is not None else None, 'ad':money(r['ad']),
            # Financial ad charges are already in payout, never subtract them twice.
            'net_after_ad':money(net) if net is not None else None,
            'margin':round(float(net/r['rev']*100),1) if net is not None and r['rev'] else None,
            'cogs':money(r['cogs']) if complete else None,
            'known_cogs':money(r['cogs']), 'missing_cost_units':float(r['missing_qty']),
            'cost_coverage_pct':round(float((units-r['missing_qty'])/units*100),2) if units else 100.,
            'fallback_cost_units':float(r['fallback_units']),
            'unverified_cost_transactions':r['unverified_cost_transactions'],
            'cost_methods':{k:float(v) for k,v in r['cost_methods'].items()},
            'cost_gaps':[{'account':a,'reference':ref,'sku':sku,'kind':kind,'quantity':float(q)}
                         for (a,ref,sku,kind),q in r['cost_gaps'].items()],
            'missing_accounts':missing_accounts, 'sources':sorted(r['sources']),
            'storno_excess_units':float(r['storno_excess'])})
    return result


def _load(start, end, accounts):
    """Thin financial fields; historical sale/return rows provide exact reversal lots."""
    wb_accounts = [a for a in accounts if a.startswith('wb_')]
    oz_accounts = [a for a in accounts if a.startswith('oz_')]
    wb = db.query("""SELECT account, payload->>'rr_dt' date, payload->>'rrd_id' rid,
        payload->>'supplier_oper_name' op, payload->>'assembly_id' aid,
        payload->>'srid' srid, payload->>'nm_id' sku, payload->>'sa_name' offer,
        payload->>'order_dt' order_date, payload->>'quantity' quantity,
        payload->>'retail_amount' revenue, payload->>'ppvz_for_pay' pay,
        payload->>'delivery_rub' delivery, payload->>'storage_fee' storage,
        payload->>'acceptance' acceptance, payload->>'deduction' deduction,
        payload->>'penalty' penalty, payload->>'cashback_amount' cashback,
        payload->>'bonus_type_name' bonus
        FROM raw_wb_report WHERE account=ANY(%s) AND (payload->>'rr_dt')::date<=%s
        AND ((payload->>'rr_dt')::date>=%s OR payload->>'supplier_oper_name' IN ('Продажа','Возврат'))""",
        (wb_accounts,end,start))
    oz = db.query("""SELECT account,payload->>'operation_date' date,
        payload->>'operation_id' rid,payload->>'operation_type' op,
        payload->>'accruals_for_sale' revenue,payload->>'amount' amount,
        payload->>'sale_commission' commission,payload->'services' services,
        payload->'posting'->>'posting_number' posting,
        payload->'posting'->>'order_date' order_date,
        (SELECT jsonb_agg(i->>'sku') FROM jsonb_array_elements(
          CASE WHEN jsonb_typeof(payload->'items')='array' THEN payload->'items' ELSE '[]'::jsonb END) i) skus
        FROM raw_ozon_transaction WHERE account=ANY(%s)
        AND (payload->>'operation_date')::date<=%s
        AND ((payload->>'operation_date')::date>=%s
          OR coalesce((payload->>'accruals_for_sale')::numeric,0)<>0)""", (oz_accounts,end,start))
    proofs = db.query("""SELECT demand_name,org,agent,cogs,qty,
        (""" + fifo_fallback.COMPLETE_DEMAND_SQL + """) complete
        FROM ms_demand_cogs d WHERE org=ANY(%s)
        AND agent IN ('Покупатель ВБ','Покупатель Озон','Озон Экспресс')""",
        ([ORG_IDS[a[-1]] for a in accounts],))
    ff = fifo_fallback.load()
    oz_cards = db.query('SELECT account,sku,offer_id,name FROM ozon_product WHERE account=ANY(%s)', (oz_accounts,))
    offers = {(r['account'],str(r['sku'])):r['offer_id'] for r in oz_cards}
    titles = {(r['account'],r['offer_id']):r['name'] for r in oz_cards}
    titles.update({(r['account'],r['offer']):r['title'] for r in db.query(
        'SELECT account,vendor_code offer,title FROM wb_cards WHERE account=ANY(%s)', (wb_accounts,))})
    manual = {(r['platform'],r['article']):dec(r['unit_cost']) for r in db.query(
        'SELECT platform,article,unit_cost FROM cogs_manual WHERE unit_cost>0')}
    fbo = {(r['account'],r['posting_number']):dec(r['cost']) for r in db.query("""
        SELECT o.account,o.posting_number,s.cost FROM mp_fbo_orders o
        JOIN mp_fbo_trace_state s ON s.order_id=o.id
        WHERE o.account=ANY(%s) AND s.state='confirmed' AND s.cost>0
          AND o.created_at::date<=%s""", (accounts,end))}
    totals, invalid = defaultdict(Decimal), set()
    proof_counts, legacy_candidates = Counter(), defaultdict(set)
    for p in proofs:
        platform = 'wb' if p['agent']=='Покупатель ВБ' else 'ozon'
        name = p['demand_name']
        key = (platform,p['org'],name)
        proof_counts[key] += 1
        if platform=='ozon':
            legacy_candidates[(p['org'],normalize_posting(name))].add(name)
        if not p['complete']:
            invalid.add(key)
        totals[key] += dec(p['cogs'])
    denominators = defaultdict(Decimal)
    for r in wb:
        if r['op']=='Продажа':
            reference = r['aid'] if r['aid'] not in (None,'','0') else r['srid']
            if reference:
                denominators[(r['account'],reference)] += dec(r['quantity'])
    financial_postings = defaultdict(set)
    for r in oz:
        if r['op']=='OperationAgentDeliveredToCustomer' and dec(r['revenue'])>0:
            denominators[(r['account'],r['posting'])] += len(r['skus'] or [])
            financial_postings[(r['account'],normalize_posting(r['posting']))].add(r['posting'])

    @lru_cache(maxsize=100000)
    def cost(platform, account, reference, offer, when):
        key = (platform,ORG_IDS[account[-1]],reference)
        denominator = denominators.get((account,reference),0)
        if (account,reference) in fbo and denominator>0:
            return fbo[(account,reference)]/denominator,'fbo_unit_fifo'
        if platform=='ozon' and key not in totals:
            legacy = legacy_candidates.get((ORG_IDS[account[-1]],normalize_posting(reference)),set())
            postings = financial_postings.get((account,normalize_posting(reference)),set())
            # A single legacy suffix may be resolved unambiguously. Never pool
            # distinct physical places into an average shipment cost.
            if len(legacy)==1 and len(postings)==1:
                key = (platform,ORG_IDS[account[-1]],next(iter(legacy)))
        if reference and key not in invalid and proof_counts[key]==1 and totals.get(key,0)>0 and denominator>0:
            return totals[key]/denominator, 'shipment_fifo'
        from ops.fbs_ms_bundle_quantity import bundle_multiplier
        try:
            multiplier = bundle_multiplier(offer, titles.get((account,offer)) or '')
        except ValueError:
            return None,'unverified_bundle'
        if offer and when:
            unit, method = ff.unit(offer[:4] if multiplier>1 else offer, when)
            if unit is not None and unit>0:
                return dec(unit)*multiplier,method
        unit = manual.get((platform,offer))
        return (unit,'manual') if unit else (None,'unknown')

    events = []
    for r in wb:
        date = day(r['date'])
        if date is None:
            continue
        kind = 'sale' if r['op']=='Продажа' else 'return' if r['op']=='Возврат' else 'fee'
        sign = -1 if kind=='return' else 1
        rev = sign*dec(r['revenue']) if kind!='fee' else Decimal(0)
        payout = sign*dec(r['pay']) - sum((dec(r[k]) for k in ('delivery','storage','acceptance','deduction','penalty','cashback')),Decimal(0))
        ad = dec(r['deduction']) if 'продвижен' in (r['bonus'] or '').lower() else Decimal(0)
        aid = r['aid'] if r['aid'] not in (None,'','0') else None
        reference = aid or r['srid'] or ('unlinked:'+str(r['rid']))
        cpu, method = cost('wb',r['account'],aid or r['srid'],r['offer'],day(r['order_date'])) if kind=='sale' else (None,None)
        products = [{'sku':str(r['sku']), 'quantity':r['quantity'], 'unit_cost':cpu, 'cost_method':method}] if kind!='fee' else []
        events.append(dict(account=r['account'],date=date,sort=str(r['rid'] or ''),
            reference=reference,kind=kind,rev=rev,payout=payout,ad=ad,products=products,
            unverified_cost=kind!='fee' and r['quantity'] is None))
    for r in oz:
        date = day(r['date'])
        if date is None:
            continue
        rev = dec(r['revenue'])
        kind = 'sale' if r['op']=='OperationAgentDeliveredToCustomer' and rev>0 else 'return' if rev<0 else 'fee'
        services = r['services'] or []
        residual = dec(r['amount'])-rev-dec(r['commission'])-sum((dec(s.get('price')) for s in services),Decimal(0))
        ad = -residual if RESID_OPS.get(r['op'],('',))[0]=='promo' else Decimal(0)
        ad -= sum((dec(s.get('price')) for s in services if SERVICES.get(s.get('name'),('',))[0]=='promo'),Decimal(0))
        reference = r['posting']
        products = []
        for sku in (r['skus'] or []) if kind!='fee' else []:
            offer = offers.get((r['account'],str(sku)))
            cpu, method = cost('ozon',r['account'],reference,offer,day(r['order_date'])) if kind=='sale' else (None,None)
            products.append({'sku':str(sku),'quantity':1,'unit_cost':cpu,'cost_method':method})
        events.append(dict(account=r['account'],date=date,sort=str(r['rid'] or ''),
            reference=reference or ('unlinked:'+str(r['rid'])),kind=kind,
            rev=rev,payout=dec(r['amount']),ad=ad,products=products,
            unverified_cost=kind!='fee' and not products))
    return events


def report(period='', org=''):
    if org and org not in ACCOUNTS:
        raise ValueError('Unknown organization')
    if not period:
        latest = db.query("""SELECT to_char(max(d),'YYYY-MM-01') p FROM (
            SELECT max((payload->>'rr_dt')::date) d FROM raw_wb_report
            UNION ALL SELECT max((payload->>'operation_date')::date) FROM raw_ozon_transaction) r""")[0]['p']
        if not latest:
            return {'rows':[], 'data_available':False}
        period = latest
    period = dt.date.fromisoformat(period).replace(day=1).isoformat()
    accounts = list(ACCOUNTS[org]) if org else [a for aa in ACCOUNTS.values() for a in aa]
    start,end = bounds(period)
    key = (period,org)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and time.monotonic()-cached[0]<300:
            return cached[1]
        events = _load(start,end,accounts)
        rows = aggregate(events,start,end,accounts)
        from zoneinfo import ZoneInfo
        today = dt.datetime.now(ZoneInfo('Europe/Moscow')).date()
        for r in rows:
            r['partial_week'] = day(r['wk'])+dt.timedelta(days=6)>=today
        result = {'rows':rows,'data_available':bool(rows),'period':period,'org':org,
            'platforms':['wb','ozon'],'ads_basis':'financial_promotion_charges',
            'cost_basis':'shipment_fifo_then_dated_product_fifo_or_manual',
            'net_basis':'marketplace_settlements_minus_cogs_before_opex',
            'calculated_at':dt.datetime.now(dt.timezone.utc).isoformat()}
        if len(_CACHE)>=16:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key]=(time.monotonic(),result)
        return result
