# поток: prc
"""Per-card marketplace observations. Activity means catalogue status, not buyability."""
import re
from datetime import datetime, timezone
from core import db

ROLES = ('main','printer','bundle','unknown')
ROLE_LABELS = {'main':'Основная','printer':'По принтерам','bundle':'Бандлы','unknown':'Тип требует проверки'}
CRITERIA = {'oz':'Оффер входит в VISIBLE Ozon',
            'wb':'Есть в кабинете ВБ и не в корзине',
            'ya':'Карточка Маркета имеет статус HAS_CARD'}




def role_title(name):
    """Ignore a trailing measure only for card type, never for product identity/qty."""
    value=re.sub(r'\s+\d+(?:[.,]\d+)?\s*(?:мл|ml|метр(?:ов|а)?|м)\.?\s*$', '', name or '',flags=re.I)
    return normalized_title(value)

def specific_printer_title(name):
    """Recognise a printer-specific title template with exactly one device model."""
    title=role_title(name)
    # Volume belongs to the physical item. Ignore it only for detecting a device,
    # never in the title key used to match different cards or calculate quantities.
    match=re.fullmatch(r'(?:(?:тонер[- ]|драм[- ])?картридж|лента|заправочный комплект|фотобарабан|тонер|чип) для '
                      r'(?:принтера )?([a-z][a-z-]*|катюша)\b (.+)',title,re.I)
    if not match:return False
    device=match[2]
    if re.search(r'[,;/+]',device):return False
    models=[token for token in re.findall(r'[\w-]+',device) if any(c.isdigit() for c in token)]
    return len(models)==1 and len(re.findall(r"\d+",models[0]))==1

def classify(offer, name='', account='', hints=None, *, infer_device=True):
    offer=(offer or '').strip();name=name or ''
    anchor=re.match(r'^(\d{4})',offer)
    if not anchor:return None
    ext=anchor[1]
    # Known literal base and numerical SEO articles, without random tails.
    if offer==ext:return ext,'main',None,'точный четырёхзначный артикул'
    if re.fullmatch(r'\d{5,}',offer):return ext,'printer',None,'цифровой артикул по принтеру'
    bundle=re.fullmatch(r'\d{4}[XХ×](\d+)([A-Z0-9]*)',offer,re.I)
    if bundle and int(bundle[1])>1:
        qty=int(bundle[1])
        confirmed=(not bundle[2] or (re.search(r'\b(?:комплект|бандл)\b',name,re.I)
                    and re.search(r'\b'+str(qty)+r'\s*шт\b',name,re.I)))
        if confirmed:return ext,'bundle',qty,'кратность артикула подтверждена названием' if bundle[2] else 'явный артикул бандла'
    # Corroborate identical vendor articles across marketplaces of the same organisation.
    hint=(hints or {}).get(offer)
    if hint:return ext,*hint
    # Established Diskver base convention. A random tail beginning X3 is ambiguous.
    if (not (bundle and int(bundle[1])>1) and re.fullmatch(re.escape(ext)+r'[A-Z][A-Z0-9]{7}',offer,re.I)):
        return ext,'main',None,'основной артикул со случайным суффиксом'
    # A new WB-only alias may have no matching offer in the Ozon catalogue yet.
    # Use a non-conflicting title reference within this exact model, after explicit formats.
    hint=(hints or {}).get((ext,normalized_title(name)))
    if hint:return ext,*hint
    hint=(hints or {}).get(('type-title',ext,role_title(name)))
    if hint:return ext,*hint
    # Numeric prefix plus random tail is not enough: tail may itself start with a digit.
    if re.match(r'^\d{5,}[A-Z]',offer,re.I) and re.search(r'\bдля\s+принтера\b',name,re.I):
        return ext,'printer',None,'цифровой префикс и конкретный принтер в названии'
    if infer_device and re.fullmatch(r'\d{5,}[A-Z][A-Z0-9]{5,7}',offer) and specific_printer_title(name):
        return ext,'printer',None,'числовое продолжение и название с одной моделью принтера'
    return ext,'unknown',None,'суффикс не подтверждает тип карточки'


