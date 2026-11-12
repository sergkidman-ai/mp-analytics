# поток: rev
"""Adapter around the existing answer generator, controller and publication rules."""
from reports import question_evidence as evidence


def complete(packet):
    return bool(packet['conditions']) and all(c['status']=='confirmed' for c in packet['conditions']) and not packet['conflicts']


def deterministic(packet):
    import re
    question=packet.get('question') or ''
    if not re.search(r'все|комплект|набор|как\s+на\s+фото|сколько',question,re.I):return None
    if re.search(r'другие|отдельно|докуп|вместо',question,re.I):return None
    conditions={c['id']:c for c in packet['conditions']}
    if set(conditions)=={'composition'} and conditions['composition']['status']=='confirmed':
        value=conditions['composition']['value']
        if isinstance(value,dict) and value.get('colors'):
            colors=value['colors']
            return 'Здравствуйте! Да, в комплекте все '+str(len(colors))+' цветов: '+', '.join(colors)+'.'
    return None


def answer(client,row,cf,corpus,packet):
    from reports import feedback_today as engine
    from reports.archive_question_policy import is_archive_question
    row=dict(row,_evidence_packet=packet)
    card=engine._card_data(row,cf)
    cls=engine.rc.classify(row.get('body'),kind='question',rating=row.get('rating'))
    text=deterministic(packet)
    ground={'source':'проверенные источники','grounded':complete(packet),'request_class':cls,
            'evidence_version':evidence.VERSION,'evidence_signature':evidence.store.digest(packet),
            'evidence_conditions':packet['conditions'],'operator_requests':packet['operator_requests'],
            'note':'Проверка условий вопроса по источникам до генерации.'}
    if is_archive_question(row):
        if text:
            ground.update(archive_no_anthropic=True,verification_method='source_rules_no_llm',
                          deterministic_verified=True,verify='deterministic',historical_analysis=True,
                          fact_basis={'field':'exact_listing_image','source_ids':['image-0']})
            return {},text,'review',.95,ground,False,False
        from reports.archive_questions import answer as archive_answer
        result=archive_answer(row,cf)
        data,reply,route,confidence,old,used_llm,used_web=result
        old.update(evidence_version=evidence.VERSION,evidence_signature=ground['evidence_signature'],
                   evidence_conditions=packet['conditions'],operator_requests=packet['operator_requests'])
        return data,reply,route,confidence,old,used_llm,used_web
    used_llm=not bool(text)
    if not text:
        data,card,_=engine._llm(client,row,cf,corpus)
        text=str(data.get('reply') or '').strip()
        packet=evidence.apply_checks(packet,data.get('checks'))
        evidence.remember(row,packet)
        ground.update(evidence_signature=evidence.store.digest(packet),evidence_conditions=packet['conditions'],
                      operator_requests=packet['operator_requests'],grounded=complete(packet),llm=True)
        row['_evidence_packet']=packet;card=engine._card_data(row,cf)
    else:
        ground.update(template=True,template_id='evidence_composition')
    text=engine._presale_scrub(text,row)
    text,issues=engine._qa_guards(text,card,row.get('body') or '')
    if issues:ground['qa_guard']=issues
    text=engine._scrub_urls(text)
    if not text or text.startswith('⚠️'):
        ground['fact_block']='Не получен содержательный ответ; '+ '; '.join(packet['operator_requests'])
        return {},'','human',0,ground,used_llm,False
    text,ground=engine._verified(client,row,text,card,ground)
    route='review'
    if ground.get('verify')!='PASS':route='human'
    if not complete(packet):
        ground['fact_block']='; '.join(packet['operator_requests']) or 'Источники противоречат друг другу'
        route='human'
    compatible=next((c for c in packet['conditions'] if c['id']=='compatibility'),None)
    if compatible and compatible['status']=='confirmed' and isinstance(compatible['value'],list):
        ground['compat']={'exact':True,'matched':compatible['value'],'source':'evidence'}
    return {},text,route,.9 if complete(packet) else .5,ground,used_llm,any(s['kind']=='manufacturer_document' for s in packet['sources'])
