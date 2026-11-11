# поток: inv
"""The OPEX total excludes non-operating categories across all periods/legal entities."""
from pathlib import Path
from decimal import Decimal
import psycopg2
import psycopg2.extras
import pytest
from core import db


MIGRATION=Path('migrations/229_opex_fact_exclusion_repair.sql').read_text()


@pytest.fixture
def cur():
    conn=psycopg2.connect(db.DATABASE_URL)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
            c.execute('set local search_path to pg_temp')
            c.execute('''create temp table opex_category(id int primary key,name text,in_opex boolean);
                create temp table bank_txn(id bigint primary key,org_inn text,operation_date date,
                    cp_name text,purpose text,amount numeric,direction text);
                create temp table bank_txn_opex(txn_id bigint primary key,category_id int,
                    start_month date,spread_months int);
                insert into opex_category values(1,'ФОТ',true),(8,'Закупка товара у поставщиков',false),
                    (9,'Перевод между своими счетами',false),(10,'Other excluded category',false),(12,'Аренда',true);
                insert into bank_txn values
                    (1,'CK','2026-10-01','P','salary',900,'DEBIT'),
                    (2,'CK','2026-10-01','P','goods',2000,'DEBIT'),
                    (3,'CK','2026-10-01','P','own transfer',3000,'DEBIT'),
                    (4,'CK','2026-10-01','P','income',100,'CREDIT'),
                    (5,'DK','2026-10-01','P','salary',700,'DEBIT'),
                    (6,'CK','2026-10-01','P','other excluded',3000,'DEBIT'),
                    (7,'CK','2026-10-01','P','rent',100,'DEBIT');
                insert into bank_txn_opex values
                    (1,1,'2026-10-01',3),(2,8,'2026-10-01',1),(3,9,'2026-10-01',1),
                    (4,1,'2026-10-01',1),(5,1,'2026-10-01',1),(6,10,'2026-10-01',1),(7,12,'2026-10-01',3);''')
            # Reproduce the restored view with the missing exclusion and an existing dependent view.
            c.execute(MIGRATION.replace('AND c.in_opex',''))
            c.execute('''create temp view opex_fact_month as select month,org_inn,category_id,category,
                sum(amount) amount,count(*)::int txn_count from opex_fact_alloc group by 1,2,3,4''')
            yield c
    finally:
        conn.rollback()
        conn.close()


def test_repair_changes_total_and_existing_dependent_month_view(cur):
    cur.execute("select sum(amount) s from opex_fact_month where org_inn='CK' and month='2026-10-01'")
    assert cur.fetchone()['s']==Decimal('8333.33')
    cur.execute(MIGRATION)
    cur.execute("select sum(amount) s from opex_fact_month where org_inn='CK' and month='2026-10-01'")
    assert cur.fetchone()['s']==Decimal('333.33')
    cur.execute('select count(*) n from opex_fact_alloc where category_id in (8,9,10)')
    assert cur.fetchone()['n']==0


def test_legal_entities_are_separate_and_credit_is_excluded(cur):
    cur.execute(MIGRATION)
    cur.execute("select sum(amount) s from opex_fact_month where org_inn='DK' and month='2026-10-01'")
    assert cur.fetchone()['s']==700
    cur.execute('select count(*) n from opex_fact_alloc where txn_id=4')
    assert cur.fetchone()['n']==0


def test_allocation_rounding_assignments_and_idempotency_preserved(cur):
    cur.execute('select * from bank_txn_opex order by txn_id');before=cur.fetchall()
    cur.execute(MIGRATION);cur.execute(MIGRATION)
    cur.execute('select * from bank_txn_opex order by txn_id');assert cur.fetchall()==before
    cur.execute('select sum(amount) s from opex_fact_alloc where txn_id=7')
    assert cur.fetchone()['s']==100
    cur.execute("select amount from opex_fact_alloc where txn_id=7 order by month")
    assert [r['amount'] for r in cur.fetchall()]==[Decimal('33.33'),Decimal('33.33'),Decimal('33.34')]
