# поток: mkt
"""Build the 05.10 chip research handoff from read-only cached evidence.

This report never writes to WB, MoySklad, or the database. Manufacturer model
requirements and supplier SKU declarations remain distinct. No silent-name rule.
"""
import collections
import csv
import html
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
TMP = pathlib.Path('/tmp')
OUT = ROOT / 'docs' / 'reports'
STAMP = '2026-10-05'


def read(path):
    return json.loads(path.read_text())


def csv_read(path):
    with path.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f, delimiter=';'))


def csv_write(path, rows):
    if not rows:
        return
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]), delimiter=';')
        writer.writeheader()
        writer.writerows(rows)


def main():
    pilot = read(TMP / 'mkt_chip_pilot30_20261005.json')
    findings = read(TMP / 'mkt_chip_pilot_findings_20261005.json')
    cactus = read(TMP / 'mkt_chip_pilot_cactus_20261005.json')
    raw = read(TMP / 'mkt_chip_raw_dictionary_pass_20261005.json')
    live = read(TMP / 'mkt_chip_ms_live_cards_20261005.json')['cards']
    deep = read(OUT / f'mkt_wb_chip_deep_{STAMP}.json')['code_rows']
    catalog = read(TMP / 'mkt_glue_eight_catalog_20261005.json')
    inventory = read(TMP / 'mkt_chip_additional_sources_20261005/inventory.json')
    names = csv_read(OUT / f'mkt_wb_chip_supplier_names_only_{STAMP}.csv')
    products = {r['ms_id']: r for r in live}
    by_code = collections.defaultdict(list)
    for r in live:
        by_code[r['external_code']].append(r)
    supplier_id = '0b14dc16-c51e-11ef-0a80-0c79000af987'
    supplier_url = '/counterparty/' + supplier_id
    evidence = []
    for r in names:
        product = products[r['ms_id']]
        evidence.append({'code': r['наш_код'], 'class': r['класс'],
                         'supplier_article': product['article'], 'supplier_product_code': product['code'],
                         'ms_id': product['ms_id'], 'source_type': 'Наименование МС',
                         'text': r['значение'], 'source': 'Текущая карточка товара МС',
                         'row': '', 'checked_at': STAMP,
                         'mapping': 'Точный ms_id и внешний код товара',
                         'scope': 'Товар указанного поставщика; остальные исполнения отдельно'})
    seen = set()
    for r in raw['verified'] + raw['unverified']:
        key = (r['наш_код'], r['поставщик'], r['артикул_поставщика'], r['файл'], r['строка'])
        if key in seen:
            continue
        candidates = [p for p in by_code[r['наш_код']]
                      if str(p['article'] or '').strip().upper() == r['артикул_поставщика'].strip().upper()]
        mapping = r['основание_связи']
        if 'требуется проверка' in mapping:
            # The two G&G handheld ink cartridges were absent from the old parser
            # (GA-/GB- not in its article regex). Verify current supplier+article,
            # not a substring of a model or the informal supplier code suffix.
            assert r['наш_код'] in {'3919', '3924'}
            exact = [p for p in candidates
                     if supplier_url in json.dumps(p.get('supplier') or {})]
            assert len(exact) == 1
            product = exact[0]
            model = next(x['tc_model'] for x in deep if x['code'] == r['наш_код'])
            assert model.upper() == product['article'].upper()
            mapping = ('Проверено 05.10: поставщик Феррет совпадает с профилем kaktus_msk; '
                       'точный артикул и внешний код в текущем МС; модель ТК совпадает')
        else:
            product = candidates[0] if candidates else {}
        seen.add(key)
        evidence.append({'code': r['наш_код'], 'class': r['класс'],
                         'supplier_article': r['артикул_поставщика'],
                         'supplier_product_code': product.get('code', ''),
                         'ms_id': product.get('ms_id', ''), 'source_type': 'Исходный прайс',
                         'text': r['исходный_текст'], 'source': r['файл'], 'row': r['строка'],
                         'checked_at': STAMP, 'mapping': mapping,
                         'scope': 'Товар указанного поставщика; остальные исполнения отдельно'})
    strict_codes = {r['code'] for r in evidence}
    assert strict_codes == {'1380', '6231', '3132', '1664', '3919', '3924'}
    web_by_code = {r['code']: r for r in cactus}
    for p in pilot:
        f = findings.get(p['code'], {'status': 'Явного класса в первоисточнике не найдено',
            'finding': 'Поиск первичных источников проведён. Отсутствие результата не означает отсутствие чипа.',
            'level': 'unresolved', 'sources': []})
        p.update(f)
        p['searched_query'] = f"{p['manufacturer']} {p['model']} chip / чип; сайты производителей и технические каталоги"
        p['checked_at'] = STAMP
        p['supplier_evidence'] = web_by_code.get(p['code'], {})
        c = p['supplier_evidence']
        if c.get('exact_article_in_title'):
            p['sources'] = p['sources'] + [{'title': f"Cactus: точный артикул {c['article']}", 'url': c['url']}]
        if c.get('chip') and c.get('exact_article_in_title') and p['code'] != '0002' and p['level'] == 'unresolved':
            p.update(status='Есть декларация производителя по артикулу', level='supplier_declaration',
                     finding='На сайте Cactus у точного артикула указано «с чипом». В контрольном Q2612A '
                             'тот же источник противоречит техническому описанию, поэтому декларацию '
                             'нужно проверить до массового переноса. Все поставщики кода этим не закрыты.')
        p['account_counts'] = [{k: r[k] for k in ('account', 'primary_cards', 'bundle_cards', 'children_cards')}
                               for r in deep if r['code'] == p['code'] and r['main_present']]
        p['inheritance'] = ('Блок: конфликт источников' if p['level'] == 'conflict'
            else 'Проверить точный товар, исполнение и состав бандла; совпадение серии принтера не является наследованием')
    levels = dict(collections.Counter(r['level'] for r in pilot))
    families = []
    for code in sorted(strict_codes):
        for r in deep:
            if r['code'] != code or not r['main_present']:
                continue
            nmids = {int(x) for x in r['nm_ids']}
            for c in catalog:
                if c['account'] != r['account'] or int(c['nm_id']) not in nmids:
                    continue
                families.append({'наш_код': code, 'аккаунт': c['account'], 'nm_id': c['nm_id'],
                    'артикул_ВБ': c['vc'], 'название': c['title'], 'тип': c['kind'],
                    'ссылка': f"https://www.wildberries.ru/catalog/{c['nm_id']}/detail.aspx",
                    'класс_товара_поставщика': ' | '.join(sorted({e['class'] for e in evidence if e['code'] == code})),
                    'решение': 'Проверить соответствие исполнению подтверждённого товара; запись не разрешена'})
    declarations = [r for r in cactus if r.get('chip') and r.get('exact_article_in_title')]
    summary = {'initial_unknown_parent_codes': 2842, 'name_codes': len({r['наш_код'] for r in names}),
        'original_price_codes': len({r['code'] for r in evidence if r['source_type'] == 'Исходный прайс'}),
        'name_or_original_price_codes': len(strict_codes), 'new_vs_names_only': len(strict_codes)-2,
        'remaining_without_name_or_price_evidence': 2842-len(strict_codes),
        'all_supplier_variants_certified': 0,
        'pilot_models': len(pilot), 'pilot_levels': levels,
        'cactus_pages_read': sum(r['status'] == 'read' for r in cactus),
        'cactus_explicit_fields': len(declarations), 'cactus_conflicts': 1,
        'cactus_nonconflicting_declarations_pending_source_validation': len(declarations)-1,
        'full_original_price_files': 5, 'unmatched_lists': 7,
        'unmatched_lists_are_not_full_prices': True, 'marketplace_writes': 0}
    payload = {'generated_at': STAMP, 'summary': summary, 'evidence': evidence,
               'pilot': pilot, 'price_source_inventory': inventory,
               'selection': '15 моделей с наибольшими заказами +15 для разнообразия производителей; не случайная выборка',
               'supplier_variant_policy': 'Источник по одному артикулу не закрывает все товары внешнего кода'}
    stem = OUT / f'mkt_wb_chip_pilot30_{STAMP}'
    stem.with_suffix('.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    csv_write(OUT / f'mkt_wb_chip_primary_evidence_{STAMP}.csv', evidence)
    csv_write(OUT / f'mkt_wb_chip_inheritance_review_{STAMP}.csv', families)
    csv_write(stem.with_suffix('.csv'), [{
        'наш_код': p['code'], 'модель': p['model'], 'производитель': p['manufacturer'],
        'статус': p['status'], 'результат': p['finding'],
        'артикул_поставщика': p['supplier_evidence'].get('article', ''),
        'чип_на_сайте_Cactus': p['supplier_evidence'].get('chip', ''),
        'наследование': p['inheritance'], 'источники': ' | '.join(s['url'] for s in p['sources'])
    } for p in pilot])
    render(stem.with_suffix('.html'), payload)
    print(json.dumps(summary, ensure_ascii=False))
    print('Saved pilot30 HTML / JSON / CSV, primary evidence CSV, inheritance review CSV')


def render(path, data):
    E = lambda x: html.escape(str(x or ''))
    s = data['summary']
    facts_rows = ''.join(f"<tr><td><code>{E(r['code'])}</code></td><td>{E(r['supplier_article'])}</td>"
        f"<td>{E(r['class'])}</td><td>{E(r['source_type'])}<br>{E(r['source'])} · строка {E(r['row'])}</td>"
        f"<td>{E(r['text'])}<details><summary>Связь с нашим кодом</summary>{E(r['mapping'])}</details></td></tr>"
        for r in data['evidence'])
    cards = []
    for p in data['pilot']:
        counts = ' · '.join(f"{E(c['account'])}: одиночных {c['primary_cards']}, бандлов {c['bundle_cards']}, "
                           f"дочерних {c['children_cards']}" for c in p['account_counts'])
        links = ' · '.join(f'<a href="{E(x["url"])}" target="_blank" rel="noopener">{E(x["title"])}</a>' for x in p['sources'])
        wb = ' · '.join(f'<a href="https://www.wildberries.ru/catalog/{c["nm_id"]}/detail.aspx" target="_blank" rel="noopener">'
                        f'{E(c["account"])} / {c["nm_id"]}</a>' for c in p['primary_wb'])
        c = p['supplier_evidence']
        declaration = f"<p>Артикул Cactus: <code>{E(c.get('article'))}</code>. Поле чипа: <b>{E(c.get('chip') or 'не заполнено')}</b>.</p>" if c.get('exact_article_in_title') else ''
        cards.append(f'<article class="model" data-level="{E(p["level"])}"><h3><code>{E(p["code"])}</code> · {E(p["model"])} '
                     f'<span>{E(p["manufacturer"])}</span></h3><p class="status">{E(p["status"])}</p><p>{E(p["finding"])}</p>'
                     f'{declaration}<p class="muted">{counts}</p><p>{wb}</p><p>{links or "Явного первичного источника по чипу не найдено"}</p>'
                     f'<p class="muted">{E(p["inheritance"])}</p></article>')
    doc = '''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Чипы: прайсы и проверка 30 моделей — 05.10.2026</title><style>
:root{color-scheme:light dark;--bg:#faf9f6;--fg:#182523;--panel:#fff;--border:#d5dfda;--accent:#14615c;--muted:#526963}
@media(prefers-color-scheme:dark){:root{--bg:#15211f;--fg:#e2ece7;--panel:#1c2b27;--border:#41574e;--accent:#83d7c2;--muted:#aec2b9}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:17px/1.6 system-ui,sans-serif}main{max-width:1180px;margin:auto;padding:36px 24px}
h1{font-size:clamp(28px,4vw,44px);line-height:1.15}h2{margin-top:42px}h3{margin-top:0}a{color:var(--accent)}code{font-variant-numeric:tabular-nums}
.muted,small{color:var(--muted)}.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}.tile,.model,.note{border:1px solid var(--border);background:var(--panel);border-radius:10px;padding:20px}
.tile strong{font-size:32px;display:block}.note{border-left:5px solid var(--accent)}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:12px;vertical-align:top;border-bottom:1px solid var(--border);text-align:left}th{background:var(--panel)}
.models{display:grid;gap:14px}.model h3 span{font-size:15px;color:var(--muted)}.status{font-weight:700;color:var(--accent)}input,select{font:inherit;border:1px solid var(--border);padding:10px;background:var(--panel);color:var(--fg);max-width:100%;margin:8px 8px 20px 0}details{margin-top:8px}.model[hidden]{display:none}
</style><main><p class="muted">Исследование на 05 октября 2026 · сведения по товару и технической модели разделены</p>
<h1>Что действительно удалось выяснить о чипах</h1>
<p>База проверки — 2 842 родительских внешних кода без явных сведений. Это уникальные коды товаров, не число карточек ВБ. Дочерние карточки по принтерам и бандлы учитываются отдельно.</p>
<div class="tiles"><div class="tile"><strong>2</strong>кода с прямым указанием в названиях МС</div><div class="tile"><strong>4</strong>кода с прямым указанием в оригинальных прайсах</div><div class="tile"><strong>6</strong>уникальных кодов с подтверждением по конкретному товару</div><div class="tile"><strong>30</strong>моделей проверено в первичных источниках</div></div>
<p>Четыре кода из оригинальных прайсов — 1664, 3132, 3919, 3924. Для 3919/3924 дополнительно проверены текущий поставщик МС (Феррет), точный артикул GA-001BK/GB-001BK и совпадение модели ТК. Они ранее оставались в списке сомнительных связей.</p>
<div class="note"><b>Эти 6 кодов не означают, что сертифицированы все исполнения всех поставщиков.</b> Доказательство относится к указанному товару. До переноса на весь код нужно проверить остальные исполнения. 2 836 исходных кодов пока не получили подтверждения из названия МС или доступного исходного прайса. Данные сайта производителя и технические сведения показаны отдельно.</div>
<h2>Проверка исходных прайсов: граница доступа</h2>
<p>Полностью доступны пять исходных файлов: Colortek, Odissey, Sakura, Cactus/Феррет и NV Print. Проверены полные наименования, примечания и все колонки строк с упоминаниями чипа. Это не все поставщики компании.</p>
<p>По Bulat, S-Print Москва, Rapid, EasyPrint, ProfiLine, VTT и Ramis получены семь списков <em>несопоставленных</em> товаров — всего 44 строки. Полные прайсы этих поставщиков идут через внешний загрузчик; маленькие списки не заменяют полный файл. Полное отсутствие сведений у этих поставщиков данным проходом не доказано.</p>
<p>Прежние 19 кодов включали описания МС. Здесь описания не подменяют наименование. Прежние 447 кодов Cactus были выводом по правилу <code>default_chip</code>, а не найденным упоминанием чипа.</p>
<h2>Подтверждения из названий и исходных файлов</h2><p class="muted">Одна строка — один товар и источник. 6231 представлен двумя товарами; итог считается по уникальному нашему коду.</p>
<div class="scroll"><table><thead><tr><th>Наш код</th><th>Артикул поставщика</th><th>Что указано</th><th>Где найдено</th><th>Исходное наименование / связь</th></tr></thead><tbody>FACT_ROWS</tbody></table></div>
<h2>Конфликт, который нельзя скрыть массовым правилом</h2>
<p><a href="https://cactus-russia.ru/catalog/item/852534/">Cactus CS-Q2612AS</a> указан «с чипом». Но <a href="https://customer.scc-inc.com/Products/Printers/HP%20LaserJet%203055">Static Control описывает Q2612A без чипа</a>, и <a href="https://www.ggimage.com/zh-en/Consumables/Replacement_Laser_Cartridges/Replacement_Toner_Cartridges_for_Hp_Q2612A_FX9_FX10_CRG303_CRG104_CRG703_CRG304.html">G&G NT-CH2612U для Q2612A</a> также имеет явное указание отсутствия чипа. Возможная ошибка атрибута Cactus — вывод из конфликта, а не доказанная причина. Проверка упаковки/физического товара либо подтверждение производителя нужны до закрытия.</p>
<p>В пилоте прочитаны 17 страниц Cactus по точно связанным артикулам. На 10 есть явное поле чипа: одна конфликтует с техническим источником, девять оставлены как декларации по конкретным артикулам, требующие проверки надёжности источника до массового переноса. На семи поле пустое; пустое поле не означает «без чипа».</p>
<h2>30 моделей: результат по каждой и ссылки</h2>
<p>Выборка целевая: 15 моделей с наибольшими заказами +15 для разнообразия производителей. По её доле находок нельзя оценивать весь остаток. Количества семей ниже относятся к снимку каталога на 05.10 и обоим аккаунтам; это объём возможной проверки, не разрешённой правки.</p>
<input id="search" type="search" aria-label="Поиск по коду или модели" placeholder="Наш код, модель, производитель"><select id="level" aria-label="Тип результата"><option value="">Все результаты</option>LEVEL_OPTIONS</select><p id="visible" class="muted"></p><div class="models">MODEL_CARDS</div>
<h2>Как использовать результат для бандлов и дочерних карточек</h2>
<ol><li>Зафиксировать точный товар поставщика: внешний код, артикул, тип чипа, источник и дату. Техническую потребность модели хранить отдельно от наличия чипа в поставке.</li>
<li>Проверить остальные поставщицкие исполнения того же внешнего кода. Несовпадение чипа означает конфликт привязки или версий; не усреднять и не выбирать большинство.</li>
<li>Только после этой проверки наследовать подтверждённое исполнение на дочерние карточки того же товара. Для бандла проверить его состав: одинаковая модель принтера сама по себе недостаточна.</li>
<li>Отдельно разбирать Q2612A и другие конфликты. Сохранять «неизвестно», когда факт отсутствует. Longlife без прямого указания безлимитности остаётся «с чипом», а не «без счётчика».</li></ol>
<p>Подготовлен список связанных карточек шести кодов для проверки: <a href="mkt_wb_chip_inheritance_review_2026-10-05.csv">CSV наследования</a>. В нём нет разрешения на автоматическую правку.</p>
<h2>Следующий проход</h2><p>Получить полные исходные прайсы из внешнего загрузчика, проверить их длинные названия/примечания тем же словарём, затем расширять поиск по точным артикулам производителей. Сначала модели с заказами. Для Q2612A сверить реальную упаковку и устранить конфликт; все 447 Cactus по умолчанию оставлять вне подтверждённого счётчика.</p>
<p><a href="mkt_wb_chip_pilot30_2026-10-05.csv">30 моделей — CSV</a> · <a href="mkt_wb_chip_primary_evidence_2026-10-05.csv">Первичные подтверждения — CSV</a> · <a href="mkt_wb_chip_pilot30_2026-10-05.json">Методика, источники и результаты — JSON</a></p>
<p class="muted">Ни ВБ, ни МС, ни ТК в этом проходе не изменялись. Рабочие старые эвристики не перезапускались; результаты исследования не переписывают рабочие поля чипа.</p>
</main><script>
const rows=[...document.querySelectorAll('.model')],q=document.getElementById('search'),level=document.getElementById('level');function filter(){let n=0;for(const r of rows){r.hidden=!(r.textContent.toLowerCase().includes(q.value.toLowerCase())&&(!level.value||r.dataset.level===level.value));if(!r.hidden)n++}document.getElementById('visible').textContent=`Показано ${n} из ${rows.length}`;}q.addEventListener('input',filter);level.addEventListener('change',filter);filter();
</script></html>'''
    labels = {'conflict': 'Конфликты', 'model_chip': 'Сведения о чипе модели',
              'variant_nochip': 'Исполнение без чипа', 'partial': 'Неполное техническое подтверждение',
              'category': 'Уточнение типа расходника', 'supplier_declaration': 'Декларация производителя',
              'unresolved': 'Класс не подтверждён'}
    options = ''.join(f'<option value="{k}">{E(labels[k])} ({v})</option>'
                      for k, v in s['pilot_levels'].items())
    path.write_text(doc.replace('FACT_ROWS', facts_rows).replace('MODEL_CARDS', ''.join(cards))
                   .replace('LEVEL_OPTIONS', options), encoding='utf-8')


if __name__ == '__main__':
    main()
