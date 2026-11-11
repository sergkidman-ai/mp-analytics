# поток: card
"""Плановый запуск прежнего A/W no-op с журналом и восстановлением незавершённых проверок."""
import argparse
from datetime import datetime, timezone, timedelta
import fcntl
import json
from pathlib import Path
import sys

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
from core import db
from collectors import ozon_card_status as detector
from tools import ozon_card_push as push, ozon_card_journal as journal

RUNTIME=BASE/'runtime/ozon_card_pusher'
LOCK=BASE/'backups/ozon_card_daily.lock'


def settle(account):
    rows=db.query("""SELECT * FROM card_push_log WHERE platform='ozon' AND account=%s
        AND task_id IS NOT NULL AND healed IS NULL ORDER BY id""",(account,))
    if rows:
        if any(row['baseline'].get('class') not in ('A','W') for row in rows):
            journal.pause('missing_verification_baseline:'+account)
            return False
        meta={r['offer_id']:(r['baseline'].get('class','A'),set(r['baseline'].get('codes',[]))) for r in rows}
        _,alarm=push._verify(push._headers(account),account,rows,meta)
        if alarm:
            journal.pause(account+':'+alarm)
            return False
        batches=list({r['baseline']['batch_key'] for r in rows if r['baseline'].get('batch_key')})
        if batches:
            checks=db.query("""SELECT baseline->>'batch_key' batch_key,count(*) n,
                count(*) FILTER(WHERE healed) healed,
                bool_or(healed IS NULL) pending FROM card_push_log
                WHERE account=%s AND baseline->>'batch_key'=ANY(%s) GROUP BY 1""",(account,batches))
            for check in checks:
                if not check['pending'] and check['n']>=push.GATE_N and check['healed']/check['n']<push.GATE_MIN_HEALED:
                    journal.pause('gate_failed:'+account)
                    return False
    unknown=db.query("""SELECT count(*) n FROM card_push_log WHERE platform='ozon' AND account=%s
        AND task_status IN ('prepared','uncertain')""",(account,))[0]['n']
    if unknown:
        journal.pause('unconfirmed_submission:'+account)
        return False
    pending=db.query("""SELECT count(*) n FROM card_push_log WHERE platform='ozon' AND account=%s
        AND task_id IS NOT NULL AND healed IS NULL""",(account,))[0]['n']
    if pending:
        return False
    return True


def main(scan_only=False):
    RUNTIME.mkdir(parents=True,exist_ok=True)
    LOCK.parent.mkdir(exist_ok=True)
    with LOCK.open('a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            print('Ozon card worker already running')
            return {'busy':True}
        result={'started_at':datetime.now(timezone.utc).isoformat(),'accounts':{}}
        errors=False
        for account in detector.ACCOUNTS:
            try:
                states=db.query('SELECT * FROM card_scan_state WHERE account=%s',(account,))
                if scan_only or not states or not states[0]['success'] or (
                        datetime.now(timezone.utc)-states[0]['completed_at']>timedelta(hours=2)):
                    detector.main(['ozon_card_status.py',account])
                if scan_only:
                    result['accounts'][account]={'scan':'complete'}
                    continue
                if not settle(account) or journal.control()['paused_reason']:
                    result['accounts'][account]={'paused':journal.control()['paused_reason'] or 'pending_verification'}
                    continue
                # Восстановление касается повторов существующих значений A/W.
                # Исторические разовые напоминания и изменения контента здесь не запускаются.
                output=push.run(account,500,True) or {'sent':0,'healed':0}
                output['legacy_waiting']=db.query("""SELECT count(*) n FROM card_status
                    WHERE platform='ozon' AND account=%s AND is_open AND history_missing
                    AND err_class IN ('A','W') AND attempts=0""",(account,))[0]['n']
                result['accounts'][account]=output
                if output.get('stop_reason') or output.get('gate_failed'):
                    journal.pause(account+':'+str(output.get('stop_reason') or 'gate_failed'))
            except Exception as error:
                errors=True
                result['accounts'][account]={'error_kind':type(error).__name__}
                print(account,'failed:',type(error).__name__,flush=True)
                if not scan_only:
                    try:
                        unknown=db.query("""SELECT count(*) n FROM card_push_log WHERE platform='ozon'
                            AND account=%s AND task_status IN ('prepared','uncertain')""",(account,))[0]['n']
                        if unknown:
                            journal.pause('unconfirmed_submission:'+account)
                    except Exception:
                        pass
        result['finished_at']=datetime.now(timezone.utc).isoformat()
        result['failed']=errors
        target=RUNTIME/('scan_latest.json' if scan_only else 'push_latest.json')
        temp=target.with_suffix('.tmp');temp.write_text(json.dumps(result,ensure_ascii=False,indent=2));temp.replace(target)
        print(json.dumps(result,ensure_ascii=False),flush=True)
        if errors:
            raise RuntimeError('Ozon card worker failed; see sanitized run state')
        return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--scan-only',action='store_true')
    main(parser.parse_args().scan_only)
