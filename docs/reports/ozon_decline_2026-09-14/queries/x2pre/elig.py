# X2 preflight: eligibility (read-only, pickles only)
import sys, re, csv, glob, pickle
sys.path.insert(0,'/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/bundles')
from prep import L, split
import pandas as pd, numpy as np
X='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/x2pre/'
ACC='oz_acc1'; CAMP='35269713'
D8_0, D8_1 = pd.Timestamp('2026-07-19'), pd.Timestamp('2026-09-13')   # 8 weeks = 56 days
TX0, TX1 = pd.Timestamp('2026-07-13'), pd.Timestamp('2026-09-07')     # 8 weeks of transactions up to 07.09

prod=L('prod'); prod=prod[prod.account==ACC].copy()
prod[['base','n']]=prod.offer_id.apply(lambda o: pd.Series(split(o)))
prod['fam']=prod.offer_id.str[:4]
offers=prod.sort_values('is_archived').drop_duplicates('offer_id')     # non-archived first
sku2off=prod.set_index('sku').offer_id.to_dict()

price=pd.read_pickle(X+'price_last.pkl'); price['sku']=price.sku.astype(str)
price=price.sort_values('collected_on').drop_duplicates('offer_id',keep='last')
priced=set(price[(price.price>0)].offer_id)  # price-index sku is a different id -> join by offer_id

# ---------- X2 cards ----------
x2=prod[(prod.n==2)].copy()
x2['active']=(~x2.is_archived)&x2.offer_id.isin(priced)
x2a=x2[x2.active].drop_duplicates('offer_id')
x2a['base_len4']=x2a.base.str.len()==4
single_active=set(offers[(offers.n==1)&(~offers.is_archived)].offer_id)
x2a['has_single']=x2a.base.isin(single_active)
funnel={'x2_offers_all':x2.offer_id.nunique(),'x2_active(не архив+цена)':len(x2a),'база 4 знака':int(x2a.base_len4.sum()),'есть активная одиночка':int((x2a.base_len4&x2a.has_single).sum())}
B=x2a[x2a.base_len4&x2a.has_single].copy()

# ---------- posts ----------
p=L('post'); p=p[(p.account==ACC)].copy()
p['fam']=p.offer_id.str[:4]
p[['b','n']]=p.offer_id.apply(lambda o: pd.Series(split(o)))
p['cart']=p.qty*p.n; p['rev']=p.qty*p.price
pc=p[p.status!='cancelled']
w8=pc[(pc.d>=D8_0)&(pc.d<=D8_1)]
fam8=w8.groupby('fam').agg(fam_cart8=('cart','sum'),fam_rev8=('rev','sum'))
sgl8=w8[w8.offer_id==w8.fam].groupby('fam').agg(sgl_units8=('qty','sum'),sgl_rev8=('rev','sum'))
x2o=w8[w8.n==2].groupby('offer_id').agg(x2_orders8=('posting_number','nunique'),x2_rev8=('rev','sum'),x2_qty8=('qty','sum'))
canc=p[(p.d>=D8_0)&(p.d<=D8_1)&(p.n==2)].groupby('offer_id').apply(lambda g:(g.status=='cancelled').sum()).rename('x2_cancel8')
B=B.merge(fam8,left_on='base',right_index=True,how='left').merge(sgl8,left_on='base',right_index=True,how='left')
B=B.merge(x2o,left_on='offer_id',right_index=True,how='left').merge(canc,left_on='offer_id',right_index=True,how='left')
for c_ in ('fam_cart8','fam_rev8','sgl_units8','sgl_rev8','x2_orders8','x2_rev8','x2_qty8','x2_cancel8'): B[c_]=B[c_].fillna(0)

# ---------- stock ----------
fbo=pd.read_pickle(X+'fbo_last.pkl'); fbo['sku']=fbo.sku.astype(str)
fbo['offer_id']=fbo.sku.map(sku2off); fbo_off=fbo.groupby('offer_id').fbo_free.sum()
ss=pd.read_pickle(X+'sstock.pkl'); ss['d']=pd.to_datetime(ss.d)
last_d=ss.d.max(); ss_last=ss[ss.d==last_d].set_index('external_code').avail
ever=set(ss.external_code)
def ms_av(b):
    if b in ss_last.index: return float(ss_last[b])
    return 0.0 if b in ever else np.nan
