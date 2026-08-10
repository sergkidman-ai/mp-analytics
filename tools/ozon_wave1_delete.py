# поток: mkt
# ozon_wave1_delete.py — удаление первой волны неэффективных позиций из кампаний Ozon acc1.
#
# Эндпоинт проверен боем 08.08.2026 на sku 4527412902 (кампания 12704286):
#   POST /api/client/campaign/{id}/products/delete  {"sku": ["<sku>", ...]}  -> 200 {}
# Чтение для сверки — GET /api/client/campaign/{id}/v2/products, ПАГИНИРОВАННОЕ
# (по умолчанию 30 штук на страницу, нужен pageSize/page).
#
# Запуск без --apply — сухой прогон: показывает, что бы ушло, на площадку ничего не отправляется.
import sys, csv, argparse
sys.path.insert(0, '/opt/mp-analytics')
import requests
from core import db
from collectors.ozon_ads import _token, PERF

ACC = 'oz_acc1'
CSV = '/opt/mp-analytics/docs/reports/ozon_acc1_wave1.csv'
BATCH = 100


def _plan(acc):
    """CSV даёт (кампания по названию, sku) — переводим в campaign_id по последнему снимку ставок."""
    idx = {(r['campaign_title'] or '', str(r['sku'])): str(r['campaign_id']) for r in db.query(
        """SELECT campaign_id, campaign_title, sku FROM ozon_bids WHERE account=%s
           AND captured_at=(SELECT max(captured_at) FROM ozon_bids WHERE account=%s)""", (acc, acc))}
    plan = {}
    for r in csv.DictReader(open(CSV)):
        cid = idx.get((r['campaign'], r['sku']))
        if cid:
            plan.setdefault(cid, set()).add(r['sku'])
    return plan


def _products(H, cid):
    """Полный список sku кампании со всеми страницами."""
    out, page = set(), 1
    while True:
        r = requests.get(f'{PERF}/api/client/campaign/{cid}/v2/products', headers=H,
                         params={'page': page, 'pageSize': 1000}, timeout=180)
        r.raise_for_status()
        items = r.json().get('products') or []
        out |= {str(x.get('sku')) for x in items}
        if len(items) < 1000:
            return out
        page += 1


def run(acc, apply_):
    H = {'Authorization': f'Bearer {_token(acc)}', 'Content-Type': 'application/json'}
    plan, log = _plan(acc), open('/opt/mp-analytics/logs/ozon_wave1_delete.log', 'a')
    total = sum(len(v) for v in plan.values())
    print(f'к удалению: {total} связок товар×кампания в {len(plan)} кампаниях'
          + ('' if apply_ else '  [DRY-RUN]'))
    for cid, skus in sorted(plan.items()):
        skus, bad = sorted(skus), 0
        if apply_:
            for i in range(0, len(skus), BATCH):
                chunk = skus[i:i + BATCH]
                r = requests.post(f'{PERF}/api/client/campaign/{cid}/products/delete',
                                  headers=H, json={'sku': chunk}, timeout=180)
                if r.status_code not in (200, 201):
                    bad += len(chunk)
                    log.write(f'{cid} batch{i} -> {r.status_code} {r.text[:200]}\n')
            left = _products(H, cid) & set(skus)          # сверка чтением: сколько осталось
            print(f'  кампания {cid}: отправлено {len(skus)}, ошибок {bad}, осталось в кампании {len(left)}')
        else:
            print(f'  кампания {cid}: {len(skus)}')
    log.close()


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--account', default=ACC)
    ap.add_argument('--apply', action='store_true', help='удалить на площадке (только по команде Сергея)')
    a = ap.parse_args()
    run(a.account, a.apply)
