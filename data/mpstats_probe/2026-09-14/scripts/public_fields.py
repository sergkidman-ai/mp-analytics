# поток: mpstats-probe — надёжность публичных полей MPSTATS против наших данных (read-only) и CDN WB
import sys, json, os, glob, pandas as pd
sys.path.insert(0, '/opt/mp-analytics')
from core.db import get_conn
P = '/opt/mp-analytics/data/mpstats_probe/2026-09-14/'
rows = []
with get_conn() as c:
    wp = pd.read_sql("""select distinct on (nm_id) nm_id::text marketplace_id, discounted_price, market_price, market_captured_at
                        from wb_price where market_price is not null order by nm_id, coalesce(market_captured_at, captured_at) desc""", c)
    fr = pd.read_sql("""select distinct on (nm_id) nm_id::text marketplace_id, feedback_rating from wb_funnel where period='2026-09-01' order by nm_id""", c)
    ozr = pd.read_sql("select sku::text marketplace_id, avg_rating, reviews_count, updated_at from ozon_rating", c)
    ozp = pd.read_sql("""select distinct on (sku) sku::text marketplace_id, price, marketing_price from ozon_price_index order by sku, collected_on desc""", c)
wp = wp.set_index('marketplace_id'); fr = fr.set_index('marketplace_id'); ozr = ozr.drop_duplicates('marketplace_id').set_index('marketplace_id'); ozp = ozp.set_index('marketplace_id')
# WB свои: последняя дневная строка by_period (13.09)
for f in sorted(glob.glob(P + 'raw/s2_wb_bp_*.json')):
    mid = f.split('_')[-1][:-5]
    d = sorted(json.load(open(f)), key=lambda x: x['data'])[-1]
    cdn = None
    r = dict(marketplace='wb', group='свой', marketplace_id=mid, mp_date=d['data'], mp_final_price=d.get('final_price'),
             our_market_price=wp.market_price.get(mid), our_discounted_price=wp.discounted_price.get(mid),
             mp_rating=d.get('rating'), our_rating=fr.feedback_rating.get(mid), mp_comments=d.get('comments'))
    rows.append(r)
# WB конкуренты Q2612A: full против CDN (название, фото)
for f in sorted(glob.glob(P + 'raw/s2_wb_full_*.json')) + [P + 'raw/s2_wb_48691548_full_fbs1.json']:
    full = json.load(open(f)); mid = str(full['id'])
    cf = P + f'raw/wb_cdn/{mid}.json'
    cdn = json.load(open(cf)) if os.path.exists(cf) and os.path.getsize(cf) > 0 else {}
    ph_cdn = (cdn.get('media') or {}).get('photo_count')
    rows.append(dict(marketplace='wb', group='свой' if mid == '181081324' else 'конкурент', marketplace_id=mid, mp_final_price=(full.get('price') or {}).get('final_price'),
                     our_market_price=wp.market_price.get(mid), mp_rating=full.get('rating'), our_rating=fr.feedback_rating.get(mid), mp_comments=full.get('comments'),
                     mp_photos=(full.get('photo') or {}).get('count'), cdn_photos=ph_cdn,
                     name_match=(str(full.get('name')).strip().lower() == str(cdn.get('imt_name')).strip().lower()) if cdn else None,
                     mp_seller=(full.get('seller') or {}).get('name'), mp_updated=full.get('updated')))
# Ozon свои: full против ozon_rating / ozon_price_index
for f in sorted(glob.glob(P + 'raw/s2_oz_full_*.json')):
    full = json.load(open(f)); mid = str(full['id'])
    rows.append(dict(marketplace='ozon', group='свой', marketplace_id=mid, mp_final_price=(full.get('price') or {}).get('final_price'),
                     mp_card_price=(full.get('price') or {}).get('ozon_card_price'), our_price=ozp.price.get(mid), our_marketing_price=ozp.marketing_price.get(mid),
                     mp_rating=full.get('rating'), our_rating=ozr.avg_rating.get(mid), mp_comments=full.get('comments'), our_reviews=ozr.reviews_count.get(mid),
                     mp_photos=(full.get('photo') or {}).get('count'), mp_seller=(full.get('seller') or {}).get('name')))
df = pd.DataFrame(rows)
df.to_csv(P + 'derived/public_fields_check.csv', index=False)

def share(a, b, tol):
    x = df[[a, b]].dropna(); x = x[(x[a].astype(float) > 0) & (x[b].astype(float) > 0)]
    if not len(x): return 'нет пар'
    ok = ((x[a].astype(float) - x[b].astype(float)).abs() / x[b].astype(float) <= tol).mean()
    return f'{round(ok*100)}% в пределах {int(tol*100)}% (n={len(x)})'
for mp in ('wb', 'ozon'):
    sub = df[df.marketplace == mp]
    globals()['df_all'] = df; df_bak = df
    df = sub
    print(mp, 'цена MPSTATS vs наша рыночная/после SPP:', share('mp_final_price', 'our_market_price' if mp == 'wb' else 'our_marketing_price', 0.03),
          '| vs наша до SPP:', share('mp_final_price', 'our_discounted_price' if mp == 'wb' else 'our_price', 0.03),
          '| рейтинг:', share('mp_rating', 'our_rating', 0.05), '| отзывы:', share('mp_comments', 'our_reviews', 0.05) if mp == 'ozon' else '',
          '| фото vs CDN:', share('mp_photos', 'cdn_photos', 0.0) if mp == 'wb' else '',
          '| название=CDN:', sub.name_match.dropna().mean() if mp == 'wb' and 'name_match' in sub else '')
    df = df_bak
