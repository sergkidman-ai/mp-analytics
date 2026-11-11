# поток: mkt
import sys,pathlib,json,csv,datetime as dt,collections,decimal
from zoneinfo import ZoneInfo
sys.dont_write_bytecode=True
sys.path.insert(0,'/opt/mp-analytics')
from core.ozon_posting import product_price
O=pathlib.Path(__file__).parent;R=O/'revision_raw';D=decimal.Decimal;TZ=ZoneInfo('Europe/Moscow')
TARGET='1510947813 863386191 1719404303 1277712843 930627436 2909045114 930623456 1658970308 866442961 1999321262 3557209622 906874407 4504239311 4518308193 652626682 4463532700 4480826404 4518304506 4489633315 4518302691'.split()
def save(n,v):(O/n).write_text(json.dumps(v,ensure_ascii=False,indent=2,default=str)+'\n')
def write(n,rs):
 with (O/n).open('w',newline='') as f:
  w=csv.DictWriter(f,fieldnames=list(rs[0]));w.writeheader();w.writerows(rs)
base28=[(dt.date(2026,10,2)+dt.timedelta(days=i)).isoformat() for i in range(28)];base14=base28[-14:];post=['2026-10-31','2026-11-01','2026-11-02'];days=base28+post
items={};alias={}
for p in [R/'product_info_targets_candidate.json']+sorted(R.glob('product_info_sold_*.json')):
 z=json.loads(p.read_text())
 if z['http']!=200:continue
 for x in z['body'].get('items',[]):
  items[str(x['id'])]=x
  for s in [x.get('sku')]+[a.get('sku') for a in x.get('sources',[])]:
   if s:alias[str(s)]=str(x['id'])
targetid={s:alias[s] for s in TARGET};targetmodel={s:str(items[pid]['model_info']['model_id']) for s,pid in targetid.items()}
models=set(targetmodel.values());targetpids=set(targetid.values())
lines=[];postingkeys=set();cancelled=0;coverage=[]
for p in sorted(R.glob('postings_*_f*.json')):
 v=json.loads(p.read_text());coverage.append({k:x for k,x in v.items() if k!='postings'})
 if not v['complete']:continue
 for x in v['postings']:
  pk=(v['scheme'],x['posting_number']);assert pk not in postingkeys;postingkeys.add(pk)
  if x['status'].lower().startswith('cancel'):
   cancelled+=1;continue
  when=dt.datetime.fromisoformat((x.get('in_process_at') or x.get('created_at')).replace('Z','+00:00')).astimezone(TZ).date().isoformat();assert when in days
  seen=set()
  for i,z in enumerate(x['products']):
   sku=str(z['sku']);assert sku not in seen,'Duplicate SKU in posting requires review';seen.add(sku)
   pid=alias.get(sku);model=str(items[pid].get('model_info',{}).get('model_id','')) if pid else ''
   lines.append({'date_msk':when,'scheme':v['scheme'],'posting_number':x['posting_number'],'order_id':str(x['order_id']),'status_asof_fetch':x['status'],'sku':sku,'canonical_sku':str(items[pid].get('sku')) if pid else None,'product_id':pid,'offer_id':z['offer_id'],'name':z['name'],'quantity':z['quantity'],'unit_price_rub':product_price(z),'ordered_value_rub':product_price(z)*z['quantity'],'current_model_id':model,'source':str(p.relative_to(O)),'line_index':i})
assert all(coverage) and all(c['complete'] for c in coverage)
assert all(l['product_id'] and l['current_model_id'] for l in lines)
write('revised_sales_lines.csv',lines)
orderdays=collections.defaultdict(set)
for l in lines:orderdays[l['order_id']].add(l['date_msk'])
multiday=sum(len(x)>1 for x in orderdays.values())
scopes={'cabinet':lambda l:True,'fixed20_exact_sku':lambda l:l['sku'] in TARGET,'fixed20_current_cards':lambda l:l['product_id'] in targetpids,'families_union_current':lambda l:l['current_model_id'] in models,'candidate_49321871_sku':lambda l:l['sku']=='6201364326'}
for s in TARGET:
 scopes['sku_'+s]=lambda l,s=s:l['sku']==s
 scopes['family_'+s]=lambda l,s=s:l['current_model_id']==targetmodel[s]
