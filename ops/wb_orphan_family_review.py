#!/usr/bin/env python3
# поток: mkt
"""Read-only audit of active WB families whose four-digit primary is absent."""
import html
import argparse
import json
import os
from pathlib import Path
import re
import time

import requests
from core import db
from collectors.wb import _content_token,_token
from ops import wb_bundle_regroup as regroup
from ops.wb_bundle_plan import fetch_catalog,is_bundle
from ops.wb_bundle_glue_singletons import now

ROOT=regroup.ROOT/'docs/reports'
SOURCE=ROOT/'mkt_wb_colour_chip_review_2026-10-12_acc1'
OUT=ROOT/'mkt_wb_orphan_families_2026-10-12_acc1'
CONTENT='https://content-api.wildberries.ru/content/v2/get/cards/'


def read(url,header,body=None):
    allowed=(url.startswith(CONTENT) and url.split('/')[-1] in {'list','trash'}) or url=='https://marketplace-api.wildberries.ru/api/v3/warehouses' or bool(re.fullmatch(r'https://marketplace-api.wildberries.ru/api/v3/stocks/\d+',url)) or url=='https://seller-analytics-api.wildberries.ru/api/analytics/v1/stocks-report/wb-warehouses'
    if not allowed:raise RuntimeError('Outside read-only endpoints')
    for attempt in range(5):
        response=requests.get(url,headers=header,timeout=60) if body is None else requests.post(url,headers=header,json=body,timeout=60)
        if response.status_code==429 or response.status_code>=500:
            time.sleep(min(30,3*(attempt+1)));continue
        response.raise_for_status();return response.json()
    raise RuntimeError('Read retries exhausted')


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    snapshot=json.loads((SOURCE/'catalog.json').read_text());cards=snapshot['cards']
    exact={c['vendorCode'] for c in cards if re.fullmatch(r'\d{4}',c['vendorCode'])}
    orphan={}
    for c in cards:
        code=c['vendorCode']
        if re.match(r'^\d{4}(?:[XХ]\d+|\d)',code,re.I) and code[:4] not in exact:
            orphan.setdefault(code[:4],[]).append(c)
    old=json.loads((ROOT/'mkt_wb_parent_audit_2026-10-12.json').read_text())['missing_families']
    codes=sorted(set(orphan)|set(old));lookup={};live={};h={'Authorization':_content_token('wb_acc1'),'Content-Type':'application/json'}
    historical=db.query('select nm_id,vendor_code,title,updated_at from wb_cards where account=%s and substring(vendor_code,1,4)=any(%s)',('wb_acc1',codes))
    regroup.save(OUT/'historical_wb_cards.json',historical)
    known={c['nmID']:c for code in codes for c in orphan.get(code,[])+old.get(code,[])}
    for row in historical:
        known.setdefault(int(row['nm_id']),{'nmID':int(row['nm_id']),'vendorCode':row['vendor_code'],'title':row['title']})
    # Exact primary search has already been verified against positive controls.
    for code in codes:
        found=fetch_catalog('wb_acc1',text_search=code)
        lookup[code]=[c for c in found if c['vendorCode']==code]
        for c in found:
            if c['vendorCode'].startswith(code):live[c['nmID']]=c
        time.sleep(.7)
    for nm in sorted(known):
        if nm in live:continue
        found=fetch_catalog('wb_acc1',text_search=nm)
        for c in found:
            if c['nmID']==nm:live[nm]=c
        time.sleep(.7)
    regroup.save(OUT/'live_active.json',{'at':now(),'primaries':lookup,'cards':list(live.values())})
    print('Live active checked',len(known),'known cards; found',len(live),flush=True)
    trash={};cursor={'limit':100};seen=set();total=0
    for page in range(1000):
        data=read(CONTENT+'trash',h,{'settings':{'cursor':cursor,'sort':{'ascending':False}}})
        if not isinstance(data.get('cards'),list):raise RuntimeError('No trash cards array')
        batch=data['cards'];total+=len(batch)
        for c in batch:
            if c.get('vendorCode','')[:4] in codes:trash[c['nmID']]=c
        if len(batch)<100:break
        cur=data.get('cursor') or {};key=(cur.get('trashedAt'),cur.get('nmID'))
        if not all(key) or key in seen:raise RuntimeError('Trash cursor did not advance')
        seen.add(key);cursor={'limit':100,'trashedAt':key[0],'nmID':key[1]};time.sleep(.8)
    else:raise RuntimeError('Trash pagination limit')
    regroup.save(OUT/'live_trash.json',{'at':now(),'all_trash_cards_examined':total,'cards':list(trash.values())})
    if set(live)&set(trash):raise RuntimeError('Active and trash snapshots disagree')
    records=merged_records(known,trash,live)
    sizes={c['nmID']:[s['chrtID'] for s in c.get('sizes',[]) if s.get('chrtID')] for c in records.values()}
    ids=sorted({i for v in sizes.values() for i in v})
    fbs={};errors={};warehouses=[]
    try:
        token=os.getenv('WB_TOKEN_RETURNS_ACC1')
        if not token:raise RuntimeError('Marketplace read token unavailable')
        hm={'Authorization':token,'Content-Type':'application/json'}
        warehouses=read('https://marketplace-api.wildberries.ru/api/v3/warehouses',hm)
        if not isinstance(warehouses,list) or not warehouses:raise RuntimeError('Warehouse list unavailable')
        for wh in warehouses:
            data=read(f'https://marketplace-api.wildberries.ru/api/v3/stocks/{wh["id"]}',hm,{'chrtIds':ids})
            if not isinstance(data.get('stocks'),list):raise RuntimeError('Stocks array missing')
            fbs[str(wh['id'])]={str(v['chrtId']):v['amount'] for v in data['stocks']}
    except Exception as exc:errors['fbs']=str(exc)
    regroup.save(OUT/'fbs.json',{'at':now(),'warehouses':warehouses,'stocks':fbs,'errors':errors})
    fbo={};fbo_items=[]
    try:
        token=os.getenv('WB_TOKEN_ANALYTICS_ACC1') or _token('wb_acc1')
        data=read('https://seller-analytics-api.wildberries.ru/api/analytics/v1/stocks-report/wb-warehouses',{'Authorization':token,'Content-Type':'application/json'},
                  {'nmIds':sorted(records),'limit':1000,'offset':0})
        regroup.save(OUT/'fbo_raw.json',data)
        fbo_items=(data.get('data') or {}).get('items')
        if not isinstance(fbo_items,list) or len(fbo_items)>=1000:raise RuntimeError('FBO read incomplete')
        if any(x.get('nmId') not in records for x in fbo_items):raise RuntimeError('Unexpected FBO card')
        for item in fbo_items:
            value=item.get('quantity')
            if value is not None:fbo[item['nmId']]=fbo.get(item['nmId'],0)+value
    except Exception as exc:errors['fbo']=str(exc)
    # A dated independent FBO snapshot remains evidence even when the live API
    # is unavailable. An absent row is unknown, never a fabricated zero.
    dated=db.query('''select distinct on (nm_id,warehouse) nm_id,warehouse,quantity,captured_ts
                     from wb_stocks where account=%s and nm_id=any(%s)
                     order by nm_id,warehouse,captured_ts desc''',('wb_acc1',list(records)))
    supply=db.query('''select external_code,supplier,store,stock,reserve,captured_at from supplier_stock
                      where external_code=any(%s) and captured_at=(select max(captured_at) from supplier_stock)''',(codes,))
    tc=db.query('select external_code,title,gone_at from prc_tc_model where external_code=any(%s)',(codes,))
    regroup.save(OUT/'dated_stocks_and_tc.json',{'fbo_snapshot':dated,'supplier_stock':supply,'tc':tc})
    finish(codes,known,live,trash,lookup,fbs,errors,fbo,dated,supply,tc)


