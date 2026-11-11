# поток: inv
"""Actual UI upsert SQL against disposable TEMP tables, always rolled back."""
import ast
from pathlib import Path
import psycopg2
import pytest
from core import db


def endpoint_sql(column):
    module=ast.parse(Path('web/app.py').read_text())
    fn=next(n for n in module.body if isinstance(n,ast.FunctionDef) and n.name=='opex_assign')
    return next(n.args[0].value for n in ast.walk(fn) if isinstance(n,ast.Call) and n.args
                and isinstance(n.args[0],ast.Constant) and isinstance(n.args[0].value,str)
                and f'INSERT INTO opex_rule ({column},' in n.args[0].value)


@pytest.fixture
def cur():
    conn=psycopg2.connect(db.DATABASE_URL)
    try:
        with conn.cursor() as c:
            c.execute('SET LOCAL search_path TO pg_temp')
            c.execute('''CREATE TEMP TABLE opex_rule(id bigserial primary key, cp_inn text,
                cp_name_key text, purpose_like text, amount_min numeric,amount_max numeric,
                category_id int,spread_months int,spread_back boolean,updated_at timestamptz default now())''')
            for column,suffix in [('cp_inn','inn'),('cp_name_key','name')]:
                c.execute(f"CREATE UNIQUE INDEX uq_opex_rule_{suffix} ON opex_rule({column}, (coalesce(lower(purpose_like),''))) WHERE coalesce({column},'')<>''")
            yield c
    finally:
        conn.rollback()
        conn.close()


def repair(cur):
    cur.execute(Path('migrations/228_opex_rule_amount_indexes_repair.sql').read_text())


def test_legacy_indexes_reproduce_ui_rule_failure(cur):
    with pytest.raises(psycopg2.errors.InvalidColumnReference):
        cur.execute(endpoint_sql('cp_inn'),('party',None,None,None,1,1,False))


@pytest.mark.parametrize('column',['cp_inn','cp_name_key'])
def test_repaired_indexes_support_ranges_and_idempotent_updates(cur,column):
    repair(cur)
    sql=endpoint_sql(column)
    for lo,hi,category in [(None,40000,1),(40000,None,2)]:
        cur.execute(sql,('party','Tax',lo,hi,category,1,False))
    cur.execute(sql,('party','TAX',None,40000,3,2,True))
    cur.execute('select category_id,spread_months,spread_back from opex_rule order by id')
    assert cur.fetchall()==[(3,2,True),(2,1,False)]


def test_index_repair_preserves_existing_rules(cur):
    cur.execute("insert into opex_rule(cp_inn,purpose_like,category_id,spread_months,spread_back) values('party','goods',1,3,true)")
    cur.execute('select * from opex_rule');before=cur.fetchall()
    repair(cur)
    cur.execute('select * from opex_rule');assert cur.fetchall()==before