B['ms_avail']=B.base.map(ms_av); B['fbo_avail']=B.base.map(fbo_off).fillna(0)
B['stock_units']=B.ms_avail+B.fbo_avail
B['need_units']=2*(B.fam_cart8/2*1.5)          # 2 x (expected family cartridges per 28d x 1.5)

# ---------- unit cost: margin_by_sku implied, fallback MS ----------
mbs=L('mbs'); mbs=mbs[(mbs.account==ACC)&(mbs.period_from>='2026-07-01')].copy(); mbs['offer_id']=mbs.sku.map(sku2off)
tx=L('tx'); tx=tx[tx.account==ACC].copy(); tx['offer_id']=tx.sku.map(sku2off)
txm=tx[tx.d>='2026-07-01']
units=txm.groupby('offer_id').apply(lambda g:(g.accr>0).sum()-(g.accr<0).sum()).rename('net_units')
cg=mbs.groupby('offer_id').cogs.sum()
mb_uc=(cg/units.reindex(cg.index)).where(units.reindex(cg.index)>=1)
ms_uc=ss[ss.d>=last_d-pd.Timedelta(days=28)].groupby('external_code').cost_seb_med.median()
B['uc_mbs']=B.base.map(mb_uc); B['uc_ms']=B.base.map(ms_uc)
B['unit_cost']=B.uc_mbs.where(B.uc_mbs>0, B.uc_ms)
B['uc_src']=np.where(B.uc_mbs>0,'margin_by_sku',np.where(B.uc_ms>0,'МС supplier_stock (fallback)','нет'))
B['uc_ratio_mbs_ms']=B.uc_mbs/B.uc_ms

# ---------- payout ratio from transactions (8 weeks to 07.09) ----------
t8=tx[(tx.d>=TX0)&(tx.d<=TX1)].copy()
for c_ in ('accr','comm','amount','deliv','serv'): t8[c_]=t8[c_]/t8.nitems
t8[['b','n']]=t8.offer_id.apply(lambda o: pd.Series(split(o)))
def pool(g): return pd.Series(dict(accr_pos=g.accr[g.accr>0].sum(),amount=g.amount.sum(),sales=(g.accr>0).sum(),comm=g.comm.sum()))
PR=t8.groupby('n').apply(pool); PR['payout']=PR.amount/PR.accr_pos; PR['comm_rate']=-PR.comm/PR.accr_pos
PR['nonpay_abs_per_sale']=(PR.accr_pos-PR.amount)/PR.sales
PR.to_csv(X+'payout_pools.csv')
POUT2=float(PR.loc[2,'payout']); NONPAY2=float(PR.loc[2,'nonpay_abs_per_sale'])
# price for X2: seller price; calibrate posting price vs price index
pr_off=price.set_index('offer_id')
B['x2_price_idx']=B.offer_id.map(pr_off.marketing_seller_price).where(lambda s:s>0, B.offer_id.map(pr_off.price))
recent=pc[(pc.d>='2026-08-15')&(pc.n==2)].groupby('offer_id').price.median()
cal=(recent/B.set_index('offer_id').x2_price_idx.reindex(recent.index)).dropna()
CAL=float(cal.median()) if len(cal) else 1.0
B['x2_price']=B.x2_price_idx*CAL

# ---------- ads economics at floor (campaign facts) ----------
ads=L('ads'); ads=ads[(ads.account==ACC)&(ads.campaign_id==CAMP)]
CPC_FLOOR=float(ads.money_spent.sum()/max(ads.clicks.sum(),1))
pickle.dump(dict(POUT2=POUT2,NONPAY2=NONPAY2,CAL=CAL,CPC_FLOOR=CPC_FLOOR,PR=PR),open(X+'consts.pkl','wb'))