def merged_records(known,trash,live):
    output={}
    for nm in set(known)|set(trash)|set(live):
        card={**known.get(nm,{}),**trash.get(nm,{}),**live.get(nm,{})}
        card.setdefault('title',card.get('name') or 'Название недоступно в ответе корзины')
        output[nm]=card
    return output


def finish(codes,known,live,trash,lookup,fbs,errors,fbo,dated,supply,tc):
    records=merged_records(known,trash,live)
    sizes={c['nmID']:[s['chrtID'] for s in c.get('sizes',[]) if s.get('chrtID')] for c in records.values()}
    result=[]
    for code in codes:
        primary=[c for c in records.values() if c['vendorCode']==code]
        family=[]
        for c in sorted([c for c in records.values() if c['vendorCode'].startswith(code)],key=lambda c:c['vendorCode']):
            size_ids=sizes[c['nmID']]
            cells=[fbs[w].get(str(i)) for w in fbs for i in size_ids]
            fq=sum(cells) if cells and all(v is not None for v in cells) and not errors.get('fbs') else None
            rows=[v for v in dated if v['nm_id']==c['nmID']]
            family.append({'nm_id':c['nmID'],'vendor_code':c['vendorCode'],'title':c['title'],
                           'kind':'основная' if c['vendorCode']==code else 'бандл' if is_bundle(c) else 'дочерняя',
                           'status':'в активном каталоге' if c['nmID'] in live else 'в корзине WB' if c['nmID'] in trash else 'не найдено в активных и корзине',
                           'trashed_at':trash.get(c['nmID'],{}).get('trashedAt'),'fbs_quantity':fq,
                           'fbo_live_quantity':fbo.get(c['nmID']),
                           'fbo_snapshot_quantity':sum(float(v['quantity']) for v in rows) if rows else None,
                           'fbo_snapshot_at':max(str(v['captured_ts']) for v in rows) if rows else None})
        result.append({'code':code,'primary_active':[c['nmID'] for c in lookup[code]],
                       'primary_in_trash':[c['nmID'] for c in primary if c['nmID'] in trash],
                       'primary_history_ids':[c['nmID'] for c in primary],
                       'cards':family,'supplier_stock_rows':[x for x in supply if x['external_code']==code],
                       'tc_rows':[x for x in tc if x['external_code']==code]})
    summary={'account':'wb_acc1','checked_at':now(),'families':len(codes),
             'active_tail_families':sum(any(c['status']=='в активном каталоге' and c['kind']!='основная' for c in r['cards']) for r in result),
             'primary_in_trash_families':sum(bool(r['primary_in_trash']) for r in result),
             'active_bundles':sum(c['kind']=='бандл' and c['status']=='в активном каталоге' for r in result for c in r['cards']),
             'active_children':sum(c['kind']=='дочерняя' and c['status']=='в активном каталоге' for r in result for c in r['cards']),
             'active_positive_fbs_cards':sum(c['status']=='в активном каталоге' and (c['fbs_quantity'] or 0)>0 for r in result for c in r['cards']),
             'active_zero_fbs_cards':sum(c['status']=='в активном каталоге' and c['fbs_quantity']==0 for r in result for c in r['cards']),
             'active_unknown_fbs_cards':sum(c['status']=='в активном каталоге' and c['fbs_quantity'] is None for r in result for c in r['cards']),
             'inactive_positive_fbs_cards':sum(c['status']!='в активном каталоге' and (c['fbs_quantity'] or 0)>0 for r in result for c in r['cards']),
             'errors':errors,'rows':result,'marketplace_writes':0}
    regroup.save(OUT/'summary.json',summary);render(summary)
    print(json.dumps({k:v for k,v in summary.items() if k!='rows'},ensure_ascii=False),flush=True)


