# поток: mkt
from collect_checks import *
body={'sku':['1510947813'],'paymentType':'CPC','marketplaceId':'MARKETPLACE_ID_RU'}
r=requests.post(PERF+'/api/client/min/sku',headers=H,json=body,timeout=60)
z={'request':body,'http':r.status_code,'retrieved_at_utc':now(),'response':r.json()};save('raw/floor_schema_probe.json',z)
print('Min SKU probe HTTP',r.status_code,'response',str(z['response'])[:1100])
