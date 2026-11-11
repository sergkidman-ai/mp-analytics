# поток: mkt
"""Offline addendum using preserved analysis and read-only current CPC membership snapshots."""
import pathlib,json,csv,decimal,collections,datetime as dt,hashlib
O=pathlib.Path(__file__).parent;R=O/'addendum_raw';D=decimal.Decimal
POST=['2026-10-31','2026-11-01','2026-11-02']
def read(n):
 with (O/n).open() as f:return list(csv.DictReader(f))
def load(n):return json.loads((O/n).read_text())
def save(n,x):(O/n).write_text(json.dumps(x,ensure_ascii=False,indent=2,default=str)+'\n')
def write(n,rs):
 with (O/n).open('w',newline='') as f:
  w=csv.DictWriter(f,fieldnames=list(rs[0]));w.writeheader();w.writerows(rs)
def num(x):return D(str(x)) if x not in ['',None] else None
def pct(a,b):return a/b*D(100) if b else None
def fmt(x,n=2):return 'нет данных' if x in ['',None] else f'{D(str(x)):,.{n}f}'.replace(',',' ').replace('.',',')
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def date(x):
 for fmt0 in ['%Y-%m-%d','%d.%m.%Y']:
  try:return dt.datetime.strptime(x,fmt0).date().isoformat()
  except ValueError:pass
 raise ValueError(x)
sales=read('revised_sales_windows.csv');sx={(r['scope'],int(r['baseline_days'])):r for r in sales};ads=read('revised_ad_summary.csv');ad={(r['scope'],int(r['baseline_days'])):r for r in ads}
# One requested table: actual post total, weekday expectations and all deltas.
combined=[]
for metric,label,scope,field,unit in [
 ('ad_spend','Рекламный расход самих 20 SKU','all_available','spend_rub','RUB'),
 ('sku_orders','Общие уникальные заказы самих 20 SKU','fixed20_exact_sku','orders','orders'),
 ('sku_value','Стоимость заказанного самих 20 SKU','fixed20_exact_sku','ordered_value_rub','RUB'),
 ('family_orders','Уникальные заказы объединения семейств','families_union_current','orders','orders'),
 ('family_value','Стоимость заказанного объединения семейств','families_union_current','ordered_value_rub','RUB')]:
  out={'metric':metric,'label':label,'unit':unit,'post_from_msk':POST[0],'post_to_msk':POST[-1],'post_full_days':3}
  for n in [14,28]:
   r=ad[(scope,n)] if metric=='ad_spend' else sx[(scope,n)]
   actual=num(r['post_'+field] if metric=='ad_spend' else r['post_total_'+field]);expect=num(r['expected_'+field] if metric=='ad_spend' else r['expected_post_'+field])
   out['actual']=actual;out[f'expected_base{n}']=expect;out[f'delta_base{n}']=actual-expect;out[f'delta_percent_base{n}']=pct(actual-expect,expect)
  out['definition']='unique order_id in noncancelled posting lines; same rule before/after' if unit=='orders' else ('sum of unique available CPC SKU×campaign×day rows' if metric=='ad_spend' else 'sum product Money RUB × quantity; family union by line identity; not realized financial revenue')
  combined.append(out)
write('addendum_combined_comparison.csv',combined)
# Family decomposition uses the SAME current model_id set for every date and both baselines.
members=read('revised_family_members.csv');families=collections.defaultdict(list)
for x in members:families[x['current_model_id']].append(x)
items={};versions=collections.defaultdict(list)
for p in [O/'revision_raw/product_info_targets_candidate.json']+sorted((O/'revision_raw').glob('product_info_sold_*.json')):
 z=json.loads(p.read_text())
 for it in z['body'].get('items',[]):
  pid=str(it['id']);items[pid]=it;versions[pid].append({'model_id':str(it['model_info']['model_id']),'source':str(p.relative_to(O)),'retrieved_at_utc':z['retrieved_at_utc']})
