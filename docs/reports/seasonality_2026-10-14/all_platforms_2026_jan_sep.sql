-- поток: fin; read-only; база оборота для премии
with monthly as (
 select 'wb' platform,account,to_char((payload->>'rr_dt')::date,'YYYY-MM') ym,coalesce(sum((payload->>'retail_amount')::numeric) filter(where payload->>'doc_type_name'='Продажа'),0) sales,coalesce(sum((payload->>'retail_amount')::numeric) filter(where payload->>'doc_type_name'='Возврат'),0) returns from raw_wb_report where (payload->>'rr_dt')::date between '2026-01-01' and '2026-09-30' group by 2,3
 union all
 select 'ozon',account,to_char(make_date(year,month,1),'YYYY-MM'),coalesce(sum((row->'delivery_commission'->>'amount')::numeric),0),coalesce(sum((row->'return_commission'->>'amount')::numeric),0) from raw_ozon_realization,lateral jsonb_array_elements(payload->'rows') row where year=2026 and month between 1 and 9 group by 2,3
 union all
 select 'yandex',account,to_char(month,'YYYY-MM'),revenue,returns_sum from yandex_finance_monthly where month >= '2026-01-01' and month < '2026-10-01'
) select platform,account,ym,sales,returns,sales-returns net from monthly order by 1,2,3;
