# поток: ozon-family-strategy; one simple empirical proposal, no ad decisions.
from common import *
import collections
cards=list(rows(O/'cards.csv'));byid={c['product_id']:c for c in cards};fam=list(rows(O/'families.csv'));h=read(O/'history_by_card.json');defs=read(O/'family_definitions.json')
byfamily={f['family_id']:f for f in fam}
rule={'status':'working_proposal_not_objective_standard','main_measure':'unique noncancelled original order_id per family during 365 retained days',
 'long_tail_max_orders':3,'bestseller_min_orders':110,'middle':'4..109',
 'evidence':'369 positive families eligible for a full year: 131 have 1..3 orders; upper observed gap is 97 -> 118, choose 110 inside gap; no natural sharp low boundary',
 'regularity':'selling weeks/months are explanatory, not score or hidden threshold',
 'exclude_from_annual_class':['zero observed orders','less than 365 days evidence since creation/first positive event'],
 'all_classes_preliminary':'current family boundary and historical availability limit inference; no causal advertising conclusion'}
for f in fam:
 n=int(f['annual_unique_orders']);age=int(f['observed_age_days'])
 if n==0:f['demand_class']='нет наблюдаемых заказов';f['classification_status']='not_classified_as_tail; availability_and_ad_exposure_unknown'
 elif age<365:f['demand_class']='недостаточный срок наблюдения';f['classification_status']='not_classified_by_annual_volume'
 else:
  f['demand_class']='бестселлер' if n>=110 else 'длинный хвост' if n<=3 else 'обычный спрос'
  f['classification_status']='предварительно: наблюдаемый спрос oz_acc1; историческая доступность неизвестна'+('; карта неполна' if f['map_completeness'].startswith('incomplete') else '')
write('families.csv',fam);save('demand_rule.json',rule)
classorders=collections.defaultdict(set)
for x in rows(O/'sales_lines.csv'):
 fid=byid[x['product_id']]['family_id']
 if fid:classorders[byfamily[fid]['demand_class']].add(x['order_id'])
rec=read(O/'allocation_reconciliation.json');classes=[]
for label in ['бестселлер','обычный спрос','длинный хвост','нет наблюдаемых заказов','недостаточный срок наблюдения']:
 fs=[f for f in fam if f['demand_class']==label];value=sum(cents(f['ordered_value_rub']) for f in fs);cpc=sum(cents(f['cpc_spend_rub']) for f in fs)
 examples=sorted(fs,key=lambda f:int(f['annual_unique_orders']),reverse=True)[:3]
 classes.append({'demand_class':label,'families':len(fs),'unique_orders_in_class':len(classorders[label]),'ordered_value_rub':format(D(value)/100,'.2f'),'cpc_spend_rub':format(D(cpc)/100,'.2f'),
  'share_of_assigned_family_value':value/rec['assigned_family_ordered_value_cents'],'share_of_all_current_value':value/rec['selected_current_ordered_value_cents'],
  'examples':js([{'family_id':f['family_id'],'name':f['family_name'],'orders':f['annual_unique_orders'],'selling_weeks':f['selling_weeks'],'selling_calendar_month_segments':f['selling_months']} for f in examples]),
  'order_count_note':'distinct within class; orders spanning classes can overlap; money is additive'})
write('demand_classes.csv',classes)
# One primary clarification per unassigned card; sales/CPC union, never duplicate monetary priority.
models={x['external_code']:x for x in read(S/'db_prc_tc_model.json')};groups=collections.defaultdict(list)
for c in cards:
 if c['family_id']:continue
 q=c['question_group']
 if q=='Q-PHYSICAL':
  typ=models.get(c['code_anchor'],{}).get('consumable_type','')
  if int(c['bundle_multiplier'])>1:q='Q-PHYSICAL-BUNDLE'
  elif typ=='Фотобарабан + картридж':q='Q-PHYSICAL-MIXED-SEO'
  elif typ=='Сервисный комплект':q='Q-PHYSICAL-SERVICE'
  elif typ=='Заправочный комплект':q='Q-PHYSICAL-REFILL'
  elif typ=='Комплект фотобарабанов':q='Q-PHYSICAL-DRUMSET'
  elif typ=='Комплект картриджей':q='Q-PHYSICAL-CARTRIDGESET'
  else:q='Q-PHYSICAL-OTHER'
 groups[q].append(c)
