# drop118: состояние 118 SKU (qty_b>=2, qty_c==0, остаток МС был). READ-ONLY: только SELECT через q.conn (readonly session).
import sys, re
sys.path.insert(0, '/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad')
from q import conn
import pandas as pd, numpy as np
S = '/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/'
C = S + 'decline/cache/'; O = S + 'drop118/'
T = pd.Timestamp
B0, B1 = T('2026-06-08'), T('2026-07-05')
C0, C1 = T('2026-08-17'), T('2026-09-13')
BR = pd.read_csv(S + 'decline/10_A_sku_bridge_full.csv', dtype={'offer_id': str})
co = BR[(BR.qty_b >= 2) & (BR.qty_c == 0) & (BR.drop_stock != 0)].copy()
nod = BR[BR.drop_nodata != 0]
offs = sorted(set(co.offer_id))

# ---------- postings (кэш, все) ----------
P = pd.read_pickle(C + 'post_lines.pkl'); P['day'] = pd.to_datetime(P.day)
P['price'] = P.price.astype(float); P['qty'] = P.qty.astype(float); P['rev'] = P.price * P.qty
OK = P[P.status != 'cancelled']; CN = P[P.status == 'cancelled']
PROD = pd.read_pickle(C + 'product.pkl')
sku_map = pd.concat([PROD[['account', 'sku', 'offer_id']], P[['account', 'sku', 'offer_id']]]).dropna().drop_duplicates()
sku_map['sku'] = sku_map.sku.astype(str)
SKUS = sku_map[sku_map.offer_id.isin(offs)].groupby(['account', 'offer_id']).sku.apply(lambda s: sorted(set(s))).to_dict()
all_skus = sorted({s for v in SKUS.values() for s in v})

