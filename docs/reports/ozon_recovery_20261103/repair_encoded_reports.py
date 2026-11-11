# поток: mkt
from acquire import *
for p in sorted(RAW.glob('sku_report*_result.json')):
 b=json.loads(p.read_text())['body']
 if 'text' not in b:continue
 name=p.name[:-len('_result.json')]
 job=json.loads((OUT/(name+'_request.json')).read_text())
 u=job['response']['UUID']
 code,body=get('/api/client/statistics/report',{'UUID':u},p.name)
 print(p.name,'HTTP',code,'decoded', 'text' not in body,'blocks',len(body),flush=True)
