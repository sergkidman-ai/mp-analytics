# поток: mkt / ozon
"""Offline two-wave acc2 proposal, individual floors and reused 28-day baseline."""
import calendar
import collections
import csv
import datetime as dt
from decimal import Decimal as D
import hashlib
import html
import json
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, '/opt/mp-analytics')
from collectors.launch_cards import classify

O = Path(__file__).parent
P = O / 'preflight'
OLD = O.parent / 'ozon_acc2_bid_candidates_20261108'
TZ = ZoneInfo('Europe/Moscow')
read = lambda p: json.loads(p.read_text())
save = lambda n, z: (O / n).write_text(json.dumps(z, ensure_ascii=False, indent=2) + '\n')
csvread = lambda p: list(csv.DictReader(p.open(encoding='utf-8-sig')))
now = dt.datetime.now(dt.timezone.utc)
cutoff = dt.datetime(2026, 5, 8, tzinfo=TZ)
items, aliases = {}, {}
for p in sorted(P.glob('cards_batch_*.json')):
    z = read(p); assert z['account'] == 'oz_acc2'
    assert set(z['requested_ids']) == {str(r['id']) for r in z['response']['items']}
    for r in z['response']['items']:
        pid = str(r['id']); assert pid not in items; items[pid] = r
        main = str(r.get('sku') or next((s['sku'] for s in r.get('sources') or [] if s.get('sku')), ''))
        for sku in [r.get('sku')] + [s.get('sku') for s in r.get('sources') or []]:
            if sku: aliases[str(sku)] = (main, pid)
all_rows = csvread(P / 'fresh_eligibility.csv')
ready = []
for r in all_rows:
    if r['eligible_for_upload'] != 'True': continue
    x = items[r['product_id']]
    role = classify(x['offer_id'], x['name'], 'oz_acc2')
    created = dt.datetime.fromisoformat(x['created_at'].replace('Z', '+00:00'))
    segment = 'bundles_x' + str(role[2]) if role and role[1] == 'bundle' else 'children' if re.match(r'^[0-9]{5}', x['offer_id']) else 'new_6months' if cutoff <= created <= now else 'old'
    ready.append({**r, 'segment': segment, 'external_code_family': x['offer_id'][:4],
                  'model_id': str((x.get('model_info') or {}).get('model_id') or ''),
                  'created_at_utc': x['created_at']})
assert len(ready) == 7788

# Keep old-card model/family blocks together; no control group or historical T/C labels.
old = [r for r in ready if r['segment'] == 'old']
root = {r['sku']: r['sku'] for r in old}
def find(s):
    while root[s] != s:
        root[s] = root[root[s]]; s = root[s]
    return s
def union(a,b): root[find(b)] = find(a)
seen_model, seen_family = {}, {}
for r in old:
    for k, ix in [(r['model_id'], seen_model), (r['external_code_family'], seen_family)]:
        if k:
            if k in ix: union(r['sku'], ix[k])
            else: ix[k] = r['sku']
blocks = collections.defaultdict(list)
for r in old: blocks[find(r['sku'])].append(r)
target = (len(old) + 1) // 2
selected_old = set()
seed = 'oz_acc2_20261108_two_waves_old_model_family_half'
for block in sorted(blocks.values(), key=lambda rr: hashlib.sha256((seed+':'+min(r['sku'] for r in rr)).encode()).hexdigest()):
    if abs(target - (len(selected_old)+len(block))) <= abs(target-len(selected_old)):
        selected_old.update(r['sku'] for r in block)

labels = {'children':'Дочерние', 'new_6months':'Новинки за полгода', 'old':'Старые',
          'bundles_x3':'Бандлы X3', 'bundles_x6':'Бандлы X6', 'bundles_x9':'Бандлы X9', 'bundles_x12':'Бандлы X12',
          'restored_children':'Дочерние после восстановления'}
for r in ready:
    r['wave'] = 1 if r['segment'] in ['children','new_6months','bundles_x3'] or r['sku'] in selected_old else 2
    r['campaign_key'] = f"wave{r['wave']}_{r['segment']}"
    r['campaign_title'] = f"oz_acc2 | Волна {r['wave']} | {labels[r['segment']]} | MIN CPC | 20261108"
    r['assignment_status'] = 'DRAFT_NOT_APPLIED'
    r['actual_campaign_id'] = ''; r['actual_activation_at'] = ''
    r['automatic_application_allowed'] = False
    r['native_budget_verified_for_acc2'] = False

def output(name, rr, fields=None):
    with (O / name).open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields or list(rr[0])); w.writeheader(); w.writerows(rr)
