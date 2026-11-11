"""Check retained annual identifiers against leading-agent immutable catalog union."""
import collections
import csv
import datetime as dt
import hashlib
import json
from pathlib import Path

ROOT = Path('/opt/mp-analytics')
HERE = Path(__file__).parent
YEAR = ROOT / 'docs/reports/ozon_year_review_20261105'
SNAP = HERE.parent / 'snapshot'
aliases = collections.defaultdict(set)
catalog_counts = {}
source_hashes = {}
for name in ('catalog.json', 'catalog_archived.json'):
    p = SNAP/name
    source_hashes[name] = hashlib.sha256(p.read_bytes()).hexdigest()
    records = json.loads(p.read_text())
    catalog_counts[name] = len(records)
    for r in records:
        for s in [r.get('sku')] + [v.get('sku') for v in r.get('sources', [])]:
            if s:
                aliases[str(s)].add(str(r['id']))
    del records

all_missing = []
results = {}
for name in ('sku_general_sales_daily_year.csv', 'sku_positive_cpc_daily_year.csv'):
    months = collections.defaultdict(lambda: collections.Counter())
    month_keys = collections.defaultdict(set)
    month_missing = collections.defaultdict(set)
    keys, missing, ambiguous = set(), set(), set()
    missing_rows = collections.Counter()
    with (YEAR/name).open(encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            sku, month = r['sku'], r['date_msk'][:7]
            keys.add(sku)
            month_keys[month].add(sku)
            months[month]['rows'] += 1
            ids = aliases.get(sku, set())
            if not ids:
                missing.add(sku)
                month_missing[month].add(sku)
                months[month]['missing_rows'] += 1
                missing_rows[sku] += 1
            elif len(ids) > 1:
                ambiguous.add(sku)
                months[month]['ambiguous_rows'] += 1
    results[name] = dict(unique_skus=len(keys), missing_unique_skus=len(missing),
        missing_rows=sum(missing_rows.values()), ambiguous_unique_skus=len(ambiguous),
        missing_skus=sorted(missing), ambiguous_skus=sorted(ambiguous),
        monthly=[dict(month=m,**v,unique_skus=len(month_keys[m]),missing_unique_skus=len(month_missing[m])) for m,v in sorted(months.items())])
    all_missing.extend(dict(layer=name,sku=k,rows=n) for k,n in sorted(missing_rows.items()))

raw_missing = collections.Counter()
raw_keys, raw_missing_ids, raw_offer_disagree = set(), set(), set()
windows = json.loads((YEAR/'order_collection_coverage.json').read_text())['windows']
for w in windows:
    z = json.loads(Path(w['path']).read_text())
    for r in z['postings']:
        raw_missing['postings'] += 1
        for product in r.get('products', []):
            sku = str(product.get('sku') or '')
            raw_keys.add(sku)
            raw_missing['product_lines'] += 1
            if not aliases.get(sku):
                raw_missing_ids.add(sku)
                raw_missing['missing_alias_product_lines'] += 1

result = dict(observed_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
              catalog_counts=catalog_counts,union_aliases=len(aliases),
              ambiguous_union_aliases=sum(len(v)>1 for v in aliases.values()),
              catalog_sha256=source_hashes,normalized_layers=results,
              raw_orders=dict(**raw_missing,unique_skus=len(raw_keys),missing_unique_skus=len(raw_missing_ids),missing_skus=sorted(raw_missing_ids)),
              interpretation='Match to current full+archived catalog only; alias presence does not prove historical card identity, family membership, or composition.')
(HERE/'c_year_key_coverage.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
readiness=json.loads((HERE/'c_year_readiness.json').read_text())
readiness['current_catalog_union_key_coverage']=result
readiness['common_snapshot_financial_coverage']=json.loads((SNAP/'db_year_coverage.json').read_text())
monthly=SNAP/'db_year_monthly_coverage.json'
if monthly.exists():
    readiness['common_snapshot_financial_monthly_coverage']=json.loads(monthly.read_text())
(HERE/'c_year_readiness.json').write_text(json.dumps(readiness,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(dict(catalog_counts=catalog_counts,union_aliases=len(aliases),ambiguous_aliases=result['ambiguous_union_aliases'],
    layer_missing={k:dict(unique_skus=v['unique_skus'],missing_skus=v['missing_unique_skus'],missing_rows=v['missing_rows'],ambiguous_skus=v['ambiguous_unique_skus']) for k,v in results.items()},
    raw_orders=result['raw_orders']),ensure_ascii=False))
