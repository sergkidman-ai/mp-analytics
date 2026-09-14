import pandas as pd
R='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/removal'
c=pd.read_csv(R+'/p1_top30_candidates_B.csv',dtype={'sku':str,'offer_id':str})
L=['| # | sku | offer_id | бренд / тип / серия | выручка с 10.08, ₽ | шт post1+post2 | поиск. показы/нед до→после | расход до снятия, ₽ | ДРР до | остаток МС 14.09 | цена 14.09 | Δцены 01→05.09 |','|---|---|---|---|---|---|---|---|---|---|---|---|']
for i,r in enumerate(c.itertuples(),1):
    st='нет строки МС' if not r.ms_row else int(r._23) if False else None
    stock = 'нет строки МС' if not r.ms_row else f"{int(r[c.columns.get_loc('stock_now_14.09')+1])}"
    drr='—' if pd.isna(r.drr_pre_ad) else f'{r.drr_pre_ad:.0%}'
    pch=r[c.columns.get_loc('price_chg_01_05.09')+1]
    L.append(f"| {i} | {r.sku} | {r.offer_id} | {r.brand} / {r.ptype} / {r.series if isinstance(r.series,str) else ''} | {r[c.columns.get_loc('rev_since_10.08')+1]:,.0f} | {int(r.post1_units+r.post2_units)} | {r.search_views_pre_wk:.0f}→{r.search_views_post_wk:.0f} | {r.pre_spend:.0f} | {drr} | {stock} | {r[c.columns.get_loc('price_14.09')+1]:,.0f} | {'' if pd.isna(pch) else f'{pch:+.1%}'} |")
open(R+'/top30.md','w').write('\n'.join(L))
print(len(L))
