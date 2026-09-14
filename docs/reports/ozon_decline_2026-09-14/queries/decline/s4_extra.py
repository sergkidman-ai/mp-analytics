# Доп. разрезы: группы волны 1 (A/B), эффект подъёма цен 02–04.09, таймлайн событий. Read-only.
import pandas as pd, numpy as np, os
D = os.path.dirname(os.path.abspath(__file__)); C = D + '/cache'; T = pd.Timestamp
P = pd.read_pickle(f'{C}/post_lines.pkl'); P['day'] = pd.to_datetime(P.day)
P['price'] = P.price.astype(float); P['qty'] = P.qty.astype(float); P['rev'] = P.price * P.qty
OK = P[P.status != 'cancelled']
PR = pd.read_pickle(f'{C}/product.pkl')
AS = pd.read_pickle(f'{C}/ads_sku.pkl'); AS['day'] = pd.to_datetime(AS.day)
for c in ['views', 'clicks', 'spend', 'orders', 'orders_money']: AS[c] = AS[c].astype(float)
AD = pd.read_pickle(f'{C}/ad_daily.pkl'); AD['day'] = pd.to_datetime(AD.day); AD['spend'] = AD.spend.astype(float)
SR = pd.read_pickle(f'{C}/search.pkl'); SR['period_start'] = pd.to_datetime(SR.period_start)
out = []

# ---------- (a) группы волны 1 ----------
W = pd.read_csv('/opt/mp-analytics/docs/reports/ozon_wave1_restore_oz_acc1_2026-08-19.csv', dtype={'sku': str})
sku2off = PR[PR.account == 'oz_acc1'].set_index('sku').offer_id.to_dict()
W['offer_id'] = W.sku.map(sku2off)
g_off = W.dropna(subset=['offer_id']).groupby('offer_id').group.agg(lambda s: ''.join(sorted(set(s)))).to_dict()   # 'A','B','AB'
def grp(acc, off): return ('wave1_' + g_off[off]) if acc == 'oz_acc1' and off in g_off else ('not_wave1_' + acc)
comp = ['drop_nostock', 'drop_stock', 'drop_nodata', 'new', 'price', 'avail', 'traffic_left_ads', 'traffic_other', 'conv', 'vol_unexplained']
for tag in ['A', 'B']:
    m = pd.read_csv(f'{D}/10_{tag}_sku_bridge_full.csv', dtype={'offer_id': str})
    m['wave1_group'] = [grp(a, o) for a, o in zip(m.account, m.offer_id)]
    m.to_csv(f'{D}/10_{tag}_sku_bridge_full.csv', index=False)
    x = m.groupby('wave1_group').agg(n_sku=('offer_id', 'count'), R_base=('r_b', 'sum'), R_cur=('r_c', 'sum'), dR=('dR', 'sum'), **{c: (c, 'sum') for c in comp}).reset_index()
    x.to_csv(f'{D}/15_{tag}_bridge_by_wave1_group.csv', index=False)
    s = x.set_index('wave1_group')
    out.append(f'{tag} by group dR/day: ' + ', '.join(f'{k}={v:.0f}' for k, v in s.dR.items()))
# недельная выручка заказов по группам
OK1 = OK.copy(); OK1['wave1_group'] = [grp(a, o) for a, o in zip(OK1.account, OK1.offer_id)]
OK1['week'] = OK1.day - pd.to_timedelta(OK1.day.dt.weekday, unit='D')
wg = OK1[OK1.week <= T('2026-09-07')].pivot_table(index='week', columns='wave1_group', values='rev', aggfunc='sum').fillna(0)
wg.to_csv(f'{D}/16_weekly_rev_by_wave1_group.csv')
# рекламные показы групп по неделям (acc1)
A1 = AS[AS.account == 'oz_acc1'].copy(); A1['g'] = A1.sku.map(W.groupby('sku').group.agg(lambda s: ''.join(sorted(set(s))))).fillna('not_wave1')
A1['week'] = A1.day - pd.to_timedelta(A1.day.dt.weekday, unit='D')
A1.pivot_table(index='week', columns='g', values=['views', 'spend', 'orders_money'], aggfunc='sum').to_csv(f'{D}/16b_weekly_ads_by_wave1_group.csv')