questiontext={
 'Q-PARENT-BOUNDARY':('Физические родительские группы подтверждены, расширенная бизнес-граница не установлена.','Можно ли считать каждую такую группу самостоятельной семьёй до новых доказательств, либо какие конкретные серии должны объединяться?','Решение владельца об общем правиле; при объединении — явная таблица серий/родителей. Не просмотр всех карточек.'),
 'Q-PARENT-CANDIDATE':('Несколько разных точных родителей имеют одинаковые явную серию ТК, полную совместимость и роль; подтверждающего полного BOM нет.','Подтверждает ли владелец объединение этих вариантов ресурса/чипа/OEM в одну бизнес-семью?','Общее решение по правилу и исключениям; parent_family_candidates.csv содержит 180 групп с точными моделями и родителями. Пока это кандидаты, не назначения.'),
 'Q-MIXED-BOUNDARY':('Точный смешанный BOM содержит полную серию картриджей и барабан; у 8 связок совместимость дополнительно подтверждена.','Являются ли серия картриджей и подтверждённый барабан одной бизнес-семьёй? Для 0005/6629 и 4312/6642 показаны точные составы.','Ответ владельца на общее правило; для несогласованных профилей — актуальная спецификация совместимости.'),
 'Q-SHARED':('Физический компонент участвует в нескольких подтверждённых сериях.','К какой одной семье относить его продажи либо нужно ли объединение по более сильному основанию?','Явное бизнес-решение о границах, не автоматическая цепочка общих компонентов.'),
 'Q-INTERFAMILY':('Подтверждённый состав затрагивает несколько разных установленных серий.','Это межсемейный набор или одна более широкая семья? Как учитывать одну продажу такого набора?','Ответ владельца по составам из очереди; до этого сумма остаётся один раз в нераспределённом остатке.'),
 'Q-COMPOSITION-BOUNDARY':('Физический состав известен; единая серия/полный профиль не подтверждены всеми полями.','Какие перечисленные составы образуют одну бизнес-семью и где проходят её границы?','Явная серия производителя/ТК с родителями и совместимостью, либо решение знающего сотрудника.'),
 'Q-PHYSICAL-BUNDLE':('Кратность XN известна; физическое исполнение производного артикула не подтверждено.','Что именно умножается на XN: одиночный товар или подтверждённый набор?','Точная спецификация полного артикула и пример заказ→позиции МС с количествами.'),
 'Q-PHYSICAL-MIXED-SEO':('Кодовый якорь указывает на смешанный набор, но child-BOM неизвестен.','Содержат ли 66281/66391 и аналогичные дочки весь набор якоря либо иное исполнение?','Спецификация именно дочки или документ фактической комплектации; префикс недостаточен.'),
 'Q-PHYSICAL-SERVICE':('Назван сервисный комплект, точные складские компоненты отсутствуют.','Каков точный состав и количества?','Спецификация сервиса производителя/ТК и точные externalCode компонентов.'),
 'Q-PHYSICAL-REFILL':('Заправочные комплекты не равны бандлам и не имеют подтверждённого полного BOM.','Каковы компоненты, единицы измерения и количества?','ТК/MS спецификация, включая тонер, чип и дополнительные части.'),
 'Q-PHYSICAL-DRUMSET':('Набор фотобарабанов заявлен, состав не восстановлен по точным кодам.','Каковы барабаны и количества в каждом полном артикуле?','BOM ТК/MS либо документ комплектации.'),
 'Q-PHYSICAL-CARTRIDGESET':('Есть признаки разных компонентов; физический состав нельзя заменить чёрным якорем.','Каков точный состав, включая бумагу/ленту у сублимационных позиций?','Точная спецификация полного артикула и складские коды; адресный TC mix_data вернул HTTP503 для проверенных примеров.'),
 'Q-PHYSICAL-OTHER':('Имеющиеся атрибуты не подтверждают обычную одиночную связь без противоречия.','Какой физический товар соответствует полному артикулу?','Точная спецификация/assembly mapping или проверенная строка отгрузки МС.'),
}
queue=[];members=[]
for q,cs in groups.items():
 value=sum(h[c['product_id']]['sales_cents'] for c in cs);cpc=sum(h[c['product_id']]['cpc_cents'] for c in cs)
 active=[c for c in cs if h[c['product_id']]['orders'] or h[c['product_id']]['cpc_cents']>0]
 sortedcs=sorted(cs,key=lambda c:(h[c['product_id']]['cpc_cents'],h[c['product_id']]['sales_cents']),reverse=True)
 known,question,needed=questiontext[q]
 candidates=sorted({fid for c in cs for fid in json.loads(c['candidate_family_ids'])})
 queue.append({'question_id':q,'cards_affected':len(cs),'cards_with_orders_or_cpc':len(active),'parent_codes':len({c['physical_parent_code'] or c['code_anchor'] for c in cs}),
  'ordered_value_rub':format(D(value)/100,'.2f'),'cpc_spend_rub':format(D(cpc)/100,'.2f'),'cpc_without_orders_rub':format(D(sum(h[c['product_id']]['cpc_cents'] for c in cs if not h[c['product_id']]['orders']))/100,'.2f'),
  'known':known,'unknown_question':question,'required_fact_or_document':needed,'candidate_family_count':len(candidates),'candidate_family_ids':js(candidates),
  'examples':js([{'offer_id':c['offer_id'],'physical_parent_code':c['physical_parent_code'],'candidates':json.loads(c['candidate_family_ids'])} for c in sortedcs[:8]]),
  'unblocks':'one exact physical/family assignment per listed card; monetary amounts stay unassigned until resolved',
  'status':'open','sources_checked':'prior snapshots, exact BOM/TC models, current parent+child attributes, current order/CPC key union; targeted_reads for seven named gaps',
  'priority_basis':'descending CPC spend, then ordered value; not a demand score'})
 for c in cs:members.append({'question_id':q,'product_id':c['product_id'],'offer_id':c['offer_id'],'code_anchor':c['code_anchor'],'physical_parent_code':c['physical_parent_code'],
  'candidate_family_ids':c['candidate_family_ids'],'ordered_value_rub':c['annual_ordered_value_rub'],'cpc_spend_rub':c['annual_cpc_spend_rub']})
