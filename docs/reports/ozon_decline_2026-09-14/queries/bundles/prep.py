import pandas as pd, numpy as np, re
D='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/bundles/data'
import warnings; warnings.filterwarnings('ignore')
def L(n):
    x=pd.read_pickle(f'{D}/{n}.pkl')
    for col in ('d','first_d','last_d','period_start','period_end','period_from'):
        if col in x.columns: x[col]=pd.to_datetime(x[col])
    for col in ('sku',):
        if col in x.columns: x[col]=x[col].astype(str)
    for col in x.columns:
        if x[col].dtype==object and col not in ('sku','offer_id','name','account','campaign_id','title','status','scheme','posting_number','op','optype','external_code','base'):
            try: x[col]=pd.to_numeric(x[col])
            except Exception: pass
    return x
RX=re.compile(r'^(.+?)X(2|4|6|8|10)$')
def split(o):
    if not isinstance(o,str): return (None,None)
    m=RX.match(o)
    return (m.group(1), int(m.group(2))) if m else (o,1)

prod=L('prod')
prod[['base','n']]=prod.offer_id.apply(lambda o: pd.Series(split(o)))
bund=prod[prod.n>1]
bases_by_acc={a:set(g.base) for a,g in bund.groupby('account')}
prod['is_family']=prod.apply(lambda r: r.base in bases_by_acc.get(r.account,()), axis=1)
# sku->offer map (per account)
sku2=prod.set_index(['account','sku'])[['offer_id','base','n','name','is_family']]

start=pd.read_csv('/opt/mp-analytics/docs/reports/ozon_acc1_bundles_start.csv', dtype=str)
start['base_s']=start.bundle.str.replace(r'X(2|4|6|8|10)$','',regex=True)
LIST193=set(start.base_s)

bids=L('bids')
camp=bids[bids.campaign_id=='35269713'].merge(prod[['account','sku','offer_id','base','n']], on=['account','sku'], how='left')
CAMP_SKU=set(camp.sku)
CAMP_BASES=set(camp.base.dropna())
# other campaigns with bundle skus
bb=bids.merge(prod[['account','sku','base','n']], on=['account','sku'], how='left')
OTHER_BUNDLE_CAMPS=bb[(bb.n>1)&(bb.campaign_id!='35269713')]

def wk(d):
    d=pd.Timestamp(d); k=(d-pd.Timestamp('2026-08-08')).days//7
    return k
