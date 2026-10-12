#!/usr/bin/env python3
# поток: mkt
"""Acc1 assortment, grouping, bundles, launches and orders: 12-point review."""
import argparse
from collections import Counter,defaultdict
import json
from pathlib import Path
import re
import datetime as dt
import html

from core import db
from ops import wb_bundle_regroup as regroup,wb_core_glue as core
from ops.wb_bundle_plan import fetch_catalog,char,color,is_bundle,normalized,quantities
from ops.wb_bundle_glue_singletons import now,read_references
from ops.wb_ad_enroll import composition
from ops.wb_component_gap_scan import laser,plain_card

ROOT=regroup.ROOT/'docs/reports'
OUT=ROOT/'mkt_wb_glue_status_2026-10-12_acc1'


def collect():
    OUT.mkdir(parents=True,exist_ok=True)
    cards=fetch_catalog('wb_acc1')
    regroup.save(OUT/'catalog.json',{'account':'wb_acc1','read_at':now(),'cards':cards})
    codes={regroup.base(c) for c in cards}-{None}
    refs,marks=read_references(codes)
    models=db.query('select external_code,title,consumable_type,color,resource,chip,printer_models,raw,first_seen,gone_at from prc_tc_model')
    regroup.save(OUT/'references.json',{'references':refs,'marks':{k:sorted(v) for k,v in marks.items()},'all_models':models,'read_at':now()})
    print('Collection saved',len(cards),'cards',len(models),'TC models',flush=True)


def collect_ads():
    OUT.mkdir(parents=True,exist_ok=True)
    comp=composition('wb_acc1')
    data={str(k):{**v,'nms':sorted(v['nms']),'subjects':dict(v['subjects'])} for k,v in comp.items()}
    regroup.save(OUT/'ad_composition.json',{'account':'wb_acc1','read_at':now(),'campaigns':data})
    print('Ad composition saved',len(data),'campaigns',flush=True)


def confirmed_events():
    out=[]
    for line in (ROOT/'mkt_glue_journal.jsonl').open():
        row=json.loads(line)
        if row.get('account')=='wb_acc1' and ((row.get('action')=='glue' and row.get('verified') is True) or (row.get('action')=='regroup' and row.get('status')=='confirmed')):
            out.append(row)
    return sorted(out,key=lambda r:r['ts'])


