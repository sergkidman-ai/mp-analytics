# Мост выручки заказов (postings, без отмен) baseline -> current по SKU (account, offer_id). Read-only, кэш s1.
import pandas as pd, numpy as np, re, os, sys, json
D = os.path.dirname(os.path.abspath(__file__)); C = D + '/cache'
P = pd.read_pickle(f'{C}/post_lines.pkl'); P['day'] = pd.to_datetime(P.day)
P['price'] = P.price.astype(float); P['qty'] = P.qty.astype(float); P['rev'] = P.price * P.qty
OK = P[P.status != 'cancelled']
ST = pd.read_pickle(f'{C}/stock.pkl'); ST['day'] = pd.to_datetime(ST.day)
SR = pd.read_pickle(f'{C}/search.pkl'); SR['period_start'] = pd.to_datetime(SR.period_start)
AS = pd.read_pickle(f'{C}/ads_sku.pkl'); AS['day'] = pd.to_datetime(AS.day)
for c in ['views', 'clicks', 'spend', 'orders', 'orders_money']: AS[c] = AS[c].astype(float)
T = pd.Timestamp
CODES = set(ST.external_code.dropna())
STOCK_DAYS = ST.groupby('external_code').day.apply(set).to_dict()
SNAP_DAYS = set(ST.day.unique())
AD_REF = (T('2026-07-27'), T('2026-08-08'))   # самый ранний период SKU-рекламы (режим A: хвост в рекламе)

def stock_key(off):
    if off in CODES: return off
    if len(off) > 4 and off[:4] in CODES: return off[:4]
    return None

# --- семьи ---
BRANDS = ['HP', 'Canon', 'Kyocera', 'Brother', 'Xerox', 'Samsung', 'Epson', 'Pantum', 'Ricoh', 'Sharp', 'Konica Minolta', 'Konica', 'OKI', 'Lexmark',
          'Panasonic', 'Toshiba', 'Riso', 'Katun', 'Develop', 'Sindoh', 'Avision', 'Deli', 'Катюша', 'F+', 'Utax', 'Triumph-Adler', 'Dell', 'Minolta']
COL = r'(?:XXL|XL)?(?:BK|PBK|MBK|K|C|M|Y|LC|LM|GY|R|B)?'
def family(name):
    n = name or ''
    brand = next((b for b in BRANDS if re.search(r'(?<![A-Za-zА-Яа-я])' + re.escape(b) + r'(?![A-Za-z])', n, re.I)), None)
    m = re.match(r'^\s*(?:Комплект картриджей|Картриджи|Картридж|Чернила|Фотобарабан|Тонер-картридж|Тонер|Драм-картридж|Барабан|Блок фотобарабана|Набор[^ ]*|Контейнер[^ ]*|Печатающая головка|Бункер[^ ]*)\s+(?:DS\s+|лазерный\s+|струйный\s+)?(?:№)?([A-Za-z0-9][A-Za-z0-9\-\./]*[0-9][A-Za-z0-9\-]*)', n)
    code = None
    if not m:
        head = re.split(r'\sдля\s', n)[0]
        m = re.search(r'(?<![A-Za-z0-9])([A-Za-z0-9][A-Za-z0-9\-\./]*[0-9][A-Za-z0-9\-]*)', re.sub(r'\(\d+\s*шт\.?\)', '', head))
        if m and len(m.group(1)) < 3: m = None
    if m:
        code = m.group(1).upper().split('-W')[0] if m.group(1).upper().startswith('W') else m.group(1).upper()
        code = re.split(r'\s', code)[0]
        # диапазоны HP: CE260X-CE263A -> CE260X
        mm = re.match(r'^((?:CE|CF|CB|CC|Q|W|CH|C)\d{3,5}[AXY])-', code)
        if mm: code = mm.group(1)
        hp = re.match(r'^((?:CE|CF|CB|CC|Q|W))(\d{3,5})([AXY])$', code)
        if hp: code = f'{hp.group(1)}{hp.group(2)[:-1]}x{hp.group(3)}'      # цвет в последней цифре
        else:
            code = re.sub(r'-(?:[A-Z]{1,3}\d*[A-Z]*)$', '', code) if re.search(r'\d.*-[A-Z]+\d*[A-Z]*$', code) and not re.match(r'^[A-Z]+-\d+$', code) else code
            code = re.sub(r'(\d)(XXL|XL)?(BK|PBK|MBK|K|C|M|Y|LC|LM|GY)$', lambda z: z.group(1) + (z.group(2) or ''), code)
        code = code.replace('№', '')
    return (brand or '?') + ' ' + (code or '(без кода)'), brand, code

