# поток: mkt
"""Read-only final audit. Reconstruct costs from raw evidence, without cogs_map/unit_cost."""
import ast
import re
from collections import Counter
from datetime import datetime, timezone
from core import db


def code(value):
    m = re.match(r'^([0-9]{4})', (value or '').strip())
    return m.group(1) if m else None


def quantity(vendor, title):
    text = (vendor or '').upper().replace('Х', 'X')
    exact = re.fullmatch(r'[0-9]{4}X([0-9]{1,2})', text)
    if exact:
        return max(1, int(exact[1]))
    suffix = re.match(r'^[0-9]{4}X([0-9]{1,2})(?![0-9])', text)
    named = re.search(r'\b([0-9]{1,2})\s*шт\b', title or '', re.I)
    if suffix and named and re.match(r'^Комплект\b', title or '', re.I):
        n = int(suffix[1])
        if n > 1 and n == int(named[1]):
            return n
    return 1


def load_evidence(acc, vendors):
    compositions = {}
    for row in db.query('SELECT external_code, components FROM set_cost'):
        comps = Counter()
        for value in row['components'] or []:
            if len(value) != 4:
                try:
                    value = ast.literal_eval(value)['external_code']
                except (ValueError, SyntaxError, KeyError, TypeError):
                    raise ValueError(f"unparsed set component: {row['external_code']}")
            comps[value] += 1
        if comps:
            compositions[row['external_code']] = dict(comps)
    links = {}
    def link(a, b):
        a, b = code(a), code(b)
        if a and b and a != b:
            links.setdefault(a, set()).add(b)
            links.setdefault(b, set()).add(a)
    for row in db.query('SELECT external_code, ref_code FROM prc_tc_link'):
        link(row['external_code'], row['ref_code'])
    for row in db.query("""SELECT payload->>'externalCode' ec, a->>'value' v
        FROM raw_moysklad_product CROSS JOIN LATERAL
        jsonb_array_elements(CASE WHEN jsonb_typeof(payload->'attributes')='array'
                            THEN payload->'attributes' ELSE '[]'::jsonb END) a
        WHERE a->>'name'='Связь'"""):
        for value in (row['v'] or '').replace(',', ';').split(';'):
            link(row['ec'], value.strip())
    wanted = {code(v) for v in vendors if code(v)}
    for parent in list(wanted):
        wanted.update(compositions.get(parent, {}))
    for parent in list(wanted):
        wanted.update(links.get(parent, set()))
    wanted = sorted(wanted)
    stock = db.query("""SELECT external_code,store,supplier,article,stock,buy_price,cost_seb,captured_at
        FROM supplier_stock WHERE captured_at=(SELECT max(captured_at) FROM supplier_stock)
        AND left(external_code,4)=ANY(%s)""", (wanted,))
    offers = db.query("""WITH ll AS (SELECT DISTINCT ON(supplier_key) id,supplier_key,moment
        FROM prc_price_load WHERE status='ok' ORDER BY supplier_key,moment DESC)
        SELECT p.external_code, r.qty AS stock, r.price_rub AS buy_price,
        ll.supplier_key AS supplier, r.article, ll.moment AS captured_at
        FROM ll JOIN prc_price_row r ON r.load_id=ll.id JOIN products p ON p.ms_id=r.ms_id
        WHERE r.status='loaded' AND r.price_rub>0 AND left(p.external_code,4)=ANY(%s)""", (wanted,))
    tc = db.query("""SELECT c.external_code,coalesce(c.buy_price,k.buy_price) buy_price,
        c.status,k.price_date last_known_date FROM tc_buy_price_latest c
        LEFT JOIN tc_buy_price_last_known k ON k.external_code=c.external_code
        WHERE left(c.external_code,4)=ANY(%s)""", (wanted,))
    cards = db.query('SELECT nm_id,vendor_code,title FROM wb_cards WHERE account=%s', (acc,))
    return dict(sets=compositions, links=links, stock=stock, offers=offers, tc=tc,
                cards={int(r['nm_id']): r for r in cards})


