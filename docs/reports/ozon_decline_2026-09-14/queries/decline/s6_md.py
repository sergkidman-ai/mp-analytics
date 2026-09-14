# Сборка report.md из CSV (read-only)
import pandas as pd, numpy as np, os
D = os.path.dirname(os.path.abspath(__file__))
def f(v, k=0, pct=False):
    if v is None or (isinstance(v, float) and np.isnan(v)): return '—'
    if pct: return f'{v*100:.1f}%'
    if isinstance(v, (int, np.integer)): return f'{v:,}'.replace(',', ' ')
    if isinstance(v, (float, np.floating)): return f'{v:,.{k}f}'.replace(',', ' ')
    return str(v)
def md(df, cols, fmt=None):
    fmt = fmt or {}
    out = ['| ' + ' | '.join(c for c in cols) + ' |', '|' + '---|' * len(cols)]
    for _, r in df.iterrows():
        out.append('| ' + ' | '.join(fmt[c](r[c]) if c in fmt else f(r[c]) for c in cols) + ' |')
    return '\n'.join(out)
M = lambda v: f(v / 1e6, 2) if pd.notna(v) else '—'
K = lambda v: f(v / 1e3, 0) if pd.notna(v) else '—'
PC = lambda v: f(v, pct=True)
I0 = lambda v: f(float(v), 0) if pd.notna(v) else '—'
L = []
L.append('# Падение Ozon (oz_acc1 + oz_acc2): измерение и мост в рублях — 14.09.2026\n')
L.append('Read-only. Скрипты `s1_extract.py` (выгрузка SELECT в cache), `s2_metrics.py`, `s3_bridge.py`, `s4_extra.py`, `s5_report.py`, `s6_md.py`. Все таблицы — CSV рядом.\n')
L.append('**Источники и границы:** выручка по транзакциям (Σ accruals_for_sale>0) — полна по 07.09; заказы — raw_ozon_posting с 01.05 по 13.09 (цена продавца × шт., отмены отдельно); реклама аккаунта — ad_spend_daily/ozon_ads с 01.06; реклама по SKU — mkt_ozon_ads_sku_daily с 27.07 (~40 % расхода); поиск — ozon_search_product, только полные недели, последняя 31.08–06.09; остатки — supplier_stock (МС, любой склад, stock>0) с 22.06, ключ offer_id или родитель offer_id[:4]; воронки нет.\n')

mon = pd.read_csv(f'{D}/01_months_mtd.csv')
L.append('## 1. Месяцы и MTD\n')
for acc in ['sum', 'oz_acc1', 'oz_acc2']:
    x = mon[mon.account == acc]
    L.append(f'**{acc}** (выручка, заказы в ₽ — млн)\n')
    L.append(md(x, ['period', 'tx_revenue', 'rev_orders', 'orders', 'units', 'avg_check', 'avg_unit_price', 'sku_sold', 'cancel_share', 'cancel_rev', 'returns_cnt', 'ad_spend_all', 'drr_all_vs_orders'],
                {'tx_revenue': M, 'rev_orders': M, 'cancel_rev': M, 'ad_spend_all': K, 'cancel_share': PC, 'drr_all_vs_orders': PC, 'orders': I0, 'units': I0, 'sku_sold': I0, 'avg_check': I0, 'avg_unit_price': I0}))
    L.append('')
