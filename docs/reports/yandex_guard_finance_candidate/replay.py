# поток: fin
"""Replay all monetary calculations from retained raw sources, without API/DB writes."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
from collectors import yandex_finance_basis as b
from tools.yandex_guard_finance_preview import compare
p=Path(__file__).resolve().parent;f=p/'fresh';old=b.load(p/'candidate.json')
services=b.service_rows(b.load(f/'united-marketplace-services_sheets.json'),old['month'])
x,_=b.build(old['month'],b.load(f/'orders.json'),b.load(f/'orders_metadata.json'),services,
            old['services'],b.load(f/'closure_rows.json'),old['closure'],old['cogs'])
assert str(x['turnover'])==old['turnover'] and str(x['expenses'])==old['expenses']
assert str(x['retention'])==old['retention'] and x['version']==old['version']
s=compare(p/'promos',p/'candidate.json',p/'baseline/reports/data/mp_yandex_hist.json')
print('PASS: exact source replay, same version and same snapshot comparison;',s['rows'],'SKU')