def reconstruct(parent, mult, evidence, minimum_stock=4, own_share=.5):
    stock, offers, tc = {}, {}, {}
    for row in evidence['stock']:
        stock.setdefault(code(row['external_code']), []).append(row)
        if row['store'] == 'Удаленный склад':
            offers.setdefault(code(row['external_code']), []).append(dict(row, origin='ms_stock'))
    for row in evidence['offers']:
        offers.setdefault(code(row['external_code']), []).append(dict(row, origin='supplier_price'))
    for row in evidence['tc']:
        if row['buy_price'] and float(row['buy_price']) > 0:
            c = code(row['external_code'])
            if c not in tc or float(row['buy_price']) < float(tc[c]['buy_price']):
                tc[c] = row
    def own(codes, need):
        rows = [r for c in codes for r in stock.get(c, []) if r['store'] in ('Звездный','Дисквер')
                and float(r['stock'] or 0)>0 and float(r['cost_seb'] or 0)>0]
        qty = sum(float(r['stock']) for r in rows)
        if qty < need:
            return None
        price = sum(float(r['stock'])*float(r['cost_seb']) for r in rows)/qty
        market = [float(r['buy_price']) for c in codes for r in offers.get(c, [])
                  if float(r['stock'] or 0)>=minimum_stock and float(r['buy_price'] or 0)>0]
        market += [float(tc[c]['buy_price']) for c in codes if c in tc]
        if market and price < own_share*min(market):
            return None
        return dict(price=price, source='own_stock', rows=rows)
    def unit(c, need):
        candidates = {c} | evidence['links'].get(c, set())
        selected = own(candidates, need)
        if selected:
            return selected
        rows = [dict(r, resolved_code=x) for x in candidates for r in offers.get(x, [])
                if float(r['stock'] or 0)>=minimum_stock and float(r['buy_price'] or 0)>0]
        if rows:
            selected = min(rows, key=lambda r:float(r['buy_price']))
            return dict(price=float(selected['buy_price']), source='supplier_min', rows=[selected],
                        quantity_short=float(selected['stock'])<need)
        remote = {x for x in candidates if any(r['store']=='Удаленный склад'
                  and float(r['stock'] or 0)>=minimum_stock for r in stock.get(x, []))}
        fallback = [tc[x] for x in remote if x in tc]
        if fallback:
            selected = min(fallback, key=lambda r:float(r['buy_price']))
            return dict(price=float(selected['buy_price']), source='tc_last_buy', rows=[selected])
        return dict(price=None, source='unavailable', rows=[])
    comps = evidence['sets'].get(parent)
    if not comps:
        u = unit(parent, mult)
        return dict(cost=u['price']*mult if u['price'] is not None else None,
                    source=u['source'], parts=[dict(code=parent, quantity=mult, **u)])
    full_own = own({parent}, mult)
    parts = [dict(code=c, quantity=n*mult, **unit(c,n*mult)) for c,n in comps.items()]
    if full_own:
        return dict(cost=full_own['price']*mult, source='set_own_stock', parts=parts, full=full_own)
    if any(p['source']=='unavailable' for p in parts):
        return dict(cost=None, source='incomplete_set', parts=parts)
    component_total = sum(p['price']*p['quantity'] for p in parts)
    direct = float(tc[parent]['buy_price'])*mult if parent in tc else None
    if direct is None:
        direct_offers = [float(r['buy_price'])*mult for r in offers.get(parent, [])
                         if float(r['stock'] or 0)>=minimum_stock and float(r['buy_price'] or 0)>0]
        direct = min(direct_offers) if direct_offers else None
    return dict(cost=max(component_total,direct or 0), source='set_complete', parts=parts,
                component_total=component_total, full_purchase_cost=direct)


def audit_account(acc, goods, primary, decisions, mp_stocks, query_evidence=load_evidence):
    from ops import wb_promo_guard as config
    selected = {nm:g for nm,g in goods.items() if g[1]>50 and g[2]>0}
    if not selected:
        return dict(account=acc, checked=0, flagged=0, cards=[], finding_counts={},
                    mp_availability_checked=bool(mp_stocks),
                    checked_at=datetime.now(timezone.utc).isoformat())
    evidence = query_evidence(acc, [g[3] for g in selected.values()])
    decisions = {r['nm_id']:r for r in decisions}
    rows=[]
    for nm,g in selected.items():
        base,discount,price,vendor=g;card=evidence['cards'].get(nm,{})
        title=card.get('title') if card.get('vendor_code')==vendor else None
        parent=code(vendor); mult=quantity(vendor,title)
        result=reconstruct(parent,mult,evidence,config.MIN_SUP_STOCK,config.OWN_COST_MIN_SHARE)
        old=primary.get(nm,{})
        findings=[]
        if code(old.get('external_code')) != parent:
            findings.append('identity_mismatch')
        if old.get('bundle',1)!=mult:
            findings.append('quantity_mismatch')
        unavailable = nm in mp_stocks and mp_stocks[nm]==0
        if unavailable:
            findings.append('no_mp_stock')
        elif result['cost'] is None:
            findings.append('cost_unresolved')
        elif old.get('cogs') is None or abs(float(old['cogs'])-result['cost'])>.5:
            findings.append('cost_changed_or_mismatch')
        if result['source'] != 'set_own_stock' and any(p.get('quantity_short') for p in result['parts']):
            findings.append('selected_supplier_quantity_short')
        cost=result['cost'];floor=None;net=None
        if cost is not None and not unavailable:
            floor=(cost+config.min_net(cost,mult))/(1-config.RETENTION[acc])
            net=price*(1-config.RETENTION[acc])-cost
            if price<floor-.5:
                findings.append('below_floor')
        rows.append(dict(nm_id=nm,vendor_code=vendor,title=title,discount=discount,price=price,
                         quantity=mult,mp_stock=mp_stocks.get(nm),primary_cost=old.get('cogs'),
                         primary_external_code=old.get('external_code'),
                         primary_source=old.get('source'),independent=result,floor=floor,net=net,
                         guard_reason=decisions.get(nm,{}).get('reason'),findings=findings))
    return dict(account=acc,checked=len(rows),flagged=sum(bool(r['findings']) for r in rows),
                finding_counts=dict(Counter(f for r in rows for f in r['findings'])),
                mp_availability_checked=bool(mp_stocks),
                checked_at=datetime.now(timezone.utc).isoformat(),cards=rows,
                membership_note='Price discount is not proof of current WB auto-action membership')
