import sys; sys.path.insert(0,'/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/bundles')
from prep import *
O='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/bundles/'
bb=pd.read_csv(O+'bases_bundle_econ.csv',dtype={'base':str})
win=bb[(bb.orders>=5)&(bb.pos_weeks>=3)&bb.non_cannib&bb.stock_ok]
cand=bb[(bb.orders>=3)]
bb.assign(winner=bb.base.isin(win.base)).query('orders>=1').sort_values('contrib',ascending=False).to_csv(O+'winners_candidates.csv',index=False)
print('winners', win.base.tolist(), 'candidates>=3 orders', cand.base.tolist())
F=pd.read_csv(O+'offers_period.csv',dtype={'offer_id':str,'base':str})
st=L('stock'); last=st[st.d==st.d.max()].set_index('external_code').avail
d=pd.read_csv(O+'did_families.csv',index_col=0); d.index=d.index.astype(str)
C1=sorted(LIST193-CAMP_BASES)
x2=prod[(prod.account=='oz_acc1')&(prod.n==2)&(~prod.is_archived)].drop_duplicates('offer_id').set_index('base')
oth=set(OTHER_BUNDLE_CAMPS.sku)
rows=[]
for b in C1:
    if b not in x2.index: continue
    uc=F[(F.base==b)&(F.n==1)].unit_cost.max()
    av=last.get(b,np.nan); famwk=d.fam_post_full.get(b,0)/28*7
    ok=(uc>0) and (av>=max(10,4*1.2*famwk*2)) and (x2.loc[b,'sku'] not in oth)
    rows.append(dict(base=b,x2_offer=x2.loc[b,'offer_id'],x2_sku=x2.loc[b,'sku'],unit_cost=uc,base_avail_1309=av,fam_cart_28d_pre=d.fam_pre.get(b,np.nan),fam_cart_28d_post=d.fam_post.get(b,np.nan),eligible=ok))
E=pd.DataFrame(rows); el=E[E.eligible].copy()
rng=np.random.default_rng(20260914)
el['u']=rng.random(len(el)); el=el.sort_values('u').head(40)
el['arm']=['treat_add_X2']*20+['holdout_permanent']*20
el.drop(columns='u').to_csv(O+'wave1_cohort.csv',index=False)
print('C1 with X2', len(E), 'eligible', int(E.eligible.sum()))
g=el.groupby('arm')[['fam_cart_28d_pre','fam_cart_28d_post','base_avail_1309']].mean().round(2)
g.to_csv(O+'wave1_balance.csv'); print(open(O+'wave1_balance.csv').read())
# expected: campaign X2 stats scaled by relative family volume of C1 vs T
T=d[d.grp=='T']; t_pre=T.fam_pre.mean(); c_pre=el[el.arm=='treat_add_X2'].fam_cart_28d_pre.mean()
camp_x2=F[(F.grp=='в 35269713')&(F.n==2)]
k=c_pre/t_pre
per_card_wk=lambda col: camp_x2[col].sum()/100/37*7
print('scale k', round(k,3))
for col in ('orders','rev','contrib_est','money_spent','orders_qty','orders_money'):
    print(col, 'per card/wk', round(per_card_wk(col),2), '-> 20 cards/wk', round(per_card_wk(col)*20*k,1), '(k=1:', round(per_card_wk(col)*20,1),')')
