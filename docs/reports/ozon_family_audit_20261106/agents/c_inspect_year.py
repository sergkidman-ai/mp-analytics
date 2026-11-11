"""C audit: inspect existing local annual artifacts; never use API/DB or mutate sources."""
import collections
import csv
import datetime as dt
import hashlib
import json
from pathlib import Path

ROOT = Path('/opt/mp-analytics')
SOURCE = ROOT / 'docs/reports/ozon_year_review_20261105'
OUTPUT = Path(__file__).with_name('c_year_readiness.json')
START, END = dt.date(2025, 11, 5), dt.date(2026, 11, 4)
EXPECTED = {str(START + dt.timedelta(days=i)) for i in range(365)}


def read(name):
    return json.loads((SOURCE / name).read_text())


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def csv_coverage(name, key):
    dates, ids, rows, seen, duplicates = set(), set(), 0, set(), 0
    with (SOURCE / name).open(encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        columns = reader.fieldnames
        for r in reader:
            rows += 1
            dates.add(r['date_msk'])
            if 'sku' in r:
                ids.add(r['sku'])
            if 'campaign_id' in r:
                ids.add(r['campaign_id'])
            k = tuple(r[c] for c in key)
            if k in seen:
                duplicates += 1
            seen.add(k)
    return dict(file=name, columns=columns, rows=rows, unique_ids=len(ids),
                date_min=min(dates), date_max=max(dates), observed_days=len(dates),
                missing_expected_days=sorted(EXPECTED - dates), duplicate_natural_keys=duplicates,
                natural_key=key)


coverage = read('order_collection_coverage.json')
days = collections.defaultdict(set)
posting_seen = set()
counts = collections.Counter()
missing_paths, incomplete, overlapping = [], [], []
retrieval_dates = []
for w in coverage['windows']:
    p = Path(w['path'])
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        missing_paths.append(str(p))
        continue
    z = json.loads(p.read_text())
    if not z.get('complete'):
        incomplete.append(str(p))
    retrieval_dates.append(z.get('retrieved_at_utc', ''))
    a, b = dt.date.fromisoformat(w['from_msk']), dt.date.fromisoformat(w['through_msk'])
    wd = {str(a + dt.timedelta(days=i)) for i in range((b-a).days+1)}
    if wd & days[w['scheme']]:
        overlapping.append(str(p))
    days[w['scheme']].update(wd)
    for r in z['postings']:
        counts[w['scheme']] += 1
        k = (w['scheme'], r['posting_number'])
        if k in posting_seen:
            counts['duplicate_posting_keys'] += 1
        posting_seen.add(k)
        counts['cancelled'] += int(r.get('status', '').lower().startswith('cancel'))
        counts['missing_order_id'] += int(not r.get('order_id'))
        for item in r.get('products', []):
            counts['product_lines'] += 1
            for field in ('sku', 'offer_id', 'quantity'):
                counts['product_missing_' + field] += int(item.get(field) is None)
orders = dict(source='order_collection_coverage.json', windows=len(coverage['windows']),
              finished=coverage['finished'], counts=dict(counts), missing_paths=missing_paths,
              incomplete_paths=incomplete, overlapping_window_paths=overlapping,
              coverage_by_scheme={k:dict(days=len(v),missing=sorted(EXPECTED-v),outside=sorted(v-EXPECTED)) for k,v in days.items()},
              retrieval_utc_min=min(filter(None,retrieval_dates)), retrieval_utc_max=max(filter(None,retrieval_dates)))

hash_ok, hash_bad, hash_missing = 0, [], []
for line in (SOURCE / 'SHA256SUMS').read_text().splitlines():
    expected, filename = line.split(maxsplit=1)
    p = Path(filename.lstrip('*'))
    if not p.is_absolute():
        p = SOURCE / p
    if not p.exists():
        hash_missing.append(str(p))
    elif digest(p) != expected:
        hash_bad.append(str(p))
    else:
        hash_ok += 1

layers = [csv_coverage(n, k) for n,k in [
    ('campaign_daily_year.csv', ['campaign_id','date_msk']),
    ('sku_positive_cpc_daily_year.csv', ['sku','date_msk']),
    ('sku_general_sales_daily_year.csv', ['sku','date_msk']),
    ('account_general_sales_daily_year.csv', ['date_msk'])]]
csv_metadata = []
for name in ('annual_sku_profile_currently_in_stock.csv','family_context_for_stock_episodes.csv',
             'sku_transition_summary_in_stock.csv','transition_episodes_currently_in_stock.csv'):
    with (SOURCE/name).open(encoding='utf-8-sig') as f:
        r = csv.DictReader(f)
        columns = r.fieldnames
        count = sum(1 for _ in r)
    csv_metadata.append(dict(file=name, rows=count, columns=columns))

jobs = read('sku_report_jobs.json')
reused = jobs['reused_sku_report_files']
result = dict(
    agent='C', account='oz_acc1', generated_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
    scope='Existing local artifact presence, keys and coverage only; no API/DB requests, effect analysis or source mutations.',
    period=dict(start_msk=str(START),end_msk=str(END),days=365),
    source=str(SOURCE.relative_to(ROOT)), command='./venv/bin/python docs/reports/ozon_family_audit_20261106/agents/c_inspect_year.py',
    inventory=dict(files=sum(1 for p in SOURCE.rglob('*') if p.is_file()),bytes=sum(p.stat().st_size for p in SOURCE.rglob('*') if p.is_file())),
    sha_manifest=dict(source='SHA256SUMS',matched=hash_ok,mismatched=hash_bad,missing=hash_missing),
    orders=orders, normalized_layers=layers, derived_csv_metadata=csv_metadata,
    sku_report_jobs=dict(status=read('sku_collection_status.json'),reused_files_count=len(reused),
                         reused_missing=[p for p in reused if not Path(p).exists()]),
    campaign_coverage=read('campaign_collection_coverage.json'),
    normalization_checkpoint=read('normalization_checkpoint.json'),
    checkpoint_cpc_sha_matches=digest(SOURCE/'sku_positive_cpc_daily_year.csv')==read('normalization_checkpoint.json')['sha256'],
    reconciliation=read('ad_daily_reconciliation.json'), date_added_quality=read('date_added_quality.json'),
    prior_db_coverage_only_not_fresh_snapshot=read('db_history_coverage.json'),
    readiness=dict(
        general_orders='AVAILABLE: complete FBS/FBO collection windows; normalized SKU-day and account-day; ordered value is not realization revenue.',
        cpc='AVAILABLE_WITH_LIMITS: retained campaign registry only; positive SKU-day layer omits zero rows; CPO history/configuration incomplete.',
        family_mapping='CURRENT_MODEL_ID_ONLY: existing context uses current product-info model_id; not large economic family or historical membership.',
        family_unique_orders='RAW_REQUIRED: SKU-day unique_orders cannot be summed; raw contains order_id and product lines.',
        financial_economics='NOT_READY_FOR_FULL_YEAR: no annual financial normalized layer; prior DB coverage starts January 2026 and does not establish realization/COGS readiness.',
        stock='CURRENT_ONLY: year artifacts do not prove historical stock or deduplicated shared physical stock.',
        complete_card_map='PENDING_A_B_AND_COMMON_SNAPSHOT: annual stock lists do not represent all 48,492 catalog cards.'),
    invariants=[
        'Every card must occur exactly once in card map or explicit unknown register.',
        'Current model_id, large economic family, article parent and historical glue are separate relation types.',
        'XN bundle multiplicity is distinct from confirmed component kit composition.',
        'Recognize sale value once per original posting product line; physical component allocations are not additional sales.',
        'Deduplicate family orders by order_id; do not sum unique_orders across SKU.',
        'Deduplicate shared stock by capture, ms_id and store; aliases and bundles do not create stock.',
        'Do not sum repeated family_context episode values across cards/episodes.',
        'Missing campaign/row/identity/stock/composition is unknown, never silently zero.'
    ],
    independent_map_validation=dict(status='PENDING',reason='Awaiting A/B maps and leading-agent shared snapshot; no completed map audit claimed.'),
    sources=[dict(path=str(p.relative_to(ROOT)),sha256=digest(p),mtime_utc=dt.datetime.fromtimestamp(p.stat().st_mtime,dt.timezone.utc).isoformat()) for p in [
        SOURCE/n for n in ['METHOD.md','HANDOFF.md','RESULT.md','order_collection_coverage.json','campaign_collection_coverage.json',
                          'sku_collection_status.json','db_history_coverage.json','normalization_checkpoint.json','ad_daily_reconciliation.json','date_added_quality.json']]]
)
OUTPUT.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(dict(output=str(OUTPUT),sha_matched=hash_ok,sha_mismatched=len(hash_bad),sha_missing=len(hash_missing),
                      order_counts=dict(counts),coverage_days={k:len(v) for k,v in days.items()},
                      layers=[{k:v for k,v in r.items() if k in ('file','rows','unique_ids','observed_days','duplicate_natural_keys')} for r in layers]),ensure_ascii=False))