def render(summary):
    def esc(x):return html.escape(str(x))
    def qty(v):return 'число не возвращено API' if v is None else str(v)
    body=[]
    for row in summary['rows']:
        primary=('Основная сейчас активна: '+', '.join(map(str,row['primary_active'])) if row['primary_active'] else
                 'Удаление основной подтверждено: она в корзине WB, nmID '+', '.join(map(str,row['primary_in_trash'])) if row['primary_in_trash'] else
                 ('Раньше основная существовала (исторический nmID '+', '.join(map(str,row['primary_history_ids']))+'), сейчас не найдена среди активных и в корзине; текущее удаление не подтверждено корзиной' if row['primary_history_ids'] else
                  'Основная не найдена среди активных и в корзине; в доступной истории точный четырёхзначный артикул также не найден. Факт удаления не подтверждён'))
        members=[]
        for c in row['cards']:
            url=f'https://www.wildberries.ru/catalog/{c["nm_id"]}/detail.aspx'
            members.append(f'<tr><td><a href="{url}" target="_blank">{c["nm_id"]} · {esc(c["vendor_code"])} · {esc(c["title"])}</a></td><td>{esc(c["kind"])}</td><td>{esc(c["status"])}</td><td>{qty(c["fbs_quantity"])}</td><td>{qty(c["fbo_live_quantity"])}</td><td>{qty(c["fbo_snapshot_quantity"])}<br>{esc(c["fbo_snapshot_at"] or "")}</td></tr>')
        supply=row['supplier_stock_rows'];positive=[r for r in supply if r['stock'] is not None and float(r['stock'])>0]
        supply_text=f'МС/поставщики: {len(positive)} строк с положительным остатком из {len(supply)} на дату '+esc(max([str(r['captured_at']) for r in supply],default='нет данных'))+'. Это запас поставщиков, не подтверждение покупки на WB.'
        body.append(f'<details open><summary><b>Родительский код {esc(row["code"])}</b></summary><p>{primary}.</p><p>{supply_text}</p><div style="overflow:auto"><table><tr><th>Карточка</th><th>Вид</th><th>Состояние WB</th><th>FBS сейчас, шт.</th><th>FBO сейчас, шт.</th><th>FBO последний срез, шт./дата</th></tr>'+''.join(members)+'</table></div></details>')
    text='<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Хвосты без родительской карточки — acc1</title><style>body{max-width:1300px;margin:28px auto;padding:20px;font:16px system-ui;line-height:1.5}a{color:#14615c}table{border-collapse:collapse}td,th{padding:8px;border:1px solid #bfc9c2;vertical-align:top}details{margin:16px 0}</style><h1>Семейства без основной карточки — acc1</h1>'
    text+=f'<p>Проверено {summary["families"]} семейств: все нынешние случаи по бандлам/дочерним и ранее найденные удалённые хвосты. Активных семейств с хвостами {summary["active_tail_families"]}, бандлов {summary["active_bundles"]}, дочерних {summary["active_children"]}. У {summary["primary_in_trash_families"]} семейств основная найдена в корзине WB. Проверка {esc(summary["checked_at"])}.</p>'
    text+='<p>Активная карточка, удалённая карточка и остаток — разные факты. «Не удалось подтвердить» не означает ноль. FBS — живые остатки всех складов продавца, FBO — склады WB; остаток поставщиков приведён отдельно. Ничего не удаляем и не обнуляем в этом проходе.</p>'
    text+=f'<p>Из активных хвостов: FBS положительный у {summary["active_positive_fbs_cards"]}, нулевой у {summary["active_zero_fbs_cards"]}, число не возвращено у {summary["active_unknown_fbs_cards"]}. У {summary["inactive_positive_fbs_cards"]} карточек в корзине числится положительный FBS — это не подтверждает возможность покупки удалённой карточки.</p>'
    if summary['errors']:text+='<p>Ограничения проверки: '+esc(summary['errors'])+'</p>'
    text+=''.join(body)+'</html>'
    (OUT/'index.html').write_text(text)
    (ROOT/'mkt_wb_missing_parent_cards_2026-10-12.html').write_text(text)


