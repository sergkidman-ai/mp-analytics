# поток: ozon-family-strategy; offline current assortment only.
from pathlib import Path
import json,csv,collections,hashlib
O=Path(__file__).parent;AUD=O.parent/'ozon_family_audit_20261106';S=AUD/'snapshot';Y=O.parent/'ozon_year_review_20261105'
def read(p):return json.loads(p.read_text())
def csvrows(p):return csv.DictReader(p.open(encoding='utf-8-sig'))
current=read(S/'catalog.json');pids={str(x['id']) for x in current};assert len(current)==len(pids)==22536 and not any(x['is_archived'] for x in current)
cards={r['product_id']:r for r in csvrows(AUD/'cards.csv') if r['product_id'] in pids}
models={x['external_code']:x for x in read(S/'db_prc_tc_model.json')}
ctx={x['set_code']:x for x in read(AUD/'agents/a_tc_contexts.json')}
current_codes={x['offer_id'] for x in current};ctx={k:v for k,v in ctx.items() if k in current_codes}
focus=['0005','6629','6642','1327','1328','1329','1330','2985','5425','4308','4309','4310','4311']
exam=[]
for code in focus:
 m=models.get(code,{})
 exam.append({'code':code,'model':m.get('title'),'type':m.get('consumable_type'),'series':m.get('united_title'),'brand':m.get('brand'),'printers':m.get('printer_models'),'raw_printers':m.get('raw',{}).get('printers'),'components':ctx.get(code,{}).get('components'),'raw_keys':list(m.get('raw',{}))})
(O/'boundary_evidence.json').write_text(json.dumps({'current_cards':len(cards),'current_composition_codes':len(ctx),'focus':exam},ensure_ascii=False,indent=2))
for x in exam:print(x['code'],x['model'],x['type'],'series=',x['series'],'printers=',str(x['printers'])[:240],'components=',x['components'])
print('annual CSV schemas')
for n in ['sku_general_sales_daily_year.csv','sku_positive_cpc_daily_year.csv','campaign_daily_year.csv']:
 print(n,next(csvrows(Y/n)).keys())
