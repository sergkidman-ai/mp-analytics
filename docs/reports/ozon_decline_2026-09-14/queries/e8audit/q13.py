import sys
sys.path.insert(0,'/opt/mp-analytics')
from core import db
S='/tmp/claude-0/-opt-mp-analytics/5a4defa5-581d-4f27-973a-eafc45f36cb9/scratchpad/e8audit/'
out=open(S+'q13_out.txt','w')
def P(*a):
    s=' '.join(str(x) for x in a); print(s[:300]); out.write(s+'\n')
for r in db.query("""select period_start, period_end, count(*) n, sum((updated_at>'2026-08-19 14:45+00')::int) late, min(updated_at) u0, max(updated_at) u1
  from ozon_search_product where account='oz_acc1' and period_end<='2026-08-19' and period_start>='2026-07-20' and period_end-period_start=7 group by 1,2 order by 1"""):
    P('SP',r['period_start'],r['period_end'],r['n'],'late',r['late'],str(r['u0'])[:16],str(r['u1'])[:16])
for r in db.query("""select date_trunc('month',in_process_at)::date m, count(*) n, min(loaded_at) l0, max(loaded_at) l1 from raw_ozon_posting where account='oz_acc1' and in_process_at>='2026-07-27' and in_process_at<'2026-08-19' group by 1"""):
    P('POST',r['m'],r['n'],str(r['l0'])[:16],str(r['l1'])[:16])
P('posting rows total acc1 Aug', db.query("select count(*) n from raw_ozon_posting where account='oz_acc1' and in_process_at>='2026-08-01' and in_process_at<'2026-09-01'")[0])
