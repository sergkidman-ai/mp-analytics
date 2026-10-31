# поток: fin
"""Publish the last closed WB month as the next month's expense rule."""
import calendar
import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from core import db
from customer_guard.domain import business_date, organization

EXPENSES = ('delivery', 'storage', 'acceptance', 'ads', 'points', 'penalty', 'other')
ACCOUNTS = ('wb_acc1', 'wb_acc2')


def candidate(snapshot, account, today):
    """Use only a complete previous calendar month; never extend a stale basis."""
    if account not in ACCOUNTS:
        raise ValueError('unsupported_wb_account')
    year, month = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    period = f'{year:04d}-{month:02d}'
    keys = snapshot['period_keys']
    if period not in keys or period in snapshot.get('provisional', []):
        raise ValueError('closed_wb_month_unavailable')
    idx = keys.index(period)
    lines = snapshot['accounts'][account]['lines']

    def amount(key):
        value = Decimal(str(lines[key][idx]))
        if not value.is_finite():
            raise ValueError('invalid_wb_expense_amount')
        return value

    revenue = amount('own_price')
    if revenue <= 0:
        raise ValueError('invalid_wb_revenue')
    net = amount('to_pay') - sum((amount(k) for k in EXPENSES), Decimal(0)) + amount('compensation')
    retention = (revenue - net - amount('returns_pay')) / revenue
    if not 0 <= retention < 1:
        raise ValueError('invalid_wb_retention')
    basis = {k: str(amount(k)) for k in ('own_price', 'to_pay', 'returns_pay', 'compensation', *EXPENSES)}
    digest = hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()
    return dict(account=account, platform='wb', organization_id=organization(account),
                retention=retention, base_from=date(year, month, 1),
                base_to=date(year, month, calendar.monthrange(year, month)[1]),
                valid_until=date(today.year, today.month, calendar.monthrange(today.year, today.month)[1]),
                version=f'wb-expense-{period}:{digest[:16]}', source_sha256=digest)


def publish(path, today=None):
    today = today or business_date(datetime.now(timezone.utc))
    path = Path(path)
    raw = path.read_bytes()
    snapshot = json.loads(raw)
    candidates = [candidate(snapshot, account, today) for account in ACCOUNTS]
    source_sha256 = hashlib.sha256(raw).hexdigest()
    for rule in candidates:
        rule['source_sha256'] = source_sha256
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            for rule in candidates:
                cur.execute('''SELECT platform,organization_id,base_to FROM ms_shipment_rules
                               WHERE account=%s FOR UPDATE''', (rule['account'],))
                previous = cur.fetchone()
                if not previous or previous[0] != 'wb' or str(previous[1]) != rule['organization_id']:
                    raise ValueError('wb_rule_identity_mismatch')
                if previous[2] > rule['base_to']:
                    raise ValueError('newer_wb_rule_already_published')
                cur.execute('''UPDATE ms_shipment_rules SET retention=%s,base_from=%s,base_to=%s,
                    valid_until=%s,source=%s,source_sha256=%s,version=%s,loaded_at=now()
                    WHERE account=%s AND (version IS DISTINCT FROM %s OR valid_until IS DISTINCT FROM %s
                        OR source_sha256 IS DISTINCT FROM %s)''',
                    (rule['retention'], rule['base_from'], rule['base_to'], rule['valid_until'],
                     str(path), rule['source_sha256'], rule['version'], rule['account'],
                     rule['version'], rule['valid_until'], rule['source_sha256']))
    return candidates
