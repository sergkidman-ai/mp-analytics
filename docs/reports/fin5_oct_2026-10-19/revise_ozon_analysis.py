"""Revise the saved October analysis; never modify production Ozon reports."""
from pathlib import Path
from decimal import Decimal as D
import csv
import io
import re
import json
import hashlib
import shutil

ROOT=Path('/opt/mp-analytics')
P=ROOT/'docs/reports/fin5_oct_2026-10-19'
SOURCE=ROOT/'docs/reports/fin5_bonus_preview_2026-10-20'
PROTECTED=['web/static/reports.html','reports/ozon_mp_page.py','reports/ozon_mp_report.py','reports/data/mp_ozon_hist.json']
before={f:hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in PROTECTED}
backup=ROOT/'backups/fin5_analysis_revision_20261020'
backup.mkdir(parents=True,exist_ok=True)
shutil.copy2(P/'report.html',backup/'report.html')
def n(s):return D(re.sub(r'[\s\u00a0]','',s or '0').replace(',','.'))
def fmt(v):return f'{D(str(v)):,.2f}'.replace(',',' ').replace('.',',')

ad=[]
for acc,cpo in [('oz_acc1','24244024'),('oz_acc2','23608084')]:
    for month,filename in [('2026-09',f'{acc}_2026-09_expense.csv'),('2026-10',f'{acc}_2026-10-01_18_expense.csv')]:
        rr=list(csv.DictReader(io.StringIO((SOURCE/filename).read_text(encoding='utf-8-sig')),delimiter=';'))
        rr=[r for r in rr if r['Дата']<=month+'-18']
        cpc_cash=sum(n(r['Расход']) for r in rr if r['ID']!=cpo)
        cpo_cash=sum(n(r['Расход']) for r in rr if r['ID']==cpo)
        bonus=sum(n(r['Расход бонусов']) for r in rr)
        ad.append(dict(account=acc,period=month,days='01-18',cash_cpc=cpc_cash,cash_cpo=cpo_cash,cash_total=cpc_cash+cpo_cash,bonus_spent=bonus,source=filename))
matched=json.loads((P/'matched_18_days.json').read_text())
for r in ad:
    sales=next(x['sales'] for x in matched if x['account']==r['account'] and x['month']==r['period'])
    r['cash_ad_sales_pct']=float(r['cash_total'])/sales*100
monthly=json.loads((P/'monthly.json').read_text())
components=[]
for acc in ['oz_acc1','oz_acc2']:
    octrow=next(r for r in monthly if r['account']==acc and r['month']=='2026-10')
    seprow=next(r for r in monthly if r['account']==acc and r['month']=='2026-09')
    current=octrow['detail']['promo'];previous=seprow['detail']['promo']
    for code in sorted(set(current)|set(previous)):
        ov=current.get(code,0);sv=previous.get(code,0)
        components.append(dict(account=acc,code=code,cash_oct=ov,cash_sep=sv,oct_pct=ov/octrow['values']['sales']*100,sep_pct=sv/seprow['values']['sales']*100,margin_impact_pp=100*(sv/seprow['values']['sales']-ov/octrow['values']['sales'])))
    impact=sum(x['margin_impact_pp'] for x in components if x['account']==acc)
    parent=100*(seprow['values']['promo']/seprow['values']['sales']-octrow['values']['promo']/octrow['values']['sales'])
    assert abs(impact-parent)<1e-8

names={'oz_acc1':'Цифровой','oz_acc2':'Дисквер'}
overview='''<p class="callout"><b>Уточнённый вывод по Ozon.</b> Отчёты МП уже содержат денежные расходы на рекламу. Резкий рост Performance Дисквера71→174 тыс. ₽ включал расходование бонусов и не доказывает рост денежных затрат. При одинаковых1–18 числах денежные CPC+CPO Дисквера98,4→98,6 тыс. ₽, Цифрового770,2→926,6 тыс. ₽. У Цифрового давление комиссии и денежного продвижения сохраняется; у Дисквера в фокусе возвраты, платные отзывы и отдельные фиксированные начисления. Аудиторский пересчёт Маркета по-прежнему даёт14,76% вместо6,63%.</p>'''
cash_table=['<section><h2>Ozon — точное разделение денег и бонусов</h2><p>Официальный экспорт Performance за одинаковые1–18 сентября и октября. «Расход» в этом CSV — денежная часть; «Расход бонусов» отдельный показатель. Изменения прибыли из-за повторного исключения бонусов нет: в Отчётах МП они уже исключены.</p><div class="scroll"><table><tr><th>Кабинет и способ оплаты</th><th>1–18 сентября, ₽</th><th>1–18 октября, ₽</th><th>Изменение</th></tr>']
for acc in ['oz_acc1','oz_acc2']:
    a=next(x for x in ad if x['account']==acc and x['period']=='2026-09');b=next(x for x in ad if x['account']==acc and x['period']=='2026-10')
    for key,label in [('cash_cpc','CPC — наши деньги'),('cash_cpo','Оплата за заказ — наши деньги'),('cash_total','Денежная реклама всего'),('bonus_spent','Использовано бонусов Ozon — справочно')]:
        delta=(b[key]/a[key]-1)*100 if a[key] else None
        cash_table.append(f'<tr><td>{names[acc]} · {label}</td><td>{fmt(a[key])}</td><td>{fmt(b[key])}</td><td>{delta:+.2f}%</td></tr>')
