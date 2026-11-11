# поток: prc
"""Read preserved launch observations without inventing historical model dates."""
from core import db
from prices.sup_group import group_of
from collectors.launch_cards import problem_codes

PAGE_SIZE = 100


def model_supplier_groups(codes):
    from invoice_bot.supplier_groups import ID2GROUP, ID2NAME
    sources = db.query("""SELECT p.external_code,
                         array_agg(DISTINCT r.payload #>> '{supplier,meta,href}') supplier_refs
                          FROM ms_product p JOIN raw_moysklad_product r USING(ms_id)
                         WHERE NOT p.archived AND p.external_code=ANY(%s)
                         GROUP BY p.external_code""", (codes or [''],))
    model_groups = {}
    for source in sources:
        groups = set()
        for ref in source['supplier_refs']:
            key = (ref or '').rsplit('/',1)[-1]
            name = ID2GROUP.get(key) or ID2NAME.get(key)
            if name: groups.add(name)
        model_groups[source['external_code']] = groups
    return model_groups


def undated_models(page=1, search='', gaps=True, supplier='', failed=False, problems=None):
    rows = db.query('''
        SELECT l.external_code,l.ms_created,l.ms_name,l.supplier_key,l.cards,l.archived,
               st.supplier stock_supplier,coalesce(p.selling,0) selling
          FROM prc_launch l
          LEFT JOIN (SELECT external_code,count(*) FILTER (WHERE state='selling') selling
                       FROM prc_launch_primary GROUP BY 1) p USING(external_code)
          LEFT JOIN (SELECT external_code,min(supplier) supplier FROM supplier_stock
                      WHERE captured_at=(SELECT max(captured_at) FROM supplier_stock)
                        AND store <> 'Брак' GROUP BY 1) st USING(external_code)
         WHERE l.ms_created IS NULL AND NOT l.archived
         ORDER BY l.external_code DESC''')
    model_groups = model_supplier_groups([r['external_code'] for r in rows])
    if problems is None: problems = problem_codes()
    all_total = len(rows)
    needle = search.strip().casefold()
    for row in rows:
        fallback = group_of(row['supplier_key'] or row.pop('stock_supplier'))
        groups = model_groups.get(row['external_code'], set())
        if fallback: groups.add(fallback)
        row['supplier_groups'] = sorted(groups)
        row['supplier_key'] = ', '.join(sorted(groups)) or None
    suppliers = sorted({g for r in rows for g in r['supplier_groups']})
    rows = [r for r in rows if (not supplier or supplier in r['supplier_groups'])
            and (not gaps or failed or r['external_code'] in problems)
            and (not needle or needle in (r['external_code']+' '+(r['ms_name'] or '')).casefold())]
    total = len(rows)
    pages = max(1, (total+PAGE_SIZE-1)//PAGE_SIZE)
    page = max(1, min(page, pages))
    return {'models': rows[(page-1)*PAGE_SIZE:page*PAGE_SIZE],
            'total':total,'all_total':all_total,'review_total':sum(r['external_code'] in problems for r in rows),'page':page,'pages':pages,
            'page_size':PAGE_SIZE,'suppliers':suppliers}


def dated_page(rows, page=1, search='', gaps=True, supplier='', problems=None, groups=None):
    """Filter before paging so every recovered historical model is reachable."""
    problems = problems or set()
    groups = groups or {}
    needle = search.strip().casefold()
    selected = []
    for r in rows:
        memberships = set(groups.get(r['external_code'], set()))
        fallback = group_of(r.get('supplier_key'))
        if fallback: memberships.add(fallback)
        if supplier and supplier not in memberships: continue
        if gaps and r['external_code'] not in problems: continue
        if needle and needle not in (r['external_code']+' '+(r['ms_name'] or '')).casefold(): continue
        selected.append(dict(r, supplier_key=', '.join(sorted(memberships)) or None))
    total = len(selected)
    pages = max(1, (total+PAGE_SIZE-1)//PAGE_SIZE)
    page = max(1, min(page, pages))
    return dict(models=selected[(page-1)*PAGE_SIZE:page*PAGE_SIZE], total=total,
                page=page, pages=pages, page_size=PAGE_SIZE,
                codes={r['external_code'] for r in selected})
