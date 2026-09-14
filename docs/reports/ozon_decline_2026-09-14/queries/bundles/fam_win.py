import sys; sys.path.insert(0,'/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/bundles')
from prep import *
import pickle
O='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/bundles/'
M=pickle.load(open(O+'data/main.pkl','rb')); off=M['off']; F=M['fullP']; W=M['W']
pd.set_option('display.width',250)
BR=['F+ Imaging','Konica Minolta','HP','Hewlett','Canon','Brother','Xerox','Kyocera','Ricoh','Epson','Lexmark','Samsung','OKI','Pantum','Sharp','Panasonic','Toshiba','Riso','Develop','Katusha','Sindoh','Dell','Avision','Olivetti','Utax','Triumph-Adler','Minolta','Deli','G&G','Катюша','Cactus','Gestetner','Lanier','Infotec','Nashuatec','Savin','Duplo','Philips','DEXP','Muratec','Fuji Xerox','Fujifilm','Bizhub','Oce','Designjet']
def brand_series(name):
    if not isinstance(name,str): return ('?','?')
    m=re.search(r'\bдля\b(.*)$',name,re.I); tail=m.group(1) if m else name
    tail=re.sub(r'^\s*(лазерных|струйных)?\s*(принтеров|принтера|МФУ|копиров|аппаратов)\s*(и\s+МФУ)?\s*','',tail,flags=re.I)
    for b in BR:
        i=tail.lower().find(b.lower())
        if i!=-1 and i<25:
            rest=tail[i+len(b):].strip().split()
            s=rest[0] if rest else ''
            s=re.sub(r'[\d,.;()]+.*$','',s)
            bb={'Hewlett':'HP','Minolta':'Konica Minolta','Bizhub':'Konica Minolta'}.get(b,b)
            return (bb, (bb+' '+s.upper()).strip() if len(s)>=2 else bb)
    return ('прочие','прочие')
bn=off[off.n==1].drop_duplicates(['account','base']).set_index(['account','base']).name
F['base_name']=[bn.get((a,b)) for a,b in zip(F.account,F.base)]
F[['brand','series']]=F.base_name.apply(lambda s: pd.Series(brand_series(s)))
F['is_bundle']=F.n>1
fam=F[F.is_bundle].groupby(['account','brand','series']).agg(bases=('base','nunique'),bundle_orders=('orders','sum'),bundle_cart=('cart_qty','sum'),bundle_rev=('rev','sum'),bundle_ads=('money_spent','sum'),bundle_contrib=('contrib_est','sum')).reset_index()
sg=F[~F.is_bundle].groupby(['account','series']).agg(single_orders=('orders','sum'),single_contrib=('contrib_est','sum'))
fam=fam.merge(sg,on=['account','series'],how='left').sort_values('bundle_contrib',ascending=False)
fam['bundle_share_orders_%']=100*fam.bundle_orders/(fam.bundle_orders+fam.single_orders)
fam.to_csv(O+'families_series.csv',index=False)
print('unclassified bundle rev share', round(F[F.is_bundle&(F.brand=='прочие')].rev.sum()/F[F.is_bundle].rev.sum(),3))
print(fam.head(10).round(0).to_string(index=False))
br=F[F.is_bundle].groupby('brand').agg(o=('orders','sum'),c=('contrib_est','sum')).sort_values('c',ascending=False)
print(br.head(6).round(0).to_dict())

