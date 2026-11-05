# поток: print
"""Organization scope from the identity supplied by the authenticated proxy."""
from dataclasses import dataclass

from print_service.domain import PrintBlocked


ORGANIZATIONS = (
    dict(id='digital', name='Цифровой квадрат', accounts=['oz_acc1', 'wb_acc1', 'ya_acc1']),
    dict(id='diskver', name='Дисквер', accounts=['oz_acc2', 'wb_acc2']),
)
STATUS_FIELDS = ('day', 'demo', 'installed', 'interval_minutes', 'worker_healthy',
                 'last_sync', 'snapshot_day')


@dataclass(frozen=True)
class PrintAccess:
    accounts: frozenset | None = None

    @property
    def restricted(self):
        return self.accounts is not None

    @property
    def organizations(self):
        return [org for org in ORGANIZATIONS if self.accounts is None or set(org['accounts']) <= self.accounts]

    def allows(self, account):
        return self.accounts is None or account in self.accounts

    def allows_counter(self, key):
        return self.allows(str(key or '').split('|', 1)[0])

    def allows_batch(self, batch):
        return (self.allows_counter(batch.get('counter_key')) and bool(batch.get('orders'))
                and all(self.allows(member.get('account')) for member in batch['orders']))

    def status(self, source):
        if not self.restricted:
            return source
        result = {key: source[key] for key in STATUS_FIELDS if key in source}
        result['rows'] = [row for row in source.get('rows', []) if self.allows(row.get('account'))]
        result['counters'] = {key: value for key, value in source.get('counters', {}).items() if self.allows_counter(key)}
        result['ready_batches'] = [batch for batch in source.get('ready_batches', []) if self.allows_counter(batch.get('counter_key'))]
        result['jobs'] = [job for job in source.get('jobs', []) if self.allows_counter(job.get('counter_key'))]
        sync = source.get('sync')
        result['sync'] = None if sync is None else {key: sync[key] for key in
            ('id', 'state', 'created_at', 'started_at', 'finished_at', 'error', 'error_message') if key in sync}
        if sync is not None:
            # One shared background sync covers both organizations. Its total
            # counts must not reveal the other organization's order volume.
            progress = sync.get('progress') or {}
            result['sync']['progress'] = {key: progress[key] for key in ('stage', 'updated_at') if key in progress}
        return result


def access_for_user(username, *, dashboard=False):
    username = str(username or '').strip().lower()
    if username == 'tolkachev':
        return PrintAccess(frozenset({'oz_acc2', 'wb_acc2'}))
    if username == 'print' or dashboard:
        return PrintAccess()
    raise PrintBlocked('print_access_denied')