queue.sort(key=lambda q:(cents(q['cpc_spend_rub']),cents(q['ordered_value_rub'])),reverse=True)
for i,q in enumerate(queue,1):q['priority_rank']=i
for q in queue:q['question_scope']='primary_map_assignment_disjoint_money'
cost_cards=[c for c in cards if c['physical_parent_code'] and c['warehouse_ms_matches']!='1']
costq={k:'' for k in queue[0]};costq.update({'question_id':'Q-COST-EXECUTION','cards_affected':len(cost_cards),'cards_with_orders_or_cpc':sum(bool(h[c['product_id']]['orders']) or h[c['product_id']]['cpc_cents']>0 for c in cost_cards),
 'parent_codes':len({c['physical_parent_code'] for c in cost_cards}),'known':'Точный внешний код имеет несколько поставщицких MS ID либо не найден в живом справочнике. Это не разрешает выбрать первый ID или сложить себестоимости.',
 'unknown_question':'Для будущей экономики нужен доказанный мост отправление→отгрузка→позиции и фактический MS ID; где получить отсутствующие строки этого моста и недостающие финансовые периоды?',
 'required_fact_or_document':'Позиции реальной отгрузки/склад заказа, FIFO этой отгрузки, сторно той же отгрузки. Прочитаны CUSTOMER_ORDER_RULES, fbs_ms_import_policy, oz_cogs_demand; fallback не равен факту.',
 'examples':js([{'offer_id':c['offer_id'],'parent':c['physical_parent_code'],'live_ms_matches':c['warehouse_ms_matches']} for c in sorted(cost_cards,key=lambda c:h[c['product_id']]['sales_cents'],reverse=True)[:5]]),
 'unblocks':'future COGS/economics; does not block physical family relationship or observed demand','status':'open_for_future_economics',
 'sources_checked':'prior ms_product, existing documented fulfillment/FIFO selection rules; no choosing/summing alternative IDs','priority_basis':'separate cross-cutting requirement; monetary priority deliberately blank to avoid double count','priority_rank':len(queue)+1,
 'question_scope':'crosscut_accounting_no_money_allocation'})
queue.append(costq)
write('clarification_queue.csv',queue);write('clarification_members.csv',members)
assert len(members)==len({x['product_id'] for x in members})==sum(not c['family_id'] for c in cards)
assert sum(cents(q['ordered_value_rub']) for q in queue)==rec['unassigned_ordered_value_cents']
assert sum(cents(q['cpc_spend_rub']) for q in queue)==rec['unassigned_cpc_cents']
# Provisional parent-group demand is available without pretending it is a finalized family.
pg=collections.defaultdict(list)
for c in cards:
 if c['family_basis']=='parent_group_only_not_final_big_family':pg[c['physical_parent_code']].append(c)
provisional=[]
for code,cs in pg.items():
 orders={oid:day for c in cs for oid,day in h[c['product_id']]['orders'].items()}
 provisional.append({'parent_group_id':'PG:'+code,'parent_model':models.get(code,{}).get('title',''),'current_cards':len(cs),'family_status':'requires_clarification_not_final_big_family',
  'annual_unique_orders':len(orders),'selling_weeks':len({datetime.date.fromisoformat(d).isocalendar()[:2] for d in orders.values()}),
  'ordered_value_rub':format(D(sum(h[c['product_id']]['sales_cents'] for c in cs))/100,'.2f'),'cpc_spend_rub':format(D(sum(h[c['product_id']]['cpc_cents'] for c in cs))/100,'.2f'),
  'demand_class':'не присваивается до границы семьи','candidate_family_ids':cs[0]['candidate_family_ids'],'question_id':cs[0]['question_group']})
write('parent_groups_pending.csv',provisional)
save('classification_summary.json',{'rule':rule,'classes':classes,'question_groups':len(queue),'question_cards':len(members),'provisional_parent_groups':len(provisional),
 'queue_covers_unassigned_money_exactly':True,'archive_analysis_performed':False})
print(js({'classes':[{k:x[k] for k in ['demand_class','families','ordered_value_rub','share_of_assigned_family_value']} for x in classes],'questions':len(queue),'provisional_parent_groups':len(provisional)}))
