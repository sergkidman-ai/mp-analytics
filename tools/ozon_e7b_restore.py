# поток: mkt
"""ozon_e7b_restore.py — точечное восстановление трёх связок, ошибочно откаченных 18.08.

Это ИСПРАВЛЕНИЕ ПОСЛЕДСТВИЙ доказанного дефекта фильтра отката E4 (заказы считались
по связке кампания×SKU вместо SKU по аккаунту), а НЕ новый рекламный эксперимент.
Событие пишется в журнал отдельным действием e7b_restore и не смешивается с E7.

  dry-run (по умолчанию)  — читает ставку из API, сверяет с журналом, показывает план
  --apply                 — отправляет ставки и делает GET-сверку. ТОЛЬКО по прямому
                            подтверждению Сергея на эти три связки.
"""
import sys, json, argparse, datetime as dt

sys.path.insert(0, '/opt/mp-analytics')
import requests
from core import db
from collectors.ozon_ads import _token, PERF

ACC = 'oz_acc1'
ACTION = 'e7b_restore'
REPORTS = '/opt/mp-analytics/docs/reports'
# (кампания, sku, ставка после ошибочного отката, целевая ставка = до отката)
TARGETS = [('10659516', '863418800', 12.00, 19.33),
           ('10626733', '1658970308', 18.00, 24.00),
           ('12704286', '864225280', 8.00, 12.89)]


def _headers():
    return {'Authorization': f'Bearer {_token(ACC)}', 'Content-Type': 'application/json'}


def api_products(H, cid):
    """Ставки кампании из API чтения: sku -> ₽ (в ответе микрорубли)."""
    out, page = {}, 1
    while True:
        r = requests.get(f'{PERF}/api/client/campaign/{cid}/v2/products', headers=H,
                         params={'page': page, 'pageSize': 1000}, timeout=180)
        r.raise_for_status()
        items = r.json().get('products') or []
        out.update({str(x['sku']): float(x['bid']) / 1e6 for x in items if x.get('sku')})
        if len(items) < 1000:
            return out
        page += 1


def journal_rollback(cid, sku):
    """Строка ошибочного отката 18.08 по связке."""
    r = db.query("""SELECT decided_on, bid_before, bid_after, applied, reason
                    FROM mkt_ozon_bid_journal
                    WHERE account=%s AND campaign_id::text=%s AND sku::text=%s AND action='rollback'
                    ORDER BY decided_on DESC LIMIT 1""", (ACC, cid, sku))
    return r[0] if r else None


def sku_facts(sku, d0, d1):
    """Заказы, выручка и расход по SKU на уровне АККАУНТА (все кампании) за окно."""
    return db.query("""SELECT count(DISTINCT campaign_id) кампаний, sum(orders_qty) заказы,
                              sum(orders_money) выручка, sum(money_spent) расход,
                              sum(clicks) клики, sum(views) показы
                       FROM mkt_ozon_ads_sku_daily
                       WHERE account=%s AND sku::text=%s AND stat_date BETWEEN %s AND %s""",
                    (ACC, sku, d0, d1))[0]


def membership(sku):
    """В какие эксперименты входит SKU (по действиям в журнале)."""
    rows = db.query("""SELECT DISTINCT action FROM mkt_ozon_bid_journal
                       WHERE account=%s AND sku::text=%s""", (ACC, sku))
    m = {'core_push': 'E5 treatment', 'core_control': 'E5 control',
         'restore_converter': 'E7', 'wave1_restore': 'E8 A', 'wave1_control': 'E8 B'}
    return sorted({m[r['action']] for r in rows if r['action'] in m})


