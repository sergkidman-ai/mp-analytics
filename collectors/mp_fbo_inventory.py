# поток: fin
"""Independent immutable FBO inventory sources for both organizations."""
import json
from collectors import wb_fbo_intraday as wb, ozon_fbo_intraday as ozon


def main():
    result={}
    for source in (wb,ozon):
        for account in source.ACCOUNTS:
            try:
                result[account]=source.collect(account)
            except Exception as exc:
                # Exception text can contain authenticated request URLs.
                result[account]={'error':type(exc).__name__}
            print(json.dumps({account:result[account]},ensure_ascii=False),flush=True)
    return int(any(v.get('error') for v in result.values()))


if __name__=='__main__':
    raise SystemExit(main())
