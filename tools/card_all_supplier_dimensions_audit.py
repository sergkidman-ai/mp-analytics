# поток: card
"""Read-only live MS catalogue/warehouse audit. Only local report files are written."""
import json
import sys
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import ms_api

OUT = ROOT / 'docs/reports/card_all_supplier_dimensions_2026-10-15'

def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False))

def collect(name, endpoint, params):
    off = 0
    rows = []
    sizes = []
    while True:
        path = OUT / 'raw' / name / f'{off:06}.json'
        if path.exists():
            data = json.loads(path.read_text())
        else:
            data = ms_api.get(endpoint, dict(params, limit=1000, offset=off))
            save(path, data)
        chunk = data.get('rows', [])
        sizes.append(data.get('meta', {}).get('size'))
        rows.extend(chunk)
        off += len(chunk)
        if off % 10000 == 0:
            print(name, off, flush=True)
        if not chunk or off >= data.get('meta', {}).get('size', off):
            break
    save(OUT / f'{name}.json', rows)
    save(OUT / f'{name}_collection.json', {'count':len(rows), 'page_sizes_reported':sizes,
        'completed_at_utc':datetime.now(timezone.utc).isoformat(), 'endpoint':endpoint, 'params':params})
    print(name, 'complete', len(rows), flush=True)
    return rows

def main():
    OUT.mkdir(exist_ok=True)
    # Resume saved pages, then use smaller pages to avoid MS response timeouts.
    products=[]
    off=0
    while (OUT/'raw/products'/f'{off:06}.json').exists():
        data=json.loads((OUT/'raw/products'/f'{off:06}.json').read_text())
        products.extend(data['rows'])
        off+=len(data['rows'])
    expected=ms_api.get('/entity/product',{'filter':'archived=false','limit':1})['meta']['size']
    def page(offset):
        data=ms_api.get('/entity/product',{'filter':'archived=false','order':'id,asc','limit':200,'offset':offset})
        save(OUT/'raw/products'/f'{offset:06}.json',data)
        return data
    with ThreadPoolExecutor(max_workers=3) as pool:
        for data in pool.map(page,range(off,expected,200)):
            products.extend(data['rows'])
            if len(products)%5000==0: print('products',len(products),flush=True)
    assert len(products)==expected and len({p['id'] for p in products})==expected
    save(OUT/'products.json',products)
    save(OUT/'products_collection.json',{'count':len(products),'completed_at_utc':datetime.now(timezone.utc).isoformat(),
        'scope':'all active entity/product; archived=false; order=id,asc'})
    # The short current report returns per-warehouse stocks directly, including negatives.
    for name,sid in [('remote','04bba1be-cb03-11ea-0a80-01600001ca2c'),
                     ('zvezdny','e0d74d6f-db92-11ee-0a80-0cda000a40b5'),
                     ('discover','d9f78430-0879-11f0-0a80-08bb001470db')]:
        data=ms_api.get('/report/stock/bystore/current',{'filter':'storeId='+sid})
        save(OUT/f'stock_current_{name}.json',data)
    save(OUT/'stocks_collection.json',{'completed_at_utc':datetime.now(timezone.utc).isoformat(),
        'endpoint':'/report/stock/bystore/current','warehouses':['remote','zvezdny','discover']})
    supplier_ids = sorted({ms_api.meta_id(p,'supplier') for p in products} - {None})
    supplier_refs = {ms_api.meta_id(p,'supplier'):p['supplier']['meta']['href'] for p in products if p.get('supplier')}
    suppliers = []
    for sid in supplier_ids:
        path = OUT / 'raw' / 'suppliers' / f'{sid}.json'
        if path.exists(): data = json.loads(path.read_text())
        else:
            data = ms_api.get(supplier_refs[sid])
            save(path,data)
        suppliers.append({'id':sid,'name':data.get('name'),'archived':data.get('archived')})
    save(OUT / 'suppliers.json',suppliers)
    archived = ms_api.get('/entity/product', {'filter':'archived=true','limit':1})
    save(OUT / 'archived_count.json', {'count':archived.get('meta',{}).get('size')})
    print('suppliers',len(suppliers),flush=True)

if __name__ == '__main__':
    main()
