import pandas as pd, numpy as np
O = '/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/drop118/'
s = pd.read_csv(O + 'state.csv', dtype={'offer_id': str, 'ms_key': str})
def f(x, n=0):
    if pd.isna(x): return '—'
    return f'{x:.{n}f}' if isinstance(x, (float, np.floating)) else str(x)
def check(r):
    c = r.cause
    if 'del' in c: return 'убедиться, что новая карточка-преемник в продаже и в рекламе'
    if 'недоступен' in c or 'витрине' in c: return 'ЛК: статус карточки/остаток FBS на складе, «Готов к продаже»'
    if 'цена' in c: return 'ЛК Цены: индекс/мин. цена конкурента, участие в акциях; сравнить с ценой последней продажи'
    if 'остаток' in c: return 'остаток у поставщика, передача остатка FBS'
    if 'поисков' in c: return 'ЛК Аналитика: позиция/показы, конкуренты в выдаче, доставка/срок в карточке'
    if 'реклам' in c: return 'Продвижение: ставка/показы по SKU'
    if 'перетекли' in c: return 'нет действия: спрос на соседней карточке'
    return 'ЛК: карточка, отзывы, срок доставки'
rows = ['acc|offer|название|точный ключ|класс|rev_b|q7/14/28 (отм28)|посл.прод|остаток МС (P=родит.)/FBO/нулевых дней|цена base→mid→now (Δ)|цвет/ext_idx/ext_min|акция 06.08→14.09|поиск/д base→cur, поз|реклама показ/д, ставка, available|рейтинг 17.07/нег.отз|причина|уверенность|проверить в ЛК']
for _, r in s.head(20).iterrows():
    rows.append('|'.join([r.account[-1], r.offer_id, r['name'][:40], f(r.exact_key), r.klass, f(r.rev_b), f'{f(r.q7)}/{f(r.q14)}/{f(r.q28)} ({f(r.cancel_28)})', f(r.last_sale),
        f'{f(r.ms_stock_now)}{"P" if r.ms_key_is_parent else ""}/{f(r.fbo_now)}/{f(r.ms_zero_days_28)} {str(r.ms_suppliers_now)[:30] if isinstance(r.ms_suppliers_now, str) else ""}',
        f'{f(r.price_base)}→{f(r.price_mid_0706_0816)}→{f(r.pi_msp_now)} ({f(r.price_chg_base_now, 2)})', f'{r.pi_color_now}/{f(r.pi_ext_idx_now, 2)}/{f(r.pi_ext_min_now)}',
        f'{r.in_action_0806}→{r.in_action_now}', f'{f(r.search_v_b_d, 1)}→{f(r.search_v_c_d, 1)}, {f(r.search_pos_b)}→{f(r.search_pos_c)}',
        f'{f(r.ad_views_c_d, 1)}, {f(r.bids_now_bid)}, {r.sp_avail}', f'{f(r.rating_0717, 1)}/{f(r.fb_neg_since0706)}', r.cause, r.confidence, check(r)]))
open(O + 'top20.txt', 'w').write('\n'.join(rows) + '\n')
s['check_in_lk'] = [check(r) for _, r in s.iterrows()]
s.to_csv(O + 'state.csv', index=False)
real = s[s.klass == 'реальный']
g = real.groupby('cause_main').agg(n=('offer_id', 'size'), rev_b=('rev_b', 'sum')).sort_values('rev_b', ascending=False)
g.to_csv(O + 'real_causes.csv')
av = s[s.sp_avail == False][['account', 'offer_id', 'rev_b', 'klass', 'ms_stock_now', 'last_sale']]
av.to_csv(O + 'avail_false.csv', index=False)
print(len(av), int(av.rev_b.sum()), 'pi_missing:', s.pi_msp_now.isna().sum(), 'sold_0914:', int((s.q_0914_partial > 0).sum()))
