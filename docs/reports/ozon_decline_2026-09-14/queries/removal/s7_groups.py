import pandas as pd, numpy as np, re
R='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/removal'
D=R+'/data'
out=[]
def P(*a):
    s=' '.join(str(x) for x in a); out.append(s); print(s)
pn=pd.read_pickle(D+'/panel_tx.pkl')
meta=pd.read_pickle(D+'/ab_meta.pkl')
BR=['Konica Minolta','Brother','Epson','HP','Canon','Kyocera','Xerox','Samsung','Ricoh','Pantum','Sharp','OKI','Lexmark','Panasonic','Toshiba','Riso','Develop','Katun','Dell','Sindoh','Avision','Deli','Catalina','Cactus','Lenovo','Philips','Minolta','Olivetti','Utax','Triumph-Adler','Gestetner','Canon/HP','G&G','Zebra','Citizen','Mimaki','Roland','Mutoh','Duplo']
def brand(n):
    n=n or ''
    hits=[(n.find(b),b) for b in BR if re.search(r'(?<![A-Za-z])'+re.escape(b)+r'(?![A-Za-z])',n)]
    return min(hits)[1] if hits else 'прочее'
def ptype(n):
    n=(n or '').lower()
    for k,v in [('комплект','Комплект/набор'),('тонер-картридж','Тонер-картридж'),('драм','Фотобарабан/драм'),('фотобарабан','Фотобарабан/драм'),('барабан','Фотобарабан/драм'),('чернил','Чернила'),('тонер','Тонер'),('картридж','Картридж'),('печатающ','Печатающая головка'),('контейнер','Бункер/контейнер'),('бункер','Бункер/контейнер')]:
        if k in n: return v
    return 'прочее'
def series(n):
    n=n or ''
    m=re.search(r'(?<![A-Za-z0-9])([A-Za-z]{1,5})[-\s]?(\d{2,})',n)
    if m: return m.group(1).upper()
    m=re.search(r'(?<![A-Za-z0-9])(\d{2,}[A-Za-z]?)',n)
    return m.group(1)[:4].upper() if m else ''
meta['brand']=meta.name.map(brand); meta['ptype']=meta.name.map(ptype); meta['series']=meta.name.map(series)
meta['bundle']=meta.offer_id.fillna('').str.contains(r'X\d+$')
pn=pn[pn.grp.isin(['A','B'])]
def agg(a,b,cols):
    return pn[(pn.d>=a)&(pn.d<=b)].groupby('sku')[cols].sum()
pre=agg('2026-07-27','2026-08-08',['views','clicks','spend','ad_units','ad_rev','units','rev'])
pre.columns=['pre_'+c for c in pre.columns]
off=agg('2026-08-10','2026-08-18',['units','rev']).add_prefix('off_')
p1=agg('2026-08-20','2026-09-01',['units','rev','spend','ad_rev']).add_prefix('post1_')
p2=agg('2026-09-05','2026-09-13',['units','rev','spend','ad_rev']).add_prefix('post2_')
# search
se=pd.read_pickle(D+'/search.pkl'); se=se[se.account=='oz_acc1']; se['ps']=pd.to_datetime(se.period_start)
sp=se[se.ps.isin(pd.to_datetime(['2026-07-27','2026-08-03']))].groupby('sku').unique_view_users.sum()/2
so=se[se.ps.isin(pd.to_datetime(['2026-08-24','2026-08-31']))].groupby('sku').unique_view_users.sum()/2
spos_pre=se[se.ps.isin(pd.to_datetime(['2026-07-27','2026-08-03']))].groupby('sku').position.mean()
spos_post=se[se.ps.isin(pd.to_datetime(['2026-08-24','2026-08-31']))].groupby('sku').position.mean()
# stock & price now
ss=pd.read_pickle(D+'/ss.pkl'); ss=ss[pd.to_datetime(ss.d)==pd.Timestamp('2026-09-14')]
off2=meta[['sku','offer_id']].copy(); m=off2.offer_id.fillna('').str.extract(r'^(.+)X(\d+)$')
off2['key']=m[0].fillna(off2.offer_id); off2['mult']=m[1].fillna('1').astype(float)
sn=off2.merge(ss,left_on='key',right_on='external_code',how='left'); sn['free']=np.floor(sn.free.astype(float).fillna(0)/sn.mult)
stock_now=sn.groupby('sku').free.sum()
pr=pd.read_pickle(D+'/price.pkl'); pr['d']=pd.to_datetime(pr.d)
pr=pr.drop(columns=['sku']).merge(meta[['sku','offer_id']],on='offer_id')
pnow=pr[pr.d=='2026-09-14'].drop_duplicates('sku').set_index('sku')[['price','marketing_price']]
p0109=pr[pr.d=='2026-09-01'].drop_duplicates('sku').set_index('sku').price
p0509=pr[pr.d=='2026-09-05'].drop_duplicates('sku').set_index('sku').price
T=meta.set_index('sku').join([pre,off,p1,p2]).fillna({c:0 for c in list(pre.columns)+list(off.columns)+list(p1.columns)+list(p2.columns)})
T['search_views_pre_wk']=sp; T['search_views_post_wk']=so; T['pos_pre']=spos_pre; T['pos_post']=spos_post
T[['search_views_pre_wk','search_views_post_wk']]=T[['search_views_pre_wk','search_views_post_wk']].fillna(0)
T['search_drop_wk']=T.search_views_pre_wk-T.search_views_post_wk
T['stock_now_14.09']=stock_now; T['price_14.09']=pnow.price; T['buyer_price_14.09']=pnow.marketing_price.astype(float)
T['price_chg_01_05.09']=p0509/p0109-1
T['drr_pre_ad']=np.where(T.pre_ad_rev>0,T.pre_spend/T.pre_ad_rev,np.nan)
T['rev_since_10.08']=T.off_rev+pn[(pn.d>='2026-08-19')].groupby('sku').rev.sum().reindex(T.index).fillna(0)
T.to_csv(R+'/p1_sku_AB.csv')
# group level A vs B
G=T.groupby(['brand','grp']).agg(sku=('name','size'),pre_spend=('pre_spend','sum'),pre_views=('pre_views','sum'),rev_since=('rev_since_10.08','sum'),post1_rev=('post1_rev','sum'),post2_rev=('post2_rev','sum'),sv_pre=('search_views_pre_wk','sum'),sv_post=('search_views_post_wk','sum')).unstack('grp')
G.columns=[f'{a}_{b}' for a,b in G.columns]; G=G.fillna(0)
G['sv_chg_A']=G.sv_post_A/G.sv_pre_A.replace(0,np.nan)-1; G['sv_chg_B']=G.sv_post_B/G.sv_pre_B.replace(0,np.nan)-1
G['rev_since_B_minus_A_per_sku']=G.rev_since_B/G.sku_B.replace(0,np.nan)-G.rev_since_A/G.sku_A.replace(0,np.nan)
G=G.sort_values('sku_B',ascending=False); G.round(3).to_csv(R+'/p1_groups_brand.csv')
for b,r in G.head(12).iterrows():
    P(f'{b}: sku A/B {int(r.sku_A)}/{int(r.sku_B)} pre_spend {r.pre_spend_A:.0f}/{r.pre_spend_B:.0f} rev_since10.08 {r.rev_since_A:.0f}/{r.rev_since_B:.0f} searchviews chg A {r.sv_chg_A:+.0%} B {r.sv_chg_B:+.0%}')
