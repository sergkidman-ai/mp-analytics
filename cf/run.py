# поток: cf
"""Ежедневное обновление существующей витрины Кэшфлоу (05:20 UTC)."""
import argparse
import datetime as dt
import fcntl
import json
import os
import pathlib
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
from cf import balances, plan_seed, build, yandex_netting, mp_balance, ms_payments
from core import db


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--light', action='store_true')
    args = ap.parse_args()
    state_dir = pathlib.Path(os.getenv('CF_RUNTIME_DIR', '/opt/mp-analytics/runtime/cashflow'))
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / 'run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('[skip] Кэшфлоу уже обновляется', flush=True)
            return
        steps = {}
        def step(name, fn):
            try:
                fn()
                steps[name] = 'ok'
            except Exception as exc:
                steps[name] = type(exc).__name__
                print(f'[FAIL] {name}: {type(exc).__name__}', flush=True)
        if not args.light:
            step('bank_balances', lambda: balances.main(['--days', '3']))
            step('ms_payments', ms_payments.collect)
            step('plan_rules', plan_seed.seed)
            step('market_netting', yandex_netting.main)
            step('mp_balances', lambda: mp_balance.main([]))
        if not args.light:
            def check_sources():
                day = dt.date.today()
                bank = db.query("select count(distinct org_inn) n from cf_balance where source='api' and bal_date=%s", (day - dt.timedelta(days=1),))[0]['n']
                mp = db.query("select count(distinct account) n from cf_mp_balance where day=%s and account in ('wb_acc1','wb_acc2','oz_acc1','oz_acc2')", (day,))[0]['n']
                if bank < 2 or mp < 4:
                    raise RuntimeError('Incomplete source refresh')
            step('source_freshness', check_sources)
        step('build', lambda: build.build(snapshot=None))
        state = {'finished_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'steps': steps}
        state_name = 'last_build' if args.light else 'last_run'
        tmp = state_dir / (state_name + '.tmp')
        tmp.write_text(json.dumps(state, ensure_ascii=False))
        tmp.replace(state_dir / (state_name + '.json'))
        if any(v != 'ok' for v in steps.values()):
            raise SystemExit(1)


if __name__ == '__main__':
    main()
