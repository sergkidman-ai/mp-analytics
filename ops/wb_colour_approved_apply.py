#!/usr/bin/env python3
# поток: mkt
"""Execute only the 78+1 colour operations explicitly approved on 12.10.

Immutable saved membership plans, exclusive locks, content verification and
bounded independently proved recovery. No price, stock or chip-field writes.
"""
import argparse
import fcntl
import hashlib
import html
import json
from pathlib import Path
import time

from ops import wb_bundle_regroup as regroup, wb_core_glue as core
from ops.wb_bundle_glue_singletons import Content,now
from ops.wb_glue_recovery import entries,make_fresh,retryable

SOURCE=regroup.ROOT/'docs/reports/mkt_wb_colour_chip_review_2026-10-12_acc1'
OUT=SOURCE/'approved_apply'


def prepare():
    OUT.mkdir(parents=True,exist_ok=True)
    path=OUT/'plan.json'
    if path.exists():return json.loads(path.read_text())
    summary=json.loads((SOURCE/'summary.json').read_text())
    ops=[]
    for kind,filename,key in [('bundle','bundle_new.json','new_bundle_ids'),('core','core_new.json','new_core_ids')]:
        plan=json.loads((SOURCE/filename).read_text());wanted=set(summary[key])
        chosen=[{**o,'approved_kind':kind} for o in plan['operations'] if o['id'] in wanted]
        if {o['id'] for o in chosen}!=wanted:raise RuntimeError('Approved IDs do not match saved report')
        ops.extend(chosen)
    if len(ops)!=79 or sum(o['approved_kind']=='bundle' for o in ops)!=78:
        raise RuntimeError('Approval scope is exactly 78 regroupings and one core join')
    seen=set()
    for op in ops:
        regroup.validate(op)
        ids={c['nmID'] for c in op['before_cards']}
        if seen & ids:raise RuntimeError('Approved operations overlap')
        seen|=ids
        anchor=next(c for c in op['before_cards'] if c['nmID']==op['anchor'])
        op.setdefault('preferred_roots',{str(anchor['imtID']):anchor['nmID']})
    manifest={'account':'wb_acc1','prepared_at':now(),'scope':'Прямая команда Сергея: разделять 78 групп и доклеить один набор как в отчёте',
              'source_read_at':summary['checked_at'],'operations':ops,
              'approved_membership_sha256':hashlib.sha256(json.dumps([(o['id'],o['partitions']) for o in ops],sort_keys=True).encode()).hexdigest()}
    regroup.save(path,manifest);return manifest


