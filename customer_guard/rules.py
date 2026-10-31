# поток: fin
"""Read existing, explicitly published expense rules for each supported cabinet."""
from datetime import date, datetime, timezone
from decimal import Decimal
from customer_guard.domain import ACCOUNTS, organization, business_date
from core import db
from core.ozon_expense_basis import BasisUnavailable, get_basis, shipment_rule
from shipment_guard.economics import number


def get_rule(account, *, as_of=None):
    today = as_of or business_date(datetime.now(timezone.utc))
    if account in ('oz_acc1','oz_acc2'):
        return shipment_rule(account, as_of=today)
    if account == 'ya_acc1':
        # Explicit customer-order policy, Sergey 2026-10-22. Not a monthly finance report.
        return dict(account=account,platform='yandex',organization_id=organization(account),enabled=True,
            retention=Decimal('0.72'),version='customer-ya-fixed-retention-72-v1',
            basis_kind='fixed',base_from=None,base_to=None,valid_until=date.max)
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
    if account in ('oz_acc1','oz_acc2'):
        return get_basis(account)
    r = get_rule(account)
    return dict(version=r['version'],period_from=str(r['base_from'] or ''),period_to=str(r['base_to'] or ''))
