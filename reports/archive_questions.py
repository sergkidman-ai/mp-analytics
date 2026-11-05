# поток: rev
"""Source-based archive answers; no Anthropic calls or unapproved cached AI prose."""
import re
from core import db
from reports.card_facts import CardFacts
from reports import answer_cache
from reports.deepseek_search import cached_primary
from reports.feedback_llm import _asked_models


def norm(text):
    return re.sub(r'[^a-zа-яё0-9]','',str(text or '').lower())


def package_contents(offer_id):
    rows=db.query('''SELECT nm_id,payload FROM raw_wb_card_content
        WHERE vendor_code=%s OR vendor_code ~ ('^'||%s||'[^0-9]')
        ORDER BY (vendor_code=%s) DESC,nm_id LIMIT 1''',(offer_id,re.escape(offer_id),offer_id))
    if not rows:return [],None
    contents=[]
    for field in rows[0]['payload'].get('characteristics',[]):
        if (field.get('name') or '').strip().lower()=='комплектация':
            value=field.get('value') or []
            contents.extend(value if isinstance(value,list) else [str(value)])
    return contents,'https://www.wildberries.ru/catalog/'+str(rows[0]['nm_id'])+'/detail.aspx'


def answer(row,facts=None):
    """Return the normal engine tuple, based only on scoped, verified source fields."""
    if row.get('platform')!='yandex' or row.get('kind')!='question':raise ValueError('Archive question scope required')
    facts=facts or CardFacts()
    native=facts.for_yandex(row.get('item_id')) or {}
    question=row.get('body') or ''
    q=question.lower()
    reply='';missing=None;basis=None;references=[]
    kind=str(native.get('kind') or '').lower()
    if not native or not native.get('name'):
        from reports.rev_archive_fact_recovery import native_archive_answer
        recovered=native_archive_answer(row)
        if recovered:return recovered
        missing='Не восстановлены название и карточка товара '+str(row.get('item_id') or '')
    elif re.search(r'тонер.*(?:входит|комплект)|комплект.*тонер',q):
        contents,url=package_contents(str(row.get('item_id') or ''))
        if len(contents)==1 and re.search(r'фотобарабан|drum',contents[0],re.I):
            reply='Здравствуйте! Нет, в комплекте только фотобарабан.'
            basis={'field':'Комплектация','value':contents};references=[url]
        else:missing='Состав комплекта не подтверждён полностью'
    elif re.search(r'оригинал|подлинн',q):
        if 'совместим' in kind:
            reply='Здравствуйте! Это совместимые чернила, не оригинальные Epson.' if 'чернил' in (native.get('name') or '').lower() and 'epson' in (native.get('name') or '').lower() else 'Здравствуйте! Это совместимый товар, не оригинал производителя принтера.'
            basis={'field':'kind','value':native.get('kind')}
        elif 'оригинал' in kind and 'неоригинал' not in kind:
            reply='Здравствуйте! Это оригинальный товар.';basis={'field':'kind','value':native.get('kind')}
        else:missing='Оригинальность конкретного товара не подтверждена карточкой'
        if re.search(r'кто.*производ|производител',q):
            maker=native.get('manufacturer')
            if maker:reply+=' Производитель — '+str(maker)+'.'
            else:
                reply+=' Производителя нужно уточнить.'
                missing='Производитель конкретного лота не подтверждён'
    elif re.search(r'какие.*цвет|цвета|цветов',q):
        color=str(native.get('color') or '')
        if 'cmyk' in color.lower():
            reply='Здравствуйте! В наборе чёрный, голубой, пурпурный и жёлтый.'
            basis={'field':'color','value':color}
        elif color:
            reply='Здравствуйте! Цвет — '+color.lower()+'.';basis={'field':'color','value':color}
        else:missing='Цвет или состав набора не подтверждён'
    if not reply and not missing:
        asked=_asked_models(question)
        models=native.get('models') or []
        if asked and all(any(norm(model).endswith(norm(target)) for model in models) for target in asked):
            reply='Здравствуйте! Да, подойдёт для '+', '.join(asked)+'.'
            basis={'field':'models','asked':asked,'value':models}
        else:
            primary=cached_primary(question,row.get('product_name'))
            scope=(primary or {}).get('scope') or {}
            if (scope.get('proof_type')=='shared_exact_oem_supply'
                    and str(scope.get('item_id'))==str(row.get('item_id'))
                    and asked and all(norm(target)==norm(scope.get('printer')) for target in asked)
                    and any(norm(model).endswith(norm(scope.get('listed_reference_model'))) for model in models)
                    and norm(scope.get('required_native_name_fragment')) in norm(native.get('name'))
                    and scope.get('oem_supply') and scope.get('listed_reference_model')
                    and scope.get('required_native_name_fragment')):
                reply='Здравствуйте! Да, подойдёт для '+str(scope['printer'])+'.'
                basis={'field':'verified_primary_same_oem_supply','scope':scope}
                references=(primary or {}).get('sources') or []
            elif scope.get('printer')=='HP DeskJet F2180' and str(row.get('item_id'))==str(scope.get('item_id')) and scope.get('series')=='HP21/21XL':
                reply='Если у Вас HP DeskJet F2180, картридж 21XL подходит. Надпись на конкретной упаковке без её фото подтвердить нельзя.'
                basis={'field':'verified_primary','scope':scope};references=(primary or {}).get('sources') or []
                missing='Надпись на конкретной упаковке не подтверждена'
            else:
                hit=answer_cache.try_hit(row,native)
                cache=(hit[1].get('cache') or {}) if hit else {}
                if hit and not cache.get('stale') and not cache.get('family'):
                    reply=hit[0];basis={'field':'approved_answer','cache':hit[1].get('cache')}
                else:missing='Нет подтверждения по точной модели или свойству; требуется проверенный источник'
    verified=bool(reply and basis and not missing)
    ground={'source':'карточка/подтверждённые источники без Anthropic',
            'archive_no_anthropic':True,'verification_method':'source_rules_no_llm',
            'deterministic_verified':verified,'verify':'deterministic' if verified else 'needs_facts',
            'historical_analysis':True,'fact_basis':basis,'sources':references,
            'grounded':verified,'llm':False,'note':'Ответ сформирован по источникам без Anthropic и без платного контролёра.'}
    if missing:ground['fact_block']=missing
    if basis and basis.get('field')=='approved_answer':ground['cache']=basis['cache']
    return {},reply,'review' if verified else 'human',.95 if verified else 0,ground,False,False
