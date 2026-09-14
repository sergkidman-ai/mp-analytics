# eligibility decision + funnel under thresholds (read-only)
import pickle, pandas as pd, numpy as np
X='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/x2pre/'
S=pickle.load(open(X+'elig_stage.pkl','rb')); B=S['B'].copy(); K=pickle.load(open(X+'consts.pkl','rb'))
PR=K['PR']; POUT=float(min(PR.loc[1,'payout'],PR.loc[2,'payout']))
BID_T=30.0; CPC_T=34.2; CR_LO,CR_HI=0.055,0.145     # CPC at ~30 bid bin (single cartridges 25-40: 34.18)
B['uc_cons']=B[['uc_mbs','uc_ms']].max(axis=1)
B['uc_disagree']=(B.uc_ratio_mbs_ms<0.7)|(B.uc_ratio_mbs_ms>1.43)
B['x2_net']=B.x2_price*POUT
B['x2_contrib_pre_ads']=B.x2_net-2*B.uc_cons
B['adcost_per_order_lo']=CPC_T/CR_HI; B['adcost_per_order_hi']=CPC_T/CR_LO
B['x2_contrib_worst']=B.x2_contrib_pre_ads-B.adcost_per_order_hi
B['x2_contrib_best']=B.x2_contrib_pre_ads-B.adcost_per_order_lo
B['c_active']=True
B['c_cost']=B.uc_cons>0
B['c_stock_link']=B.ms_avail.notna()
B['c_stock']=B.c_stock_link&(B.stock_units>=np.maximum(B.need_units,2))
B['c_contrib']=B.c_cost&(B.x2_contrib_worst>0)
B['c_noexp_strict']=~B.ov_x2_or_single
fam_ov=B[[c for c in B.columns if c.startswith('ov_') and c.endswith('_family')]].any(axis=1)
B['c_noexp_family']=~fam_ov
B['c_noexp_E8stratum']=~B[['ov_E5_x2','ov_E5_single','ov_E7_x2','ov_E7_single','ov_H001_x2','ov_H001_single','ov_E8_x2']].any(axis=1)
rows=[]
for th in (1,2,4,8):
    s=B.sgl_units8>=th
    r={'порог_одиночки_8нед':th,'базы_с_X2':len(B),'продажи':int(s.sum())}
    s=s&B.c_cost; r['+себест']=int(s.sum())
    s=s&B.c_stock; r['+остаток']=int(s.sum())
    s=s&B.c_contrib; r['+contrib>0 (худший)']=int(s.sum())
    r['+без E5/E7/E8/H001 (X2|одиночка)']=int((s&B.c_noexp_strict).sum())
    r['+без пересечений по семье']=int((s&B.c_noexp_family).sum())
    r['E8 как страта (искл. E5/E7/H001)']=int((s&B.c_noexp_E8stratum).sum())
    r['из них в 35269713 (strict)']=int((s&B.c_noexp_strict&B.in_35269713).sum())
    r['5631 в строгом']=bool((s&B.c_noexp_strict&B.is_5631).any())
    rows.append(r)
F=pd.DataFrame(rows); F.to_csv(X+'eligibility_funnel.csv',index=False)
# chosen rule: threshold 2, E8 -> stratum (strict set reported alongside)
TH=2
B['eligible_strict']=(B.sgl_units8>=TH)&B.c_cost&B.c_stock&B.c_contrib&B.c_noexp_strict
B['eligible_E8stratum']=(B.sgl_units8>=TH)&B.c_cost&B.c_stock&B.c_contrib&B.c_noexp_E8stratum
B['eligible_th1_E8stratum']=(B.sgl_units8>=1)&B.c_cost&B.c_stock&B.c_contrib&B.c_noexp_E8stratum
keep=['base','offer_id','sku','sgl_units8','sgl_rev8','fam_cart8','fam_rev8','x2_orders8','x2_cancel8','ms_avail','fbo_avail','stock_units','need_units',
      'uc_mbs','uc_ms','uc_src','uc_disagree','x2_price','x2_contrib_pre_ads','x2_contrib_worst','x2_contrib_best','in_35269713','x2_other_camps','fam_skus_in_other_camps',
      'ov_E5_x2','ov_E5_single','ov_E7_x2','ov_E7_single','ov_E8_x2','ov_E8_single','ov_E8_family','ov_H001_x2','ov_H001_single','is_5631',
      'c_cost','c_stock_link','c_stock','c_contrib','eligible_strict','eligible_E8stratum','eligible_th1_E8stratum']
B[keep].to_csv(X+'x2_bases_all.csv',index=False)
pickle.dump(dict(B=B,POUT=POUT),open(X+'elig_final.pkl','wb'))
print(F.T.to_csv(sep='|',header=False))
for c in ('eligible_strict','eligible_E8stratum','eligible_th1_E8stratum'):
    e=B[B[c]]; print(c,len(e),'in_camp',int(e.in_35269713.sum()),'5631',bool(e.is_5631.any()),'uc_mbs',int((e.uc_src=='margin_by_sku').sum()),'uc_disagree',int(e.uc_disagree.sum()),
      'contrib_worst med',round(e.x2_contrib_worst.median()),'x2_price med',round(e.x2_price.median()),'fam_rev8 sum',round(e.fam_rev8.sum()))
b=B[B.is_5631]; print('5631:', b[['sgl_units8','fam_cart8','stock_units','need_units','uc_src','x2_contrib_worst','in_35269713','ov_E8_single','ov_H001_single']].round(0).to_dict('records'))
print('POUT',round(POUT,3))