L.append('ad_spend_all — тыс ₽; DRR = весь расход Performance / выручка заказов. returns_cnt — mp_returns по дате создания (физические возвраты FBS/ПВЗ). Сентябрь транзакций 1–7.09, заказов 1–13.09; отмены последних дней ещё дорастут.\n')
am = pd.read_pickle(f'{D}/cache/ads_month.pkl'); am['spend'] = am.spend.astype(float)
am = am.groupby('period').sum(numeric_only=True).reset_index(); am['ctr'] = am.clicks / am.views; am['cpc'] = am.spend / am.clicks
L.append('Реклама Performance по месяцам (ozon_ads, оба аккаунта; сентябрь по 14.09):\n')
L.append(md(am, ['period', 'spend', 'views', 'clicks', 'ctr', 'cpc', 'orders', 'ad_revenue'], {'spend': K, 'views': I0, 'clicks': I0, 'ctr': PC, 'cpc': lambda v: f(v, 1), 'orders': I0, 'ad_revenue': M}))
co = pd.read_csv(f'{D}/04_contribution_est_monthly.csv'); co = co[co.account == 'sum']
L.append('\nContribution — **оценка, не прибыль BI**: сальдо транзакций с items[] − себест margin_by_sku; оверхед (операции без items: реклама, подписка, эквайринг…) отдельно. Сентябрь — операции по 08.09 (частично).\n')
L.append(md(co, ['m', 'revenue', 'saldo_items', 'cogs', 'contribution_est', 'contrib_pct', 'overhead_noitems'], {'revenue': M, 'saldo_items': M, 'cogs': M, 'contribution_est': M, 'overhead_noitems': M, 'contrib_pct': PC}))

win = pd.read_csv(f'{D}/02_windows.csv'); so = pd.read_csv(f'{D}/05_stockout_windows.csv')
L.append('\n## 2. Окна (последние полные дни: заказы по 13.09, транзакции по 07.09)\n')
L.append('prev — предыдущее равное окно; minus4w/minus8w — то же окно на 28/56 дней раньше. Метрики SKU-рекламы только где окно ≥27.07.\n')
for acc in ['sum', 'oz_acc1', 'oz_acc2']:
    x = win[(win.account == acc) & (win.src == 'postings')].copy()
    x['win'] = x.period.str.replace('postings_', '')
    s2 = so[so.account == acc].set_index('window').oos_share_sku_days
    x['oos'] = [s2.get(w, np.nan) if so[(so.account == acc) & (so.window == w)].full.all() else np.nan for w in x.win]
    L.append(f'**{acc} — заказы (postings)**\n')
    L.append(md(x, ['win', 'date_from', 'date_to', 'rev_orders', 'orders', 'units', 'avg_unit_price', 'sku_sold', 'cancel_share', 'returns_cnt', 'ad_spend_all', 'drr_all_vs_orders', 'skuads_views', 'skuads_ctr', 'skuads_cpc', 'skuads_drr', 'oos'],
                {'rev_orders': M, 'orders': I0, 'units': I0, 'avg_unit_price': I0, 'sku_sold': I0, 'cancel_share': PC, 'ad_spend_all': K, 'drr_all_vs_orders': PC, 'skuads_views': I0, 'skuads_ctr': PC, 'skuads_cpc': lambda v: f(v, 1), 'skuads_drr': PC, 'oos': PC}))
    y = win[(win.account == acc) & (win.src == 'tx')].copy(); y['win'] = y.period.str.replace('tx_', '')
    L.append(f'\n**{acc} — выручка по транзакциям**\n')
    L.append(md(y, ['win', 'date_from', 'date_to', 'tx_revenue', 'tx_returns', 'tx_saldo'], {'tx_revenue': M, 'tx_returns': M, 'tx_saldo': M}))
    L.append('')
L.append('oos — доля SKU-дней без остатка в МС у активных SKU (85 % активных SKU имеют ключ остатка); окна до 22.06 — «—».\n')

wk = pd.read_csv(f'{D}/03_weekly.csv')
L.append('## 3. Недельный тренд (пн–вс, только полные недели)\n')
pv = wk.pivot_table(index='period', columns='account', values=['rev_orders', 'units', 'ad_spend_all', 'search_views', 'skuads_views', 'tx_revenue'], aggfunc='first')
t = pd.DataFrame({'week': pv.index})
for c, a in [('rev_orders', 'sum'), ('rev_orders', 'oz_acc1'), ('rev_orders', 'oz_acc2'), ('units', 'sum'), ('units', 'oz_acc1'), ('units', 'oz_acc2'), ('tx_revenue', 'sum'), ('ad_spend_all', 'sum'), ('skuads_views', 'sum'), ('search_views', 'oz_acc1'), ('search_views', 'oz_acc2')]:
    t[f'{c}_{a}'] = pv[(c, a)].values
