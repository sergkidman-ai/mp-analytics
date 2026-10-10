#!/usr/bin/env python3
# поток: mkt
"""Acc1 regrouping under Sergey's 10.10 rules. Read-only unless --apply.

Partitions preserve all cards and content. Source and destination membership
are checked before every write; uncertain writes stop and are never repeated.
Missing chip is a separate class, not evidence of absence. No price/ads writes.
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

from ops.wb_bundle_plan import char, color, fetch_catalog, fingerprint, is_bundle, normalized, number, quantities
from ops.wb_bundle_glue_singletons import Content, compatible_values, content_fingerprint, explicit_chip, now

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/reports/mkt_wb_bundle_regroup_2026-10-10_acc1'
UNKNOWN = 'чип не указан'
CARTRIDGES = {'Картридж', 'Тонер-картридж'}
SUPPORTED = CARTRIDGES | {'Фотобарабан','Тонер','Чернила','Лента для принтера',
                         'Комплект картриджей','Комплект чернил','Фотобарабан + картридж'}


def save(path, obj):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    tmp.replace(path)


def base(card):
    code = card.get('vendorCode', '')
    return code[:4] if re.fullmatch(r'\d{4}',code) or is_bundle(card) else None


def chip_info(parent, bundles, ref, marks):
    declared = set(marks)
    tc = {'chip':'с чипом','nochip':'без чипа','chip_free':'чип без счётчика'}.get(ref.get('chip'))
    if tc:
        declared.add(tc)
    wb = set()
    for c in [parent, *bundles]:
        for text in (c.get('title',''), char(c,378533), c.get('description','')):
            wb |= explicit_chip(text)
    all_marks = declared | wb
    return (next(iter(all_marks)) if len(all_marks)==1 else UNKNOWN if not all_marks else 'противоречие'), sorted(all_marks)


def reasons(parent, bundles, ref, chip):
    rr = []
    if not ref or ref.get('consumable_type') not in SUPPORTED:
        rr.append('Тип товара не подтверждён или вне правил')
    if chip == 'противоречие':
        rr.append('Противоречие явных данных о чипе')
    if normalized(char(parent,5023)) != normalized(ref.get('title','')) or not char(parent,5023):
        rr.append('Модель родителя не подтверждается ТК')
    if ref.get('consumable_type') in {'Комплект картриджей','Комплект чернил','Фотобарабан + картридж'} and bundles:
        rr.append('Бандл набора: требуется ручная проверка содержимого')
    if bundles and number(char(parent,179792)) != 1:
        rr.append('Количество в родительской карточке не равно 1')
    tc_printers=(ref.get('raw') or {}).get('printer_models')
    ink_reference=(ref.get('consumable_type')=='Чернила' and tc_printers)
    if bundles and not compatible_values(parent,179078) and not ink_reference:
        rr.append('Не заполнена совместимость родителя')
    for b in bundles:
        qty, field, title, code = quantities(b)
        if code is None or code <= 1 or any(v is not None and v!=code for v in (field,title)):
            rr.append('Количество бандла расходится между полями')
        if base(b) != parent['vendorCode'] or b['subjectID'] != parent['subjectID']:
            rr.append('Бандл другой семьи или предмета')
        for ident,label in ((5023,'модель'),(58854,'цвет'),(88963,'ресурс')):
            if normalized(char(parent,ident)) != normalized(char(b,ident)):
                rr.append('Бандл отличается от родителя: '+label)
        if compatible_values(parent,179078) != compatible_values(b,179078):
            rr.append('Бандл отличается по списку принтеров')
    return sorted(set(rr))


def prepare(snapshot, source):
    cards = snapshot['cards']
    refs, marks = source['references'], source['marks']
    byid = {c['nmID']:c for c in cards}
    groups, parents, bundles = defaultdict(list), defaultdict(list), defaultdict(list)
    for c in cards:
        groups[c['imtID']].append(c)
        if re.fullmatch(r'\d{4}',c.get('vendorCode','')):
            parents[c['vendorCode']].append(c)
        if is_bundle(c):
            bundles[base(c)].append(c)
    families, review = {}, []
    for code, pp in parents.items():
        if len(pp)!=1:
            continue
        p=pp[0]; bb=bundles.get(code,[]); ref=refs.get(code,{})
        chip,evidence=chip_info(p,bb,ref,marks.get(code,[]))
        families[code]={'parent':p,'bundles':bb,'ref':ref,'chip':chip,'evidence':evidence,
                        'reasons':reasons(p,bb,ref,chip)}
    # Connected components encompass every current group touched by a family.
    links=defaultdict(set)
    for code,bb in bundles.items():
        ff=families.get(code)
        if not ff:
            review.append({'base':code,'reason':'Точный родитель не найден','cards':[c['nmID'] for c in bb]})
            continue
        roots={c['imtID'] for c in [ff['parent'],*bb]}
        for imt in roots:
            links[imt] |= roots
    seen=set(); components=[]
    for imt in sorted(links):
        if imt in seen: continue
        stack=[imt]; current=set()
        while stack:
            x=stack.pop()
            if x in current: continue
            current.add(x); stack.extend(links[x]-current)
        seen |= current; components.append(current)
    plans=[]
    for imts in components:
        cc=sorted([c for imt in imts for c in groups[imt]],key=lambda c:c['nmID'])
        codes={base(c) for c in cc}
        rr=[]
        if None in codes: rr.append('В группе есть дочерняя по принтеру или непризнанная карточка')
        for code in codes-{None}:
            if code not in families:
                rr.append('Нет точного родителя '+code)
            else:
                rr += [code+': '+r for r in families[code]['reasons']]
        # Only within established mixed groups do chips need manual decisions.
        for imt in imts:
            core=[c for c in groups[imt] if not is_bundle(c)]
            cartridge_core=[c for c in core if families.get(base(c),{}).get('ref',{}).get('consumable_type') in CARTRIDGES]
            chips={families[base(c)]['chip'] for c in cartridge_core}
            if UNKNOWN in chips and len(chips)>1:
                rr.append('Неизвестный чип рядом с известным: ручная выборка')
            if any(families.get(base(c),{}).get('ref',{}).get('consumable_type') in {'Фотобарабан','Фотобарабан + картридж'} for c in core) and len(chips)>1:
                rr.append('В группе барабана разные чипы картриджей: ручная выборка')
            if any(families.get(base(c),{}).get('ref',{}).get('consumable_type')=='Комплект картриджей' for c in core):
                all_chips={families[base(c)]['chip'] for c in core if base(c) in families and families[base(c)]['ref']['consumable_type'] in CARTRIDGES|{'Комплект картриджей'}}
                if UNKNOWN in all_chips and len(all_chips)>1:
                    rr.append('У цветного набора и картриджей неодинаковая определённость чипа: ручная выборка')
        if rr:
            review.append({'component':min(imts),'reason':'; '.join(sorted(set(rr))),
                           'cards':[c['nmID'] for c in cc]})
            continue
        desired=defaultdict(list); rules=set()
        for imt in sorted(imts):
            core=[c for c in groups[imt] if not is_bundle(c)]
            if not core: continue
            core_types={families[base(c)]['ref']['consumable_type'] for c in core}
            is_drum='Фотобарабан' in core_types or 'Фотобарабан + картридж' in core_types
            is_color=any(color(char(c,58854)) in {'C','M','Y','CMYK','CMY','LC','LM'} for c in core)
            # Full resource equality, not letters. Capacity-only mixed groups
            # are split only when every member has its own validated bundles.
            resources={normalized(char(c,88963)) for c in core if families[base(c)]['ref']['consumable_type'] in CARTRIDGES}
            cartridge_cards=[c for c in core if families[base(c)]['ref']['consumable_type'] in CARTRIDGES]
            capacity=(not is_drum and not is_color and core_types <= CARTRIDGES|{'Тонер'} and len(resources)>1)
            if capacity:
                printer_sets={compatible_values(c,179078) for c in cartridge_cards}
                if len(printer_sets)!=1 or not next(iter(printer_sets)) or not all(families[base(c)]['bundles'] for c in cartridge_cards):
                    rr.append('Ресурс: нет точного совпадения принтеров или бандлов у всех родителей')
            for c in core:
                f=families[base(c)]; typ=f['ref']['consumable_type']; chip=f['chip']
                if typ=='Тонер':
                    key=('family',base(c)); rules.add('заправочный тонер отдельно')
                elif capacity:
                    key=('family',base(c)); rules.add('ресурс раздельно с бандлами')
                elif typ=='Лента для принтера':
                    if len(core)>1:
                        key=('core',imt,chip); rules.add('ленты: бандлы отдельно')
                    else:
                        key=('family',base(c)); rules.add('одиночный+свои бандлы')
                elif typ in CARTRIDGES and not is_drum and not is_color and f['bundles']:
                    key=('family',base(c)); rules.add('одиночные семьи с бандлами отдельно')
                else:
                    # Chip concerns apply to cartridge alternatives. Other
                    # product roles (drum/kit/ink) aren't assigned a no-chip label.
                    key=('core',imt,chip if not is_drum else 'components')
                desired[key].append(c['nmID'])
                for b in f['bundles']:
                    n=quantities(b)[3]
                    if typ=='Тонер' or capacity or key[0]=='family':
                        bk=key
                    elif is_drum:
                        bk=key if typ in CARTRIDGES and n==2 else ('bundles',base(c))
                        rules.add('барабан+компоненты, только тонер-картридж x2')
                    elif typ=='Лента для принтера':
                        bk=('bundles',base(c))
                    elif len(core)>1:
                        bk=key if n==2 else ('bundles',base(c))
                        rules.add('цвет/существующая группа: только x2')
                    else:
                        bk=key
                        rules.add('одиночный+свои бандлы')
                    desired[bk].append(b['nmID'])
        if rr:
            review.append({'component':min(imts),'reason':'; '.join(sorted(set(rr))),
                           'cards':[c['nmID'] for c in cc]}); continue
        parts=[sorted(set(v)) for v in desired.values()]
        if not parts or len([nm for part in parts for nm in part])!=len(cc) or set(nm for p in parts for nm in p)!={c['nmID'] for c in cc}:
            review.append({'component':min(imts),'reason':'Не все участники покрыты точными семьями', 'cards':[c['nmID'] for c in cc]}); continue
        if any(len(part)>30 for part in parts):
            review.append({'component':min(imts),'reason':'Группа превысит 30 карточек','cards':[c['nmID'] for c in cc]}); continue
        old={frozenset(c['nmID'] for c in groups[x]) for x in imts}
        if old=={frozenset(p) for p in parts}: continue
        anchor=next(c for c in cc if not is_bundle(c))
        plans.append({'id':min(imts),'anchor':anchor['nmID'],'url':f"https://www.wildberries.ru/catalog/{anchor['nmID']}/detail.aspx",
                      'title':anchor.get('title',''),'rules':sorted(rules),
                      'before_groups':{str(x):sorted(c['nmID'] for c in groups[x]) for x in sorted(imts)},
                      'before_cards':cc,'before_fingerprint':fingerprint(cc),'partitions':parts})
    census=Counter()
    for c in cards:
        if not is_bundle(c): continue
        f=families.get(base(c),{}); ref=f.get('ref',{})
        census[(ref.get('consumable_type','?'),ref.get('raw',{}).get('assignment','?'),color(char(c,58854)))]+=1
    return {'account':'wb_acc1','prepared_at':now(),'catalog_read_at':snapshot['read_at'],
            'operations':plans,'review':review,'families':len(families),'bundle_cards':sum(len(b) for b in bundles.values()),
            'census':[{'type':t,'assignment':a,'color':co,'count':v} for (t,a,co),v in sorted(census.items())]}


def validate(op):
    all_ids=[c['nmID'] for c in op['before_cards']]
    pp=[nm for p in op['partitions'] for nm in p]
    if not pp or len(pp)!=len(set(pp)) or set(pp)!=set(all_ids) or any(not p or len(p)>30 for p in op['partitions']):
        raise RuntimeError('Partitions are not a lossless, disjoint allocation')
    if fingerprint(op['before_cards'])!=op['before_fingerprint']:
        raise RuntimeError('Plan fingerprint mismatch')
    if len({c['subjectID'] for c in op['before_cards']})!=1:
        raise RuntimeError('Different WB subjects')


def journal(entry):
    with (OUT/'journal.jsonl').open('a') as f:
        f.write(json.dumps({'ts':now(),**entry},ensure_ascii=False)+'\n'); f.flush()


def by_nm(client,nm):
    cc=fetch_catalog('wb_acc1',text_search=nm)
    cc=[c for c in cc if c['nmID']==nm]
    if len(cc)!=1: raise RuntimeError('Anchor read missing')
    return cc[0]


def execute(op, client):
    validate(op)
    groups={k:client.group(int(k)) for k in op['before_groups']}
    if {k:sorted(c['nmID'] for c in v) for k,v in groups.items()}!=op['before_groups'] or fingerprint([c for v in groups.values() for c in v])!=op['before_fingerprint']:
        return {'id':op['id'],'status':'skipped_changed','anchor':op['anchor']}
    state={c['nmID']:c for v in groups.values() for c in v}
    content_before=content_fingerprint(op['before_cards'])
    preferred={int(k):v for k,v in op.get('preferred_roots',{}).items()}
    for root,anchor in preferred.items():
        if root not in {c['imtID'] for c in state.values()}:
            raise RuntimeError('Preferred original root no longer exists')
        if state[anchor]['imtID']!=root:
            if sum(c['imtID']==root for c in state.values())>=30:
                raise RuntimeError('Reclaiming original root would exceed 30')
            perform(client,op,{'targetIMT':root,'nmIDs':[anchor]},state)
    remaining=sorted(op['partitions'],key=lambda p:(-sum(not is_bundle(state[n]) for n in p),-len(p),p))
    reserved=set(); allocation=[]
    for part in remaining:
        candidates={state[n]['imtID'] for n in part}-reserved
        if candidates:
            wanted=[r for r,a in preferred.items() if a in part and r in candidates]
            root=wanted[0] if wanted else max(candidates,key=lambda r:(sum(not is_bundle(state[n]) for n in part if state[n]['imtID']==r),
                                                                     sum(state[n]['imtID']==r for n in part)))
            target=next(n for n in part if state[n]['imtID']==root)
            reserved.add(root)
            allocation.append([part,root,target,False])
        else:
            allocation.append([part,None,part[0],True])
    # Detach small outgoing partitions, leaving the main parent/color/drum
    # group at its original imtID. Never move the whole core unnecessarily.
    for item in allocation:
        if item[3]:
            perform(client,op,{'nmIDs':[item[2]]},state)
            item[1]=state[item[2]]['imtID']
    pending=sorted(allocation,key=lambda x:(-sum(not is_bundle(state[n]) for n in x[0]),not x[3],len(x[0])))
    target_for=[]
    while pending:
        progressed=False
        for item in pending[:]:
            part,root,_,_=item
            moved=[n for n in part if state[n]['imtID']!=root]
            actual={n for n,c in state.items() if c['imtID']==root}
            if len(actual|set(moved))>30: continue
            if moved: perform(client,op,{'targetIMT':root,'nmIDs':moved},state)
            target_for.append((part,root));pending.remove(item);progressed=True
        if not progressed: raise RuntimeError('Intermediate grouping would exceed 30; requires staged manual review')
    current={}
    verified_roots=[]
    for part,root in target_for:
        for delay in (0,2,4,8,12):
            if delay: time.sleep(delay)
            cc=client.group(root)
            if {c['nmID'] for c in cc}==set(part): break
        else: raise RuntimeError('Final partition not confirmed')
        current.update({c['nmID']:c for c in cc})
        verified_roots.append((part,root))
    if content_fingerprint(list(current.values()))!=content_before:
        raise RuntimeError('Card content changed during grouping')
    row={'id':op['id'],'status':'confirmed','anchor':op['anchor'],'cards':len(state),
         'partitions':[{'root':r,'nm_ids':p} for p,r in verified_roots],'rules':op['rules']}
    journal(row)
    with (ROOT/'docs/reports/mkt_glue_journal.jsonl').open('a') as f:
        f.write(json.dumps({'ts':now(),'account':'wb_acc1','action':'regroup','before':op['before_groups'],**row},ensure_ascii=False)+'\n')
    return row


def perform(client, op, body, state):
    old_root=state[body['nmIDs'][0]]['imtID']
    affected={state[n]['imtID'] for n in body['nmIDs']}
    if 'targetIMT' in body: affected.add(body['targetIMT'])
    before=[c for r in affected for c in client.group(r)]
    expected=[c for c in state.values() if c['imtID'] in affected]
    for delay in (0,2,4,8,12):
        if delay:
            time.sleep(delay)
            before=[c for r in affected for c in client.group(r)]
        if fingerprint(before)==fingerprint(expected): break
    else: raise RuntimeError('Live preflight changed; stop')
    journal({'id':op['id'],'status':'attempt','body':body,
             'before_groups':{str(r):sorted(c['nmID'] for c in expected if c['imtID']==r) for r in affected}})
    r=client.move(body)
    journal({'id':op['id'],'status':'response','http':r.status_code,'response':r.text[:500]})
    r.raise_for_status()
    if r.content and r.json().get('error'): raise RuntimeError('WB rejected grouping')
    # An accepted detach creates a new root; discover it through the moved anchor.
    for delay in (2,4,8,12,20):
        time.sleep(delay)
        anchor=by_nm(client,body['nmIDs'][0]); root=body.get('targetIMT',anchor['imtID'])
        if 'targetIMT' not in body and root==old_root:
            # Search and group indices may both still show the old state.
            # Detach is not confirmed until the moved card has a different root.
            continue
        after_roots=affected | {anchor['imtID'],root}
        after=[c for x in after_roots for c in client.group(x)]
        byid={c['nmID']:c for c in after}
        if set(byid)!=set(c['nmID'] for c in expected): continue
        if any(byid[n]['imtID']!=root for n in body['nmIDs']): continue
        if content_fingerprint(list(byid.values()))!=content_fingerprint(expected):
            raise RuntimeError('Content fingerprint changed; stop')
        state.update(byid)
        journal({'id':op['id'],'status':'move_confirmed','body':body,'target_root':root})
        return
    raise RuntimeError('Write accepted but not confirmed; stop without retrying write')


def report(plan, results):
    byid={c['nmID']:c for op in plan['operations'] for c in op['before_cards']}
    done={r['id']:r for r in results}
    rows=[]
    for op in plan['operations']:
        row={'id':op['id'],'parent_nm':op['anchor'],'title':op['title'],'url':op['url'],
             'status':done.get(op['id'],{}).get('status','prepared'),'rules':'; '.join(op['rules'])}
        rows.append(row)
    if rows:
        with (OUT/'operations.csv').open('w') as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    catalog=json.loads((OUT/'catalog.json').read_text())
    all_cards={c['nmID']:c for c in catalog['cards']}
    manual=[]
    for item in plan['review']:
        parents=[all_cards[n] for n in item['cards'] if n in all_cards and not is_bundle(all_cards[n])]
        for c in parents or [all_cards[n] for n in item['cards'] if n in all_cards][:1]:
            manual.append({'nm_id':c['nmID'],'vendor_code':c.get('vendorCode',''),'title':c.get('title',''),
                           'url':f"https://www.wildberries.ru/catalog/{c['nmID']}/detail.aspx",'reason':item['reason']})
    if manual:
        with (OUT/'manual_review.csv').open('w') as f:
            w=csv.DictWriter(f,fieldnames=list(manual[0]));w.writeheader();w.writerows(manual)
    body=[]
    for op in plan['operations']:
        status=done.get(op['id'],{}).get('status','prepared')
        parts=['<li>'+html.escape('; '.join(byid[n]['vendorCode']+' '+byid[n]['title'] for n in p))+'</li>' for p in op['partitions']]
        body.append(f'<details><summary><a href="{op["url"]}" target="_blank" rel="noopener">{op["anchor"]} {html.escape(op["title"])}</a> — {status}</summary><p>{html.escape("; ".join(op["rules"]))}</p><ol>{"".join(parts)}</ol></details>')
    review=''.join('<li><a href="'+r['url']+'" target="_blank" rel="noopener">'+str(r['nm_id'])+' '+html.escape(r['title'])+'</a>: '+html.escape(r['reason'])+'</li>' for r in manual)
    (OUT/'index.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><title>Склейки acc1 — 10.10</title><style>body{max-width:1100px;margin:30px auto;padding:20px;font:17px system-ui;line-height:1.5}details{padding:12px;border-bottom:1px solid #ddd}a{color:#14615c}summary{cursor:pointer}</style><h1>Склейки acc1 — 10.10.2026</h1><p>Подготовлено '+str(len(rows))+' компонентов. Подтверждено '+str(sum(r['status']=='confirmed' for r in results))+'. Каталог: '+html.escape(plan['catalog_read_at'])+'</p>'+''.join(body)+'<h2>Ручная проверка</h2><ul>'+review+'</ul></html>')


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--prepare',action='store_true');ap.add_argument('--apply',action='store_true')
    ap.add_argument('--ids',type=int,nargs='*');ap.add_argument('--limit',type=int);a=ap.parse_args()
    OUT.mkdir(exist_ok=True)
    if a.prepare:
        plan=prepare(json.loads((OUT/'catalog.json').read_text()),json.loads((OUT/'references.json').read_text()))
        save(OUT/'plan.json',plan);report(plan,[])
        print('Prepared:',len(plan['operations']),'Review:',len(plan['review']))
        print('Rules:',dict(Counter(r for op in plan['operations'] for r in op['rules'])))
        return
    plan=json.loads((OUT/'plan.json').read_text()); path=OUT/'apply_results.json'
    results=json.loads(path.read_text()).get('results',[]) if path.exists() else []
    if not a.apply: print('DRY-RUN:',len(plan['operations']));return
    lock=(OUT/'apply.lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    if path.exists() and json.loads(path.read_text()).get('stopped_at'):
        raise RuntimeError('Previous write is uncertain: reconcile it before any continuation')
    client=Content();done={r['id'] for r in results};count=0
    for op in plan['operations']:
        if op['id'] in done or (a.ids and op['id'] not in a.ids): continue
        if a.limit is not None and count>=a.limit: break
        try: row=execute(op,client)
        except Exception as exc:
            save(path,{'finished':False,'results':results,'stopped_at':op['id'],'reason':str(exc)})
            report(plan,results)
            print('STOP:',op['id'],type(exc).__name__,str(exc),flush=True)
            raise
        results.append(row);count+=1
        save(path,{'finished':False,'results':results});report(plan,results)
        print(row['status'],op['id'],op['anchor'],op['rules'],flush=True)
    save(path,{'finished':len(results)==len(plan['operations']),'results':results});report(plan,results)


if __name__=='__main__': main()