def run(apply_=False):
    H = _headers()
    day = dt.date.today().isoformat()
    caps = {cid: api_products(H, cid) for cid in {c for c, _, _, _ in TARGETS}}
    out = ['# Точечное восстановление трёх связок после дефекта фильтра отката E4', '',
           f'Прогон {day}. Режим: {"ПРИМЕНЕНИЕ" if apply_ else "СУХОЙ ПРОГОН (ничего не отправлено)"}.',
           'Событие журнала: `E7B_RESTORE_AFTER_E4_FILTER_BUG` (action `e7b_restore`), '
           'с E7 не смешивается.', '',
           '| кампания | SKU | ставка в API сейчас | после отката | до отката (цель) | '
           'откат в журнале | диапазон ставок кампании | эксперименты |', '|---|---|---:|---:|---:|---|---|---|']
    plan, blocked = [], []
    for cid, sku, after, target in TARGETS:
        cur = caps[cid].get(sku)
        j = journal_rollback(cid, sku)
        lo, hi = (min(caps[cid].values()), max(caps[cid].values())) if caps[cid] else (None, None)
        exps = membership(sku)
        jt = (f'{j["decided_on"]}: {float(j["bid_before"]):.2f}→{float(j["bid_after"]):.2f}'
              f'{"" if j["applied"] else " (не применён)"}') if j else 'нет строки'
        ok = (cur is not None and j is not None and abs(float(j['bid_before']) - target) < 0.01
              and abs(float(j['bid_after']) - after) < 0.01 and abs(cur - after) < 0.01)
        (plan if ok else blocked).append((cid, sku, cur, target, exps))
        out.append(f'| {cid} | {sku} | {cur if cur is None else f"{cur:.2f}"} | {after:.2f} | '
                   f'{target:.2f} | {jt} | {lo:.2f}–{hi:.2f} | {", ".join(exps) or "—"} |')
    out += ['', '**Допустимость ставки.** Целевые значения уже стояли в этих кампаниях '
            'с 08–13.08 по 18.08 включительно (снимки `ozon_bids`) и принимались площадкой — '
            'то есть лежат внутри допустимого диапазона. Отдельной ручки «границы ставки» '
            'Performance API не отдаёт; проверка — по факту прежнего приёма и по разбросу '
            'ставок внутри той же кампании.', '']
    out.append('| SKU | окно | кампаний | показы | клики | расход, ₽ | заказы | выручка, ₽ |')
    out.append('|---|---|---:|---:|---:|---:|---:|---:|')
    for _, sku, _, _ in TARGETS:
        for name, d0, d1 in (('до отката 12–18.08', '2026-08-12', '2026-08-18'),
                             ('после отката 19.08+', '2026-08-19', day)):
            f = sku_facts(sku, d0, d1)
            out.append(f'| {sku} | {name} | {f["кампаний"] or 0} | {f["показы"] or 0:,.0f} | '
                       f'{f["клики"] or 0:,.0f} | {f["расход"] or 0:,.0f} | {f["заказы"] or 0:,.0f} | '
                       f'{f["выручка"] or 0:,.0f} |')
    out += ['', '## План восстановления', '']
    for cid, sku, cur, target, exps in plan:
        out.append(f'* кампания {cid}, SKU {sku}: {cur:.2f} → {target:.2f} ₽ '
                   f'(PUT /api/client/campaign/{cid}/products, ставка в микрорублях)')
    for cid, sku, cur, target, exps in blocked:
        out.append(f'* ЗАБЛОКИРОВАНО: кампания {cid}, SKU {sku} — факты не сошлись '
                   f'(в API {cur}, ожидали {TARGETS and ""}{target:.2f} после отката); не трогаем')
    out += ['', '## Влияние на действующие эксперименты', '',
            '* E5 (core_push): затронуто SKU из когорты — см. столбец «эксперименты». '
            'Если пересечение есть, оно фиксируется как загрязнение: ставка меняется не '
            'экспериментом, а исправлением дефекта;',
            '* E7 (возврат конвертеров): те же 18.08-жертвы, но другая партия. Отдельное '
            'действие `e7b_restore` не даёт смешать когорты при оценке 02.09;',
            '* E8 (A/B волны 1): когорта волны 1 снималась 09.08 — пересечения быть не должно, '
            'проверяется столбцом «эксперименты»;',
            '* G6: три связки добавляют расход в ядро, а не в хвост, — доля хвоста не растёт.', '']
    path = f'{REPORTS}/ozon_e7b_restore_{"apply" if apply_ else "dryrun"}_{day}.md'
    if not apply_:
        out += ['**Ничего не отправлено.** Для применения нужен явный ответ Сергея именно '
                'по этим трём связкам, затем `--apply`.']
        open(path, 'w', encoding='utf-8').write('\n'.join(out) + '\n')
        print('\n'.join(out))
        print(f'\nотчёт: {path}')
        return
    if blocked:
        raise SystemExit('часть связок не сошлась с журналом — применение отменено целиком')
    from tools.ozon_bid_ramp import _apply_bids
    for cid, sku, cur, target, exps in plan:
        code, body = _apply_bids(ACC, cid, [{'sku': sku, 'bid': str(int(round(target * 1e6)))}])
        ok = code == 200
        db.execute("""INSERT INTO mkt_ozon_bid_journal
              (decided_on, week_start, account, campaign_id, sku, action, bid_before, bid_after,
               reason, applied, applied_at, api_response)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s)
            ON CONFLICT (week_start, account, campaign_id, sku) DO UPDATE SET
              action=EXCLUDED.action, bid_before=EXCLUDED.bid_before, bid_after=EXCLUDED.bid_after,
              reason=EXCLUDED.reason, applied=EXCLUDED.applied, applied_at=now(),
              api_response=EXCLUDED.api_response""",
            (day, day, ACC, cid, sku, ACTION, cur, target,
             'E7B_RESTORE_AFTER_E4_FILTER_BUG: возврат ставки, снятой ошибочным фильтром отката 18.08',
             ok, f'{code} {str(body)[:200]}'))
        out.append(f'* {cid}/{sku}: {cur:.2f} → {target:.2f} ₽ — ответ {code}')
    caps2 = {cid: api_products(H, cid) for cid in {c for c, _, _, _ in TARGETS}}
    out += ['', '## GET-сверка после применения', '']
    for cid, sku, cur, target, exps in plan:
        got = caps2[cid].get(sku)
        out.append(f'* {cid}/{sku}: в API {got if got is None else f"{got:.2f}"} ₽, '
                   f'ожидали {target:.2f} — {"совпало" if got and abs(got - target) < 0.01 else "РАСХОЖДЕНИЕ"}')
    open(path, 'w', encoding='utf-8').write('\n'.join(out) + '\n')
    print('\n'.join(out[-12:]))
    print(f'\nотчёт: {path}')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='точечный возврат трёх ставок после дефекта E4')
    ap.add_argument('--apply', action='store_true', help='отправить ставки (только по команде Сергея)')
    a = ap.parse_args()
    run(a.apply)
