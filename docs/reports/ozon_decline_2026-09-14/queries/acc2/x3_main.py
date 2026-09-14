# acc2 (Дисквэр): помесячно/недельно, мосты, концентрация, каннибализация. Read-only, из кэшей.
import pandas as pd, numpy as np, re, os
os.chdir(os.path.dirname(os.path.abspath(__file__)))
C = 'cache/'; DC = '../decline/cache/'
src = open('../decline/s3_bridge.py').read()
exec(src[src.index('# --- семьи ---'):src.index('def per_period')])   # BRANDS, family()
OUT = []
def p(*a): OUT.append(' '.join(str(x) for x in a))
A2 = 'oz_acc2'
# ---------- справочник ----------
PR = pd.read_pickle(C + 'product2.pkl')
PR['fam_name'] = [family(n)[0] for n in PR.name.fillna('')]
PR['base4'] = PR.offer_id.str[:4]
coded = PR[~PR.fam_name.str.contains(r'\(без кода\)') & ~PR.name.str.contains(r'^Комплект', na=False)]
b4fam = coded.groupby('base4').fam_name.agg(lambda s: s.value_counts().index[0]).to_dict()
PR['family'] = [b4fam.get(b, f) for b, f in zip(PR.base4, PR.fam_name)]
sku2off = PR.drop_duplicates(['account', 'sku']).set_index(['account', 'sku']).offer_id
off2fam = PR.drop_duplicates(['account', 'offer_id']).set_index(['account', 'offer_id']).family
def fam_of(acc, off, name=''):
    f = off2fam.get((acc, off))
    if f is None:
        b = off[:4]; f = b4fam.get(b) or family(name)[0]
    return f