output('all_waves_DRAFT.csv', ready)
for wave in [1,2]: output(f'wave_{wave}_DRAFT.csv', [r for r in ready if r['wave']==wave])
campaigns = []
for key in sorted({r['campaign_key'] for r in ready}):
    rr = [r for r in ready if r['campaign_key']==key]
    output(key+'.csv', rr)
    campaigns.append({'key':key,'wave':rr[0]['wave'],'segment':rr[0]['segment'], 'title':rr[0]['campaign_title'],
                      'SKU_count':len(rr),'status':'DRAFT_NOT_CREATED','actual_campaign_id':None,
                      'individual_min_bid_range_rub':[str(min(D(r['bid_rub']) for r in rr)),str(max(D(r['bid_rub']) for r in rr))],
                      'native_weekly_budget_preview_UNVERIFIED_rub':len(rr)*2000,
                      'preview_basis':'acc1 observed Ozon 2000RUB/SKU/week; acc2 not confirmed',
                      'approved_weekly_spend_rub':None,'new_campaign_floor_refresh_required_before_upload':True})
campaigns.append({'key':'wave2_restored_children','wave':2,'segment':'restored_children',
                  'title':'oz_acc2 | Волна 2 | Дочерние после восстановления | MIN CPC | 20261108',
                  'SKU_count':None,'status':'DYNAMIC_REFRESH_AT_D0_PLUS_7_NOT_CREATED',
                  'actual_campaign_id':None,'approved_weekly_spend_rub':None,
                  'condition':'restored after W1 cutoff; individual stock/channel/price/errors/minimum pass; absent W1 and any running CPC'})
assert len(campaigns)==9
assert len({r['sku'] for r in ready})==len(ready)
assert not ({r['sku'] for r in ready if r['wave']==1}&{r['sku'] for r in ready if r['wave']==2})
model_waves = collections.defaultdict(set)
for r in ready:
    if r['model_id']: model_waves[r['model_id']].add(r['wave'])
output('model_wave_context.csv', [{'model_id':m,'waves':';'.join(map(str,sorted(ws))),
                                  'split_across_waves':len(ws)>1,'causal_control_claimed':False} for m,ws in sorted(model_waves.items())])

# Reuse the existing account-specific raw postings and ad reports, no new year/28d download.
baseline = O / 'baseline_provisional'; baseline.mkdir(exist_ok=True)
own_orders=collections.defaultdict(set); own_value=collections.defaultdict(lambda:D(0))
model_orders=collections.defaultdict(set); model_value=collections.defaultdict(lambda:D(0))
family_orders=collections.defaultdict(set); family_value=collections.defaultdict(lambda:D(0))
account_orders=set(); account_value=D(0); unknown_sold=0; cancelled=0; historical_sold_mapping=0
old_map=read(OLD/'current_card_map.json'); assert old_map['account']=='oz_acc2'
for scheme in ['fbs','fbo']:
    z=read(OLD/'raw'/f'postings_{scheme}.json'); assert z['account']=='oz_acc2' and z['complete']
    for post in z['postings']:
        if post['status'].lower().startswith('cancel'):
            cancelled+=1;continue
        for prod in post['products']:
            raw_sku=str(prod['sku']);alias=aliases.get(raw_sku)
            if alias:
                main,pid=alias; x=items[pid];m=str((x.get('model_info') or {}).get('model_id') or '');fam=x['offer_id'][:4]
            else:
                main=old_map['canonical_by_alias'].get(raw_sku)
                historic=old_map['cards'].get(main,{})
                if not historic: unknown_sold+=1; continue
                m=historic['model_id'];fam=historic['article'][:4];historical_sold_mapping+=1
            from core.ozon_posting import product_price
            value=product_price(prod)*D(str(prod['quantity']));oid=str(post['order_id'])
            own_orders[main].add(oid);own_value[main]+=value;model_orders[m].add(oid);model_value[m]+=value
            family_orders[fam].add(oid);family_value[fam]+=value;account_orders.add(oid);account_value+=value
assert unknown_sold==0
baseline_rows=[]
for r in ready:
    sku=r['sku'];m=r['model_id'];fam=r['external_code_family']
    baseline_rows.append({'account':'oz_acc2','sku':sku,'offer_id':r['offer_id'],'segment':r['segment'],'draft_wave':r['wave'],
                          'own_unique_orders':len(own_orders[sku]),'own_ordered_value_rub':str(own_value[sku]),
                          'model_id':m,'model_unique_orders':len(model_orders[m]),'model_ordered_value_rub':str(model_value[m]),
                          'article_family':fam,'family_unique_orders':len(family_orders[fam]),'family_ordered_value_rub':str(family_value[fam]),
                          'from_msk':'2026-10-11','through_msk':'2026-11-07','baseline_status':'PROVISIONAL_REFRESH_LAST_DAY_AT_ACTUAL_D0'})