# ---------- overlaps ----------
def skus(pattern, flagcol=None):
    s=set()
    for f in glob.glob(pattern):
        for r in csv.DictReader(open(f)):
            if flagcol and str(r.get(flagcol,'1')) not in ('1','true','True'): continue
            s.add(str(r['sku']))
    return s
C='/opt/mp-analytics/docs/experiments/cohorts/'
OV={'E5':skus(C+'E5_*.csv'),'E7':skus(C+'E7_*.csv'),'E8':skus(C+'E8_*.csv')|skus('/opt/mp-analytics/docs/reports/ozon_wave1_restore_oz_acc1_2026-08-19.csv'),
    'H001':skus(C+'HALO_CORE_A_*.csv','в_когорте')|skus(C+'HALO_BASELINE_*.csv','в_когорте')|skus(C+'HALO_PERSISTENT_*.csv','в_когорте')}
fam_skus=prod.groupby('fam').sku.apply(set)
sgl_skus=prod[prod.n==1].groupby('offer_id').sku.apply(set)
for k,S in OV.items():
    B[f'ov_{k}_x2']=B.sku.isin(S)
    B[f'ov_{k}_single']=B.base.map(lambda b: bool(sgl_skus.get(b,set())&S))
    B[f'ov_{k}_family']=B.base.map(lambda b: bool(fam_skus.get(b,set())&S))
B['ov_x2_or_single']=B[[f'ov_{k}_{w}' for k in OV for w in ('x2','single')]].any(axis=1)
B['ov_E8_only_single']=B.ov_E8_single & ~B[[f'ov_{k}_{w}' for k in ('E5','E7','H001') for w in ('x2','single')]].any(axis=1) & ~B.ov_E8_x2

# ---------- campaigns ----------
bids=pd.read_pickle(X+'bids_acc1.pkl'); bids['d']=pd.to_datetime(bids.d); bids['sku']=bids.sku.astype(str); bids['campaign_id']=bids.campaign_id.astype(str)
bl=bids[bids.d>=bids.d.max()-pd.Timedelta(days=6)]
inc=set(bl[bl.campaign_id==CAMP].sku)
oth=bl[bl.campaign_id!=CAMP].groupby('sku').campaign_id.nunique()
B['in_35269713']=B.sku.isin(inc); B['x2_other_camps']=B.sku.map(oth).fillna(0).astype(int)
fam_off_adv=bl[bl.campaign_id!=CAMP].assign(fam=lambda x:x.sku.map(sku2off).str[:4]).groupby('fam').sku.nunique()
B['fam_skus_in_other_camps']=B.base.map(fam_off_adv).fillna(0).astype(int)
B['is_5631']=B.base=='5631'
pickle.dump(dict(B=B,OV={k:len(v) for k,v in OV.items()}),open(X+'elig_stage.pkl','wb'))
print('funnel',funnel); print('payout pools n:',PR[['payout','comm_rate','nonpay_abs_per_sale','sales']].round(3).to_dict('index'))
print('CAL price',round(CAL,3),'CPC floor camp',round(CPC_FLOOR,2),'ms last',last_d.date())
print('uc_src',B.uc_src.value_counts().to_dict(),' mbs/ms ratio q',B.uc_ratio_mbs_ms.quantile([.1,.5,.9]).round(2).tolist())
print('stock NaN(no link)',int(B.ms_avail.isna().sum()),' in camp',int(B.in_35269713.sum()),' x2 other camps',int((B.x2_other_camps>0).sum()))
print('overlap x2|single',int(B.ov_x2_or_single.sum()),{k:(int(B[f'ov_{k}_x2'].sum()),int(B[f'ov_{k}_single'].sum()),int(B[f'ov_{k}_family'].sum())) for k in OV})
print('sgl_units8 quantiles', B.sgl_units8.quantile([.25,.5,.75,.9]).tolist(), 'n bases', len(B))
for th in (1,2,4,8): print(' sgl>=',th, int((B.sgl_units8>=th).sum()))
