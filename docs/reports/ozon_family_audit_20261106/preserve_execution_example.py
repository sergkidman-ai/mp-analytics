# поток: ozon-family. Preserve only structural fields from existing evidence.
import json,pathlib,hashlib,datetime
R=pathlib.Path('/opt/mp-analytics');O=pathlib.Path(__file__).parent/'snapshot'
p=R/'.private/bundle_quantities_20261106/final_authorized_verification.json'
rows=json.loads(p.read_text())
result={'source':str(p.relative_to(R)),'source_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),
 'source_mtime_utc':datetime.datetime.fromtimestamp(p.stat().st_mtime,datetime.timezone.utc).isoformat(),
 'scope':'existing verification of 2 concrete orders, not all SEO/card executions',
 'rows':[{k:r[k] for k in ['number','order_quantity','demand_quantity','bundle','reserve']} for r in rows],
 'offer_id':'6452X3MS22I0','physical_ms_code':'6452at','external_code':'6452',
 'mapping_source':'docs/reports/bundle_quantities_20261106/RESULT.md; named source verifies quantities; no new order modifications'}
(O/'executed_bundle_example.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
print('preserved',len(result['rows']),'structural examples')