def metrics(rr):
 return {'orders':len({x['order_id'] for x in rr}),'postings':len({(x['scheme'],x['posting_number']) for x in rr}),'units':sum(x['quantity'] for x in rr),'ordered_value_rub':sum((x['ordered_value_rub'] for x in rr),D(0))}
daily=[];idx={}
for scope,predicate in scopes.items():
 for day in days:
  rec={'scope':scope,'date_msk':day,**metrics([l for l in lines if l['date_msk']==day and predicate(l)]),'status':'complete noncancelled postings feed'}
  daily.append(rec);idx[(scope,day)]=rec
write('revised_sales_daily.csv',daily)
windowrows=[]
for scope,predicate in scopes.items():
 for n,base in [(14,base14),(28,base28)]:
  rec={'scope':scope,'baseline_days':n,'post_days':3,'composition_basis':'current model_id snapshot for family scopes; historical family composition unavailable'}
  bm=metrics([l for l in lines if l['date_msk'] in base and predicate(l)]);pm=metrics([l for l in lines if l['date_msk'] in post and predicate(l)])
  for m in ['orders','postings','units','ordered_value_rub']:
   expected=sum((sum((D(str(idx[(scope,b)][m])) for b in base if dt.date.fromisoformat(b).weekday()==dt.date.fromisoformat(d).weekday()),D(0))/D(n//7) for d in post),D(0))
   rec['base_total_'+m]=bm[m];rec['base_daily_'+m]=D(str(bm[m]))/D(n)
   rec['post_total_'+m]=pm[m];rec['post_daily_'+m]=D(str(pm[m]))/D(3)
   rec['expected_post_'+m]=expected;rec['expected_daily_'+m]=expected/D(3);rec['delta_'+m]=D(str(pm[m]))-expected
   rec['delta_percent_'+m]=D(100)*rec['delta_'+m]/expected if expected else None
  windowrows.append(rec)
write('revised_sales_windows.csv',windowrows)
# Membership is current. Full sold-SKU universe means no observed noncancelled sales line was left unclassified.
familyrows=[]
for model in sorted(models):
 for pid,x in sorted(items.items()):
  if str(x.get('model_info',{}).get('model_id'))!=model:continue
  familyrows.append({'current_model_id':model,'target_skus_sharing_family':';'.join(s for s in TARGET if targetmodel[s]==model),'product_id':pid,'sku':x.get('sku'),'offer_id':x.get('offer_id'),'name':x.get('name'),'reported_family_product_count':x.get('model_info',{}).get('count'),'has_observed_noncancelled_sales':any(l['product_id']==pid for l in lines),'membership_basis':'Seller product/info model_info, current snapshot 2026-11-03; not historical','catalog_completeness':'members without sales outside target set not enumerated; all SKU with observed noncancelled account sales classified'})
write('revised_family_members.csv',familyrows)
summary={'coverage':coverage,'unique_raw_postings':len(postingkeys),'cancelled_excluded':cancelled,'noncancelled_sales_lines':len(lines),'sold_sku_metadata_unmapped_lines':sum(not l['product_id'] for l in lines),'target_current_model_count':len(models),'target_skus':20,'target_product_ids':len(targetpids),'current_family_member_rows':len(familyrows),'orders_with_multiple_process_dates':multiday,'time_basis':'in_process_at, falling back to created_at, Europe/Moscow; noncancelled status at fetch, not cash realization','family_overlap_handling':'19 current model_id families for 20 target SKU; union uses unique posting product lines, not sum of 20 family totals','family_historical_membership_available':False,'self_sku_alias_policy':'primary self figures use exact SKU; current-card alias scope is separately available'}
save('revised_sales_quality.json',summary)
for scope in ['cabinet','fixed20_exact_sku','families_union_current']:
 x=next(r for r in windowrows if r['scope']==scope and r['baseline_days']==28)
 print(scope,'post value',x['post_total_ordered_value_rub'],'expected',x['expected_post_ordered_value_rub'],'delta%',x['delta_percent_ordered_value_rub'],'orders',x['post_total_orders'],'expected',x['expected_post_orders'])
print('Family models',len(models),'unmapped sold lines',summary['sold_sku_metadata_unmapped_lines'],'cross-day order IDs',multiday)
