#!/usr/bin/env python3
# поток: card
"""Read-only Bulat short-name fallback after owner clarified feed precedence."""
import csv
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.card_bulat_description_audit import classes, normalized

OUT = ROOT / 'docs/reports/card_bulat_current_description_review_2026-10-12'
FALLBACK = {'EMPTY_SOURCE_DESCRIPTION', 'NO_SAFE_TITLE_BOUNDARY'}
OWNER_SHORT_NAME_OVERRIDE = {'AFPMBP5100030', 'AFRX00B230010'}


def firmware(text):
    return bool(re.search(r'прошив|до версии|firmware|fw\s*\d', text, re.I))


def pages(text):
    values = re.findall(r'(?<![A-Za-zА-Яа-я\d-])(\d+(?:[.,]\d+)?)\s*[kк]\b', text, re.I)
    if values:
        return round(float(values[-1].replace(',', '.')) * 1000)
    values = re.findall(r'(?<![\d.])(\d{1,3}(?:[ .]\d{3})+|\d{3,6})\s*стр\b', text, re.I)
    return int(re.sub(r'[ .]', '', values[-1])) if values else None


def main():
    review = json.loads((OUT / 'review.json').read_text())
    tc = {str(r['external_code']): r for r in json.loads((OUT / 'tc_scope.json').read_text())}
    fallback, holds = [], []
    for r in review:
        if r['status'] not in FALLBACK:
            continue
        short = normalized(r['source_name'])
        why = []
        if not short:
            why.append('NO_SHORT_NAME')
        elif r['article'] != r['source_id']:
            why.append('NOT_EXACT_FEED_ID')
        elif r['article'] not in OWNER_SHORT_NAME_OVERRIDE and classes(r['old_description']) and classes(short) != classes(r['old_description']):
            why.append('CHIP_DETAIL_WOULD_CHANGE')
        elif r['article'] not in OWNER_SHORT_NAME_OVERRIDE and firmware(r['old_description']) and not firmware(short):
            why.append('FIRMWARE_DETAIL_WOULD_DISAPPEAR')
        sr, mr = pages(short), pages(r['old_description'])
        tcr = tc.get(str(r['external_code']), {}).get('resource')
        if short and sr and mr and abs(sr-mr)/max(sr,mr) > .1 and tcr != sr:
            why.append('LARGE_RESOURCE_CONFLICT_NOT_CONFIRMED_BY_TC')
        result = {'external_code':r['external_code'], 'article':r['article'], 'id':r['id'],
                  'stock':r['stock'], 'entered':r['entered'], 'source_id':r['source_id'],
                  'old_description':r['old_description'], 'short_name':short,
                  'proposed_description':short if not why else '', 'hold_reason':';'.join(why),
                  'old_pages':mr, 'short_pages':sr, 'tc_pages':tcr,
                  'owner_short_name_override':r['article'] in OWNER_SHORT_NAME_OVERRIDE}
        (holds if why else fallback).append(result)
    assert len(fallback) == 130 and len(holds) == 85
    offers = {o.get('id'):o for o in ET.fromstring((OUT/'clientapi.xml').read_bytes()).findall('.//offer')}
    resolved = []
    for r in review:
        if r['status'] != 'AMBIGUOUS_CURRENT_FEED_ARTICLE':
            continue
        o = offers.get(r['article'])
        assert o is not None and float(o.findtext('count') or 0) > 0
        short = normalized(o.findtext('name'))
        resolved.append({'external_code':r['external_code'], 'article':r['article'], 'id':r['id'],
                         'stock':float(o.findtext('count')), 'exact_feed_id':o.get('id'),
                         'short_name':short, 'ms_description':r['old_description'],
                         'short_name_equals_ms':short.casefold()==normalized(r['old_description']).casefold()})
    assert len(resolved)==3 and all(r['short_name_equals_ms'] for r in resolved)
    for filename, rows in [('short_name_fallback_targets.csv',fallback),
                            ('short_name_fallback_holds.csv',holds),
                            ('exact_id_ambiguities_resolved.csv',resolved)]:
        with (OUT/filename).open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.DictWriter(f,fieldnames=list(rows[0]),delimiter=';');w.writeheader();w.writerows(rows)
    summary={'short_name_policy_rows':215, 'usable_short_name':130,
             'short_name_targets':len(fallback), 'short_name_unavailable':sum(
                 'NO_SHORT_NAME' in r['hold_reason'] for r in holds),
             'owner_short_name_overrides':2,
             'chip_or_firmware_holds':len(holds)-sum('NO_SHORT_NAME' in r['hold_reason'] for r in holds),
             'exact_feed_id_ambiguities_resolved_equal_ms':len(resolved),
             'truncated_title_rows_not_yet_reclassified':18,
             'external_writes':0}
    (OUT/'short_name_fallback_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(summary,ensure_ascii=False))


if __name__=='__main__':
    main()
