#!/usr/bin/env python3
# поток: mkt
"""Find missing acc1 color/kit-component groups, independently of bundles.

Exact kit models, whole printer lists and explicit chip classes are required.
Current good groups are preserved; foreign members and ambiguous matches go
to review. Writes use the verified lossless executor, never blind retries.
"""
import argparse
from collections import Counter, defaultdict
import csv
import fcntl
import html
import json
from pathlib import Path
import re
import time

from ops import wb_bundle_regroup as regroup
from ops.wb_bundle_plan import char, color, fetch_catalog, fingerprint, is_bundle, normalized, quantities
from ops.wb_bundle_glue_singletons import Content, compatible_values, now, read_references

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'docs/reports/mkt_wb_core_glue_2026-10-10_acc1'
BUNDLE_OUT=regroup.OUT
KIT_TYPES={'Комплект картриджей','Комплект чернил','Фотобарабан + картридж'}


def tokens(model):
    """Expand only explicit, short numeric ranges; never fuzzy model prefixes."""
    model=str(model or '').strip()
    if not model: return None
    if '+' in model:
        parts=model.split('+')
        expanded=[tokens(p) for p in parts]
        return [m for pp in expanded for m in pp] if all(expanded) else None
    match=re.fullmatch(r'\s*(.+?)\s*[-–]\s*(.+?)\s*',model)
    if match:
        a,b=match.groups()
        first=re.fullmatch(r'(.*?)(\d+)([^\d]*)',a)
        second=re.fullmatch(r'(.*?)(\d+)([^\d]*)',b)
        if first and second:
            prefix,start,suffix=first.groups();prefix2,end,suffix2=second.groups()
            if normalized(prefix)==normalized(prefix2) and normalized(suffix)==normalized(suffix2) and len(start)==len(end):
                lo,hi=sorted((int(start),int(end)))
                if 1 <= hi-lo <= 8:
                    return [normalized(prefix+str(i).zfill(len(start))+suffix) for i in range(lo,hi+1)]
    # A dash inside one model is not a range (CLI-521, GI-490, DR-2175).
    return [normalized(model)]


def printers(card,ref):
    values=compatible_values(card,179078)
    if values: return ('wb',values)
    raw=(ref.get('raw') or {}).get('printer_models')
    if isinstance(raw,list): values=tuple(sorted(set(normalized(x) for x in raw if x)))
    elif isinstance(raw,str): values=tuple(sorted(set(normalized(x.strip()) for x in re.split('[;,\n]',raw) if x.strip())))
    else: values=()
    return ('tc',values)


def model_matches(token,c,ref):
    title=normalized(ref.get('title',''))
    if token==title: return True
    # Drum kits often spell a role suffix absent from the separate drum model.
    if ref.get('consumable_type')=='Фотобарабан' and token.endswith('DU'):
        return token[:-2]==title
    # A kit named for a family may omit only an explicit color suffix. Do not
    # erase XL/H/resource letters or arbitrary model digits to make it fit.
    if title.startswith(token):
        suffix=title[len(token):]
        actual=color(char(c,58854))
        suffix_colors={'K':{'K'},'BK':{'K'},'C':{'C'},'M':{'M'},'Y':{'Y'},
                       'LC':{'LC'},'LM':{'LM'},'PBK':{'PBK'},'PGBK':{'K','PBK'},'MBK':{'MBK'}}
        return suffix in suffix_colors and actual in suffix_colors[suffix]
    return False