# ---------- winners ----------
Wf=W[W.k<=4]  # 5 full weeks 08.08-11.09
wb=Wf.groupby(['account','offer_id','base','n','grp','k']).contrib_est.sum().unstack('k').fillna(0)
posw=(wb>0).sum(axis=1).rename('pos_weeks')
sku=F.set_index(['account','offer_id','base','n','grp'])[['orders','rev','money_spent','contrib_est','cart_qty','cancels','unit_cost']].join(posw).reset_index()
b=sku[sku.n>1]
print('bundle SKU distribution orders>=1', (b.orders>=1).sum(), '>=3', (b.orders>=3).sum(), '>=5', (b.orders>=5).sum(), ' pos_weeks>=3', (b.pos_weeks>=3).sum(), ' pos>=2', (b.pos_weeks>=2).sum())
# base level (all bundles of a base)
bw=Wf[Wf.n>1].groupby(['account','base','k']).contrib_est.sum().unstack('k').fillna(0)
bb=F[F.n>1].groupby(['account','base']).agg(orders=('orders','sum'),rev=('rev','sum'),ads=('money_spent','sum'),contrib=('contrib_est','sum'),cart=('cart_qty','sum'),in_camp=('grp',lambda s:(s=='в 35269713').any())).join((bw>0).sum(axis=1).rename('pos_weeks')).reset_index()
fw=Wf.groupby(['account','base','k']).contrib_est.sum().unstack('k').fillna(0)   # family incl single
bb=bb.join(((fw>0).sum(axis=1)).rename('fam_pos_weeks'),on=['account','base'])
did=pd.read_csv(O+'did_families.csv',index_col=0,dtype={0:str}); did.index=did.index.astype(str)
bb['fam_pre_b']=bb.base.map(did.fam_pre_b); bb['fam_post']=bb.base.map(did.fam_post); bb['sgl_pre_b']=bb.base.map(did.sgl_pre_b); bb['sgl_post']=bb.base.map(did.sgl_post)
bb['non_cannib']=(bb.fam_post>=bb.fam_pre_b)&(bb.sgl_post>=0.7*bb.sgl_pre_b)
# stock scalability
st=L('stock'); last=st[st.d==st.d.max()].set_index('external_code').avail
p=L('post'); p=p[(p.status!='cancelled')&(p.d>='2026-08-08')&(p.d<='2026-09-13')]
p[['base','n']]=p.offer_id.apply(lambda o: pd.Series(split(o)))
wkd=(p.assign(c=p.qty*p.n).groupby(['account','base']).c.sum()/37*7)
bb['fam_cart_week']=[wkd.get((a,x),0) for a,x in zip(bb.account,bb.base)]
bb['base_avail_1309']=bb.base.map(last)
bb['weeks_cover_x1.2']=bb.base_avail_1309/(bb.fam_cart_week*1.2).replace(0,np.nan)
bb['stock_ok']=bb['weeks_cover_x1.2']>=4
bb.to_csv(O+'bases_bundle_econ.csv',index=False)
print('bases with bundle orders', (bb.orders>0).sum(), 'orders>=3', (bb.orders>=3).sum(), 'orders>=5', (bb.orders>=5).sum(), 'pos_weeks>=3', (bb.pos_weeks>=3).sum())
cand=bb[(bb.orders>=3)].sort_values('contrib',ascending=False)
print(cand[['account','base','in_camp','orders','rev','ads','contrib','pos_weeks','fam_pos_weeks','fam_pre_b','fam_post','non_cannib','weeks_cover_x1.2']].round(1).head(20).to_string(index=False))
# stock scalability by size (bases active: orders>0 or in campaign)
F2=F.merge(bb[['account','base','weeks_cover_x1.2','base_avail_1309','fam_cart_week']],on=['account','base'],how='left')
act=F2[(F2.n>1)&((F2.orders>0)|(F2.grp=='в 35269713'))]
act['bundle_cover_w']=(act.base_avail_1309/act.n)/(act.orders/37*7*1.2).replace(0,np.nan)
print(act.groupby(['account','n']).agg(skus=('offer_id','size'),no_stock_data=('base_avail_1309',lambda s:s.isna().sum()),fam_cover_lt4=('weeks_cover_x1.2',lambda s:(s<4).sum()),avail0=('base_avail_1309',lambda s:(s<1).sum()),bundle_lt_n=('base_avail_1309',lambda s:0)).to_string())
act['lt_n']=act.base_avail_1309<act.n
print('bundle skus active where base avail < n (cannot assemble 1 set):', act.groupby('n').lt_n.sum().to_dict())
act.to_csv(O+'bundle_stock_active.csv',index=False)
