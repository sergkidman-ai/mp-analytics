# поток: fin
"""Read existing, explicitly published expense rules for each supported cabinet."""
from datetime import date, datetime, timezone
import calendar
import hashlib
import json
import re
from pathlib import Path
from decimal import Decimal, localcontext
from customer_guard.domain import ACCOUNTS, organization, business_date
from core import db
from core.ozon_expense_basis import BasisUnavailable, get_basis, shipment_rule
from shipment_guard.economics import number


def pinned_month():
    """Only the customer's explicit guard setting can pin a historical month."""
    if not db.query("SELECT to_regclass('customer_guard_runtime') name")[0]['name']:
        return None
    rows = db.query('SELECT settings FROM customer_guard_runtime WHERE id')
    month = (rows[0]['settings'] or {}).get('expense_month') if rows else None
    if month is not None and not re.fullmatch(r'\d{4}-\d{2}', str(month)):
        raise BasisUnavailable('invalid_customer_expense_month')
    return month


def fixed_month_rule(account, month):
    year, month_number = map(int, month.split('-'))
    first = date(year, month_number, 1)
    last = first.replace(day=calendar.monthrange(year, month_number)[1])
    if account in ('oz_acc1', 'oz_acc2'):
        rows = db.query("""SELECT payload FROM fin_ozon_expense_basis
            WHERE account=%s AND period_from=%s AND period_to=%s AND status='complete'
            ORDER BY created_at DESC,version LIMIT 1""", (account, first, last))
        if not rows:
            raise BasisUnavailable('confirmed_pinned_ozon_basis_missing')
        basis = rows[0]['payload']
        if (basis.get('account') != account or basis.get('status') != 'complete'
                or basis.get('period_from') != str(first) or basis.get('period_to') != str(last)):
            raise BasisUnavailable('pinned_ozon_basis_identity_mismatch')
        retention = number(basis['retention'])
        with localcontext() as context:
            context.prec = 80
            if number(basis['keep']) != Decimal(1) - retention:
                raise BasisUnavailable('pinned_ozon_basis_ratio_mismatch')
        rule = dict(account=account, platform='ozon', organization_id=organization(account),
                    retention=retention, version=basis['version'],
                    source_sha256=basis['source_sha256'])
    elif account in ('wb_acc1', 'wb_acc2'):
        from shipment_guard.wb_expense_basis import candidate
        path = Path(__file__).resolve().parents[1] / 'reports/data/mp_wb_hist.json'
        raw = path.read_bytes()
        # The original publisher selects the month preceding this explicit base date.
        publication_date = date(year + (month_number == 12), month_number % 12 + 1, 1)
        try:
            rule = candidate(json.loads(raw), account, publication_date)
        except (KeyError, ValueError):
            raise BasisUnavailable('confirmed_pinned_wb_basis_missing') from None
        rule['source_sha256'] = hashlib.sha256(raw).hexdigest()
    else:
        raise BasisUnavailable('unsupported_guard_account')
    if not 0 <= number(rule['retention']) < 1:
        raise BasisUnavailable('invalid_pinned_expense_ratio')
    return dict(rule, enabled=True, base_from=first, base_to=last,
                valid_until=date.max, basis_kind='monthly', basis_pinned=True,
                source='explicit customer_guard expense_month=' + month,
                version='customer-fixed-month-v1:' + month + ':' + rule['version'])


def get_rule(account, *, as_of=None):
    today = as_of or business_date(datetime.now(timezone.utc))
    if account == 'ya_acc1':
        # Explicit customer-order policy, Sergey 2026-10-22. Not a monthly finance report.
        return dict(account=account,platform='yandex',organization_id=organization(account),enabled=True,
            retention=Decimal('0.72'),version='customer-ya-fixed-retention-72-v1',
            basis_kind='fixed',base_from=None,base_to=None,valid_until=date.max)
    month = pinned_month()
    if month:
        return fixed_month_rule(account, month)
    if account in ('oz_acc1','oz_acc2'):
        return shipment_rule(account, as_of=today)
    if account not in ('wb_acc1','wb_acc2'):
        raise BasisUnavailable('unsupported_guard_account')
    if not db.query("SELECT to_regclass('ms_shipment_rules') name")[0]['name']:
        raise BasisUnavailable('wb_rule_missing')
    rows = db.query("SELECT * FROM ms_shipment_rules WHERE account=%s", (account,))
    if len(rows) != 1:
        raise BasisUnavailable('wb_rule_missing')
    r = rows[0]
    if not (r['enabled'] and r['platform']=='wb' and r['valid_until']>=today
            and str(r['organization_id'])==organization(account)
            and r['base_to']<today and r.get('version') and 0 <= number(r['retention']) < 1):
        raise BasisUnavailable('wb_rule_unavailable_or_expired')
    return r


def expense_basis(account):
    if account in ('oz_acc1','oz_acc2') and not pinned_month():
        return get_basis(account)
    r = get_rule(account)
    return dict(version=r['version'],period_from=str(r['base_from'] or ''),period_to=str(r['base_to'] or ''))
