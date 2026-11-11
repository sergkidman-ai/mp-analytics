# поток: prc
import ast
from pathlib import Path
from datetime import date
from types import SimpleNamespace
from web import launch_recovery as recovery


def test_restored_models_keep_unknown_dates_and_offer_search_and_pagination(monkeypatch):
    data=[dict(external_code=f'{i:04}',ms_created=None,ms_name='Картридж HP',
               supplier_key=None,stock_supplier='Test',cards=1,archived=False,selling=4) for i in range(205)]
    data.append(dict(data[0],external_code='9000',selling=5))
    monkeypatch.setattr(recovery.db,'query',lambda sql,*a: [] if 'supplier_refs' in sql else data)
    monkeypatch.setattr(recovery,'problem_codes',lambda:{r['external_code'] for r in data if r['selling']<5})
    r=recovery.undated_models(page=2)
    assert r['all_total']==206 and r['total']==205 and r['pages']==3
    assert len(r['models'])==100 and all(m['ms_created'] is None for m in r['models'])
    assert recovery.undated_models(page=99)['page']==3
    assert recovery.undated_models(search='0002')['total']==1
    assert recovery.undated_models(search='missing')['total']==0
    assert recovery.undated_models(gaps=False)['total']==206
    assert recovery.undated_models(supplier='Other')['total']==0
    assert recovery.undated_models(failed=True)['total']==206


def test_launch_api_displays_undated_models_without_cohort_or_sla_dates(monkeypatch):
    from collectors import launch_cards
    monkeypatch.setattr(launch_cards,'summaries',lambda *a:{})
    monkeypatch.setattr(launch_cards,'problem_codes',lambda:{'7000'})
    row=dict(external_code='7000',ms_created=None,ms_name='Картридж HP',supplier_key='Test',cards=1,archived=False)
    monkeypatch.setattr(recovery,'model_supplier_groups',lambda *a:{})
    monkeypatch.setattr(recovery,'undated_models',lambda *a,**kw:dict(models=[row],total=1,all_total=1,page=1,pages=1,suppliers=['Test']))
    cutoffs=[]
    class ReadOnlyDB:
        def query(self,sql,params=None):
            if "WHERE ms_created IS NOT NULL AND ms_created >= %s" in sql: cutoffs.append(params[0])
            if 'SELECT showcase,status,note,checked_at' in sql:return [{'showcase':'oz_acc1','status':'error'}]
            if 'baseline_date' in sql:return [{'baseline_date':date(2026,11,6)}]
            if 'SELECT external_code, showcase, state,' in sql:return [dict(external_code='7000',showcase='oz_acc1',state='selling',selling_at=None,card_at=None,seeded=True,offer='7000',note='')]
            if 'sum(stock)' in sql:return [dict(ext='7000',st=2,tr=0,supplier='Test')]
            if 'sum(pos.qty)' in sql:return [dict(ext='7000',first_sale=date(2026,1,1),qty=3)]
            if 'SELECT p.showcase,p.state,count(*)' in sql:return [dict(showcase='oz_acc1',state='selling',n=1)]
            if 'today,' in sql:return [dict(today=0,d30=0,known=0,models=1)]
            if 'percentile_cont' in sql:return [dict(med=None,n=0)]
            if 'max(updated_at)' in sql:return [dict(u=None)]
            return []
    class App:
        def get(self,*args):return lambda f:f
    source=Path('web/app.py').read_text();node=next(n for n in ast.parse(source).body if isinstance(n,ast.FunctionDef) and n.name=='novelties_launch')
    ns={'db':ReadOnlyDB(),'app':App()}
    exec(compile(ast.Module(body=[node],type_ignores=[]),'web/app.py','exec'),ns)
    result=ns['novelties_launch']()
    assert result['known']==0 and result['cohort']==0 and result['rows']==[]
    r=result['recovered']['rows'][0]
    assert r['created'] is None and r['days_old'] is None and r['sale_days'] is None
    assert r['stock']==2 and r['sold']==3
    assert r['mp']['oz_acc1']['state']=='unknown'
    assert result['recovered']['funnel']['oz_acc1']['unknown']==1
    ns['novelties_launch'](days=0)
    assert cutoffs[-1]==date.min


def test_supplier_filter_recovers_all_groups_from_ms_cards_when_stock_has_no_supplier(monkeypatch):
    from invoice_bot import supplier_groups
    monkeypatch.setattr(supplier_groups,'ID2GROUP',{'id-a':'Группа А','id-b':'Группа Б'})
    model=dict(external_code='7000',ms_created=None,ms_name='Тонер',supplier_key=None,
               stock_supplier=None,cards=2,archived=False,selling=4)
    def query(sql,*args):
        if 'supplier_refs' in sql:
            return [dict(external_code='7000',supplier_refs=['https://api/x/id-a','https://api/x/id-b'])]
        return [dict(model)]
    monkeypatch.setattr(recovery.db,'query',query)
    monkeypatch.setattr(recovery,'problem_codes',lambda:{'7000'})
    for group in ['Группа А','Группа Б']:
        r=recovery.undated_models(supplier=group)
        assert r['total']==1 and r['suppliers']==['Группа А','Группа Б']
        assert r['models'][0]['supplier_key']=='Группа А, Группа Б'


def test_dated_models_before_june_remain_searchable_on_every_page():
    rows=[dict(external_code=f'{i:04}',ms_created=date(2020,1,1),ms_name='Картридж',supplier_key=None) for i in range(621)]
    groups={r['external_code']:{'Группа А','Группа Б'} for r in rows}
    r=recovery.dated_page(rows,page=7,gaps=False,supplier='Группа Б',groups=groups)
    assert r['total']==621 and r['pages']==7 and len(r['models'])==21
    assert r['models'][-1]['external_code']=='0620'
    assert r['models'][0]['supplier_key']=='Группа А, Группа Б'
    assert recovery.dated_page(rows,search='0620',gaps=False)['total']==1
    assert recovery.dated_page(rows,gaps=True,problems={'0620'})['total']==1
