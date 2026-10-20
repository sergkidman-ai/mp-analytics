# поток: fin
"""Уточнение сохранённого среза 19.10: type127 — фиксированная Analytics Plus.

Использует только сохранённые источники. monthly/comparison.json остаются исходными;
переклассифицированные данные сохраняются отдельно. БД и Пульт не изменяет.
"""
import copy
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
EVIDENCE = ROOT / 'docs/reports/fin5_ozon_28000_2026-10-20'
PROTECTED = ['web/static/reports.html', 'reports/ozon_mp_page.py',
             'reports/ozon_mp_report.py', 'reports/data/mp_ozon_hist.json']


def read(path):
    return json.loads(path.read_text())


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2))


def fmt(value):
    return f'{value:,.2f}'.replace(',', ' ').replace('.', ',')


def main():
    hashes = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in PROTECTED}
    original = read(OUT / 'monthly.json')
    original_comparison = read(OUT / 'comparison.json')
    monthly = copy.deepcopy(original)
    comparison = copy.deepcopy(original_comparison)
    source = read(EVIDENCE / 'accruals.json')
    catalog = read(EVIDENCE / 'accrual_types_live.json')['accrual_types']
    assert next(x for x in catalog if x['id'] == 127)['name'] == 'AnalyticsPlus'
    summaries, promo_components = [], []
    code = 'OP:OperationSubscriptionAnalyticsPlus'
    for account, label in [('oz_acc1', 'Цифровой'), ('oz_acc2', 'Дисквер')]:
        charge, = [x for x in source if x['account'] == account]
        assert charge['payload']['non_item_fee']['type_id'] == 127
        assert charge['payload']['date'] == '2026-10-01'
        assert float(charge['total_amount']) == -28000
        current = next(x for x in monthly if x['account'] == account and x['month'] == '2026-10')
        previous = next(x for x in monthly if x['account'] == account and x['month'] == '2026-09')
        before = next(x for x in original if x['account'] == account and x['month'] == '2026-10')
        values, prev = current['values'], previous['values']
        values['promo'] += 28000
        values['unclassified'] -= 28000
        current['detail']['promo'][code] = 28000
        current['source'] = 'original_2026-10-19_snapshot_reclassified_2026-10-20'
        assert abs(sum(current['detail']['promo'].values()) - values['promo']) < 0.005
        for key in values:
            if key not in ('promo', 'unclassified'):
                assert values[key] == before['values'][key], (account, key)
        assert abs(values['promo'] + values['unclassified'] -
                   before['values']['promo'] - before['values']['unclassified']) < 0.005
        compared = next(x for x in comparison if x['account'] == account)
        for part in compared['components']:
            if part['key'] in ('promo', 'unclassified'):
                part['now'] = values[part['key']]
                part['pct'] = 100 * part['now'] / values['sales']
                part['impact_pp'] = part['prev_pct'] - part['pct']
        compared['components'].sort(key=lambda x: x['impact_pp'])
        assert abs(sum(x['impact_pp'] for x in compared['components']) -
                   (compared['margin'] - compared['prev_margin'])) < 1e-8
        now_detail, prev_detail = current['detail']['promo'], previous['detail']['promo']
        for key in sorted(set(now_detail) | set(prev_detail)):
            now, old = now_detail.get(key, 0), prev_detail.get(key, 0)
            promo_components.append(dict(account=account, code=key, cash_oct=now, cash_sep=old,
                oct_pct=100 * now / values['sales'], sep_pct=100 * old / prev['sales'],
                margin_impact_pp=100 * (old / prev['sales'] - now / values['sales'])))
        parent_impact = 100 * (prev['promo'] / prev['sales'] - values['promo'] / values['sales'])
        assert abs(sum(x['margin_impact_pp'] for x in promo_components if x['account'] == account)
                   - parent_impact) < 1e-8
        summaries.append(dict(account=account, label=label, subscription_sep=24990,
            subscription_oct=34990, monthly_increase=10000, promo_oct=values['promo'],
            promo_sep=prev['promo'], promo_oct_pct=100 * values['promo'] / values['sales'],
            promo_sep_pct=100 * prev['promo'] / prev['sales'],
            promo_margin_impact_pp=parent_impact, unclassified_oct=values['unclassified'],
            subscription_oct_pct=100 * 34990 / values['sales'],
            subscription_sep_pct=100 * 24990 / prev['sales'],
            subscription_margin_impact_pp=100 * (24990 / prev['sales'] - 34990 / values['sales']),
            net=values['net'], margin=values['margin']))
    save('monthly_subscription_reclassified.json', monthly)
    save('comparison_subscription_reclassified.json', comparison)
    save('subscription_revision.json', dict(revision_date='2026-10-20',
        financial_snapshot='2026-10-19, original coverage preserved', accounts=summaries,
        promo_components=promo_components, financial_totals_changed=False,
        subscription_period='30.09.26-30.10.26',
        interpretation='Полная фикс-плата начислена сразу; октябрь промежуточный, сентябрь полный. '
                       'Вклады в п.п. включают разницу оборота и момента начисления, не только рост тарифа.'))
    funding = read(OUT / 'advertising_funding_revision.json')
    funding['promotion_cash_components'] = promo_components
    funding['subscription_revision_applied'] = True
    save('advertising_funding_revision.json', funding)

    path = OUT / 'report.html'
    text = path.read_text()
    backup = ROOT / 'backups/fin5_subscription_analysis_20261020'
    backup.mkdir(parents=True, exist_ok=True)
    if not (backup / 'report.html').exists():
        (backup / 'report.html').write_text(text)
    unchanged_sections = [section for section in re.findall(r'<section>.*?</section>', text, re.S)
                          if any(f'<h2>{h}</h2>' in section for h in
                                 ['Маркет — ошибка границы периода', 'WB — маржа держится',
                                  'Какие новости уже действуют'])]
    replacements = {
        'Меньшие расходы на отзывы и отдельные компоненты подписки частично компенсируют рост; строка денежного продвижения в целом9,90→10,41% (+0,51 п.п.).':
        'Меньшие расходы на отзывы частично компенсируют рост. С учётом полной подписки денежное продвижение составляет985 968,56 ₽: доля9,90→10,72% (+0,82 п.п. нагрузки, вклад в маржу−0,82 п.п.). Подписка выросла с24 990 до34 990 ₽; её доля в этом промежуточном срезе0,17→0,38% (+0,21 п.п.). Прежний вывод о снижении затрат на подписку снят.',
        'Общая строка денежного продвижения снизилась с8,89% до7,83% оборота, то есть помогла марже на1,06 п.п.':
        'С учётом полной подписки денежное продвижение составляет176 318,33 ₽: доля выросла с8,89% до9,31% оборота, вклад в маржу−0,42 п.п. Прежний вывод об улучшении на1,06 п.п. снят. Подписка выросла с24 990 до34 990 ₽; её доля0,89→1,85%, вклад в маржу−0,96 п.п. Это сравнение промежуточного октября с полным сентябрём, а не оценка только повышения тарифа.',
        'При этом оплата за заказ и другие компоненты в доле оборота снизились сильнее.':
        'Снижение доли оплаты за заказ и других рекламных расходов уже не перекрывает совокупный рост первых отзывов и полной подписки.',
        'Также у обоих кабинетов есть списание28 000 ₽ с нерасшифрованным кодом AccrualType_127; отдельная строка подписки6 990 ₽ не показывает всей возможной стоимости Premium. Совпадение с новой структурой оплаты аналитики — гипотеза до сверки акта.28 000 ₽ уже включены в денежные удержания в «Неклассифицировано», повторного добавления нет. У Дисквера эта сумма особенно заметна на меньшем обороте.':
        'Списания по28 000 ₽ подтверждены справочником Ozon: type127 — AnalyticsPlus, «Аналитика Plus». Вместе с6 990 ₽ Premium Plus полный платёж составляет34 990 ₽ на кабинет. В Пульте оба списания уже перенесены из «Неклассифицировано» в «Продвижение → Аналитика Plus». Это месячная фикс-плата; в прогнозе её учитываем один раз. Прибыль не уменьшаем повторно.',
        'Проверить состав товаров в «Сборе первых отзывов» и смысл фиксированных 28 000 ₽ по актам двух юрлиц Ozon.':
        'Проверить состав товаров в «Сборе первых отзывов» и окупаемость полной подписки34 990 ₽ в каждом кабинете. Назначение28 000 ₽ установлено: «Аналитика Plus», расшифровка уже внесена в Пульт.',
        'у Дисквера в фокусе возвраты, платные отзывы и отдельные фиксированные начисления.':
        'у Дисквера в фокусе возвраты, платные отзывы и подорожавшая подписка. С учётом аналитики общая доля продвижения выросла8,89→9,31%; у Цифрового9,90→10,72%.',
        'Анализ уточнён20.10: деньги и рекламные бонусы разделены. Срез исходных финансовых показателей сохранён на19.10. Отчёты Ozon в Пульте не изменялись.':
        'Анализ уточнён20.10: деньги и бонусы разделены; полная подписка34 990 ₽ учтена в продвижении. Срез финансовых сумм сохранён на19.10, обновлена только классификация. Расшифровка подписки уже применена в Пульте.',
        'Данные БД и площадок не изменялись.':
        'Этот пересмотр анализа не меняет БД и площадки; ранее согласованная классификация подписки уже применена в Пульте.',
        'href="comparison.json"': 'href="comparison_subscription_reclassified.json"',
    }
    for old, new in replacements.items():
        assert old in text or new in text, old
        text = text.replace(old, new)
    table = '''<section id="subscription-revision"><h2>Ozon — полная стоимость подписки и уточнение структуры расходов</h2><p>С1 октября в каждом кабинете отдельно списаны6 990 ₽ Premium Plus и28 000 ₽ «Аналитика Plus» от «Озон Технологий». Итого34 990 ₽ вместо24 990 ₽: +10 000 ₽ в месяц на кабинет (+40,02%), суммарно+20 000 ₽. Обе операции относятся к периоду30.09–30.10.2026.</p><div class="scroll"><table><tr><th>Кабинет</th><th>Подписка сентября, ₽</th><th>Полная подписка октября, ₽</th><th>Продвижение октября, ₽</th><th>Доля продвижения: сентябрь → октябрь</th><th>Вклад продвижения в маржу, п.п.</th></tr>'''
    for row in summaries:
        table += (f"<tr><td>{row['label']}</td><td>24 990</td><td>34 990</td>"
                  f"<td>{fmt(row['promo_oct'])}</td><td>{fmt(row['promo_sep_pct'])}% → "
                  f"{fmt(row['promo_oct_pct'])}%</td><td>−{fmt(-row['promo_margin_impact_pp'])}</td></tr>")
    table += '''</table></div><p>Расшифровка переместила по28 000 ₽ из «Неклассифицировано» в продвижение. Остаток неклассифицированного в исходном срезе: Цифровой−18 074,16 ₽ (нетто-приход), Дисквер2 452,25 ₽. Всего удержаний, прибыль и маржа не меняются:17,05% и14,96%. Это структура исходного среза19.10, без подмешивания новых начислений20.10.</p><p>Сентябрь полный, октябрь промежуточный; подписка за новый период начислена сразу целиком. Поэтому изменение её доли в обороте не равно чистому эффекту повышения тарифа. Положительные корректировки18 325,99/3 331,99 ₽ уже учтены отдельно и согласуются с возвратом неиспользованных дней старой подписки; их generic type57 актом отдельно не расшифрован, в подписку их не переносим.</p><p>Источник расшифровки: <a href="../fin5_ozon_28000_2026-10-20/report.html">проверка двух списаний</a>; <a href="subscription_revision.json">расчёт структуры и вкладов</a>; <a href="monthly_subscription_reclassified.json">исходный срез с уточнённой классификацией</a>.</p></section>'''
    if '<section id="subscription-revision">' in text:
        text = re.sub(r'<section id="subscription-revision">.*?</section>', lambda _: table, text, count=1, flags=re.S)
    else:
        anchor = '<section><h2>Ozon Цифровой — рост денежных расходов подтверждён</h2>'
        assert anchor in text
        text = text.replace(anchor, table + anchor, 1)
    assert all(section in text for section in unchanged_sections)
    assert 'гипотеза до сверки акта' not in text
    # Длинные пути файлов в методике должны переноситься на узком экране.
    if 'code{overflow-wrap:anywhere}' not in text:
        text = text.replace('</style>', 'code{overflow-wrap:anywhere}</style>', 1)
    path.write_text(text)
    ads_path = OUT / 'diskver_ads.html'
    ads = ads_path.read_text()
    if not (backup / 'diskver_ads.html').exists():
        (backup / 'diskver_ads.html').write_text(ads)
    if 'id="subscription-note"' not in ads:
        note = ('<p id="subscription-note"><b>Уточнение подписки20.10.</b> '
                'Полная подписка Дисквера —34 990 ₽ (6 990 Premium Plus +28 000 '
                '«Аналитика Plus»). Она входит в общую строку продвижения, отдельно '
                'от CPC/CPO и показателей71/174тыс. ₽. После расшифровки доля всего '
                'продвижения в исходном срезе выросла8,89→9,31% оборота, хотя '
                'денежные клики и заказы в сопоставимые дни почти не изменились. '
                '<a href="report.html#subscription-revision">Обновлённый анализ</a>.</p>')
        ads = ads.replace('</h1>', '</h1>' + note, 1)
        ads_path.write_text(ads)
    after = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in PROTECTED}
    assert hashes == after
    save('subscription_revision_checks.json', dict(production_files_unchanged=True,
        protected_sha256=after, financial_totals_unchanged=True,
        original_monthly_unchanged=original == read(OUT / 'monthly.json'),
        original_comparison_unchanged=original_comparison == read(OUT / 'comparison.json'),
        other_market_sections_unchanged=True, promo_components_reconciled=True))
    for row in summaries:
        print(row['account'], 'promo', fmt(row['promo_oct']),
              'share', fmt(row['promo_sep_pct']), '→', fmt(row['promo_oct_pct']),
              'margin impact', fmt(row['promo_margin_impact_pp']))
    print('Analysis revised; original cutoff, financial totals and production files preserved.')


if __name__ == '__main__':
    main()