cash_table.append('''</table></div><p>Эта таблица — расход за даты оказания рекламы. В финансовых транзакциях исходного среза октябрьская денежная реклама отражена пока по17.10: CPC418 618,49/29 673,29 ₽ и CPO474 255,25/64 623,90 ₽. Отличие от полного1–18 связано с18 октября: за1–17 денежные колонки экспорта и транзакции сошлись до копейки. Бонусы могут расходоваться и в обычных кампаниях; название «Бонусы…» не заменяет источник способа оплаты. Числа141 842,26 ₽ по бонусным кампаниям и141 698,54 ₽ фактической оплаты бонусами — разные показатели.</p><p>Начисления бонусов не являются выручкой, а оплата ими рекламы не является расходом наших денег. Баллы за скидки покупателям в продажах и платные отзывы за баллы сохраняют своё прежнее значение.</p></section>''')
digital='''<section><h2>Ozon Цифровой — рост денежных расходов подтверждён</h2><p>Маржа исходного среза17,05% против17,83% в сентябре; денежные удержания60,98% против59,67%. Переклассификация бонусов эти показатели не меняет. В августе маржа была21,38%: ухудшение началось ещё в сентябре.</p><p>В Отчётах МП CPC+CPO занимают9,70% оборота против8,60% в сентябре: +1,11 п.п. денежной рекламной нагрузки. Внутри этого CPC+0,53 п.п., оплата за заказ+0,58 п.п. Меньшие расходы на отзывы и отдельные компоненты подписки частично компенсируют рост; строка денежного продвижения в целом9,90→10,41% (+0,51 п.п.).</p><p>На строго одинаковых1–18 числах официальный экспорт показывает рост денежной рекламы770 223,26→926 582,03 ₽ (+20,30%) при росте транзакционного оборота8,58%. Доля денежной рекламы9,14→10,13%. Это реальное увеличение нагрузки, которое нужно разбирать по отдаче CPC и оплаты за заказ.</p><p>Остальные вклады в снижение маржи: комиссия−0,67 п.п., доля COGS−0,44 п.п., услуги партнёров−0,28 п.п., доставка−0,13 п.п. Меньшие штрафы и возвраты часть ухудшения компенсируют. Уменьшение скидки за отсрочку с28.09 — возможная часть изменения комиссии; весь эффект тарифа отдельно не изолирован.</p><p>Повышение цен06.10 ещё смешано со старыми заказами:54,8% признанного октябрьского оборота относятся к заказам, созданным раньше06.10.</p></section>'''
diskver='''<section><h2>Ozon Дисквер — денежная реклама не объясняет падение маржи</h2><p>Маржа14,96% против16,66%; денежные удержания56,52% против60,83%. Общая строка денежного продвижения снизилась с8,89% до7,83% оборота, то есть помогла марже на1,06 п.п. Денежные CPC+CPO отдельно снизились с6,62% до4,98% оборота в сравнении исходного среза с полным сентябрём.</p><p>Сопоставимое окно1–18 подтверждает отсутствие резкого роста: CPC+CPO98 380,32→98 643,78 ₽ (+0,27%) при росте транзакционного оборота52,54%. CPC деньгами21 163,32→30 887,93 ₽, оплата за заказ77 217→67 755,85 ₽. В первоначальной денежной проводке было94 297,19 ₽, поскольку18 октября ещё не отразилось. Бонусы49 893,50→141 698,54 ₽ показаны отдельно и не уменьшают прибыль. Прежний вывод о174 тыс. ₽ как денежном ухудшении рекламы снят.</p><p>Внутри продвижения ухудшились именно платные первые отзывы:36 966,14 ₽ против19 194 ₽ за полный сентябрь, доля0,68→1,95% (+1,27 п.п.). При этом оплата за заказ и другие компоненты в доле оборота снизились сильнее. Следует проверить состав товаров и реальную пользу «Сбора первых отзывов», подключённого Ozon в июле.</p><p>Строка возвратов168 984 ₽,8,92% оборота против0,93% в сентябре; её отдельный арифметический вклад−8,00 п.п. частично компенсируют сторно комиссии и изменение COGS. Это не самостоятельная оценка потери прибыли. Два крупных возврата93 342 и18 333 ₽ относятся к заказам сентября.</p><p>Заказ44671170-0530 подтверждён тремя ClientReturn с причиной «Товар в неполной комплектации». Продажа93 342 ₽ признана04.10 и сторнирована09.10, комиссия43 870,74 ₽ возвращена. Сумма всех связанных операций Ozon−5 125,39 ₽ до COGS:517 ₽ исходящей логистики/приёмки,408 ₽ обратной логистики,4 200,39 ₽ жалобы. <a href="return_44671170.html">Подтверждение и последовательность операций</a>.</p><p>Также у обоих кабинетов есть списание28 000 ₽ с нерасшифрованным кодом AccrualType_127; отдельная строка подписки6 990 ₽ не показывает всей возможной стоимости Premium. Совпадение с новой структурой оплаты аналитики — гипотеза до сверки акта.28 000 ₽ уже включены в денежные удержания в «Неклассифицировано», повторного добавления нет. У Дисквера эта сумма особенно заметна на меньшем обороте.</p></section>'''
path=P/'report.html';s=path.read_text();s=re.sub(r'<p class="callout">.*?</p>',lambda _:overview,s,count=1,flags=re.S)
for heading,replacement in [('Ozon Цифровой — комиссия и продвижение',digital),('Ozon Дисквер — возвраты и платные отзывы',diskver)]:
    pattern=r'<section><h2>'+re.escape(heading)+r'</h2>.*?</section>'
    s,count=re.subn(pattern,lambda _:replacement,s,count=1,flags=re.S);assert count==1,heading
