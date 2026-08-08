# поток: mkt
# ozon_new_campaigns.py — заведение двух новых кампаний acc1 по решению Сергея 08.08.2026:
#   1) позиции с подтверждённым спросом, но вне рекламы (docs/reports/ozon_acc1_add.csv);
#   2) бандлы x2/x6 на моделях, чьи одиночки реально продаются (ozon_acc1_bundles_start.csv).
#
# Схема создания подобрана по ответам API: POST /api/client/campaign/cpc/v2/product,
# дата старта — ТОЛЬКО сегодняшняя, бюджет в микрорублях, placement — строкой.
# Товары добавляются POST /api/client/campaign/{id}/products {"bids":[{"sku","bid"}]}
# (тот же путь под PUT меняет ставку уже заведённым товарам).
#
# Без --apply ничего на площадку не уходит: печатается состав и бюджеты.
import sys, csv, json, argparse, datetime as dt
sys.path.insert(0, '/opt/mp-analytics')
import requests
from core import db
from collectors.ozon_ads import _token, PERF

ACC = 'oz_acc1'
RPT = '/opt/mp-analytics/docs/reports/'
RPT_W = '/opt/mp-analytics/.claude/worktrees/mkt-ozon/docs/reports/'
START_BID = 15.0           # пол ставки в новой кампании (ниже API отвечает «ставка вне диапазона»)
BUNDLE_TARGET = 30.0       # цель разгона для бандлов: чек в 2-10 раз выше одиночки, но факта ещё нет
BUNDLE_BASES = 100         # сколько базовых моделей берём в первый заход
micro = lambda x: str(int(round(float(x) * 1_000_000)))


def _sku_by_offer(acc):
    return {r['offer_id']: str(r['sku']) for r in
            db.query('SELECT offer_id, sku FROM ozon_product WHERE account=%s', (acc,))}


def _create(H, title, daily_rub):
    """Тело собрано по образцу живой кампании 10604674 (GET /api/client/campaign).
    Обязательны все пять полей ниже: без productAutopilotStrategy=TARGET_BIDS API отвечает
    «стратегия управления ставками недоступна для выбранного типа оплаты», без
    budgetType=WEEKLY — «дата старта может быть только сегодняшней». placement — скаляр,
    хотя на чтении приходит массивом. Бюджет недельный, дневной API игнорирует."""
    body = {'title': title, 'fromDate': str(dt.date.today()), 'toDate': '2026-12-31',
            'placement': 'PLACEMENT_TOP_PROMOTION',
            'productCampaignMode': 'PRODUCT_CAMPAIGN_MODE_AUTO',
            'productAutopilotStrategy': 'TARGET_BIDS', 'PaymentType': 'CPC',
            'expenseStrategy': 'DAILY_BUDGET', 'budgetType': 'PRODUCT_CAMPAIGN_BUDGET_TYPE_WEEKLY',
            'weeklyBudget': micro(daily_rub * 7), 'startWeekDay': 'SATURDAY', 'endWeekDay': 'FRIDAY'}
    r = requests.post(f'{PERF}/api/client/campaign/cpc/v2/product', headers=H, json=body, timeout=120)
    return r.status_code, r.text[:300]


def _add(H, cid, items):
    body = {'bids': [{'sku': str(s), 'bid': micro(b)} for s, b in items]}
    r = requests.post(f'{PERF}/api/client/campaign/{cid}/products', headers=H, json=body, timeout=180)
    return r.status_code, r.text[:200]


def load_add():
    """111 позиций вне рекламы. Заводим по полу 15 ₽, расчётная ставка от потолка — ЦЕЛЬ разгона."""
    return [(r['offer_id'], START_BID, max(float(r['bid_start']), START_BID))
            for r in csv.DictReader(open(RPT + 'ozon_acc1_add.csv'))]


def load_bundles():
    """Бандлы x2/x6 по топ-моделям: x10 придержим до первых цифр по x2/x6."""
    rows = list(csv.DictReader(open(RPT_W + 'ozon_acc1_bundles_start.csv')))
    order, seen = [], set()
    for r in rows:                                   # файл уже отсортирован по выручке одиночки
        if r['base'] not in seen:
            seen.add(r['base']); order.append(r['base'])
    top = set(order[:BUNDLE_BASES])
    return [(r['bundle'], START_BID, BUNDLE_TARGET) for r in rows
            if r['base'] in top and r['mult'] in ('2', '6')]


def run(apply_):
    H = {'Authorization': f'Bearer {_token(ACC)}', 'Content-Type': 'application/json'}
    bridge = _sku_by_offer(ACC)
    # existing: кампания уже заведена на площадке — только наполняем (повторный прогон не плодит РК)
    jobs = [('Вне РК — спрос без рекламы (08.08)', 1000, load_add(), '35269704'),
            ('Бандлы x2/x6 — старт (08.08)', 500, load_bundles(), '35269713')]
    for title, budget, rows, existing in jobs:
        rows = [r for r in rows if r[0] in bridge]
        items = [(bridge[o], b) for o, b, _ in rows]
        print(f'\n{title}: {len(items)} товаров (из {len(rows)} в списке), бюджет {budget} ₽/день'
              + ('' if apply_ else '   [DRY-RUN]'))
        if not apply_:
            continue
        if existing:
            cid = existing
            print(f'  кампания уже создана: {cid}')
        else:
            code, resp = _create(H, title, budget)
            print(f'  создание: {code} {resp[:160]}')
            if code not in (200, 201):
                continue
            cid = str((json.loads(resp) or {}).get('campaignId') or '')
        if not cid:
            print('  не вернулся id кампании — товары не добавляю'); continue
        ok = True
        for i in range(0, len(items), 500):
            c, t = _add(H, cid, items[i:i + 500])
            print(f'  товары {i + 1}-{i + len(items[i:i + 500])}: {c} {t[:120]}')
            ok = ok and c in (200, 201)
        if not ok:
            continue
        for off, start, tgt in rows:                       # ставим на тот же разгон +10 %/день
            db.execute("""INSERT INTO mkt_ozon_bid_ramp
                  (account,campaign_id,sku,offer_id,bid_start,bid_target,bid_current,step_pct,grp,
                   status,priority,note)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,10,%s,'running',1,%s)
                ON CONFLICT (account,campaign_id,sku) DO UPDATE SET bid_target=EXCLUDED.bid_target,
                  bid_current=EXCLUDED.bid_current, status='running', updated_at=now()""",
                (ACC, cid, bridge[off], off, start, tgt, start, title, 'новая кампания 08.08'))
        print(f'  кампания {cid} наполнена, {len(rows)} позиций поставлены на разгон')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='создать кампании на площадке')
    run(ap.parse_args().apply)
