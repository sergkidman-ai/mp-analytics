import json
R='/opt/mp-analytics/data/mpstats_probe/2026-09-14/raw/'
ids={}
for n in ('s2_A_items_0_20','s2_B_items_20_40','s2_C_items_filter_id','stage1_02_wb_subject_items_q2612a'):
    d=json.load(open(R+n+'.json')); rows=d.get('data',[]) if isinstance(d,dict) else []
    meta={k:v for k,v in d.items() if k!='data'} if isinstance(d,dict) else str(d)[:200]
    rv=[r.get('revenue') for r in rows]
    ids[n]=[r['id'] for r in rows]
    print(n,'meta',meta,'rows',len(rows),'first',ids[n][:2],'last',ids[n][-1:], 'sorted_desc',all(rv[i]>=rv[i+1] for i in range(len(rv)-1)), 'rev first/last',rv[:1],rv[-1:])
A,B=set(ids['s2_A_items_0_20']),set(ids['s2_B_items_20_40'])
print('A∩B',len(A&B),'A==stage1[0:20]',ids['s2_A_items_0_20'][:20]==ids['stage1_02_wb_subject_items_q2612a'][:20],'B==stage1[20:40]',ids['s2_B_items_20_40'][:20]==ids['stage1_02_wb_subject_items_q2612a'][20:40])
