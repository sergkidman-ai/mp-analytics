# поток: rev
"""Bounded manufacturer search and a separate, source-linked operator explanation."""
from datetime import datetime, timezone
import html
import re
from urllib.parse import urlparse

DOMAINS={'epson':'epson.eu','brother':'brother.com','hp':'hp.com','canon':'canon.com',
         'kyocera':'kyoceradocumentsolutions.com','xerox':'xerox.com','pantum':'pantum.com'}


def exact_model_in(model,text):
    compact=re.sub(r'[^a-z0-9]','',model.lower())
    return bool(compact and re.search(r'(?<![a-z0-9])'+r'[-\s]*'.join(map(re.escape,compact))+r'(?![a-z0-9])',text,re.I))


def focused_text(text,context,limit=2400):
    if len(text)<=limit:return text
    chunks=[text[:800]];ranges=[(0,800)]
    for term in sorted(set(re.findall(r'\d{3,}',context)),key=lambda x:(-len(x),x)):
        match=re.search(re.escape(term),text)
        if not match or any(a<=match.start()<b for a,b in ranges):continue
        a=max(0,match.start()-300);b=min(len(text),match.end()+500)
        chunks.append(text[a:b]);ranges.append((a,b))
        if sum(map(len,chunks))>=limit:break
    if len(chunks)==1:chunks.append(text[-1500:])
    return '\n[…]\n'.join(chunks)[:limit]


def cartridge_codes(facts):
    """Codes from our listing, preserving variant suffixes; never printer model guesses."""
    from reports.card_facts import _CODE_RX
    from reports.sku_relations import code_of_title
    title=re.split(r'\bдля\b|\bfor\b',str(facts.get('name') or ''),maxsplit=1,flags=re.I)[0]
    text=str(facts.get('code') or '')+' '+title
    codes=[]
    for candidate in [facts.get('code'),code_of_title(title)]:
        candidate=re.sub(r'\s+','',str(candidate or '')).upper()
        if re.fullmatch(r'[A-Z0-9][A-Z0-9/-]{1,29}',candidate) and re.search(r'\d',candidate):
            codes.append(candidate)
    codes += re.findall(r'\bC13S\d{6}[A-Z0-9]*\b',text,re.I)
    codes += [m.group(0) for m in _CODE_RX.finditer(text)]
    return list(dict.fromkeys(re.sub(r'\s+','',c).upper() for c in codes))[:2]


def lookup(row,facts,missing):
    from reports import deepseek_search,question_evidence as e
    question=str(row.get('body') or '')
    versions=re.findall(r'\d+(?:\.\d+){2,}(?:-\d+)?',question)
    models,_=e.matching_models(question,[])
    first=' '.join(versions+models+[str(facts.get('code') or '')[:25],question])[:128]
    audit={'provider':'DeepSeek Search','checked_at':datetime.now(timezone.utc).isoformat(),
           'attempts':[],'status':'no_primary','documents':[]}
    codes=cartridge_codes(facts)
    audit['cartridge_codes']=codes
    def search(query,direction='printer_to_cartridge'):
        try:
            result=deepseek_search.targeted(query,max_age_hours=24 if 'firmware' in missing else 168)
        except Exception as error:
            audit['attempts'].append({'query':query,'direction':direction,'error_type':type(error).__name__})
            raise
        docs=result.get('documents') or []
        primary=[d for d in docs if e.primary_url(d.get('url','')) and len(d.get('text') or '')>=80]
        entities=codes if direction=='cartridge_to_printer' else models
        relevant=[d for d in primary if not entities or any(exact_model_in(m,d.get('title','')+' '+d.get('text','')) for m in entities)]
        audit['attempts'].append({'query':query,'results':len(docs),'primary':len(primary),
                                  'direction':direction,
                                  'relevant_primary':len(relevant),
                                  'searched_at':result.get('searched_at'),'cached':result.get('cache_hit',False)})
        known={d['url'] for d in audit['documents']}
        audit['documents'] += [d for d in relevant if d['url'] not in known]
        if direction=='cartridge_to_printer':audit['reverse_urls']=[d['url'] for d in relevant]
    try:
        search(first)
        context=(question+' '+str(facts.get('name') or '')).lower()
        brand=next((b for b in DOMAINS if re.search(r'\b'+b+r'\b',context)),None)
        # One explicit manufacturer search; never conclude "no" from an empty search.
        if not audit['documents'] and brand and models:
            topic='ribbon cartridge' if re.search('лент|ribbon',context) else 'consumables compatibility'
            search(('site:'+DOMAINS[brand]+' '+brand+' '+' '.join(models+versions)+' '+topic)[:128])
        if audit['documents']:audit['status']='primary_found'
    except Exception as error:
        audit['status']='error';audit['error_type']=type(error).__name__
    if codes and 'compatibility' in missing:
        # A failure in the forward lookup must not prevent the independent reverse lookup.
        try:
            search((' '.join('"'+c+'"' for c in codes)+' compatible printers')[:128],
                   'cartridge_to_printer')
            if audit['documents'] and audit['status']!='error':audit['status']='primary_found'
        except Exception as error:
            audit['status']='error';audit['error_type']=type(error).__name__
    dates=[a['searched_at'] for a in audit['attempts'] if a.get('searched_at')]
    if dates:audit['checked_at']=max(dates,key=lambda value:datetime.fromisoformat(value.replace('Z','+00:00')))
    unique=[];seen=set()
    for doc in audit['documents']:
        parsed=urlparse(doc['url']);product=re.search(r'/p/(\d+)/?$',parsed.path)
        key=('epson',product[1]) if product and (parsed.hostname or '').endswith('.epson.eu') else doc['url']
        if key not in seen:unique.append(doc);seen.add(key)
    reverse=next((d for d in unique if d['url'] in audit.get('reverse_urls',[])),None)
    if reverse and reverse not in unique[:3]:unique.insert(1,unique.pop(unique.index(reverse)))
    audit['documents']=unique[:3]
    return audit