def components_for(kit,records):
    kref=kit['ref'];typ=kref['consumable_type'];model=kref.get('title','')
    if typ=='Фотобарабан + картридж' and '+' not in model:
        return [],'В модели набора не перечислены оба компонента'
    requested=tokens(model)
    if not requested: return [],'Состав набора не удалось прочитать точно'
    compatible=[]
    for rec in records:
        typ2=rec['ref'].get('consumable_type')
        allowed={'Фотобарабан','Картридж','Тонер-картридж'} if typ=='Фотобарабан + картридж' else {'Чернила'} if typ=='Комплект чернил' else {'Картридж','Тонер-картридж'}
        if typ2 not in allowed or rec['card']['subjectID']!=kit['card']['subjectID']:continue
        # Both objects must use the same, full compatibility source and values.
        if not kit['printers'][1] or rec['printers']!=kit['printers']:continue
        if not any(model_matches(t,rec['card'],rec['ref']) for t in requested):continue
        compatible.append(rec)
    selected=[]
    for token in requested:
        candidates=[r for r in compatible if model_matches(token,r['card'],r['ref'])]
        if not candidates:return [],'Не найден точный компонент '+token+' с тем же полным списком принтеров'
        # Chip restrictions apply to cartridges, including unknown vs known.
        candidates=[r for r in candidates if r['ref']['consumable_type']=='Фотобарабан' or r['chip']==kit['chip']]
        if not candidates:return [],'Компонент '+token+': другой или неизвестный относительно набора чип'
        buckets=defaultdict(list)
        for r in candidates:
            buckets[color(char(r['card'],58854))].append(r)
        if any(len(v)>1 for v in buckets.values()):return [],'Несколько исполнений точной модели '+token+' одного цвета'
        selected.extend(candidates)
    unique={r['card']['nmID']:r for r in selected}
    selected=list(unique.values())
    if typ=='Фотобарабан + картридж':
        roles={r['ref']['consumable_type'] for r in selected}
        if 'Фотобарабан' not in roles or not (roles & regroup.CARTRIDGES):return [],'Не найдены одновременно барабан и тонер-картридж'
    else:
        colors=[color(char(r['card'],58854)) for r in selected]
        if len(selected)<2 or len(set(colors))!=len(colors):return [],'Цветной состав неполный или дублируются цвета'
        if set(colors) & {'C','M','Y'} and not {'C','M','Y'} <= set(colors):
            return [],'Не найдены все C/M/Y компоненты цветного набора'
        expected=(kref.get('raw') or {}).get('colors_count')
        if str(expected or '').isdigit() and int(expected)>0 and len(colors)!=int(expected):
            return [],'Число цветов не соответствует подтверждённому составу набора'
        if not any(c in {'C','M','Y','CMY','CMYK','PBK','MBK','LC','LM','R','G','B'} or regroup.colored_card(r['card']) for c,r in zip(colors,selected)):
            return [],'Набор не подтверждён как цветная линейка'
    return selected,None


