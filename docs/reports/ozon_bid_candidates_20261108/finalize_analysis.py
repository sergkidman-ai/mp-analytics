# поток: mkt / ozon
"""Rank saved read-only evidence and render a non-executable bid proposal."""
import csv,json,datetime as dt,html,collections
from decimal import Decimal as D,ROUND_HALF_UP
from pathlib import Path
O=Path(__file__).parent;P=O.parent;L=P/'ozon_launch_20261106';read=lambda f:json.loads(Path(f).read_text());csvread=lambda f:list(csv.DictReader(Path(f).open(encoding='utf-8-sig')));save=lambda n,x:(O/n).write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n')
fmt=lambda n:f'{D(str(n)):,.2f}'.replace(',',' ').replace('.',',')
def csvout(name,data):
 fields=list(dict.fromkeys(k for r in data for k in r))
 with (O/name).open('w',encoding='utf-8-sig',newline='') as stream:
  w=csv.DictWriter(stream,fieldnames=fields);w.writeheader();w.writerows(data)
rows=read(O/'all_screened_skus.json');by={r['article']:r for r in rows};maps=read(Path(read(L/'risk_policy.json')['family_maps_path']));cards={}
for x in read(O/'fresh_cards.json')['response']['items']:
 aliases=[str(x.get('sku'))]+[str(r.get('sku')) for r in x.get('sources',[])];sku=next((maps['canonical_by_alias'][a] for a in aliases if a in maps['canonical_by_alias']),None)
 assert sku;stocks=x.get('stocks') or {};available=sum(max(D(0),D(str(r.get('present') or 0))-D(str(r.get('reserved') or 0))) for r in stocks.get('stocks',[]));cards[sku]={'sku':sku,'article':x['offer_id'],'has_stock':stocks.get('has_stock') is True,'available_stock':str(available),'available_channel':any(r.get('availability')=='AVAILABLE' for r in x.get('availabilities') or []),'archived':bool(x.get('is_archived') or x.get('is_autoarchived')),'errors':len(x.get('errors') or []),'model_id':str((x.get('model_info') or {}).get('model_id') or ''),'snapshot_at_utc':read(O/'fresh_cards.json')['at_utc']}
floors={str(r['sku']):D(str(r['bid'])) for r in read(O/'fresh_floors.json')['response']['minBids']};placements=read(O/'fresh_bids.json')['placements'];assert len(placements)==len({(x['sku'],x['campaign_id']) for x in placements});pp={(x['sku'],x['campaign_id']):x for x in placements};plans=[]
auto_articles=['7214','7242','7245','7210'];manual_articles=['4838','2306'];cheap_articles=['239327','02521','3263','5918'];selected=[]
for art in auto_articles+manual_articles+cheap_articles:
 r=dict(by[art]);sku=r['sku'];card=cards[sku];assert card['has_stock'] and D(card['available_stock'])>0 and card['available_channel'] and not card['archived'] and card['errors']==0;assert card['model_id']==r['model_id'];r.update(card);r['minimum_target_bid_rub']=str(floors[sku]);r['priority']='A: смена автостратегии' if art in auto_articles else 'B: тест −10% дорогого размещения' if art in manual_articles else 'C: малый бюджет, второй этап';selected.append(r)
 if art in auto_articles:
  assert r['28d_own_unique_orders']==0 and r['28d_model_unique_orders']==0 and r['8d_own_unique_orders']==0
  cid=r['current_campaign_ids'];live=pp[(sku,cid)];assert live['strategy']=='MAX_CLICKS' and live['state']=='CAMPAIGN_STATE_RUNNING'
  plans.append({'article':art,'sku':sku,'campaign_id':cid,'current_strategy':'MAX_CLICKS','current_bid_rub':'AUTO: API bid=0 is not actual CPC','fresh_minimum_target_bid_rub':str(floors[sku]),'proposed_strategy':'TARGET_BIDS','proposed_target_bid_rub':str(floors[sku]),'action':'REVIEW_STRATEGY_CHANGE_ENTIRE_CAMPAIGN','whole_campaign_member_review_required':True,'overlaps_wave1_model':r['overlaps_wave1_model'],'automatic_application_allowed':False})
 else:
  options=[x for x in placements if x['sku']==sku and x['strategy']=='TARGET_BIDS' and x['state']=='CAMPAIGN_STATE_RUNNING'];assert options;highest=max(options,key=lambda x:D(x['bid_rub']));bid=D(highest['bid_rub']);target=max(floors[sku],(bid*D('.9')).quantize(D('.01'),rounding=ROUND_HALF_UP));assert floors[sku]<=target<bid
  plans.append({'article':art,'sku':sku,'campaign_id':highest['campaign_id'],'current_strategy':'TARGET_BIDS','current_bid_rub':str(bid),'fresh_minimum_target_bid_rub':str(floors[sku]),'proposed_strategy':'TARGET_BIDS','proposed_target_bid_rub':str(target),'action':'REVIEW_WEEKLY_MINUS_10_PERCENT_ONE_EXPENSIVE_PLACEMENT','other_placements':'KEEP_CURRENT_BID','overlaps_wave1_model':r['overlaps_wave1_model'],'automatic_application_allowed':False})
