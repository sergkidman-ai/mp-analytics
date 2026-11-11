# поток: mkt / ozon
"""Offline publication of current wave1 performance and whole-account comparisons."""
import csv
import datetime as dt
from decimal import Decimal as D
from html import escape
import json
from pathlib import Path
from zoneinfo import ZoneInfo

P=Path(__file__).parent
A=json.loads((P/'analysis.json').read_text())
TZ=ZoneInfo('Europe/Moscow')
LABEL={'oz_acc1':'Акк1 — Цифровой квадрат','oz_acc2':'Акк2 — Дисквэр'}
money=lambda n:f'{D(str(n)):,.2f}'.replace(',',' ').replace('.',',')
whole=lambda n:f'{int(D(str(n))):,}'.replace(',',' ')
pct=lambda n:('—' if n is None else f'{D(str(n)):+.1f}%'.replace('.',','))
stamp=lambda n:dt.datetime.fromisoformat(n).astimezone(TZ).strftime('%d.%m %H:%M')


def table(head,rows):
    return '<div class="scroll"><table><tr>'+''.join('<th>'+escape(str(h))+'</th>' for h in head)+'</tr>'+''.join('<tr>'+''.join('<td>'+escape(str(v))+'</td>' for v in row)+'</tr>' for row in rows)+'</table></div>'


cutoff=next(iter(A['accounts'].values()))['through_utc']
assert len({a['through_utc'] for a in A['accounts'].values()})==1
body=f'<h1>Ozon: первая волна и общий профиль</h1><p>Срез {stamp(cutoff)} МСК, 2026 год. Все 8 РК работают. Сегодня неполный день.</p>'
body+='<p><b>На акк1 общий профиль вырос в наблюдаемом периоде. На акк2 накопительный итог положительный, но устойчивого роста пока не видно: последний полный день был ниже обычного.</b> Выделить причинный эффект первой волны по этим данным нельзя: контрольной группы нет, действуют другие рекламные и сезонные изменения.</p>'
body+='<h2>Весь аккаунт: сопоставимые периоды</h2><p>Для каждого аккаунта взят период с его фактического запуска до общего среза. Эталон — среднее четырёх окон, сдвинутых на 7/14/21/28 дней: одинаковые дни недели и часы. Периоды аккаунтов разной длины.</p>'
display=[]
for account,a in A['accounts'].items():
    r=a['profile_scopes']['account'];display.append([LABEL[account],f"{stamp(a['from_utc'])} — {stamp(a['through_utc'])}",money(r['baseline4_mean']['orders']),whole(r['actual']['orders']),pct(r['change_percent']['orders']),money(r['baseline4_mean']['ordered_value_RUB']),money(r['actual']['ordered_value_RUB']),pct(r['change_percent']['ordered_value_RUB'])])
body+=table(['Аккаунт','Окно МСК','Заказов обычно','Заказов сейчас','Δ заказов','Стоимость обычно, ₽','Стоимость сейчас, ₽','Δ стоимости'],display)
body+='<h2>Последний полный день — 10 ноября</h2>'
display=[]
for account,a in A['accounts'].items():
    r=next(d for d in a['account_days'] if d['date_msk']=='2026-11-10');assert not r['partial']
    display.append([LABEL[account],money(r['baseline4_mean']['orders']),whole(r['actual']['orders']),pct(r['change_percent']['orders']),money(r['baseline4_mean']['ordered_value_RUB']),money(r['actual']['ordered_value_RUB']),pct(r['change_percent']['ordered_value_RUB'])])
