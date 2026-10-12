#!/usr/bin/env python3
# поток: mkt
"""Explain each of the 336 set-review rows using its actual card evidence.

Source facts stay unchanged. A failed matching rule is not automatically a
content error or proof that a component does not exist.
"""
from collections import Counter,defaultdict
import html
import json
import re

from ops import wb_bundle_regroup as regroup,wb_core_glue as core
from ops.wb_bundle_plan import char,color,is_bundle,normalized,number
from ops.wb_bundle_glue_singletons import now

SOURCE=regroup.ROOT/'docs/reports/mkt_wb_colour_chip_review_2026-10-12_acc1'
OUT=regroup.ROOT/'docs/reports/mkt_wb_colour_set_explanations_2026-10-12_acc1'


def alternative_tokens(model):
    # Diagnostic parsing only: it never changes a WB membership plan.
    pieces=re.split(r'\s*[/;,]\s*',model)
    if len(pieces)>1:return [t for piece in pieces for t in (core.tokens(piece) or [])]
    pair=re.fullmatch(r'\s*([A-Za-zА-Яа-я]+[- ]?\d+[A-Za-z]*)\s*-\s*([A-Za-zА-Яа-я]+[- ]?\d+[A-Za-z]*)\s*',model)
    if pair:
        left,right=pair.groups();a=re.fullmatch(r'(.*?)(\d+)([^\d]*)',left);b=re.fullmatch(r'(.*?)(\d+)([^\d]*)',right)
        if a and b and normalized(a[1])==normalized(b[1]) and normalized(a[3])==normalized(b[3]):
            lo,hi=sorted((int(a[2]),int(b[2])))
            if hi-lo<=30:return [normalized(a[1]+str(i).zfill(len(a[2]))+a[3]) for i in range(lo,hi+1)]
        return [normalized(left),normalized(right)]
    return []


def family_candidates(token,kit,records):
    """Suggestions for human review only, never certified glue components."""
    if not kit['printers'][1]:return []
    output=[]
    for r in records:
        if r['ref'].get('consumable_type') not in regroup.CARTRIDGES or r['printers'][1]!=kit['printers'][1]:continue
        title=normalized(r['ref'].get('title',''))
        inserted=any(title[:i]+title[i+1:]==token for i,ch in enumerate(title) if ch in 'KCMY' and color(char(r['card'],58854)) in {'K','C','M','Y'})
        suffix=title[len(token):] if title.startswith(token) else None
        if inserted or suffix in {'K','BK','C','M','Y','LC','LM','PC','PM','PBK','MBK','GY','PGY','CO','R','G','B'}:
            output.append(r)
    return output


