# поток: ozon-family-strategy; preserve dependencies, hashes and executed commands.
from common import *
import sys
if len(sys.argv)>1 and sys.argv[1]=='verify':
 bad=[];n=0
 for line in (O/'SHA256SUMS').read_text().splitlines():
  expected,name=line.split('  ',1);n+=1
  if sha(O/name)!=expected:bad.append(name)
 print(js({'files_verified':n,'mismatches':bad}));assert not bad;sys.exit(0)
expected={}
for line in (AUD/'SHA256SUMS').read_text().splitlines():
 digest,n=line.split('  ',1);expected[n]=digest
audpaths=['cards.csv','agents/a_cards.csv','agents/a_tc_contexts.json','snapshot/catalog.json','snapshot/db_prc_tc_model.json','snapshot/db_ms_product.json','snapshot/db_set_cost.json','snapshot/db_manifest.json','snapshot/catalog_coverage.json','snapshot/catalog_provenance.json']
audpaths += [str(p.relative_to(AUD)) for p in sorted(S.glob('attrs_*.json'))]
sources=[];mismatch=[]
for n in audpaths:
 p=AUD/n;v=sha(p)
 if expected.get(n)!=v:mismatch.append(n)
 sources.append({'path':str(p.relative_to(O.parent.parent.parent)),'sha256':v,'previous_audit_hash_matches':expected.get(n)==v})
assert not mismatch,mismatch
hist=read(O/'history_reconciliation.json')
yearpaths={Path(p) for p in hist['raw_sales_sources']+hist['raw_cpc_sources']}
yearpaths.update(Y/n for n in ['sku_general_sales_daily_year.csv','sku_positive_cpc_daily_year.csv','order_collection_coverage.json','sku_collection_status.json','sku_report_jobs.json'])
for p in sorted(yearpaths):sources.append({'path':str(p.relative_to(O.parent.parent.parent)),'sha256':sha(p)})
codepaths=['docs/CUSTOMER_ORDER_RULES.md','.claude/rules/customer-orders.md','.claude/rules/fin.md','ops/fbs_ms_import_policy.py','ops/fbs_ms_import_next5.py','collectors/oz_cogs_demand.py','core/ozon_posting.py','collectors/set_cost.py']
root=O.parent.parent.parent
for n in codepaths:sources.append({'path':n,'sha256':sha(root/n)})
manifest={'account':'oz_acc1','created_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'scope':read(O/'scope.json'),
 'source_files':sources,'previous_audit_used_files_unchanged':True,'archive_scope':'excluded, technical count only; mixed input files filtered to current product_id before analysis',
 'new_api_reads':{'purpose':'seven exact current missing compositions','endpoint':'TC /api/catalog/mix_data','codes':['2393','2128','6112','3257','3780','66281','66391'],'result':'HTTP503 each; no repeat','new_broad_collections':0},
 'database_access_this_stage':'none; saved read-only snapshots reused','business_mutations':0,'financial_profit_computed':False,
 'current_only_alias_count':len(current()[2]),'money_reconciliation':read(O/'allocation_reconciliation.json'),
 'validation_status':read(O/'validation.json')['status'],'owner_business_boundary_answers':'pending, not inferred from elapsed time or network approval'}
save('SOURCE_MANIFEST.json',manifest)
commands='''# Воспроизведение этапа

Все команды из `/opt/mp-analytics`. Выходы только в этом каталоге; первичный аудит и исходники года читаются по ссылкам. Архивные Ozon-карточки исключаются по положительному текущему universe до расчётов.

Офлайн-порядок (API, БД и внедрение не запускает):

```bash
./venv/bin/python docs/reports/ozon_family_strategy_20261106/inspect_evidence.py
./venv/bin/python docs/reports/ozon_family_strategy_20261106/extract_history.py
./venv/bin/python docs/reports/ozon_family_strategy_20261106/inspect_boundaries.py
./venv/bin/python docs/reports/ozon_family_strategy_20261106/resolve_physical.py
./venv/bin/python docs/reports/ozon_family_strategy_20261106/build_families.py
./venv/bin/python docs/reports/ozon_family_strategy_20261106/aggregate_families.py
./venv/bin/python docs/reports/ozon_family_strategy_20261106/classify_and_queue.py
./venv/bin/python docs/reports/ozon_family_strategy_20261106/validate.py
./venv/bin/python docs/reports/ozon_family_strategy_20261106/render_report.py
./venv/bin/python docs/reports/ozon_family_strategy_20261106/finalize.py
./venv/bin/python docs/reports/ozon_family_strategy_20261106/finalize.py verify
```

Отдельно уже выполнено `read_exact_compositions.py`: семь кодов зафиксированы в скрипте, только чтение mix_data. Первая попытка в песочнице дала ConnectionError; после разрешённого сетевого запуска получены семь HTTP503. Составы из этих ответов не выдумывались. Повторять запросы автоматически не требуется.

Для сокращения артефактов history_by_card хранит точные деньги/исходные ID заказов и первые даты CPC, а не копию всех 1,6 млн дневных рекламных строк. sales_lines сохраняет проверяемый ключ каждой выбранной товарной строки и её источник. Никакой новый годовой сбор не запускался.

Ответа на два вопроса о бизнес-границах не получено. Отсутствие owner_rules.json означает pending; подтверждение сетевого чтения не является принятием правил семей. Предложенные C-кандидаты, расширения и межсемейные составы не получают произвольных назначений.
'''
(O/'COMMANDS.md').write_text(commands)
files=sorted(p for p in O.rglob('*') if p.is_file() and p.name!='SHA256SUMS' and '__pycache__' not in p.parts and p.suffix not in ['.pyc','.tmp'])
(O/'SHA256SUMS').write_text(''.join(f'{sha(p)}  {p.relative_to(O)}\n' for p in files))
print(js({'output_files':len(files),'dependency_files':len(sources),'previous_audit_sources_unchanged':True,'business_writes':0}))