# ---------- транзакции ----------
TU = pd.read_pickle(C + 'tx_units.pkl'); TU['day'] = pd.to_datetime(TU.day); TU['accr'] = TU.accr.astype(float)
TU = TU[TU.day <= '2026-09-07']
TU['offer_id'] = [sku2off.get((a, s)) for a, s in zip(TU.account, TU.sku)]
TU['m'] = TU.day.dt.strftime('%Y-%m'); TU.loc[TU.day >= '2026-09-01', 'm'] = '2026-09(1-7)'
TU['wk'] = (TU.day - pd.to_timedelta(TU.day.dt.weekday, 'D'))
DEL = TU[TU.op == 'OperationAgentDeliveredToCustomer']; RET = TU[TU.op == 'ClientReturnAgentOperation']
TT = pd.read_pickle(C + 'tx_types.pkl'); TT['day'] = pd.to_datetime(TT.day); TT = TT[TT.day <= '2026-09-07']
TT['m'] = TT.day.dt.strftime('%Y-%m'); TT.loc[TT.day >= '2026-09-01', 'm'] = '2026-09(1-7)'
TT['wk'] = (TT.day - pd.to_timedelta(TT.day.dt.weekday, 'D'))
TT['amount'] = TT.amount.astype(float); TT['accr'] = TT.accr.astype(float)
ADOPS = ['OperationMarketplaceCostPerClick', 'OperationPromotionWithCostPerOrder']
# ---------- постинги ----------
PL = pd.read_pickle(DC + 'post_lines.pkl'); PL['day'] = pd.to_datetime(PL.day)
PL['rev'] = PL.price.astype(float) * PL.qty.astype(float); PL['qty'] = PL.qty.astype(float)
PL = PL[PL.day <= '2026-09-13']
PL['m'] = PL.day.dt.strftime('%Y-%m'); PL.loc[PL.day >= '2026-09-01', 'm'] = '2026-09(1-13)'
PL['wk'] = (PL.day - pd.to_timedelta(PL.day.dt.weekday, 'D'))
# ---------- поиск ----------
SR = pd.read_pickle(DC + 'search.pkl'); SR['ps'] = pd.to_datetime(SR.period_start)
for c in ['unique_view_users', 'unique_search_users', 'order_count', 'gmv']: SR[c] = SR[c].astype(float)
SR['m'] = SR.ps.dt.strftime('%Y-%m')
# ---------- остатки МС (по base4; общий склад двух кабинетов) ----------
ST = pd.read_pickle(DC + 'stock.pkl'); ST['day'] = pd.to_datetime(ST.day)
STK = ST.groupby('day').external_code.apply(set).to_dict(); SDAYS = sorted(STK)
# ---------- реализация ----------
RL = pd.read_pickle(C + 'real.pkl'); RL['price'] = RL.price.astype(float); RL['dq'] = RL.dq.astype(float); RL['rq'] = RL.rq.astype(float)
RL['rev'] = RL.price * RL.dq; RL['m'] = RL.year.astype(str) + '-' + RL.month.astype(str).str.zfill(2)
RL['family'] = [fam_of(a, o, n) for a, o, n in zip(RL.account, RL.offer_id, RL.name.fillna(''))]
RG = RL.groupby(['account', 'm', 'offer_id']).agg(rev=('rev', 'sum'), q=('dq', 'sum'), rq=('rq', 'sum'), family=('family', 'first'), name=('name', 'first')).reset_index()
# ---------- реклама ----------
AD = pd.read_pickle(DC + 'ad_daily.pkl'); AD['day'] = pd.to_datetime(AD.day)
BIDS = pd.read_pickle(C + 'bids.pkl'); BS2 = pd.read_pickle(C + 'bids_sku2.pkl')
ADS = pd.read_pickle(DC + 'ads_sku.pkl'); ADS['day'] = pd.to_datetime(ADS.day)
# ============ 1. помесячно ============
months = ['2026-03', '2026-04', '2026-05', '2026-06', '2026-07', '2026-08', '2026-09(1-7)']
rows = []
basket = set(RG[(RG.account == A2) & (RG.m.isin(['2026-03', '2026-04', '2026-05', '2026-06', '2026-07', '2026-08']))].offer_id)
for m in months:
    d = DEL[(DEL.account == A2) & (DEL.m == m)]; r = RET[(RET.account == A2) & (RET.m == m)]; t = TT[(TT.account == A2) & (TT.m == m)]
    rev = d.accr.sum(); u = d.units.sum()
    row = dict(m=m, rev_tx=rev, units_tx=u, avg_price_tx=rev / u if u else np.nan, sku_sold=d.offer_id.nunique(),
               fam_sold=len({fam_of(A2, o) for o in d.offer_id.dropna().unique()}),
               ret_units=r.units.sum(), ret_rub=-r.accr.sum(), ret_share=(-r.accr.sum()) / rev if rev else np.nan,
               ads_tx=-t[t.op.isin(ADOPS)].amount.sum(), stars=-t[t.op == 'StarsMembership'].amount.sum(),
               saldo=t.amount.sum())
    pm = m.replace('(1-7)', '(1-13)')
    pl = PL[(PL.account == A2) & (PL.m == pm)]
    if len(pl):
        ok = pl[pl.status != 'cancelled']
        row.update(post_rev=ok.rev.sum(), orders=ok.posting_number.nunique(), units_post=ok.qty.sum(), avg_price_post=ok.rev.sum() / ok.qty.sum(),
                   cancel_share=pl[pl.status == 'cancelled'].posting_number.nunique() / pl.posting_number.nunique(), sku_ordered=ok.offer_id.nunique())
    sm = SR[(SR.account == A2) & (SR.m == m[:7])]
    if len(sm) and m != '2026-09(1-7)':
        nw = sm.ps.nunique()
        row.update(search_weeks=nw, views_wk=sm.unique_view_users.sum() / nw, demand_wk=sm.unique_search_users.sum() / nw, sku_in_search_wk=len(sm) / nw,
                   search_orders_wk=sm.order_count.sum() / nw)
    days = [x for x in SDAYS if x.strftime('%Y-%m') == m[:7] and (m != '2026-09(1-7)' or x.day <= 7)]
    if days:
        b4 = {o[:4] for o in basket}
        row['avail_basket'] = np.mean([len(b4 & STK[x]) / len(b4) for x in days])
    bd = BIDS[(BIDS.account == A2) & (pd.to_datetime(BIDS.day).dt.strftime('%Y-%m') == m[:7])]
    if len(bd): row['ads_sku_mean'] = bd.n_sku.mean()
    rows.append(row)
