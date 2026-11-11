# поток: ozon-family-strategy; current assortment, whole retained year; no APIs.
import sys,datetime,collections,csv,json
from zoneinfo import ZoneInfo
from common import *
sys.dont_write_bytecode=True;sys.path.insert(0,'/opt/mp-analytics')
from core.ozon_posting import product_price
cat,cards,alias=current();TZ=ZoneInfo('Europe/Moscow')
metric={pid:{'sales_cents':0,'units':0,'orders':{},'cpc_cents':0,'cpc_rows':0,'first_cpc_evidence':'','first_positive_spend_date':''} for pid in cat}
coverage=read(Y/'order_collection_coverage.json');assert coverage['finished']
covered=collections.defaultdict(set);seen=set();linekeys=set();selected_lines=[];months=collections.defaultdict(lambda:collections.Counter());sources=[]
selected_cancelled=0
for w in coverage['windows']:
 p=Path(w['path']);z=read(p);assert z['complete'];sources.append(str(p))
 a=datetime.date.fromisoformat(w['from_msk']);b=datetime.date.fromisoformat(w['through_msk'])
 covered[w['scheme']].update((a+datetime.timedelta(days=i)).isoformat() for i in range((b-a).days+1))
 for r in z['postings']:
  # Ignore every excluded card before collecting any business metrics.
  selected=[(i,v) for i,v in enumerate(r['products']) if str(v.get('sku')) in alias]
  if not selected:continue
  pk=(w['scheme'],r['posting_number']);assert pk not in seen;seen.add(pk)
  if r['status'].lower().startswith('cancel'):selected_cancelled+=1;continue
  day=datetime.datetime.fromisoformat((r.get('in_process_at') or r.get('created_at')).replace('Z','+00:00')).astimezone(TZ).date().isoformat()
  if not START<=day<=END:continue
  oid=str(r['order_id']);assert oid and oid!='None'
  for i,v in selected:
   key=(*pk,i);assert key not in linekeys;linekeys.add(key)
   pid=alias[str(v['sku'])];qty=D(str(v['quantity']));assert qty==qty.to_integral_value() and qty>0
   amount=int((product_price(v)*qty*100).to_integral_value());m=metric[pid];m['sales_cents']+=amount;m['units']+=int(qty)
   if oid in m['orders']:assert m['orders'][oid]==day
   m['orders'][oid]=day;months[day[:7]]['sales_cents']+=amount;months[day[:7]]['units']+=int(qty)
   selected_lines.append({'scheme':w['scheme'],'posting_number':r['posting_number'],'line_index':i,'order_id':oid,'date_msk':day,'product_id':pid,'sku':str(v['sku']),'market_units':int(qty),'ordered_value_kopeks':amount,'source':str(p.relative_to(O.parent.parent.parent))})
assert all(len(x)==365 for x in covered.values())
print('Selected current sales lines',len(selected_lines),flush=True)
# Independent reconciliation with existing normalized sales, same current alias filter.
norm_sales=collections.Counter();norm_units=collections.Counter();sales_norm_rows=0
for r in rows(Y/'sku_general_sales_daily_year.csv'):
 if r['sku'] not in alias:continue
 assert START<=r['date_msk']<=END;pid=alias[r['sku']]
 norm_sales[pid]+=cents(r['ordered_value_rub']);norm_units[pid]+=int(D(r['units']));sales_norm_rows+=1
assert all(m['sales_cents']==norm_sales[pid] and m['units']==norm_units[pid] for pid,m in metric.items())
adkeys=set();cpcdays=collections.defaultdict(set);spenddays=collections.defaultdict(set)
for r in rows(Y/'sku_positive_cpc_daily_year.csv'):
 if r['sku'] not in alias:continue
 assert START<=r['date_msk']<=END;k=(r['sku'],r['date_msk']);assert k not in adkeys;adkeys.add(k)
 pid=alias[r['sku']];v=cents(r['spend_rub']);metric[pid]['cpc_cents']+=v;metric[pid]['cpc_rows']+=1;cpcdays[pid].add(r['date_msk'])
 if v>0:spenddays[pid].add(r['date_msk'])
 months[r['date_msk'][:7]]['cpc_cents']+=v
