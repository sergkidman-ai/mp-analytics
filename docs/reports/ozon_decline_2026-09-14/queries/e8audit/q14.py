import csv,subprocess,io,hashlib
R='/opt/mp-analytics/'
rows=list(csv.DictReader(open(R+'docs/reports/ozon_wave1_restore_oz_acc1_2026-08-19.csv',encoding='utf-8')))
grp={r['sku']:r['group'] for r in rows}
for nm,g in (('E8_treatment','A'),('E8_control','B')):
    p=f'docs/experiments/cohorts/{nm}_2026-08-20.csv'
    blob=subprocess.run(['git','-C',R,'show',f'95996f7:{p}'],capture_output=True).stdout
    work=open(R+p,'rb').read()
    x=list(csv.DictReader(io.StringIO(blob.decode('utf-8'))))
    s={r['sku'] for r in x}; gs={k for k,v in grp.items() if v==g}
    print(nm,'git==work',hashlib.sha256(blob).hexdigest()==hashlib.sha256(work).hexdigest(),'rows',len(x),'sku',len(s),'==csv19 group',s==gs,'diff',len(s^gs),'assigned_group ok',all(r['assigned_group']==g for r in x),'applied false',sum(1 for r in x if r['applied']!='true'))
