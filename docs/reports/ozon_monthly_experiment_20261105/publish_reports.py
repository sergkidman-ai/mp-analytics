# поток: mkt / ozon
"""Publish the drafted monthly protocol and separate annual observational review to existing BI."""
import csv
import html
import json
import pathlib
import re

OUT=pathlib.Path(__file__).parent
YEAR=OUT.parent/'ozon_year_review_20261105'
BASE='/api/wb-bids/reports/'
MONTH_KEY='ozon-monthly-plan-2026-11-05'
YEAR_KEY='ozon-year-review-2026-11-05'


def inline(text):
    text=html.escape(text)
    text=re.sub(r'\[([^\]]+)\]\((https?://[^\s)]+|/api/[^\s)]+)\)',r'<a href="\2">\1</a>',text)
    text=re.sub(r'\*\*(.+?)\*\*',r'<strong>\1</strong>',text)
    text=re.sub(r'`([^`]+)`',r'<code>\1</code>',text)
    return text


def markdown(source):
    result=[];paragraph=[];table=[];list_kind=None
    def flush_paragraph():
        if paragraph:result.append('<p>'+inline(' '.join(paragraph))+'</p>');paragraph.clear()
    def flush_table():
        if not table:return
        result.append('<div class="scroll"><table>')
        header=True
        for row in table:
            cells=[x.strip() for x in row.strip().strip('|').split('|')]
            if all(re.fullmatch(r':?-+:?',x) for x in cells):continue
            tag='th' if header else 'td';result.append('<tr>'+''.join(f'<{tag}>'+inline(x)+f'</{tag}>' for x in cells)+'</tr>');header=False
        result.append('</table></div>');table.clear()
    def close_list():
        nonlocal list_kind
        if list_kind:result.append('</'+list_kind+'>');list_kind=None
    for line in source.splitlines()+['']:
        if line.startswith('|'):
            flush_paragraph();close_list();table.append(line);continue
        flush_table()
        if not line.strip():flush_paragraph();close_list();continue
        head=re.match(r'^(#{1,4}) (.+)',line)
        if head:
            flush_paragraph();close_list();n=len(head.group(1));result.append(f'<h{n}>'+inline(head.group(2))+f'</h{n}>');continue
        item=re.match(r'^(- |\d+\. )(.+)',line)
        if item:
            flush_paragraph();kind='ul' if item.group(1)=='- ' else 'ol'
            if list_kind!=kind:close_list();result.append('<'+kind+'>');list_kind=kind
            result.append('<li>'+inline(item.group(2))+'</li>');continue
        if list_kind and line.startswith('  '):
            result.append('<p class="continuation">'+inline(line.strip())+'</p>');continue
        close_list();paragraph.append(line.strip())
    return '\n'.join(result)


def page(title,body,links):
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex,nofollow,noarchive"><title>{html.escape(title)}</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f4f6f8;color:#182230;font:16px/1.65 system-ui,sans-serif}}main{{max-width:1100px;margin:28px auto;padding:32px;background:white;border:1px solid #e4e7ec;border-radius:14px}}h1{{font-size:29px;line-height:1.3}}h2{{font-size:22px;margin-top:32px}}h3{{font-size:19px}}a{{color:#175cd3}}code{{overflow-wrap:anywhere}}p{{margin:12px 0;overflow-wrap:anywhere}}.meta{{color:#667085;font-size:14px}}.notice{{padding:17px;border:1px solid #fedf89;background:#fffaeb;border-radius:10px;margin:18px 0}}.links{{display:flex;gap:10px;flex-wrap:wrap;margin:20px 0}}.links a{{padding:9px 13px;background:#eff4ff;border:1px solid #b2ccff;border-radius:8px;text-decoration:none}}.scroll{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;margin:16px 0}}th,td{{padding:11px;border-bottom:1px solid #e4e7ec;text-align:right}}th:first-child,td:first-child{{text-align:left;min-width:150px}}th{{background:#f8fafc}}li{{margin:9px 0}}.continuation{{margin-left:12px}}footer{{margin-top:30px;padding-top:16px;border-top:1px solid #e4e7ec}}@media(max-width:640px){{main{{margin:0;padding:20px 16px;border:0;border-radius:0}}h1{{font-size:25px}}th,td{{padding:8px}}}}@media print{{body{{background:white}}main{{border:0;padding:0}}.links{{display:none}}}}</style></head><body><main><div class="meta">oz_acc1 · подготовлено 05.11.2026 · доступ через существующий вход BI</div><div class="links">{links}</div>{body}<footer>Реклама, ставки, бюджеты и экспериментальные назначения на этом шаге не применялись.</footer></main></body></html>'''


