# поток: mkt
from acquire import *
coverage=json.loads((OUT/'product_coverage.json').read_text())
for v in coverage:
 if v['http']==200:continue
 cid=v['campaign_id'];code,body=get(f'/api/client/campaign/{cid}/v2/products',{'page':1,'pageSize':1000},f'products_error_{cid}.json')
 print('Composition unavailable',cid,'HTTP',code,'message',str(body)[:160],flush=True)
 time.sleep(.2)
