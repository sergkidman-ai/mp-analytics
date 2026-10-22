# поток: fin
"""Read existing, explicitly published expense rules for each supported cabinet."""
from datetime import date, timedelta
import calendar
import os
from customer_guard.domain import ACCOUNTS, organization
from core import db
from core.ozon_expense_basis import BasisUnavailable, get_basis, shipment_rule
from shipment_guard.economics import number


def get_rule(account, *, as_of=None):
    today = as_of or date.today()
    if account in ('oz_acc1','oz_acc2'):
        return shipment_rule(account, as_of=today)
    if account == 'ya_acc1':
        from collectors.yandex_finance_basis import active_basis
        try:
            b=active_basis(os.getenv('CUSTOMER_GUARD_YANDEX_BASIS',
                '/opt/mp-analytics/reports/data/yandex_finance_basis/active.json'))
            start=date.fromisoformat(b['month']+'-01')
            end=start.replace(day=calendar.monthrange(start.year,start.month)[1])
            if end != today.replace(day=1)-timedelta(days=1):
                raise ValueError('stale_yandex_basis')
            return dict(account=account,platform='yandex',organization_id=organization(account),enabled=True,
                retention=b['retention'],version='yaexp:'+b['version'],base_from=start,base_to=end,
                valid_until=today.replace(day=calendar.monthrange(today.year,today.month)[1]))
        except (RuntimeError,ValueError,KeyError,OSError) as exc:
            raise BasisUnavailable('yandex_basis_unavailable') from None
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
    return dict(version=r['version'],period_from=str(r['base_from']),period_to=str(r['base_to']))