print('Selected current CPC rows',len(adkeys),flush=True)
# Check saved original CPC reports using the identical current alias filter.
raw_spend=collections.Counter();raw_keys=set();raw_positive_rows=0;raw_sources=[]
def date(v):
 try:return datetime.datetime.strptime(v,'%d.%m.%Y').date().isoformat()
 except ValueError:
  try:return datetime.date.fromisoformat(v[:10]).isoformat()
  except ValueError:return None
def add(cid,sku,day,spend,views,clicks):
 global raw_positive_rows
 sku=str(sku)
 if sku not in alias or not day or not START<=day<=END:return
 if not(spend>0 or D(str(views or 0).replace(',','.'))>0 or D(str(clicks or 0).replace(',','.'))>0):return
 k=(str(cid),sku,day);assert k not in raw_keys;raw_keys.add(k);raw_positive_rows+=1;raw_spend[alias[sku]]+=spend
for folder in sorted((Y/'sku_reports').iterdir()):
 if not (folder/'complete.json').exists():continue
 for p in folder.glob('*.csv'):
  raw_sources.append(str(p));cid=p.name.split('_')[0]
  if not cid.isdigit():
   req=read(folder/'complete.json')['request']['campaigns'];assert len(req)==1;cid=req[0]
  with p.open(encoding='utf-8-sig') as f:
   next(f,None)
   for r in csv.DictReader(f,delimiter=';'):add(cid,r.get('sku'),date(r.get('День','')),cents(r.get('Расход, ₽, с НДС')),r.get('Показы'),r.get('Клики'))
for filename in read(Y/'sku_report_jobs.json')['reused_sku_report_files']:
 raw_sources.append(filename)
 for cid,block in read(Path(filename))['body'].items():
  for r in (block.get('report') or {}).get('rows',[]):add(cid,r.get('sku'),date(r.get('date','')),cents(r.get('moneySpent')),r.get('views'),r.get('clicks'))
diff={pid:raw_spend[pid]-m['cpc_cents'] for pid,m in metric.items() if raw_spend[pid]!=m['cpc_cents']}
assert not diff,diff
for pid,m in metric.items():m['first_cpc_evidence']=min(cpcdays[pid]) if cpcdays[pid] else '';m['first_positive_spend_date']=min(spenddays[pid]) if spenddays[pid] else ''
save('history_by_card.json',metric);write('sales_lines.csv',selected_lines)
save('history_reconciliation.json',{'scope':'current_nonarchived_snapshot_2026-11-06','current_cards':len(cat),'excluded_archived_cards_technical_count':25956,'period':[START,END],
 'scheme_covered_days':{k:len(v) for k,v in covered.items()},'selected_sales_lines':len(selected_lines),'selected_cancelled_postings_excluded':selected_cancelled,
 'selected_unique_orders':len({x['order_id'] for x in selected_lines}),'selected_market_units':sum(m['units'] for m in metric.values()),
 'selected_ordered_value_cents':sum(m['sales_cents'] for m in metric.values()),'selected_cpc_cents':sum(m['cpc_cents'] for m in metric.values()),
 'normalized_sales_rows':sales_norm_rows,'normalized_cpc_rows':len(adkeys),'raw_cpc_positive_rows':raw_positive_rows,
 'sales_vs_normalized_per_card_mismatches':0,'cpc_vs_original_reports_per_card_mismatches':len(diff),'months':months,
 'raw_sales_sources':sources,'raw_cpc_sources':raw_sources,'no_archive_breakdown':True,'money_semantics':'noncancelled ordered value; CPC only; not financial realization or profit'})
print('History reconciliation PASS',sum(m['sales_cents'] for m in metric.values()),sum(m['cpc_cents'] for m in metric.values()),flush=True)