def prepare(snapshot,source):
    groups=defaultdict(list);own=defaultdict(list);parents={}
    for c in snapshot['cards']:
        groups[c['imtID']].append(c)
        if is_bundle(c):own[regroup.base(c)].append(c)
        elif re.fullmatch(r'\d{4}',c.get('vendorCode','')):parents[c['vendorCode']]=c
    records=[];review=[]
    for code,c in parents.items():
        ref=source['references'].get(code,{})
        chip,evidence=regroup.chip_info(c,own[code],ref,source['marks'].get(code,[]))
        if not ref or normalized(char(c,5023))!=normalized(ref.get('title','')):
            if ref.get('consumable_type') in KIT_TYPES:review.append({'anchor':c['nmID'],'title':c['title'],'type':ref['consumable_type'],'reason':'Модель набора WB не совпадает с ТК'})
            continue
        if chip=='противоречие':
            if ref.get('consumable_type') in KIT_TYPES:review.append({'anchor':c['nmID'],'title':c['title'],'type':ref['consumable_type'],'reason':'Противоречие явных сведений о чипе набора'})
            continue
        records.append({'card':c,'ref':ref,'chip':chip,'evidence':evidence,'printers':printers(c,ref)})
    proposals=[];same=Counter();bytype=Counter()
    for kit in records:
        typ=kit['ref']['consumable_type']
        if typ not in KIT_TYPES:continue
        members,reason=components_for(kit,records)
        if reason:
            review.append({'anchor':kit['card']['nmID'],'title':kit['card']['title'],'type':typ,'reason':reason});continue
        cards=[kit['card'],*(r['card'] for r in members)]
        if len({c['imtID'] for c in cards})==1:
            same[typ]+=1;continue
        core_ids={c['nmID'] for c in cards}
        touched={c['imtID'] for c in cards}
        all_cards=[c for imt in touched for c in groups[imt]]
        foreign=[c for c in all_cards if not is_bundle(c) and c['nmID'] not in core_ids]
        if foreign:
            review.append({'anchor':kit['card']['nmID'],'title':kit['card']['title'],'type':typ,'reason':'В исходной группе есть другие одиночные/наборы; сохранить и разобрать весь состав','foreign':[c['nmID'] for c in foreign]});continue
        all_codes={regroup.base(c) for c in cards}
        if any(regroup.base(c) not in all_codes for c in all_cards):
            review.append({'anchor':kit['card']['nmID'],'title':kit['card']['title'],'type':typ,'reason':'В исходной группе чужое семейство бандлов'});continue
        # Include all quantities of each exact family, even if currently separate.
        for c in cards:
            for b in own[regroup.base(c)]:touched.add(b['imtID'])
        all_cards=[c for imt in touched for c in groups[imt]]
        if any(regroup.base(c) not in all_codes or (not is_bundle(c) and c['nmID'] not in core_ids) for c in all_cards):
            review.append({'anchor':kit['card']['nmID'],'title':kit['card']['title'],'type':typ,'reason':'Бандл стоит с другими товарами; перенос потребует отдельного разбора'});continue
        bad=[]
        for c in cards:
            code=regroup.base(c);ref=source['references'].get(code,{})
            # Bundles of kits are never certified by title quantity alone.
            if c['nmID']==kit['card']['nmID'] and own[code]:bad.append('Бандл набора требует проверки содержимого');continue
            cl,_=regroup.chip_info(c,own[code],ref,source['marks'].get(code,[]))
            bad.extend(regroup.reasons(c,own[code],ref,cl))
        if bad:
            review.append({'anchor':kit['card']['nmID'],'title':kit['card']['title'],'type':typ,'reason':'; '.join(sorted(set(bad)))});continue
        main=set(core_ids);leftovers=defaultdict(list)
        for b in all_cards:
            if not is_bundle(b):continue
            rec=next(r for r in members if regroup.base(r['card'])==regroup.base(b))
            if quantities(b)[3]==2 and (typ!='Фотобарабан + картридж' or rec['ref']['consumable_type'] in regroup.CARTRIDGES):main.add(b['nmID'])
            else:leftovers[regroup.base(b)].append(b['nmID'])
        parts=[sorted(main),*(sorted(v) for v in leftovers.values())]
        if any(len(p)>30 for p in parts):
            review.append({'anchor':kit['card']['nmID'],'title':kit['card']['title'],'type':typ,'reason':'Лимит 30 карточек'});continue
        anchor=kit['card']
        op={'id':anchor['nmID'],'anchor':anchor['nmID'],'title':anchor['title'],'url':f'https://www.wildberries.ru/catalog/{anchor["nmID"]}/detail.aspx',
            'type':typ,'component_ids':sorted(core_ids),'chip':kit['chip'],'rules':['создать отсутствующую склейку: '+typ],
            'before_groups':{str(imt):sorted(c['nmID'] for c in groups[imt]) for imt in touched},
            'before_cards':all_cards,'before_fingerprint':fingerprint(all_cards),'partitions':parts,
            'preferred_roots':{str(anchor['imtID']):anchor['nmID']}}
        regroup.validate(op);proposals.append(op);bytype[typ]+=1
    # No two automatic operations may touch the same existing group or card.
    owners=defaultdict(list)
    for op in proposals:
        for nm in (c['nmID'] for c in op['before_cards']):owners[nm].append(op['id'])
    conflict={x for ids in owners.values() if len(ids)>1 for x in ids}
    operations=[]
    for op in proposals:
        if op['id'] in conflict:review.append({'anchor':op['anchor'],'title':op['title'],'type':op['type'],'reason':'Несколько наборов претендуют на общие компоненты: ручная проверка всей семьи'})
        else:operations.append(op)
    return {'account':'wb_acc1','prepared_at':now(),'catalog_read_at':snapshot['read_at'],'operations':operations,'review':review,'already_complete':dict(same),
            'ready_by_type':dict(Counter(o['type'] for o in operations)),'all_parent_records':len(records)}


def report(plan,state):
    OUT.mkdir(parents=True,exist_ok=True)
    done={r['id']:r for r in state.get('results',[])}
    for name,rows in [('operations',plan['operations']),('manual_review',plan['review'])]:
        projected=[{'nm_id':r['anchor'],'title':r['title'],'type':r['type'],'url':f'https://www.wildberries.ru/catalog/{r["anchor"]}/detail.aspx',
                    'status':done.get(r.get('id'),{}).get('status','prepared') if name=='operations' else 'manual',
                    'reason':r.get('reason',''),'component_ids':';'.join(map(str,r.get('component_ids',[])))} for r in rows]
        with (OUT/f'{name}.csv').open('w') as f:
            w=csv.DictWriter(f,fieldnames=['nm_id','title','type','url','status','reason','component_ids']);w.writeheader();w.writerows(projected)
    body=[]
    for op in plan['operations']:
        fields={c['nmID']:c for c in op['before_cards']}
        parts=''.join('<li>'+html.escape('; '.join(fields[n]['vendorCode']+' '+fields[n]['title'] for n in p))+'</li>' for p in op['partitions'])
        body.append(f'<details><summary><a href="{op["url"]}" target="_blank">{op["anchor"]} {html.escape(op["title"])}</a> — {done.get(op["id"],{}).get("status","prepared")}</summary><p>{html.escape(op["chip"])}</p><ol>{parts}</ol></details>')
    reviews=''.join(f'<li><a href="https://www.wildberries.ru/catalog/{r["anchor"]}/detail.aspx">{r["anchor"]} {html.escape(r["title"])}</a>: {html.escape(r["reason"])}</li>' for r in plan['review'])
    (OUT/'index.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><title>Недостающие склейки acc1</title><style>body{max-width:1100px;margin:30px auto;padding:20px;font:17px system-ui;line-height:1.5}details{padding:12px;border-bottom:1px solid #ddd}a{color:#14615c}</style><h1>Недостающие склейки цветов и компонентов — acc1</h1><p>Каталог '+html.escape(plan['catalog_read_at'])+'. Подготовлено '+str(len(plan['operations']))+'; подтверждено '+str(sum(r['status']=='confirmed' for r in done.values()))+'. Бандлы не обязательны для поиска.</p><p>Готово по видам: '+html.escape(str(plan['ready_by_type']))+'</p>'+''.join(body)+'<h2>Ручная проверка</h2><ul>'+reviews+'</ul></html>')