s=s.replace(digital,''.join(cash_table)+digital,1)
s=s.replace('Сверить бонусы и реальные денежные списания рекламы Дисквера; отдельно оценить оплату за заказ у Цифрового.','Разделение бонусов и денег подтверждено официальным экспортом. Оценить денежную отдачу CPC и оплаты за заказ у Цифрового; у Дисквера проверить платные первые отзывы и фиксированные списания.')
s=s.replace('<h1>Промежуточные итоги октября 2026</h1>','<h1>Промежуточные итоги октября 2026</h1><p class="note">Анализ уточнён20.10: деньги и рекламные бонусы разделены. Срез исходных финансовых показателей сохранён на19.10. Отчёты Ozon в Пульте не изменялись.</p>')
path.write_text(s)
(P/'advertising_funding_revision.json').write_text(json.dumps({'revision_date':'2026-10-20','financial_cutoff':'original 2026-10-19 snapshot preserved','aligned_ad_funding':ad,'promotion_cash_components':components,'cash_financial_totals_changed':False,'production_ozon_reports_changed':False},ensure_ascii=False,default=str,indent=2))
with (P/'advertising_funding_revision.csv').open('w',newline='') as f:
    writer=csv.DictWriter(f,fieldnames=list(ad[0]));writer.writeheader();writer.writerows(ad)
after={f:hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in PROTECTED};assert before==after
(P/'advertising_revision_checks.json').write_text(json.dumps({'production_files_unchanged':before==after,'protected_sha256':after,'promotion_effects_reconciled':True},indent=2))
print('Analysis revised; Ozon pages, calculation modules and frozen data unchanged.')
for acc in ['oz_acc1','oz_acc2']:
    a=next(x for x in ad if x['account']==acc and x['period']=='2026-09');b=next(x for x in ad if x['account']==acc and x['period']=='2026-10')
    print(acc,'cash ads',a['cash_total'],'→',b['cash_total'],'change%',round((b['cash_total']/a['cash_total']-1)*100,3),'share%',round(a['cash_ad_sales_pct'],3),'→',round(b['cash_ad_sales_pct'],3))