source_model_disagreement=[pid for pid,vs in versions.items() if len({v['model_id'] for v in vs})>1]
assert not source_model_disagreement
lines=read('revised_sales_lines.csv');family_evidence=[];familyrows=[]
for model,mm in sorted(families.items()):
 target=mm[0]['target_skus_sharing_family'].split(';')[0]
 famitems=[items[m['product_id']] for m in mm]
 nostock=[str(x['sku']) for x in famitems if not x.get('stocks',{}).get('has_stock')]
 notavailable=[str(x['sku']) for x in famitems if not any(v.get('availability')=='AVAILABLE' for v in x.get('availabilities',[]))]
 for m,x in zip(mm,famitems):
  family_evidence.append({'model_id':model,'target_skus':m['target_skus_sharing_family'],'member_sku':x['sku'],'offer_id':x['offer_id'],'name':x['name'],'has_stock_current':x.get('stocks',{}).get('has_stock'),'any_source_AVAILABLE_current':any(v.get('availability')=='AVAILABLE' for v in x.get('availabilities',[])),'stock_by_source':json.dumps(x.get('stocks',{}).get('stocks',[]),ensure_ascii=False),'availability_by_source':json.dumps(x.get('availabilities',[]),ensure_ascii=False),'metadata_sources':json.dumps(versions[str(x['id'])],ensure_ascii=False),'historical_availability':'не восстановлена','historical_membership':'не восстановлен; одна текущая модель применена ко всем датам'})
 for n in [14,28]:
  rr=sx[('family_'+target,n)]
  row={'model_id':model,'target_skus':mm[0]['target_skus_sharing_family'],'representative_article':items[mm[0]['product_id']]['offer_id'],'representative_name':items[mm[0]['product_id']]['name'],'baseline_days':n,'known_member_products':len(mm),'api_reported_member_count':famitems[0]['model_info']['count'],'current_no_stock_known_skus':';'.join(nostock),'current_no_available_source_known_skus':';'.join(notavailable),'historical_stock_issue_confirmed':False,'historical_stock_status':'нет данных; текущий остаток не доказывает прошлый','historical_membership_changes':'не установлены; история отсутствует','membership_basis':'same current model_id set applied to baseline/post','noncancelled_sales_feed_coverage':'complete baseline and post FBS/FBO; all sold SKU mapped to current model_id'}
  for metric in ['orders','postings','units','ordered_value_rub']:
   for prefix in ['post_total_','expected_post_','delta_','delta_percent_']:row[prefix+metric]=num(rr[prefix+metric])
  familyrows.append(row)
write('addendum_family_decomposition.csv',familyrows);write('addendum_family_stock_evidence.csv',family_evidence)
# Costs are additive across distinct model_id groups; distinct order counts are not additive.
for n in [14,28]:
 delta=sum(r['delta_ordered_value_rub'] for r in familyrows if r['baseline_days']==n)
 assert delta==num(sx[('families_union_current',n)]['delta_ordered_value_rub'])
family_summary=[]
for n in [14,28]:
 ranked=sorted([r for r in familyrows if r['baseline_days']==n],key=lambda r:r['delta_ordered_value_rub'])
 five=ranked[:5];fall=sum(r['delta_ordered_value_rub'] for r in five);net=num(sx[('families_union_current',n)]['delta_ordered_value_rub'])
 family_summary.append({'baseline_days':n,'top5_model_ids':[r['model_id'] for r in five],'top5_value_delta_rub':fall,'other_families_net_delta_rub':net-fall,'all_families_delta_rub':net,'explanation':'arithmetic decomposition, not causal attribution','historical_stock_or_composition_cause_verified':False})
save('addendum_family_summary.json',family_summary)
# Exact identifiers, provenance and fingerprints; separate from unavailable immutable manifest.
prepared=read('revised_prepared_composition_2953.csv');assert len(prepared)==len({r['sku'] for r in prepared})==2953
stream={r['sku']:r['stream'] for r in prepared};provenance=[]
for cid,label,count in [('49173478','new',458),('49173499','bundles',1425),('49173502','old',1070)]:
 source=O/f'revision_raw/products_refresh_{cid}.json';z=json.loads(source.read_text());ss={str(x['sku']) for x in z['products']};assert ss=={s for s,v in stream.items() if v==label} and len(ss)==count
 original=O/f'raw/products_{cid}.json';zz=json.loads(original.read_text());assert ss=={str(x['sku']) for x in zz['products']}
 provenance.append({'stream':label,'campaign_id':cid,'unique_sku_count':len(ss),'source':str(source.resolve()),'api_method':f'GET /api/client/campaign/{cid}/v2/products','retrieved_at_utc':z['retrieved_at_utc'],'sha256':sha(source),'original_snapshot_source':str(original.resolve()),'original_retrieved_at_utc':zz['retrieved_at_utc'],'original_sha256':sha(original),'identifier_sets_match':True,'classification_source':'owner-confirmed preparation campaign → stream mapping'})
