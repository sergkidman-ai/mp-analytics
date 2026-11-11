# поток: fin
"""Restore loss-only stock evidence and recalculate documented MS shipment costs.
Never writes marketplace/MS data, financial marts or invents missing official PDFs.
"""
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from core import db
from ops.wb_loss_official import Catalogue
from ops.wb_loss_cost import allocate
from ops.fbs_ms_bundle_quantity import bundle_multiplier

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = 'docs/reports/wb_lost_warehouses_2026-08-21.md'
ORGS = {'wb_acc1':'a5e7ee84-8d63-41f2-9fae-0be2ef90462b',
        'wb_acc2':'f5bea6e0-c2ac-11ef-0a80-0221000adcc4'}
LABELS = {'Новосемейкино':'Самара (Новосемейкино)', 'Владимир: Воршинское':'Владимир',
          'Алексин (склад ВБ «Тула»)':'Тула', 'Шушары (склад)':'СПБ Шушары',
          'Шушары (сортировочный центр)':'СЦ Шушары', 'Коледино (Подольск)':'Коледино'}


def number(s):
    m = re.search(r'\d[\d \u00a0]*(?:[,.]\d+)?', s)
    return Decimal(m[0].replace(' ', '').replace('\u00a0', '').replace(',', '.')) if m else None


def archived_rows(text):
    rows=[]; wh=None; snap=None
    for line in text.splitlines():
        if line.startswith('### '):
            wh=line[4:].split(' — удар ')[0];wh=LABELS.get(wh,wh);snap=None
        elif line.startswith('Снимок '):
            snap=datetime.strptime(line.split()[1],'%d.%m.%Y').date().isoformat()
        elif line.startswith('| wb_acc'):
            c=[x.strip() for x in line.strip('|').split('|')]
            rows.append(dict(account=c[0],warehouse=wh,nm_id=int(c[1]),vendor_code=c[2],qty=int(c[3]),
                snapshot_date=snap,source='archived_20260822',source_ref=ARCHIVE,
                archive=dict(qty=int(c[3]),components=number(c[4]),unit_cost=number(c[5]) or None,
                             supply_date=c[6],total_cost=number(c[7]) or None,precision='rounded_rubles')))
    if len({(r['account'],r['warehouse'],r['nm_id']) for r in rows})!=len(rows):
        raise ValueError('duplicate_archive_stock')
    return rows


def stock_rows():
    old=archived_rows((ROOT/ARCHIVE).read_text())
    latest=db.query('''SELECT s.account,s.warehouse,s.nm_id,s.vendor_code,s.quantity qty,s.captured_at
      FROM wb_stocks s JOIN wb_damage_stock_warehouse w USING(warehouse)
      WHERE s.captured_at=(SELECT max(z.captured_at) FROM wb_stocks z
        WHERE z.account=s.account AND z.warehouse=s.warehouse)''')
    # Whole account/warehouse snapshot replaces the old snapshot, never add revisions together.
    groups={(r['account'],r['warehouse']) for r in latest}
    index={(r['account'],r['warehouse'],r['nm_id']):r['archive'] for r in old}
    out=[r for r in old if (r['account'],r['warehouse']) not in groups]
    for r in latest:
        if r['qty']<=0:continue
        out.append(dict(account=r['account'],warehouse=r['warehouse'],nm_id=r['nm_id'],
            vendor_code=r['vendor_code'],qty=int(r['qty']),snapshot_date=str(r['captured_at']),
            source='frozen_wb_stock',source_ref='wb_stocks',
            archive=index.get((r['account'],r['warehouse'],r['nm_id']))))
    return out


def catalogue():
    models=[r['external_code'] for r in db.query('SELECT external_code FROM prc_tc_model')]
    sets={r['external_code']:r['components'] for r in db.query('SELECT external_code,components FROM set_cost WHERE components IS NOT NULL')}
    links=[(r['external_code'],r['ref_code']) for r in db.query('SELECT external_code,ref_code FROM prc_tc_link')]
    path=ROOT/'.private/wb_loss_restore_20261106/ms_universal_links.json'
    if path.exists():links.extend(tuple(x) for x in json.loads(path.read_text()))
    else:
        saved=db.query("SELECT payload FROM wb_loss_recovery_snapshot WHERE id='ms_universal_links_20261106'")
        if saved:links.extend(tuple(x) for x in saved[0]['payload']['links'])
    return Catalogue(models,sets,links)


def expand_card(cat,code,title=''):
    # Confirmed bundle markers multiply a whole BOM; derivative suffixes alone do not.
    m=re.fullmatch(r'(\d{4})[XХxх](\d+)([A-Za-z0-9]*)',code or '')
    derivative = bool(m and m[3] and int(m[2]) > 1000 and not re.match(r'^(Комплект|Бандл)\b',title or '',re.I))
    multiplier=bundle_multiplier(code,title) if m and not derivative else 1
    base=m[1] if m and multiplier>1 else code
    return Counter({c:q*multiplier for c,q in cat.expand(base).items()})