ws = so[(so.account == 'sum') & so.window.str.startswith('week')].set_index('window').oos_share_sku_days
t['oos'] = [ws.get(w, np.nan) for w in t.week]
t['week'] = t.week.str.replace('week_', '')
t.to_csv(f'{D}/03b_weekly_wide.csv', index=False)
L.append(md(t, list(t.columns), {**{c: M for c in t.columns if c.startswith(('rev_', 'tx_'))}, **{c: K for c in t.columns if c.startswith('ad_spend')}, **{c: I0 for c in t.columns if c.startswith(('units', 'skuads', 'search'))}, 'oos': PC}))
L.append('\nsearch_views — Σ unique_view_users по SKU за неделю (неделя 07.09 ещё не собрана). skuads_views — показы SKU-статистики CPC (≈40 % расхода), с 27.07.\n')

L.append('''## 4. Baseline и current

- **Baseline = 08.06–05.07 (4 полные недели).** Последнее плато до перелома: штуки 444/508/464/510 в неделю, выручка заказов 2,78–3,57 млн/нед; следующая неделя (06.07) — 415 шт, затем 343/369/356/327/317 — перелом приходится на неделю 06.07 (acc1 штуки 442→309, поисковые показы acc1 194→181→170→140 тыс к 20.07). Длина 28 дней = длине current, иначе мост по SKU тонет в ротации хвоста (при 14-дневной базе «выпавших»/«новых» было бы вдвое больше, проверено). Остатки и поиск покрывают только вторую половину базы (22.06–05.07) — факторы доступности/трафика для базы оценены по ней.
- **Current (как задано) = 17.08–13.09 (28 дней).** Но в него входит резкий отскок недель 31.08 и 07.09 (4,30 и 4,83 млн против 2,28–2,77), поэтому выручка current **выше** baseline.
- **Дополнительно — «дно» = 27.07–23.08 (4 недели минимальной выручки заказов)**: мост B объясняет само падение; он же целиком покрыт SKU-рекламой.
- Реклама по SKU до 27.07 не существует, поэтому «уход из рекламы» = SKU с показами 27.07–08.08 (режим A, хвост ещё в рекламе) и нулём показов в сравниваемом периоде.
''')

L.append('## 5. Мост выручки заказов (без отмен), ₽/день\n')
comp = [('drop_nostock', 'Выпавшие SKU: остатка не было ≥50 % дней current'), ('drop_stock', 'Выпавшие SKU: остаток был'), ('drop_nodata', 'Выпавшие SKU: нет данных об остатке'),
        ('new', 'Новые SKU'), ('price', 'Цена (продолжающие, q_cur×Δp)'), ('avail', 'Объём: доступность (доля дней в наличии)'), ('traffic_left_ads', 'Объём: поисковые показы — SKU, ушедшие из рекламы'),
        ('traffic_other', 'Объём: поисковые показы — прочие'), ('conv', 'Объём: конверсия (шт / поисковый показ)'), ('vol_unexplained', 'Объём: без покрытия факторов (необъяснённое)')]