listpath=O/'revised_prepared_composition_2953.csv'
prov={'identifier_file':str(listpath.resolve()),'identifier_file_sha256':sha(listpath),'identifier_rows':2953,'unique_sku_ids':2953,'source_scope':'actual API compositions of three preparation campaigns on new VPS, not the original immutable planning file','source_snapshots':provenance,'original_2963_manifest_available':False,'original_2963_given_sha256':'54f332222592045527346f050d331d065c601ec1adec90a19b9b1e9fb95325c2','match_to_original_immutable_manifest_verified':False,'unrecovered_original_part':'10 identifiers excluded before prepared 2953, their stream and exclusion reasons; original exact file/order/format','individual_floor_compliance_verified':False}
save('addendum_prepared_provenance.json',prov)
# Full prepared list vs fresh accessible CPC composition, with exact active coverage and honest archive bounds.
campaignsnap=load('addendum_raw/campaigns_current.json');cs=campaignsnap['body']['list'];meta={str(c['id']):c for c in cs}
coverage=load('addendum_raw/coverage_current.json');activeids={str(c['id']) for c in cs if c.get('PaymentType')=='CPC' and c['state']=='CAMPAIGN_STATE_RUNNING'}
coverage_byid={r['campaign_id']:r for r in coverage};active_complete=all(coverage_byid[c]['complete'] for c in activeids)
hits=collections.defaultdict(list)
for p in sorted(R.glob('products_current_*.json')):
 z=json.loads(p.read_text());cid=z['campaign_id']
 for x in z.get('products',[]):
  s=str(x['sku'])
  if s in stream:hits[s].append(cid)
memberrows=[]
for s,label in sorted(stream.items()):
 cids=sorted(set(hits[s]));running=[c for c in cids if meta[c]['state']=='CAMPAIGN_STATE_RUNNING'];other=[c for c in cids if c not in running]
 status='active_campaign_membership' if running else ('inactive_only_confirmed' if cids and active_complete else ('not_found_in_accessible_compositions' if not cids else 'unknown_due_to_active_coverage'))
 memberrows.append({'sku':s,'stream':label,'status':status,'running_campaign_ids':';'.join(running),'nonrunning_campaign_ids':';'.join(other),'known_campaign_count':len(cids),'multiple_accessible_campaigns':len(cids)>1,'multiple_running_campaigns':len(running)>1,'classification_basis':'recovered preparation campaign composition + owner mapping','running_cpc_coverage_complete':active_complete,'archive_membership_complete':False,'count_bound':'known campaign count is a lower bound; inaccessible archives may add inactive placements','snapshot_utc':campaignsnap['retrieved_at_utc']})
write('addendum_prepared_current_membership.csv',memberrows)
summ=[]
for label in ['new','bundles','old','ALL']:
 rr=[r for r in memberrows if label=='ALL' or r['stream']==label]
 summ.append({'stream':label,'recovered_unique_skus':len(rr),'in_running_cpc_campaigns':sum(r['status']=='active_campaign_membership' for r in rr),'only_nonrunning_cpc_campaigns':sum(r['status']=='inactive_only_confirmed' for r in rr),'not_found_in_accessible_compositions':sum(r['status']=='not_found_in_accessible_compositions' for r in rr),'multiple_accessible_cpc_campaigns_minimum':sum(r['multiple_accessible_campaigns'] for r in rr),'multiple_running_cpc_campaigns':sum(r['multiple_running_campaigns'] for r in rr),'all_running_cpc_campaigns_complete':active_complete,'multi_campaign_count_exact_including_archives':False})