def per_period(a, b):
    nd = (b - a).days + 1
    x = OK[(OK.day >= a) & (OK.day <= b)]
    g = x.groupby(['account', 'offer_id']).agg(rev=('rev', 'sum'), qty=('qty', 'sum')).reset_index()
    g['r'] = g.rev / nd; g['q'] = g.qty / nd
    return g, nd

def avail(off, a, b):
    k = stock_key(off)
    if k is None: return np.nan
    days = [d for d in SNAP_DAYS if a <= d <= b]
    if not days: return np.nan
    s = STOCK_DAYS.get(k, set())
    return sum(1 for d in days if d in s) / len(days)

def search_pd(a, b):
    wk = SR[(SR.period_start >= a) & (SR.period_start + pd.Timedelta(days=6) <= b)]
    nw = wk.period_start.nunique()
    g = wk.groupby(['account', 'offer_id']).agg(sv=('unique_view_users', 'sum'), spos=('position', 'mean')).reset_index()
    g['v'] = g.sv / (nw * 7) if nw else np.nan
    return g[['account', 'offer_id', 'v', 'spos']], nw

def ads_pd(a, b):
    x = AS[(AS.day >= max(a, T('2026-07-27'))) & (AS.day <= b)]
    nd = (b - max(a, T('2026-07-27'))).days + 1
    if nd <= 0 or len(x) == 0: return None
    g = x.groupby(['account', 'offer_id']).agg(adv=('views', 'sum'), adcl=('clicks', 'sum'), adsp=('spend', 'sum'), ado=('orders', 'sum'), adom=('orders_money', 'sum')).reset_index()
    for c in ['adv', 'adcl', 'adsp', 'ado', 'adom']: g[c] = g[c] / nd
    return g

