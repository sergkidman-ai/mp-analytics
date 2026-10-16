#!/usr/bin/env python3
# поток: card
"""Read-only cartridge.ru product evidence, with optional authorized B2B login."""
import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET

import requests
from lxml import html

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv

BASE = 'https://cartridge.ru'
DEFAULT_OUT = ROOT / 'docs/reports/card_solutionsprint_scraper_restore_2026-10-11'


def text(node):
    return re.sub(r'\s+', ' ', node.text_content()).strip()


def cls(name):
    return f'contains(concat(" ", normalize-space(@class), " "), " {name} ")'


def allowed_url(url):
    p = urlparse(url)
    if p.scheme != 'https' or p.hostname not in ('cartridge.ru', 'www.cartridge.ru') or p.query or p.fragment:
        raise ValueError('Only credential-free HTTPS cartridge.ru URLs allowed')
    return url


def get(session, url):
    allowed_url(url)
    try:
        r = session.get(url, timeout=(15, 45))
    except requests.RequestException:
        raise RuntimeError('Supplier GET failed') from None
    if r.status_code != 200:
        raise RuntimeError(f'Supplier HTTP {r.status_code}')
    allowed_url(r.url)
    return r


def login(session):
    user, password = os.getenv('SOLUTIONSPRINT_LOGIN'), os.getenv('SOLUTIONSPRINT_PASSWORD')
    if not user or not password:
        raise RuntimeError('SOLUTIONSPRINT_LOGIN and SOLUTIONSPRINT_PASSWORD required in .env')
    page = get(session, BASE + '/personal/')
    doc = html.fromstring(page.content)
    forms = doc.xpath('//form[.//input[@name="USER_PASSWORD"]]')
    if len(forms) != 1:
        raise RuntimeError('Unique Bitrix login form not found')
    form = forms[0]
    data = {n.get('name'): n.get('value', '') for n in form.xpath('.//input[@type="hidden"]') if n.get('name')}
    data.update(AUTH_FORM='Y', TYPE='AUTH', backurl='/personal/', USER_LOGIN=user,
                USER_PASSWORD=password, USER_REMEMBER='N')
    action = allowed_url(urljoin(BASE, form.get('action') or '/personal/'))
    try:
        r = session.post(action, data=data, timeout=(15, 45))
    except requests.RequestException:
        raise RuntimeError('Supplier login request failed') from None
    if r.status_code != 200:
        raise RuntimeError(f'Supplier login HTTP {r.status_code}')
    doc = html.fromstring(r.content)
    logout = doc.xpath('//a[contains(@href,"logout=yes") or contains(@href,"logout=Y")]')
    if not logout or doc.xpath('//input[@name="USER_PASSWORD"]'):
        raise RuntimeError('Authenticated session not confirmed; no further private requests')
    return {'authenticated': True, 'verification': 'logout link present, password form absent'}


def parse_card(content, url, authenticated=False):
    doc = html.fromstring(content)
    mains = doc.xpath('//*[@data-entity="main-product-wrap"]')
    if len(mains) != 1:
        raise ValueError('Unique main product block missing')
    main = mains[0]
    # Reject nested recommendations, bundles and related products even inside main wrapper.
    primary = main.xpath('.//*[' + cls('product-page') + ']')
    if len(primary) != 1:
        raise ValueError('Unique primary product section missing')
    primary = primary[0]
    h1 = doc.xpath('//h1')
    if len(h1) != 1 or not text(h1[0]):
        raise ValueError('Unique product title missing')
    title = text(h1[0])
    code_nodes = main.xpath('.//*[' + cls('item-code') + ']')
    code_values = {re.sub(r'^Код товара\s*:\s*', '', text(n)) for n in code_nodes}
    if len(code_values) != 1 or not re.fullmatch(r'\d+', next(iter(code_values), '')):
        raise ValueError('Unique numeric supplier code missing')
    # The dedicated characteristics table is outside the related-product carousel.
    tables = main.xpath('.//table[' + cls('characteristics-table') + ']')
    if len(tables) != 1:
        raise ValueError('Unique primary characteristics table missing')
    table = tables[0]
    if any('recommend' in (n.get('class') or '') for n in table.iterancestors()):
        raise ValueError('Characteristics belong to recommendations')
    props = {}
    source_rows = []
    for row in table.xpath('.//tr'):
        cells = row.xpath('./th | ./td')
        if len(cells) != 2:
            continue
        label, value = text(cells[0]).rstrip(':'), text(cells[1])
        if label in props and props[label] != value:
            raise ValueError('Conflicting primary characteristics')
        if label:
            props[label] = value
            source_rows.append({'label': label, 'value': value})
    availability = primary.xpath('.//*[' + cls('product-availability') + ']')
    if len(availability) > 1:
        raise ValueError('Ambiguous primary availability')
    availability_text = text(availability[0]) if availability else ''
    # Do not infer a quantity from price, add-to-cart buttons or related bundles.
    positive = None
    lower = availability_text.casefold()
    if re.search(r'под\s+заказ|нет\s+в\s+наличии|отсутствует', lower):
        positive = False
    elif re.search(r'в\s+наличии', lower):
        positive = True
    quantity_match = re.search(r'(более\s+)?(\d+)\s*шт', lower)
    quantity = int(quantity_match.group(2)) if quantity_match else None
    warehouse_stocks = {}
    for match in re.finditer(r'Склад\s+([^:—–-]+?)\s*[-:—–]\s*([\d\s]+?)\s*шт\.?', availability_text, re.I):
        warehouse = match.group(1).strip()
        count = int(re.sub(r'\s+', '', match.group(2)))
        if warehouse in warehouse_stocks and warehouse_stocks[warehouse] != count:
            raise ValueError('Conflicting warehouse stock values')
        warehouse_stocks[warehouse] = count
    if warehouse_stocks:
        quantity = None  # Preserve separate warehouse quantities; do not use the first as total.
        positive = any(v > 0 for v in warehouse_stocks.values())
    if quantity == 0:
        positive = False
    badges = [text(n) for n in primary.xpath('.//*[' + cls('label-item') + ']')]
    chip_badges = [b for b in badges if re.search(r'чип|chip|сч[её]тчик', b, re.I)]
    note_nodes = primary.xpath('.//*[' + cls('comments-to-product') + ']')
    notes = [text(n) for n in note_nodes]
    return {'url': url, 'supplier_code': next(iter(code_values)), 'name': title,
            'authenticated': authenticated, 'properties': props,
            'color': props.get('Цвет', ''), 'resource': props.get('Ресурс', ''),
            'product_type': props.get('Вид товара', ''), 'analog': props.get('Аналог', ''),
            'chip_property': {k:v for k,v in props.items() if re.search(r'чип|chip|сч[её]тчик', k, re.I)},
            'chip_badges': chip_badges, 'primary_notes': notes,
            'availability_text': availability_text, 'stock_positive': positive,
            'stock_quantity': quantity, 'quantity_is_lower_bound': bool(quantity_match and quantity_match.group(1)),
            'warehouse_stocks': warehouse_stocks,
            'stock_scope': 'explicit named warehouses' if warehouse_stocks else 'current site region/session; not sum of supplier warehouses',
            'source_property_rows': source_rows,
            'page_sha256': hashlib.sha256(content if isinstance(content, bytes) else content.encode()).hexdigest()}


