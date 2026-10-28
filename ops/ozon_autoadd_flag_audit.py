"""Read-only Ozon auto-add flag audit for both seller accounts (stream: mkt).

The relevant field is price.auto_add_to_ozon_actions_list_enabled. It is
different from price.auto_action_enabled and from the minimum-price timer.
This audit never sends price updates to Ozon.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path

from collectors.ozon_price_index import Api, PRICES_PATH
from ops import ozon_stock_action as oz


PAGE_LIMIT = 1000
MAX_PAGES = 500
JOURNAL = Path('/opt/mp-analytics/logs/ozon_autoadd_flag_runs.jsonl')


def scan_account(account, api=None):
    """Return a complete count or raise; never call a mutating endpoint."""
    api = api or Api(account)
    cursor = ''
    seen_cursors = set()
    seen_products = set()
    counts = Counter()
    examples = []
    pages = 0
    while pages < MAX_PAGES:
        code, data = api.post(PRICES_PATH, {
            'cursor': cursor, 'limit': PAGE_LIMIT,
            'filter': {'visibility': 'ALL'},
        })
        if code != 200:
            raise RuntimeError(f'{account}: price list HTTP {code} on page {pages + 1}')
        batch = data.get('items') or []
        pages += 1
        for item in batch:
            product_id = item.get('product_id')
            if product_id is None or product_id in seen_products:
                raise RuntimeError(f'{account}: missing or duplicate product_id on page {pages}')
            seen_products.add(product_id)
            value = (item.get('price') or {}).get('auto_add_to_ozon_actions_list_enabled')
            if value is True:
                counts['enabled'] += 1
                if len(examples) < 10:
                    examples.append(item.get('offer_id') or str(product_id))
            elif value is False:
                counts['disabled'] += 1
            else:
                counts['unknown'] += 1
        next_cursor = data.get('cursor') or ''
        if not next_cursor:
            break
        if not batch or next_cursor == cursor or next_cursor in seen_cursors:
            raise RuntimeError(f'{account}: incomplete pagination on page {pages}')
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    else:
        raise RuntimeError(f'{account}: pagination exceeded {MAX_PAGES} pages')
    reported_total = data.get('total')
    if reported_total is not None and int(reported_total) != len(seen_products):
        raise RuntimeError(f'{account}: received {len(seen_products)} of total {reported_total}')
    return {
        'account': account, 'total': len(seen_products), 'enabled': counts['enabled'],
        'disabled': counts['disabled'], 'unknown': counts['unknown'],
        'examples': examples, 'pages': pages, 'api_calls': api.calls,
    }


def message(rows, errors):
    lines = ['Автодобавление в акции Ozon · проверка флага']
    for row in rows:
        lines.append(f"{row['account']}: включено {row['enabled']} из {row['total']}; "
                     f"выключено {row['disabled']}; без значения {row['unknown']}")
        if row['enabled']:
            lines.append('Примеры: ' + ', '.join(row['examples']))
    for error in errors:
        lines.append('⚠️ ' + error)
    if not errors and rows and all(row['enabled'] == 0 and row['unknown'] == 0 for row in rows):
        lines.append('✅ Флаг выключен у всех прочитанных карточек.')
    elif not errors:
        lines.append('⚠️ Есть включённые или неизвестные флаги; требуется проверка источника цен.')
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only audit of Ozon action auto-add flags')
    parser.add_argument('--account', choices=oz.ACCOUNTS, help='default: both accounts')
    parser.add_argument('--notify', action='store_true', help='send summary to promo guard Telegram chat')
    args = parser.parse_args(argv)
    rows, errors = [], []
    for account in ([args.account] if args.account else oz.ACCOUNTS):
        try:
            rows.append(scan_account(account))
        except Exception as exc:
            errors.append(f'{account}: {type(exc).__name__}: {exc}')
    record = {'at': datetime.now(timezone.utc).isoformat(), 'rows': rows, 'errors': errors}
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + '\n')
    summary = message(rows, errors)
    print(summary, flush=True)
    if args.notify:
        if not oz.send(summary[:3900]):
            errors.append('Telegram notification failed')
    if errors or any(row['unknown'] for row in rows):
        return 2
    if any(row['enabled'] for row in rows):
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