def build(snapshot,source,plan):
    groups=defaultdict(list);own=defaultdict(list);parents={}
    cards={c['nmID']:c for c in snapshot['cards']}
    for c in snapshot['cards']:
        groups[c['imtID']].append(c)
        if is_bundle(c):own[regroup.base(c)].append(c)
        if re.fullmatch(r'\d{4}',c['vendorCode']):parents[c['vendorCode']]=c
    records=[]
    for code,c in parents.items():
        ref=source['references'].get(code,{})
        chip,evidence=regroup.chip_info(c,own[code],ref,source['marks'].get(code,[]))
        records.append({'card':c,'ref':ref,'chip':chip,'evidence':evidence,'printers':core.printers(c,ref),
                        'model_valid':normalized(char(c,5023))==normalized(ref.get('title',''))})
    indexed={r['card']['nmID']:r for r in records}
    result=[]
    for item in plan['review']:
        if item['type']!='Комплект картриджей':continue
        kit=indexed[item['anchor']];ref=kit['ref'];model=ref.get('title','');tokens=core.tokens(model) or []
        notes=[];categories=[];candidates=[]
        def note(category,text,action):
            categories.append(category);notes.append({'what':text,'next':action})
        if not kit['model_valid']:
            note('модель набора расходится',f'У набора WB в поле «Модель» стоит «{char(kit["card"],5023)}», а в ТК — «{model}». Поэтому состав проверялся по двум разным записям.',
                 'Проверить фактическое содержимое и исправить ошибочную запись модели или привязку ТК; не подбирать компоненты только по названию принтера.')
        if kit['chip']=='противоречие':
            note('явное противоречие чипа','У самого набора найдены одновременно такие явные обозначения: '+', '.join(kit['evidence'])+'. Это не пустое поле.',
                 'Сверить актуальные источники и конкретное исполнение набора; удалить только ошибочную запись/привязку после проверки.')
        if own[kit['card']['vendorCode']]:
            note('бандлы самого набора','У набора есть собственные карточки XN. Число упаковок не подтверждает количество отдельных картриджей внутри них.',
                 'Подтвердить содержимое этих упаковок отдельно. Бандлы наборов не включать в обычное правило одиночный картридж + X2.')
        requested_candidates={}
        for token in tokens:
            matches=[r for r in records if r['ref'].get('consumable_type') in regroup.CARTRIDGES and core.model_matches(token,r['card'],r['ref'])]
            requested_candidates[token]=matches
            for r in matches:
                if r not in candidates:candidates.append(r)
            if not matches:
                alt=alternative_tokens(model)
                alt_found=[r for r in records if r['ref'].get('consumable_type') in regroup.CARTRIDGES and any(core.model_matches(t,r['card'],r['ref']) for t in alt)]
                if alt_found:
                    for r in alt_found:
                        if r not in candidates:candidates.append(r)
                    note('запись состава не разобрана','Модель набора «'+model+'» обозначает несколько моделей или диапазон, который прежняя проверка прочитала как одну модель. После разбора записи отдельные модели найдены в каталоге.',
                         'Уточнить поддержку этой записи в проверке; затем проверить полный состав и принтеры. Карточки не объявлять отсутствующими.')
                elif re.search(r'\d+\s*шт|[XХ]\d+ШТ',model,re.I):
                    note('упаковка одной модели','«'+model+'» содержит количество одинаковых картриджей. Проверка цветных наборов пыталась найти это название как модель отдельного картриджа.',
                         'Отнести к упаковкам одной модели после проверки содержимого; цветовую склейку не искать по названию упаковки.')
                else:
                    other_types=[r for r in records if r['card']['nmID']!=kit['card']['nmID'] and r['ref'].get('consumable_type') not in regroup.CARTRIDGES|core.KIT_TYPES and core.model_matches(token,r['card'],r['ref'])]
                    # Separate WB-only matches reveal a reference-data issue,
                    # rather than absence from the whole marketplace catalogue.
                    wb=[r for r in records if r['card']['nmID']!=kit['card']['nmID'] and r['ref'].get('consumable_type') not in core.KIT_TYPES and normalized(char(r['card'],5023))==token]
                    related=family_candidates(token,kit,records)
                    for r in wb+other_types+related:
                        if r not in candidates:candidates.append(r)
                    if other_types:
                        note('найден компонент другого типа',f'Модель «{token}» найдена среди товаров типа '+', '.join(sorted({r['ref'].get('consumable_type') or 'не указан' for r in other_types}))+', а проверка искала только одиночный картридж.',
                             'Проверить классификацию набора и его физическое содержимое. Для барабана с картриджем применять правило компонентного набора, а не цветного.')
                    elif related:
                        note('название семейства вместо полного состава','В названии набора указана серия «'+model+'». Есть потенциальные отдельные цветовые модели с тем же полным списком принтеров, но их обозначения не совпадают с этой короткой записью. Они показаны ниже для проверки.',
                             'Подтвердить физический состав по моделям и цветам. Это ограничение чтения модели алгоритмом, а не доказательство отсутствия карточек; предложения пока не считаются готовыми к переносу.')
                    else:
                        note('отдельная модель не подтверждена',f'Для модели «{token}» не найден отдельный основной артикул с такой моделью в справочнике ТК.'+(' На WB похожая точная запись модели есть, но ТК/тип товара отличается.' if wb else ''),
                             'Проверить, входит ли эта модель в набор, есть ли её основная карточка и верно ли заполнена/привязана модель. Если карточки нет — передать создание контентщику.')
                continue
            model_valid=[r for r in matches if r['model_valid'] and r['chip']!='противоречие']
            if not model_valid:
                note('компонент исключён по данным',f'Карточки модели «{token}» найдены, но у всех модель WB расходится с ТК или есть явный конфликт по чипу.',
                     'Сверить карточки компонентов в таблице ниже. Исправить конкретную модель/привязку или конфликт, а не создавать дубликат.')
            same=[r for r in model_valid if r['printers']==kit['printers'] and kit['printers'][1]]
            if model_valid and not same:
                same_values=[r for r in model_valid if r['printers'][1]==kit['printers'][1] and kit['printers'][1]]
                if same_values:
                    note('списки равны, источники разные',f'Модель «{token}» найдена. Полные списки принтеров совпадают, но один взят из WB, другой из ТК; старая проверка требовала и одинаковый источник.',
                         'Уточнить проверку источников: само совпадение принтеров уже есть. Сверить качество списков и остальные поля.')
                else:
                    note('список принтеров отличается',f'Модель «{token}» есть в каталоге, но полный список принтеров набора и компонента различается или пуст. Это не отсутствие карточки.',
                         'Проверить конкретные недостающие/добавленные принтеры в таблице. Исправить опечатку или неполный список, если фактическая совместимость совпадает; иначе оставить отдельно.')
            eligible=[r for r in same if r['chip']==kit['chip'] or (kit['chip']==regroup.UNKNOWN) or (kit['chip']=='с чипом' and r['chip']==regroup.UNKNOWN)]
            if same and not eligible:
                note('чип компонента отличается',f'Модель «{token}» и принтеры совпадают, но набор имеет класс «{kit["chip"]}», а компоненты — '+', '.join(sorted({r['chip'] for r in same}))+'.',
                     'Выбрать соответствующее исполнение. Явный чип/отсутствие/без счётчика не заменять выводом по большинству.')
            bycolor=defaultdict(list)
            for r in eligible:bycolor[color(char(r['card'],58854))].append(r)
            duplicate=[(col,rr) for col,rr in bycolor.items() if len(rr)>1]
            if duplicate:
                descriptions=['цвет '+col+': '+', '.join(r['card']['vendorCode'] for r in rr) for col,rr in duplicate]
                note('несколько подходящих артикулов',f'Для модели «{token}» есть несколько подходящих основных артикулов одного цвета: '+'; '.join(descriptions)+'.',
                     'Сравнить ресурс, чип, бренд и исполнение в таблице. Это не доказательство дубликата; надо выбрать компонент именно этого набора.')
        exact=[r for r in candidates if r['printers']==kit['printers'] and r['model_valid'] and r['chip']!='противоречие']
        expected=(ref.get('raw') or {}).get('colors_count')
        actual=[color(char(r['card'],58854)) for r in exact]
        if item['reason'].startswith(('Число цветов','Цветной состав','Не найдены все C/M/Y')):
            combined=[r for r in exact if regroup.colored_card(r['card']) and color(char(r['card'],58854)) not in {'C','M','Y','LC','LM'}]
            if combined:
                note('многоцветный картридж посчитан как один цвет','В составе есть многоцветный картридж. Прежняя проверка сравнивала число картриджей/различных значений поля цвета с числом чернил из ТК. Для «чёрный + многоцветный» это разные величины.',
                     'Проверять модели и количество физических картриджей по составу набора. Эта формулировка сама по себе не доказывает ошибку карточек.')
            else:
                note('состав или количество компонентов не сходится',f'ТК указывает {expected if expected is not None else "неуказанное число"} цветов; после отбора найдены {len(exact)} основных карточек, значения цвета: '+(', '.join(actual) or 'нет')+'.',
                     'Уточнить фактический состав набора. Два чёрных разных типов или отсутствующий чёрный нельзя различить только по числу цветов; смотреть модели и принтеры ниже.')
        if 'исходной группе' in item['reason'] or 'Бандл стоит' in item['reason']:
            foreign=item.get('foreign',[])
            note('нужно разобрать существующую группу целиком',('В исходной склейке есть дополнительные основные карточки: '+', '.join(map(str,foreign))+'.') if foreign else 'Карточка/бандл уже объединён с другими товарами. Простое перемещение может разобрать полезную текущую склейку.',
                 'Посмотреть полный текущий состав по ссылкам и определить, кого сохранить вместе. Не переносить один найденный компонент без решения по остальным.')
        if 'Несколько наборов претендуют' in item['reason']:
            note('несколько наборов используют одни компоненты','Одни и те же отдельные картриджи подходят нескольким наборам. Автоматические независимые переносы будут мешать друг другу.',
                 'Составить одну общую группу для всей семьи, если модели/чипы совместимы, или выбрать набор. Проверить лимит 30 и правило X2.')
        if 'Набор не подтверждён как цветная' in item['reason']:
            note('набор не является цветовой серией','У найденных компонентов не подтверждены разные цвета. Остановка цветовой проверки не означает, что нельзя объединять такой набор по другому правилу.',
                 'Определить физический состав: несколько одинаковых картриджей, пара разных ресурсов либо компонентный набор. Цветовую склейку здесь не применять.')
        if not notes:
            note('нужна проверка состава','Не удалось подтвердить весь состав набора по имеющимся полям. Подробные модели, чипы и принтеры приведены ниже.',
                 'Проверить конкретные компоненты и исходную склейку; исходная формулировка сохранена для сопоставления.')
        def fact(r):
            c=r['card'];kp=set(kit['printers'][1]);cp=set(r['printers'][1])
            return {'nm_id':c['nmID'],'vendor_code':c['vendorCode'],'title':c['title'],'model_wb':char(c,5023),'model_tc':r['ref'].get('title',''),
                    'type':r['ref'].get('consumable_type','не указан'),'color':char(c,58854),'resource':char(c,88963),'chip':r['chip'],'chip_evidence':r['evidence'],
                    'chip_tc':{'chip':'с чипом','nochip':'без чипа','chip_free':'чип без счётчика'}.get(r['ref'].get('chip'),'не указан'),
                    'chip_supplier_mentions':source['marks'].get(c['vendorCode'],[]),
                    'wb_complectation':char(c,378533),
                    'wb_chip_sentences':[sentence.strip() for sentence in re.split(r'(?<=[.!?])\s+',c.get('description','')) if re.search(r'чип|chip',sentence,re.I)],
                    'printer_source':r['printers'][0],'printers':list(r['printers'][1]),
                    'printers_only_in_kit':sorted(kp-cp),'printers_only_in_component':sorted(cp-kp),'imt_id':c['imtID']}
        result.append({'number':len(result)+1,'original_reason':item['reason'],'kit':fact(kit),'quantity_wb':char(kit['card'],179792),
                       'expected_colours_tc':expected,'categories':sorted(set(categories)),'notes':notes,'candidates':[fact(r) for r in candidates],
                       'original_group':[{'nm_id':c['nmID'],'vendor_code':c['vendorCode'],'title':c['title']} for c in groups[kit['card']['imtID']]],
                       'source_observed_at':snapshot['read_at']})
    return result