write('addendum_prepared_current_summary.csv',summ)
write('addendum_current_campaign_coverage.csv',[{k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v for k,v in {'campaign_id':r['campaign_id'],'state':r['state'],'type':r['type'],'complete':r['complete'],'http':r.get('http'),'reason':r.get('reason'),'source':f"addendum_raw/products_current_{r['campaign_id']}.json"}.items()} for r in coverage])
# Reuse all original Nov 1-3 JSON statistics: no broad new statistics request.
statindex={};stat_sources=[]
for p in sorted((O/'raw').glob('sku_report_2026-11-01_2026-11-03_*_result.json')):
 z=json.loads(p.read_text());stat_sources.append(str(p.relative_to(O)))
 for cid,block in z['body'].items():
  if not isinstance(block,dict):continue
  for r in block.get('report',{}).get('rows',[]) or []:
   s=str(r.get('sku'))
   if s not in stream or date(r['date'])!='2026-11-02':continue
   key=(s,str(cid),'2026-11-02');assert key not in statindex;statindex[key]=r
stats=[]
for label in ['new','bundles','old','ALL']:
 ids={s for s,t in stream.items() if label=='ALL' or t==label};rr=[r for (s,c,d),r in statindex.items() if s in ids];observed={s for s,c,d in statindex if s in ids}
 stats.append({'scope':label,'date_msk':'2026-11-02','recovered_skus':len(ids),'skus_with_explicit_statistics':len(observed),'skus_without_statistics':len(ids-observed),'returned_sku_campaign_rows':len(rr),'views':sum(D(str(r.get('views',0))) for r in rr) if rr else None,'spend_rub':sum(D(str(r['moneySpent']).replace(',','.').replace(' ','')) for r in rr) if rr else None,'status':'explicit returned rows only' if rr else 'нет данных; пустой ряд не считается нулём','wave_launch_proof':False,'sources':';'.join(stat_sources)})
