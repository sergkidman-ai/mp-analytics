# поток: fin
"""Read existing, explicitly published expense rules for each supported cabinet."""
from datetime import date
from core import db
from core.ozon_expense_basis import BasisUnavailable, get_basis, shipment_rule
from shipment_guard.economics import number


def get_rule(account, *, as_of=None):
    today = as_of or date.today()
    if account == 'oz_acc2':
        return shipment_rule(account, as_of=today)
    if account != 'wb_acc2':
        raise BasisUnavailable('unsupported_guard_account')
    if not db.query("SELECT to_regclass('ms_shipment_rules') name")[0]['name']:
        raise BasisUnavailable('wb_rule_missing')
    rows = db.query("SELECT * FROM ms_shipment_rules WHERE account=%s", (account,))
    if len(rows) != 1:
        raise BasisUnavailable('wb_rule_missing')
    r = rows[0]
    if not (r['enabled'] and r['platform']=='wb' and r['valid_until']>=today
            and str(r['organization_id'])=='f5bea6e0-c2ac-11ef-0a80-0221000adcc4'
            and r['base_to']<today and r.get('version') and 0 <= number(r['retention']) < 1):
        raise BasisUnavailable('wb_rule_unavailable_or_expired')
    return r


def expense_basis(account):
    if account == 'oz_acc2':
        return get_basis(account)
    r = get_rule(account)
    return dict(version=r['version'],period_from=str(r['base_from']),period_to=str(r['base_to']))