def analyse():
    snapshot=json.loads((OUT/'catalog.json').read_text());source=json.loads((OUT/'references.json').read_text())
    gap_state_path=OUT/'laser_gaps/apply_results.json'
    if gap_state_path.exists():
        state=json.loads(gap_state_path.read_text());lookup={c['nmID']:c for c in snapshot['cards']}
        for result in state['results']:
            if result['status']=='confirmed':
                for part in result['partitions']:
                    for nm in part['nm_ids']:lookup[nm]['imtID']=part['root']
        snapshot['verified_overlay_at']=state.get('finished_at')
    models=json.loads((OUT/'all_models.json').read_text());refs={r['external_code']:r for r in models if not r['gone_at']}
    cards=snapshot['cards'];byid={c['nmID']:c for c in cards};groups=defaultdict(list);parents={};own=defaultdict(list)
    colored_keys={normalized(r.get('united_title','')) for r in refs.values() if r.get('united_title') and laser(r) and color(r.get('color','')) in {'C','M','Y'}}
    for c in cards:
        groups[c['imtID']].append(c)
        if re.fullmatch(r'\d{4}',c['vendorCode']):parents[c['vendorCode']]=c
        if is_bundle(c):own[regroup.base(c)].append(c)
    def ref(c):return refs.get(regroup.base(c) or c['vendorCode'][:4],{})
    def kind(c):
        r=ref(c);typ=r.get('consumable_type','тип не подтверждён');assignment=str((r.get('raw') or {}).get('assignment',''))
        if typ in regroup.CARTRIDGES:
            if 'струйн' in assignment.lower():return 'Струйные картриджи'
            if laser(r):
                if normalized(r.get('united_title','')) in colored_keys:return 'Цветные лазерные картриджи'
                if any(ref(x).get('consumable_type') in {'Фотобарабан','Фотобарабан + картридж'} for x in groups[c['imtID']] if not is_bundle(x)):
                    return 'Тонер-картриджи с фотобарабаном/набором'
                return 'Одиночные лазерные картриджи'
        return typ
    x2_with=[];x2_without=[];full=[];leftover=[];all_bundle_groups=[];singles_full=[]
    for code,bb in sorted(own.items()):
        p=parents.get(code)
        if not p:continue
        for b in bb:
            if quantities(b)[3]==2:
                entry={'parent':plain_card(p),'bundle':plain_card(b),'type':kind(p),'group':b['imtID'],'members':len(groups[b['imtID']])}
                (x2_with if p['imtID']==b['imtID'] else x2_without).append(entry)
        q={quantities(b)[3] for b in bb}
        if {2,6,10}<=q and all(b['imtID']==p['imtID'] for b in bb):
            row={'parent':plain_card(p),'type':kind(p),'bundles':[plain_card(b) for b in bb],'quantities':sorted(q)}
            full.append(row)
            if ref(p).get('consumable_type') in regroup.CARTRIDGES:singles_full.append(row)
    for root,cc in groups.items():
        bb=[c for c in cc if is_bundle(c)]
        if len(cc)>1 and bb:all_bundle_groups.append({'root':root,'bundles':len(bb),'cards':len(cc)})
        if len(cc)>1 and len(bb)==len(cc) and len({regroup.base(c) for c in bb})==1 and {quantities(c)[3] for c in bb}=={6,10}:
            leftover.append({'anchor':plain_card(cc[0]),'root':root,'bundles':[plain_card(c) for c in cc],'type':kind(cc[0])})
    # Confirmed resource splits: before and after membership, not guessed from
    # the present layout. Repeated later revisions do not multiply SKU counts.
    events=confirmed_events();split_ids=set();split_ops=[];child_moved=set();timeline=defaultdict(list)
    child_ids={c['nmID'] for c in cards if re.match(r'^\d{5,}',c['vendorCode'])}
    for event in events:
        if event['action']=='glue':
            ids=(event.get('body') or {}).get('nmIDs',[]);child_moved.update(set(ids)&child_ids)
            for nm in ids:
                if nm in byid and is_bundle(byid[nm]):timeline[nm].append((event['ts'],True))
        else:
            pp=event['partitions']
            old_roots={nm:str(root) for root,ids in event.get('before',{}).items() for nm in ids}
            for part in pp:
                for nm in part['nm_ids']:
                    if nm in child_ids and old_roots.get(nm)!=str(part['root']):child_moved.add(nm)
                    if nm in byid and is_bundle(byid[nm]):timeline[nm].append((event['ts'],len(part['nm_ids'])>1))
            if any('ресурс' in rule.lower() for rule in event.get('rules',[])):
                members={n for p in pp for n in p['nm_ids']};affected=[]
                for nm in members:
                    p=byid.get(nm)
                    if not p or p['vendorCode'] not in parents or ref(p).get('consumable_type') not in regroup.CARTRIDGES:continue
                    part=next(x['nm_ids'] for x in pp if nm in x['nm_ids'])
                    if own[p['vendorCode']] and {b['nmID'] for b in own[p['vendorCode']]}<=set(part):affected.append(nm)
                split_ids.update(affected)
                split_ops.append({'at':event['ts'],'anchor':event['anchor'],'parents':[plain_card(byid[n]) for n in affected]})
    # Customer review rating (0..5), not content quality product_rating (0..10).
    funnel=db.query('''with totals as (select nm_id,sum(order_count) orders,sum(open_count) opens
                       from wb_funnel where account=%s and period>=%s group by nm_id), latest as (
                       select distinct on (nm_id) nm_id,feedback_rating rating,period,updated_at
                       from wb_funnel where account=%s order by nm_id,period desc,updated_at desc)
                       select t.*,l.rating,l.period rating_period,l.updated_at rating_observed_at
                       from totals t join latest l using(nm_id)''',('wb_acc1','2026-08-01','wb_acc1'))
    ratings={r['nm_id']:r for r in funnel};child_families=defaultdict(list)
    for nm in child_ids:child_families[byid[nm]['vendorCode'][:4]].append(byid[nm])
    leader_rows=[]
    for code,cc in sorted(child_families.items()):
        if len(cc)<2:continue
        rated=[c for c in cc if float(ratings.get(c['nmID'],{}).get('rating') or 0)>0]
        leader=max(rated,key=lambda c:(float(ratings[c['nmID']]['rating']),int(ratings[c['nmID']]['orders']),int(ratings[c['nmID']]['opens']))) if rated else None
        leader_rows.append({'code':code,'children':len(cc),'current_groups':len({c['imtID'] for c in cc}),
                            'leader':plain_card(leader) if leader else None,'rating':float(ratings[leader['nmID']]['rating']) if leader else None,
                            'rating_observed_at':str(ratings[leader['nmID']]['rating_observed_at']) if leader else None,
                            'members':[plain_card(c) for c in cc]})
    # WB-created primary cards and newly observed model references are different
    # novelty definitions. Both lists retain their actual evidence dates.
    primary_new=[];derivative_new=Counter();tc_new=[]
    for c in cards:
        created=str(c.get('createdAt',''))[:10]
        if '2026-09-01'<=created<='2026-10-11':
            if re.fullmatch(r'\d{4}',c['vendorCode']):primary_new.append({'card':plain_card(c),'created_at':c['createdAt'],'type':kind(c),'tc_first_seen':str(ref(c).get('first_seen',''))})
            else:derivative_new['бандлы' if is_bundle(c) else 'дочерние/другие']+=1
    for r in refs.values():
        first=str(r['first_seen'])[:10]
        if '2026-09-01'<=first<='2026-10-11':tc_new.append({'code':r['external_code'],'model':r['title'],'type':r['consumable_type'],'first_seen':str(r['first_seen']),
                                                                      'card':plain_card(parents[r['external_code']]) if r['external_code'] in parents else None})
    comp=json.loads((OUT/'ad_composition.json').read_text());all_ad=set();active_ad=set();active_cpc=set();ad_occ=Counter()
    for row in comp['campaigns'].values():
        all_ad.update(row['nms'])
        if row['status']==9:
            active_ad.update(row['nms']);ad_occ.update(row['nms'])
            if row['payment']=='cpc':active_cpc.update(row['nms'])
    ad_report={'read_at':comp['read_at'],'campaigns':len(comp['campaigns']),'all_nonarchive_skus':len(all_ad&set(byid)),
               'active_skus':len(active_ad&set(byid)),'active_cpc_skus':len(active_cpc&set(byid)),
               'active_duplicate_skus':sum(nm in byid and n>1 for nm,n in ad_occ.items()),
               'outside_ad':len(set(byid)-all_ad),'coverage_pct':round(100*len(all_ad&set(byid))/len(byid),2)}
    # Finance sale dates and order dates: only actual Sale/Sale lines. Packaging
    # quantity is commercial units, not multiplied cartridge contents.
    bundle_ids={c['nmID'] for bb in own.values() for c in bb}
    financial=db.query('''select rrd_id,payload->>'nm_id' nm_id,payload->>'sa_name' code,payload->>'order_dt' order_at,
                          payload->>'sale_dt' sale_at,payload->>'quantity' qty,payload->>'retail_price_withdisc_rub' revenue,
                          payload->>'srid' srid from raw_wb_report where account=%s
                          and payload->>'doc_type_name'='Продажа' and payload->>'supplier_oper_name'='Продажа'
                          and payload->>'sale_dt'>=%s and (payload->>'nm_id')::bigint=any(%s)''',('wb_acc1','2026-09-01',list(bundle_ids)))
    sold=[];after=[];unknown_time=[];seen=set()
    for row in financial:
        nm=int(row['nm_id']);key=(nm,row['srid'] or str(row['rrd_id']))
        if key in seen:continue
        seen.add(key);item={**row,'card':plain_card(byid[nm]),'currently_joined':len(groups[byid[nm]['imtID']])>1}
        sold.append(item)
        try:
            order=dt.datetime.fromisoformat(row['order_at'].replace('Z','+00:00'))
            if order.tzinfo is None:raise ValueError('order timezone missing')
            eligible=[(dt.datetime.fromisoformat(ts),state) for ts,state in timeline[nm] if dt.datetime.fromisoformat(ts)<=order]
            if eligible and eligible[-1][1]:after.append(item)
        except (ValueError,TypeError):unknown_time.append(item)
    drum_data=drum_gaps(cards,refs)
    summary={'account':'wb_acc1','catalog_read_at':snapshot['read_at'],'verified_overlay_at':snapshot.get('verified_overlay_at'),'created_at':now(),'assortment_skus':len(cards),'bundle_cards':len(bundle_ids),
             'x2_with_parent':len(x2_with),'x2_without_parent':len(x2_without),'x2_with_parent_by_type':dict(Counter(r['type'] for r in x2_with)),
             'parents_all_bundles':len(full),'cartridge_parents_all_bundles':len(singles_full),'full_by_type':dict(Counter(r['type'] for r in full)),
             'resource_split_parent_skus':len(split_ids),'resource_split_operations':len(split_ops),
             'x6_x10_only_groups':len(leftover),'x6_x10_only_bundle_skus':sum(len(r['bundles']) for r in leftover),
             'refill_kit_full':sum(r['type']=='Заправочный комплект' for r in full),'toner_refill_full':sum(r['type']=='Тонер' for r in full),
             'refill_kit_primary_skus':sum(ref(p).get('consumable_type')=='Заправочный комплект' for p in parents.values()),
             'refill_kit_parents_with_bundles':sum(ref(p).get('consumable_type')=='Заправочный комплект' and bool(own[p['vendorCode']]) for p in parents.values()),
             'printer_families':len(leader_rows),'printer_families_with_leader':sum(bool(r['leader']) for r in leader_rows),'printer_child_skus_moved':len(child_moved),
             'new_wb_primary_cards':len(primary_new),'new_tc_model_refs':len(tc_new),'new_derivative_cards':dict(derivative_new),
             'advertising':ad_report,'bundle_groups':len(all_bundle_groups),'bundle_skus_joined':sum(g['bundles'] for g in all_bundle_groups),
             'bundle_sales_since_sep1':len(sold),'bundle_orders_after_verified_glue':len(after),'unverifiable_order_time':len(unknown_time),
             'drums':drum_data['summary']}
    details={'x2_with_parent':x2_with,'x2_without_parent':x2_without,'full_families':full,'resource_splits':split_ops,'x6_x10_groups':leftover,
             'printer_leaders':leader_rows,'new_primary':sorted(primary_new,key=lambda r:r['created_at']),'new_tc_models':tc_new,
             'bundle_sales':sold,'bundle_sales_after_glue':after,'drum_gaps':drum_data}
    regroup.save(OUT/'summary.json',summary);regroup.save(OUT/'details.json',details)
    print(json.dumps(summary,ensure_ascii=False),flush=True)