M = pd.DataFrame(rows)
# реализация: SKU с продажами
M['sku_real'] = [RG[(RG.account == A2) & (RG.m == m)].offer_id.nunique() if '(' not in m else np.nan for m in M.m]
# конверсия
M['conv_post_per_view'] = M.units_post / (M.views_wk * M.m.map(lambda m: 4.33 if '(' not in m else np.nan)) if 'views_wk' in M else np.nan
# фикс-корзина цены (цепной Фишер по реализации, общие offer_id соседних месяцев)
r2 = RG[RG.account == A2]; idx = [1.0]; ncom = [np.nan]
for a, b in zip(months[:5], months[1:6]):
    x = r2[r2.m == a].set_index('offer_id'); y = r2[r2.m == b].set_index('offer_id'); com = x.index.intersection(y.index)
    pa = x.loc[com].rev / x.loc[com].q; pb = y.loc[com].rev / y.loc[com].q; qa = x.loc[com].q; qb = y.loc[com].q
    L = (pb * qa).sum() / (pa * qa).sum(); Pq = (pb * qb).sum() / (pa * qb).sum()
    idx.append(idx[-1] * np.sqrt(L * Pq)); ncom.append(len(com))
M['price_idx_fixed'] = idx + [np.nan]; M['n_common'] = ncom + [np.nan]
# ads spend daily (Performance) — сверка
M['ads_daily'] = [AD[(AD.account == A2) & (AD.day.dt.strftime('%Y-%m') == m[:7]) & ((m != '2026-09(1-7)') | (AD.day.dt.day <= 7))].spend.sum() for m in M.m]
M.to_csv('01_monthly_acc2.csv', index=False)
# ============ недельно ============
wk = []
for w in sorted(DEL[DEL.account == A2].wk.unique()):
    d = DEL[(DEL.account == A2) & (DEL.wk == w)]; t = TT[(TT.account == A2) & (TT.wk == w)]
    pl = PL[(PL.account == A2) & (PL.wk == w)]; ok = pl[pl.status != 'cancelled']
    sw = SR[(SR.account == A2) & (SR.ps == w)]
    wk.append(dict(week=w.date(), rev_tx=d.accr.sum(), units_tx=d.units.sum(), sku_sold=d.offer_id.nunique(),
                   ads_tx=-t[t.op.isin(ADOPS)].amount.sum(), ret_rub=-RET[(RET.account == A2) & (RET.wk == w)].accr.sum(),
                   post_rev=ok.rev.sum() if len(pl) else np.nan, units_post=ok.qty.sum() if len(pl) else np.nan,
                   avg_price_post=ok.rev.sum() / ok.qty.sum() if len(ok) else np.nan,
                   cancel_share=(pl.status == 'cancelled').groupby(pl.posting_number).max().mean() if len(pl) else np.nan,
                   views=sw.unique_view_users.sum() if len(sw) else np.nan, demand=sw.unique_search_users.sum() if len(sw) else np.nan,
                   sku_search=len(sw) if len(sw) else np.nan,
                   conv=ok.qty.sum() / sw.unique_view_users.sum() if len(sw) and len(pl) else np.nan))
