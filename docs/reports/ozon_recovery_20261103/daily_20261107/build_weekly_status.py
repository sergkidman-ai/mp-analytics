# поток: mkt / ozon
"""Offline fixed-20 weekly report from existing snapshots; no API or DB access."""
from pathlib import Path
from decimal import Decimal as D
import csv, json, datetime as dt, html, shutil
O=Path(__file__).parent; P=O.parent
read=lambda f:list(csv.DictReader(f.open(encoding='utf-8-sig')))
write_json=lambda f,d:f.write_text(json.dumps(d,ensure_ascii=False,indent=2)+'\n')
def write_csv(f,rows):
 with f.open('w',encoding='utf-8-sig',newline='') as stream:
  w=csv.DictWriter(stream,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
num=lambda n:f'{D(str(n)):,.2f}'.replace(',',' ').replace('.',',')
pct=lambda a,b:(D(str(a))/D(str(b))-1)*100 if D(str(b)) else None
cfg=json.loads((O/'monitor_config.json').read_text());quality=json.loads((O/'monitor20_quality.json').read_text());data=json.loads((O/'weekly_periods_intermediate.json').read_text());periods=data['periods'];before,after=periods
assert quality['posting_snapshots_complete'] and quality['ad_sku_day_coverage']==140
base_lines=[r for r in read(P.parent/'ozon_launch_20261106/baseline/sales_lines.csv') if before['from_msk']<=r['date_msk']<=before['through_msk']]
before['account_orders']=len({r['order_id'] for r in base_lines});before['account_ordered_value_rub']=str(sum(D(r['ordered_value_rub']) for r in base_lines))
main=read(P/'revised_sku_summary_20.csv');oldads=read(P/'revised_sku_campaign_daily.csv');freshads=read(O/'monitor20_ad_fresh.csv');fresh_summary={r['sku']:r for r in read(O/'monitor20_sku_summary_20.csv')};target={r['sku'] for r in main};models={r['current_family_model_id'] for r in main};oldlines=read(P/'revised_sales_lines.csv')
assert len(target)==20 and len(models)==19
per_sku=[]
for item in main:
 sku=item['sku'];row={'sku':sku,'article':item['article'],'frozen_model_id':item['current_family_model_id']}
 for period,ads in [(before,oldads),(after,freshads)]:
  aa=[r for r in ads if r['sku']==sku and period['from_msk']<=r['date_msk']<=period['through_msk']];prefix=period['period']+'_';spend=sum(D(r['spend_rub']) for r in aa);sales=sum(D(r['ad_revenue_rub']) for r in aa);clicks=sum(D(r['clicks']) for r in aa)
  row.update({prefix+'ad_spend_rub':str(spend),prefix+'explicit_ad_days':len({r['date_msk'] for r in aa}),prefix+'explicit_placements':len({r['campaign_id'] for r in aa}),prefix+'ad_sales_rub':str(sales),prefix+'drr_percent':str(spend/sales*100) if sales else '',prefix+'clicks':str(clicks),prefix+'avg_cpc_rub':str(spend/clicks) if clicks else ''})
  if period['period']=='before':
   ll=[r for r in base_lines if r['sku']==sku];orders=len({r['order_id'] for r in ll});value=sum(D(r['ordered_value_rub']) for r in ll)
  else:orders=int(fresh_summary[sku]['orders']);value=D(fresh_summary[sku]['ordered_value_rub'])
  row.update({prefix+'own_unique_orders':orders,prefix+'own_ordered_value_rub':str(value)})
 per_sku.append(row)
assert sum(D(r['after_ad_spend_rub']) for r in per_sku)==D(after['ad_spend_rub'])
assert sum(D(r['before_ad_spend_rub']) for r in per_sku)==D(before['ad_spend_rub'])
write_csv(O/'weekly_by_sku_20.csv',per_sku);write_csv(O/'weekly_period_comparison.csv',periods)
changes={k:str(pct(after[k],before[k])) for k in ['ad_spend_rub','fixed20_orders','fixed20_ordered_value_rub','families_orders','families_ordered_value_rub','account_orders','account_ordered_value_rub','avg_cpc_rub']}
coverage={'known_placements':36,'before_explicit_placements':before['explicit_placement_count'],'after_explicit_placements':after['explicit_placement_count'],'before_sku_day_coverage':before['sku_day_coverage'],'after_sku_day_coverage':140,'sku_day_expected':140,'before_missing_sku_days':[{'sku':'1510947813','date_msk':'2026-10-29'}],'previous_exact_gap_report':'../revision_raw/gap_1510947813_2026-10-29.zip; no matching rows','missing_rows_are_zero':False,'all_36_placements_complete':False}
result={'checked_at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'account':'oz_acc1','periods':periods,'changes_percent':changes,'observed_spend_difference_rub':str(D(before['ad_spend_rub'])-D(after['ad_spend_rub'])),'coverage':coverage,'post_raw_snapshot_config':cfg,'late_attribution_final':False,'baseline_sales_source':'../ozon_launch_20261106/baseline/sales_lines.csv, captured 06.11; fixed20/family totals agree with original frozen baseline','baseline_ad_source':'../revised_sku_campaign_daily.csv','account_expense_scope':'CPC SKU campaigns only; CPO/CPM not measured here. Not total account advertising expense.','ordered_value_is_financial_revenue':False,'causal_claim_allowed':False,'new_wave_confounder':json.loads((P.parent/'ozon_launch_20261106/activation_20261106/fixed20_exposure_context.json').read_text()),'marketplace_mutations':0,'database_writes':0,'monitoring_review_date_msk':'2026-11-10','full_days_review_through':'2026-11-09'}
write_json(O/'weekly_status.json',result)
daily=read(O/'monitor20_daily.csv')
text='''# Фиксированные 20 SKU — недельный итог на 07.11.2026

Ставки не менялись. Сумма по всем полученным размещениям, без деления на две кампании.
Каждый период включает по одному полному дню каждого дня недели.
30 октября — переходный день, исключён. Текущий 7 ноября в сравнение не входит.

| Период | Расход по доступным строкам, ₽ | Средний CPC, ₽ | ДРР Ozon |
|---|---:|---:|---:|
'''
for r in periods:text+=f"| {r['from_msk']} — {r['through_msk']} | {num(r['ad_spend_rub'])} | {num(r['avg_cpc_rub'])} | {num(r['drr_percent'])}% |\n"
text+='\n| Период | Общие заказы 20 SKU | Заказано по 20 SKU, ₽ | Заказы 19 склеек | Заказано по склейкам, ₽ |\n|---|---:|---:|---:|---:|\n'
for r in periods:text+=f"| {'До' if r['period']=='before' else 'После'} | {r['fixed20_orders']} | {num(r['fixed20_ordered_value_rub'])} | {r['families_orders']} | {num(r['families_ordered_value_rub'])} |\n"
text+=f"\nНаблюдаемая разница расходов {num(result['observed_spend_difference_rub'])} ₽ ({num(changes['ad_spend_rub'])}%). Общие заказы самих товаров 31 → 31; стоимость заказанного +{num(changes['fixed20_ordered_value_rub'])}%, склеек +{num(changes['families_ordered_value_rub'])}%. По этим данным потери стоимости заказов самих 20 SKU за неделю нет. Причинный эффект и изменение прибыли не установлены. ДРР = расход / приписанные Ozon рекламные продажи: {num(before['ad_sales_rub'])} → {num(after['ad_sales_rub'])} ₽. Расход / все общие заказы самих SKU: {num(before['expense_to_all_sku_orders_percent'])}% → {num(after['expense_to_all_sku_orders_percent'])}%.\n"
text+='\n## По дням после снижения\n\n| День МСК | Расход 20 SKU, ₽ | Общие заказы 20 SKU | Заказано 20 SKU, ₽ | Заказы склеек | Заказано по склейкам, ₽ |\n|---|---:|---:|---:|---:|---:|\n'
for r in daily:text+=f"| {r['date_msk']} | {num(r['spend_rub'])} | {r['fixed20_orders']} | {num(r['fixed20_ordered_value_rub'])} | {r['families_union_orders']} | {num(r['families_union_ordered_value_rub'])} |\n"
text+='\n## Весь аккаунт за те же периоды\n\n| Период | Общие уникальные заказы | Стоимость заказанного, ₽ | CPC SKU-кампаний, ₽ (без CPO/CPM) |\n|---|---:|---:|---:|\n'
for r in periods:text+=f"| {'До' if r['period']=='before' else 'После'} | {r['account_orders']} | {num(r['account_ordered_value_rub'])} | {num(r['account_cpc_spend_rub'])} |\n"
text+=f"\nСтоимость заказанного аккаунта {num(changes['account_ordered_value_rub'])}%, заказы {num(changes['account_orders'])}%. Это стоимость товарных строк неотменённых заказов, не финансовая выручка/прибыль. База общих продаж перечитана из уже сохранённого более свежего среза 06.11: 430 заказов вместо старых 433, стоимость 3 672 728 вместо 3 701 142 ₽; показатели самих 20 и склеек совпали. CPC без CPO/CPM нельзя выдавать за общий расход рекламы аккаунта.\n"
if (O/'holiday_sensitivity.json').exists():
 holiday=json.loads((O/'holiday_sensitivity.json').read_text())
 text+='\n## Сравнение аккаунта без праздничной среды\n\nИсключены 04.11 и соответствующая среда до снижения — 28.10. Остаются по шесть дней с одинаковым составом дней недели.\n\n| Период | Общие заказы | Стоимость заказанного, ₽ |\n|---|---:|---:|\n'
 for row in holiday['rows']:text+=f"| {'До' if row['phase']=='before' else 'После'} | {row['orders']} | {num(row['ordered_value_rub'])} |\n"
 text+=f"\nСтоимость заказанного +{num(holiday['ordered_value_change_percent'])}% (+{num(holiday['ordered_value_delta_rub'])} ₽), заказы +{num(holiday['orders_change_percent'])}%. Снижения на этом шестидневном сравнении нет. Исключение дня не измеряет причинный вклад праздника; его и влияние ставок отделить пока нельзя. С 06.11 действует также новая волна рекламы.\n"
text+='''
## Полнота и ограничения

Из 36 известных SKU × кампания отчётные строки есть по 31 связке до и по 33 после.
До покрыто 139/140 SKU-дней, после 140/140. Отсутствует SKU 1510947813 за 29.10;
сохранённый повторный точечный запрос его строк не дал. Полнота всех 36 связок не
доказана; отсутствия не заменены нулями. Денежные суммы — по полученным строкам.
Свежие поздние заказы/отмены и рекламная атрибуция за 31.10–06.11 пересчитаны,
прежние дни заменены, не сложены повторно. Будущие уточнения возможны.

19 склеек зафиксированы одинаково по обе стороны; исторический состав не доказан.
С 06.11 16:09 новая волна не пересекает эти 20 напрямую, но затрагивает 5 их склеек.
Её воздействие и общий сезонный/праздничный фактор нельзя приписывать снижению CPC.
Склейки содержат сами целевые SKU; эти заказы не складывать с заказами 20 SKU.

Следующий шаг: продолжать ежедневный сбор до контрольного среза 10 ноября
по полным дням 31.10–09.11. Ставки повторно не снижать.

Данные: weekly_status.json, weekly_period_comparison.csv, weekly_by_sku_20.csv,
monitor20_daily.csv; API-сырьё и журнал чтений — raw/ и api_reads.jsonl.
'''
(O/'WEEKLY_STATUS.md').write_text(text)
# Simple static HTML, restricted to aggregate figures and the 20-SKU download.
parts=[];lines=text.splitlines();i=0
while i<len(lines):
 line=lines[i]
 if line.startswith('|'):
  group=[]
  while i<len(lines) and lines[i].startswith('|'):group.append(lines[i]);i+=1
  parts.append('<div class="scroll"><table>')
  for n,row in enumerate(group):
   if n==1:continue
   tag='th' if n==0 else 'td';parts.append('<tr>'+''.join(f'<{tag}>'+html.escape(cell.strip())+f'</{tag}>' for cell in row.strip('|').split('|'))+'</tr>')
  parts.append('</table></div>');continue
 if line.startswith('# '):parts.append('<h1>'+html.escape(line[2:])+'</h1>')
 elif line.startswith('## '):parts.append('<h2>'+html.escape(line[3:])+'</h2>')
 elif line:parts.append('<p>'+html.escape(line)+'</p>')
 i+=1
style='body{font:17px/1.6 system-ui;background:#f5f6f8;color:#182230}main{max-width:1200px;margin:auto;padding:24px;background:white}.scroll{overflow:auto}table{border-collapse:collapse;width:100%}td,th{padding:10px;border-bottom:1px solid #ddd;text-align:left}th{background:#eef2f6}h1{font-size:27px}h2{font-size:23px}'
(O/'index.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>20 SKU: недельный итог</title><style>'+style+'</style><main>'+''.join(parts)+'<p><a href="/api/wb-bids/reports/ozon-fixed20-2026-11-07-skus">Все 20 SKU: CSV</a></p></main></html>')
for name in ['fetch_monitor20.py','analyze_monitor20.py']:shutil.copyfile(P/'launch_preflight'/name,O/('used_'+name))
print(json.dumps({'periods':periods,'changes_percent':changes,'observed_spend_difference_rub':result['observed_spend_difference_rub'],'coverage':coverage},ensure_ascii=False))
