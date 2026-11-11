# поток: ozon-family-strategy; exact money conservation, unique original orders.
from common import *
import collections
cards=list(rows(O/'cards.csv'));byid={c['product_id']:c for c in cards};h=read(O/'history_by_card.json');defs=read(O/'family_definitions.json')
assigned=collections.defaultdict(list);candidate=collections.defaultdict(set)
for c in cards:
 if c['family_id']:assigned[c['family_id']].append(c)
 else:
  for f in json.loads(c['candidate_family_ids']):candidate[f].add(c['product_id'])
lines=list(rows(O/'sales_lines.csv'));familylines=collections.defaultdict(list)
for x in lines:
 f=byid[x['product_id']]['family_id']
 if f:familylines[f].append(x)
out=[]
for fid,f in defs.items():
 cs=assigned[fid];orders={};days=set();weeks=set();months=set();sale=0;units=0;phys=0;phys_unknown=0;recent=collections.Counter()
 for x in familylines[fid]:
  day=x['date_msk'];oid=x['order_id'];orders[oid]=min(day,orders.get(oid,day));days.add(day);d=datetime.date.fromisoformat(day);weeks.add(d.isocalendar()[:2]);months.add(day[:7]);sale+=int(x['ordered_value_kopeks']);units+=int(x['market_units'])
  cc=byid[x['product_id']];comp=json.loads(cc['physical_components'])
  if comp:phys+=int(x['market_units'])*sum(comp.values())
  else:phys_unknown+=int(x['market_units'])
  delta=(datetime.date.fromisoformat(END)-d).days
  if delta<90:recent['last90_value']+=int(x['ordered_value_kopeks'])
  elif delta<180:recent['previous90_value']+=int(x['ordered_value_kopeks'])
 firstcreated=min([x['created_at'][:10] for x in cs if x['created_at']]+['9999-99-99'])
 firstpositive=min(list(days)+[h[c['product_id']]['first_cpc_evidence'] for c in cs if h[c['product_id']]['first_cpc_evidence']]+['9999-99-99'])
 start=min(firstcreated,firstpositive);age=max(0,min(365,(datetime.date.fromisoformat(END)-datetime.date.fromisoformat(start)).days+1)) if start!='9999-99-99' else 0
 recentorders=sum((datetime.date.fromisoformat(END)-datetime.date.fromisoformat(d)).days<90 for d in orders.values())
 previousorders=sum(90<=(datetime.date.fromisoformat(END)-datetime.date.fromisoformat(d)).days<180 for d in orders.values())
 pending=candidate[fid];related=set(f['related_question_ids']);complete=not pending and not related
 out.append({'family_id':fid,'family_name':f['family_name'],'functional_role':f['functional_role'],'parent_codes':js(f['parent_codes']),'composition_codes':js(f['composition_codes']),
 'assigned_current_cards':len(cs),'pending_candidate_cards':len(pending),'membership_status':'corroborated_series_memberships','boundary_status':f['boundary_status'],
 'map_completeness':'listed_members_complete_no_known_candidates' if complete else 'incomplete_boundary_or_candidate_members','question_ids':js(sorted(related)),
 'annual_unique_orders':len(orders),'selling_days':len(days),'selling_weeks':len(weeks),'selling_months':len(months),'market_units':units,'physical_units_known':phys,'market_units_unknown_physical_count':phys_unknown,
 'ordered_value_rub':format(D(sale)/100,'.2f'),'cpc_spend_rub':format(D(sum(h[c['product_id']]['cpc_cents'] for c in cs))/100,'.2f'),
 'orders_last90':recentorders,'orders_previous90':previousorders,'ordered_value_last90_rub':format(D(recent['last90_value'])/100,'.2f'),'ordered_value_previous90_rub':format(D(recent['previous90_value'])/100,'.2f'),
 'earliest_creation_metadata':firstcreated,'first_positive_evidence':firstpositive,'observed_age_days':age,'novelty_status':'new_or_insufficient_observation' if age<90 else 'partial_year_observation' if age<365 else 'full_year_eligible',
 'historical_availability':'unknown','annual_financial_profit':'','financial_status':'not_computed_incomplete_sources; missing_not_zero',
 'demand_class':'pending_empirical_rule','classification_status':'preliminary_observed_account_demand','source':'cards.csv;sales_lines.csv;existing normalized CPC reconciled to original reports',
 'period_start':START,'period_end':END,'membership_observed_at':'2026-11-06; current classification applied to retained historical facts, not historical glue'})
write('families.csv',out)
baseline=read(O/'history_reconciliation.json');assigned_sales=sum(cents(x['ordered_value_rub']) for x in out);assigned_cpc=sum(cents(x['cpc_spend_rub']) for x in out)
un=[c for c in cards if not c['family_id']];un_sale=sum(h[c['product_id']]['sales_cents'] for c in un);un_cpc=sum(h[c['product_id']]['cpc_cents'] for c in un)
assert assigned_sales+un_sale==baseline['selected_ordered_value_cents'];assert assigned_cpc+un_cpc==baseline['selected_cpc_cents']
assert sum(int(x['market_units']) for x in out)+sum(h[c['product_id']]['units'] for c in un)==baseline['selected_market_units']
counts=sorted([int(x['annual_unique_orders']) for x in out if int(x['annual_unique_orders'])>0])
dist={'families':len(out),'with_observed_orders':len(counts),'without_observed_orders':len(out)-len(counts),'order_count_frequency':dict(sorted(collections.Counter(counts).items())),
 'top_order_counts':counts[-50:][::-1],'selling_months_frequency':dict(sorted(collections.Counter(int(x['selling_months']) for x in out if int(x['annual_unique_orders'])>0).items())),
 'no_rule_selected_yet':True}
save('demand_distribution.json',dist)
save('allocation_reconciliation.json',{'selected_current_ordered_value_cents':baseline['selected_ordered_value_cents'],'assigned_family_ordered_value_cents':assigned_sales,'unassigned_ordered_value_cents':un_sale,
 'selected_current_cpc_cents':baseline['selected_cpc_cents'],'assigned_family_cpc_cents':assigned_cpc,'unassigned_cpc_cents':un_cpc,'assigned_sales_share':assigned_sales/baseline['selected_ordered_value_cents'],
 'assigned_cpc_share':assigned_cpc/baseline['selected_cpc_cents'],'money_difference_cents':0,'market_units_difference':0,
 'orders_not_additive_across_families':'same original order can contain products from multiple families; each family uses distinct original order_id',
 'candidate_families_receive_zero_allocations':True,'components_receive_no_extra_sales':True})
print(js(read(O/'allocation_reconciliation.json')));print('Distribution',js({k:dist[k] for k in ['families','with_observed_orders','without_observed_orders','top_order_counts','selling_months_frequency']}))
