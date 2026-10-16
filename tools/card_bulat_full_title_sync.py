#!/usr/bin/env python3
# поток: card
"""Freeze safe complete Bulat titles from descriptions; authorized one-off write at 21 MSK."""
import argparse
import csv
import fcntl
import hashlib
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import ms_api
from invoice_bot.supplier_groups import ID2GROUP
from tools.card_rapid_description_sync import apply
from tools.card_bulat_description_audit import classes, article_key, normalized

OUT = ROOT / 'docs/reports/card_bulat_full_title_sync_2026-10-09'
IDS = {sid for sid, group in ID2GROUP.items() if group == 'Булат'}
APPROVED_TITLES = {
    ('3779', 'AFBRHL2350030'): 'Тонер-картридж Brother HL-L2350 TN-2420 (3k) 7Q, с чипом',
    ('5777', 'AFBRHL2350020'): 'Тонер-картридж 7Q TN-2410 для Brother HL-L2350 (Чёрный, 1200 стр.), с чипом',
}
MSK = ZoneInfo('Europe/Moscow')
PROSE = re.compile(r'представляет\s+собой|практичное\s+решение|основные\s+характеристики|'
                   r'позволяет|обеспечивает|предназначен|рассчитан|пользовател|для\s+организаций|'
                   r'совместимый\s+расходный\s+материал|высококачественн|для\s+устройств\s*:|'
                   r'\s[—–]\s|[.!?]\s+[А-ЯA-Z]', re.I)


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def extract_title(description):
    text = html.unescape(description or '').strip()
    if not text:
        return '', 'EMPTY_SOURCE_DESCRIPTION'
    if re.search(r'<[^>]+>', text):
        return '', 'UNSUPPORTED_HTML'
    # The supplied feed separates title and body with repeated whitespace.
    title = normalized(re.split(r'\s{2,}', text, maxsplit=1)[0])
    if not 20 <= len(title) <= 350 or PROSE.search(title):
        return '', 'NO_SAFE_TITLE_BOUNDARY'
    return title, ''


def fetch_feed():
    response = requests.get('https://bulat-group.ru/price/clientapi.xml', timeout=(15, 90))
    response.raise_for_status()
    root = ET.fromstring(response.content)
    assert root.tag == 'yml_catalog'
    return response.content, root


def index_offers(root):
    """Stock is deliberately not a filter: all offers remain eligible."""
    index = defaultdict(dict)
    for offer in root.findall('.//offer'):
        for value in (offer.get('id'), offer.findtext('vendorCode')):
            if article_key(value):
                index[article_key(value)][offer.get('id')] = offer
    return index


def prepare():
    raw, root = fetch_feed()
    (OUT / 'clientapi.xml').write_bytes(raw)
    index = index_offers(root)
    cards = {}
    for sid in sorted(IDS):
        for card in ms_api.iter_rows('/entity/product',
                                    {'filter': f'supplier={ms_api.BASE}/entity/counterparty/{sid};archived=false'}, page=500):
            assert ms_api.meta_id(card, 'supplier') in IDS and not card.get('archived')
            cards[card['id']] = card
    save('ms_before.json', list(cards.values()))
    plan, review, held, counts = [], [], [], Counter()
    for card in cards.values():
        matches = list(index.get(article_key(card.get('article')), {}).values())
        row = {'id': card['id'], 'external_code': card.get('externalCode') or '',
               'article': card.get('article') or '', 'supplier_id': ms_api.meta_id(card, 'supplier'),
               'old_description': card.get('description') or '', 'description': '',
               'feed_name': '', 'feed_offer_id': '', 'api_free_stock': '', 'status': ''}
        if len(matches) != 1:
            row['status'] = 'NO_UNIQUE_FEED_MATCH'
        else:
            offer = matches[0]
            row.update({'feed_name': offer.findtext('name') or '', 'feed_offer_id': offer.get('id'),
                        'api_free_stock': float((offer.findtext('count') or '0').replace(',', '.'))})
            title, reason = extract_title(offer.findtext('description'))
            approval = APPROVED_TITLES.get((row['external_code'], row['feed_offer_id']))
            if approval:
                title, reason = approval, ''
            row['description'] = title
            if approval:
                row['status'] = 'ALREADY_EQUAL' if normalized(row['old_description']) == title else 'UPDATE_APPROVED'
                held.append({**row, 'feed_description': offer.findtext('description') or '',
                             'ms_name': card.get('name') or '', 'user_approved': True})
                if row['status'] == 'UPDATE_APPROVED':
                    plan.append(row)
            elif reason:
                row['status'] = reason
            elif len(title) < len(normalized(row['old_description'])) * 0.75:
                row['status'] = 'POSSIBLY_TRUNCATED_TITLE'
            elif classes(row['old_description']) and classes(title) != classes(row['old_description']):
                row['status'] = 'EXISTING_CHIP_INFORMATION_CHANGE'
            elif normalized(row['old_description']) == title:
                row['status'] = 'ALREADY_EQUAL'
            else:
                row['status'] = 'UPDATE'
                plan.append(row)
        counts[row['status']] += 1
        review.append(row)
    plan.sort(key=lambda r: r['id'])
    save('plan.json', plan)
    save('review.json', review)
    save('approved_3779_5777.json', held)
    with (OUT / 'plan.csv').open('w', encoding='utf-8-sig', newline='') as fh:
        writer = csv.DictWriter(fh, fieldnames=list(review[0]), delimiter=';')
        writer.writeheader()
        writer.writerows(plan)
    summary = {'at': datetime.now(MSK).isoformat(), 'feed_date': root.attrib.get('date'),
               'feed_sha256': hashlib.sha256(raw).hexdigest(), 'active_supplier_cards': len(cards),
               'changes': len(plan), 'statuses': dict(counts), 'held_codes': [],
               'approved_codes': ['3779', '5777'],
               'changes_with_zero_stock': sum(r['api_free_stock'] == 0 for r in plan),
               'stock_filter': False, 'source': 'safe title before prose in feed description plus approved titles',
               'writes': 0, 'verified': 0, 'status': 'prepared',
               'authorized_start': '2026-10-09 21:00 MSK', 'write_fields': ['description']}
    save('summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / 'lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not args.apply:
            if (OUT / 'journal.jsonl').exists():
                raise RuntimeError('Writing has begun; do not replace frozen plan')
            prepare()
            return
        now = datetime.now(MSK)
        if now.date().isoformat() != '2026-10-09' or not 21 <= now.hour < 23:
            raise RuntimeError('Only authorized date/window 09 Oct 21:00-23:00 MSK')
        plan = json.loads((OUT / 'plan.json').read_text())
        summary = json.loads((OUT / 'summary.json').read_text())
        raw, root = fetch_feed()
        if hashlib.sha256(raw).hexdigest() != summary['feed_sha256']:
            raise RuntimeError('Feed changed since frozen plan; stop before writes')
        if any(r['supplier_id'] not in IDS for r in plan):
            raise RuntimeError('Frozen plan scope invalid')
        for row in plan:
            if row['status'] == 'UPDATE_APPROVED' and row['description'] != APPROVED_TITLES.get((row['external_code'], row['feed_offer_id'])):
                raise RuntimeError('Approved title does not match user decision')
        summary['explicit_time_authorization'] = 'User requested 21:00 MSK'
        apply(OUT, plan, summary, allow_daytime=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        if (OUT / 'summary.json').exists():
            result = json.loads((OUT / 'summary.json').read_text())
            result.update({'status': 'failed', 'error_type': type(exc).__name__})
            save('summary.json', result)
        raise