# ---------- (b) эффект подъёма цен 02–04.09 ----------
PI = pd.read_pickle(f'{C}/price_idx.pkl'); PI['collected_on'] = pd.to_datetime(PI.collected_on); PI['price'] = PI.price.astype(float)
pv = PI.pivot_table(index=['account', 'offer_id'], columns='collected_on', values='price', aggfunc='median')
pv.columns = [f'p_{d:%m%d}' for d in pv.columns]
pv['chg_0901_0905'] = pv.p_0905 / pv.p_0901 - 1
pv['raised5'] = pv.chg_0901_0905 > 0.05
pv = pv.reset_index()
pv_stats = pv.groupby('account').agg(n=('offer_id', 'count'), n_raised5=('raised5', 'sum'), med_chg_raised=('chg_0901_0905', lambda s: s[s > 0.05].median()))
out.append('price idx 01.09->05.09: ' + '; '.join(f'{a}: {int(r.n_raised5)}/{int(r.n)} SKU >+5%, median {r.med_chg_raised*100:.1f}%' for a, r in pv_stats.iterrows()))

def price_bridge(label, a0, b0, a1, b1):
    n0 = (b0 - a0).days + 1; n1 = (b1 - a1).days + 1
    g0 = OK[(OK.day >= a0) & (OK.day <= b0)].groupby(['account', 'offer_id']).agg(rev0=('rev', 'sum'), q0=('qty', 'sum'))
    g1 = OK[(OK.day >= a1) & (OK.day <= b1)].groupby(['account', 'offer_id']).agg(rev1=('rev', 'sum'), q1=('qty', 'sum'))
    m = g0.join(g1, how='outer').fillna(0).reset_index()
    m['r0'] = m.rev0 / n0; m['r1'] = m.rev1 / n1; m['qd0'] = m.q0 / n0; m['qd1'] = m.q1 / n1
    m = m.merge(pv[['account', 'offer_id', 'chg_0901_0905', 'raised5']], on=['account', 'offer_id'], how='left')
    m['raised5'] = m.raised5.fillna(False).astype(bool)
    cont = (m.q0 > 0) & (m.q1 > 0)
    m['p0'] = m.rev0 / m.q0.replace(0, np.nan); m['p1'] = m.rev1 / m.q1.replace(0, np.nan)
    m['price_eff'] = np.where(cont, m.qd1 * (m.p1 - m.p0), 0.0)
    m['vol_eff'] = np.where(cont, m.p0 * (m.qd1 - m.qd0), 0.0)
    m['churn'] = np.where(~cont, m.r1 - m.r0, 0.0)
    rows = []
    for acc in ['oz_acc1', 'oz_acc2', 'sum']:
        for rs in [True, False, None]:
            x = m if acc == 'sum' else m[m.account == acc]
            if rs is not None: x = x[x.raised5 == rs]
            rows.append(dict(cmp=label, account=acc, raised5=('all' if rs is None else rs), n_sku_cont=int(((x.q0 > 0) & (x.q1 > 0)).sum()),
                             R0_day=x.r0.sum(), R1_day=x.r1.sum(), dR_day=(x.r1 - x.r0).sum(), price_eff_day=x.price_eff.sum(), vol_eff_cont_day=x.vol_eff.sum(), churn_day=x.churn.sum(),
                             units0_day=x.qd0.sum(), units1_day=x.qd1.sum(),
                             unit_price_cont0=(x.rev0[(x.q0 > 0) & (x.q1 > 0)].sum() / max(1, x.q0[(x.q0 > 0) & (x.q1 > 0)].sum())),
                             unit_price_cont1=(x.rev1[(x.q0 > 0) & (x.q1 > 0)].sum() / max(1, x.q1[(x.q0 > 0) & (x.q1 > 0)].sum()))))
    return rows
