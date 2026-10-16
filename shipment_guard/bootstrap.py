# поток: fin
"""Explicit first checkpoint and verified expense snapshot; never edits historic dashboards."""
import argparse
import hashlib
import json
from datetime import date,datetime,timedelta,timezone
from pathlib import Path
from shipment_guard import store,config
from shipment_guard.economics import number

def seed_rules(source_root):
    root=Path(source_root)
    rules=[]
    for platform,name in [('wb','wb'),('ozon','ozon')]:
        path=root/'reports/data'/f'mp_{name}_hist.json'
        raw=path.read_bytes(); data=json.loads(raw); i=data['months'].index('Авг')
        for account,a in data['accounts'].items():
            L=a['lines']
            if platform=='ozon':
                rate=sum(number(L[k][i]) for k in ['commission','delivery','partners','fbo','promo','penalty','unclassified'])/number(L['sales'][i])
            else:
                other=sum(number(L[k][i]) for k in ['delivery','storage','acceptance','ads','points','penalty','other'])
                revenue=number(L['own_price'][i]); net=number(L['to_pay'][i])-other+number(L.get('compensation',[0]*len(data['months']))[i])
                rate=(revenue-net-number(L.get('returns_pay',[0]*len(data['months']))[i]))/revenue
            rules.append((account,platform,rate,date(2026,8,1),date(2026,8,31),str(path),hashlib.sha256(raw).hexdigest()))
    path=root/'docs/reports/yandex_weekly_aug_sep_2026/checks.json'; raw=path.read_bytes()
    checks=json.loads(raw)
    rate=number(checks['full_month_retention'])
    if not .66<float(rate)<.68: raise ValueError('verified Market September baseline changed')
    rules.append(('ya_acc1','yandex',rate,date(2026,9,1),date(2026,9,30),str(path),hashlib.sha256(raw).hexdigest()))
    with store.transaction() as c:
        for account,platform,rate,start,end,path,digest in rules:
            oid=next(k for k,v in config.ORGS.items() if v[0]==account[-1])
            c.execute("""INSERT INTO ms_shipment_rules(account,platform,organization_id,retention,base_from,base_to,valid_until,source,source_sha256,version)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(account) DO NOTHING""",
              (account,platform,oid,rate,start,end,end+timedelta(days=60),path,digest,'promo-absolute-300-or-10pct-v1:'+digest[:12]))
    return len(rules)


def main():
    p=argparse.ArgumentParser(); p.add_argument('--source-root',default='/opt/mp-analytics'); p.add_argument('--initial-hours',type=int,default=24); a=p.parse_args()
    count=seed_rules(a.source_root)
    with store.transaction() as c:
        c.execute('INSERT INTO ms_shipment_sync_state(account_id,checkpoint) VALUES(%s,%s) ON CONFLICT DO NOTHING',
          (config.account_id(),datetime.now(timezone.utc)-timedelta(hours=a.initial_hours)))
    print('Rules checked:',count,'; checkpoint initialized only if absent')
if __name__=='__main__': main()
