# поток: rev
"""Exact native archive evidence. No LLM, publication, or operator-state mutation.

Deleted suffixes are significant: a live base offer is never a substitute. The
native archived offer must also match model IDs retained in historical reviews.
Only public product fields enter grounding; prices and procurement fields do not.
"""
import hashlib
import re


def answer_from_native_mapping(row, mapping, historical_model_ids):
    """Return the engine tuple for proven compatible Epson ink, otherwise None."""
    if row.get('platform') != 'yandex' or row.get('kind') != 'question':
        return None
    question = (row.get('body') or '').strip().lower()
    # This rule answers only the original/not-original question, not a compound
    # question about a manufacturer, parcel, or compatibility.
    if not re.fullmatch(r'(?:это\s+)?оригинал\s*[?!.]*', question):
        return None
    offer = mapping.get('offer') or {}
    model_id = (mapping.get('mapping') or {}).get('marketModelId')
    history = {str(value) for value in historical_model_ids if value is not None}
    if (str(offer.get('offerId')) != str(row.get('item_id'))
            or offer.get('archived') is not True
            or not history or str(model_id) not in history or len(history) != 1):
        return None
    name = offer.get('name') or ''
    description = offer.get('description') or ''
    product_kind = re.search(r'(?:это|является)\s+совместим\w*\s+чернил\w*', description, re.I)
    vendor = str(offer.get('vendor') or '').strip()
    if not (product_kind and 'чернил' in name.lower() and 'epson' in name.lower()
            and vendor and vendor.casefold() != 'epson'):
        return None
    basis = {
        'field': 'exact_native_archived_offer',
        'offer_id': offer['offerId'], 'market_model_id': model_id,
        'historical_model_ids': sorted(history), 'name': name, 'brand': vendor,
        'product_kind_quote': product_kind.group(0),
        'description_sha256': hashlib.sha256(description.encode()).hexdigest(),
        'scope': 'originality_only',
    }
    ground = {
        'source': 'точная архивная карточка Яндекс.Маркета',
        'archive_no_anthropic': True, 'verification_method': 'source_rules_no_llm',
        'deterministic_verified': True, 'verify': 'deterministic',
        'historical_analysis': True, 'historical_publication_requires_owner': True,
        'fact_basis': basis,
        'sources': [{'title': 'Market Partner API: exact archived offer and historical model identity',
                     'url': 'https://api.partner.market.yandex.ru/v2/businesses/{businessId}/offer-mappings',
                     'offer_id': offer['offerId']}],
        'grounded': True, 'llm': False,
        'note': 'Точный offerId; modelId совпадает с историческими отзывами. Без Anthropic.',
    }
    return ({}, 'Здравствуйте! Это совместимые чернила, не оригинальные Epson.',
            'review', .95, ground, False, False)


def native_archive_answer(row):
    """Read already restored raw evidence; never fetch APIs or modify state."""
    from core import db
    mappings = db.query('''SELECT payload,loaded_at FROM raw_yandex_offer
        WHERE account=%s AND offer_id=%s''', (row.get('account'), row.get('item_id')))
    if not mappings:
        return None
    history = db.query('''SELECT DISTINCT payload->'identifiers'->>'modelId' AS model_id
        FROM raw_feedback WHERE platform='yandex' AND account=%s AND kind='review'
        AND item_id=%s''', (row.get('account'), row.get('item_id')))
    result = answer_from_native_mapping(row, mappings[0]['payload'], [r['model_id'] for r in history])
    if result and mappings[0].get('loaded_at'):
        result[4]['source_checked_at'] = mappings[0]['loaded_at'].isoformat()
    return result


def public_product_attributes(attributes, definitions):
    """Return only proven brand/country; brand is deliberately not manufacturer."""
    labels = {int(d['id']): d.get('name', '') for d in definitions}
    result = {}
    for attribute in attributes:
        aid = int(attribute['id'])
        label = labels.get(aid)
        key = {'Бренд': 'brand', 'Страна-изготовитель': 'country_of_manufacture'}.get(label)
        if key:
            values = [str(v['value']) for v in attribute.get('values', []) if v.get('value')]
            if values:
                result[key] = {'attribute_id': aid, 'attribute_name': label, 'values': values}
    return result