def operator_block(row):
    from core import db
    from reports.question_evidence import primary_url
    if row.get('kind')!='question' or not (row.get('draft_grounding') or {}).get('evidence_version'):return ''
    records=db.query('''SELECT payload FROM rev_question_evidence
        WHERE (platform,account,kind,ext_id)=(%s,%s,%s,%s)''',
        tuple(row.get(k) for k in ('platform','account','kind','ext_id')))
    if not records:return ''
    packet=records[0]['payload'];audit=packet.get('web_search') or {}
    sources=[s for s in packet.get('sources',[]) if s.get('kind')=='manufacturer_document' and primary_url(s.get('url',''))]
    if not audit and not sources:return ''
    lines=['🔎 <b>Веб-проверка для модератора</b>']
    if audit:
        checked=str(audit.get('checked_at') or '')[:16].replace('T',' ')
        lines.append(html.escape('DeepSeek Search · '+checked+' UTC'))
        for direction,label in [('printer_to_cartridge','Принтер → расходник'),('cartridge_to_printer','Расходник → принтер')]:
            attempts=[a for a in audit.get('attempts',[]) if a.get('direction','printer_to_cartridge')==direction]
            if attempts:
                found=any(a.get('relevant_primary',0) for a in attempts)
                state=('первоисточник найден' if found else 'поиск не завершён из-за ошибки сервиса'
                       if any(a.get('error_type') for a in attempts) else 'официальное подтверждение не найдено')
                lines.append(label+': '+state+'.')
    if not sources:
        lines.append('Поиск не завершён из-за ошибки сервиса.' if audit.get('status')=='error' else
                     'Официального подтверждения не найдено. Это не означает «не подходит».')
        return '\n'.join(lines)
    ground=row.get('draft_grounding') or {}
    ids={s['id'] for s in sources}
    proven=[c for c in packet.get('conditions',[]) if c.get('status')=='confirmed' and ids.intersection(c.get('source_ids',[]))]
    from reports.rev_fact_sources import digest
    verified=(ground.get('verify')=='PASS' and ground.get('evidence_signature')==digest(packet)
              and bool(packet.get('conditions')) and all(c.get('status')=='confirmed' for c in packet['conditions'])
              and not packet.get('conflicts'))
    compatibility=next((c for c in proven if c.get('id')=='compatibility'),{})
    verdict=compatibility.get('conclusion') if verified else None
    if verdict in ('yes','no'):
        lines.append('По проверенным источникам: <b>'+('подходит' if verdict=='yes' else 'не подходит')+'</b>.')
    else:
        lines.append('Подтверждение из официального источника учтено в проверенном ответе.' if verified and proven else
                     'Официальные источники найдены; окончательный вывод по нашему товару ещё не подтверждён.')
    cited={sid for c in proven for sid in c.get('source_ids',[])}
    ordered=sorted(sources,key=lambda s:s['id'] not in cited)
    for s in [s for s in ordered if len(s['url'])<=350][:2]:
        label=' '.join((s.get('title') or 'Источник производителя').split()[:4])[:70]
        lines.append('<a href="'+html.escape(s['url'],quote=True)+'">'+html.escape(label)+'</a>')
        quote=next((c.get('value') for c in proven if s['id'] in c.get('source_ids',[]) and isinstance(c.get('value'),str)),None)
        if not quote:
            # A literal excerpt helps the operator even when the full compatibility chain
            # remains unproven. Matching a code fragment is not a compatibility verdict.
            card=' '.join(item.get('text','') for item in packet['sources'] if item['id']=='card')
            numbers=sorted(set(re.findall(r'\d{4,}',card)),key=lambda value:(-len(value),value))
            quote=next((line.strip() for number in numbers for line in s.get('text','').splitlines()
                        if number in line),None)
        if quote:lines.append('Цитата: '+html.escape(' '.join(quote.split()[:16])[:180]))
    if not proven:lines.append('Нет модели в нашей карточке ≠ доказанная несовместимость.')
    return '\n'.join(lines)
