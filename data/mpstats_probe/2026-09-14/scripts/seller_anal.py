# поток: mpstats-probe — разбор завышения выручки наших продавцов (локально по raw)
import json,csv,statistics as st
out=open('derived/seller_top200_flap.csv','w',newline=''); w=csv.writer(out)
w.writerow(['seller_id','nm','name','final_price','mp_sales','mp_revenue','sum_sales_graph','stock_drops_sum','drops_to_zero','zero_days','flap_share_sales','is_fbs'])
summary={}
for sid,tot in (('1334975',177447352),('4382395',134508548)):
    rows=json.load(open(f'raw/s2_wb_seller_{sid}_items.json'))['data']
    flap_sales=0; all_sales=0; flappy=0; rev_flappy=0; revs=0
    for r in rows:
        sg=r.get('sales_graph') or []; stg=r.get('stocks_graph') or []
        drops=sum(max(stg[i-1]-stg[i],0) for i in range(1,len(stg)))
        dz=sum(1 for i in range(1,len(stg)) if stg[i-1]>0 and stg[i]==0)
        zd=sum(1 for x in stg if x==0)
        # продажи в дни, когда остаток упал до нуля
        fs=sum(sg[i] for i in range(1,min(len(sg),len(stg))) if stg[i-1]>0 and stg[i]==0)
        share=fs/max(sum(sg),1)
        flap_sales+=fs; all_sales+=sum(sg); revs+=r['revenue'] or 0
        if dz>=3: flappy+=1; rev_flappy+=r['revenue'] or 0
        w.writerow([sid,r['id'],r['name'][:50],r['final_price'],r['sales'],r['revenue'],sum(sg),drops,dz,zd,round(share,2),r.get('is_fbs')])
    summary[sid]=dict(rows=len(rows),sales_on_drop_to_zero_pct=round(flap_sales/max(all_sales,1)*100,1),sku_with_3plus_zero_drops=flappy,rev_share_of_flappy_pct=round(rev_flappy/revs*100,1),median_price=st.median(r['final_price'] for r in rows))
out.close()
# пересечение карточек двух кабинетов по совпадающему графику остатка
a={tuple(r.get('stocks_graph') or []):r['id'] for r in json.load(open('raw/s2_wb_seller_1334975_items.json'))['data'] if sum(r.get('stocks_graph') or [])>0}
b=[r for r in json.load(open('raw/s2_wb_seller_4382395_items.json'))['data'] if tuple(r.get('stocks_graph') or []) in a]
summary['same_stock_graph_pairs_in_top200']=len(b)
print(json.dumps(summary,ensure_ascii=False,indent=0))