output('baseline_provisional/sku_model_family_baseline.csv',baseline_rows)
save('baseline_provisional/coverage.json',{'account':'oz_acc2','from_msk':'2026-10-11','through_msk':'2026-11-07',
                                         'raw_source':str(OLD/'raw'),'fresh_download_performed':False,'cancelled_postings_excluded':cancelled,
                                         'unique_account_orders':len(account_orders),'account_ordered_value_rub':str(account_value),
                                         'unmapped_sold_lines':unknown_sold,'sold_lines_using_saved_account2_historical_card_map':historical_sold_mapping,
                                         'valid_final_prelaunch_baseline':False,
                                         'required_before_launch':'anchor to actual D0 and update missing last full days plus delayed order revisions, reuse overlapping raw'})
counts={str(w):dict(collections.Counter(r['segment'] for r in ready if r['wave']==w)) for w in [1,2]}
summary={'account':'oz_acc2','draft_prepared_at_utc':now.isoformat(),'preflight_at_utc':read(P/'preflight_summary.json')['finished_at_utc'],
         'known_current_card_scope':19766,'own_eligible_outside_CPC':7788,'wave_counts':counts,
         'wave1_total':sum(counts['1'].values()),'wave2_fixed_total':sum(counts['2'].values()),
         'wave2_recovered_children_additional':'unknown until fresh D0+7 cutoff; at most about700 from current100/day schedule in7days',
         'actual_activation_at':None,'actual_stop_at':None,'month_rule':'one common calendar month from actual W1 activation; W2 at D0+7, shorter exposure',
         'old_split_rule':seed,'old_block_counts':len(blocks),'old_wave1_target':target,'old_wave1_actual':len(selected_old),
         'floor_histogram':dict(collections.Counter(r['bid_rub'] for r in ready)),
         'native_model_ids':len(model_waves),'models_split_across_waves':sum(len(ws)>1 for ws in model_waves.values()),
         'family_exclusions':0,'all_SKU_wave_intersections_zero':True,'historical_TC_assignments_reused':False,
         'control_group_assigned':False,'global_CPO_running':True,'incremental_CPC_over_existing_CPO_test':True,
         'budget_authorization_pending':True,'account2_guard_installed':False,'account2_month_stop_installed':False,
         'new_campaigns_created':0,'new_campaigns_activated':0,'ad_mutations':0,
         'late_wave_policy':'children restored after W2 cutoff not automatically added; separate future intake decision',
         'campaigns':campaigns}
save('experiment_DRAFT.json',summary)
save('risk_policy_DRAFT_NOT_INSTALLED.json',{'account':'oz_acc2','status':'PROPOSAL_NOT_INSTALLED','account1_guard_must_not_be_reused_as_if_covers_acc2':True,
    'check_every_hours':3,'proposed_zero_order_stop_rub_per_SKU':1000,'require_complete_own_model_article_general_order_data':True,
    'missing_data_action':'signal only, unknown is not zero','proposed_action':'pause affected new campaign preserving members if individual pause unavailable',
    'budget_risk':'signal and evaluate on exceeding approved reference; reference not yet provided',
    'approved_weekly_reference_rub':None,'scope_new_campaign_ids':[],'automatic_activation_or_pause_enabled':False,
    'month_stop_must_be_installed_and_checked_before_activation':True})
slug='/api/wb-bids/reports/ozon-acc2-monthly-experiment-2026-11-08'
body='<h1>Акк2 · месячный эксперимент · проект двух волн</h1><p><b>Подготовлено, не запущено.</b> Новых кампаний0, активаций0. Бюджет акк2 ещё не согласован. Состав и индивидуальные минимумы проверены API; чужие рекламные настройки не менялись.</p>'
body+='<table><tr><th>Группа</th><th>Волна1</th><th>Волна2</th></tr>'
for g in ['children','new_6months','old','bundles_x3','bundles_x6','bundles_x9','bundles_x12']:
    body+=f'<tr><td>{labels[g]}</td><td>{counts["1"].get(g,0)}</td><td>{counts["2"].get(g,0)}</td></tr>'