def drum_gaps(cards,refs):
    parents={c['vendorCode']:c for c in cards if re.fullmatch(r'\d{4}',c['vendorCode'])};groups=defaultdict(list)
    for c in cards:groups[c['imtID']].append(c)
    def role(c):return refs.get(c['vendorCode'],{}).get('consumable_type')
    mixed=[];excluded=[]
    for root,cc in groups.items():
        core_cards=[c for c in cc if c['vendorCode'] in parents]
        drums=[c for c in core_cards if role(c)=='Фотобарабан'];cart=[c for c in core_cards if role(c) in regroup.CARTRIDGES]
        if any(role(c)=='Комплект фотобарабанов' or (role(c)=='Фотобарабан' and regroup.colored_card(c)) for c in core_cards):
            excluded.append(root);continue
        if not drums or not cart:continue
        kit=[];ambiguous=[]
        for c in core_cards:
            if role(c)=='Фотобарабан + картридж':kit.append(c);continue
            if role(c)=='Комплект картриджей':
                toks=core.tokens(refs[c['vendorCode']]['title']) or []
                if any(core.model_matches(t,d,refs[d['vendorCode']]) for t in toks for d in drums) and any(core.model_matches(t,p,refs[p['vendorCode']]) for t in toks for p in cart):kit.append(c)
                else:ambiguous.append(c)
        mixed.append({'root':root,'drums':[plain_card(c) for c in drums],'cartridges':[plain_card(c) for c in cart],
                      'kits':[plain_card(c) for c in kit],'other_sets':[plain_card(c) for c in ambiguous],
                      'kit_missing':not kit and not ambiguous,'needs_set_content_check':not kit and bool(ambiguous)})
    pair_rows=[]
    individual=[(c,refs.get(code,{})) for code,c in parents.items() if refs.get(code,{}).get('consumable_type') in regroup.CARTRIDGES|{'Фотобарабан'}]
    for ref in refs.values():
        if ref['consumable_type']!='Фотобарабан + картридж':continue
        tokens=core.tokens(ref['title']) or [];found=[];missing=[]
        if '+' not in ref['title']:continue
        for token in tokens:
            matches=[c for c,r in individual if core.model_matches(token,c,r)]
            found.extend(matches)
            if not matches:missing.append(token)
        kit=parents.get(ref['external_code']);found=list({c['nmID']:c for c in found}.values())
        if any(role(c)=='Фотобарабан' and regroup.colored_card(c) for c in found):continue
        if not found and not kit:continue
        nodes=[*found,*([kit] if kit else [])]
        prs={core.printers(c,refs[c['vendorCode']])[1] for c in nodes}
        joined=len({c['imtID'] for c in nodes})==1 and not missing and kit is not None
        if not joined:
            pair_rows.append({'kit_model':ref['title'],'kit_code':ref['external_code'],'kit':plain_card(kit) if kit else None,
                              'components':[plain_card(c) for c in found],'missing_models':missing,'kit_missing':kit is None,
                              'groups':sorted({c['imtID'] for c in nodes}),'full_printers_match':len(prs)==1 and bool(next(iter(prs))),
                              'ambiguity':len(found)>len(tokens)})
    return {'summary':{'existing_pair_groups':len(mixed),'existing_pair_groups_without_kit':sum(r['kit_missing'] for r in mixed),
                       'existing_pair_groups_set_ambiguous':sum(r['needs_set_content_check'] for r in mixed),
                       'tc_pair_cases_not_complete':len(pair_rows),'tc_pairs_missing_kit_card':sum(r['kit_missing'] for r in pair_rows),
                       'tc_pairs_missing_component_models':sum(bool(r['missing_models']) for r in pair_rows),
                       'current_groups_in_tc_gap_cases':len({root for r in pair_rows for root in r['groups']}),
                       'coloured_drum_group_exclusions':len(excluded)},'mixed_groups':mixed,'pair_cases':pair_rows}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--collect',action='store_true');parser.add_argument('--ads',action='store_true');parser.add_argument('--analyse',action='store_true');args=parser.parse_args()
    if args.collect:collect()
    elif args.ads:collect_ads()
    elif args.analyse:analyse()
    else:raise SystemExit('Choose --collect, --ads or --analyse')


if __name__=='__main__':main()
