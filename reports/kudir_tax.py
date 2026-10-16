# поток: fin
"""УСН 15% from imported accountant rows; no tax-ledger generation or payments."""
from decimal import Decimal, ROUND_HALF_UP
import json

from reports import kudir_report

RATE = Decimal('0.15')
MIN_RATE = Decimal('0.01')
RUBLE = Decimal('1')
ZERO = Decimal('0.00')
FNS_URL = 'https://www.nalog.gov.ru/rn77/taxation/TAXES/usn/'
CONFIRMATION_PATH = kudir_report.BASE_DIR / 'reports/data/kudir/confirmations/cifrovoi_2026_income.json'
LABELS = {1: 'I квартал', 2: 'Полугодие', 3: '9 месяцев', 4: 'Год'}


def confirmed_income(data):
    """A user's confirmation belongs only to the same file, rows and amounts."""
    if data.get('org_inn') != '7807355364' or data['quarter'] != 2 or not CONFIRMATION_PATH.is_file():
        return None
    note = json.loads(CONFIRMATION_PATH.read_text(encoding='utf-8'))
    rows = data['income_rows_outside_formula']
    amount = sum((Decimal(x['income']) for x in data['operations'] if x['row'] in rows), ZERO)
    if (note.get('org') != 'cifrovoi' or note.get('year') != data['year']
            or note.get('quarter') != data['quarter'] or note.get('source_sha256') != data['sha256']
            or note.get('rows') != rows or Decimal(note['income']) != amount):
        return None
    return note


def calculate_periods(reports):
    """Tax in whole rubles, cumulative base, annual minimum only for a full year."""
    income = expense = ZERO
    previous_tax = Decimal('0')
    result = []
    expected = 1
    year = None
    org_inn = None
    for data in sorted(reports, key=lambda x: x['quarter']):
        if data['quarter'] != expected:
            raise ValueError('Для расчёта нужны все кварталы с начала года')
        if year is not None and (data['year'] != year or data.get('org_inn') != org_inn):
            raise ValueError('Нельзя смешивать годы или организации')
        year, org_inn = data['year'], data.get('org_inn')
        if result and result[-1]['is_partial']:
            raise ValueError('Нельзя продолжать расчёт после неполного квартала')
        expected += 1
        income += Decimal(data['calculated']['income'])
        expense += Decimal(data['calculated']['expense'])
        difference = income - expense
        base = max(difference, ZERO)
        ordinary = (base * RATE).quantize(RUBLE, rounding=ROUND_HALF_UP)
        partial = bool(data.get('is_partial'))
        annual = data['quarter'] == 4 and not partial
        minimum_reference = (max(income, ZERO) * MIN_RATE).quantize(RUBLE, rounding=ROUND_HALF_UP)
        tax = max(ordinary, minimum_reference) if annual else ordinary
        result.append({
            'quarter': data['quarter'], 'year': year,
            'label': LABELS[data['quarter']] if not partial else 'С начала года · неполный IV квартал',
            'income': str(income), 'expense': str(expense), 'difference': str(difference),
            'tax_base': str(base), 'ordinary_tax': str(ordinary), 'tax': str(tax),
            'tax_change': str(tax - previous_tax), 'rate_percent': '15',
            'is_partial': partial, 'is_annual': annual,
            'minimum_tax': str(minimum_reference) if annual else None,
            'minimum_tax_reference': str(minimum_reference),
            'minimum_applied': annual and minimum_reference > ordinary,
            'through': (data.get('document_date_range') or {}).get('to') if partial else None,
        })
        previous_tax = tax
    return result


def load(org, quarter):
    reports = [kudir_report.load(q, org) for q in kudir_report.quarters(org)]
    periods = calculate_periods(reports)
    current = next(x for x in periods if x['quarter'] == quarter)
    confirmation = next((note for data in reports if data['quarter'] <= quarter
                         if (note := confirmed_income(data))), None)
    return {'year': current['year'], 'org': org, 'current': current, 'periods': periods,
            'source': 'calculated_all_imported_rows', 'rate_percent': '15',
            'income_confirmation': confirmation, 'paid_advances': None,
            'prior_year_losses': None, 'automatic_kudir_enabled': False,
            'fns_url': FNS_URL}