def bridge(tag, a0, b0, a1, b1):
    B, nb = per_period(a0, b0); Cu, nc = per_period(a1, b1)
    m = B.merge(Cu, on=['account', 'offer_id'], how='outer', suffixes=('_b', '_c')).fillna({'rev_b': 0, 'qty_b': 0, 'r_b': 0, 'q_b': 0, 'rev_c': 0, 'qty_c': 0, 'r_c': 0, 'q_c': 0})
    m['a_b'] = [avail(o, a0, b0) for o in m.offer_id]; m['a_c'] = [avail(o, a1, b1) for o in m.offer_id]
    sb, nwb = search_pd(a0, b0); sc, nwc = search_pd(a1, b1)
    m = m.merge(sb.rename(columns={'v': 'v_b', 'spos': 'pos_b'}), on=['account', 'offer_id'], how='left').merge(sc.rename(columns={'v': 'v_c', 'spos': 'pos_c'}), on=['account', 'offer_id'], how='left')
    adb = ads_pd(*AD_REF) if a0 < T('2026-07-27') else ads_pd(a0, b0)
    adc = ads_pd(a1, b1); adnow = ads_pd(b1 - pd.Timedelta(days=6), b1)
    m = m.merge(adb.add_suffix('_b').rename(columns={'account_b': 'account', 'offer_id_b': 'offer_id'}), on=['account', 'offer_id'], how='left')
    m = m.merge(adc.add_suffix('_c').rename(columns={'account_c': 'account', 'offer_id_c': 'offer_id'}), on=['account', 'offer_id'], how='left')
    m = m.merge(adnow[['account', 'offer_id', 'adv']].rename(columns={'adv': 'adv_last7'}), on=['account', 'offer_id'], how='left')
    for c in ['adv_b', 'adsp_b', 'adv_c', 'adsp_c', 'adv_last7', 'adom_b', 'adom_c', 'adcl_b', 'adcl_c']: m[c] = m[c].fillna(0)
    m['in_ads_now'] = m.adv_last7 > 0
    m['left_ads'] = (m.adv_b > 0) & (m.adv_c == 0)
    m['dR'] = m.r_c - m.r_b
    comp_cols = ['drop_nostock', 'drop_stock', 'drop_nodata', 'new', 'price', 'avail', 'traffic_left_ads', 'traffic_other', 'conv', 'vol_unexplained']
    for c in comp_cols: m[c] = 0.0
    cont = (m.q_b > 0) & (m.q_c > 0)
    drop = (m.q_b > 0) & (m.q_c == 0); new = (m.q_b == 0) & (m.q_c > 0)
    m.loc[drop & (m.a_c < 0.5), 'drop_nostock'] = m.dR
    m.loc[drop & (m.a_c >= 0.5), 'drop_stock'] = m.dR
    m.loc[drop & m.a_c.isna(), 'drop_nodata'] = m.dR
    m.loc[new, 'new'] = m.dR
    m['info_drop_tail'] = np.where(drop & (m.qty_b <= 2), m.dR, 0.0); m['info_new_tail'] = np.where(new & (m.qty_c <= 2), m.dR, 0.0)
    m['p_b'] = np.where(m.q_b > 0, m.r_b / m.q_b.replace(0, np.nan), np.nan); m['p_c'] = np.where(m.q_c > 0, m.r_c / m.q_c.replace(0, np.nan), np.nan)
    V = np.where(cont, m.p_b * (m.q_c - m.q_b), 0.0); Pr = np.where(cont, m.q_c * (m.p_c - m.p_b), 0.0)
    m['price'] = Pr; m['vol'] = V
    for i in np.where(cont)[0]:
        r = m.iloc[i]; v = V[i]
        if v == 0: continue
        lq = np.log(r.q_c / r.q_b)
        if abs(lq) < 1e-12: m.iat[i, m.columns.get_loc('vol_unexplained')] = v; continue
        has_a = pd.notna(r.a_b) and pd.notna(r.a_c) and r.a_b > 0 and r.a_c > 0
        has_v = pd.notna(r.v_b) and pd.notna(r.v_c) and r.v_b > 0 and r.v_c > 0
        parts = {}
        if has_a and has_v:
            parts['avail'] = np.log(r.a_c / r.a_b); parts['traffic'] = np.log((r.v_c / r.a_c) / (r.v_b / r.a_b)); parts['conv'] = np.log((r.q_c / r.v_c) / (r.q_b / r.v_b))
        elif has_v:
            parts['traffic'] = np.log(r.v_c / r.v_b); parts['conv'] = np.log((r.q_c / r.v_c) / (r.q_b / r.v_b))
        elif has_a:
            parts['avail'] = np.log(r.a_c / r.a_b); parts['vol_unexplained'] = lq - parts['avail']
        else:
            parts['vol_unexplained'] = lq
        for k, lk in parts.items():
            col = k if k != 'traffic' else ('traffic_left_ads' if r.left_ads else 'traffic_other')
            m.iat[i, m.columns.get_loc(col)] += v * lk / lq
    m['check'] = m[comp_cols].sum(axis=1) - m.dR
    assert m.check.abs().max() < 1e-6, m.check.abs().max()
    # диагноз
    def diag(r):
        if r.q_b > 0 and r.q_c == 0:
            if pd.isna(r.a_c): return 'наличие?'
            if r.a_c < 0.5: return 'наличие'
            if r.left_ads: return 'реклама'
            if pd.notna(r.v_b) and (pd.isna(r.v_c) or r.v_c < 0.5 * r.v_b): return 'показы'
            return 'конверсия'
        if r.q_b == 0: return 'новый'
        cands = {'цена': r.price, 'наличие': r.avail, ('реклама' if r.left_ads else 'показы'): r.traffic_left_ads + r.traffic_other, 'конверсия': r.conv, 'объём?': r.vol_unexplained}
        k = min(cands, key=cands.get)
        if cands[k] >= 0: return 'рост'
        if k == 'конверсия' and r.adcl_b > 0 and r.adv_b > 0 and r.adv_c > 0 and (r.adcl_c / r.adv_c) < 0.7 * (r.adcl_b / r.adv_b): k = 'CTR'
        return k
    m['diag'] = m.apply(diag, axis=1)
    names = OK.sort_values('day').groupby(['account', 'offer_id']).name.last()
    m['name'] = [names.get((a, o), '') for a, o in zip(m.account, m.offer_id)]
    fam = [family(n) for n in m.name]
    m['family'] = [f[0] for f in fam]
    # дети без кода -> семья родителя (offer_id[:4]) того же аккаунта
    par = {(a, o): f for a, o, f in zip(m.account, m.offer_id, m.family) if len(o) == 4 and '(без кода)' not in f}
    m['family'] = [par.get((a, o[:4]), f) if '(без кода)' in f else f for a, o, f in zip(m.account, m.offer_id, m.family)]
    m.to_csv(f'{D}/10_{tag}_sku_bridge_full.csv', index=False)
    # итог моста
    rows = []
    for acc in ['oz_acc1', 'oz_acc2', 'sum']:
        x = m if acc == 'sum' else m[m.account == acc]
        d = {'account': acc, 'R_base_per_day': x.r_b.sum(), 'R_cur_per_day': x.r_c.sum(), 'dR_per_day': x.dR.sum()}
        for c in comp_cols: d[c] = x[c].sum()
        d['sum_components'] = sum(d[c] for c in comp_cols)
        d['info_drop_tail_le2u'] = x.info_drop_tail.sum(); d['info_new_tail_le2u'] = x.info_new_tail.sum()
        d['info_cont_rev_share_base'] = x[(x.q_b > 0) & (x.q_c > 0)].r_b.sum() / max(1e-9, x.r_b.sum())
        unexpl = d['vol_unexplained'] + d['drop_nodata']
        d['unexplained_total'] = unexpl
        d['explained_share_of_dR'] = (d['dR_per_day'] - unexpl) / d['dR_per_day'] if d['dR_per_day'] else np.nan
        d['explained_share_abs'] = 1 - abs(unexpl) / sum(abs(d[c]) for c in comp_cols)
        d['n_sku_base'] = int((x.q_b > 0).sum()); d['n_sku_cur'] = int((x.q_c > 0).sum()); d['n_drop'] = int(((x.q_b > 0) & (x.q_c == 0)).sum()); d['n_new'] = int(((x.q_b == 0) & (x.q_c > 0)).sum())
        d['n_cont'] = int(((x.q_b > 0) & (x.q_c > 0)).sum())
        d['rev_share_cont_with_stock_and_search_base'] = x[(x.q_b > 0) & x.a_b.notna() & x.v_b.notna()].r_b.sum() / max(1e-9, x.r_b.sum())
        d['search_weeks_base'] = nwb; d['search_weeks_cur'] = nwc; d['days_base'] = nb; d['days_cur'] = nc
        rows.append(d)
    br = pd.DataFrame(rows)
    br.to_csv(f'{D}/11_{tag}_bridge_summary.csv', index=False)
    # топ-20
    top = m.sort_values('dR').head(20).copy()
    top['dR_28d'] = top.dR * 28
    top['dprice_pct'] = (top.p_c / top.p_b - 1) * 100
    top['dqty_28d'] = (top.q_c - top.q_b) * 28
    top['search_v_base_wk'] = top.v_b * 7; top['search_v_cur_wk'] = top.v_c * 7
    top['ad_views_base_d'] = top.adv_b; top['ad_views_cur_d'] = top.adv_c; top['ad_spend_base_d'] = top.adsp_b; top['ad_spend_cur_d'] = top.adsp_c
    top['name_short'] = top.name.str.slice(0, 60)
    cols = ['account', 'offer_id', 'name_short', 'family', 'r_b', 'r_c', 'dR', 'dR_28d', 'dqty_28d', 'dprice_pct', 'a_b', 'a_c', 'search_v_base_wk', 'search_v_cur_wk', 'pos_b', 'pos_c',
            'ad_views_base_d', 'ad_views_cur_d', 'ad_spend_base_d', 'ad_spend_cur_d', 'in_ads_now', 'left_ads', 'diag', 'price', 'avail', 'traffic_left_ads', 'traffic_other', 'conv', 'vol_unexplained', 'drop_nostock', 'drop_stock', 'drop_nodata']
    top[cols].to_csv(f'{D}/12_{tag}_top20_sku.csv', index=False)
    # семьи
    fg = m.groupby('family').agg(n_sku=('offer_id', 'count'), accounts=('account', lambda s: ','.join(sorted(set(s)))), R_base=('r_b', 'sum'), R_cur=('r_c', 'sum'), dR=('dR', 'sum'),
                                 **{c: (c, 'sum') for c in comp_cols}).reset_index()
    fg['dR_28d'] = fg.dR * 28
    fg.sort_values('dR').to_csv(f'{D}/13_{tag}_families.csv', index=False)
    # доля семей с кодом
    fam_cov = m[~m.family.str.contains(r'\(без кода\)')].r_b.sum() / m.r_b.sum()
    # диагнозы суммарно
    dg = m[m.dR < 0].groupby('diag').dR.agg(['count', 'sum']).reset_index(); dg.to_csv(f'{D}/14_{tag}_diag_counts.csv', index=False)
    return br, top, fg, fam_cov, m

if __name__ == '__main__':
    runs = {'A': ('2026-06-08', '2026-07-05', '2026-08-17', '2026-09-13'),
            'B': ('2026-06-08', '2026-07-05', '2026-07-27', '2026-08-23')}
    out = {}
    for tag, (a0, b0, a1, b1) in runs.items():
        br, top, fg, fc, m = bridge(tag, T(a0), T(b0), T(a1), T(b1))
        s = br[br.account == 'sum'].iloc[0]
        print(tag, 'Rb/d', round(s.R_base_per_day), 'Rc/d', round(s.R_cur_per_day), 'dR/d', round(s.dR_per_day), 'expl', round(s.explained_share_of_dR, 3), round(s.explained_share_abs, 3), 'fam_cov', round(fc, 3), 'cov_base', round(s.rev_share_cont_with_stock_and_search_base, 3))
