# поток: ev — official WB reports; legacy snapshot estimates are kept separately.
from pathlib import Path
from decimal import Decimal
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, Response
from core import db

router = APIRouter()
STATIC = Path(__file__).parent / 'static'

@router.get('/warehouse/loss', response_class=HTMLResponse)
def warehouse_loss_page():
    return HTMLResponse((STATIC/'warehouse_loss.html').read_text(),headers={'Cache-Control':'no-cache'})

@router.get('/api/warehouse/loss')
def warehouse_loss():
    rows=db.query('''SELECT d.id document_id,d.account,d.warehouse,d.header_date,d.pdf_name,d.imported_at,
        i.nm_id,i.vendor_code,i.qty,i.source_pages FROM wb_loss_document d
        JOIN wb_loss_item i ON i.document_id=d.id ORDER BY d.warehouse,i.vendor_code,i.nm_id''')
    analyses=db.query('SELECT account,payload,built_at FROM wb_loss_analysis')
    matches={(a['account'],i['document_id'],i['nm_id']):i for a in analyses for i in a['payload']['items']}
    cost_rows=db.query('SELECT account,payload,built_at FROM wb_loss_cost_result')
    cost_map={(a['account'],i['document_id'],i['nm_id']):i for a in cost_rows for i in a['payload']['items']}
    warehouses={}
    for r in rows:
        key=(r['account'],r['document_id'])
        wh=warehouses.setdefault(key,{k:r[k] for k in ('document_id','account','warehouse','header_date','pdf_name')})
        wh.setdefault('items',[]);wh.setdefault('qty',0);wh['qty']+=r['qty']
        wh.setdefault('known_cost_rub',Decimal(0));wh.setdefault('costed_qty',0)
        cost=cost_map.get((r['account'],r['document_id'],r['nm_id']))
        if cost:
            wh['known_cost_rub']+=Decimal(cost['known_cost_rub']);wh['costed_qty']+=cost['costed_qty']
        m=matches.get((r['account'],r['document_id'],r['nm_id']))
        wh['items'].append(dict(nm_id=r['nm_id'],vendor_code=r['vendor_code'],qty=r['qty'],pages=r['source_pages'],
            bom=m['bom'] if m else {},mapping_error=m['mapping_error'] if m else None,
            candidate_count=m['candidate_count'] if m else None,
            reviewed=bool(m),cost={k:cost[k] for k in ('costed_qty','unmatched_qty','missing_cost_qty','known_cost_rub','total_cost_rub','complete')} if cost else None))
    missing=db.query('''SELECT DISTINCT l.account,l.warehouse FROM wb_wh_loss l WHERE NOT EXISTS
        (SELECT 1 FROM wb_loss_document d WHERE d.account=l.account AND d.warehouse=l.warehouse)
        ORDER BY l.account,l.warehouse''')
    return dict(warehouses=list(warehouses.values()),
        total=dict(qty=sum(r['qty'] for r in rows),rows=len(rows),documents=len(warehouses)),
        analysis=[dict(account=a['account'],built_at=a['built_at'],summary=a['payload']['summary'],
          history_note=a['payload']['history_note'],search_pages=a['payload']['search_pages'],
          cutoff_exclusive=a['payload']['cutoff_exclusive']) for a in analyses],
        costs=[dict(account=a['account'],built_at=a['built_at'],summary=a['payload']['summary'],allocation_note=a['payload']['allocation_note']) for a in cost_rows],
        missing_reports=missing,cost_calculated=bool(cost_rows))

@router.get('/api/warehouse/loss/document/{document_id}')
def warehouse_loss_pdf(document_id: str):
    rows=db.query('SELECT pdf,pdf_name FROM wb_loss_document WHERE id=%s',(document_id,))
    if not rows:raise HTTPException(404,'Отчёт не найден')
    return Response(bytes(rows[0]['pdf']),media_type='application/pdf',
        headers={'Content-Disposition':f'inline; filename="{rows[0]["pdf_name"]}"'})

@router.get('/api/warehouse/loss/item/{document_id}/{nm_id}')
def warehouse_loss_item(document_id: str,nm_id: int):
    rows=db.query('''SELECT a.payload FROM wb_loss_analysis a JOIN wb_loss_document d USING(account)
                    WHERE d.id=%s''',(document_id,))
    for row in rows:
        for item in row['payload']['items']:
            if item['document_id']==document_id and item['nm_id']==nm_id:
                costs=db.query('SELECT c.payload FROM wb_loss_cost_result c JOIN wb_loss_document d USING(account) WHERE d.id=%s',(document_id,))
                item['cost']=next((i for c in costs for i in c['payload']['items'] if i['document_id']==document_id and i['nm_id']==nm_id),None)
                chosen={e['demand_id']:e for e in item['cost']['selected']} if item['cost'] else {}
                for e in item['evidence']:
                    e['selected']=e['demand_id'] in chosen
                    e['cost_rub']=chosen.get(e['demand_id'],{}).get('cost_rub')
                return item
    raise HTTPException(404,'Подбор отгрузок ещё не выполнен')
