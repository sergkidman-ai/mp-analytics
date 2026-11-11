# поток: mkt
"""Keep historical cohorts verbatim; do not invent a current family T/C mapping."""
import pathlib,json,csv,collections,hashlib,datetime as dt
O=pathlib.Path(__file__).parent;PRE=O.parent;ROOT=PRE.parent;PROJECT=pathlib.Path('/opt/mp-analytics')
rows=json.loads((O/'corrected_eligibility.json').read_text());skurow={r['sku']:r for r in rows};byid={};alias={}
for p in sorted((PRE/'raw').glob('*_info_*.json')):
 z=json.loads(p.read_text())
 for x in z['body'].get('items',[]):
  pid=str(x['id']);byid[pid]=x
  for s in [x.get('sku')]+[v.get('sku') for v in x.get('sources',[])]:
   if s:alias[str(s)]=pid
relevant={r['model_id'] for r in rows};ledger=collections.defaultdict(list);exact=[];sourceaudit=[]
for exp in ['E5','E8']:
 for arm in ['treatment','control']:
  p=PROJECT/f'docs/experiments/cohorts/{exp}_{arm}_2026-08-20.csv'
  if not p.exists():continue
  with p.open() as f:rr=list(csv.DictReader(f))
  sourceaudit.append({'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'experiment':exp,'arm':arm,'temporal_scope':'historical Aug/Sep 2026; not a November family allocation'})
  for x in rr:
   if x.get('account','oz_acc1')!='oz_acc1':continue
   s=x.get('sku','');pid=alias.get(s);mid=str(byid[pid]['model_info']['model_id']) if pid else None
   if s in skurow or mid in relevant:
    rec={'experiment':exp,'historical_arm':arm,'sku':s,'current_model_id':mid,'exact_recovered_sku':s in skurow,'stream':skurow[s]['stream'] if s in skurow else None,'assigned_group_raw':x.get('assigned_group'),'applied_raw':x.get('applied'),'source':str(p),'scope':'preserved historical label; current family map is separate and unknown'}
    exact.append(rec)
    if mid in relevant:ledger[mid].append(rec)
familyrows=[]
for mid in sorted(relevant):
 evidence=ledger[mid];perexp={exp:sorted({x['historical_arm'] for x in evidence if x['experiment']==exp}) for exp in ['E5','E8']}
 familyrows.append({'family_key':'oz_acc1:model_id:'+mid,'model_id':mid,'recovered_skus':sorted(r['sku'] for r in rows if r['model_id']==mid),'streams':sorted({r['stream'] for r in rows if r['model_id']==mid}),'current_wave_assignment':None,'current_assignment_status':'UNRESOLVED_NO_NOVEMBER_SOURCE','historical_labels_by_experiment':perexp,'historical_evidence':evidence,'historical_labels_changed':False,'family_split_into_experimental_arms':False,'assignment_rule':'delivery campaign or stream never creates a T/C arm; all family members must use the same confirmed current assignment'})
(O/'family_assignment_ledger.json').write_text(json.dumps(familyrows,ensure_ascii=False,indent=2)+'\n')
if exact:
 with (O/'historical_assignment_evidence.csv').open('w',newline='') as f:
  w=csv.DictWriter(f,fieldnames=list(exact[0]));w.writeheader();w.writerows(exact)
status={'sources':sourceaudit,'closeout_source':str(PROJECT/'docs/reports/ozon_codex_handoff_2026-10-10/HANDOFF.md'),'closeout_sha256':hashlib.sha256((PROJECT/'docs/reports/ozon_codex_handoff_2026-10-10/HANDOFF.md').read_bytes()).hexdigest(),'interpretation':['E8 explicitly closed 18 September and not reopened per 10 October handoff; do not import historical E8 arm into November as a new assignment','E5 measurement window elapsed and verdict inconclusive; preserve its historical labels and do not declare them replaced','no confirmed November first-wave family T/C allocation found in restored sources'],'exact_recovered_history_overlap':dict(collections.Counter(x['experiment']+':'+x['historical_arm'] for x in exact if x['exact_recovered_sku'])),'current_family_assignments_invented':0,'historical_files_modified':False,'current_assignment_confirmation_required_before_experimental_activation':True,'family_count':len(relevant)}
(O/'assignment_preservation_audit.json').write_text(json.dumps(status,ensure_ascii=False,indent=2)+'\n')
print('Historical exact overlaps:',status['exact_recovered_history_overlap'])
print('Family ledger',len(familyrows),'current allocations invented 0; historical labels unchanged')
