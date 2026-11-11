# поток: inv
"""Rules list aggregation against real PostgreSQL TEMP tables, rolled back."""
import ast
from pathlib import Path
import psycopg2
import psycopg2.extras
import pytest
from core import db


def rules_sql():
    fn=next(n for n in ast.parse(Path('web/app.py').read_text()).body
            if isinstance(n,ast.FunctionDef) and n.name=='opex_rules')
    return next(n.args[0].value for n in ast.walk(fn) if isinstance(n,ast.Call)
                and isinstance(n.func,ast.Attribute) and n.func.attr=='query')


@pytest.fixture
def cur():
    conn=psycopg2.connect(db.DATABASE_URL)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
            defs={}
            for view in ['opex_rule_reach','opex_rule_match']:
                c.execute('select pg_get_viewdef(%s::regclass,true) v',('public.'+view,))
                defs[view]=c.fetchone()['v'].replace('public.','pg_temp.')
            c.execute('set local search_path to pg_temp')
            c.execute('''create temp table bank_txn(id int primary key, cp_inn text,cp_name text,
                operation_date date,direction text,amount numeric,purpose text);
                create temp table opex_rule(id int primary key,cp_inn text,cp_name_key text,
                purpose_like text,amount_min numeric,amount_max numeric,category_id int,
                spread_months int default 1,spread_back boolean default false);
                create temp table bank_txn_opex(txn_id int primary key,category_id int,source text);
                create temp table opex_category(id int primary key,name text);
                insert into opex_category values(1,'General'),(2,'Tax'),(3,'Service');
                insert into opex_rule(id,cp_inn,cp_name_key,purpose_like,amount_min,category_id) values
                    (1,'party',null,null,null,1),(2,'party',null,'УСН',40000,2),
                    (3,null,'ипимя',null,null,3),(4,'absent',null,null,null,1);
                insert into bank_txn values
                    (10,'party','Old name','2026-01-01','DEBIT',100,'goods'),
                    (11,'party','New name','2026-01-02','DEBIT',50000,'УСН'),
                    (12,'party','New name','2026-01-03','DEBIT',40000,'УСН'),
                    (13,null,'ИП Имя','2026-01-04','DEBIT',10,'service'),
                    (14,'party','Latest name','2026-01-05','CREDIT',999,'income');
                insert into bank_txn_opex values(10,1,'rule'),(11,2,'rule'),(12,2,'manual'),(13,3,'rule');''')
            for view,sql in defs.items():c.execute('create temp view '+view+' as '+sql)
            yield c
    finally:
        conn.rollback()
        conn.close()


def test_counts_only_winning_rules_and_excludes_manual(cur):
    cur.execute(rules_sql());rows={r['id']:r for r in cur.fetchall()}
    assert [(rows[i]['n_auto'],rows[i]['s_auto']) for i in [1,2,3,4]]==[(1,100),(1,50000),(1,10),(0,0)]
    assert rows[2]['amount_min']==40000
    assert rows[2]['purpose_like']=='УСН'


def test_latest_names_support_inn_name_fallback_and_unused_rules(cur):
    cur.execute(rules_sql());rows={r['id']:r for r in cur.fetchall()}
    assert rows[1]['cp_name']==rows[2]['cp_name']=='Latest name'
    assert rows[3]['cp_name']=='ИП Имя'
    assert rows[4]['cp_name']==''


def test_winner_counts_materialize_once_for_entire_list(cur):
    cur.execute('explain (analyze,format json) '+rules_sql())
    plan=cur.fetchone()['QUERY PLAN'][0]['Plan']
    def walk(node):
        yield node
        for child in node.get('Plans',[]):yield from walk(child)
    aggregate=[n for n in walk(plan) if n.get('Subplan Name')=='CTE auto_counts']
    assert len(aggregate)==1 and aggregate[0]['Actual Loops']==1
