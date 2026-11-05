# поток: rev
"""Shared daily reservations; unknown-price requests must not be submitted."""
from decimal import Decimal
import os
from core import db


def setup():
    db.execute('''CREATE TABLE IF NOT EXISTS rev_llm_budget (
        request_key text PRIMARY KEY, day date NOT NULL,
        reserved_usd numeric NOT NULL CHECK(reserved_usd>=0),
        actual_usd numeric CHECK(actual_usd>=0), provider text NOT NULL,
        created_at timestamptz DEFAULT now())''')


def reserve(key, provider, upper_cost):
    if upper_cost is None or Decimal(str(upper_cost)) <= 0:
        raise RuntimeError('Unpriced LLM request blocked')
    amount = Decimal(str(upper_cost))
    cap = min(Decimal('2'), Decimal(os.getenv('FEEDBACK_LLM_DAILY_CAP_USD', '2')))
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('rev_llm_daily_budget'))")
            cur.execute('SELECT 1 FROM rev_llm_budget WHERE request_key=%s', (key,))
            if cur.fetchone():
                return False
            cur.execute("SELECT COALESCE(sum(COALESCE(actual_usd,reserved_usd)),0) FROM rev_llm_budget WHERE day=(now() AT TIME ZONE 'Europe/Moscow')::date")
            if cur.fetchone()[0] + amount > cap:
                raise RuntimeError('Shared daily LLM cap reached')
            cur.execute("INSERT INTO rev_llm_budget(request_key,day,reserved_usd,provider) VALUES(%s,(now() AT TIME ZONE 'Europe/Moscow')::date,%s,%s)", (key,amount,provider))
    return True


def status():
    return db.query("""SELECT COALESCE(sum(actual_usd),0) AS spent_usd,
        COALESCE(sum(reserved_usd) FILTER(WHERE actual_usd IS NULL),0) AS reserved_usd
        FROM rev_llm_budget WHERE day=(now() AT TIME ZONE 'Europe/Moscow')::date""")[0]
