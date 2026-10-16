# поток: mpstats-probe — сопоставление Q2612A: характеристики (CDN WB) + рыночные поля (MPSTATS) -> equivalence_class
import json, os, re, pandas as pd
P = '/opt/mp-analytics/data/mpstats_probe/2026-09-14/'
IDS = [181081324, 48691548, 158868469, 48824057, 209690957, 1244128512, 172632042, 877283027, 175158012]
OURS = 181081324
kw = {r['id']: r for r in json.load(open(P + 'raw/s2_wb_items_keyword_POST.json'))['data']}
sim = {r['id']: r for r in json.load(open(P + 'raw/stage1_03_wb_similar_items.json'))['data']}

def opt(opts, *names):
    for o in opts:
        if any(n.lower() in str(o.get('name', '')).lower() for n in names): return str(o.get('value'))
    return None

rows = []
for i in IDS:
    c = {}
    f = P + f'raw/wb_cdn/{i}.json'
    if os.path.exists(f) and os.path.getsize(f) > 0: c = json.load(open(f))
    opts = c.get('options', [])
    text = ' '.join([str(c.get('imt_name', '')), str(c.get('description', '')), ' '.join(str(o.get('value')) for o in opts)]).lower()
    full = json.load(open(P + f'raw/s2_wb_full_{i}.json')) if os.path.exists(P + f'raw/s2_wb_full_{i}.json') else (
        json.load(open(P + 'raw/s2_wb_48691548_full_fbs1.json')) if i == 48691548 else None)
    name = (full or {}).get('name') or c.get('imt_name') or kw.get(i, {}).get('name') or sim.get(i, {}).get('name')
    nm = str(name).lower()
    pack_s = opt(opts, 'Количество предметов')
    pack = int(re.search(r'\d+', pack_s).group()) if pack_s and re.search(r'\d+', pack_s) else (
        int(m.group(1)) if (m := re.search(r'\((\d+)\s*шт', nm)) else (1 if c or full else None))
    res_s = opt(opts, 'Ресурс', 'страниц')
    res = int(re.search(r'\d[\d\s]*', res_s).group().replace(' ', '')) if res_s and re.search(r'\d', res_s) else (
        int(m.group(1)) if (m := re.search(r'(\d{3,5})\s*стр', nm + ' ' + text)) else None)
    code = 'Q2612X' if 'q2612x' in nm or '12x' in nm.replace(' ', '') else ('Q2612A' if re.search(r'q?2612a|12a|fx-?10|703', nm + ' ' + text) else None)
    kind_s = (opt(opts, 'Вид картриджа', 'Тип картриджа') or '').lower()
    brand = (full or {}).get('brand') or kw.get(i, {}).get('brand')
    original = 'оригинал' if ('оригинал' in kind_s or str(brand).upper() == 'HP') and 'совмест' not in kind_s else 'совместимый'
    chip = 'да' if 'чип' in nm or 'с чипом' in text else ('нет/не указан')
    color = opt(opts, 'Цвет')
    compat = opt(opts, 'Совместимость', 'Модель принтера')
    ps = (full or {}).get('period_stats') or {}
    srow = sim.get(i, {})
    price = ((full or {}).get('price') or {}).get('final_price') or srow.get('final_price')
    stock = (full or {}).get('stock') or {}
    rows.append(dict(nm_id=i, name=str(name)[:70], brand=brand, seller=((full or {}).get('seller') or {}).get('name') or (kw.get(i, {}).get('seller') or {}).get('name'),
                     pack_qty=pack, resource_pages=res, model_code=code, chip=chip, color=color, compat=(compat or '')[:80], type=original,
                     rating=(full or {}).get('rating') or srow.get('rating'), reviews=(full or {}).get('comments') or srow.get('comments'),
                     price=price, price_per_cartridge=round(price / pack) if price and pack else None,
                     stock_fbo=stock.get('fbo'), stock_fbs=stock.get('fbs'),
                     mp_sales_30d=ps.get('sales') if ps else srow.get('sales'), mp_revenue_30d=ps.get('revenue') if ps else srow.get('revenue'),
                     photos=((full or {}).get('photo') or {}).get('count'), cdn_card=bool(c), mpstats_source='full fbs=1' if full else ('similar list (stage1)' if srow else 'только поиск')))
df = pd.DataFrame(rows)
o = df[df.nm_id == OURS].iloc[0]

def eq(r):
    if r.nm_id == OURS: return 'наш товар'
    if r.model_code is None: return 'нерелевантный товар'
    if pd.isna(r.pack_qty): return 'не подтверждено (нет карточки)'
    if r.pack_qty != 1: return 'другая комплектация'
    if r.model_code != o.model_code: return 'функциональный аналог (другой ресурс/код)'
    if r.type != o.type: return 'функциональный аналог (оригинал vs совместимый)'
    if o.resource_pages and r.resource_pages and abs(r.resource_pages - o.resource_pages) / o.resource_pages > 0.15: return 'функциональный аналог (ресурс)'
    if r.resource_pages is None or o.resource_pages is None: return 'точный аналог (ресурс не подтверждён)'
    return 'точный аналог' + (' (в названии «с чипом» — у Q2612A чипа нет, маркетинг)' if r.chip == 'да' else '')
df['equivalence_class'] = df.apply(eq, axis=1)
df.to_csv(P + 'derived/q2612a_equivalence.csv', index=False)
for r in df.itertuples():
    print(r.nm_id, r.equivalence_class, '|', r.model_code, 'pack', r.pack_qty, 'res', r.resource_pages, 'chip', r.chip, r.type, '|', str(r.seller)[:18], '| p', r.price, '/шт', r.price_per_cartridge, '| r', r.rating, 'rev', r.reviews, '| fbo/fbs', r.stock_fbo, r.stock_fbs, '| mp30d', r.mp_sales_30d, '|', r.mpstats_source)