rows = price_bridge('05-13.09 vs 26.08-01.09 (запрошено; дни недели не совпадают)', T('2026-08-26'), T('2026-09-01'), T('2026-09-05'), T('2026-09-13'))
rows += price_bridge('07-13.09 vs 24-30.08 (пн-вс, те же дни недели)', T('2026-08-24'), T('2026-08-30'), T('2026-09-07'), T('2026-09-13'))
rows += price_bridge('07-13.09 vs 31.08-06.09 (пн-вс; 02-04.09 внутри базы)', T('2026-08-31'), T('2026-09-06'), T('2026-09-07'), T('2026-09-13'))
pb = pd.DataFrame(rows); pb.to_csv(f'{D}/17_price_raise_0902_effect.csv', index=False)
for r in pb[pb.raised5 == 'all'].itertuples():
    out.append(f'{r.cmp[:22]} {r.account}: dR/d {r.dR_day:.0f}, price {r.price_eff_day:.0f}, vol_cont {r.vol_eff_cont_day:.0f}, churn {r.churn_day:.0f}, unitP {r.unit_price_cont0:.0f}->{r.unit_price_cont1:.0f}')

# ---------- (c) таймлайн событий: 7 дней до / 7 дней после ----------
EV = [('2026-07-01', 'начало дыры снимков ставок (ozon_bids) 01.07–06.08'), ('2026-08-07', 'конец дыры ставок'),
      ('2026-08-08', 'acc1 разгон +10%, кампании Бандлы/Вне РК'), ('2026-08-09', 'acc1 снята волна 1 (8 834 SKU)'),
      ('2026-08-10', 'выкл. Звёздные товары, оплата за заказ 7→5% (09–11.08)'), ('2026-08-13', 'заморозка разгона'),
      ('2026-08-18', 'acc1 откат 1 104 связок; acc2 снято 1 326 связок (E1)'), ('2026-08-19', 'acc1 возвращена группа A (4 433 SKU); ставки не применяются'),
      ('2026-08-22', 'робот региональных акций ежедневно'), ('2026-09-02', 'acc1 подъём цен ~8,9 тыс SKU (02–04.09)'), ('2026-09-10', 'acc2 +5,8 тыс SKU в индексе / снижения 11–12.09')]
day_rev = OK.groupby(['day']).rev.sum(); day_units = OK.groupby('day').qty.sum()
day_adv = AS.groupby('day').views.sum(); day_sp = AD.groupby('day').spend.sum()
def win(s, a, b): x = s[(s.index >= a) & (s.index <= b)]; return x.sum() / 7 if len(x) == 7 else np.nan
rows = []
for d, lab in EV:
    d = T(d); a0, b0, a1, b1 = d - pd.Timedelta(days=7), d - pd.Timedelta(days=1), d, d + pd.Timedelta(days=6)
    r = dict(date=d.date(), event=lab)
    for nm, s in [('rev_orders', day_rev), ('units', day_units), ('ad_spend', day_sp), ('sku_ad_views', day_adv)]:
        r[nm + '_7d_before'] = win(s, a0, b0); r[nm + '_7d_after'] = win(s, a1, b1)
        r[nm + '_chg'] = r[nm + '_7d_after'] / r[nm + '_7d_before'] - 1 if r[nm + '_7d_before'] else np.nan
    rows.append(r)
ev = pd.DataFrame(rows); ev.to_csv(f'{D}/20_timeline_events.csv', index=False)
for r in ev.itertuples():
    out.append(f'{r.date} rev {r.rev_orders_chg*100 if pd.notna(r.rev_orders_chg) else float("nan"):+.0f}% units {r.units_chg*100 if pd.notna(r.units_chg) else float("nan"):+.0f}% spend {r.ad_spend_chg*100 if pd.notna(r.ad_spend_chg) else float("nan"):+.0f}% adviews {r.sku_ad_views_chg*100 if pd.notna(r.sku_ad_views_chg) else float("nan"):+.0f}%  {r.event[:40]}')
open(f'{D}/s4_out.txt', 'w').write('\n'.join(out))
print('\n'.join(out))
