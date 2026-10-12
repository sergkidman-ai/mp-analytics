#!/usr/bin/env python3
# поток: mkt
"""Read-only colour/set review under Sergey's 12.10 missing-chip rule.

Stores fresh raw content separately, compares old/new rules on the same input,
and publishes a finite review list. This command never writes to WB.
"""
import argparse
from collections import Counter
import html
import json
from pathlib import Path

from ops import wb_bundle_regroup as regroup, wb_core_glue as core
from ops.wb_bundle_glue_singletons import now, read_references
from ops.wb_bundle_plan import fetch_catalog, is_bundle

OUT=regroup.ROOT/'docs/reports/mkt_wb_colour_chip_review_2026-10-12_acc1'


def page(summary, snapshot, source, new_bundle, new_core):
    byid={c['nmID']:c for c in snapshot['cards']}
    parents={c['vendorCode']:c for c in snapshot['cards'] if c['vendorCode']==regroup.base(c)}
    own={}
    for c in snapshot['cards']:
        if is_bundle(c):own.setdefault(regroup.base(c),[]).append(c)
    chips={code:regroup.chip_info(c,own.get(code,[]),source['references'].get(code,{}),source['marks'].get(code,[]))[0]
           for code,c in parents.items()}
    def card_link(nm):
        c=byid[nm]
        return (f'<a target="_blank" href="https://www.wildberries.ru/catalog/{nm}/detail.aspx">'
                f'{nm} · {html.escape(c["vendorCode"])} · {html.escape(c["title"])}</a>')
    def section(title,ops):
        rows=[]
        for op in ops:
            parts=''.join('<li>'+ '<br>'.join(card_link(n) for n in p)+'</li>' for p in op['partitions'])
            inf=op.get('chip_inference',{})
            codes=sorted({code for mapping in inf.values() for code in mapping}) if isinstance(inf,dict) else sorted(inf)
            inferred=html.escape(', '.join(codes))
            rows.append(f'<details><summary>{card_link(op["anchor"])}</summary><p>Чип не был указан: {inferred}. Это решение о склейке, поля карточек не меняем.</p><p>Предложенный состав:</p><ol>{parts}</ol></details>')
        return f'<h2>{title} — {len(ops)}</h2>'+''.join(rows)
    ready_bundle=[o for o in new_bundle['operations'] if o['id'] in summary['new_bundle_ids']]
    ready_core=[o for o in new_core['operations'] if o['id'] in summary['new_core_ids']]
    inferred=[]
    for root,mapping in new_bundle['colour_chip_inferences'].items():
        cc=[c for c in snapshot['cards'] if str(c['imtID'])==root and not is_bundle(c)]
        known=[c['vendorCode'] for c in cc if chips.get(c['vendorCode'])=='с чипом']
        inferred.append('<details><summary>'+card_link(cc[0]['nmID'])+f' — {len(cc)} основных, пробелы у {len(mapping)}</summary>'
                        +'<br>'.join(card_link(c['nmID'])+' — '+html.escape(chips.get(c['vendorCode'],'?')) for c in cc)
                        +f'<p>Чип подтверждён у артикулов {html.escape(", ".join(known))}. Не указан у {html.escape(", ".join(mapping))}. '
                        +'Модели состава набора и полный список принтеров совпадают, явного противоположного исполнения нет. Пробел не мешает этой склейке.</p></details>')
    remaining=[]
    for r in new_core['review']:
        if r['type']!='Комплект картриджей':continue
        remaining.append('<li>'+card_link(r['anchor'])+': '+html.escape(r['reason'])+'</li>')
    text='''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Цветные наборы: новая логика чипов — acc1</title><style>body{max-width:1150px;margin:24px auto;padding:20px;font:17px system-ui;line-height:1.5;color:#172622;background:#fafaf7}a{color:#14615c}details{padding:12px;border-bottom:1px solid #ccd5d0}summary{cursor:pointer}table{border-collapse:collapse}td,th{padding:9px;border:1px solid #bbc8bf}</style><h1>Цветные картриджи и наборы — acc1</h1>'''
    text+=f'<p>Свежий каталог WB: {html.escape(snapshot["read_at"])}; {len(snapshot["cards"])} карточек. Сравнение старого и нового правила на одном составе. Этот проход проверяет и готовит список, переносов не выполняет.</p>'
    text+='<p><b>Правило:</b> точная цветовая серия и её набор; совпадает полный список принтеров. Неуказанный чип у набора/части цветов допускается, когда подтверждённые компоненты имеют чип и нет явного противоположного исполнения. Явные «без чипа», «без счётчика» и противоречия не перекрываются большинством. Полностью неизвестная серия не объявляется серией с чипом.</p>'
    text+='<table><tr><th>Показатель</th><th>Количество</th></tr>'+''.join(f'<tr><td>{html.escape(label)}</td><td>{summary[key]}</td></tr>' for key,label in [('colour_groups_resolved','Уже существующие группы с допустимыми пробелами'),('unknown_family_codes_resolved','В них внешних кодов с неуказанным чипом'),('new_bundle_ready','Дополнительно готовые группы: отделить большие бандлы'),('new_core_ready','Дополнительно готовые группы: доклеить недостающие компоненты'),('core_chip_block_before','Наборы с препятствием по чипу — до'),('core_chip_block_after','Наборы с препятствием по чипу — после')])+'</table>'
    text+=section('Новые готовые переразборы существующих групп',ready_bundle)
    text+=section('Новые готовые склейки набора с компонентами',ready_core)
    text+=f'<h2>Существующие серии, где пробел допустим — {len(inferred)}</h2>'+''.join(inferred)
    text+=f'<h2>Что ещё мешает набору — {len(remaining)}</h2><p>Список не означает ошибку каждой карточки: здесь и разные исполнения, и неполные/неоднозначные модели, принтеры, посторонние участники.</p><ul>'+''.join(remaining)+'</ul></html>'
    (OUT/'index.html').write_text(text,encoding='utf-8')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--fresh',action='store_true');args=parser.parse_args()
    OUT.mkdir(parents=True,exist_ok=True)
    if args.fresh or not (OUT/'catalog.json').exists():
        cards=fetch_catalog('wb_acc1');snapshot={'read_at':now(),'cards':cards}
        regroup.save(OUT/'catalog.json',snapshot)
        refs,marks=read_references({regroup.base(c) for c in cards}-{None})
        regroup.save(OUT/'references.json',{'references':refs,'marks':{k:sorted(v) for k,v in marks.items()}})
    snapshot=json.loads((OUT/'catalog.json').read_text());source=json.loads((OUT/'references.json').read_text())
    old_bundle=regroup.prepare(snapshot,source,allow_colour_gaps=False)
    new_bundle=regroup.prepare(snapshot,source)
    old_core=core.prepare(snapshot,source,allow_colour_gaps=False);new_core=core.prepare(snapshot,source)
    for label,plan in [('bundle_old',old_bundle),('bundle_new',new_bundle),('core_old',old_core),('core_new',new_core)]:
        regroup.save(OUT/(label+'.json'),plan)
    nb={o['id'] for o in new_bundle['operations']}-{o['id'] for o in old_bundle['operations']}
    nc={o['id'] for o in new_core['operations']}-{o['id'] for o in old_core['operations']}
    chip=lambda p:sum('чип' in r['reason'].lower() for r in p['review'] if r['type']=='Комплект картриджей')
    mapping=new_bundle['colour_chip_inferences']
    summary={'account':'wb_acc1','checked_at':now(),'catalog_cards':len(snapshot['cards']),
             'colour_groups_resolved':len(mapping),'unknown_family_codes_resolved':len({code for x in mapping.values() for code in x}),
             'new_bundle_ready':len(nb),'new_core_ready':len(nc),'new_bundle_ids':sorted(nb),'new_core_ids':sorted(nc),
             'core_chip_block_before':chip(old_core),'core_chip_block_after':chip(new_core),
             'bundle_old_ready':len(old_bundle['operations']),'bundle_new_ready':len(new_bundle['operations']),
             'core_old_ready':len(old_core['operations']),'core_new_ready':len(new_core['operations']),
             'core_complete_before':old_core['already_complete'],'core_complete_after':new_core['already_complete'],
             'remaining_colour_reasons':dict(Counter(r['reason'] for r in new_core['review'] if r['type']=='Комплект картриджей'))}
    regroup.save(OUT/'summary.json',summary);page(summary,snapshot,source,new_bundle,new_core)
    print(json.dumps({k:v for k,v in summary.items() if k not in {'remaining_colour_reasons','new_bundle_ids','new_core_ids'}},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
