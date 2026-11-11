# поток: mkt / ozon
"""Offline account-scoped native model map and article-family structural comparison."""
import collections
import csv
import html
import json
import re
import sys
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, '/opt/mp-analytics')
from collectors.launch_cards import classify

O = Path(__file__).parent / 'model_comparison'
O.mkdir(exist_ok=True)
BASE = O.parent
read = lambda p: json.loads(p.read_text())
save = lambda n,z: (O/n).write_text(json.dumps(z,ensure_ascii=False,indent=2)+'\n')
metrics, models, article_models, compositions = {}, {}, {}, {}
member_rows, group_rows, windows = [], [], {}
for account in ['oz_acc1','oz_acc2']:
    rows={}
    for p in (BASE/account/'raw').glob('info_all_*.json'):
        z=read(p);assert z['account']==account
        if z['http']==200:
            for r in z['response']['items']:rows[str(r['id'])]=(r,str(p.relative_to(BASE)))
    windows[account]=read(BASE/account/'acquisition_summary.json')['finished_at_utc']
    if account=='oz_acc2':
        p=BASE.parent/'ozon_dsprint_restore_20261108/independent_final_readback.json';z=read(p);assert z['account']==account
        for r in z['response']['items']:rows[str(r['id'])]=(r,'../ozon_dsprint_restore_20261108/independent_final_readback.json')
        windows['oz_acc2_restore_overlay']=z['at_utc']
    allg=collections.defaultdict(list);stockg=collections.defaultdict(list);family=collections.defaultdict(set)
    missing=0;missing_stock=0;shape=collections.Counter();live_count=0;stock_count=0
    for pid,(r,source) in rows.items():
        if r.get('is_archived') or r.get('is_autoarchived'):continue
        live_count+=1
        stock=r.get('stocks') or {};free=sum((max(D(0),D(str(s.get('present') or 0))-D(str(s.get('reserved') or 0))) for s in stock.get('stocks') or []),D(0))
        stocked=stock.get('has_stock') is True and free>0;stock_count+=stocked
        m=str((r.get('model_info') or {}).get('model_id') or '')
        role=classify(r['offer_id'],r['name'],account)
        kind='bundle_x'+str(role[2]) if role and role[1]=='bundle' else 'child' if (re.fullmatch(r'[0-9]{5,}',r['offer_id']) if account=='oz_acc1' else re.match(r'^[0-9]{5}',r['offer_id'])) else 'main'
        card={'account':account,'product_id':pid,'sku':str(r.get('sku') or ''),'offer_id':r['offer_id'],
              'model_id':m,'article_family':r['offer_id'][:4],'kind':kind,'in_stock':stocked,'source':source}
        member_rows.append(card)
        if not m:
            missing+=1;missing_stock+=stocked;continue
        allg[m].append(card)
        if stocked:
            stockg[m].append(card);family[card['article_family']].add(m)
    models[account]=(allg,stockg);article_models[account]=family
    for scope,groups,number in [('all_nonarchived',allg,live_count),('stock_nonarchived',stockg,stock_count)]:
        metrics[account+'_'+scope]={'cards':number,'model_ids_including_singletons':len(groups),
                                  'multi_SKU_groups_2plus':sum(len(v)>=2 for v in groups.values()),
                                  'single_SKU_model_ids':sum(len(v)==1 for v in groups.values()),
                                  'cards_inside_multi_SKU_groups':sum(len(v) for v in groups.values() if len(v)>=2),
                                  'missing_model_id':missing if scope=='all_nonarchived' else missing_stock}
        for mid,rr in groups.items():
            kinds=collections.Counter(r['kind'] for r in rr)
            signature=';'.join(f'{k}:{v}' for k,v in sorted(kinds.items()))
            group_rows.append({'account':account,'scope':scope,'model_id':mid,'our_card_count':len(rr),
                               'our_family_count':len({r['article_family'] for r in rr}),
                               'family_codes':';'.join(sorted({r['article_family'] for r in rr})),
                               'observed_our_member_signature':signature})
            if scope=='all_nonarchived':shape[signature]+=1
    compositions[account]=shape.most_common(8)

keys1=set(article_models['oz_acc1']);keys2=set(article_models['oz_acc2']);common=keys1&keys2
family_rows=[{'article_family':f,'acc1_has_stock_card':f in keys1,'acc2_has_stock_card':f in keys2,
              'acc1_native_models_touched_by_stock_cards':len(article_models['oz_acc1'].get(f,set())),
              'acc2_native_models_touched_by_stock_cards':len(article_models['oz_acc2'].get(f,set()))}
             for f in sorted(keys1|keys2)]