body+=f'<tr><th>Итого фиксированный состав</th><th>{summary["wave1_total"]}</th><th>{summary["wave2_fixed_total"]}</th></tr></table>'
body+='<p>Во вторую дополнительно войдут дочерние, восстановленные после отсечения первой волны и доступные кD0+7. Сейчас точного числа нет. При100восстановлениях/сутки за неделю появится до700кандидатов, каждый заново проверяется. Остальные3419не восстановятся за неделю; добавлений после отсечения второй волны автоматически не планируется.</p>'
body+='<p>Предложен недельный интервал. Один общий календарный месяц от фактического старта первой волны; вторая получит примерно23дня внутри него. Месяц каждой волне отдельно не назначен. D0 и дата остановки пока не определены. Старые разделены примерно пополам целыми блоками их моделей/артикульных семейств, это проект назначения волн, не контрольная группа.</p>'
body+='<p>Первая волна:4отдельныеРК — дочерние, новинки, старые, X3. Вторая:отдельныеРКдляX6/X9/X12, старых и новых восстановленных дочерних; всего9планируемыхРК, изних8сфиксированнымсоставом. Списки SKU волн не пересекаются; родители не добавляются и не требуются.</p>'
body+='<h2>Ставки и бюджет</h2><p>Все7 788минимумов прочитаны индивидуально:7₽—228SKU,8₽—436SKU,10₽—7124SKU. Перед загрузкой их нужно перепроверить. Минимальная ставка не является ограничением общего расхода.</p>'
body+='<p>Вопрос владельцу о12 500₽/неделю для первой волны ещё без ответа. Бюджет аккаунта1 не переносится наакк2 автоматически. У акк1 APIтребовал2000₽/SKU/неделю технических настроек: это лишь неподтверждённая дляакк2 оценка, не фактический расход и не согласованный лимит. Суммы акк2 необходимо подтвердить егоAPI. Платный запуск до согласования бюджета не выполняется.</p>'
body+='<h2>Что измеряем</h2><p>Приоритет — весь аккаунт: все неотменённые заказы и стоимость заказанного, рекламные расходы деньгами отдельно от бонусов, CPC и CPO отдельно. Затем каждыйSKU, каждая фактическаяOzon-склейка model_id со всеми соседями и дополнительно артикульная семья дочерних. Заказы внутри каждого уровня дедуплицируются; уровни не складываются.</p>'
body+='<p>На аккаунте уже работает CPO«Все товары»: это тест добавления CPC поверх существующего фона, не сравнение рекламы с её полным отсутствием. Новые/старые/дочерние/бандлы не пересекаются. Восстановление из архива фиксируется отдельным событием, его эффект нельзя автоматически приписывать CPC.</p>'
body+='<p>Промежуточные итоги7/14дней и общий месяц, сравнение одинаковых дней недели/часов. Длявторойволны общийpre-D0 и свой срез до включения, с учётом раннего влияния первой волны на общие модели. '+str(summary['models_split_across_waves'])+'моделей имеют кандидатов в обеих волнах: требование X3сначала/прочие позже не позволяет обещать непересекающиеся склейки или строгий причинный эксперимент.</p>'
body+='<h2>База и защита до запуска</h2><p>Подготовлена предварительная28-дневная база11.10–07.11изуже сохранённых акк2postings; новых широких выгрузок нет. Для фактическогоD0 нужно обновить последние полные дни и пересчитать уточнённые заказы. Стоимость заказанного — не финансовая реализация и не прибыль.</p>'
body+='<p>Предложение защиты:проверкакаждые3часа, порог1000₽наSKUбез подтверждённых собственных/модельных/семейных заказов, остальные риски — сигнал. У акк2 пока не установлен ни такой guard, ни месячный стоп. Старый guardакк1 защищает только его подписанныеРК. Перед активацией должны быть собственные подписанныеIDакк2, установленные проверки и календарная остановка отфактическогоD0.</p>'
body+=f'<p><a href="{slug}-wave1">ПерваяволнаCSV</a> · <a href="{slug}-wave2">ФиксированнаячастьвторойCSV</a> · <a href="{slug}-all">Все7 788SKUсминимумамиCSV</a> · <a href="{slug}-baseline">ПредварительнаябазаSKU/склеек/семейCSV</a>.</p>'
(O/'index.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Акк2: месячный эксперимент</title><style>body{font:16px/1.55 system-ui;max-width:1200px;margin:25px auto;padding:0 16px}td,th{border:1px solid #ddd;padding:9px;text-align:left}table{border-collapse:collapse;width:100%}th{background:#edf2fa}a{color:#155ab6}</style>'+body+'</html>')
(O/'RESULT.md').write_text('# Акк2: месячный эксперимент, DRAFT\n\n'+json.dumps({k:v for k,v in summary.items() if k!='campaigns'},ensure_ascii=False,indent=2)+'\n\nhttps://bi.beyindi.com'+slug+'\n')
print(json.dumps({k:v for k,v in summary.items() if k!='campaigns'},ensure_ascii=False))