rows = []
def win(df, a, b): return df[(df.day >= a) & (df.day <= b)]
for _, r in co.iterrows():
    k = (r.account, r.offer_id); x = OK[(OK.account == r.account) & (OK.offer_id == r.offer_id)]
    xc = CN[(CN.account == r.account) & (CN.offer_id == r.offer_id)]
    xb = win(x, B0, B1)
    d = dict(account=r.account, offer_id=r.offer_id, name=(r['name'] or '')[:70], family=r.family, rev_b=round(r.rev_b), qty_b=r.qty_b,
             orders_b=xb.posting_number.nunique(), days_b=xb.day.nunique(), weeks_b=((xb.day - B0).dt.days // 7).nunique(),
             price_base=round(r.rev_b / r.qty_b, 0) if r.qty_b else np.nan,
             sold_pre_0501_0607=win(x, T('2026-05-01'), T('2026-06-07')).qty.sum(),
             sold_mid_0706_0816=win(x, T('2026-07-06'), T('2026-08-16')).qty.sum(),
             price_mid_0706_0816=round(win(x, T('2026-07-06'), T('2026-08-16')).price.mean(), 0),
             price_last_sale=x.sort_values('day').price.iloc[-1] if len(x) else np.nan,
             q7=win(x, T('2026-09-07'), C1).qty.sum(), q14=win(x, T('2026-08-31'), C1).qty.sum(), q28=win(x, C0, C1).qty.sum(),
             q_0914_partial=win(x, T('2026-09-14'), T('2026-09-14')).qty.sum(),
             cancel_28=win(xc, C0, C1).qty.sum(), cancel_b=win(xc, B0, B1).qty.sum(),
             last_sale=x.day.max().date() if len(x) else None, last_cancel=xc.day.max().date() if len(xc) else None,
             scheme_b=','.join(sorted(set(xb.scheme))), in_ads_now=r.in_ads_now,
             search_v_b_d=r.v_b, search_v_c_d=r.v_c, search_pos_b=r.pos_b, search_pos_c=r.pos_c,
             ad_views_ref_d=r.adv_b, ad_views_c_d=r.adv_c, ad_clicks_c_d=r.adcl_c, ad_spend_c_d=r.adsp_c, ad_views_last7_d=r.adv_last7)
    rows.append(d)
st = pd.DataFrame(rows)

# транзакции до 01.05 (с 14.03)
TX = pd.read_pickle(C + 'tx_sku_daily.pkl'); TX['day'] = pd.to_datetime(TX.day); TX['sku'] = TX.sku.astype(str)
TXp = TX[(TX.day >= T('2026-03-14')) & (TX.day <= T('2026-04-30')) & (TX.accr_pos.astype(float) > 0)]
st['tx_days_0314_0430'] = [TXp[(TXp.account == a) & TXp.sku.isin(SKUS.get((a, o), []))].day.nunique() for a, o in zip(st.account, st.offer_id)]

# ---------- семья: первые 4 знака + семья по названию ----------
OKb, OKc = win(OK, B0, B1), win(OK, C0, C1)
def fam_stats(mask_b, mask_c):
    return OKb[mask_b].qty.sum(), OKc[mask_c].qty.sum()
f4b = OKb.offer_id.str[:4]; f4c = OKc.offer_id.str[:4]
COLW = r'(черный|чёрный|голубой|пурпурный|желтый|жёлтый|синий|красный|серый|цветной|трехцветный|трёхцветный|фото-черный|матовый черный)'
def xkey(n):
    # точный ключ картриджа: тип + код как в названии (цвет НЕ схлопываем) + цветовое слово в конце
    if not isinstance(n, str): return None
    m = re.match(r'^\s*(Комплект картриджей|Картриджи|Картридж|Чернила|Фотобарабан|Тонер-картридж|Тонер|Драм-картридж|Барабан|Блок фотобарабана|Контейнер\S*|Печатающая головка)\s+(?:DS\s+)?(?:лазерный\s+|струйный\s+)?№?\s*([A-Za-z0-9][A-Za-z0-9\-\./]*\d[A-Za-z0-9\-\./]*)', n)
    if not m: return None
    col = re.search(COLW + r'\s*$', n.strip(), re.I)
    return (m.group(1).lower() + ' ' + m.group(2).upper().rstrip(',.') + ' ' + (col.group(1).lower().replace('ё', 'е') if col else '')).strip()
nm = {}
for (a_, o_), g_ in P.groupby(['account', 'offer_id']).name: nm[(a_, o_)] = xkey(g_.iloc[-1])
for a_, o_, n_ in zip(BR.account, BR.offer_id, BR['name']): nm.setdefault((a_, o_), xkey(n_))
OKb_f = pd.Series([nm.get(k) for k in zip(OKb.account, OKb.offer_id)], index=OKb.index)
OKc_f = pd.Series([nm.get(k) for k in zip(OKc.account, OKc.offer_id)], index=OKc.index)
fr = []
for _, r in st.iterrows():
    p4 = r.offer_id[:4]; self_b = (OKb.account == r.account) & (OKb.offer_id == r.offer_id)
    sib4b, sib4c = fam_stats((f4b == p4) & ~self_b, (f4c == p4) & ~((OKc.account == r.account) & (OKc.offer_id == r.offer_id)))
    xk = nm.get((r.account, r.offer_id))
    if xk:
        sibnb, sibnc = fam_stats((OKb_f == xk) & ~self_b, (OKc_f == xk) & ~((OKc.account == r.account) & (OKc.offer_id == r.offer_id)))
        top = OKc[(OKc_f == xk) & (OKc.offer_id != r.offer_id)].groupby(['account', 'offer_id']).qty.sum().sort_values(ascending=False)
    else:
        sibnb = sibnc = np.nan; top = pd.Series(dtype=float)
    top4 = OKc[(f4c == p4) & (OKc.offer_id != r.offer_id)].groupby(['account', 'offer_id']).qty.sum().sort_values(ascending=False)
    t = {}
    for s_ in (top4, top):
        for kk, q in s_.items(): t[kk] = max(t.get(kk, 0), q)
    t = sorted(t.items(), key=lambda z: -z[1])
    fr.append(dict(exact_key=xk, sib4_qty_b=sib4b, sib4_qty_c=sib4c, sibname_qty_b=sibnb, sibname_qty_c=sibnc,
                   sib_top_c=';'.join(f'{a[-1]}:{o}={int(q)}' for (a, o), q in t[:3])))
st = pd.concat([st, pd.DataFrame(fr)], axis=1)

# ---------- БД: карточка, остатки, цены, акции, рейтинг, реклама, доставка ----------
Q = {
 'prod': ("select account, offer_id, bool_and(is_archived) arch_all, bool_or(is_archived) arch_any, max(updated_at)::date upd from ozon_product where offer_id = any(%(o)s) group by 1,2", {}),
 'card': ("select distinct on (account, offer_id) account, offer_id, status, err_class, is_selling, healed_at::date healed, last_seen::date last_seen, left(coalesce(err_texts::text,''),80) err from card_status where platform='ozon' and offer_id = any(%(o)s) order by account, offer_id, last_seen desc", {}),
 'promo': ("select distinct on (account, sku) account, sku, available, promo_status, views_week, visibility_index, bid from ozon_search_promo where sku = any(%(s)s) order by account, sku, captured_at desc", {}),
 'pi': ("select account, offer_id, collected_on, min(price) price, min(old_price) old_price, min(marketing_price) mprice, min(marketing_seller_price) msp, min(min_price) min_price, max(color_index) color, max(external_index) ext_idx, min(external_min_price) ext_min, max(ozon_index) oz_idx, min(ozon_min_price) oz_min, max(self_index) self_idx, bool_or(auto_action_enabled::text='true') auto_act from ozon_price_index where offer_id = any(%(o)s) and collected_on in ('2026-08-06','2026-08-17','2026-09-01','2026-09-05','2026-09-14') group by 1,2,3", {}),
 'act': ("select account, offer_id, count(*) n, max(ts)::date last_ts, (array_agg(left(action_title,40) order by ts desc))[1] last_title, (array_agg(coalesce(note,'') order by ts desc))[1] last_note, (array_agg(action_price order by ts desc))[1] last_aprice from oz_action_log where offer_id = any(%(o)s) group by 1,2", {}),
 'lad': ("select account, offer_id, rung, floor_price, cap_price, last_price, last_step_on from oz_action_ladder where offer_id = any(%(o)s)", {}),
 'rating': ("select account, sku, avg_rating, reviews_count from ozon_rating where sku = any(%(s)s)", {}),
 'fb': ("select account, item_id sku, rating, created_at::date d from raw_feedback where platform='ozon' and kind='review' and item_id = any(%(s)s)", {}),
 'fbo': ("select account, sku, captured_at::date d, warehouse, free_to_sell from ozon_fbo_stock where sku = any(%(s)s) and captured_at >= '2026-08-17'", {}),
 'ss_last': ("select external_code, string_agg(distinct left(supplier,18), '/') sup, sum(stock) stock, sum(coalesce(reserve,0)) res, count(distinct store) stores from supplier_stock where captured_at=(select max(captured_at) from supplier_stock) and external_code = any(%(k)s) and stock>0 group by 1", {}),
 'ads_bid': ("select distinct on (account, sku) account, sku, stat_date, bid from mkt_ozon_ads_sku_daily where sku = any(%(s)s) order by account, sku, stat_date desc", {}),
 'bids': ("select account, sku, count(distinct campaign_id) camps, max(bid) bid, string_agg(distinct state, ',') st from ozon_bids where captured_at=(select max(captured_at) from ozon_bids) and sku = any(%(s)s) group by 1,2", {}),
 'deliv': ("select p.account, e->>'offer_id' offer_id, p.payload->'financial_data'->>'cluster_from' cf, p.payload->'financial_data'->>'cluster_to' ct, extract(epoch from ((p.payload->>'delivering_date')::timestamptz - p.in_process_at))/86400 lag_d from raw_ozon_posting p, jsonb_array_elements(p.payload->'products') e where e->>'offer_id' = any(%(o)s) and p.in_process_at >= '2026-06-07' and p.in_process_at < '2026-07-06' and p.status<>'cancelled'", {}),
 'sig': ("select distinct on (account, offer_id) account, offer_id, days_without_sales, idc from ozon_stock_signals where offer_id = any(%(o)s) order by account, offer_id, captured_at desc", {}),
}
STK = pd.read_pickle(C + 'stock.pkl'); STK['day'] = pd.to_datetime(STK.day)
CODES = set(STK.external_code.dropna())
def skey(o): return o if o in CODES else (o[:4] if len(o) > 4 and o[:4] in CODES else None)
keys = sorted({k for k in (skey(o) for o in offs) if k})
R = {}
with conn() as cn:
    for k, (sql, _) in Q.items():
        R[k] = pd.read_sql(sql, cn, params={'o': offs, 's': all_skus, 'k': keys})
def by_offer_sku(df, agg):
    if df.empty: return {}
    df = df.copy(); df['sku'] = df.sku.astype(str)
    m = sku_map[sku_map.offer_id.isin(offs)]
    j = df.merge(m, on=['account', 'sku'])
    if j.empty: return {}
    return j.groupby(['account', 'offer_id']).apply(agg).stack().to_dict()

prod = R['prod'].set_index(['account', 'offer_id']); card = R['card'].set_index(['account', 'offer_id'])
st['arch_all'] = [prod.arch_all.get(k) for k in zip(st.account, st.offer_id)]
st['arch_any'] = [prod.arch_any.get(k) for k in zip(st.account, st.offer_id)]
for c_ in ['status', 'err_class', 'is_selling', 'healed', 'last_seen', 'err']:
    st['card_' + c_] = [card[c_].get(k) if k in card.index else 'нет записи' for k in zip(st.account, st.offer_id)]
pm = by_offer_sku(R['promo'], lambda g: pd.Series({'avail': bool(g.available.fillna(False).any()), 'promo_on': bool(g.promo_status.fillna(False).any()), 'views_week': g.views_week.fillna(0).sum(), 'vis_idx': g.visibility_index.max()}))
for c_ in ['avail', 'promo_on', 'views_week', 'vis_idx']:
    st['sp_' + c_] = [pm.get((a, o, c_)) for a, o in zip(st.account, st.offer_id)]
PI = R['pi']; PI['collected_on'] = PI.collected_on.astype(str)
def pi_get(a, o, d, c_):
    z = PI[(PI.account == a) & (PI.offer_id == o) & (PI.collected_on == d)]
    return z[c_].iloc[0] if len(z) else np.nan
for d_, tag in [('2026-08-06', '0806'), ('2026-08-17', '0817'), ('2026-09-01', '0901'), ('2026-09-05', '0905'), ('2026-09-14', 'now')]:
    for c_ in ['msp', 'price', 'color', 'ext_idx', 'ext_min']:
        if tag in ('0901', '0905') and c_ in ('ext_min',): continue
        st[f'pi_{c_}_{tag}'] = [pi_get(a, o, d_, c_) for a, o in zip(st.account, st.offer_id)]
for c_ in ['old_price', 'mprice', 'min_price', 'oz_idx', 'oz_min', 'self_idx', 'auto_act']:
    st[f'pi_{c_}_now'] = [pi_get(a, o, '2026-09-14', c_) for a, o in zip(st.account, st.offer_id)]
for tag in ['0806', '0817', '0901', 'now']:
    st[f'in_action_{tag}'] = np.where(st[f'pi_msp_{tag}'].isna(), None, st[f'pi_msp_{tag}'] < st[f'pi_price_{tag}'] - 0.5)
st['price_chg_base_now'] = (st.pi_msp_now / st.price_base - 1).round(3)
st['price_chg_0901_0905'] = (st.pi_msp_0905 / st.pi_msp_0901 - 1).round(3)
st['price_chg_0806_now'] = (st.pi_msp_now / st.pi_msp_0806 - 1).round(3)
st['ext_min_chg_0806_now'] = (st.pi_ext_min_now / st.pi_ext_min_0806 - 1).round(3)
act = R['act'].set_index(['account', 'offer_id']); lad = R['lad'].set_index(['account', 'offer_id'])
for c_ in ['n', 'last_ts', 'last_title', 'last_note', 'last_aprice']:
    st['actlog_' + c_] = [act[c_].get(k) if k in act.index else None for k in zip(st.account, st.offer_id)]
st['ladder_rung'] = [lad.rung.get(k) if k in lad.index else None for k in zip(st.account, st.offer_id)]
rt = by_offer_sku(R['rating'], lambda g: pd.Series({'avg': g.avg_rating.astype(float).mean(), 'n': g.reviews_count.sum()}))
st['rating_0717'] = [rt.get((a, o, 'avg')) for a, o in zip(st.account, st.offer_id)]
st['reviews_0717'] = [rt.get((a, o, 'n')) for a, o in zip(st.account, st.offer_id)]
FB = R['fb']; FB['d'] = pd.to_datetime(FB.d)
fb = by_offer_sku(FB, lambda g: pd.Series({'n': len(g), 'avg': g.rating.astype(float).mean(), 'last': g.d.max().date(), 'neg_since0706': int(((g.d >= T('2026-07-06')) & (g.rating.astype(float) <= 3)).sum()), 'n_since0706': int((g.d >= T('2026-07-06')).sum())}))
for c_ in ['n', 'avg', 'last', 'neg_since0706', 'n_since0706']:
    st['fb_' + c_] = [fb.get((a, o, c_)) for a, o in zip(st.account, st.offer_id)]
FBO = R['fbo']; FBO['d'] = pd.to_datetime(FBO.d)
fbo = by_offer_sku(FBO, lambda g: pd.Series({'now': g[g.d == g.d.max()].free_to_sell.sum(), 'wh_now': '/'.join(sorted(set(g[(g.d == g.d.max()) & (g.free_to_sell > 0)].warehouse.str[:14]))), 'days_pos': g[(g.free_to_sell > 0) & (g.d <= C1)].d.nunique()}))
st['fbo_now'] = [fbo.get((a, o, 'now'), 0) for a, o in zip(st.account, st.offer_id)]
st['fbo_wh_now'] = [fbo.get((a, o, 'wh_now'), '') for a, o in zip(st.account, st.offer_id)]
st['fbo_days_pos_28'] = [fbo.get((a, o, 'days_pos'), 0) for a, o in zip(st.account, st.offer_id)]
st['ms_key'] = [skey(o) for o in st.offer_id]
st['ms_key_is_parent'] = st.ms_key.notna() & (st.ms_key != st.offer_id)
ss = R['ss_last'].set_index('external_code')
st['ms_stock_now'] = [ss.stock.get(k, 0) if k else np.nan for k in st.ms_key]
st['ms_reserve_now'] = [ss.res.get(k, 0) if k else np.nan for k in st.ms_key]
st['ms_suppliers_now'] = [ss.sup.get(k, '') if k else '' for k in st.ms_key]
snap = sorted(d for d in STK.day.unique() if C0 <= d <= C1)
SD = STK[STK.day.isin(snap)].groupby('external_code').day.nunique().to_dict()
st['ms_zero_days_28'] = [len(snap) - SD.get(k, 0) if k else np.nan for k in st.ms_key]
st['ms_snap_days'] = len(snap)
ab = by_offer_sku(R['ads_bid'], lambda g: pd.Series({'bid': g.sort_values('stat_date').bid.iloc[-1], 'd': g.stat_date.max()}))
st['ads_last_bid'] = [ab.get((a, o, 'bid')) for a, o in zip(st.account, st.offer_id)]
st['ads_last_bid_date'] = [ab.get((a, o, 'd')) for a, o in zip(st.account, st.offer_id)]
bd = by_offer_sku(R['bids'], lambda g: pd.Series({'camps': g.camps.sum(), 'bid': g.bid.max(), 'st': ','.join(sorted(set(g.st)))}))
st['bids_now_camps'] = [bd.get((a, o, 'camps'), 0) for a, o in zip(st.account, st.offer_id)]
st['bids_now_bid'] = [bd.get((a, o, 'bid')) for a, o in zip(st.account, st.offer_id)]
st['bid_on_floor'] = st.bids_now_bid.astype(float) <= 8
DV = R['deliv']
dv = DV.groupby(['account', 'offer_id']).agg(cl_from=('cf', lambda s: s.mode().iloc[0] if s.notna().any() else None), cl_to_n=('ct', 'nunique'), lag_med=('lag_d', 'median')).to_dict('index')
st['base_cluster_from'] = [dv.get((a, o), {}).get('cl_from') for a, o in zip(st.account, st.offer_id)]
st['base_lag_to_delivering_d'] = [round(dv.get((a, o), {}).get('lag_med') or np.nan, 1) for a, o in zip(st.account, st.offer_id)]
sig = R['sig'].set_index(['account', 'offer_id'])
st['fbo_days_without_sales'] = [sig.days_without_sales.get(k) if k in sig.index else None for k in zip(st.account, st.offer_id)]
# конверсия
st['conv_order_per_search_view_b'] = (st.qty_b / 28 / st.search_v_b_d).round(4)
st['conv_order_per_search_view_c'] = np.where(st.search_v_c_d > 0, 0.0, np.nan)
st['search_ratio_c_b'] = (st.search_v_c_d / st.search_v_b_d).round(2)

# ---------- классификация ----------
out_b = (st.sold_pre_0501_0607 > 0) | (st.sold_mid_0706_0816 > 0) | (st.tx_days_0314_0430 > 0)
rand = (st.orders_b <= 2) | (st.days_b <= 2) | ~out_b
real = (st.orders_b >= 3) & (st.weeks_b >= 2) & out_b
st['klass'] = np.where(rand, 'случайный', np.where(real, 'реальный', 'пограничный'))
st['klass_why'] = [('заказов≤2 ' if a <= 2 else '') + ('дней≤2 ' if b <= 2 else '') + ('только baseline ' if not c else '') for a, b, c in zip(st.orders_b, st.days_b, out_b)]

# ---------- причина ----------
def cause(r):
    ev = []
    fam_c = np.nanmax([r.sib4_qty_c, r.sibname_qty_c if not pd.isna(r.sibname_qty_c) else 0])
    fam_b = np.nanmax([r.sib4_qty_b, r.sibname_qty_b if not pd.isna(r.sibname_qty_b) else 0])
    pc = r.price_chg_base_now
    inc = np.nanmax([r.sib4_qty_c - r.sib4_qty_b, (r.sibname_qty_c - r.sibname_qty_b) if not pd.isna(r.sibname_qty_c) else -1])
    flow = inc >= max(1, 0.5 * r.qty_b)
    if r.offer_id.lower().endswith('del'):
        return 'карточка выведена (артикул *del), продажи ушли на новую карточку' if flow else 'карточка выведена (артикул *del)', 'высокая'
    if r.arch_all is True: return 'карточка в архиве', 'высокая'
    if r.card_healed is None and r.card_status not in ('нет записи', 'price_sent') and r.card_err_class in ('C', 'W'): return f'карточка с ошибкой ({r.card_err_class})', 'средняя'
    if pd.isna(r.pi_msp_now) and r.sp_avail is not True: return 'нет на витрине Ozon сейчас (нет в индексе цен/недоступна)', 'средняя'
    if (not pd.isna(r.ms_stock_now)) and r.ms_stock_now - (r.ms_reserve_now or 0) <= 0 and r.fbo_now == 0: return 'остаток МС сейчас 0 (перебои)', 'средняя'
    if r.sp_avail is False: ev.append(('площадка видит «недоступен» (search_promo.available=false) при остатке МС', 'средняя'))
    if flow: ev.append(('продажи перетекли на соседнюю карточку семьи', 'высокая' if inc >= r.qty_b else 'средняя'))
    if not pd.isna(pc) and pc >= 0.10:
        bad = r.pi_color_now in ('RED', 'YELLOW') or (not pd.isna(r.pi_ext_idx_now) and r.pi_ext_idx_now > 1.05)
        ev.append((f'цена выросла {pc:+.0%}' + (' и выше конкурента (индекс)' if bad else ''), 'высокая' if (bad and pc >= 0.15) else 'средняя'))
    elif r.pi_color_now in ('RED',) or (not pd.isna(r.pi_ext_idx_now) and r.pi_ext_idx_now > 1.15):
        ev.append(('цена выше рынка (индекс RED/ext>1.15)', 'средняя'))
    if r.in_action_0806 is True and r.in_action_now is False: ev.append(('вышел из акции после 06.08', 'средняя'))
    if not pd.isna(r.ext_min_chg_0806_now) and r.ext_min_chg_0806_now <= -0.10 and (pd.isna(r.price_chg_0806_now) or abs(r.price_chg_0806_now) < 0.05): ev.append((f'конкурент снизил мин. цену {r.ext_min_chg_0806_now:+.0%}', 'средняя'))
    if fam_b >= 3 and fam_c <= 0.4 * fam_b: ev.append(('спрос всей семьи упал', 'низкая'))
    if (r.fb_neg_since0706 or 0) > 0 or (not pd.isna(r.rating_0717) and r.rating_0717 < 4): ev.append(('негатив в отзывах/рейтинг<4', 'низкая'))
    if not pd.isna(r.search_ratio_c_b) and r.search_ratio_c_b <= 0.4 and (pd.isna(pc) or abs(pc) < 0.10): ev.append((f'упали поисковые показы ×{r.search_ratio_c_b} при той же цене', 'средняя'))
    elif pd.isna(r.search_v_c_d) and not pd.isna(r.search_v_b_d): ev.append(('пропал из поисковой статистики', 'низкая'))
    if r.ad_views_ref_d > 0 and r.ad_views_c_d == 0: ev.append(('выпал из рекламы', 'низкая'))
    elif r.in_ads_now and r.bid_on_floor is True and r.ad_views_c_d < 20: ev.append(('реклама на полу ставки, мало показов', 'низкая'))
    if not ev: return 'не установлено (показы есть/нет данных, 0 заказов)', 'низкая'
    return ev[0][0] + (' | ' + '; '.join(e[0] for e in ev[1:3]) if len(ev) > 1 else ''), ev[0][1]
cc = st.apply(cause, axis=1)
st['cause'] = [c_[0] for c_ in cc]; st['confidence'] = [c_[1] for c_ in cc]
st['cause_main'] = st.cause.str.split(' \\| ').str[0].str.replace(r'[+\-]\d+%', 'X%', regex=True).str.replace(r'×[\d.]+', '×k', regex=True)
st = st.sort_values('rev_b', ascending=False)
st.to_csv(O + 'state.csv', index=False)

# ---------- сводки в файл ----------
with open(O + 'summary.txt', 'w') as f:
    f.write(f'cohort {len(st)} rev_b {st.rev_b.sum():.0f}; acc {st.account.value_counts().to_dict()}; in_ads_now {int(st.in_ads_now.sum())}\n')
    f.write(f'drop_nodata: {len(nod)} SKU, rev_b {nod.rev_b.sum():.0f}, qty_b>=2: {(nod.qty_b>=2).sum()}, acc {nod.account.value_counts().to_dict()}\n')
    f.write(st.groupby('klass').agg(n=('offer_id', 'size'), rev_b=('rev_b', 'sum')).to_string() + '\n')
    f.write(st.groupby(['klass', 'cause_main']).agg(n=('offer_id', 'size'), rev_b=('rev_b', 'sum')).sort_values('rev_b', ascending=False).to_string() + '\n')
    f.write('flags: arch_any %d, sp_avail_false %d, pi_missing_now %d, in_action_0806 %d now %d, ms_parent_key %d, ms_stock0 %d, fbo>0 %d, bid_floor %d, rating_n %d, fb_n %d\n' % (
        (st.arch_any == True).sum(), (st.sp_avail == False).sum(), st.pi_msp_now.isna().sum(), (st.in_action_0806 == True).sum(), (st.in_action_now == True).sum(),
        st.ms_key_is_parent.sum(), (st.ms_stock_now == 0).sum(), (st.fbo_now > 0).sum(), (st.bid_on_floor == True).sum(), st.rating_0717.notna().sum(), st.fb_n.notna().sum()))
print(open(O + 'summary.txt').read()[:3000])
