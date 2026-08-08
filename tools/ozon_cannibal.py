# поток: mkt
# ozon_cannibal.py — каннибализм: разные наши SKU ранжируются по одному запросу и мешают друг другу.
#
# Данные: ozon_search_query (живая выдача Ozon, июнь–август 2026) + ozon_product (мост sku->offer_id).
# Семья товара = первые 4 цифры offer_id (материнская карточка картриджа, правило gab-4).
# Бандл = offer_id содержит X<кратность>.
#
# Логика: по каждому запросу берём лучшую позицию каждого нашего SKU. Спор считаем реальным,
# только если оба SKU стоят в зоне, где вообще есть покупатели (позиция <= 30 — ниже её
# на 454 тыс. спроса пришёлся 1 заказ). Внутри семьи спор = мы платим дважды за один товар.
import sys, csv, collections
sys.path.insert(0, '/opt/mp-analytics')
from core import db

ACC, POS_MAX = 'oz_acc1', 30
RPT = '/opt/mp-analytics/.claude/worktrees/mkt-ozon/docs/reports/'
fam = lambda o: (o or '')[:4]
is_bundle = lambda o: 'X' in (o or '').upper()[4:]

rows = db.query("""
    SELECT q.query, q.sku::text sku, p.offer_id, min(q.position) pos,
           max(q.unique_search_users) demand, sum(q.order_count) orders, sum(q.gmv) gmv
      FROM ozon_search_query q JOIN ozon_product p ON p.account=q.account AND p.sku=q.sku
     WHERE q.account=%s AND q.position>0 AND q.position<=%s
     GROUP BY 1,2,3""", (ACC, POS_MAX))

byq = collections.defaultdict(list)
for r in rows:
    byq[r['query']].append(r)

def _live_in_ads():
    """Живой состав кампаний с площадки, а не снимок ozon_bids: снимок сделан ДО удаления
    первой волны 08.08.2026 и показал бы удалённые карточки как рекламируемые."""
    import requests
    from collectors.ozon_ads import _token, PERF
    H = {'Authorization': f'Bearer {_token(ACC)}'}
    ids = [str(c['id']) for c in requests.get(f'{PERF}/api/client/campaign', headers=H,
           timeout=120).json().get('list', []) if c.get('state') == 'CAMPAIGN_STATE_RUNNING']
    skus = set()
    for cid in ids:
        page = 1
        while True:
            r = requests.get(f'{PERF}/api/client/campaign/{cid}/v2/products', headers=H,
                             params={'page': page, 'pageSize': 1000}, timeout=180)
            if r.status_code != 200:
                break
            items = r.json().get('products') or []
            skus |= {str(x.get('sku')) for x in items}
            if len(items) < 1000:
                break
            page += 1
    return skus

inads = _live_in_ads()

conf_fam, conf_diff, out = [], 0, []
for q, lst in byq.items():
    if len(lst) < 2:
        continue
    groups = collections.defaultdict(list)
    for r in lst:
        groups[fam(r['offer_id'])].append(r)
    for f, g in groups.items():
        if len(g) < 2:
            continue                                   # разные семьи по одному запросу — это ассортимент, не спор
        g.sort(key=lambda r: (r['pos'], -float(r['orders'] or 0)))
        lead, rest = g[0], g[1:]
        dem = float(lst[0]['demand'] or 0)
        conf_fam.append((q, f, len(g), dem))
        for r in rest:
            out.append({'query': q, 'demand': int(dem), 'family': f,
                        'keep_sku': lead['sku'], 'keep_offer': lead['offer_id'], 'keep_pos': lead['pos'],
                        'keep_orders': float(lead['orders'] or 0),
                        'drop_sku': r['sku'], 'drop_offer': r['offer_id'], 'drop_pos': r['pos'],
                        'drop_orders': float(r['orders'] or 0),
                        'drop_in_ads': int(r['sku'] in inads),
                        'kind': 'бандл_vs_одиночка' if is_bundle(r['offer_id']) != is_bundle(lead['offer_id'])
                                else 'дубли_одного_товара'})
    if len(groups) > 1:
        conf_diff += 1

out.sort(key=lambda r: -r['demand'])
with open(RPT + 'ozon_acc1_cannibal_v2.csv', 'w', newline='') as fh:
    w = csv.DictWriter(fh, fieldnames=list(out[0]))
    w.writeheader(); w.writerows(out)

paid = [r for r in out if r['drop_in_ads']]
dead = [r for r in paid if r['drop_orders'] == 0 and r['keep_orders'] > 0]
print(f'запросов с нашими SKU (позиции 1-{POS_MAX}): {len(byq)}')
print(f'запросов, где 2+ НАШИХ карточки ОДНОГО товара: {len(set(x[0] for x in conf_fam))}')
print(f'  спорных пар (лидер + лишние): {len(out)}')
print(f'  из них лишняя карточка стоит в рекламе: {len(paid)}')
dem_tot = sum(d for _, d in {(x[0], x[3]) for x in conf_fam})
print(f'  спрос под спорными запросами: {dem_tot:,.0f} чел.'.replace(',', ' '))
for k, n in collections.Counter(r['kind'] for r in out).most_common():
    print(f'  тип «{k}»: {n}')
print(f'явно лишние (в рекламе, 0 заказов, а лидер продаёт): {len(dead)} карточко-запросов, '
      f'{len(set(r["drop_sku"] for r in dead))} уникальных SKU')
print('файл:', RPT + 'ozon_acc1_cannibal_v2.csv')

# --- Самый дорогой случай: по одному запросу в рекламе стоят ОБЕ карточки одного товара ---
both = [r for r in out if r['drop_in_ads'] and r['keep_sku'] in inads]
with open(RPT + 'ozon_acc1_cannibal_paid.csv', 'w', newline='') as fh:
    w = csv.DictWriter(fh, fieldnames=list(out[0])); w.writeheader(); w.writerows(both)
print('--- платим дважды за один товар ---')
print(f'  карточко-запросов: {len(both)}, запросов: {len(set(r["query"] for r in both))}, '
      f'спрос: {sum(d for _, d in {(r["query"], r["demand"]) for r in both}):,.0f} чел.'.replace(',', ' '))
print(f'  кандидатов снять с рекламы (лишние SKU): {len(set(r["drop_sku"] for r in both))}')
for k, n in collections.Counter(r['kind'] for r in both).most_common():
    print(f'  тип «{k}»: {n}')
print('файл:', RPT + 'ozon_acc1_cannibal_paid.csv')