def wait_previous():
    # Hold the same lock for the whole new pass, so the old executor cannot resume.
    lock=(BUNDLE_OUT/'apply.lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX)
    state=json.loads((BUNDLE_OUT/'apply_results.json').read_text())
    if state.get('stopped_at') or not state.get('finished'):
        raise RuntimeError('Previous bundle pass is not finished/confirmed; no new grouping writes')
    return lock


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--prepare',action='store_true');ap.add_argument('--apply',action='store_true')
    ap.add_argument('--fresh',action='store_true');ap.add_argument('--after-bundles',action='store_true');ap.add_argument('--disjoint-bundle-plan',action='store_true')
    ap.add_argument('--limit',type=int);ap.add_argument('--ids',type=int,nargs='*');a=ap.parse_args()
    OUT.mkdir(parents=True,exist_ok=True)
    previous_lock=wait_previous() if a.after_bundles else None
    own_lock=(OUT/'apply.lock').open('a');fcntl.flock(own_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    path=OUT/'apply_results.json';state=json.loads(path.read_text()) if path.exists() else {'results':[]}
    if state.get('stopped_at'):raise RuntimeError('Uncertain previous core write requires independent reconciliation')
    if a.fresh:
        snapshot={'read_at':now(),'cards':fetch_catalog('wb_acc1')}
        refs,marks=read_references({c.get('vendorCode','')[:4] for c in snapshot['cards']})
        source={'references':refs,'marks':{k:sorted(v) for k,v in marks.items()}}
        regroup.save(OUT/'catalog.json',snapshot);regroup.save(OUT/'references.json',source)
    else:
        snapshot=json.loads(((OUT if (OUT/'catalog.json').exists() else BUNDLE_OUT)/'catalog.json').read_text())
        source=json.loads(((OUT if (OUT/'references.json').exists() else BUNDLE_OUT)/'references.json').read_text())
    if a.prepare or a.fresh or not (OUT/'plan.json').exists():
        plan=prepare(snapshot,source);regroup.save(OUT/'plan.json',plan)
    else:plan=json.loads((OUT/'plan.json').read_text())
    report(plan,state)
    print('Ready:',len(plan['operations']),plan['ready_by_type'],'Already complete:',plan['already_complete'],'Review:',len(plan['review']),flush=True)
    if not a.apply:return
    if not a.after_bundles and not a.disjoint_bundle_plan:raise RuntimeError('Apply needs exclusive after-bundles run or proven disjoint scope')
    forbidden=set()
    if a.disjoint_bundle_plan:
        old=json.loads((BUNDLE_OUT/'plan.json').read_text())
        forbidden={c['nmID'] for op in old['operations'] for c in op['before_cards']}
    regroup.OUT=OUT
    client=Content();done={r['id'] for r in state['results'] if r['status']=='confirmed'};count=0
    for op in plan['operations']:
        if op['id'] in done or (a.ids and op['id'] not in a.ids):continue
        if forbidden & {c['nmID'] for c in op['before_cards']}:continue
        if a.limit is not None and count>=a.limit:break
        try:row=regroup.execute(op,client)
        except Exception as exc:
            state.update(stopped_at=op['id'],reason=str(exc));regroup.save(path,state);report(plan,state);raise
        state['results'].append(row);regroup.save(path,state);report(plan,state);count+=1
        print(row['status'],op['anchor'],op['type'],[len(p) for p in op['partitions']],flush=True)
    state['finished']=all(o['id'] in {r['id'] for r in state['results'] if r['status']=='confirmed'} for o in plan['operations'])
    regroup.save(path,state)


if __name__=='__main__':main()
