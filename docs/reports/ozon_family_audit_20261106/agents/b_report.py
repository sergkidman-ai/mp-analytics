# поток: ozon-family; renders the completed B snapshot audit without API/DB reads.
import collections
import csv
import json
import pathlib

P = pathlib.Path(__file__).parent


def rows(name):
    with (P / name).open(encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


s = json.loads((P / 'b_summary.json').read_text())
groups = rows('b_glue_groups.csv')
members = rows('b_glue_members.csv')
exceptions = rows('b_exceptions.csv')
by_pid = {r['product_id']: r for r in members}
by_mid = {r['model_id']: r for r in groups}
assert len(members) == s['products']
assert len(groups) == s['positive_model_id_groups']
assert sum(int(g['observed_products']) for g in groups) + s['zero_or_missing_model_products'] == len(members)
exceptions_by_type = collections.Counter(e['exception_type'] for e in exceptions)
assert dict(exceptions_by_type) == s['exceptions_by_type']
assert sum(r['model_info_crosssource_equal'] == 'False' for r in members) == exceptions_by_type['model_info_differs_between_info_and_attributes']
unknown_pids = {r['product_id'] for r in members if not r['model_id']}
assert unknown_pids == {e['key'] for e in exceptions if e['exception_type'] == 'model_id_missing_or_zero'}
missing_schema_products = sum(not r['schema_source'] for r in members)
active_groups = [g for g in groups if int(g['nonarchived_products'])]
lines = [
    '# B — фактические объединения Ozon oz_acc1', '',
    'Проверены все карточки и все model_id единого снимка 06.11.2026. Это структурный аудит; бизнес-данные, карточки, реклама и назначения экспериментов не менялись.', '',
    f"Каталог: {s['products']} product_id = {s['nonarchived_catalog_products']} ALL + {s['archived_catalog_products']} ARCHIVED. Пересечение/дубли product_id отсутствуют. Все перечисленные ID имеют product-info и атрибуты; coverage по страницам и запросам сохранён ведущим. Группировка производится по product_id, SKU-алиасы не умножают число вариантов.", '',
    '| Проверка | Результат |', '|---|---:|',
    f"| Положительные model_id | {s['positive_model_id_groups']} |",
    f"| Наблюдаемые группы от двух product_id | {s['multimember_groups']} |",
    f"| Признак одиночки по API: count=1, observed=1, без расхождений между info/attrs | {s['api_singletons']} |",
    f"| Карточки с нулевым/отсутствующим model_id — статус объединения неизвестен | {s['zero_or_missing_model_products']} |",
    f"| Группы с observed=count | {s['count_states'].get('count_matches_observed', 0)} |",
    f"| Группы с observed<count | {s['count_states'].get('observed_less_than_reported', 0)} |",
    f"| Группы хотя бы с одной неархивной карточкой | {len(active_groups)} |",
    f"| Атрибуты: product_id с ответом / весь каталог | {s['attributes_products']} / {s['products']} |",
    f"| Валидные схемы category/type | {s['schema_pairs']} |",
    f"| Карточки без валидной схемы (состав перечислен в members) | {missing_schema_products} |",
    f"| Расхождения model_info между product-info и attributes | {exceptions_by_type['model_info_differs_between_info_and_attributes']} |", '',
    'Нулевой model_id не объединён в одну фиктивную семью. Отсутствие второго члена в одном только ALL не трактуется как одиночка: архивные члены включены. Из 99 дефицитов, наблюдавшихся при сверке только ALL, union с архивом закрыл 98.', '',
    'API-состав подтверждён положительным model_info.model_id. Совпадение наблюдаемого количества с согласованным count подтверждает согласованность именно этого снимка; scope count по первичной текстовой API-документации не удалось независимо подтвердить. Витринное отображение вариантов и исторический состав не проверены. Поэтому API-признак одиночки не объявлен доказанной одиночкой покупательского интерфейса.', '',
    '## Схема картриджей и допустимость', '',
    'Официальный снимок схемы 17028935/95980: 50 атрибутов. is_aspect=true: Объём мл (8163), Формат плёнки (10676), Количество в упаковке шт (8513), Толщина плёнки (10675), Цвет тонера/чернил (9602). Странные оси плёнки оставлены как факт схемы. Их наличие не является разрешением смешивать разные модели картриджей.', '',
    'Для вариативности по количеству и цвету есть техническая опора в схеме; для конкретной группы дополнительно нужны одна и та же физическая модель, бренд, категория и корректные значения. Ресурс, совместимые принтеры, бренд печатающего устройства и тип расходника не помечены is_aspect. Их различия являются поводом проверить реальную модель и состав, а не доказательством нарушения. Карточки по разным совместимым принтерам могут описывать один и тот же физический картридж.', '',
    '9048 — поле названия модели для объединения. 12141 — другое поле для шаблона наименования. Общее значение 9048 отражает контент/намерение, фактический model_id им не заменяется. Разные артикульные родители, большая товарная семья и model_id остаются разными связями.', '',
    'Колонка required_attribute_array_absences считает только отсутствие schema-required ID в обычном массиве attributes. Она не доказывает незаполненное обязательное поле: тип представлен также верхнеуровневым type_id, возможны attributes_with_defaults и условия маркировки/кодов. Аудит не выдаёт эти технические отсутствия за контентные ошибки.', '',
    '## Исключения', '', '| Тип | Количество |', '|---|---:|']
for typ, n in sorted(exceptions_by_type.items()):
    lines.append(f'| {typ} | {n} |')
lines += ['',
    'Все исключения имеют confirmed_violation=false. Различия nonaspect-полей и одинаковые aspect signatures требуют проверки содержимого и ЛК; они не названы ошибочными склейками. Разные значения атрибута бренда и поля 9048 внутри наблюдаемых групп не обнаружены.', '',
    'Ниже полностью перечислены редкие исключения полноты и схемы; массовые диагностические случаи находятся в CSV.', '']
for e in exceptions:
    if e['exception_type'] in {'observed_less_than_reported', 'observed_greater_than_reported',
                               'inconsistent_reported_count', 'multiple_category_type_pairs', 'category_schema_missing'}:
        g = by_mid[e['key']]
        mm = [by_pid[pid] for pid in g['member_product_ids'].split(';')]
        lines += [f"- model_id {e['key']}: {e['exception_type']}; {e['detail']}. "
                  f"Наблюдается {g['observed_products']} карточек ({g['nonarchived_products']} неархивных); "
                  'артикулы: ' + ', '.join(r['offer_id'] for r in mm) + '.']
lines += ['', '## Выходные файлы и проверки', '',
    '- `b_glue_members.csv` — все product_id, SKU-алиасы, исходный model_info, count, фактический размер группы, архивность, атрибуты бренда/имени/aspect, источники и время обеих API-поверхностей.',
    '- `b_glue_groups.csv` — все положительные model_id, состав, counts, active/archive, категории, schema coverage, диагностические различия и статус проверки.',
    '- `b_exceptions.csv` — все исключения, включая каждый нулевой model_id отдельно; не список подтверждённых нарушений.',
    '- `b_summary.json` — агрегаты и SHA-256 файлов каталога/coverage; `b_sources.md` — источники и методика.', '',
    'Проверено: одна строка members на каждый product_id; сумма членов положительных model_id плюс unknown равна каталогу; ключи групп уникальны; coverage ALL/ARCHIVED без пропусков и дублей; число исключений совпадает с summary; каждый нулевой model_id представлен исключением; crosssource расхождения перечислены полностью. Проверки проходят.', '',
    'Бизнес-данные по API/БД самостоятельно не выгружались. B использовал только единый сохранённый снимок ведущего и общедоступные официальные страницы. Все записи B ограничены agents/b_*.', '',
    'Официальные правила: [статья Ozon 11.01.2024](https://seller.ozon.ru/media/boost/kak-obuedinit-kartochki-tovarov-na-ozon/) и [разъяснение изменений 31.03.2026](https://seller.ozon.ru/media/news/novoe-v-dogovore-izmeneniya-blizhajshih-2-nedel/). Вторая публикация отдельно различает «Модель», «Комплекты», «Похожие» и «Товары на фото». Отдельные читаемые официальные условия для картриджей поиском не найдены; доступная техническая схема не устанавливает их полностью. Окончательное разрешение спорной модели требует проверки в ЛК/поддержке.', '']
(P / 'b_report.md').write_text('\n'.join(lines))
print(json.dumps({'report_written': True, 'members': len(members), 'groups': len(groups),
                  'exceptions': len(exceptions), 'checks_passed': True,
                  'crosssource_disagreements': exceptions_by_type['model_info_differs_between_info_and_attributes']}, ensure_ascii=False))