W = pd.DataFrame(wk)
# недели постингов после 07.09
for w in sorted(PL[(PL.account == A2) & (PL.wk > pd.Timestamp('2026-08-31'))].wk.unique()):
    if w.date() in set(W.week): continue
    pl = PL[(PL.account == A2) & (PL.wk == w)]; ok = pl[pl.status != 'cancelled']
    W = pd.concat([W, pd.DataFrame([dict(week=w.date(), post_rev=ok.rev.sum(), units_post=ok.qty.sum(), avg_price_post=ok.rev.sum() / ok.qty.sum())])])
W.to_csv('02_weekly_acc2.csv', index=False)
# ============ 2. мост апрель -> август (реализация, offer_id) ============
def bridge(acc, ma, mb):
    x = RG[(RG.account == acc) & (RG.m == ma)].set_index('offer_id'); y = RG[(RG.account == acc) & (RG.m == mb)].set_index('offer_id')
    allo = x.index.union(y.index)
    B = pd.DataFrame(index=allo); B['ra'] = x.rev; B['qa'] = x.q; B['rb'] = y.rev; B['qb'] = y.q
    B = B.fillna(0); B['family'] = [x.family.get(o) if o in x.index else y.family.get(o) for o in allo]
    B['name'] = [x.name.get(o) if o in x.index else y.name.get(o) for o in allo]
    B['cls'] = np.where((B.ra > 0) & (B.rb > 0), 'cont', np.where(B.ra > 0, 'drop', 'new'))
    pa = B.ra / B.qa.replace(0, np.nan); pb = B.rb / B.qb.replace(0, np.nan)
    c = B.cls == 'cont'
    B['price_eff'] = np.where(c, (pb - pa) * (B.qa + B.qb) / 2, 0); B['vol_eff'] = np.where(c, (B.qb - B.qa) * (pa + pb) / 2, 0)
    B['dR'] = B.rb - B.ra
    return B
B = bridge(A2, '2026-04', '2026-08')
# статус выпавших: карточка жива? остаток МС в августе/сейчас? в рекламе?
act2 = set(PR[(PR.account == A2) & (~PR.is_archived)].offer_id)
aug_days = [x for x in SDAYS if x.strftime('%Y-%m') == '2026-08']; last_day = SDAYS[-1]
B['card_active_now'] = B.index.isin(act2)
B['stock_aug_share'] = [np.mean([o[:4] in STK[x] for x in aug_days]) for o in B.index]
B['stock_now'] = [o[:4] in STK[last_day] for o in B.index]
off2sku2 = PR[PR.account == A2].groupby('offer_id').sku.apply(set).to_dict()
bid_sets = BS2.groupby(BS2.day.astype(str)).sku.apply(set).to_dict()
B['ads_0807'] = [bool(off2sku2.get(o, set()) & bid_sets.get('2026-08-07', set())) for o in B.index]
B['ads_0914'] = [bool(off2sku2.get(o, set()) & bid_sets.get('2026-09-14', set())) for o in B.index]
B.to_csv('03_bridge_apr_aug_sku.csv')
dR = B.dR.sum()
comp = dict(dropped=B[B.cls == 'drop'].dR.sum(), new=B[B.cls == 'new'].dR.sum(), price_cont=B.price_eff.sum(), vol_cont=B.vol_eff.sum())
p(f'МОСТ апр→авг acc2 (реализация): R апр {B.ra.sum():,.0f} → авг {B.rb.sum():,.0f}; ΔR {dR:,.0f}')
for k, v in comp.items(): p(f'  {k}: {v:,.0f}  n={ {"dropped":(B.cls=="drop").sum(),"new":(B.cls=="new").sum(),"price_cont":(B.cls=="cont").sum(),"vol_cont":(B.cls=="cont").sum()}[k]}')
p(f'  остаток: {dR - sum(comp.values()):,.2f}')
dr = B[B.cls == 'drop']
p(f'  выпавшие: карточка активна сейчас {dr[dr.card_active_now].ra.sum():,.0f} ₽апр ({dr.card_active_now.sum()} SKU); архив/нет {dr[~dr.card_active_now].ra.sum():,.0f}')
p(f'  выпавшие: остаток МС в авг ≥50% дней {dr[dr.stock_aug_share >= .5].ra.sum():,.0f} ₽ ({(dr.stock_aug_share >= .5).sum()}); <50% {dr[dr.stock_aug_share < .5].ra.sum():,.0f} ({(dr.stock_aug_share < .5).sum()})')
p(f'  выпавшие: в рекламе 07.08 {dr[dr.ads_0807].ra.sum():,.0f} ({dr.ads_0807.sum()}); в рекламе 14.09 {dr[dr.ads_0914].ra.sum():,.0f}')
# новые/выпавшие по классам цены
# семьи
F = B.groupby('family').agg(n=('ra', 'size'), ra=('ra', 'sum'), rb=('rb', 'sum'), dR=('dR', 'sum'),
                            drop=('dR', lambda s: s[B.loc[s.index, 'cls'] == 'drop'].sum()), new=('dR', lambda s: s[B.loc[s.index, 'cls'] == 'new'].sum()),
                            price=('price_eff', 'sum'), vol=('vol_eff', 'sum')).sort_values('dR')