def normalized_title(title):
    value=re.sub(r'\bDS\b','',title or '',flags=re.I)
    # The same printer-specific card may append the print yield on one marketplace.
    # Preserve model numbers, bundle quantities, ink volume and physical composition.
    value=re.sub(r'\b\d+(?:[.,]\d+)?\s*(?:страниц(?:а|ы)?|стр\.?|знак(?:ов|а)?|символ(?:ов|а)?)(?=\W|$)',' ',value,flags=re.I)
    return re.sub(r'[,;\s]+',' ',value).strip(' .,:;').casefold()


def role_hints(account):
    """Use explicit article formats as references, including aliases seen only on WB.

    Saved inferred roles are never fed back as evidence. An unresolved title or
    conflicting roles cannot supply a hint for another card.
    """
    accounts=['oz_acc1','oz_acc2'] if account.endswith('acc2') else ['oz_acc1']
    showcases=['oz_acc1','wb_acc1','ya_acc1']
    if account.endswith('acc2'):showcases+=['oz_acc2','wb_acc2']
    rows=db.query('''SELECT offer_id,name FROM ozon_product WHERE account=ANY(%s)
                     UNION SELECT offer AS offer_id,name FROM prc_launch_card
                           WHERE present AND showcase=ANY(%s)''',(accounts,showcases))
    references={};ambiguous=set()
    for r in rows:
        role=classify(r['offer_id'],r['name'],account,infer_device=False)
        if not role or role[1]=='unknown':continue
        value=(role[1],role[2],'название подтверждённой карточки этой модели')
        for key in ((role[0],normalized_title(r['name'])),('type-title',role[0],role_title(r['name']))):
            if not key[-1]:continue
            if key in references and references[key][:2]!=value[:2]:ambiguous.add(key)
            references[key]=value
    hints={k:v for k,v in references.items() if k not in ambiguous}
    hints.update({k:('unknown',None,'название совпадает у карточек разных типов') for k in ambiguous})
    offers={};conflicts=set()
    for r in rows:
        role=classify(r['offer_id'],r['name'],account,infer_device=False)
        if not role:continue
        value=role[1:] if role[1]!='unknown' else (hints.get((role[0],normalized_title(r['name']))) or hints.get(('type-title',role[0],role_title(r['name']))))
        if not value or value[0]=='unknown':continue
        offer=r['offer_id']
        if offer in offers and offers[offer][:2]!=value[:2]:conflicts.add(offer)
        offers[offer]=value
    hints.update({k:v for k,v in offers.items() if k not in conflicts})
    return hints


def observation_dates(old, state, today, seed_new=False):
    if old is None:
        return (None,None,True) if seed_new else (today,today if state=='selling' else None,False)
    card_at,active_at,seeded=old['card_at'],old['active_at'],old['seeded']
    if not seed_new:
        if not old['present'] and card_at is None and not seeded:card_at=today
        if state=='selling' and old['state']!='selling' and active_at is None:active_at=today
    return card_at,active_at,seeded


def persist_source(account, observations, now=None):
    """Atomically replace one successful source; preserve prior snapshots on failure."""
    from psycopg2.extras import execute_values, RealDictCursor
    now=now or datetime.now(timezone.utc);hints=role_hints(account);rows={}
    for st in observations.values():
        for card in st.get('variants',[]):
            result=classify(card['offer'],card.get('name',''),account,hints)
            if not result:continue
            ext,role,qty,basis=result
            key=(card['offer'],card['card_id'])
            item=dict(card,external_code=ext,role=role,multiplier=qty,role_basis=basis)
            if key not in rows or item['state']=='selling':rows[key]=item
    with db.get_conn() as c,c.cursor(cursor_factory=RealDictCursor) as q:
        q.execute('SELECT * FROM prc_launch_card WHERE showcase=%s FOR UPDATE',(account,))
        previous={(r['offer'],r['card_id']):r for r in q.fetchall()}
        q.execute('SELECT status FROM prc_launch_source_state WHERE showcase=%s',(account,));health=q.fetchone()
        seed_new=not previous or not health or health['status']!='ok'
        q.execute('UPDATE prc_launch_card SET present=false WHERE showcase=%s',(account,))
        values=[]
        for key,r in rows.items():
            card_at,active_at,seeded=observation_dates(previous.get(key),r['state'],now.date(),seed_new)
            values.append((account,r['offer'],r['card_id'],r['external_code'],r['role'],r['role_basis'],
                           r.get('name'),r['state'],r.get('note'),r['multiplier'],card_at,active_at,seeded,now,now))
        if values:
            execute_values(q,'''INSERT INTO prc_launch_card
             (showcase,offer,card_id,external_code,role,role_basis,name,state,note,multiplier,
              card_at,active_at,seeded,first_observed,checked_at)
             VALUES %s ON CONFLICT(showcase,offer,card_id) DO UPDATE SET
              external_code=excluded.external_code,role=excluded.role,role_basis=excluded.role_basis,
              name=excluded.name,state=excluded.state,note=excluded.note,multiplier=excluded.multiplier,
              card_at=excluded.card_at,active_at=excluded.active_at,seeded=excluded.seeded,
              present=true,checked_at=excluded.checked_at''',values,page_size=500)
        q.execute('INSERT INTO prc_launch_card_source(showcase,checked_at) VALUES(%s,%s) '
                  'ON CONFLICT(showcase) DO UPDATE SET checked_at=excluded.checked_at',(account,now))
    return len(rows)