G2=T.groupby(['ptype','grp']).agg(sku=('name','size'),rev_since=('rev_since_10.08','sum'),sv_pre=('search_views_pre_wk','sum'),sv_post=('search_views_post_wk','sum')).unstack('grp'); G2.columns=[f'{a}_{b}' for a,b in G2.columns]
G2.round(1).to_csv(R+'/p1_groups_type.csv')
for b,r in G2.fillna(0).iterrows(): P(f'type {b}: sku A/B {int(r.sku_A)}/{int(r.sku_B)} rev_since {r.rev_since_A:.0f}/{r.rev_since_B:.0f} sv A {r.sv_pre_A:.0f}->{r.sv_post_A:.0f} B {r.sv_pre_B:.0f}->{r.sv_post_B:.0f}')
# overall search per group
P('search views/wk A pre->post',round(T[T.grp=='A'].search_views_pre_wk.sum()),round(T[T.grp=='A'].search_views_post_wk.sum()),'B',round(T[T.grp=='B'].search_views_pre_wk.sum()),round(T[T.grp=='B'].search_views_post_wk.sum()))
# candidates B
B=T[T.grp=='B'].copy()
B['rk_rev']=B['rev_since_10.08'].rank(ascending=False)
B['rk_drop']=B.search_drop_wk.rank(ascending=False)
B['ms_row']=B.index.isin(set(sn[sn.external_code.notna()].sku))
cand=B.sort_values(['rev_since_10.08','search_drop_wk'],ascending=[False,False]).head(30)
cols=['ms_row','offer_id','name','brand','ptype','series','bundle','pre_views','pre_clicks','pre_spend','pre_ad_rev','drr_pre_ad','rev_since_10.08','post1_units','post1_rev','post2_units','post2_rev','search_views_pre_wk','search_views_post_wk','search_drop_wk','pos_pre','pos_post','stock_now_14.09','price_14.09','buyer_price_14.09','price_chg_01_05.09']
cand[cols].round(3).to_csv(R+'/p1_top30_candidates_B.csv')
P('B sku with rev since 10.08:',int((B['rev_since_10.08']>0).sum()),'sum',round(B['rev_since_10.08'].sum()),'| top30 in stock now',int((cand['stock_now_14.09']>0).sum()))
P('top30 rev sum',round(cand['rev_since_10.08'].sum()),'share of B',round(cand['rev_since_10.08'].sum()/B['rev_since_10.08'].sum(),3),'ms_row',int(cand.ms_row.sum()),'pre_spend',round(cand.pre_spend.sum()))
for s,r in cand.head(10).iterrows():
    P(f"{s} {r.offer_id} {r.brand}/{r.ptype}/{r.series} rev10.08+ {r['rev_since_10.08']:.0f} sv {r.search_views_pre_wk:.0f}->{r.search_views_post_wk:.0f} pre_spend {r.pre_spend:.0f} drr {r.drr_pre_ad} stock {r['stock_now_14.09']:.0f} msrow {r.ms_row} price {r['price_14.09']} chg {r['price_chg_01_05.09']:.3f}")
open(R+'/s7_out.txt','w').write('\n'.join(out))