summary={'metrics':metrics,'source_windows_utc':windows,
         'observed_our_members_only_not_marketplace_total':True,
         'shared_numeric_model_ids_in_stock':len(set(models['oz_acc1'][1])&set(models['oz_acc2'][1])),
         'stock_article_family_counts':{'oz_acc1':len(keys1),'oz_acc2':len(keys2),'common':len(common),'union':len(keys1|keys2)},
         'article_family_Jaccard_percent':round(100*len(common)/len(keys1|keys2),2),
         'common_article_families_same_number_native_models':sum(len(article_models['oz_acc1'][f])==len(article_models['oz_acc2'][f]) for f in common),
         'top_group_composition_all_nonarchived':compositions,
         'numeric_model_id_not_a_cross_account_join_key':True,
         'article_family_similarity_not_proof_same_native_group_membership':True,
         'new_API_reads':0,'ad_mutations':0,'production_DB_writes':0}
save('model_comparison_summary.json',summary)
def output(name,rows):
    with (O/name).open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
output('native_model_groups.csv',group_rows);output('native_model_members.csv',member_rows);output('article_family_overlap.csv',family_rows)
slug='/api/wb-bids/reports/ozon-model-map-comparison-2026-11-08'
body='<h1>Склейки Ozon: акк1 и акк2</h1><p><b>Среди товаров в наличии число склеек с2+нашимиSKU почти одинаковое:1 834наакк1 и1 810наакк2.</b> Разница24группы,−1,31%. Большое отличие общего числа model_id вызвано одиночными карточками, прежде всего дочерними.</p>'
body+='<table><tr><th>Неархивные в наличии</th><th>Акк1</th><th>Акк2</th></tr>'
labels={'cards':'Карточек SKU','model_ids_including_singletons':'Все model_id, включая одиночные','multi_SKU_groups_2plus':'Группы с2+нашимиSKU','single_SKU_model_ids':'Одиночные model_id','cards_inside_multi_SKU_groups':'SKU внутри групп2+'}
for k,label in labels.items():body+=f'<tr><td>{label}</td><td>{metrics["oz_acc1_stock_nonarchived"][k]}</td><td>{metrics["oz_acc2_stock_nonarchived"][k]}</td></tr>'
body+='</table><p>Если взять все неархивные, включая нулевой остаток:групп2+наакк1 2 518,наакк2 2 522. Всеmodel_id соответственно11 726и6 330;у3карточекакк2 model_idнеуказан,ониневналичии. Отсутствующее значение не считалосьсклейкой.</p>'
body+='<h2>Насколько схожа структура</h2><p>Самая частая базовая структура акк1:основная+X2+X6+X10(1392группы). Акк2:основная+X3+X6+X9+X12(1461группа). Логика основной карточки с бандлами похожа, сами кратности различаются. Большинство дочерних являются отдельнымиmodel_id, поэтому их автоархив особенно уменьшает число одиночных моделей акк2.</p>'
body+='<p>По первым4цифрам внешнего кода среди товаров в наличии4 546общихсемейств;акк1всего4 651,акк2 4 575. Пересечение/объединение97,14%. <b>Это сходство ассортимента артикульных семейств, не97%доказанного совпадения Ozon-склеек.</b></p>'
body+='<p>Совпадающих числовыхmodel_idмежду аккаунтами в наличии0. Поэтому переносить IDили экспериментальные назначения акк1 наакк2 нельзя. Семья по внешнемукоду и model_id — разные карты; каждая фиксируется отдельно. Сопоставление количества моделей внутри общего семейства также не доказывает равенство состава.</p>'
body+='<p>Источник:одинаковый полный срез обоих аккаунтов08.11около21:06МСК,дляакк2 дополнительно учтены107фактически восстановленных карточек по независимомуAPIreadback. Срез не атомарный, времена сохранены. Никаких повторных выгрузок для этого сравнения.</p>'
body+=f'<p><a href="{slug}-groups">Модели и состав групп CSV</a> · <a href="{slug}-members">Члены всех моделей CSV</a> · <a href="{slug}-families">Пересечение артикульных семейств CSV</a> · <a href="/api/wb-bids/reports/ozon-acc2-monthly-experiment-2026-11-08">Проект месячного эксперимента акк2</a>.</p>'
(O/'index.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Сравнение склеек Ozon</title><style>body{font:16px/1.55 system-ui;max-width:1100px;margin:25px auto;padding:0 16px}td,th{padding:10px;border:1px solid #ddd;text-align:left}table{border-collapse:collapse;width:100%}th{background:#edf2fa}a{color:#155ab6}</style>'+body+'</html>')
(O/'RESULT.md').write_text('# Карта склеек акк1/акк2\n\n'+json.dumps(summary,ensure_ascii=False,indent=2)+'\n\nhttps://bi.beyindi.com'+slug+'\n')
print(json.dumps(summary,ensure_ascii=False))
