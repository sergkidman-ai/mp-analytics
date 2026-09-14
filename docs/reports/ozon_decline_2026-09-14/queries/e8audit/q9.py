import csv,sys,collections,json
import pandas as pd
sys.path.insert(0,'/opt/mp-analytics')
from core import db
R='/opt/mp-analytics/docs/'
S='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/e8audit/'
rows=list(csv.DictReader(open(R+'reports/ozon_wave1_restore_oz_acc1_2026-08-19.csv',encoding='utf-8')))
grp={r['sku']:r['group'] for r in rows}
A={s for s,g in grp.items() if g=='A'}; B={s for s,g in grp.items() if g=='B'}
out=open(S+'q9_out.txt','w'); det=csv.writer(open(S+'overlaps_detail.csv','w')); det.writerow(['source','group','sku'])
def P(*a):
    s=' '.join(str(x) for x in a); print(s[:300]); out.write(s+'\n')
prod=db.query("select sku::text sku, offer_id, is_archived from ozon_product where account='oz_acc1'")
o2s=collections.defaultdict(set)
for r in prod: o2s[str(r['offer_id'])].add(r['sku'])
arch={r['sku'] for r in prod if r['is_archived']}; known={r['sku'] for r in prod}
P('catalog: A not in ozon_product',len(A-known),'B',len(B-known),'| archived A',len(A&arch),'B',len(B&arch))
def rep(name,skus):
    skus={str(s) for s in skus}
    a=A&skus; b=B&skus
    for s in a: det.writerow([name,'A',s])
    for s in b: det.writerow([name,'B',s])
    P(f'{name}: src={len(skus)} A={len(a)} ({100*len(a)/len(A):.2f}%) B={len(b)} ({100*len(b)/len(B):.2f}%)')
rep('E1_steplog_08-13.08',[r['s'] for r in db.query("select distinct sku::text s from mkt_ozon_bid_step_log where account='oz_acc1' and step_date between '2026-08-08' and '2026-08-13' and applied")])
rep('E4_journal_rollback',[r['s'] for r in db.query("select distinct sku::text s from mkt_ozon_bid_journal where account='oz_acc1' and action='rollback'")])
rep('E4_replay_csv',pd.read_csv(R+'reports/ozon_e4_rollback_replay_2026-08-20.csv',dtype=str).sku)
C=R+'experiments/cohorts/'
for nm in ['E5_treatment','E5_control','E6_treatment','E6_control','E7_treatment']:
    x=pd.read_csv(C+nm+'_2026-08-20.csv',dtype=str); x=x[x.account=='oz_acc1'] if 'account' in x else x
    rep(nm,x.sku)
for nm in ['HALO_CORE_A_STABLE_ADVERTISED','HALO_BASELINE_NEVER_ADVERTISED']:
    x=pd.read_csv(C+nm+'_2026-08-21.csv',dtype=str); col=[c for c in x.columns if c.startswith('в_когорт')]
    rep(nm+'_all',x.sku)
    if col: rep(nm+'_in',x[x[col[0]]=='1'].sku)
bn=pd.read_csv(R+'reports/ozon_acc1_bundles_start.csv',dtype=str)
rep('bundle_X_cards(sku)',bn.sku); rep('bundle_bases',bn.base)
bb=set(); [bb.update(o2s.get(str(b),set())) for b in bn.base]; rep('bundle_bases_via_offer',bb)
rep('camp35269713_after19',[r['s'] for r in db.query("select distinct sku::text s from ozon_bids where account='oz_acc1' and campaign_id::text='35269713' and captured_at>='2026-08-20'")])
rep('camp40385357',[r['s'] for r in db.query("select distinct sku::text s from ozon_bids where account='oz_acc1' and campaign_id::text='40385357'")])
rep('journal_weekly_after19(non-wave1)',[r['s'] for r in db.query("select distinct sku::text s from mkt_ozon_bid_journal where account='oz_acc1' and decided_on>'2026-08-19' and action not like 'wave1%%'")])
act=db.query("select offer_id, ts, ok from oz_action_log where account='oz_acc1' and ts>='2026-08-19'")
s=set(); [s.update(o2s.get(str(r['offer_id']),set())) for r in act]; rep('oz_action_log_after19',s)
act0=db.query("select offer_id from oz_action_log where account='oz_acc1' and ts>='2026-07-27' and ts<'2026-08-19'")
s=set(); [s.update(o2s.get(str(r['offer_id']),set())) for r in act0]; rep('oz_action_log_pre',s)
try:
    cs=db.query("select offer_id, attempts, last_attempt_at from card_status where platform='ozon' and account='oz_acc1' and last_attempt_at>='2026-08-19'")
    s=set(); [s.update(o2s.get(str(r['offer_id']),set())) for r in cs]; rep('card_status_pusher_after19',s)
except Exception as e: P('card_status err',str(e)[:100])
# price raise 02-04.09
pi=db.query("select sku::text s, collected_on d, price from ozon_price_index where account='oz_acc1' and collected_on in ('2026-09-01','2026-09-05')")
pm=collections.defaultdict(dict)
for r in pi: pm[r['s']][str(r['d'])]=float(r['price'] or 0)
up={s for s,v in pm.items() if v.get('2026-09-01') and v.get('2026-09-05') and v['2026-09-05']>v['2026-09-01']*1.05}
dn={s for s,v in pm.items() if v.get('2026-09-01') and v.get('2026-09-05') and v['2026-09-05']<v['2026-09-01']*0.95}
P('price obs 01&05.09: A',len([s for s in A if len(pm.get(s,{}))==2]),'B',len([s for s in B if len(pm.get(s,{}))==2]))
rep('price_up>5%_01->05.09',up); rep('price_down>5%_01->05.09',dn)
# TK xlsx
for fn in ['ozon_card_marking_2026-08-24.xlsx','ozon_card_tnved_2026-08-24.xlsx','ozon_card_hashtags_2026-08-24.xlsx']:
    try:
        x=pd.read_excel(R+'reports/'+fn,dtype=str)
        col=[c for c in x.columns if 'offer' in c.lower() or 'артикул' in c.lower()]
        skc=[c for c in x.columns if c.lower()=='sku']
        s=set()
        if skc: s=set(x[skc[0]].dropna())
        elif col: [s.update(o2s.get(str(v),set())) for v in x[col[0]].dropna()]
        rep('TK_'+fn[:22],s)
    except Exception as e: P(fn,'err',str(e)[:80])
