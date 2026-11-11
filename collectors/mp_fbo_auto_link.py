# поток: fin
"""Replay local order/inventory evidence for all configured FBO accounts."""
import json
from collectors import wb_fbo_auto_link as wb, ozon_fbo_intraday as ozon


def main():
    result={}
    for source in (wb,ozon):
        for account in source.ACCOUNTS:
            try:
                result[account]=source.run(account)
            except Exception as exc:
                result[account]={'error':type(exc).__name__}
            print(json.dumps({account:result[account]},ensure_ascii=False),flush=True)
    try:
        from collectors.fbo_traceability import run
        result['trace_history']=run(proof_limit=0)
    except Exception as exc:
        result['trace_history']={'error':type(exc).__name__}
    print(json.dumps({'trace_history':result['trace_history']},ensure_ascii=False),flush=True)
    return int(any(v.get('error') for v in result.values()))


if __name__=='__main__':
    raise SystemExit(main())