protect={r['sku'] for r in csvread(P/'ozon_recovery_20261103/revised_sku_summary_20.csv')}|{r['sku'] for r in csvread(L/'uploaded_sku_manifest.csv')};assert all(r['sku'] not in protect for r in selected)
autos=[r for r in selected if r['article'] in auto_articles];sp28=sum(D(r['28d_spend_rub']) for r in autos);sp8=sum(D(r['8d_spend_rub']) for r in autos);clicks8=sum(D(r['8d_clicks']) for r in autos);scenario=clicks8*D(10);assert clicks8==48
# Today is an extra partial-day check, kept outside the closed-day ranking.
today=read(O/'raw/auto_today.json');today_rows=today['response']['rows'];sp_today=sum(D(r['expense']) for r in today_rows);assert all(D(r['orders'])==0 and D(r['modelOrders'])==0 for r in today_rows)
comparison=[by[art] for art in ['5422','0005','5767']];already={'article':'7249X2','sku':'6201364326','campaign_id':'49321871','state':next(c['state'] for c in read(O/'live_campaigns.json')['response']['list'] if str(c['id'])=='49321871')};assert already['state']=='CAMPAIGN_STATE_STOPPED'
result={'checked_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'primary_window':['2026-10-11','2026-11-07'],'recent_window':['2026-10-31','2026-11-07'],'strongest_auto_skus':autos,'conditional_manual_skus':[r for r in selected if r['article'] in manual_articles],'small_budget_followup':[r for r in selected if r['article'] in cheap_articles],'not_first_priority':comparison,'auto_total_28d_rub':str(sp28),'auto_total_8d_rub':str(sp8),'auto_8d_clicks':str(clicks8),'scenario_10rub_actual_cpc_same_clicks':{'cost_rub':str(scenario),'difference_rub':str(sp8-scenario),'is_forecast':False,'traffic_and_average_cpc_may_change':True},'partial_today_auto_expense_rub':str(sp_today),'partial_today_at_utc':today['at_utc'],'already_stopped_excluded':already,'companion_in_campaign_44498580':{'sku':'5910506170','article':'7219','has_stock':False,'preserve_composition':True,'not_a_candidate_due_to_no_stock':True},'all_four_auto_models_overlap_wave1':all(r['overlaps_wave1_model'] for r in autos),'new_wave_skus_changed':False,'fixed20_skus_changed':False,'marketplace_mutations':0,'production_database_writes':0,'profit_or_causal_ad_effect_established':False,'current_risk_guard_scope':'Only 12 bound new experiment campaigns; these legacy campaigns are outside it','official_strategy_source':'https://seller.ozon.ru/media/boost/oplata-za-klik-instrument-prodvizheniya-na-ozon/'}
execution_path=O/'apply_20261108/manual_result.json'
funding_path=O/'apply_20261108/bonus_funding_summary.json'
execution=read(execution_path) if execution_path.exists() else None
funding=read(funding_path) if funding_path.exists() else None
if execution:
 result['execution']=execution
 result['marketplace_mutations']=sum(x['changed_now'] for x in execution['results'])
 for row in plans:
  applied=next((x for x in execution['results'] if x['article']==row['article']),None)
  row['application_status']='APPLIED_VERIFIED' if applied else 'HELD_BONUS_FUNDING_DECISION' if row['article'] in auto_articles else 'DRAFT_NOT_AUTHORIZED'
  if applied:
   row['bid_before_rub']=row['current_bid_rub'];row['current_bid_rub']=applied['verified_bid_rub'];row['applied_at_utc']=applied['verified_at_utc']
if funding:
 assert funding['all_rows_in_requested_dates'] and D(funding['money_total'])==0 and D(funding['prepayment_total'])==0
 result.update(bonus_funding_review=funding,auto_recommendation='HOLD_UNCHANGED_PENDING_EXPLICIT_RUBLE_FUNDING_DECISION',reported_auto_CPC_is_not_cash_saving=True)
 result['bonus_program_conditions_source']='https://seller.ozon.ru/media/news/bonusy-na-prodvizhenie-dlya-fbs-novinok/'
save('analysis_result.json',result);csvout('candidate_shortlist.csv',selected);csvout('draft_bid_plan.csv',plans);csvout('reviewed_good_performance.csv',comparison);save('fresh_readiness_summary.json',cards)
text='''# Ozon oz_acc1: кандидаты на снижение стоимости клика

Анализ на 08.11.2026. Основной период **11.10–07.11, 28 полных дней**, контроль последних дней **31.10–07.11, 8 полных дней**. Расход суммируется по всем полученным размещениям и SKU-алиасам; на число кампаний не делится. Общие заказы — уникальные неотменённые FBS/FBO, стоимость заказанного не является финансовой выручкой/прибылью.

**Реклама не менялась.** Фиксированные 20 SKU и все 9073 SKU нового месячного эксперимента исключены из ценовых предложений. Наличие, каналы, ошибки карточки и индивидуальные минимумы списка проверены чтением API 08.11. Наличие родственника не используется как запрет на отдельный SKU.

## Первая очередь: четыре дорогих SKU в трёх авто-РК

| Артикул | SKU | Расход 28 дней, ₽ | CPC факт, ₽ | Расход 8 дней, ₽ | Все заказы SKU / склейки, 28 дней | Остаток |
|---|---|---:|---:|---:|---:|---:|
'''
for r in autos:text+=f"| {r['article']} | {r['sku']} | {fmt(r['28d_spend_rub'])} | {fmt(r['28d_avg_cpc_rub'])} | {fmt(r['8d_spend_rub'])} | {r['28d_own_unique_orders']} / {r['28d_model_unique_orders']} | {r['available_stock']} |\n"
text+=f"\nИтого **{fmt(sp28)} ₽ за 28 дней**, **{fmt(sp8)} ₽ за последние 8 дней**, общих продаж самих товаров и их текущих склеек нет. За неполный 08.11 дополнительный SKU-отчёт показывает **{fmt(sp_today)} ₽**, прямых/модельных рекламных покупок нет; этот день в таблицу не добавлен. Нулевые общие продажи подтверждаются полными снимками заказов, а не отсутствием рекламной атрибуции.\n"
text+='''
У них стратегия **MAX_CLICKS**, цену выбирает Ozon. Ноль в поле bid API означает отсутствие индивидуальной ставки, а не бесплатные клики. Установить обычное «минус 10%» от этого нуля нельзя. Кандидат на отдельное решение — перевод трёх существующих РК в TARGET_BIDS с индивидуальным целевым значением **10 ₽**, подтверждённым min/sku. Это целевая средняя цена, не жёсткий предел каждого фактического клика. Настройки не применялись.

В РК 44498580 кроме 7214 уже есть 7219, сейчас без остатка. Его не удалять и не считать доступным кандидатом; смена стратегии общая для всей РК, полный состав должен быть сохранён и проверен при применении. РК 48489446 содержит 7242/7245, РК 43607968 — 7210. Создание новых кампаний и повторное добавление товаров не требуется для анализа.

Все четыре модели пересекаются с первой волной. Это не исключает доступные SKU из списка: если изменить стратегию, необходимо записать отдельное воздействие на модель и больше не приписывать всю динамику этой модели только первой волне. Текущий трёхчасовой стоп-сторож управляет лишь 12 подписанными новыми РК; эти старые авто-РК им не покрыты. Перед применением им нужен отдельный согласованный мониторинг.
'''
text+=f"\n**Масштаб, не обещание экономии:** за последние 8 дней получено {int(clicks8)} кликов. При таком же числе кликов и фактическом среднем CPC 10 ₽ расход составил бы {fmt(scenario)} ₽ вместо {fmt(sp8)} ₽; разница {fmt(sp8-scenario)} ₽. Трафик и конверсия после смены стратегии изменятся, фактический средний CPC может колебаться. Это сценарий, не прогноз и не расчёт прибыли.\n"
text+='\n## Обычный тест снижения ставки: два условных кандидата\n\n| Артикул | Расход 28 дней, ₽ | ДРР с модельной атрибуцией, 28 дней | ДРР 8 дней | Общие заказы SKU / склейки, 28 дней | Черновой первый шаг |\n|---|---:|---:|---:|---:|---|\n'
for art in manual_articles:
 r=by[art];plan=next(x for x in plans if x['article']==art);text+=f"| {art} | {fmt(r['28d_spend_rub'])} | {fmt(r['28d_drr_with_model_percent'])}% | {fmt(r['8d_drr_with_model_percent'])}% | {r['28d_own_unique_orders']} / {r['28d_model_unique_orders']} | РК {plan['campaign_id']}: {fmt(plan.get('bid_before_rub',plan['current_bid_rub']))} → {fmt(plan['proposed_target_bid_rub'])} ₽ |\n"
text+='''
У 4838 индивидуальная ставка 106,29 ₽ при минимуме 10 ₽; первый управляемый тест −10%: 95,66 ₽. У 2306 две действующие ставки: 14 ₽ в 12704286 и 50,17 ₽ в 10659294; предложение затрагивает только дорогую: 50,17 →45,15 ₽, дешёвую 14 ₽ оставить. Это кандидаты для теста, не доказанно убыточные товары. У 2306 модель также пересекается с первой волной — отдельная отметка воздействия обязательна.

ДРР здесь = CPC / сумма возвращённых Ozon собственных и модельных рекламных продаж. Это атрибуция площадки; общий заказ не равен рекламному, возможны пересечения между размещениями. CPO не входит в SKU-CPC и ДРР этой таблицы. Порог безубыточности не рассчитан: известное завышение Ozon-маржи в margin_by_sku не превращалось в обоснование ставок.

## Длинный хвост: второй этап, небольшой абсолютный эффект

'''
text+='| Артикул | Расход 28 дней, ₽ | Общие заказы SKU / модели | Дорогая ставка → черновик |\n|---|---:|---:|---|\n'
for art in cheap_articles:
 r=by[art];plan=next(x for x in plans if x['article']==art);text+=f"| {art} | {fmt(r['28d_spend_rub'])} | {r['28d_own_unique_orders']} / {r['28d_model_unique_orders']} | {fmt(plan['current_bid_rub'])} → {fmt(plan['proposed_target_bid_rub'])} ₽ |\n"
text+='\nУ 239327 второе размещение уже по минимуму 10 ₽ — его не снижать. У 27305 проверенное размещение уже 10 ₽ при минимуме 10 ₽: запаса для снижения нет. У недорогого хвоста клики около 10–16 ₽, поэтому абсолютная экономия значительно меньше, чем у четырёх авто-SKU. В черновике только самый дорогой действующий слот каждого SKU, остальные сохраняются.\n'
text+='\n## Что не ставлю в первую очередь\n\n| Артикул | Расход 28 дней, ₽ | ДРР с модельной атрибуцией | Все заказы модели | Стоимость заказанного модели, ₽ |\n|---|---:|---:|---:|---:|\n'
for r in comparison:text+=f"| {r['article']} | {fmt(r['28d_spend_rub'])} | {fmt(r['28d_drr_with_model_percent'])}% | {r['28d_model_unique_orders']} | {fmt(r['28d_model_ordered_value_rub'])} |\n"
text+='''
5422 имеет большой расход, но значительно лучшую наблюдаемую отдачу, чем четыре авто-SKU. У 0005 учёт модельных продаж снижает кажущийся ДРР; у 5767 много общих продаж в склейке и улучшение последнего окна. Один высокий расход или высокий ДРР отдельного SKU не доказывает ненужность рекламы. SKU 7249X2 не включён: его РК 49321871 уже STOPPED на текущем чтении, нами не выключалась.

## Происхождение данных и ограничения

Год заново не выгружался. Перепарсены четыре сохранённых октябрьских отчёта, сохранённые отчёты 01–04.11, готовые 05.11, существующие шесть РК за 06.11. Единственный недостающий закрытый день для восьми кампаний получен существующим fetch_sku_history.py с отдельными параметрами output/jobs; 07.11 прочитан быстрым API, в том числе расход уже остановленной РК. SKU × кампания × день дедуплицирован до объединения алиасов. Расчёты — отдельно от исходного API.

Текущие остатки, минимумы и выбранные страницы ставок перепроверены 08.11; просмотрена 61 карточка и 1 недоступный сосед. Отсутствующие исторические строки не заменены нулями. Полнота удалённых кампаний и исторический состав склеек/наличие не восстановлены; при расчёте общих продаж использована одна текущая карта model_id. История off/on из годового анализа — контекст, не доказательство отсутствия эффекта. CPO и реализационная прибыль требуют отдельного учёта.

Следующий конкретный шаг: выбрать перевод трёх авто-РК с четырьмя доступными SKU на индивидуальную целевую цену 10 ₽, сохранить составы и привязать отдельное наблюдение SKU/модели/аккаунта. Для обычных ставок — отдельный небольшой недельный тест 4838 и/или 2306 с указанными слотами −10%. Ни один вариант не применён автоматически.
'''
if execution:
 text=text.replace('**Реклама не менялась.**','**08.11 по прямой команде изменены только две согласованные ставки; результат подтверждён чтением API.**')
 text=text.replace('## Обычный тест снижения ставки: два условных кандидата','## Согласованный тест: две ставки уже снижены')
 text=text.replace('Черновой первый шаг','Применённое изменение')
 text=text.replace('Следующий конкретный шаг: выбрать перевод трёх авто-РК с четырьмя доступными SKU на индивидуальную целевую цену 10 ₽, сохранить составы и привязать отдельное наблюдение SKU/модели/аккаунта. Для обычных ставок — отдельный небольшой недельный тест 4838 и/или 2306 с указанными слотами −10%. Ни один вариант не применён автоматически.', 'Следующий шаг: наблюдать две выполненные ставки; результат оценивать по SKU, моделям и аккаунту. Четыре бонусных SKU оставлены без изменения до отдельного решения по финансированию. Длинный хвост не менялся. Повторно применять те же −10% нельзя.')
if funding:
 text=text.replace('## Первая очередь: четыре дорогих SKU в трёх авто-РК','## Четыре авто-SKU: бонусное финансирование, изменение удержано')
 text=text.replace('Кандидат на отдельное решение — перевод трёх существующих РК в TARGET_BIDS с индивидуальным целевым значением **10 ₽**, подтверждённым min/sku. Это целевая средняя цена, не жёсткий предел каждого фактического клика. Настройки не применялись.', 'Первоначальный вариант перевода трёх РК в TARGET_BIDS с целевой средней ценой 10 ₽ не применён. Финансовая проверка ниже показала, что это бонусные расходы; изменение стратегии требует отдельного решения из-за возможного последующего списания рублей.')
 text=text.replace('**Масштаб, не обещание экономии:**','**Первоначальный расчёт объёма CPC; это не рублевая экономия:**')
 prefix='''# Выполнено 08.11: две ставки снижены, бонусные кампании сохранены

**4838:** РК 10659516, SKU 1261464375 — 106,29 →95,66 ₽, проверено API 08.11 в 16:40 МСК.
**2306:** РК 10659294, SKU 863386567 — 50,17 →45,15 ₽. Второе размещение в 12704286 осталось 14 ₽. Другие ставки на проверенных страницах не менялись.

**Пересечение с волной 1:** у 4838 его нет. Ozon объединяет 2306 и бандл **2294X2 (SKU 5251202928)** в model_id **220510583**. Бандл находится в работающей РК волны 1 **49852681**. Ставку бандла не меняли; снижение старого 2306 записано отдельным воздействием для следующих сравнений склейки. Это связь Ozon model_id, а не утверждение одинакового физического состава или равенства артикулов.

**Четыре авто-SKU не менялись.** Три РК имеют isAutocreated=true и название «Бонусы на продвижение товаров-новинок FBS». Финансовая проверка expense/json за тот же период 11.10–07.11 подтвердила:

| РК | Денежный расход, ₽ | Бонусы | Предоплата, ₽ |
|---|---:|---:|---:|
'''
 for cid,amounts in funding['campaigns'].items():prefix+=f"| {cid} | {fmt(amounts['moneySpent'])} | {fmt(amounts['bonusSpent'])} | {fmt(amounts['prepaymentSpent'])} |\n"
 prefix+=f"\nИтого по трём РК: **{fmt(funding['bonus_total'])} бонусов, 0 ₽ денег и предоплаты**. В сумму входит также историческое продвижение 7219, сейчас без остатка. Объём 12 201,19 ₽ по четырём доступным SKU тоже оплачен бонусами. Предыдущая рекомендация трактовать его как возможность экономии рублей была преждевременной: источники финансирования не были разделены.\n"
 prefix+='''
По условиям акции Ozon после изменения стратегии, бюджета или состава после окончания бонусов могут начаться списания рублями. Смена стратегии не отправлялась даже как проба: сохранение само по себе может изменить условие оплаты, а откат не доказывает восстановления прежнего статуса. История прежних настроек не восстановлена, поэтому будущую автоматическую остановку этих РК не обещаем.

Ожидается отдельный ответ владельца: сохранить бонусные настройки или согласовать смену стратегии с возможными рублёвыми расходами. Текущие недельные бюджеты этих трёх РК — 4000 +4000 +2000 =10000 ₽, отдельно от бюджета первой волны. Новых РК, удалений товаров, изменений бонусных бюджетов нет.

Ниже сохранён исходный количественный разбор; расходы по авто-РК следует читать как бонусный объём продвижения, не расходы рублями.

'''
 text=prefix+text.replace('# Ozon oz_acc1: кандидаты на снижение стоимости клика','## Исходный срез и данные для наблюдения')
(O/'RESULT.md').write_text(text)
# Render markdown tables without exposing source postings, order IDs, credentials or private rows.
parts=[];lines=text.splitlines();i=0
while i<len(lines):
 line=lines[i]
 if line.startswith('|'):
  block=[]
  while i<len(lines) and lines[i].startswith('|'):block.append(lines[i]);i+=1
  parts.append('<div class="scroll"><table>')
  for n,row in enumerate(block):
   if n==1:continue
   tag='th' if n==0 else 'td';parts.append('<tr>'+''.join(f'<{tag}>'+html.escape(x.strip())+f'</{tag}>' for x in row.strip('|').split('|'))+'</tr>')
  parts.append('</table></div>');continue
 if line.startswith('# '):parts.append('<h1>'+html.escape(line[2:])+'</h1>')
 elif line.startswith('## '):parts.append('<h2>'+html.escape(line[3:])+'</h2>')
 elif line:parts.append('<p>'+html.escape(line).replace('**','')+'</p>')
 i+=1
css='body{font:17px/1.65 system-ui;background:#f5f6f8;color:#182230}main{max-width:1250px;margin:auto;padding:24px;background:white}.scroll{overflow:auto}table{border-collapse:collapse;width:100%}th,td{padding:10px;text-align:left;border-bottom:1px solid #ddd}th{background:#eef2f6}h1{font-size:28px}h2{font-size:23px}a{color:#175cd3}'
links='<p><a href="/api/wb-bids/reports/ozon-bid-candidates-2026-11-08-skus">Кандидаты — CSV</a> · <a href="/api/wb-bids/reports/ozon-bid-candidates-2026-11-08-plan">План и статусы применения — CSV</a></p><p><a href="'+result['official_strategy_source']+'">Ozon: управление стратегиями и минимальными ставками</a> · <a href="https://seller.ozon.ru/media/news/bonusy-na-prodvizhenie-dlya-fbs-novinok/">Ozon: условия бонусных FBS-кампаний</a></p>'
(O/'index.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>Ozon: кандидаты на снижение CPC</title><style>'+css+'</style><main>'+links+''.join(parts)+links+'</main></html>')
save('validation.json',{'at_utc':result['checked_at_utc'],'unique_shortlisted_skus':len(selected),'fresh_stock_and_channels_verified':True,'all_shortlist_archived_or_error':False,'all_shortlist_models_match_frozen_map':True,'auto_candidates_confirmed_no_general_SKU_or_model_sales_28d':True,'no_fixed20_or_new_experiment_SKUs_in_plan':True,'fresh_minimum_verified_for_all_proposals':True,'manual_proposal_bids_do_not_increase':True,'auto_API_bid_zero_not_treated_as_real_CPC':True,'whole_campaign_companion_review_explicit':True,'savings_scenario_not_forecast':True,'marketplace_mutations':0,'production_database_writes':0})
if execution:
 validation=read(O/'validation.json');validation.update(marketplace_mutations=result['marketplace_mutations'],manual_changes_verified=2,autocreated_campaign_write_calls=0,bonus_funding_verified=bool(funding),legacy_2306_cheap_slot_preserved=True,bonus_strategy_change_held=True);save('validation.json',validation)
print(json.dumps({'auto_28d_rub':str(sp28),'auto_recent8d_rub':str(sp8),'auto_recent_clicks':str(clicks8),'conditional_scenario_difference_rub':str(sp8-scenario),'partial_today_auto_rub':str(sp_today),'shortlist':len(selected),'draft_rows':len(plans),'manual_plan':[r for r in plans if r['article'] in manual_articles]},ensure_ascii=False))
