# поток: mpstats-probe — качество keywords MPSTATS: релевантность и precision@10 против названия и наших запросов
import json, glob, re, pandas as pd
P = '/opt/mp-analytics/data/mpstats_probe/2026-09-14/'
titles = {}
for f, mp in (('actual_all_wb.csv', 'wb'), ('actual_all_oz.csv', 'ozon')):
    a = pd.read_csv(P + 'derived/' + f, dtype={'marketplace_id': str}, usecols=['marketplace_id', 'title'])
    titles.update({(mp, k): v for k, v in zip(a.marketplace_id, a.title)})
ours = pd.read_csv(P + 'derived/our_search_queries.csv', dtype={'marketplace_id': str})
GENERIC = r'(картридж|катридж|картриж|картрідж|тонер|расходн|краск|чернил|принтер|мфу|печат|фотобарабан|драм|лазерн|струйн|cartridge|toner)'
SERVICE = r'(промокод|чат поддержки|сентябр|скидк|распродаж|акци|доставк|купон|бесплатн)'
BRANDS = r'(hp|canon|кэнон|канон|epson|эпсон|kyocera|куосера|киосера|brother|бразер|xerox|ксерокс|samsung|самсунг|pantum|пантум|ricoh|konica|minolta|oki|panasonic|панасоник|lexmark|sharp|toshiba|catalyst|лоджик)'

LAT = str.maketrans('еаросхкмтвн', 'eapocxkmtbh')
FOREIGN = r'(vaporesso|нутелл|наушник|пульт|3d|вейп|pod|испарител|бритв|фильтр|кофе|телевизор|минфин|3 штуки)'
def tokens(title):
    t0 = str(title).lower(); t = t0
    codes = set(x.translate(LAT) for x in re.findall(r'[a-zа-я]*\d+[a-zа-я0-9\-]*', t0))
    codes = {c.strip('-') for c in codes if len(c) >= 3 and not re.fullmatch(r'\d{1,2}шт|\d+шт|\d{4,}стр', c)}
    brands = set(re.findall(BRANDS, t))
    return codes, brands

def classify(q, title):
    ql = str(q).lower(); qlat = ql.translate(LAT)
    if re.search(FOREIGN, ql): return 'мусорный'
    codes, brands = tokens(title)
    code_hit = any(c in qlat.replace(' ', '') or c in qlat for c in codes)
    brand_hit = bool(re.search(BRANDS, ql)); brand_ours = any(b in ql for b in brands)
    gen = bool(re.search(GENERIC, ql))
    if re.search(SERVICE, ql): return 'служебный/навигационный'
    if code_hit: return 'релевантный'
    if gen and brand_ours: return 'релевантный'
    if gen and not brand_hit and len(ql.split()) <= 4: return 'частично релевантный'
    if brand_ours and not gen: return 'частично релевантный'
    if gen and brand_hit and not brand_ours: return 'мусорный'
    return 'мусорный'

rows = []; summ = []
for f in sorted(glob.glob(P + 'raw/s2_*_kw_*.json')):
    mp = 'wb' if '_wb_' in f else 'ozon'; mid = re.search(r'_(\d+)\.json', f).group(1)
    d = json.load(open(f))
    words = (d['data']['words'] if isinstance(d.get('data'), dict) else d['data']) if isinstance(d, dict) else d
    words = sorted(words, key=lambda x: -(x.get('count') or 0))
    title = titles.get((mp, mid), '')
    our_q = set(ours[(ours.marketplace == mp) & (ours.marketplace_id == mid)]['query'].str.lower())
    cls = []
    for i, w in enumerate(words):
        q = w.get('query'); c = classify(q, title); cls.append(c)
        rows.append(dict(marketplace=mp, marketplace_id=mid, rank=i + 1, query=q, count=w.get('count'), wb_count=w.get('wb_count'),
                         avg_position=w.get('avg_position'), cls=c, in_our_queries=str(q).lower() in our_q, title=str(title)[:60]))
    top = cls[:10]; k = len(top)
    summ.append(dict(marketplace=mp, marketplace_id=mid, n_queries=len(words), top_k=k,
                     precision10_strict=round(top.count('релевантный') / k, 2) if k else None,
                     precision10_lenient=round((top.count('релевантный') + top.count('частично релевантный')) / k, 2) if k else None,
                     garbage_top=top.count('мусорный'), service_top=top.count('служебный/навигационный'),
                     our_queries=len(our_q), overlap_with_ours=len(our_q & {str(w.get('query')).lower() for w in words})))
pd.DataFrame(rows).to_csv(P + 'derived/keywords_classified.csv', index=False)
S = pd.DataFrame(summ); S.to_csv(P + 'derived/keywords_quality.csv', index=False)
for mp in ('wb', 'ozon'):
    x = S[S.marketplace == mp]; r = pd.DataFrame(rows); r = r[(r.marketplace == mp) & (r['rank'] <= 10)]
    print(mp, 'SKU', len(x), 'avg queries', round(x.n_queries.mean(), 1), 'P@10 strict', round(x.precision10_strict.mean(), 2),
          'lenient', round(x.precision10_lenient.mean(), 2), 'pooled top10 classes', r.cls.value_counts().to_dict(),
          'overlap with ours', int(x.overlap_with_ours.sum()), 'of our', int(x.our_queries.sum()))
