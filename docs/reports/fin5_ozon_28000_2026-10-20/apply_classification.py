# поток: fin
"""Точечная смена классификации двух type127; исходные деньги не меняются."""
import json
from pathlib import Path

import psycopg2.extras

from core import db
from collectors import ozon_accrual_adapter as adapter

OUT = Path(__file__).resolve().parent
KEYS = (('oz_acc1', 64837649742), ('oz_acc2', 64837429390))


def main():
    changes = []
    with db.get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute('''SELECT t.* , a.payload AS accrual_payload
                           FROM raw_ozon_transaction t JOIN raw_ozon_accrual a
                             ON a.account=t.account AND a.accrual_id=t.operation_id
                           WHERE (t.account,t.operation_id) IN %s FOR UPDATE OF t''',
                        (KEYS,))
            rows = [dict(x) for x in cur.fetchall()]
            assert len(rows) == 2
            assert not (OUT / 'classification_db_backup.json').exists(), 'Already applied; inspect saved verification.'
            for row in rows:
                old = row['payload']
                assert old['_source'] == 'accrual'
                assert old['operation_type'] == 'AccrualType_127'
                assert float(old['amount']) == -28000
                new = adapter.build(row['accrual_payload'], None, {})
                changed = {k for k in new if new[k] != old.get(k)}
                assert changed == {'operation_type', 'operation_type_name', 'type'}, changed
                changes.append({'account': row['account'], 'operation_id': row['operation_id'],
                                'patch': {k: new[k] for k in changed}})
            (OUT / 'classification_db_backup.json').write_text(
                json.dumps(rows, ensure_ascii=False, indent=2, default=str))
            for change in changes:
                cur.execute('''UPDATE raw_ozon_transaction SET payload=payload || %s::jsonb
                               WHERE account=%s AND operation_id=%s
                                 AND payload->>'_source'='accrual'
                                 AND payload->>'operation_type'='AccrualType_127' ''',
                            (psycopg2.extras.Json(change['patch']), change['account'],
                             change['operation_id']))
                assert cur.rowcount == 1
    (OUT / 'classification_applied.json').write_text(
        json.dumps(changes, ensure_ascii=False, indent=2))
    print('Updated exactly 2 derived transaction classifications; money/source accruals unchanged.')


if __name__ == '__main__':
    main()