def discover(session):
    r = get(session, BASE + '/sitemap-iblock-1.xml')
    root = ET.fromstring(r.content)
    return sorted({n.text for n in root.findall('.//{*}loc') if n.text
                   and re.fullmatch(r'/catalog/[^/]+/', urlparse(n.text).path)
                   and urlparse(n.text).hostname == 'cartridge.ru'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', action='append', default=[])
    parser.add_argument('--urls-file', type=Path)
    parser.add_argument('--discover', action='store_true')
    parser.add_argument('--login', action='store_true', help='Use only .env supplier-specific credentials')
    parser.add_argument('--limit', type=int, default=5)
    parser.add_argument('--out', type=Path, default=DEFAULT_OUT / 'public_probe')
    args = parser.parse_args()
    if not 1 <= args.limit <= 10000:
        parser.error('limit must be 1..10000; default is a small read-only probe')
    load_dotenv(ROOT / '.env')
    session = requests.Session()
    session.headers['User-Agent'] = 'DigitalSquare supplier-card audit (read-only)'
    auth = login(session) if args.login else {'authenticated': False, 'verification': 'public mode'}
    urls = args.url[:]
    if args.urls_file:
        urls.extend(x.strip() for x in args.urls_file.read_text().splitlines() if x.strip())
    if args.discover:
        urls.extend(discover(session))
    urls = list(dict.fromkeys(urls))[:args.limit]
    if not urls:
        parser.error('Specify --url, --urls-file or --discover')
    args.out.mkdir(parents=True, exist_ok=True)
    records, errors = [], []
    for url in urls:
        try:
            response = get(session, url)
            record = parse_card(response.content, url, auth['authenticated'])
            record['captured_at_utc'] = datetime.now(timezone.utc).isoformat()
            # Persist extracted evidence only: no cookies, signed forms or login HTML.
            (args.out / (record['supplier_code'] + '.json')).write_text(json.dumps(record, ensure_ascii=False, indent=2)+'\n')
            records.append(record)
        except (ValueError, RuntimeError) as exc:
            errors.append({'url': url, 'error': str(exc)})
        time.sleep(1)
    (args.out / 'records.json').write_text(json.dumps(records, ensure_ascii=False, indent=2)+'\n')
    (args.out / 'errors.json').write_text(json.dumps(errors, ensure_ascii=False, indent=2)+'\n')
    if records:
        with (args.out / 'records.csv').open('w', encoding='utf-8-sig', newline='') as f:
            rows = [{k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v
                     for k,v in r.items()} for r in records]
            w=csv.DictWriter(f,fieldnames=list(rows[0]),delimiter=';');w.writeheader();w.writerows(rows)
    summary = {'at_utc': datetime.now(timezone.utc).isoformat(), **auth,
               'requested': len(urls), 'parsed': len(records), 'errors': len(errors),
               'positive_stock_cards': sum(r['stock_positive'] is True for r in records),
               'unknown_stock_cards': sum(r['stock_positive'] is None for r in records),
               'properties_with_chip': sum(bool(r['chip_property']) for r in records),
               'color_present': sum(bool(r['color']) for r in records),
               'resource_present': sum(bool(r['resource']) for r in records),
               'type_present': sum(bool(r['product_type']) for r in records),
               'ms_writes':0, 'db_writes':0}
    (args.out / 'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(summary,ensure_ascii=False))


if __name__ == '__main__':
    main()