body+=table(['Аккаунт','Заказов обычно','Заказов 10.11','Δ заказов','Стоимость обычно, ₽','Стоимость 10.11, ₽','Δ стоимости'],display)
body+='<p>Акк1: в текущую сумму входит крупный заказ 634 470 ₽. Если убрать крупнейший заказ из текущего и каждого эталонного окна, прирост стоимости остаётся +28,3%. Акк2: всего 31 общий заказ после запуска; 17 из них были 10 ноября. Объём пока мал для устойчивого вывода.</p>'
md=[]
for account,a in A['accounts'].items():
    rows=sorted(a['campaigns'],key=lambda c:next(i for i,k in enumerate(['Новинки','Бандлы','Старые','Дочерние']) if c['group'].startswith(k)))
    totals=a['ad_totals'];title=LABEL[account]+f" — с {stamp(a['from_utc'])} МСК"
    body+='<h2>'+escape(title)+'</h2>'
    head=['Группа','SKU','Расход, ₽','Показы','Клики','Заказы Ozon']
    display=[[r['group'].replace(' за полгода',''),whole(r['cohort_SKU']),money(r['spend_RUB']),whole(r['views']),whole(r['clicks']),whole(r['ad_orders'])] for r in rows]
    display.append(['Итого',whole(totals['cohort_SKU']),money(totals['spend_RUB']),whole(totals['views']),whole(totals['clicks']),whole(totals['ad_orders'])])
    body+=table(head,display)
    attributed=D(totals['ad_order_value_RUB']);drr=D(totals['spend_RUB'])/attributed*100 if attributed else None
    body+=f'<p>Стоимость приписанных Ozon заказов: {money(attributed)} ₽; рекламный ДРР: {money(drr) if drr is not None else "—"}%. Расход первой волны: {money(D(totals["spend_RUB"])/12500*100)}% недельного ориентира 12 500 ₽. Это мягкий ориентир, не жёсткий лимит.</p>'
    md += [title,'| '+' | '.join(head)+' |','|'+'|'.join(['---']*len(head))+'|']+['| '+' | '.join(r)+' |' for r in display]+['']
    body+='<h3>Уровни продаж</h3>'
    labels={'wave_SKU':'Сами SKU волны','native_models':'Фактические склейки Ozon','child_article_families':'Артикульные семьи дочерних','account':'Весь аккаунт'}
    body+=table(['Уровень','Заказы обычно → сейчас','Стоимость обычно, ₽','Стоимость сейчас, ₽','Δ стоимости'],[[labels[s],f"{money(r['baseline4_mean']['orders'])} → {whole(r['actual']['orders'])}",money(r['baseline4_mean']['ordered_value_RUB']),money(r['actual']['ordered_value_RUB']),pct(r['change_percent']['ordered_value_RUB'])] for s,r in a['profile_scopes'].items()])
    body+='<p>Уровни пересекаются: их нельзя складывать. Склейки берутся из сохранённой фактической карты model_id, артикульные семьи считаются отдельно; наличие родственника не используется для исключения SKU.</p>'
    ref=a['profile_scopes']['account']['prior_windows']
    body+='<details><summary>Четыре эталонных окна</summary>'+table(['С МСК','По МСК','Заказы','Стоимость, ₽'],[[stamp(r['from_utc']),stamp(r['through_utc']),r['orders'],money(r['ordered_value_RUB'])] for r in ref])+'</details>'
body+='<h2>Как читать результат</h2><p>Заказы рекламы — атрибуция Performance API, возможен досчёт. Общие заказы — уникальные order_id по FBS+FBO, отменённые отправления исключены. Стоимость — сумма цен × количество, не финансовая реализация и не прибыль. Органические заказы вычитанием рекламных не рассчитывались.</p>'
body+='<p>Текущие отправления обновлены за весь период работы волн; базовые отправления переиспользованы из полных снимков до запуска. Статусы исторических отмен — на момент тех снимков. Общие продажи включают все товары аккаунта независимо от наличия model_id; текущих строк без карты model_id — 0.</p>'
body+='<p>На результат также влияют снижение ставок 20 SKU акк1 30 октября, две дополнительные правки 8 ноября, другие CPC/CPO-кампании, праздник 4 ноября в части эталонных окон, акции 11.11 и восстановление карточек акк2 из автоархива. Историческое наличие и изменение склеек не реконструировались. Все эти причины мешают приписать общий рост одной новой волне.</p>'
body+='<p>Текущие состояния прочитаны отдельно. Последний guard — 11.11 в 09:00 МСК, оба CHECKED, действий остановки 0. В этом обновлении ставок, бюджетов, составов, активаций и остановок не было.</p>'
slug='/api/wb-bids/reports/ozon-wave1-profile-2026-11-11'
body+=f'<p><a href="{slug}-campaigns">РК и рекламные показатели CSV</a> · <a href="{slug}-profile">Сравнение профиля/склеек CSV</a> · <a href="{slug}-daily">Профиль по дням CSV</a> · <a href="{slug}-data">Полная сводка JSON</a></p>'
(P/'index.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Ozon: волна 1 и профиль 11 ноября</title><style>body{font:16px/1.55 system-ui;max-width:1400px;margin:24px auto;padding:0 16px}table{border-collapse:collapse;width:100%;font-size:14px}td,th{border:1px solid #ddd;padding:9px;text-align:left}th{background:#edf2fa}.scroll{overflow-x:auto}a{color:#155ab6}summary{cursor:pointer;margin:14px 0}</style>'+body+'</html>')
(P/'TABLES.md').write_text('\n'.join(md)+'\n')
daily=[]
for account,a in A['accounts'].items():
    for r in a['account_days']:
        daily.append({'account':account,'date_msk':r['date_msk'],'from_msk':r['from_msk'],'through_msk':r['through_msk'],'partial':r['partial'],
                      'actual_orders':r['actual']['orders'],'baseline4_mean_orders':r['baseline4_mean']['orders'],'orders_change_percent':r['change_percent']['orders'],
                      'actual_ordered_value_RUB':r['actual']['ordered_value_RUB'],'baseline4_mean_ordered_value_RUB':r['baseline4_mean']['ordered_value_RUB'],
                      'value_change_percent':r['change_percent']['ordered_value_RUB']})
with (P/'account_daily.csv').open('w',encoding='utf-8-sig',newline='') as f:
    w=csv.DictWriter(f,fieldnames=list(daily[0]));w.writeheader();w.writerows(daily)
print('Rendered:8RUNNING; profile value changes +48.4%/+18.7%; full10Nov +89.0%/-16.6%; causal effect not identified')