# Candidate is a separately observed row, never added to recovered prepared list or assigned to new.
cd=next(r for r in read('campaign_daily.csv') if r['campaign_id']=='49321871' and r['date_msk']=='2026-11-02')
stats.append({'scope':'candidate_49321871_OUTSIDE_recovered_list','date_msk':'2026-11-02','recovered_skus':None,'skus_with_explicit_statistics':1,'skus_without_statistics':None,'returned_sku_campaign_rows':1,'views':D(cd['views']),'spend_rub':D(cd['spend_rub']),'status':'отдельный кандидат с 1 SKU 6201364326; неполный день, вне 2953','wave_launch_proof':False,'sources':cd['source']})
write('addendum_prepared_20261102_metrics.csv',stats)
# Human report.
now=dt.datetime.now(dt.timezone.utc).isoformat();overall=summ[-1]
L=['# Дополнение: 20 SKU, причины семейного изменения и источник 2953 SKU','',f'oz_acc1; расчёт дополнения {now}. Продажи и рекламная статистика — сохранённый срез за 31.10–02.11.2026; текущие составы CPC обновлены отдельным чтением API. Реклама, ставки, составы и расписания не менялись.','',
'## 1. Одна таблица расходов и общих продаж','',
'Пост-период — **3 полных дня 31.10–02.11 МСК**. База 14 дней: 16–29.10; база 28 дней: 02–29.10. 30.10 исключён. Для каждой субботы/воскресенья/понедельника поста ожидание — среднее соответственно 2 или 4 таких же дней недели базы; ожидания трёх дней суммируются. Δ = факт − ожидание; % = 100 × Δ / ожидание.','',
'| Показатель | Факт за 3 дня | Ожидание 14 дней | Δ 14, ₽/заказов | Δ 14, % | Ожидание 28 дней | Δ 28, ₽/заказов | Δ 28, % |','|---|---:|---:|---:|---:|---:|---:|---:|']
for r in combined:L.append('| '+' | '.join([r['label']+(' (₽)' if r['unit']=='RUB' else ' (шт.)'),fmt(r['actual'],0 if r['unit']=='orders' else 2),fmt(r['expected_base14']),fmt(r['delta_base14']),fmt(r['delta_percent_base14']),fmt(r['expected_base28']),fmt(r['delta_base28']),fmt(r['delta_percent_base28'])])+' |')
L+=['',
'**Заказ здесь = уникальный `order_id` среди неотменённых отправлений, содержащих товар выбранного SKU/объединения семейств.** Правило одинаково до и после. Разбивка одного заказа на несколько отправлений не увеличивает число заказов; несколько подходящих строк в одном заказе также не увеличивают его. Отправления (`scheme + posting_number`) и товарные единицы сохранены отдельно. В выбранных данных order_id не пересекаются между датами. Отмена оценивается на момент получения ленты.','',
'Стоимость заказанного — сумма `products.price` в RUB × quantity по фактическим товарным строкам, не финансовая выручка реализации. Строка считается один раз по схеме, posting_number и позиции. Рекламный расход — сумма уникальных доступных CPC-строк SKU × кампания × день; CPO по SKU не распределялся. Общие заказы не подменены метрикой рекламных товаров orders.','',
'Для обеих сторон сравнения применяется один и тот же **текущий набор 19 model_id Seller API**, связанный с фиксированными 20 SKU. Общая группа SKU 866442961 и 652626682 учитывается в объединении один раз. Все SKU с полученными неотменёнными продажами кабинета классифицированы по этому текущему снимку. Исторический состав семей не восстановлен: смена группировки между прошлым и настоящим остаётся ограничением.','',
'### Какие семейства дали падение','',
'Ниже пять крупнейших отрицательных вкладов по 28-дневной базе; колонка 14 дней показывает те же семейства при другой базе. Группы заданы `model_id`, целевой SKU — ориентир для владельца. Полная декомпозиция всех 19 групп и обеих баз сохранена отдельно.','',
'| model_id / целевой SKU | Стоимость за 3 дня, ₽ | Ожидание 28, ₽ | Δ 28, ₽ | Δ 28, % | Заказы: факт / ожидание 28 | Δ стоимости по базе 14, ₽ |','|---|---:|---:|---:|---:|---:|---:|']
top=sorted([r for r in familyrows if r['baseline_days']==28],key=lambda r:r['delta_ordered_value_rub'])[:5]
famidx={(r['model_id'],r['baseline_days']):r for r in familyrows}
for r in top:L.append('| '+' | '.join([r['model_id']+' / '+r['target_skus'],fmt(r['post_total_ordered_value_rub']),fmt(r['expected_post_ordered_value_rub']),fmt(r['delta_ordered_value_rub']),fmt(r['delta_percent_ordered_value_rub']),fmt(r['post_total_orders'],0)+' / '+fmt(r['expected_post_orders']),fmt(famidx[(r['model_id'],14)]['delta_ordered_value_rub'])])+' |')
s28=next(x for x in family_summary if x['baseline_days']==28);s14=next(x for x in family_summary if x['baseline_days']==14)
L+=['',f'Эти пять групп дали **{fmt(s28["top5_value_delta_rub"])} ₽**, остальные группы вместе **{fmt(s28["other_families_net_delta_rub"])} ₽**, итог объединения — **{fmt(s28["all_families_delta_rub"])} ₽**. У групп 2804580775, 367856200 и 458952779 в пост-периоде нет неотменённых заказов; две другие дали меньше стоимости, чем расчётное ожидание. Это арифметическое объяснение изменения, не установление его причины. Числа заказов отдельных семейств нельзя складывать для общего итога: один заказ может содержать товары разных семейств.',
'',f'Для 14-дневной базы пять наибольших падений: '+', '.join(x['target_skus'] for x in sorted([r for r in familyrows if r['baseline_days']==14],key=lambda r:r['delta_ordered_value_rub'])[:5])+f'; их суммарный вклад {fmt(s14["top5_value_delta_rub"])} ₽, остальных {fmt(s14["other_families_net_delta_rub"])} ₽. Полная таблица содержит model_id и суммы по обеим базам.','',
'**Наличие:** в сохранённом текущем снимке 03.11 у всех известных товаров пяти основных падающих групп есть stock.has_stock=true и хотя бы один источник AVAILABLE. Это относится к известным целевым/продававшимся товарам (по группам 1/2/3/2/3 продукта), а не доказательство полной доступности всех вариантов семьи в прошлом. Исторических остатков 31.10–02.11 нет; прошлые stock-out, ограничения доставки и временную недоступность подтвердить или исключить нельзя. Текущий present/reserved не превращён в исторический остаток.','',
'**Состав и полнота:** текущая модель применена одинаково к базе и посту, а не меняется между окнами. Между доступными снимками метаданных 03.11 расхождений model_id для одного product_id не обнаружено; это не история состава за октябрь. Ленты FBS/FBO выбранных окон выгружены полностью; непривязанных к текущей модели продававшихся SKU нет. Отсутствующие варианты без продаж не добавляют неучтённую стоимость в полученную полную ленту. Однако точный исторический состав, поздние отмены и короткий трёхдневный пост ограничивают вывод. Доказательств, что падение вызвано отсутствием товара, изменением состава или ставкой, нет.','',
'## 2. Откуда восстановлены 2953 идентификатора','',
'Сохранены **сами 2953 уникальных SKU**, не только количества. Точная формулировка: «восстановлен фактический SKU-состав трёх подготовительных кампаний по API-снимкам нового VPS на 03.11». Это не подтверждение соответствия исходному immutable manifest 2963 SKU и не доказательство запуска волны.','',
f'Файл списка: `{listpath.resolve()}`. SHA256: `{prov["identifier_file_sha256"]}`. Строк/уникальных SKU: **2953/2953**. Источник — GET `/api/client/campaign/{{id}}/v2/products`, только oz_acc1. Классификация потоков — заданное владельцем соответствие кампаний new/bundles/old, проверенное на фактических идентификаторах; пересечений потоков нет.','',
'| Поток | Кампания | ID восстановлено | Время API-снимка, UTC | Файл исходного ответа | SHA256 исходного файла |','|---|---|---:|---|---|---|']
for r in provenance:L.append('| '+' | '.join([r['stream'],r['campaign_id'],str(r['unique_sku_count']),r['retrieved_at_utc'],r['source'].replace(str(O.resolve())+'/',''),r['sha256']])+' |')
L+=['',
'Наборы ID совпадают также с первоначальными снимками `raw/products_49173478.json`, `raw/products_49173499.json`, `raw/products_49173502.json` от 03.11 около 08:52 UTC; точные даты и контрольные суммы обеих версий — в provenance JSON. Сохранённые ID сопоставлены со свежими составами ниже. Исходный файл **2963 SKU** с сообщённым SHA256 `54f332222592045527346f050d331d065c601ec1adec90a19b9b1e9fb95325c2` отсутствует. Поэтому 10 исключённых SKU, их категории/причины, исходный порядок и формат файла не восстановлены; совпадение с этим хешем не объявляется. Старый VPS и Beget не использовались.','',
'### Весь восстановленный список против текущих CPC-кампаний','',
f'Снимок каталога кампаний: **{campaignsnap["retrieved_at_utc"]} UTC**. «Активная реклама» в этой сверке означает членство в CPC-кампании со статусом RUNNING; это не утверждение о показах каждого товара. Все **{len(activeids)} RUNNING CPC-кампаний** успешно прочитаны.','',
'| Поток | SKU в списке | В RUNNING CPC | Только в неактивных CPC | Не найдено | В нескольких доступных CPC, минимум |','|---|---:|---:|---:|---:|---:|']
for r in summ:L.append('| '+' | '.join([r['stream'],str(r['recovered_unique_skus']),str(r['in_running_cpc_campaigns']),str(r['only_nonrunning_cpc_campaigns']),str(r['not_found_in_accessible_compositions']),str(r['multiple_accessible_cpc_campaigns_minimum'])])+' |')
L+=['',
'Все 2953 найдены как минимум в своих подготовительных INACTIVE-кампаниях. Поэтому «только неактивные» и «не найдено» определены без подстановок. Одновременное членство в доступных составах нескольких кампаний выявлено у **4 SKU**, все из new:','',
'| SKU | Кампании | Текущая работа |','|---|---|---|']
for r in memberrows:
 if r['multiple_accessible_campaigns']:L.append('| '+' | '.join([r['sku'],r['nonrunning_campaign_ids'],'все наблюдаемые размещения неактивны'])+' |')