for tag, title in [('A', 'Мост A: baseline 08.06–05.07 → current 17.08–13.09'), ('B', 'Мост B: baseline 08.06–05.07 → дно 27.07–23.08')]:
    b = pd.read_csv(f'{D}/11_{tag}_bridge_summary.csv').set_index('account')
    rows = [('R baseline, ₽/день', 'R_base_per_day'), ('R current, ₽/день', 'R_cur_per_day')] + [(t2, c) for c, t2 in comp] + [('**ΔR итого, ₽/день**', 'dR_per_day'), ('ΔR итого × 28 дней', None),
            ('справочно: выпавшие с ≤2 шт в базе', 'info_drop_tail_le2u'), ('справочно: новые с ≤2 шт в current', 'info_new_tail_le2u'), ('необъяснённое (объём без покрытия + выпавшие без данных об остатке)', 'unexplained_total')]
    L.append(f'**{title}**\n')
    L.append('| компонента | oz_acc1 | oz_acc2 | сумма |\n|---|---|---|---|')
    for lab, c in rows:
        if c is None: vals = [b.loc[a, 'dR_per_day'] * 28 for a in ['oz_acc1', 'oz_acc2', 'sum']]
        else: vals = [b.loc[a, c] for a in ['oz_acc1', 'oz_acc2', 'sum']]
        L.append(f'| {lab} | ' + ' | '.join(f(v, 0) for v in vals) + ' |')
    L.append('| SKU база / current / выпало / новых / продолжающих | ' + ' | '.join(f"{int(b.loc[a,'n_sku_base'])} / {int(b.loc[a,'n_sku_cur'])} / {int(b.loc[a,'n_drop'])} / {int(b.loc[a,'n_new'])} / {int(b.loc[a,'n_cont'])}" for a in ['oz_acc1', 'oz_acc2', 'sum']) + ' |')
    L.append('')
L.append('''Метод: по SKU (account, offer_id) ΔR = p_base×Δq + q_cur×Δp — точно, без остатка взаимодействия. Объём продолжающих SKU раскладывается логарифмически (LMDI): q = доля_дней_в_наличии × (поиск.показы/доля) × (шт/поиск.показ). Где нет ключа остатка — только трафик/конверсия; где нет поиска — доступность, остальное в «необъяснённое». Конверсия — производная величина (остаток после показов), не независимое измерение. Сумма компонент = ΔR (проверено assert).
''')
for tag in ['A', 'B']:
    g = pd.read_csv(f'{D}/15_{tag}_bridge_by_wave1_group.csv')
    L.append(f'**Мост {tag} по группам волны 1 (acc1, файл ozon_wave1_restore…19.csv; группа по offer_id через ozon_product):**\n')
    L.append(md(g, ['wave1_group', 'n_sku', 'R_base', 'R_cur', 'dR', 'drop_stock', 'drop_nodata', 'new', 'price', 'traffic_other', 'conv'], {c: I0 for c in ['R_base', 'R_cur', 'dR', 'drop_stock', 'drop_nodata', 'new', 'price', 'traffic_other', 'conv', 'n_sku']}))
    L.append('')
L.append('Группы A/B волны 1 в продажах — это только offer_id, у которых нет продаж в базе (новые карточки лета, попавшие в «хвост без истории»). Разбор E8 — у другого агента; здесь только метка.\n')
pb = pd.read_csv(f'{D}/17_price_raise_0902_effect.csv')
L.append('## 6. Эффект цены после подъёма 02–04.09\n')
L.append('Индекс цен 01.09→05.09: acc1 9 558 из 22 524 SKU подняты >5 % (медиана +6,7 %), acc2 335 SKU. raised5 — флаг по ozon_price_index (price). Эффекты по заказам, ₽/день; только продолжающие SKU для цены/объёма, «churn» — появившиеся/исчезнувшие.\n')
L.append(md(pb, ['cmp', 'account', 'raised5', 'n_sku_cont', 'R0_day', 'R1_day', 'dR_day', 'price_eff_day', 'vol_eff_cont_day', 'churn_day', 'unit_price_cont0', 'unit_price_cont1'], {c: I0 for c in ['R0_day', 'R1_day', 'dR_day', 'price_eff_day', 'vol_eff_cont_day', 'churn_day', 'unit_price_cont0', 'unit_price_cont1']}))