def render(result):
    def esc(x):return html.escape(str(x))
    def link(c):return f'<a target="_blank" href="https://www.wildberries.ru/catalog/{c["nm_id"]}/detail.aspx">{c["nm_id"]} · {esc(c["vendor_code"])} · {esc(c["title"])}</a>'
    def fact(c):
        chip='<td>'+esc(c['chip'])+'<details><summary>Из каких данных получено</summary>ТК: '+esc(c['chip_tc'])+'<br>Упоминания поставщиков/прайсов: '+esc(', '.join(c['chip_supplier_mentions']) or 'нет явных упоминаний')+'<br>WB «Комплектация»: '+esc(c['wb_complectation'] or 'пусто')+'<br>Описание WB: '+esc(' '.join(c['wb_chip_sentences']) or 'нет упоминания')+'</details></td>'
        return '<tr><td>'+link(c)+'</td>'+''.join('<td>'+esc(c[key])+'</td>' for key in ['type','model_wb','model_tc','color','resource'])+chip+'<td>'+esc(c['printer_source'])+'<details><summary>Полный список принтеров</summary>'+esc(', '.join(c['printers']) or 'пусто')+'</details></td><td>'+esc('Только у набора: '+(', '.join(c['printers_only_in_kit']) or 'нет')+'; только у компонента: '+(', '.join(c['printers_only_in_component']) or 'нет'))+'</td></tr>'
    body=[]
    for row in result:
        notes=''.join('<li><b>'+esc(n['what'])+'</b><br>Что сделать: '+esc(n['next'])+'</li>' for n in row['notes'])
        columns='<tr><th>Карточка</th><th>Тип товара ТК</th><th>Модель WB</th><th>Модель ТК</th><th>Цвет</th><th>Ресурс</th><th>Чип</th><th>Принтеры/источник</th><th>Отличие принтеров от набора</th></tr>'
        body.append('<details class="case" id="sku-'+str(row['kit']['nm_id'])+'"><summary>№'+str(row['number'])+' — '+link(row['kit'])+' — '+esc('; '.join(row['categories']))+'</summary><ol>'+notes+'</ol><p>Поле количества WB: '+esc(row['quantity_wb'])+'; число цветов из ТК: '+esc(row['expected_colours_tc'])+'. Эти поля не обязательно считают одно и то же.</p><div class="scroll"><table>'+columns+fact(row['kit'])+''.join(fact(c) for c in row['candidates'])+'</table></div><details><summary>Кто был в исходной склейке</summary>'+'<br>'.join(link(c) for c in row['original_group'])+'</details><details><summary>Прежняя формулировка для сопоставления</summary>'+esc(row['original_reason'])+'</details></details>')
    counts=Counter(c for r in result for c in r['categories'])
    text='<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>336 наборов: понятный разбор — acc1</title><style>body{max-width:1300px;margin:28px auto;padding:20px;font:16px system-ui;line-height:1.5}a{color:#14615c}.case{padding:15px;border-bottom:1px solid #bfc9c2}summary{cursor:pointer}table{border-collapse:collapse}td,th{padding:8px;border:1px solid #bfc9c2;vertical-align:top}.scroll{overflow:auto}input{padding:12px;width:min(90%,650px)}</style><h1>336 наборов: что конкретно мешает — acc1</h1>'
    text+='<p>Повторная проверка на основе полного каталога WB и ТК, с конкретными кандидатами и различиями принтеров. Здесь не 336 доказанных ошибок: часть прежних остановок вызвана ограничением алгоритма или записью состава. Содержание карточек не меняли.</p>'
    text+='<p>Категории пересекаются: '+esc('; '.join(f'{k}: {v}' for k,v in counts.most_common()))+'</p><input placeholder="Поиск по артикулу, модели, причине" oninput="document.querySelectorAll(\'.case\').forEach(x=>x.hidden=!x.textContent.toLowerCase().includes(this.value.toLowerCase()))">'+''.join(body)+'</html>'
    text=text.replace('</html>','<script>if(location.hash){const x=document.getElementById(location.hash.slice(1));if(x){x.open=true;x.scrollIntoView();}}</script></html>')
    (OUT/'index.html').write_text(text)
    main=SOURCE/'index.html'
    if main.exists():
        original=main.read_text()
        marker='<h2>Что ещё мешает набору — 336</h2>'
        if marker in original:
            start=original.index(marker)
            original=original[:start]+marker+'<p>Все 336 случаев перепроверены. <a href="/api/wb-bids/reports/glue-colour-set-details-2026-10-12">Понятные объяснения: что конкретно не сходится, найденные компоненты и что проверить</a>. Прежнее общее «не найден» заменено разбором наличия, моделей, принтеров и исполнения.</p></html>'
            main.write_text(original)


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    snapshot=json.loads((SOURCE/'catalog.json').read_text());source=json.loads((SOURCE/'references.json').read_text());plan=json.loads((SOURCE/'core_new.json').read_text())
    result=build(snapshot,source,plan)
    regroup.save(OUT/'details.json',{'checked_at':now(),'account':'wb_acc1','rows':result})
    render(result)
    counts=Counter(c for r in result for c in r['categories'])
    regroup.save(OUT/'summary.json',{'checked_at':now(),'rows':len(result),'categories':dict(counts),'marketplace_writes':0})
    print('Rows rechecked:',len(result),'categories:',dict(counts),flush=True)


if __name__=='__main__':main()