def button(key,text):return f'<a href="{BASE}{key}">{html.escape(text)}</a>'
manifest=json.loads((OUT/'wave_draft_manifest.json').read_text())
monthlinks=''.join(button(MONTH_KEY+'-wave'+str(w),'Волна '+str(w)+' — CSV (DRAFT)') for w in [1,2,3])
monthlinks+=button(MONTH_KEY+'-all','Все 9242 — черновик CSV')+button(YEAR_KEY,'Отдельный годовой анализ')
body='<div class="notice"><strong>Черновик, не запуск.</strong> Три решения ещё открыты: общий месяц или месяц каждой волне; постоянная контрольная группа; максимальный дополнительный CPC-расход за месяц. Показанные CSV — сценарий полного пула в трёх волнах, без применённого T/C.</div>'
body+=markdown((OUT/'PROTOCOL.md').read_text())
body+='<p>'+button(MONTH_KEY+'-year-flags','Исторические признаки для черновых волн — CSV')+' '+button(MONTH_KEY+'-data','Manifest DRAFT JSON')+' '+button(MONTH_KEY+'-cost','Расчёт технического бюджета JSON')+'</p>'
(OUT/'index.html').write_text(page('Месячный эксперимент Ozon: три волны',body,monthlinks))

s=json.loads((YEAR/'analysis_summary.json').read_text())
def rub(value):return f'{float(value):,.2f}'.replace(',',' ').replace('.',',')
cost_share=float(s['annual_CPC_spend_without_usable_off_on_current_stock_rub'])/float(s['annual_CPC_spend_current_stock_rub'])*100
result=['# Годовой разбор CPC и длинного хвоста — предварительный результат','',
        '**Все основные списки ниже — только товары, которые сейчас в наличии.** Исторический период: 05.11.2025–04.11.2026.','',
        'Собраны общие FBS/FBO за все 365 дней: 19 151 отправление, из них 1914 отменены. Рекламные кампанийные даты покрыты за весь год; 16 новых подробных SKU-отчётов и четыре октябрьских файла переиспользованы.','',
        '| Результат | SKU в наличии | Что это означает |','|---|---:|---|',
        f'| С наблюдаемыми переходами CPC-трафика | {s["currently_in_stock_skus_with_episodes"]} | Не обязательно доказанные включения/выключения конфигурации |',
        '| Редкий спрос и до, и после CPC | 4948 | Кандидаты для проверки затрат, не доказанный нулевой эффект |',
        '| Прочие: недостаточно точности, зрелой базы или проблемы идентичности | 4074 | Нельзя автоматически назвать ростом либо отсутствием эффекта |',
        f'| Продавались за год без наблюдаемого CPC-трафика | {s["selling_without_observed_cpc_in_stock_skus"]} | CPO и удалённые кампании не исключены |',
        f'| Есть эпизоды продаж до CPC без видимого улучшения после | {s["selling_before_CPC_no_observed_gain_review_skus"]} | Список ручной проверки, не команда отключения |',
        f'| Рекламировались, но нет пригодного перехода для сравнения | {s["current_stock_advertised_without_usable_off_on_comparison"]} | Нельзя восстановить контрфакт «без CPC» |','',
        f'Из найденных переходов {s.get("historical_E8_applied_restore_matched_stock_skus",0)} SKU сопоставлены с документированным применённым возвратом E8 в августе. Остальные случаи — наблюдаемая смена трафика; поле «Дата добавления» не подменяет журнал конфигурации. Исторические группы E8 сохранены и не становятся группами нового ноября.','',
        '## Что внутри редкого хвоста','',
        f'Из 4948 SKU у {s["rare_both_modes_skus_with_some_sales_before_CPC"]} встречались продажи в окне до CPC; у {s["rare_both_modes_skus_zero_sales_in_both_all_compared_windows"]} нет заказов ни в одном из сравниваемых окон до/после. Это не означает отсутствие продаж в остальные дни года.','',
        f'Сейчас {s["rare_both_modes_skus_currently_in_CPC"]} из этих SKU находятся в RUNNING CPC, а {s["rare_both_modes_skus_in_new_monthly_pool"]} входят в новый месячный пул вне CPC. Исторический флаг не исключает их автоматически из нового теста.','',
        f'Их общий учтённый CPC-расход за год — {rub(s["rare_both_modes_annual_CPC_spend_rub"])} ₽. Это не доказанная сумма потерь: редкость и отсутствие прироста в выбранных окнах сами по себе не дают причинного эффекта или прибыли.','',
        f'У {s.get("rare_both_modes_skus_with_neighbor_sales_in_after_windows",0)} редких SKU в окнах после CPC были продажи соседних товаров склейки. Поэтому слабые собственные продажи ещё не доказывают бесполезность трафика для семьи. Семейный контекст рассчитан по одному текущему составу моделей, исторический состав не восстановлен.','',
        '## Где находится большая часть расходов, для которой нет ответа «без CPC»','',
        f'По всем текущим товарам в наличии учтённый CPC-расход за год — {rub(s["annual_CPC_spend_current_stock_rub"])} ₽. Из них {rub(s["annual_CPC_spend_without_usable_off_on_current_stock_rub"])} ₽ ({cost_share:.1f}%) приходится на товары без пригодного периода перехода. Их способность продаваться без CPC нельзя доказать этой историей; потребуется отдельно согласованный тест сокращения CPC на части склеек.','',
        '## Бестселлеры и эффект рекламы — две разные оси','',
        'Рабочие частотные группы определены по окну до появления CPC, с приведением к 28 дням: до 2 заказов включительно — редкий спрос, больше 2 и меньше 8 — средний, 8+ — частый в наблюдаемом окне. Короткий отрезок сам по себе не доказывает устойчивый бестселлер.','',
        '| Спрос в пригодном окне до CPC | SKU |','|---|---:|']