def replay():
    historical=json.loads((OUT/'historical_wb_cards.json').read_text())
    old=json.loads((ROOT/'mkt_wb_parent_audit_2026-10-12.json').read_text())['missing_families']
    snap=json.loads((SOURCE/'catalog.json').read_text())['cards']
    data=json.loads((OUT/'live_active.json').read_text());lookup=data['primaries'];codes=sorted(lookup)
    known={c['nmID']:c for c in snap if c.get('vendorCode','')[:4] in codes}
    for cc in old.values():
        for c in cc:known.setdefault(c['nmID'],c)
    for r in historical:known.setdefault(int(r['nm_id']),{'nmID':int(r['nm_id']),'vendorCode':r['vendor_code'],'title':r['title']})
    live={c['nmID']:c for c in data['cards']};trash={c['nmID']:c for c in json.loads((OUT/'live_trash.json').read_text())['cards']}
    fbs_data=json.loads((OUT/'fbs.json').read_text());errors=fbs_data['errors']
    fbo={}
    for r in json.loads((OUT/'fbo_raw.json').read_text())['data']['items']:
        if r.get('quantity') is not None:fbo[r['nmId']]=fbo.get(r['nmId'],0)+r['quantity']
    extra=json.loads((OUT/'dated_stocks_and_tc.json').read_text())
    finish(codes,known,live,trash,lookup,fbs_data['stocks'],errors,fbo,extra['fbo_snapshot'],extra['supplier_stock'],extra['tc'])


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--from-saved',action='store_true');args=parser.parse_args()
    replay() if args.from_saved else main()