def details(codes, health):
    result={}
    for r in db.query('''SELECT external_code,showcase,offer,card_id,role,role_basis,name,state,note,
                         multiplier,checked_at FROM prc_launch_card WHERE present AND external_code=ANY(%s)
                         ORDER BY role,offer,card_id''',(codes or [''],)):
        if health.get(r['showcase'],{}).get('status')!='ok':r=dict(r,state='unknown',note='Источник не обновлён')
        r['criterion']=CRITERIA.get(r['showcase'].split('_')[0],'')
        result.setdefault(r['external_code'],{}).setdefault(r['showcase'],[]).append(r)
    return result


def summaries(codes, health):
    result={}
    for r in db.query('''SELECT external_code,showcase,role,state,count(*) n
                         FROM prc_launch_card WHERE present AND external_code=ANY(%s)
                         GROUP BY 1,2,3,4''',(codes or [''],)):
        groups=result.setdefault(r['external_code'],{}).setdefault(r['showcase'],{})
        group=groups.setdefault(r['role'],dict(total=0,active=0,card=0,unknown=0))
        state=r['state'] if health.get(r['showcase'],{}).get('status')=='ok' else 'unknown'
        group['total']+=r['n']
        group['active' if state=='selling' else 'card' if state=='card' else 'unknown']+=r['n']
    return result


def card_page(code, showcase, role='', page=1):
    params=[code,showcase];where='external_code=%s AND showcase=%s AND present'
    if role:
        if role not in ROLES:raise ValueError('Неизвестный тип карточки')
        where+=' AND role=%s';params.append(role)
    total=db.query('SELECT count(*) n FROM prc_launch_card WHERE '+where,params)[0]['n']
    pages=max(1,(total+49)//50);page=max(1,min(page,pages))
    cards=db.query('''SELECT offer,card_id,role,role_basis,name,state,note,multiplier,
                     card_at,active_at,seeded,checked_at FROM prc_launch_card WHERE '''+where+
                   ' ORDER BY offer,card_id LIMIT 50 OFFSET %s',params+[(page-1)*50])
    health=db.query('SELECT status FROM prc_launch_source_state WHERE showcase=%s',(showcase,))
    good=bool(health and health[0]['status']=='ok')
    for r in cards:
        if not good:r.update(state='unknown',note='Источник не обновлён')
    return dict(cards=cards,total=total,page=page,pages=pages,
                criterion=CRITERIA.get(showcase.split('_')[0],''))


def problem_codes():
    """Review missing/inactive main and every observed inactive/uncertain derivative.
    Zero derivatives is not an inactive card: no required assortment matrix is defined.
    """
    return {r['external_code'] for r in db.query('''
        SELECT external_code FROM prc_launch_primary WHERE state <> 'selling'
        UNION
        SELECT c.external_code FROM prc_launch_card c
        LEFT JOIN prc_launch_source_state h USING(showcase)
        WHERE c.present AND (c.state <> 'selling' OR c.role='unknown'
                             OR h.status IS DISTINCT FROM 'ok')''')}