F.to_csv('04_bridge_families_apr_aug.csv')
p('ТОП-8 семей потерь (ΔR, выпавшие, новые, цена, объём):')
for f, r in F.head(8).iterrows(): p(f'  {f}: апр {r.ra:,.0f} авг {r.rb:,.0f} Δ {r.dR:,.0f} | drop {r["drop"]:,.0f} new {r.new:,.0f} price {r["price"]:,.0f} vol {r.vol:,.0f}')
p(f'  выпавшие по семьям: топ-10 семей дают {dr.groupby("family").dR.sum().nsmallest(10).sum():,.0f} из {dr.dR.sum():,.0f}')
# ============ 2b. мост июль→август по постингам с факторами ============
def post_month(acc, m):
    ok = PL[(PL.account == acc) & (PL.m == m) & (PL.status != 'cancelled')]
    return ok.groupby('offer_id').agg(r=('rev', 'sum'), q=('qty', 'sum'))
ja, au = post_month(A2, '2026-07'), post_month(A2, '2026-08')
J = pd.DataFrame(index=ja.index.union(au.index)); J['ra'] = ja.r; J['qa'] = ja.q; J['rb'] = au.r; J['qb'] = au.q; J = J.fillna(0)
J['cls'] = np.where((J.ra > 0) & (J.rb > 0), 'cont', np.where(J.ra > 0, 'drop', 'new'))
pa = J.ra / J.qa.replace(0, np.nan); pb = J.rb / J.qb.replace(0, np.nan); c = J.cls == 'cont'
J['price'] = np.where(c, (pb - pa) * (J.qa + J.qb) / 2, 0); J['vol'] = np.where(c, (J.qb - J.qa) * (pa + pb) / 2, 0); J['dR'] = J.rb - J.ra
# факторы: доступность (МС base4 дни), поисковые показы/нед, конверсия (шт/показ)
jul_days = [x for x in SDAYS if x.strftime('%Y-%m') == '2026-07']
def av(o, days): return np.mean([o[:4] in STK[x] for x in days]) if days else np.nan
s2 = SR[SR.account == A2]
vw = s2.groupby(['m', 'offer_id']).unique_view_users.sum() / s2.groupby('m').ps.nunique()
J['aa'] = [av(o, jul_days) for o in J.index]; J['ab'] = [av(o, aug_days) for o in J.index]
J['va'] = [vw.get(('2026-07', o), 0) for o in J.index]; J['vb'] = [vw.get(('2026-08', o), 0) for o in J.index]
def L(a, b): return (a - b) / (np.log(a) - np.log(b)) if a != b else a
comps = {'avail': 0.0, 'traffic': 0.0, 'conv': 0.0, 'uncovered': 0.0}
for o, r in J[c].iterrows():
    if r.qa == r.qb: continue
    ok = min(r.aa, r.ab, r.va, r.vb) > 0 if not np.isnan(r.aa) else False
    if not ok: comps['uncovered'] += r.vol; continue
    w = r.vol / np.log(r.qb / r.qa)
    fa = np.log(r.ab / r.aa); ft = np.log((r.vb / r.ab) / (r.va / r.aa)); fc = np.log((r.qb / r.vb) / (r.qa / r.va))
    comps['avail'] += w * fa; comps['traffic'] += w * ft; comps['conv'] += w * fc