def report(plan,state):
    done={r['id']:r for r in state['results']};confirmed=[r for r in done.values() if r['status']=='confirmed']
    summary={'confirmed':len(confirmed),'total':len(plan['operations']),'finished':state.get('finished',False),
             'regroup_confirmed':sum(o['approved_kind']=='bundle' and done.get(o['id'],{}).get('status')=='confirmed' for o in plan['operations']),
             'core_confirmed':sum(o['approved_kind']=='core' and done.get(o['id'],{}).get('status')=='confirmed' for o in plan['operations']),
             'cards_confirmed':sum(r.get('cards',0) for r in confirmed),'checked_at':now(),
             'remaining':[o['id'] for o in plan['operations'] if done.get(o['id'],{}).get('status')!='confirmed'],
             'stopped_at':state.get('stopped_at'),'reason':state.get('reason')}
    regroup.save(OUT/'summary.json',summary)
    rows=[]
    for op in plan['operations']:
        result=done.get(op['id'],{});status=result.get('status','ожидает')
        status={'confirmed':'выполнено и подтверждено API','skipped_changed':'состав/поля изменились — требуется сверка'}.get(status,status)
        sizes=' + '.join(str(len(p)) for p in op['partitions'])
        rows.append(f'<tr><td><a href="{op["url"]}" target="_blank">{op["anchor"]} · {html.escape(op["title"])}</a></td><td>{sizes}</td><td>{html.escape(status)}</td></tr>')
    text='<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Выполнение цветных групп acc1</title><style>body{max-width:1100px;margin:30px auto;padding:20px;font:17px system-ui;line-height:1.5}a{color:#14615c}table{border-collapse:collapse}td,th{padding:9px;border:1px solid #bbc8bf}</style><h1>Утверждённые цветные группы — acc1</h1>'
    text+=f'<p>Подтверждено {summary["confirmed"]} из79: переразборов {summary["regroup_confirmed"]} из78, доклеек {summary["core_confirmed"]} из1. Проверено {summary["cards_confirmed"]} карточек. Обновлено {html.escape(summary["checked_at"])}.</p>'
    text+='<p>Цвета и набор остаются с собственными X2; большие бандлы отдельной группой своей семьи. Поля карточек не меняем. Подтверждение состава через Content API; отображение витрины отдельно.</p>'
    if state.get('stopped_at'):text+='<p>Приостановлено на независимую сверку: '+html.escape(str(state.get('reason','')))+'.</p>'
    text+='<table><tr><th>Основная карточка</th><th>Размеры групп после</th><th>Состояние</th></tr>'+''.join(rows)+'</table></html>'
    (OUT/'index.html').write_text(text)
    main=SOURCE/'index.html'
    if main.exists():
        banner=f'<!-- APPLY-PROGRESS --><p><b>Выполнение по команде Сергея: {summary["confirmed"]}/79 подтверждено.</b> <a href="/api/wb-bids/reports/glue-colour-applied-2026-10-12">Ход и результаты переносов</a>.</p><!-- /APPLY-PROGRESS -->'
        import re
        body=main.read_text();body=re.sub(r'<!-- APPLY-PROGRESS -->.*?<!-- /APPLY-PROGRESS -->','',body,flags=re.S)
        body=body.replace('<h1>Цветные картриджи и наборы — acc1</h1>','<h1>Цветные картриджи и наборы — acc1</h1>'+banner)
        body=body.replace('Этот проход проверяет и готовит список, переносов не выполняет.','Исходный проверочный проход завершён; ниже сохранены утверждённые составы. Текущее выполнение — по ссылке выше.')
        main.write_text(body)


def run():
    plan=prepare();locks=[]
    for folder in [regroup.OUT,core.OUT,OUT]:
        handle=(folder/'apply.lock').open('a');fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB);locks.append(handle)
    state_path=OUT/'apply_results.json'
    state=json.loads(state_path.read_text()) if state_path.exists() else {'results':[],'started_at':now(),'finished':False}
    regroup.OUT=OUT
    report(plan,state)
    for original in plan['operations']:
        if any(r['id']==original['id'] and r['status']=='confirmed' for r in state['results']):continue
        op=original
        events=entries(OUT/'journal.jsonl',op['id'])
        if events:
            fresh,proof=make_fresh(op,Content(),events)
            regroup.save(OUT/f'restart_proof_{op["id"]}.json',proof);op=fresh
        for attempt in range(4):
            try:
                result=regroup.execute(op,Content())
                if result['status']!='confirmed':
                    raise RuntimeError('Live source changed since the approved report; no write performed')
                state['results']=[r for r in state['results'] if r['id']!=original['id']]+[result]
                for key in ['stopped_at','reason']:state.pop(key,None)
                regroup.save(state_path,state);report(plan,state)
                print('CONFIRMED',len(state['results']),'/79',result['anchor'],[len(p['nm_ids']) for p in result['partitions']],flush=True)
                break
            except Exception as exc:
                state.update(stopped_at=original['id'],reason=str(exc));regroup.save(state_path,state);report(plan,state)
                events=entries(OUT/'journal.jsonl',original['id'])
                if attempt==3 or not retryable(state,events):raise
                time.sleep(10*(attempt+1))
                op,proof=make_fresh(original,Content(),events)
                regroup.save(OUT/f'recovery_{original["id"]}_{attempt+1}.json',proof)
    state.update(finished=True,finished_at=now());regroup.save(state_path,state);report(plan,state)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    if args.apply:run()
    else:
        plan=prepare();path=OUT/'apply_results.json'
        state=json.loads(path.read_text()) if path.exists() else {'results':[],'finished':False}
        report(plan,state);print('Prepared only:',len(plan['operations']))


if __name__=='__main__':main()
