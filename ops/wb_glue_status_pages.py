#!/usr/bin/env python3
# поток: mkt
"""Human-readable 12-point acc1 report and a qualified content-creation queue."""
from collections import Counter
import html
import json
from pathlib import Path
import re

from ops.wb_glue_status_report import OUT
from ops.wb_bundle_plan import char,normalized
from ops import wb_bundle_regroup as regroup
from ops import wb_core_glue as core


def esc(v):return html.escape(str(v if v is not None else 'не указано'))


def link(c):
    if not c:return 'нет активной основной карточки'
    return f'<a target="_blank" href="{c.get("url") or "https://www.wildberries.ru/catalog/"+str(c["nm_id"])+"/detail.aspx"}">{c.get("code",c.get("vendor_code",""))} · {esc(c["title"])}</a>'


def doc(title,body):
    return '<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+esc(title)+'</title><style>body{max-width:1250px;margin:28px auto;padding:20px;font:16px system-ui;line-height:1.55;background:#fafaf7;color:#182721}a{color:#14615c}table{border-collapse:collapse;width:100%}td,th{padding:8px;border:1px solid #bbc8c0;vertical-align:top;text-align:left}.scroll{overflow:auto}details{padding:10px;border-bottom:1px solid #bbc8c0}summary{cursor:pointer}h2{margin-top:34px}input{padding:10px;width:min(90%,650px)}</style><h1>'+esc(title)+'</h1>'+body+'</html>'


def table(headers,rows):return '<div class="scroll"><table><tr>'+''.join('<th>'+esc(h)+'</th>' for h in headers)+'</tr>'+''.join('<tr>'+''.join('<td>'+v+'</td>' for v in row)+'</tr>' for row in rows)+'</table></div>'


def main():
    s=json.loads((OUT/'summary.json').read_text());d=json.loads((OUT/'details.json').read_text());gap=json.loads((OUT/'laser_gaps/plan.json').read_text());state=json.loads((OUT/'laser_gaps/apply_results.json').read_text())
    snap=json.loads((OUT/'catalog.json').read_text());parents={c['vendorCode']:c for c in snap['cards'] if re.fullmatch(r'\d{4}',c['vendorCode'])}
    refs={r['external_code']:r for r in json.loads((OUT/'all_models.json').read_text())}
    all_kits=[c for code,c in parents.items() if refs.get(code,{}).get('consumable_type')=='Комплект картриджей']
    done={r['id'] for r in state['results'] if r['status']=='confirmed'}
    qualified=[];uncertain=[]
    def diagnostic_signature(model):
        return re.sub(r'\d+',lambda m:str(int(m[0])),normalized(model))
    for row in gap['create']:
        missing=[m for m in row['missing_colors'] if m.get('code') and m.get('model')]
        undecided=[m for m in row['missing_colors'] if not m.get('code') or not m.get('model')]
        existing_alt=[]
        for m in missing[:]:
            matches=[c for c in parents.values() if diagnostic_signature(char(c,5023))==diagnostic_signature(m['model'])]
            if matches:
                existing_alt.append({'missing':m,'cards':[{'nm_id':c['nmID'],'code':c['vendorCode'],'title':c['title']} for c in matches]});missing.remove(m)
        full_four={r['color'] for r in row['components']}=={'K','C','M','Y'}
        kit_alternatives=[]
        if row['kit_missing'] and full_four:
            for c in all_kits:
                ref=refs[c['vendorCode']];tokens=core.tokens(ref['title']) or []
                def matches(token,part):
                    model_ref={'title':part['model'],'consumable_type':'Картридж'}
                    virtual={'characteristics':[{'id':58854,'value':[{'K':'Черный','C':'Голубой','M':'Пурпурный','Y':'Желтый'}[part['color']]]}]}
                    return core.model_matches(token,virtual,model_ref)
                if tokens and all(any(matches(t,p) for t in tokens) for p in row['components']) and all(any(matches(t,p) for p in row['components']) for t in tokens):
                    kit_alternatives.append({'nm_id':c['nmID'],'code':c['vendorCode'],'title':c['title']})
        if missing or (row['kit_missing'] and full_four and not existing_alt and not kit_alternatives):
            qualified.append({**row,'missing_colors':missing,'kit_missing':row['kit_missing'] and full_four and not existing_alt and not kit_alternatives,
                              'kit_composition':[r['model'] for r in row['components']]})
        if undecided or existing_alt or kit_alternatives or (row['kit_missing'] and not full_four):
            uncertain.append({**row,'undetermined_models':undecided,'same_model_other_codes':existing_alt,
                              'existing_set_candidates':kit_alternatives,
                              'reason':'Сначала определить точную модель цвета/вариант или сопоставить существующие артикулы; не создавать дубликат по одному принтеру'})
    creation_summary={'qualified_families':len(qualified),'specific_single_models_missing':sum(len(r['missing_colors']) for r in qualified),
                      'new_sets_proposed':sum(r['kit_missing'] for r in qualified),'manual_before_creation_families':len(uncertain)}
    regroup.save(OUT/'content_creation.json',{'summary':creation_summary,'qualified':qualified,'manual':uncertain})
    laser_body='<p>По acc1. Использованы явные семейства моделей ТК, полный список принтеров WB и известные исполнения чипов. Модель, уже присутствующая под другим артикулом, отправляется на сверку, а не на создание дубликата.</p>'
    laser_body+='<p>Подтверждены '+str(len(done))+' доклеек по API. Они объединяют найденные компоненты; отдельные семейства остаются неполными из-за отсутствующих моделей/набора.</p>'
    known_set_alternatives={(r['family'],r['chip']):r['existing_set_candidates'] for r in uncertain if r['existing_set_candidates']}
    for r in gap['records']:
        components='<br>'.join(esc(c['color']+' / '+c['model'])+' — '+link(c['card']) for c in r['components'])
        missing='<br>'.join(esc((m.get('model') or 'точную модель уточнить')+' / '+m['color']+': '+m['why']) for m in r['missing_colors']) or 'все модели цветов найдены'
        outcome='Доклейка выполнена и подтверждена' if r.get('operation_id') in done else 'Автоматического переноса нет'
        kit_cell=link(r['kit'])
        alternatives=r.get('other_kits',[])+known_set_alternatives.get((r['family'],r['chip']),[])
        if alternatives:kit_cell+='<br>Другие найденные наборы: проверить чип/состав, не создавать дубликат<br>'+'<br>'.join(link(c) for c in alternatives)
        laser_body+='<details><summary>'+esc(r['family']+' · '+r['chip'])+' — '+outcome+'</summary>'+table(['Что есть','Набор','Чего не хватает / что мешает'],[[components,kit_cell,missing+'<br>'+esc('; '.join(r['reasons']))]])+'<details><summary>Полные списки принтеров</summary>WB: '+esc(', '.join(r.get('printers_wb',[])))+'<br>ТК: '+esc(', '.join(r['printers']))+'</details></details>'
    laser_body+='<h2>Очередь контентщику — отсутствующие основные и новые наборы</h2><p>'+esc(creation_summary)+'</p><p>Перед повторным заведением основной проверить, не удалена ли она намеренно. Для похожих обозначений моделей (включая различие ведущих нулей) предложена ручная сверка, а не создание дубликата. Такая диагностическая схожесть не использовалась для автоматической склейки.</p>'
    for r in qualified:
        laser_body+='<details><summary>'+esc(r['family']+' · '+r['chip'])+'</summary><p>Отсутствующие основные артикулы: '+esc('; '.join(m['code']+' '+m['model'] for m in r['missing_colors']) or 'нет')+'</p><p>Предлагаемый состав нового набора: '+esc(' + '.join(r['kit_composition']) if r['kit_missing'] else 'набор не требуется создавать')+'</p><p>Имеющиеся компоненты:</p>'+'<br>'.join(link(c['card']) for c in r['components'])+'<p>'+esc(r['caution'])+'</p></details>'
    laser_body+='<h2>Перед созданием нужно уточнить модель или исполнение</h2>'
    for r in uncertain:
        laser_body+='<details><summary>'+esc(r['family']+' · '+r['chip'])+'</summary><p>'+esc(r['reason'])+'</p><p>Цвета без точной подтверждённой модели: '+esc(', '.join(m['color'] for m in r['undetermined_models']))+'</p>'+'<br>'.join(link(c) for m in r['same_model_other_codes'] for c in m['cards'])+('<p>Найденные наборы этого состава: проверить чипы и полные списки принтеров перед решением о создании.</p>'+'<br>'.join(link(c) for c in r['existing_set_candidates']) if r['existing_set_candidates'] else '')+'</details>'
    laser_body+='<h2>Семейство не заполнено в ТК</h2>'+table(['Код','Модель','Что проверить'],[[esc(r['code']),esc(r['model']),esc(r['reason'])] for r in gap['unresolved_family_codes']])
    (OUT/'laser_gaps/index.html').write_text(doc('Цветные лазерные: доклейки и список на создание',laser_body))
    pairs=s['drums'];ad=s['advertising']
    overview=[
      ['1','Цветные лазерные',f'{len(done)} доклеек выполнено; {creation_summary["specific_single_models_missing"]} конкретных моделей отсутствуют под своим основным кодом; {creation_summary["new_sets_proposed"]} наборов предложено создать. Неопределённые варианты — отдельно.'],
      ['2','Бандлы X2 со своим родителем',f'{s["x2_with_parent"]}; без своего родителя {s["x2_without_parent"]}. Разбивка по видам ниже.'],
      ['3','Родители со всеми X2/X6/X10',f'{s["cartridge_parents_all_bundles"]} картриджевых SKU; {s["parents_all_bundles"]} родителей всех типов. Это фактический состав, не отметка полного соответствия всем правилам.'],
      ['4','Разделение разных ресурсов',f'{s["resource_split_parent_skus"]} уникальных родительских SKU в {s["resource_split_operations"]} подтверждённых операциях.'],
      ['5','Отдельные группы только X6+X10',f'{s["x6_x10_only_groups"]} групп / {s["x6_x10_only_bundle_skus"]} бандла. Без родителей и без X2.'],
      ['6','Фотобарабан + картридж',f'{pairs["existing_pair_groups"]} существующая смешанная группа; в {pairs["existing_pair_groups_without_kit"]} не найден соответствующий набор, {pairs["existing_pair_groups_set_ambiguous"]} требует проверки имеющегося набора. По точным парам ТК ещё {pairs["tc_pair_cases_not_complete"]} неполных случаев, затрагивающих {pairs["current_groups_in_tc_gap_cases"]} групп.'],
      ['7','Заправка с полной серией',f'Заправочные комплекты: {s["refill_kit_full"]}; отдельный тонер для заправки: {s["toner_refill_full"]}. Это два разных типа ТК.'],
      ['8','Дочерние по принтерам',f'{s["printer_families"]} семей с несколькими дочерними; у {s["printer_families_with_leader"]} найден кандидат-лидер по рейтингу отзывов. Подтверждённые перемещения дочерних нашими скриптами: {s["printer_child_skus_moved"]}.'],
      ['9','Новинки 01.09–11.10',f'{s["new_wb_primary_cards"]} новых активных основных карточек WB. Отдельно {s["new_tc_model_refs"]} впервые наблюдённых записей ТК: это не автоматически новые модели в ассортименте WB.'],
      ['10','Реклама',f'{ad["active_skus"]} уникальных SKU из {s["assortment_skus"]} ({ad["coverage_pct"]}%) в работающих кампаниях; все эти SKU есть в работающих CPC. Неархивных кампаний: {ad["campaigns"]}.'],
      ['11','Продажи бандлов',f'С 01.09 подтверждено {s["bundle_sales_since_sep1"]} реализаций. С заказом после подтверждённой нами склейки: {s["bundle_orders_after_verified_glue"]}. Подробные даты ниже; вывод об эффекте пока невозможен.'],
      ['12','Бандлы сейчас в склейках',f'{s["bundle_skus_joined"]} из {s["bundle_cards"]}; групп с бандлами {s["bundle_groups"]}. В одиночных группах остаётся {s["bundle_cards"]-s["bundle_skus_joined"]}.'],
    ]
    body=f'<p>Аккаунт wb_acc1. Полный каталог: {esc(s["catalog_read_at"])}; подтверждённые доклейки учтены до {esc(s["verified_overlay_at"])}. Рекламный состав: {esc(ad["read_at"])}. Числа SKU, семей и групп не складываются между собой.</p>'
    body+=table(['Пункт','Тема','Результат'],[[f'<a href="#p{r[0]}">{r[0]}</a>',esc(r[1]),esc(r[2])] for r in overview])
    body+='<h2 id="p1">1. Цветные лазерные</h2><p><a href="/api/wb-bids/reports/glue-laser-gaps-2026-10-12">Выполненные доклейки, конкретные отсутствующие модели, состав новых наборов и спорные варианты</a>.</p>'
    body+='<h2 id="p2">2. X2 со своим родителем — по видам</h2>'+table(['Вид','X2'],[[esc(k),str(v)] for k,v in s['x2_with_parent_by_type'].items()])
    for typ in s['x2_with_parent_by_type']:
        rr=[r for r in d['x2_with_parent'] if r['type']==typ]
        body+='<details><summary>'+esc(typ)+' — '+str(len(rr))+'</summary>'+table(['Основная карточка','Бандл X2 / размер склейки'],[[link(r['parent']),esc(r['bundle']['code']+' / '+str(r['members']))] for r in rr])+'</details>'
    body+='<h2 id="p3">3. Все собственные X2/X6/X10 с основной</h2>'+table(['Вид','Родителей'],[[esc(k),str(v)] for k,v in s['full_by_type'].items()])
    body+='<details><summary>Полный список родителей</summary>'+table(['Основная','Вид','Кратности'],[[link(r['parent']),esc(r['type']),esc(r['quantities'])] for r in d['full_families']])+'</details>'
    body+='<h2 id="p4">4. Ресурс: подтверждённые разделения</h2><p>Считаются по журналу выполненных переносов; повторно встречающийся SKU считается один раз.</p>'+table(['Время','Разделённые родители'],[[esc(r['at']),'<br>'.join(link(c) for c in r['parents'])] for r in d['resource_splits']])
    body+='<h2 id="p5">5. Отдельные X6+X10</h2><details><summary>Все '+str(s['x6_x10_only_groups'])+' группы</summary>'+table(['Карточка группы','Вид','Артикулы бандлов'],[[link(r['anchor']),esc(r['type']),esc(', '.join(c['code'] for c in r['bundles']))] for r in d['x6_x10_groups']])+'</details>'
    body+='<h2 id="p6">6. Фотобарабан, картридж и набор</h2><p>Цветные барабаны и комплекты фотобарабанов исключены. Существующие смешанные группы и неполные точные пары ТК — пересекающиеся списки, их нельзя складывать.</p>'
    body+='<details><summary>Существующие смешанные группы без подтверждённого набора</summary>'+table(['Барабаны','Картриджи','Набор'],[['<br>'.join(link(c) for c in r['drums']),'<br>'.join(link(c) for c in r['cartridges']),('Есть другой набор: уточнить состав' if r['needs_set_content_check'] else 'Соответствующий набор в этой склейке не найден')] for r in d['drum_gaps']['mixed_groups'] if r['kit_missing'] or r['needs_set_content_check']])+'</details>'
    body+='<details><summary>Неполные пары по конкретным моделям ТК — '+str(pairs['tc_pair_cases_not_complete'])+'</summary>'+table(['Модель набора / карточка','Найденные компоненты','Что мешает'],[[esc(r['kit_model'])+'<br>'+link(r['kit']),'<br>'.join(link(c) for c in r['components']),esc(('Не подтверждены отдельные основные модели: '+', '.join(r['missing_models']) if r['missing_models'] else 'Все отдельные модели найдены, но расположены в разных группах')+('; полный список принтеров расходится' if not r['full_printers_match'] else '')+('; несколько исполнений компонента — ручной выбор' if r['ambiguity'] else ''))] for r in d['drum_gaps']['pair_cases']])+'</details>'
    body+='<h2 id="p7">7. Заправочные комплекты и тонер</h2><p>На WB есть '+str(s['refill_kit_primary_skus'])+' основных заправочных комплектов; собственные бандлы найдены у '+str(s['refill_kit_parents_with_bundles'])+'. Поэтому полной серии X2/X6/X10 у этих комплектов нет. «Тонер» — другой тип товара, его считаем отдельно.</p>'+table(['Тип','Полные семьи X2/X6/X10'],[['Заправочный комплект',str(s['refill_kit_full'])],['Тонер',str(s['toner_refill_full'])]])
    body+='<h2 id="p8">8. Принтерные дочерние: работа и лидеры</h2><p>Наших подтверждённых переносов дочерних в журнале нет. Лидеры ниже — кандидаты, выявленные для этого отчёта. Использован последний наблюдённый рейтинг отзывов WB 0–5; оценка наполнения карточки 0–10 не использовалась. При одинаковом рейтинге — больше заказов и открытий с августа.</p><details><summary>Семьи с кандидатом-лидером — '+str(s['printer_families_with_leader'])+'</summary>'+table(['Родительский код','Лидер','Рейтинг / дата наблюдения','Дочерних / нынешних групп'],[[esc(r['code']),link(r['leader']),esc(str(r['rating'])+' / '+str(r['rating_observed_at'])),esc(str(r['children'])+' / '+str(r['current_groups']))] for r in d['printer_leaders'] if r['leader']])+'</details>'
    body+='<h2 id="p9">9. Новые основные карточки WB: 01.09–11.10 включительно</h2><p>Дата — createdAt Content API, а не день обновления или загрузки в нашу БД. Бандлы и дочерние по принтерам исключены. Это дата появления карточки на WB; она не доказывает дату появления самой модели у производителя.</p>'+table(['Новая основная','Дата WB','Вид','Первое наблюдение модели ТК'],[[link(r['card']),esc(r['created_at'][:10]),esc(r['type']),esc(r['tc_first_seen'][:10])] for r in d['new_primary']])
    body+='<details><summary>Первые наблюдения моделей в ТК за тот же период — '+str(s['new_tc_model_refs'])+'</summary>'+table(['Код / модель','Дата ТК','Карточка acc1'],[[esc(r['code']+' / '+r['model']),esc(r['first_seen'][:10]),link(r['card'])] for r in d['new_tc_models']])+'</details>'
    body+='<h2 id="p10">10. Рекламный охват ассортимента</h2><p>Состав кампаний прочитан напрямую через API, а не оценён по показам. '+str(ad['active_skus'])+' SKU в работающих кампаниях; вне всех неархивных кампаний '+str(ad['outside_ad'])+'. Повторяются между работающими кампаниями '+str(ad['active_duplicate_skus'])+' SKU; в общем количестве каждое учтено один раз.</p>'
    body+='<h2 id="p11">11. Продажи бандлов и дата склейки</h2><p>Источник — строки «Продажа / Продажа» финансового отчёта WB; услуги и компенсации исключены. Реализация не равна новому заказу: сравнивается дата заказа с подтверждёнными переносами. Реализации загружены по 11.10; новые заказы могут ещё не дойти до финансового отчёта.</p>'+table(['Бандл','Заказ','Реализация','Упаковок','Сейчас в склейке'],[[link(r['card']),esc(r['order_at']),esc(r['sale_at']),esc(r['qty']),'да' if r['currently_joined'] else 'нет'] for r in d['bundle_sales']])
    body+='<p>Подтверждённых реализаций с заказом после нашей склейки: '+str(s['bundle_orders_after_verified_glue'])+'. Единственная найденная продажа произошла до массовой склейки 07.10. По этим данным эффект увеличения продаж ещё не доказан.</p>'
    body+='<h2 id="p12">12. Есть ли бандлы в склейках</h2><p>Да: '+str(s['bundle_skus_joined'])+' из '+str(s['bundle_cards'])+' бандлов находятся в группах минимум по две карточки. Учитываются и родительские группы, и отдельные X6+X10/другие бандловые группы; это не сумма только X2 и полных семей.</p>'
    (OUT/'index.html').write_text(doc('Склейки acc1 — отчёт по 12 вопросам',body))
    regroup.save(OUT/'report_summary.json',{**s,'laser_creation':creation_summary,'laser_joins_confirmed':len(done)})
    print('Pages rendered. Creation queue:',creation_summary,flush=True)


if __name__=='__main__':main()
