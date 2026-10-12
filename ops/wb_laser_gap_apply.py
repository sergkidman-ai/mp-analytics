#!/usr/bin/env python3
# поток: mkt
"""Apply only current colour-laser gap plans authorized in the 12-point task."""
import fcntl
import argparse
import json
import time
from ops import wb_bundle_regroup as regroup,wb_core_glue as core
from ops.wb_bundle_glue_singletons import Content,now
from ops.wb_glue_recovery import entries,make_fresh,retryable

OUT=regroup.ROOT/'docs/reports/mkt_wb_glue_status_2026-10-12_acc1/laser_gaps'


def main():
    plan=json.loads((OUT/'plan.json').read_text());path=OUT/'apply_results.json'
    state=json.loads(path.read_text()) if path.exists() else {'results':[],'started_at':now(),'finished':False}
    locks=[]
    for folder in [regroup.OUT,core.OUT,OUT]:
        handle=(folder/'apply.lock').open('a');fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB);locks.append(handle)
    old_out=regroup.OUT;regroup.OUT=OUT
    ids=set()
    for op in plan['operations']:
        regroup.validate(op)
        current={c['nmID'] for c in op['before_cards']}
        if ids&current:raise RuntimeError('Plans overlap')
        ids|=current
    for original in plan['operations']:
        if any(r['id']==original['id'] and r['status']=='confirmed' for r in state['results']):continue
        op=original;events=entries(OUT/'journal.jsonl',op['id'])
        if events:
            op,proof=make_fresh(original,Content(),events);regroup.save(OUT/f'restart_{op["id"]}.json',proof)
        for attempt in range(4):
            try:
                result=regroup.execute(op,Content())
                if result['status']!='confirmed':raise RuntimeError('Live contents changed since prepared scan; no write')
                result['confirmed_at']=now();state['results'].append(result)
                state.pop('stopped_at',None);state.pop('reason',None);regroup.save(path,state)
                print('CONFIRMED',len(state['results']),'/',len(plan['operations']),original['title'],[len(p['nm_ids']) for p in result['partitions']],flush=True);break
            except Exception as exc:
                state.update(stopped_at=original['id'],reason=str(exc));regroup.save(path,state)
                events=entries(OUT/'journal.jsonl',op['id'])
                if attempt==3 or not retryable(state,events):raise
                time.sleep(10*(attempt+1));op,proof=make_fresh(original,Content(),events);regroup.save(OUT/f'recovery_{op["id"]}_{attempt}.json',proof)
    state.update(finished=True,finished_at=now());regroup.save(path,state);regroup.OUT=old_out


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    if args.apply:main()
    else:
        plan=json.loads((OUT/'plan.json').read_text());print('Read-only preview:',len(plan['operations']),'prepared colour-laser operations; use --apply only under current authorization')