L+=['',
'Количество «в нескольких» — **нижняя граница по доступным составам**, не точное число с учётом архивов: состав 9 архивных SKU-кампаний недоступен (прежний HTTP 400, статус по свежему каталогу остаётся ARCHIVED). В активных кампаниях неопределённости нет: все их составы прочитаны, целевых 2953 там нет. Не-SKU CPC-баннер также архивный и не имеет применимого SKU-состава. Скрытые архивные членства могут увеличить только число неактивных размещений/дублей, но не изменить подтверждённый ноль RUNNING-пересечений.','',
'### Доступные фактические показы и расход за 2 ноября','',
'| Набор | SKU с явными строками статистики | Показы | Расход, ₽ |','|---|---:|---:|---:|']
for r in stats:L.append('| '+' | '.join([r['scope'],str(r['skus_with_explicit_statistics']),fmt(r['views'],0),fmt(r['spend_rub'])])+' |')
L+=['',
'Для восстановленного списка 2953 за 02.11 **нет ни одной явной SKU-строки** в ранее полученных дневных отчётах всех 31 SKU-кампаний кабинета: показатели помечены «нет данных», а не нулём. Подготовительные кампании не дали и кампанийных дневных строк. Повторную полную выгрузку статистики для этого дополнения не заказывали.','',
'49321871 показана отдельно: 20 показов и 0 ₽ относятся к SKU 6201364326 **вне** восстановленных 2953, день создания неполный. Наличие четырёх подготовленных new SKU в старых INACTIVE-кампаниях не считается доказательством запуска новой волны. Текущий ноль в RUNNING не доказывает, что волна никогда не активировалась; точная история событий отсутствует.','',
'## Сохранённые файлы','',
'- [addendum_combined_comparison.csv](addendum_combined_comparison.csv) — одна таблица факта, ожиданий и отклонений.',
'- [addendum_family_decomposition.csv](addendum_family_decomposition.csv), [addendum_family_summary.json](addendum_family_summary.json), [addendum_family_stock_evidence.csv](addendum_family_stock_evidence.csv) — разложение по 19 семействам и наличию.',
'- [addendum_prepared_provenance.json](addendum_prepared_provenance.json) — точные источники, даты и SHA256 списка и сырых ответов.',
'- [addendum_prepared_current_membership.csv](addendum_prepared_current_membership.csv), [addendum_prepared_current_summary.csv](addendum_prepared_current_summary.csv), [addendum_current_campaign_coverage.csv](addendum_current_campaign_coverage.csv) — сверка всех ID с текущими CPC.',
'- [addendum_prepared_20261102_metrics.csv](addendum_prepared_20261102_metrics.csv) — доступность статистики 02.11; `addendum_raw/` — свежие чтения составов.',
'- [addendum_validation.json](addendum_validation.json), `ADDENDUM_SHA256SUMS` — проверки и контрольные суммы. Все файлы: `/opt/mp-analytics/docs/reports/ozon_recovery_20261103/`. Исходные выгрузки и прежние отчёты сохранены без изменения.','']
(O/'ADDENDUM_20SKU_AND_2953.md').write_text('\n'.join(L))
save('addendum_summary.json',{'created_at_utc':now,'post_days':POST,'family_current_groups':19,'active_cpc_count_read':len(activeids),'active_cpc_coverage_complete':active_complete,'current_prepared_counts':overall,'prepared_list_sha256':prov['identifier_file_sha256'],'family_decomposition':family_summary,'prepared_stat_rows_nov2':len(statindex),'cause_attributed':False,'marketplace_mutations':0,'db_data_writes':0})
print('Combined comparison:',[(r['metric'],str(r['actual']),str(r['delta_base28']),str(r['delta_percent_base28'])) for r in combined])
print('Family top 5:',[(r['model_id'],r['target_skus'],str(r['delta_ordered_value_rub'])) for r in top])
print('Prepared provenance SHA256:',prov['identifier_file_sha256'])
print('Current counts:',overall)
print('Prepared Nov2 explicit rows:',len(statindex))