L.append('\n## 7. Топ-20 SKU по вкладу в падение (мост A; для моста B — `12_B_top20_sku.csv`)\n')
for tag, nb, nc in [('A', 28, 28)]:
    x = pd.read_csv(f'{D}/12_{tag}_top20_sku.csv', dtype={'offer_id': str})
    x['days_stock_b'] = x.a_b * nb; x['days_stock_c'] = x.a_c * nc
    x['nm'] = x.name_short.str.replace('Картриджи ', '').str.replace('Картридж ', '').str.replace(' для принтеров', '').str.slice(0, 40)
    x['dR_day'] = x.dR
    L.append(md(x, ['account', 'offer_id', 'nm', 'r_b', 'r_c', 'dR_day', 'dR_28d', 'dqty_28d', 'dprice_pct', 'days_stock_b', 'days_stock_c', 'search_v_base_wk', 'search_v_cur_wk', 'ad_views_base_d', 'ad_views_cur_d', 'ad_spend_base_d', 'ad_spend_cur_d', 'in_ads_now', 'diag'],
                {'r_b': I0, 'r_c': I0, 'dR_day': I0, 'dR_28d': I0, 'dqty_28d': I0, 'dprice_pct': lambda v: f(v, 1), 'days_stock_b': I0, 'days_stock_c': I0, 'search_v_base_wk': I0, 'search_v_cur_wk': I0,
                 'ad_views_base_d': I0, 'ad_views_cur_d': I0, 'ad_spend_base_d': lambda v: f(v, 1), 'ad_spend_cur_d': lambda v: f(v, 1)}))
L.append('\nr_b/r_c — ₽/день; поисковые показы — в неделю; рекламные показы/расход — в день, база рекламы = 27.07–08.08; дни в наличии — из 28 (для базы оценка по 22.06–05.07). Диагноз: крупнейшая отрицательная компонента SKU; «наличие?» — выпал, данных об остатке нет; «объём?» — нет покрытия факторов; «CTR» — конверсия при падении рекламного CTR >30 %.\n')
L.append('## 8. Товарные семьи\n')
L.append('Эвристика: бренд — первое вхождение из списка брендов в названии; код — первый токен после «Картридж(и)/Фотобарабан/Чернила/Тонер/Комплект… [DS]», иначе первый токен с цифрой до « для »; HP-коды вида CE410A/W2030A → серия с «x» вместо цветовой цифры (CE41xA), у прочих срезаются цветовые суффиксы (TK-5370Y→TK-5370, LC-472XLBK→LC-472XL); дети без кода (offer_id >4 знаков) наследуют семью родителя offer_id[:4]. Покрытие кодом 96 % выручки базы.\n')
for tag in ['A', 'B']:
    fg = pd.read_csv(f'{D}/13_{tag}_families.csv').head(10)
    L.append(f'**Мост {tag}: топ-10 семей по ΔR**\n')
    L.append(md(fg, ['family', 'accounts', 'n_sku', 'R_base', 'R_cur', 'dR', 'dR_28d', 'drop_stock', 'drop_nodata', 'new', 'price', 'traffic_other', 'conv', 'vol_unexplained'], {c: I0 for c in ['R_base', 'R_cur', 'dR', 'dR_28d', 'drop_stock', 'drop_nodata', 'new', 'price', 'traffic_other', 'conv', 'vol_unexplained', 'n_sku']}))
    L.append('')
ev = pd.read_csv(f'{D}/20_timeline_events.csv')
L.append('## 9. Тайминг: события vs ряд (7 дней после против 7 дней до, ₽/день и шт/день; совпадение ≠ причина)\n')
L.append(md(ev, ['date', 'event', 'rev_orders_7d_before', 'rev_orders_7d_after', 'rev_orders_chg', 'units_chg', 'ad_spend_chg', 'sku_ad_views_chg'], {'rev_orders_7d_before': I0, 'rev_orders_7d_after': I0, 'rev_orders_chg': PC, 'units_chg': PC, 'ad_spend_chg': PC, 'sku_ad_views_chg': PC}))
open(f'{D}/report_tables.md', 'w').write('\n'.join(L))
print('ok', len(L))
