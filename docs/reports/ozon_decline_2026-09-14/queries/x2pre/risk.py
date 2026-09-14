import pickle, pandas as pd, numpy as np, importlib.util, sys
X='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/x2pre/'
E=pickle.load(open(X+'elig_final.pkl','rb')); B=E['B']; POUT=E['POUT']
B['eligible_th1_strict']=(B.sgl_units8>=1)&B.c_cost&B.c_stock&B.c_contrib&B.c_noexp_strict
pickle.dump(E|{'B':B},open(X+'elig_final.pkl','wb'))
B[['base','offer_id','sku','eligible_th1_strict']].to_csv(X+'x2_bases_th1strict_flag.csv',index=False)
VIEWS=26; CTR=0.009; CPC=34.2; D=28
for u in ('eligible_strict','eligible_E8stratum','eligible_th1_strict','eligible_th1_E8stratum'):
    e=B[B[u]]; nT=round(len(e)/2)
    spend_fam=VIEWS*CTR*CPC*D; clicks=VIEWS*CTR*D
    o_lo,o_hi=clicks*0.055,clicks*0.145
    pre=(e.x2_contrib_pre_ads).median(); price=e.x2_price.median()
    sgl_contrib28=((e.sgl_rev8*POUT - e.sgl_units8*e.uc_cons)/2).clip(lower=0)
    cann_worst=0.64*sgl_contrib28.mean()*nT; cann_mid=0.31*sgl_contrib28.mean()*nT
    canc=e.x2_cancel8.sum()/max((e.x2_orders8+e.x2_cancel8).sum(),1)
    print(u,'N',len(e),'nT',nT,'in_camp',int(e.in_35269713.sum()),'E8single',int(e.ov_E8_single.sum()),'| spend/fam/28d',round(spend_fam),'week T',round(spend_fam/4*nT),
      '| orders/fam/28d',round(o_lo,2),round(o_hi,2),'rev/fam',round(o_lo*price),round(o_hi*price),'contrib net/fam',round(o_lo*pre-spend_fam),round(o_hi*pre-spend_fam),
      '| sgl contrib28/fam',round(sgl_contrib28.mean()),'cann worst T 28d',round(cann_worst),'mid',round(cann_mid),'| x2 cancel%',round(100*canc,1),'fam_rev28 mean',round(e.fam_rev8.mean()/2),'uc_disagree',int(e.uc_disagree.sum()),'mbs',int((e.uc_src=='margin_by_sku').sum()))
