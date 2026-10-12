#!/usr/bin/env python3
# поток: mkt
"""Conservative colour-laser gap map using explicit TC series and full printers.

No fuzzy model or half-printer matching. Only lossless, disjoint, wholly owned
colour groups become executable; missing inventory is a content-manager list.
"""
from collections import defaultdict,Counter
import hashlib
import json
import re

from ops import wb_bundle_regroup as regroup,wb_core_glue as core
from ops.wb_bundle_plan import char,color,is_bundle,normalized,quantities,fingerprint
from ops.wb_bundle_glue_singletons import now


def laser(ref):return 'лазер' in str((ref.get('raw') or {}).get('assignment','')).lower()


def plain_card(c):
    return {'nm_id':c['nmID'],'code':c['vendorCode'],'title':c['title'],'url':f'https://www.wildberries.ru/catalog/{c["nmID"]}/detail.aspx','imt':c['imtID']}


def scan(snapshot,source,models):
    cards=snapshot['cards'];byid={c['nmID']:c for c in cards};parents={};groups=defaultdict(list);own=defaultdict(list)
    for c in cards:
        groups[c['imtID']].append(c)
        if re.fullmatch(r'\d{4}',c['vendorCode']):parents[c['vendorCode']]=c
        if is_bundle(c):own[regroup.base(c)].append(c)
    refs={r['external_code']:r for r in models if not r.get('gone_at')}
    series=defaultdict(list);unresolved=[]
    for ref in refs.values():
        if ref['consumable_type'] not in regroup.CARTRIDGES|{'Комплект картриджей'} or not laser(ref):continue
        if not ref.get('united_title'):
            if ref['consumable_type'] in regroup.CARTRIDGES and color(ref.get('color','')) in {'C','M','Y'}:
                unresolved.append({'code':ref['external_code'],'model':ref['title'],'reason':'В ТК не задано семейство модели; нельзя объединять разные модели только по одному принтеру'})
            continue
        # Full explicit TC printer list; WB lists are separately compared below.
        pr=core.printers({'characteristics':[]},ref)[1]
        if not pr:continue
        series[(normalized(ref['united_title']),pr)].append(ref)
    records=[];proposals=[];create=[]
    for (family,pr),rr in sorted(series.items()):
        singles=[r for r in rr if r['consumable_type'] in regroup.CARTRIDGES]
        if not any(color(r.get('color','')) in {'C','M','Y'} for r in singles):continue
        kits=[r for r in rr if r['consumable_type']=='Комплект картриджей']
        explicit=[]
        for r in singles+kits:
            c=parents.get(r['external_code'])
            chip,_=regroup.chip_info(c or {'characteristics':[]},own[r['external_code']],r,source['marks'].get(r['external_code'],[]))
            explicit.append((r,chip))
        known={cl for _,cl in explicit if cl not in {regroup.UNKNOWN,'противоречие'}}
        variants=sorted(known) or [regroup.UNKNOWN]
        for variant in variants:
            eligible=[r for r,cl in explicit if cl==variant or (cl==regroup.UNKNOWN and known=={'с чипом'})]
            available_kits=[r for r in eligible if r['consumable_type']=='Комплект картриджей']
            comps=[r for r in eligible if r['consumable_type'] in regroup.CARTRIDGES]
            # A known kit's literal model composition separates an XL/standard
            # sibling inside a broad TC united-title family. Never strip XL/H.
            if len(available_kits)==1:
                toks=core.tokens(available_kits[0]['title']) or []
                matched=[r for r in comps if any(core.model_matches(t,parents.get(r['external_code'],{'characteristics':[{'id':58854,'value':[r.get('color','')]}]}),r) for t in toks)]
                if {color(r.get('color','')) for r in matched}>={'C','M','Y'}:comps=matched
            buckets=defaultdict(list)
            for r in comps:
                co=color(r.get('color',''))
                if co in {'K','C','M','Y'}:buckets[co].append(r)
            if not (set(buckets)&{'C','M','Y'}):continue
            reasons=[];expected={};found=[];missing=[]
            for co in ['K','C','M','Y']:
                candidates=buckets.get(co,[])
                if len(candidates)>1:
                    reasons.append('Для цвета '+co+' несколько моделей/ресурсов: '+', '.join(r['external_code']+' '+r['title'] for r in candidates));continue
                if not candidates:
                    alternatives=[r for r in singles if color(r.get('color',''))==co and r['external_code'] in parents]
                    if alternatives:
                        reasons.append('Цвет '+co+' есть на WB, но чип/исполнение не подтверждены для этого варианта')
                        missing.append({'color':co,'code':None,'model':None,'needs_creation':False,
                                        'existing_alternatives':[plain_card(parents[r['external_code']]) for r in alternatives],
                                        'why':'Цвет есть в ассортименте; нужна проверка чипа/исполнения, а не создание дубликата'})
                    else:
                        missing.append({'color':co,'code':None,'model':None,'needs_creation':True,'why':'Точной модели цвета нет в подтверждённом семействе ТК'})
                    continue
                ref=candidates[0];expected[co]=ref;c=parents.get(ref['external_code'])
                if not c:
                    missing.append({'color':co,'code':ref['external_code'],'model':ref['title'],'needs_creation':True,'why':'Основной четырёхзначной карточки нет на WB acc1'});continue
                found.append(c)
                if normalized(char(c,5023))!=normalized(ref['title']):reasons.append(ref['external_code']+': модель WB отличается от ТК')
                if color(char(c,58854))!=co:reasons.append(ref['external_code']+': цвет WB расходится с ТК')
                cl,_=regroup.chip_info(c,own[ref['external_code']],ref,source['marks'].get(ref['external_code'],[]))
                if cl=='противоречие':reasons.append(ref['external_code']+': явное противоречие чипа')
            if len(available_kits)>1:reasons.append('Несколько наборов одной серии: проверить конкретное содержимое и исполнение')
            kitref=available_kits[0] if len(available_kits)==1 else None
            kit=parents.get(kitref['external_code']) if kitref else None
            other_kits=[parents[r['external_code']] for r in kits if r['external_code'] in parents]
            if kit is None and other_kits:reasons.append('Набор есть в ассортименте, но его чип/вариант или состав не подтверждён; не создавать дубликат')
            if kit:
                found.append(kit)
                if normalized(char(kit,5023))!=normalized(kitref['title']):reasons.append('Модель набора WB отличается от ТК')
                if int(quantities(kit)[1] or 0)!=4 or str((kitref.get('raw') or {}).get('colors_count'))!='4':reasons.append('Содержимое набора не подтверждено как четыре одиночных CMYK картриджа')
                if own[kitref['external_code']]:reasons.append('Есть бандлы самого набора: содержимое требует отдельного разбора')
            identity=family+'|'+variant+'|'+';'.join(pr)
            wb_lists={core.printers(c,refs[c['vendorCode']])[1] for c in found}
            if len(wb_lists)>1:reasons.append('Полные списки принтеров WB у компонентов/набора различаются; нужно сверить конкретные отличия')
            if any(not char(c,179078) for c in found):reasons.append('У компонента/набора WB пустой список принтеров')
            target_pr=next(iter(wb_lists)) if len(wb_lists)==1 else pr
            item={'family':next(r['united_title'] for r in rr),'chip':variant,'printers':list(pr),
                  'components':[{'color':co,'code':r['external_code'],'model':r['title'],'card':plain_card(parents[r['external_code']]) if r['external_code'] in parents else None} for co,r in expected.items()],
                  'missing_colors':missing,'kit':plain_card(kit) if kit else None,'kit_code':kitref['external_code'] if kitref else None,
                  'kit_missing':kit is None and len(available_kits)<2 and not other_kits,
                  'other_kits':[plain_card(c) for c in other_kits if c is not kit],'reasons':reasons,'identity':identity}
            roots={c['imtID'] for c in found};item['current_groups']=sorted(roots)
            item['needs_join']=len(roots)>1
            # Report only incomplete compositions / absent kit / unresolved
            # counterparts, while preserving a complete census separately.
            item['printers_wb']=list(target_pr)
            if missing or item['kit_missing'] or len(roots)>1:records.append(item)
            create_missing=[m for m in missing if m['needs_creation']]
            if create_missing or item['kit_missing']:
                create.append({'family':item['family'],'chip':variant,'printers':list(pr),'missing_colors':create_missing,
                               'kit_missing':item['kit_missing'],'kit_code':item['kit_code'],'components':item['components'],
                               'caution':'Проверить намеренное удаление основной карточки перед повторным заведением' if any(m['code'] in {'1032','1719','2446','2525','2811','3997','4007','4257','5772','6121','6185','6825','6889','6902','6938','7108'} for m in missing) else ''})
            if reasons or len(found)<2 or not item['needs_join']:continue
            codes={c['vendorCode'] for c in found};touched=set(roots)
            for c in found:
                touched.update(b['imtID'] for b in own[c['vendorCode']])
                if c is not kit:
                    ref=refs[c['vendorCode']];cl,_=regroup.chip_info(c,own[c['vendorCode']],ref,source['marks'].get(c['vendorCode'],[]))
                    reasons.extend(regroup.reasons(c,own[c['vendorCode']],ref,cl))
            whole=[c for rt in touched for c in groups[rt]]
            core_ids={c['nmID'] for c in found}
            if any((not is_bundle(c) and c['nmID'] not in core_ids) or (is_bundle(c) and regroup.base(c) not in codes) for c in whole):
                reasons.append('В исходной склейке есть другие товары: перенос без разбора всей группы запрещён')
            if reasons:continue
            main=set(core_ids);left=defaultdict(list)
            for c in whole:
                if not is_bundle(c):continue
                if quantities(c)[3]==2:main.add(c['nmID'])
                else:left[regroup.base(c)].append(c['nmID'])
            parts=[sorted(main),*[sorted(ids) for ids in left.values()]]
            if max(map(len,parts))>30:reasons.append('Превышение лимита 30');continue
            anchor=next((c for c in found if c is kit),found[0]);ident=int.from_bytes(hashlib.blake2b(identity.encode(),digest_size=8).digest(),'big')
            op={'id':ident,'anchor':anchor['nmID'],'title':item['family'],'url':plain_card(anchor)['url'],
                'rules':['доклеить точное цветное лазерное семейство, набор и свои X2; X6/X10 отдельно'],
                'before_cards':whole,'before_groups':{str(rt):sorted(c['nmID'] for c in groups[rt]) for rt in touched},
                'before_fingerprint':fingerprint(whole),'partitions':parts,'preferred_roots':{str(anchor['imtID']):anchor['nmID']}}
            regroup.validate(op);proposals.append(op);item['operation_id']=ident
    # Family descriptors can overlap; never execute competing plans.
    owners=defaultdict(list)
    for op in proposals:
        for c in op['before_cards']:owners[c['nmID']].append(op['id'])
    overlap={ident for ids in owners.values() if len(ids)>1 for ident in ids}
    for r in records:
        if r.get('operation_id') in overlap:r['reasons'].append('Компоненты участвуют в нескольких вариантах семьи: ручной выбор');r.pop('operation_id')
    return {'account':'wb_acc1','prepared_at':now(),'catalog_read_at':snapshot['read_at'],
            'records':records,'create':create,'unresolved_family_codes':unresolved,
            'operations':[o for o in proposals if o['id'] not in overlap]}


if __name__=='__main__':
    root=regroup.ROOT/'docs/reports/mkt_wb_glue_status_2026-10-12_acc1'
    snapshot=json.loads((root/'catalog.json').read_text());source=json.loads((root/'references.json').read_text());models=json.loads((root/'all_models.json').read_text())
    plan=scan(snapshot,source,models);out=root/'laser_gaps';out.mkdir(exist_ok=True);regroup.save(out/'plan.json',plan)
    print('Laser families needing review',len(plan['records']),'create entries',len(plan['create']),'safe joins',len(plan['operations']),'undefined series',len(plan['unresolved_family_codes']))
