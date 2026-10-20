# поток: fin
"""Cooperative Ozon posting API lock shared by list and point-get callers.

Separate two-int advisory namespace from the shipment worker's bigint MS lock.
A dedicated autocommit connection owns the session lock; closing it releases
ownership. Call lease.check() before each HTTP attempt, including point-get retries.
"""
import asyncio
import math
import os
import threading
import time
from contextlib import contextmanager, suppress
from contextvars import ContextVar

import psycopg2
from core import db

LOCK_NAMESPACE = 0x4F5A4F4E  # OZON, signed int32; two-key lock space, not MS bigint.
ACCOUNT_KEYS = {'oz_acc1': 1, 'oz_acc2': 2}
_HELD = ContextVar('ozon_posting_locks', default={})


class OzonLockBusy(TimeoutError):
    pass


class OzonLockLost(RuntimeError):
    pass


def _owner():
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return os.getpid(), threading.get_ident(), id(task) if task is not None else None


class _Lease:
    def __init__(self, connection):
        self.connection = connection
        self.owner = _owner()
        self.active = True

    def check(self):
        """Fail closed if the lock-owning DB session has been lost/released."""
        if not self.active or self.owner != _owner() or self.connection.closed:
            raise OzonLockLost('Ozon account lock is no longer owned')
        try:
            with self.connection.cursor() as cur:
                cur.execute('SELECT 1')
        except psycopg2.Error:
            raise OzonLockLost('Ozon account lock connection lost') from None


@contextmanager
def account_lock(account, *, timeout=5):
    """Own the account's posting API session lock for this context.

    timeout bounds contention waiting; 0 is one nonblocking acquisition attempt.
    The dedicated DB connection has a separate <=5s connection timeout. Nested
    calls in the SAME thread/task reuse the lease, without reacquiring/unlocking.
    Different processes/threads/tasks compete through PostgreSQL, not Python state.
    Do not hold this lock while acquiring the MS worker lock or writing MS docs.
    """
    if account not in ACCOUNT_KEYS:
        raise ValueError('Unknown Ozon account for API lock')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
        raise ValueError('Ozon lock timeout must be finite nonnegative seconds')
    held = _HELD.get().get(account)
    if held is not None and held.owner == _owner():
        held.check()
        yield held
        return
    deadline = time.monotonic() + timeout
    try:
        connection = psycopg2.connect(db.DATABASE_URL,
            connect_timeout=max(1, min(5, math.ceil(timeout))),
            application_name='ozon-posting-api-lock',
            options='-c statement_timeout=1000',
            keepalives=1, keepalives_idle=5, keepalives_interval=1, keepalives_count=2,
            tcp_user_timeout=5000)
    except psycopg2.Error:
        raise OzonLockLost('Cannot establish Ozon account lock connection') from None
    lease = _Lease(connection)
    key = (LOCK_NAMESPACE, ACCOUNT_KEYS[account])
    token = None
    acquired = False
    try:
        connection.autocommit = True
        while True:
            try:
                with connection.cursor() as cur:
                    cur.execute('SELECT pg_try_advisory_lock(%s, %s)', key)
                    acquired = bool(cur.fetchone()[0])
            except psycopg2.Error:
                raise OzonLockLost('Cannot acquire Ozon account lock') from None
            if acquired:
                if timeout > 0 and time.monotonic() > deadline:
                    raise OzonLockBusy('Ozon account lock acquisition budget exhausted')
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OzonLockBusy('Ozon posting API account is busy')
            time.sleep(min(0.1, remaining))
        token = _HELD.set({**_HELD.get(), account: lease})
        yield lease
    finally:
        lease.active = False
        if token is not None:
            _HELD.reset(token)
        if acquired:
            with suppress(psycopg2.Error):
                with connection.cursor() as cur:
                    cur.execute('SELECT pg_advisory_unlock(%s, %s)', key)
        with suppress(psycopg2.Error):
            connection.close()