# реклама: SKU, бывшие в рекламе 07.08 и снятые к 19.08
J['ads_0807'] = [bool(off2sku2.get(o, set()) & bid_sets.get('2026-08-07', set())) for o in J.index]
J['ads_0819'] = [bool(off2sku2.get(o, set()) & bid_sets.get('2026-08-19', set())) for o in J.index]
p(f'МОСТ июл→авг acc2 (постинги без отмен): {J.ra.sum():,.0f} → {J.rb.sum():,.0f}; ΔR {J.dR.sum():,.0f}')
p(f'  выпавшие {J[J.cls=="drop"].dR.sum():,.0f} (n {(J.cls=="drop").sum()}), новые {J[J.cls=="new"].dR.sum():,.0f} (n {(J.cls=="new").sum()}), цена {J.price.sum():,.0f}, объём {J.vol.sum():,.0f}')
p('  объём продолжающих: ' + ', '.join(f'{k} {v:,.0f}' for k, v in comps.items()) + f' | остаток {J.vol.sum() - sum(comps.values()):,.0f}')
lost_ads = J.ads_0807 & ~J.ads_0819
p(f'  SKU в рекламе 07.08 и сняты к 19.08: n {lost_ads.sum()}, их ΔR {J[lost_ads].dR.sum():,.0f}; остались в рекламе ΔR {J[J.ads_0807 & J.ads_0819].dR.sum():,.0f}; не в рекламе ΔR {J[~J.ads_0807].dR.sum():,.0f}')
J.to_csv('05_bridge_jul_aug_postings_sku.csv')
# выпавшие июля: остаток в авг
jd = J[J.cls == 'drop']; p(f'  выпавшие июля: остаток МС в авг ≥50% {jd[jd.ab >= .5].dR.sum():,.0f}; <50% {jd[jd.ab < .5].dR.sum():,.0f}; поиск.показы в авг >0 {jd[jd.vb > 0].dR.sum():,.0f}')
# ============ 3. концентрация ============
ra = RG[(RG.account == A2) & (RG.m == '2026-04')].sort_values('rev', ascending=False)
top = ra.head(20).offer_id.tolist()
PI = pd.read_pickle(C + 'pidx2.pkl'); PI['d'] = pd.to_datetime(PI.collected_on)
pnow = PI[PI.d == '2026-09-14'].set_index('offer_id')
sep = PL[(PL.account == A2) & (PL.day >= '2026-09-01') & (PL.status != 'cancelled')].groupby('offer_id').qty.sum()
T20 = B.loc[top, ['family', 'name', 'ra', 'qa', 'rb', 'qb', 'card_active_now', 'stock_aug_share', 'stock_now', 'ads_0807', 'ads_0914']].copy()
T20['price_apr'] = T20.ra / T20.qa; T20['price_now'] = pnow.reindex(top).price.astype(float).values; T20['mprice_now'] = pnow.reindex(top).marketing_price.astype(float).values
T20['color_now'] = pnow.reindex(top).color_index.values; T20['sep_units_1_13'] = sep.reindex(top).fillna(0).values
T20.to_csv('06_top20_sku_apr.csv')
p(f'ТОП-20 SKU апр: {T20.ra.sum():,.0f} из {ra.rev.sum():,.0f} ({T20.ra.sum()/ra.rev.sum():.0%}); в авг {T20.rb.sum():,.0f}; с продажами в авг {(T20.rb>0).sum()}, в сент(1–13) {(T20.sep_units_1_13>0).sum()}; карточка активна {T20.card_active_now.sum()}; остаток МС сейчас {T20.stock_now.sum()}; в рекламе 07.08 {T20.ads_0807.sum()} / 14.09 {T20.ads_0914.sum()}; цена сейчас/апр медиана {(T20.price_now/T20.price_apr).median():.2f}')
FA = RG[(RG.account == A2)].pivot_table(index='family', columns='m', values='rev', aggfunc='sum').fillna(0)
top10f = FA['2026-04'].nlargest(10).index
p(f'ТОП-10 семей апр: {FA.loc[top10f,"2026-04"].sum():,.0f} ({FA.loc[top10f,"2026-04"].sum()/FA["2026-04"].sum():.0%}) → авг {FA.loc[top10f,"2026-08"].sum():,.0f}')
# ============ каннибализация acc1 ============
F1 = RG[(RG.account == 'oz_acc1')].pivot_table(index='family', columns='m', values='rev', aggfunc='sum').fillna(0)
fam_d2 = (FA['2026-08'] - FA['2026-04'])
lostf = fam_d2[fam_d2 < 0].index
d1_lost = (F1.reindex(lostf).fillna(0)['2026-08'] - F1.reindex(lostf).fillna(0)['2026-04'])
others = F1.index.difference(lostf)
p(f'КАННИБАЛИЗАЦИЯ: семьи acc2 с потерей ({len(lostf)}) Δacc2 {fam_d2[lostf].sum():,.0f}; те же семьи на acc1 апр {F1.reindex(lostf).fillna(0)["2026-04"].sum():,.0f} → авг {F1.reindex(lostf).fillna(0)["2026-08"].sum():,.0f} (Δ {d1_lost.sum():,.0f}); прочие семьи acc1 Δ {(F1.loc[others,"2026-08"]-F1.loc[others,"2026-04"]).sum():,.0f} ({(F1.loc[others,"2026-08"].sum()/F1.loc[others,"2026-04"].sum()-1):+.0%})')
p(f'  те же семьи acc1 %: {(F1.reindex(lostf).fillna(0)["2026-08"].sum()/F1.reindex(lostf).fillna(0)["2026-04"].sum()-1):+.0%}; семей, где acc1 вырос: {(d1_lost>0).sum()} (+{d1_lost[d1_lost>0].sum():,.0f}), упал {(d1_lost<0).sum()}')
T10 = pd.DataFrame({'acc2_apr': FA.loc[top10f, '2026-04'], 'acc2_aug': FA.loc[top10f, '2026-08'], 'acc1_apr': F1.reindex(top10f).fillna(0)['2026-04'], 'acc1_aug': F1.reindex(top10f).fillna(0)['2026-08']})
T10.to_csv('07_top10_families_acc1_vs_acc2.csv')
for f, r in T10.iterrows(): p(f'  {f}: acc2 {r.acc2_apr:,.0f}→{r.acc2_aug:,.0f} | acc1 {r.acc1_apr:,.0f}→{r.acc1_aug:,.0f}')
# base4 уровень для топ-20 SKU
b4_1 = RL[RL.account == 'oz_acc1'].assign(b4=lambda d: d.offer_id.str[:4]).groupby(['b4', 'm']).rev.sum()
tb = {o[:4] for o in top}
s_apr = sum(b4_1.get((b, '2026-04'), 0) for b in tb); s_aug = sum(b4_1.get((b, '2026-08'), 0) for b in tb)
p(f'  base4 топ-20 SKU acc2 на acc1: апр {s_apr:,.0f} → авг {s_aug:,.0f}')
FA.to_csv('08_families_monthly_acc2.csv'); F1.to_csv('08b_families_monthly_acc1.csv')
open('x3_main.txt', 'w').write('\n'.join(OUT)); print('\n'.join(OUT))
