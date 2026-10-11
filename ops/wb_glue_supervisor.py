#!/usr/bin/env python3
# поток: mkt
"""Run the authorized grouping queue and reconcile bounded transient failures."""
import argparse
import fcntl
import json
from pathlib import Path
import subprocess
import sys
import time

from ops import wb_bundle_regroup as regroup
from ops import wb_core_glue as core
from ops.wb_bundle_glue_singletons import Content,now
from ops.wb_glue_recovery import UnsafeRecovery,entries,make_fresh,retryable

ROOT=Path(__file__).resolve().parents[1]


def log(out,**event):
    with (out/'supervisor_journal.jsonl').open('a') as f:
        f.write(json.dumps({'ts':now(),**event},ensure_ascii=False)+'\n')
    print('SUPERVISOR',event,flush=True)


def pending_id(out,state):
    if state.get('stopped_at'):
        return state['stopped_at']
    done={r['id'] for r in state.get('results',[])}
    path=out/'journal.jsonl'
    if path.exists():
        for line in reversed(path.read_text().splitlines()):
            row=json.loads(line)
            if row.get('id') not in done:
                return row.get('id')
            break
    return None


def recover(out,kind,limit):
    state_path=out/'apply_results.json'
    state=json.loads(state_path.read_text()) if state_path.exists() else {'results':[]}
    ident=pending_id(out,state)
    if ident is None:
        return False
    plan=json.loads((out/'plan.json').read_text())
    op=next((o for o in plan['operations'] if o['id']==ident),None)
    if op is None:
        raise UnsafeRecovery('Stopped operation not present in saved plan')
    lock=(out/'apply.lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    old_out=regroup.OUT;regroup.OUT=out
    try:
        for attempt in range(1,limit+1):
            events=entries(out/'journal.jsonl',ident)
            if not retryable(state,events):
                raise UnsafeRecovery('WB error is not eligible for automatic recovery')
            state.update(finished=False,stopped_at=ident,auto_recovering=True,recovery_attempt=attempt)
            regroup.save(state_path,state)
            log(out,event='recovery_attempt',id=ident,attempt=attempt)
            time.sleep(min(60,10*attempt))
            try:
                fresh,proof=make_fresh(op,Content(),events)
                regroup.save(out/f'auto_recovery_{ident}_{attempt}.json',proof)
                result=regroup.execute(fresh,Content())
                if result.get('status')!='confirmed':
                    raise UnsafeRecovery('State changed between recovery reads and execution')
                state['results']=[r for r in state.get('results',[]) if r['id']!=ident]+[result]
                for key in ('stopped_at','reason','auto_recovering','recovery_attempt','recovery_blocked'):
                    state.pop(key,None)
                state['reconciled_at']=now()
                regroup.save(state_path,state)
                if kind=='bundle':regroup.report(plan,state['results'])
                else:core.report(plan,state)
                log(out,event='recovered',id=ident,explanation=proof['explanation'])
                return True
            except UnsafeRecovery:
                raise
            except Exception as exc:
                state['reason']=str(exc);regroup.save(state_path,state)
                log(out,event='recovery_error',id=ident,attempt=attempt,error=str(exc))
        raise UnsafeRecovery('Automatic recovery attempts exhausted')
    finally:
        regroup.OUT=old_out;lock.close()


def wait_bundles(out):
    while True:
        path=regroup.OUT/'apply_results.json'
        state=json.loads(path.read_text()) if path.exists() else {}
        if state.get('recovery_blocked'):
            raise UnsafeRecovery('Bundle recovery requires review; core writes remain blocked')
        if state.get('finished'):
            return
        time.sleep(5)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--kind',choices=['bundle','core'],required=True)
    ap.add_argument('--recovery-attempts',type=int,default=3)
    args=ap.parse_args()
    if not 1<=args.recovery_attempts<=5:ap.error('recovery attempts must be 1..5')
    out=regroup.OUT if args.kind=='bundle' else core.OUT
    out.mkdir(parents=True,exist_ok=True)
    own=(out/'supervisor.lock').open('a')
    fcntl.flock(own,fcntl.LOCK_EX|fcntl.LOCK_NB)
    try:
        if args.kind=='core':
            log(out,event='waiting_for_bundle_completion')
            wait_bundles(out)
        recovered=0;startup_failures=0
        while True:
            if recover(out,args.kind,args.recovery_attempts):recovered+=1
            module='ops.wb_bundle_regroup' if args.kind=='bundle' else 'ops.wb_core_glue'
            command=[sys.executable,'-u','-m',module,'--apply']
            fresh_started=None
            if args.kind=='core':
                command+=['--after-bundles']
                # Fresh baseline once, never overwrite an interrupted plan.
                marker=out/'supervisor_fresh_catalog_ready'
                if not marker.exists():
                    command+=['--fresh']
                    fresh_started=now()
            log(out,event='batch_start',module=module,recoveries=recovered)
            child=subprocess.run(command,cwd=ROOT)
            if fresh_started and (out/'catalog.json').exists() and (out/'plan.json').exists():
                catalog=json.loads((out/'catalog.json').read_text())
                plan=json.loads((out/'plan.json').read_text())
                if catalog['read_at']>=fresh_started and plan['catalog_read_at']==catalog['read_at']:
                    marker.write_text(catalog['read_at'])
            state=json.loads((out/'apply_results.json').read_text())
            if child.returncode==0:
                log(out,event='batch_finished',finished=state.get('finished'),
                    confirmed=sum(r.get('status')=='confirmed' for r in state.get('results',[])))
                return 0
            log(out,event='batch_interrupted',returncode=child.returncode)
            if pending_id(out,state) is None:
                startup_failures+=1
                if startup_failures>=3:
                    raise UnsafeRecovery('Batch startup failed three times without a pending write')
                # No unrecorded membership change is repeated: confirmed rows
                # are skipped by the executor on this new startup attempt.
                log(out,event='startup_retry',attempt=startup_failures)
                time.sleep(60)
            else:
                startup_failures=0
    except UnsafeRecovery as exc:
        path=out/'apply_results.json'
        state=json.loads(path.read_text()) if path.exists() else {'results':[]}
        state.update(finished=False,recovery_blocked=True,auto_recovering=False,recovery_reason=str(exc))
        regroup.save(path,state)
        log(out,event='review_required',error=str(exc))
        return 78


if __name__=='__main__':sys.exit(main())