def build():
    items=stock_rows();cat=catalogue()
    titles={(r['account'],r['nm_id']):r['title'] for r in db.query('SELECT account,nm_id,title FROM wb_cards')}
    result={}; counts={}
    for account,org in ORGS.items():
        operations=defaultdict(set)
        for r in db.query("SELECT DISTINCT payload->>'assembly_id' aid,payload->>'supplier_oper_name' op FROM raw_wb_report WHERE account=%s",(account,)):
            operations[r['aid']].add(r['op'])
        orders={str(r['order_id']):r['payload'] for r in db.query(
            "SELECT order_id,payload FROM wb_loss_order_raw WHERE account=%s AND source LIKE 'archive:%%'",(account,))}
        returned={r['demand_name'] for r in db.query("SELECT demand_name FROM ms_return_cogs WHERE org=%s AND agent='Покупатель ВБ'",(org,))}
        raw=db.query('''SELECT d.demand_id,d.demand_name,d.moment,d.cogs,d.qty,d.npos,d.loaded_at,
           p.ms_id,p.qty position_qty,p.cost,m.external_code,m.name
          FROM ms_demand_cogs d JOIN ms_demand_pos p USING(demand_id)
          LEFT JOIN ms_product m USING(ms_id)
          WHERE d.org=%s AND d.agent='Покупатель ВБ' AND d.moment<'2026-07-16 00:00:00+00' ''',(org,))
        docs={}
        for r in raw:
            d=docs.setdefault(r['demand_id'],dict(demand_id=r['demand_id'],order_id=r['demand_name'],
                moment=r['moment'].replace(tzinfo=timezone(timedelta(hours=3))).isoformat(),positions=[],parts=Counter(),errors=[],cost=Decimal(0),
                expected_qty=r['qty'],expected_cost=r['cogs'],npos=r['npos'],loaded_at=str(r['loaded_at'])))
            d['positions'].append(dict(ms_id=r['ms_id'],external_code=r['external_code'],qty=str(r['position_qty']),name=r['name'],cost=str(r['cost'])))
            if r['cost'] is None or r['cost']<=0:d['errors'].append('missing_cost')
            else:d['cost']+=r['cost']
            try:
                if r['position_qty'] is None or r['position_qty']<=0:raise ValueError('invalid_qty')
                for c,n in cat.expand(r['external_code']).items():d['parts'][c]+=n*r['position_qty']
            except (ValueError,SyntaxError) as e:d['errors'].append(str(e))
        for lookup in db.query("SELECT payload FROM wb_loss_ms_lookup WHERE account=%s",(account,)):
            for rawdoc in lookup['payload'].get('rows',[]):
                if rawdoc['organization']['meta']['href'].split('/')[-1] != org:
                    raise ValueError('MS_organization_mismatch')
                if rawdoc['moment'] >= '2026-07-16' or not rawdoc.get('applicable'):continue
                positions=rawdoc['positions'].get('rows',[])
                if len(positions)!=rawdoc['positions']['meta']['size']:raise ValueError('incomplete_live_positions')
                d=dict(demand_id=rawdoc['id'],order_id=rawdoc['name'],
                    moment=rawdoc['moment']+'+03:00',positions=[],parts=Counter(),errors=[],
                    cost=Decimal(0),expected_qty=sum(Decimal(str(x['quantity'])) for x in positions),
                    expected_cost=None,npos=len(positions),loaded_at='live_MS_restoration')
                for pos in positions:
                    m=pos['assortment'];q=Decimal(str(pos['quantity']))
                    d['positions'].append(dict(ms_id=m['id'],external_code=m.get('externalCode'),
                        name=m.get('name'),qty=str(q)))
                    try:
                        if q<=0:raise ValueError('invalid_qty')
                        for c,n in cat.expand(m.get('externalCode')).items():d['parts'][c]+=n*q
                    except (ValueError,SyntaxError) as e:d['errors'].append(str(e))
                d['errors'].append('missing_cost')
                docs[d['demand_id']]=d
        stored_costs={r['demand_id']:r for r in db.query(
            "SELECT demand_id,total,state FROM wb_loss_demand_cost WHERE account=%s",(account,))}
        candidates=defaultdict(list)
        for d in docs.values():
            d['operations']=sorted(x for x in operations[d['order_id']] if x)
            status=orders.get(d['order_id'],{}).get('status',{})
            if d['order_id'] in returned:continue
            if status.get('wbStatus') in ('declined_by_client','canceled') or status.get('supplierStatus')=='cancel':continue
            if status.get('wbStatus') not in ('canceled_by_client','defect') and 'Возврат' not in d['operations']:continue
            d['wb_status']=status.get('wbStatus')
            if len(d['positions'])!=d['npos'] or sum(Decimal(p['qty']) for p in d['positions'])!=d['expected_qty']:d['errors'].append('incomplete_positions')
            if d['parts'] and not [e for e in d['errors'] if e!='missing_cost']:
                cached=stored_costs.get(d['demand_id'])
                if cached:
                    d['cost']=cached['total'];d['expected_cost']=cached['total']
                    d['errors']=[] if cached['state']=='ok' and cached['total'] is not None else ['missing_cost']
                candidates[cat.signature(d['parts'])].append(d)
        current=[]
        for r in items:
            if r['account']!=account:continue
            i=dict(r,document_id='recovered:'+account+':'+r['warehouse'])
            try:
                bom=expand_card(cat,r['vendor_code'],titles.get((account,r['nm_id']),''));error=None
            except (ValueError,SyntaxError) as e:bom=Counter();error=str(e)
            i.update(bom=dict(bom),mapping_error=error,evidence=[])
            for d in candidates.get(cat.signature(bom),[]) if bom else []:
                cost_ok=not d['errors'] and d['expected_cost'] is not None and abs(d['cost']-d['expected_cost'])<Decimal('.01')
                i['evidence'].append(dict(demand_id=d['demand_id'],order_id=d['order_id'],moment=d['moment'],
                    positions=d['positions'],usable_candidate=True,cost_rub=str(d['cost'].quantize(Decimal('.01'))) if cost_ok else None,
                    cost_state='ok' if cost_ok else 'missing_or_inconsistent_cost',loaded_at=d['loaded_at'],operations=d['operations'],wb_status=d.get('wb_status')))
            i['candidate_count']=len(i['evidence']);current.append(i)
        allocated=allocate(current);by_key={(x['document_id'],x['nm_id']):x for x in allocated}
        for i in current:
            a=by_key[i['document_id'],i['nm_id']];i['selected']=a['selected'];i['unmatched_qty']=a['unmatched_qty']
            i['costed_qty']=sum(e['cost_rub'] is not None for e in i['selected'])
            i['known_cost_rub']=str(sum((Decimal(e['cost_rub']) for e in i['selected'] if e['cost_rub'] is not None),Decimal(0)))
            i['missing_reason'] = ('Не подтверждён состав товара' if i['mapping_error'] else
                'Не хватает подходящих отгрузок МС с подтверждённым возвратом' if i['unmatched_qty'] else
                'У выбранной отгрузки нет подтверждённой себестоимости МС' if i['costed_qty']<i['qty'] else None)
            i.pop('evidence')
        total=sum(x['qty'] for x in current);costed=sum(x['costed_qty'] for x in current)
        summary=dict(qty=total,rows=len(current),costed_qty=costed,missing_qty=total-costed,
            known_cost_rub=str(sum((Decimal(x['known_cost_rub']) for x in current),Decimal(0))))
        result[account]=dict(items=current,summary=summary)
        counts[account]=summary
    return dict(mode='recovered_snapshot',built_at=datetime.now(timezone.utc).isoformat(),accounts=result,
        source_note='Последние сохранившиеся замороженные остатки WB; для отсутствующих складов — архивный снимок из отчёта 22.08.2026. Это сведения для убытков, не доступный остаток и не восстановленные PDF-акты.',
        cost_note='Новый расчёт: самые поздние подходящие отгрузки МС до 15.07.2026 включительно, полный состав наборов, подтверждённые связи моделей, без повторного использования отгрузок внутри организации. Возврат в отчёте WB обязателен; возвраты продавцу по МС исключены. Связь с конкретной утраченной единицей не доказана.',
        archive_note='Архивная стоимость — сохранённый расчёт по последним приёмкам МС до происшествия, округлённый до рублей. Она показана отдельно от нового расчёта и не прибавляется к нему. Поздние ручные дополнения не найдены.',
        archived_items=archived_rows((ROOT/ARCHIVE).read_text()),
        universal_links_source='МС: дополнительное поле Связь, ведущие нули и базовые коды сохранены',
        archive_sha256=hashlib.sha256((ROOT/ARCHIVE).read_bytes()).hexdigest())


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--publish',action='store_true');args=p.parse_args()
    result=build();Path(args.output).write_text(json.dumps(result,ensure_ascii=False,default=str))
    if args.publish:
        from psycopg2.extras import Json
        db.upsert('wb_loss_recovery_snapshot',[dict(id='saved_sources_20261106',payload=Json(json.loads(json.dumps(result,default=str))))],['id'])
    print(json.dumps({a:r['summary'] for a,r in result['accounts'].items()},ensure_ascii=False))
