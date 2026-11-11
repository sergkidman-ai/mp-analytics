# поток: ozon-family; offline snapshot audit, writes only agents/b_* artifacts.
"""Audit every captured card and positive model_id without querying API or DB."""
import collections
import csv
import hashlib
import json
import pathlib

HERE = pathlib.Path(__file__).parent
SNAP = HERE.parent / 'snapshot'


def read(p):
    return json.loads(p.read_text())


def dump(name, value):
    (HERE / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def csvout(name, rows, fields):
    with (HERE / name).open('w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def packed(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def valid_id(x):
    return isinstance(x, int) and not isinstance(x, bool) and x > 0


def canon_values(a):
    return sorted(({'dictionary_value_id': v.get('dictionary_value_id'), 'value': v.get('value')}
                   for v in a.get('values', [])), key=packed)


def is_merge_name(label):
    return 'для объединения' in label.lower() or label.lower() == 'объединить на одной карточке'


def load_attrs(schemas):
    """Read finalized leader outputs if present; preserve original source/time."""
    attrs, provenance, duplicates = {}, {}, []
    files = sorted(SNAP.glob('attributes_*.json')) + sorted(SNAP.glob('attrs_*.json'))
    for f in files:
        z = read(f)
        if not isinstance(z, dict) or 'body' not in z:
            continue
        body = z['body']
        rr = body.get('result', []) if isinstance(body, dict) else body
        if isinstance(rr, dict):
            rr = rr.get('items', [])
        for a in rr:
            pid = str(a.get('id', a.get('product_id', '')))
            if pid in attrs:
                duplicates.append(pid)
            pair = (str(a.get('description_category_id')), str(a.get('type_id')))
            schema = schemas.get(pair, {})
            keep_values = {aid for aid, d in schema.items() if d.get('is_aspect') or d.get('is_required')
                           or is_merge_name(d.get('name', '')) or d.get('name', '').lower() in
                           {'бренд', 'ресурс', 'совместимые модели принтеров', 'бренд печатающего устройства',
                            'тип расходного материала для печати', 'тип', 'наличие чипа', 'с чипом'}}
            attrs[pid] = {'attributes': [v if str(v['id']) in keep_values else
                          {'id': v['id'], 'values': [{'value': 'PRESENT'}] if v.get('values') else []}
                          for v in a.get('attributes', [])], 'model_info': a.get('model_info')}
            provenance[pid] = {'source': 'snapshot/' + f.name, 'observed_at': z.get('observed_at', '')}
    return attrs, provenance, duplicates


def load_schemas():
    schemas, sources = {}, {}
    for f in sorted(SNAP.glob('category_schema_*.json')):
        z = read(f)
        if not isinstance(z, dict) or 'request' not in z or 'body' not in z or z.get('http') != 200:
            continue
        req = z['request']
        pair = (str(req.get('description_category_id')), str(req.get('type_id')))
        result = z['body'].get('result', [])
        if isinstance(result, list):
            schemas[pair] = {str(a['id']): a for a in result}
            sources[pair] = 'snapshot/' + f.name
    return schemas, sources


def main():
    cards, prov, coverages = [], {}, []
    for suffix in ['', '_archived']:
        cards.extend({k: v for k, v in x.items() if k in
                     {'id', 'offer_id', 'sku', 'name', 'model_info', 'sources', 'is_archived',
                      'is_autoarchived', 'description_category_id', 'type_id', 'statuses', 'errors'}}
                     for x in read(SNAP / ('catalog' + suffix + '.json')))
        for p in read(SNAP / ('catalog' + suffix + '_provenance.json')):
            prov[str(p['product_id'])] = p
        coverages.append(read(SNAP / ('catalog' + suffix + '_coverage.json')))
    pids = [str(x['id']) for x in cards]
    assert len(pids) == len(set(pids)), 'Overlapping/duplicate cards must be resolved by leader'
    coverage_ok = all(not c['missing_info'] and not c['unexpected_info'] and not c['list_duplicates']
                      and not c['info_duplicates'] and c['unique_listed'] == c['unique_info']
                      and c['final_page_short'] for c in coverages)
    assert coverage_ok, 'Complete active/archive catalog required'
    schemas, schema_sources = load_schemas()
    attrs, attrprov, attrdups = load_attrs(schemas)
    groups, invalid = collections.defaultdict(list), []
    aliases = collections.defaultdict(set)
    for x in cards:
        pid = str(x['id'])
        mid = (x.get('model_info') or {}).get('model_id')
        if valid_id(mid):
            groups[str(mid)].append(x)
        else:
            invalid.append(x)
        for s in [x.get('sku')] + [s.get('sku') for s in x.get('sources', [])]:
            if s:
                aliases[str(s)].add(pid)
    exceptions, group_rows, member_rows, group_lookup = [], [], [], {}

    def ex(kind, scope, key, detail, severity='requires_check', source=''):
        exceptions.append(dict(exception_type=kind, scope=scope, key=key, severity=severity,
                               detail=detail, evidence_source=source, confirmed_violation=False))

    for mid, rr in sorted(groups.items(), key=lambda kv: int(kv[0])):
        cc = [(x.get('model_info') or {}).get('count') for x in rr]
        validcounts = all(valid_id(n) for n in cc)
        unique = sorted(set(cc), key=str)
        state = 'count_missing_or_invalid' if not validcounts else 'inconsistent_reported_count' if len(unique) != 1 else 'count_matches_observed' if unique[0] == len(rr) else 'observed_less_than_reported' if unique[0] > len(rr) else 'observed_greater_than_reported'
        observed_singleton = len(rr) == 1
        crosssource_disagreements = sum(isinstance(attrs.get(str(x['id']), {}).get('model_info'), dict)
                                       and attrs[str(x['id'])]['model_info'] != (x.get('model_info') or {}) for x in rr)
        singleton_api = observed_singleton and state == 'count_matches_observed' and unique == [1] and not crosssource_disagreements
        pairs = sorted({(str(x.get('description_category_id')), str(x.get('type_id'))) for x in rr})
        attr_rows = [attrs[str(x['id'])] for x in rr if str(x['id']) in attrs]
        schema_ok = all(pair in schemas for pair in pairs)
        names, brands = set(), set()
        aspects, required_missing, attrs_unknown = {}, [], set()
        structural = collections.defaultdict(set)
        for x in rr:
            pid = str(x['id'])
            pair = (str(x.get('description_category_id')), str(x.get('type_id')))
            schema = schemas.get(pair, {})
            a = attrs.get(pid)
            if not a:
                continue
            values = {str(v['id']): canon_values(v) for v in a.get('attributes', [])}
            for aid, val in values.items():
                label = schema.get(aid, {}).get('name', '').lower()
                if is_merge_name(label):
                    names.add(packed(val))
                if label == 'бренд':
                    brands.add(packed(val))
                if schema and aid not in schema:
                    attrs_unknown.add(aid)
                if label in {'ресурс', 'совместимые модели принтеров', 'бренд печатающего устройства',
                             'тип расходного материала для печати', 'тип', 'наличие чипа', 'с чипом'}:
                    structural[schema[aid]['name']].add(packed(val))
            sig = {aid: values.get(aid, []) for aid, d in schema.items() if d.get('is_aspect')}
            aspects[pid] = packed(sig) if schema else ''
            for aid, d in schema.items():
                if d.get('is_required') and not values.get(aid):
                    required_missing.append(pid + ':' + aid)
        aspect_sigs = collections.Counter(v for v in aspects.values() if v)
        nactive = sum(not (x.get('is_archived') or x.get('is_autoarchived')) for x in rr)
        row = dict(model_id=mid, observed_products=len(rr), nonarchived_products=nactive,
                   archived_products=len(rr) - nactive, api_reported_counts=packed(unique),
                   count_state=state, api_singleton_confirmed=singleton_api,
                   model_info_crosssource_disagreements=crosssource_disagreements,
                   singleton_scope='API snapshot only' if singleton_api else '',
                   observed_multimember=len(rr) > 1, storefront_membership_verified=False,
                   category_type_pairs=packed(pairs), category_type_pair_count=len(pairs),
                   attributes_covered_products=len(attr_rows), schema_pairs_covered=sum(pair in schemas for pair in pairs),
                   schema_pairs_total=len(pairs), model_name_variant_count=len(names),
                   brand_variant_count=len(brands), aspect_signature_count=len(aspect_sigs),
                   duplicate_aspect_signature_products=sum(n for n in aspect_sigs.values() if n > 1),
                   required_attribute_array_absences=len(required_missing), unknown_attribute_ids=packed(sorted(attrs_unknown)),
                   variable_nonaspect_structural_fields_json=packed({label: len(vals) for label, vals in structural.items() if len(vals) > 1}),
                   content_review_status='requires_category_and_content_review' if len(rr) > 1 else 'not_assessed_as_merge',
                   model_name_is_membership_evidence=False, confirmed_violation=False,
                   member_product_ids=';'.join(sorted(str(x['id']) for x in rr)),
                   first_observed_at=min(prov[str(x['id'])]['observed_at'] for x in rr),
                   last_observed_at=max(prov[str(x['id'])]['observed_at'] for x in rr))
        group_rows.append(row)
        group_lookup[mid] = row
        if state != 'count_matches_observed':
            ex(state, 'model_id', mid, f'observed={len(rr)}; reported={packed(unique)}; count scope not verified in official API docs')
        if len(pairs) > 1:
            ex('multiple_category_type_pairs', 'model_id', mid, packed(pairs))
        if len(brands) > 1:
            ex('multiple_brand_attribute_values', 'model_id', mid, 'Attribute-level variation; requires checking product brand/content, not automatic violation')
        if len(names) > 1:
            ex('multiple_model_name_attribute_values', 'model_id', mid, 'Name variation is diagnostic only; actual membership comes from model_id')
        if any(len(vals) > 1 for vals in structural.values()):
            ex('nonaspect_structural_field_variation', 'model_id', mid,
               packed({label: len(vals) for label, vals in structural.items() if len(vals) > 1}) + '; content/category review required; no automatic violation')
        if len(rr) > 1 and len(attr_rows) != len(rr):
            ex('attributes_missing_for_group_members', 'model_id', mid, f'observed={len(rr)}; attributes_covered={len(attr_rows)}')
        if len(rr) > 1 and not schema_ok:
            ex('category_schema_missing', 'model_id', mid, packed([p for p in pairs if p not in schemas]))
        if len(rr) > 1 and schema_ok and len(attr_rows) == len(rr) and any(n > 1 for n in aspect_sigs.values()):
            ex('duplicate_aspect_signatures', 'model_id', mid, 'Some captured members have identical schema aspect values; verify required separating fields and storefront')

    for x in cards:
        pid = str(x['id'])
        mi = x.get('model_info') or {}
        mid = str(mi['model_id']) if valid_id(mi.get('model_id')) else ''
        a = attrs.get(pid, {})
        pair = (str(x.get('description_category_id')), str(x.get('type_id')))
        schema = schemas.get(pair, {})
        values = {str(v['id']): canon_values(v) for v in a.get('attributes', [])}
        model_names = {aid: val for aid, val in values.items()
                       if is_merge_name(schema.get(aid, {}).get('name', ''))}
        brand = {aid: val for aid, val in values.items() if schema.get(aid, {}).get('name', '').lower() == 'бренд'}
        aspect_values = {aid: values.get(aid, []) for aid, d in schema.items() if d.get('is_aspect')}
        ownaliases = sorted({str(s) for s in [x.get('sku')] + [s.get('sku') for s in x.get('sources', [])] if s})
        gr = group_lookup.get(mid, {})
        ami = a.get('model_info')
        crosssource_equal = mi == ami if isinstance(ami, dict) else None
        member_rows.append(dict(account='oz_acc1', product_id=pid, offer_id=x.get('offer_id', ''),
            sku=x.get('sku', ''), sku_aliases=';'.join(ownaliases), name=x.get('name', ''),
            model_info_present='model_info' in x, model_info_json=packed(x.get('model_info')),
            model_id=mid, reported_count=mi.get('count', ''), observed_group_products=gr.get('observed_products', ''),
            count_state=gr.get('count_state', 'model_id_missing_or_zero'),
            membership_evidence='positive_model_info.model_id' if mid else 'unknown',
            api_singleton_confirmed=gr.get('api_singleton_confirmed', False), storefront_membership_verified=False,
            is_archived=x.get('is_archived'), is_autoarchived=x.get('is_autoarchived'),
            description_category_id=pair[0], type_id=pair[1],
            status_json=packed(x.get('statuses')), errors_json=packed(x.get('errors')),
            attributes_present=bool(a), attributes_count=len(a.get('attributes', [])),
            model_name_attributes_json=packed(model_names), brand_attributes_json=packed(brand),
            category_aspect_values_json=packed(aspect_values),
            attributes_model_info_json=packed(ami), model_info_crosssource_equal=crosssource_equal,
            attributes_source=attrprov.get(pid, {}).get('source', ''),
            attributes_observed_at=attrprov.get(pid, {}).get('observed_at', ''),
            schema_source=schema_sources.get(pair, ''),
            evidence_source=prov[pid]['source'], observed_at=prov[pid]['observed_at']))
        if not mid:
            ex('model_id_missing_or_zero', 'product_id', pid, 'No positive model_id: separate-card status unknown; do not form a common zero-id group', source=prov[pid]['source'])
        if crosssource_equal is False:
            ex('model_info_differs_between_info_and_attributes', 'product_id', pid,
               'info=' + packed(mi) + '; attributes=' + packed(ami) + '; snapshots are sequential, no historical transition inferred',
               source=prov[pid]['source'] + ';' + attrprov.get(pid, {}).get('source', ''))
    for sku, pp in aliases.items():
        if len(pp) > 1:
            ex('sku_alias_multiple_products', 'sku', sku, ';'.join(sorted(pp)))
    for pid in sorted(set(attrdups)):
        ex('duplicate_attribute_product_id', 'product_id', pid, 'Repeated attributes in snapshot files')
    assert len(member_rows) == len(cards)
    assert sum(r['observed_products'] for r in group_rows) + len(invalid) == len(cards)
    assert len(group_rows) == len({r['model_id'] for r in group_rows})
    csvout('b_glue_members.csv', member_rows, list(member_rows[0]))
    csvout('b_glue_groups.csv', group_rows, list(group_rows[0]))
    csvout('b_exceptions.csv', exceptions, ['exception_type', 'scope', 'key', 'severity', 'detail', 'evidence_source', 'confirmed_violation'])
    summary = dict(account='oz_acc1', products=len(cards), nonarchived_catalog_products=coverages[0]['unique_info'],
        archived_catalog_products=coverages[1]['unique_info'], catalog_coverage_ok=coverage_ok,
        positive_model_id_groups=len(groups), zero_or_missing_model_products=len(invalid),
        multimember_groups=sum(r['observed_products'] > 1 for r in group_rows),
        api_singletons=sum(r['api_singleton_confirmed'] for r in group_rows),
        count_states=dict(collections.Counter(r['count_state'] for r in group_rows)),
        attributes_products=len(attrs), schema_pairs=len(schemas),
        exceptions_by_type=dict(collections.Counter(r['exception_type'] for r in exceptions)),
        confirmed_content_violations=0, storefront_membership_verified=False,
        input_sha256={f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in
                      [SNAP/'catalog.json', SNAP/'catalog_archived.json', SNAP/'catalog_coverage.json', SNAP/'catalog_archived_coverage.json']})
    dump('b_summary.json', summary)
    print(packed(summary))


if __name__ == '__main__':
    main()
