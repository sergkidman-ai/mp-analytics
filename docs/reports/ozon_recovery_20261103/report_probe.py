# поток: mkt
from acquire import *
p=json.loads((OUT/'report_probe_request.json').read_text())
u=p['response']['UUID']
code,body=get('/api/client/statistics/'+u,None,'report_probe_status.json')
print('Status',code,'state',body.get('state'))
if body.get('state')=='OK':
 code,body=get('/api/client/statistics/report',{'UUID':u},'report_probe_result.json')
 print('Report HTTP',code,'top keys',list(body)[:12])
 for cid,block in body.items():
  if not isinstance(block,dict):continue
  rows=block.get('report',{}).get('rows') or []
  print('campaign',cid,'rows',len(rows),'fields',list(rows[0]) if rows else [])