labels={'RARE_BEFORE_CPC':'Редкий спрос','MIDDLE_BEFORE_CPC':'Средний спрос','FREQUENT_BEFORE_CPC':'Частый спрос в окне','INSUFFICIENT_BASELINE':'Нет пригодной базы'}
for k,n in s.get('baseline_demand_segments_current_stock',{}).items():result.append(f'| {labels[k]} | {n} |')
result+=['', 'Не получено надёжной группы «причинно доказанный рост от рекламы» или «причинно доказанный нулевой эффект». В данных нет полной истории остатков и конфигурации рекламы; часто заказов слишком мало. Отсутствие значимого результата не заменяет доказательство отсутствия эффекта.','',
         '## Полные файлы','',
         f'- [Сводка 9022 SKU с переходами]({BASE}{YEAR_KEY}-skus)',
         f'- [Все сравниваемые эпизоды товаров в наличии]({BASE}{YEAR_KEY}-episodes)',
         f'- [4948 SKU с редким спросом в обоих режимах]({BASE}{YEAR_KEY}-rare)',
         f'- [154 товара с продажами без наблюдаемого CPC за год]({BASE}{YEAR_KEY}-no-cpc-sales)',
         f'- [107 товаров для проверки продаж до CPC без наблюдаемого улучшения]({BASE}{YEAR_KEY}-no-gain-review)',
         f'- [Годовые расходы/заказы всех 15 255 текущих товаров в наличии]({BASE}{YEAR_KEY}-profile)',
         f'- [Товары без пригодного сравнения]({BASE}{YEAR_KEY}-no-comparison)',
         f'- [Группы спроса в окне до CPC]({BASE}{YEAR_KEY}-demand)', '',
         f'- [Продажи склеек и соседей в исторических эпизодах]({BASE}{YEAR_KEY}-families)',
         f'- [Эпизоды, сопоставленные с применённым возвратом E8]({BASE}{YEAR_KEY}-logged)', '',
         'Общие продажи — уникальные неотменённые order_id и стоимость заказанного, не финансовая выручка/чистая прибыль. Включённый общий CPO не позволяет называть периоды без CPC полностью органическими.','',
         'Следующий содержательный шаг: проверить кандидатов с расходами и редким спросом, затем проспективно проверить необходимость CPC на контролируемых семействах. Массовое отключение этим отчётом не назначено.','']
(YEAR/'RESULT.md').write_text('\n'.join(result))
yearlinks=button(YEAR_KEY+'-skus','Сводка SKU — CSV')+button(YEAR_KEY+'-rare','Редкий спрос — CSV')+button(YEAR_KEY+'-profile','Все расходы/продажи в наличии — CSV')+button(MONTH_KEY,'Новый месячный эксперимент')
yearbody='<div class="notice"><strong>Это наблюдательный разбор, не доказанный причинный эффект.</strong> Редко продававшийся товар и товар с доказанным отсутствием эффекта рекламы — разные категории. Во всех списках явно сохранена неопределённость.</div>'
yearbody+=markdown((YEAR/'RESULT.md').read_text())+'<hr>'+markdown((YEAR/'METHOD.md').read_text())
(YEAR/'index.html').write_text(page('Годовой анализ Ozon: CPC и длинный хвост',yearbody,yearlinks))
print('Published HTML artifacts: monthly draft protocol and separate annual observational review.')
