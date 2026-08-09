# поток: prc
# -*- coding: utf-8 -*-
"""
Сверка новинок поставщика с НАШИМ каталогом по признакам.

Зачем отдельно от загрузки: матчинг прайса в МС строгий — по артикулу, и это правильно
(оприходование не имеет права ошибиться товаром). Но «не нашлось по артикулу» ещё не значит
«у нас такого нет»: тот же C-EXV65 голубой 11000 стр. лежит у нас под кодом 6058 с суффиксом
поставщика, а артикул у каждого поставщика свой. Здесь мы ищем именно ТОВАР, а не строку:
разбираем название на признаки и сравниваем признаки.

Признаков шесть, и результат сверки по каждому сохраняется отдельно:
  модель  — общий код в названии/артикуле (C-EXV65, TK-8335, ...);
  тип     — картридж / флакон тонера / чернила / драм: разный товар на один принтер;
  бренд   — бренд ПРИНТЕРА, множествами (картридж бывает и к HP, и к Canon); ловит
            случайное совпадение кода модели («bizhub C250i» и «Canon iR C250i»);
  цвет    — строго равен;
  ресурс  — расхождение до 25% (поставщики округляют и меряют по-разному);
  чип     — не противоречит (у Кактуса все картриджи с чипом, в наших названиях чип часто
            вообще не упомянут — это «не знаем», а не «без чипа»).

У каждого признака три исхода: подтвердился (True), противоречит (False) и молчит (None —
в названии карточки признака нет). Молчание не считается ни совпадением, ни отказом.

Каталог берём из локальной витрины `ms_product`, в МойСклад не ходим.
"""
import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

from core.db import execute, query

from . import features as F
from .novelty import kind, volume

# Код нашей карточки = число (товар) + суффикс поставщика: 6058sk, 6058sf, 6058gp.
# Товар — это число; суффикс лишь говорит, чьей марки коробка на полке.
CODE_NUM_RE = re.compile(r"^(\d+)")

RESOURCE_TOLERANCE = 0.25
COMMON_CODE_LIMIT = 400      # код, встречающийся чаще, кандидатов не порождает (шум вроде «A4»)
TOP_VARIANTS = 5             # сколько вариантов показывать по одной строке прайса


def load_catalog():
    """Живые карточки каталога с разобранными признаками."""
    rows = query("""
        select ms_id, coalesce(code, '') as code, name, coalesce(article, '') as article
          from ms_product
         where not archived and name is not null
    """)
    out = []
    for row in rows:
        item = dict(row)
        item["num"] = (CODE_NUM_RE.match(item["code"]) or [None])[0]
        item["kind"] = kind(item["name"])
        item.update(F.parse(item["name"], item["article"]))
        out.append(item)
    return out


def build_index(catalog):
    """код -> карточки. Слишком частые коды выкидываем: кандидатов от них миллион, толку ноль."""
    index = defaultdict(list)
    for item in catalog:
        for code in item["codes"]:
            index[code].append(item)
    return {code: items for code, items in index.items() if len(items) <= COMMON_CODE_LIMIT}


def art_key(text):
    """Артикул -> ключ сравнения: регистр и разделители не значат ничего.

    «CS-TN-217» в прайсе и «CS-TN217» в карточке — один и тот же артикул, просто заведён
    разными руками. Тем же ключом сравнивает счета поставщиков (invoice_to_po.py:432).
    """
    return re.sub(r"[^0-9A-ZА-ЯЁ]", "", str(text or "").upper())


def build_article_index(catalog):
    """ключ артикула -> карточки. Пустые артикулы не индексируем: они склеили бы всё подряд."""
    index = defaultdict(list)
    for item in catalog:
        key = art_key(item["article"])
        if key:
            index[key].append(item)
    return index


def supplier_articles(row, article_re):
    """Артикулы поставщика в строке: колонка прайса + вынесенные в наименование.

    В наименование поставщик пишет свой код («Картридж лазерный Cactus CS-TN217 TN-217…»),
    и в карточку МС попадает то одна форма, то другая — ищем по обеим.
    """
    out = {art_key(row.get("article"))}
    if article_re:
        out |= {art_key(m.group(0)) for m in re.finditer(article_re, str(row.get("name") or ""))}
    return {key for key in out if len(key) >= 5}      # короткий огрызок совпадёт со всем


def by_article(row, art_index, article_re):
    """Карточки с тем же артикулом поставщика. Совпадение артикула сильнее любых признаков:
    это буквально тот же товар, даже если название описывает его другими словами.

    Признаки всё равно считаем и показываем: артикул остаётся главным, но если при
    совпавшем артикуле расходится бренд или ресурс — строку нельзя закрывать молча
    (см. `save`), скорее всего название одной из карточек заполнено неверно.
    """
    out, seen = [], set()
    for key in sorted(supplier_articles(row, article_re)):
        for item in art_index.get(key, ()):
            if item["ms_id"] in seen:
                continue
            seen.add(item["ms_id"])
            hit = {"item": item, "code": item["article"], "shared": 0, "rarity": 0,
                   "confirmed": 3, "by_article": True}
            hit.update(compare(row, item))
            out.append(hit)
    return out


def close(want, got):
    """Числа сходятся в пределах допуска (или одного из них нет — тогда не спорим)."""
    if not want or not got:
        return None                                # «не знаем» — не совпадение и не отказ
    return abs(want - got) <= RESOURCE_TOLERANCE * max(want, got)


def measure(item):
    """Чем меряется товар: картридж — ресурсом печати, флакон — объёмом.

    У тонера и чернил ресурса в названии нет и быть не может, зато есть граммы и
    миллилитры, и 100-граммовый флакон — не тот же товар, что 50-граммовый. Признак
    один и тот же по смыслу («сколько внутри»), поэтому и допуск берём общий.
    """
    if item["kind"] in ("toner", "ink"):
        return volume(item["name"])[0]
    return item["resource"]


def color_ok(want, got):
    """Цвет не противоречит.

    Молчание каталога — не отказ, но только для ЧЁРНОГО: чёрный часто не пишут вовсе, он
    подразумевается («Драм-картридж Kyocera ECOSYS P4140 DK-7310» — чёрный по определению).
    А вот если поставщик заявил голубой, а карточка цвет не называет — это почти наверняка
    другой товар: у цветных позиций цвет в названии стоит всегда.
    """
    if want and got:
        return want == got
    if want and want != "BK":
        return False
    return None


def chip_ok(want, got):
    """Чип не противоречит. None с любой стороны — молчание, а не «без чипа»."""
    if want is None or got is None:
        return None
    return want == got


def model_ok(want, got):
    """Коды модели пересекаются. Пустой набор с любой стороны — молчание."""
    if not want or not got:
        return None
    return bool(want & got)


def kind_ok(want, got):
    """Тип товара совпадает. Неопознанный тип с любой стороны — молчание."""
    if not want or not got:
        return None
    return want == got


def brand_ok(want, got):
    """Бренды принтера пересекаются.

    Сравниваем МНОЖЕСТВАМИ: в строке прайса законно стоят два бренда сразу, и достаточно
    одного общего. Бренд есть в 100% строк прайса и лишь в 72% наших карточек — остальное
    молчание (`None`), а не отказ.
    """
    if not want or not got:
        return None
    return bool(want & got)


def compare(row, item):
    """Все шесть признаков строки прайса против карточки каталога. -> флаги и счёт.

    Считаем одинаково для обоих путей поиска: и там, где карточку нашли по общему коду
    модели, и там, где совпал артикул поставщика. Раньше путь по артикулу отдавал признаки
    пустыми, и две строки TN-321 закрылись сами на карточку Brother вместо Konica Minolta —
    расходились и бренд, и ресурс в 16 раз, но увидеть это было негде.
    """
    flags = {"model_ok": model_ok(row["codes"], item["codes"]),
             "kind_ok": kind_ok(row["kind"], item["kind"]),
             "brand_ok": brand_ok(row["brand"], item["brand"]),
             "color_ok": color_ok(row["color"], item["color"]),
             "resource_ok": close(measure(row), measure(item)),
             "chip_ok": chip_ok(row["chip"], item["chip"])}
    flags["score"] = sum(1 for value in flags.values() if value)
    return flags


def candidates(row, index):
    """Карточки каталога, у которых есть общий код с этой строкой прайса."""
    hits = defaultdict(set)
    for code in row["codes"]:
        for item in index.get(code, ()):
            hits[item["ms_id"]].add(code)
    return hits


def match(row, index, by_id):
    """Варианты каталога для одной строки прайса, лучшие первыми.

    Порядок — по редкости общего кода: совпадение по C-EXV65 весомее совпадения по модели
    принтера, которая стоит у десятка разных расходников (bizhub C250i и Canon iR C250i
    вообще совпали случайно). Подтверждённость признаков — второй ключ: ресурс и чип
    в наших названиях указаны через раз, и их молчание не должно опускать точный код.

    Бренд из списка отбора выведен намеренно: наши карточки заполняли руками, и ошибка
    в названии («TN-321 для Brother») не должна ПРЯТАТЬ правильный вариант от человека.
    Конфликт по бренду лишь опускает карточку в хвост списка — то есть меняет только то,
    что предвыбрано в выпадающем списке.
    """
    out = []
    for ms_id, shared in candidates(row, index).items():
        item = by_id[ms_id]
        if row["kind"] != item["kind"]:
            continue        # флакон тонера и тонер-картридж на один принтер — разные товары
        flags = compare(row, item)
        if False in (flags["color_ok"], flags["resource_ok"], flags["chip_ok"]):
            continue
        rarest = min(len(index[c]) for c in shared)
        best_code = max(shared, key=lambda c: (len(index[c]) == rarest, len(c)))
        hit = {"item": item, "code": best_code, "shared": len(shared), "rarity": rarest,
               "confirmed": sum(1 for x in (flags["color_ok"], flags["resource_ok"],
                                            flags["chip_ok"]) if x)}
        hit.update(flags)
        out.append(hit)
    out.sort(key=lambda h: (h["brand_ok"] is False, h["rarity"], -h["confirmed"], -h["shared"]))
    return out


def read_novelties(path):
    """Файл новинок внешнего формата: name;price;quantity;msId;defective;Barcode;sku."""
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        # Режем с КОНЦА: хвост из шести полей фиксирован, а в названии поставщика
        # точка с запятой встречается («…LBP215dw; с чипом»).
        parts = (line.rsplit(";", 6) + [""] * 7)[:7]
        price = parts[1].replace(",", ".").strip()
        rows.append({"name": parts[0], "article": parts[6].strip(),
                     "price": float(price) if price else None})
    return rows


def save(rows, supplier_key, hits_by_article):
    """Разложить новинки и найденные варианты по таблицам. -> сколько закрылось само.

    Решение человека не трогаем: строка приходит с каждым прогоном прайса, а разбирается
    один раз. Обновляем только описание строки и подсказки — сами варианты пересобираются
    заново, они лишь результат сегодняшней сверки.

    Точное совпадение артикула поставщика закрывает строку САМО (`decision = 'exists'`):
    товар в МС есть, просто оприходование его не нашло, и человеку разбирать тут нечего.
    Решение обратимо — строка видна во вкладке под фильтром «уже в МС», кнопка «вернуть
    в работу» на месте: на случай, если артикул случайно совпал с чужой карточкой.

    Предохранитель: при совпавшем артикуле, но конфликте по БРЕНДУ принтера или РЕСУРСУ
    строка не закрывается — остаётся человеку. Такое расхождение означает, что название
    одной из двух карточек заполнено неверно, и молча принимать её нельзя. Статус `exists`
    ставится ТОЛЬКО автоматом (человек ставит matched/new/partial/skip), поэтому обратный
    ход однозначен: уже закрытая строка с конфликтом возвращается в работу.
    """
    auto = 0
    for row in rows:
        found = query("""
            INSERT INTO prc_novelty (supplier_key, article_norm, article, name, kind,
                                     color, measure, chip, price_rub, brand)
            VALUES (%s, upper(btrim(%s)), %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (supplier_key, article_norm) DO UPDATE
               SET name = excluded.name, kind = excluded.kind, color = excluded.color,
                   measure = excluded.measure, chip = excluded.chip,
                   price_rub = excluded.price_rub, brand = excluded.brand, last_seen = now()
            RETURNING id
        """, (supplier_key, row["article"], row["article"], row["name"], row["kind"],
              row["color"], measure(row), row["chip"], row["price"],
              F.brand_text(row["brand"])))
        novelty_id = found[0]["id"]
        execute("DELETE FROM prc_novelty_candidate WHERE novelty_id = %s", (novelty_id,))
        hits = hits_by_article.get(row["article"], ())
        for rank, hit in enumerate(hits, start=1):
            item = hit["item"]
            execute("""
                INSERT INTO prc_novelty_candidate (novelty_id, rank, ms_id, ms_code, ms_name,
                                                   color, measure, chip, shared_code, verdict,
                                                   brand, kind, model_ok, kind_ok, brand_ok,
                                                   color_ok, resource_ok, chip_ok, score)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (novelty_id, rank, item["ms_id"], item["code"], item["name"], item["color"],
                  measure(item), item["chip"], hit["code"], verdict(hit),
                  F.brand_text(item["brand"]), item["kind"], hit["model_ok"], hit["kind_ok"],
                  hit["brand_ok"], hit["color_ok"], hit["resource_ok"], hit["chip_ok"],
                  hit["score"]))
        if hits and hits[0].get("by_article"):
            hit, item = hits[0], hits[0]["item"]
            if False in (hit["brand_ok"], hit["resource_ok"]):
                execute("""
                    UPDATE prc_novelty
                       SET decision = 'pending', ms_id = null, ms_code = null, ms_name = null,
                           decided_at = null
                     WHERE id = %s AND decision = 'exists'
                """, (novelty_id,))
                continue
            auto += execute("""
                UPDATE prc_novelty
                   SET decision = 'exists', ms_id = %s, ms_code = %s, ms_name = %s,
                       decided_at = now()
                 WHERE id = %s AND decision = 'pending'
            """, (item["ms_id"], item["code"], item["name"], novelty_id)) or 0
    return auto


FEATURE_NAMES = {"model_ok": "модель", "kind_ok": "тип", "brand_ok": "бренд",
                 "color_ok": "цвет", "resource_ok": "ресурс/объём", "chip_ok": "чип"}


def verdict(hit):
    """Словами: что противоречит, что подтвердилось, а что в каталоге не написано.

    Конфликты идут первыми и всегда: по пути «совпал артикул» именно они — единственный
    сигнал человеку, что за одинаковым артикулом стоят разные товары.
    """
    bad = [name for key, name in FEATURE_NAMES.items() if hit.get(key) is False]
    silent = [name for key, name in FEATURE_NAMES.items() if hit.get(key) is None]
    notes = [f"НЕ СОВПАЛО: {', '.join(bad)}"] if bad else []
    if hit.get("by_article"):
        notes.append(f"совпал артикул поставщика ({hit['code']})")
        return "; ".join(notes)
    if silent:
        notes.append(f"не указано в каталоге: {', '.join(silent)}")
    return "; ".join(notes) if notes else "совпало по всем признакам"


def analyze(rows, default_chip="chip", article_re=None):
    """Разобрать строки на признаки и подобрать каждой варианты каталога.

    Возвращает {артикул: [варианты]} — по одному представителю на товар (карточек одного
    товара у нас столько, сколько поставщиков, показывать их все смысла нет). Первыми идут
    совпадения по артикулу поставщика: МойСклад при создании оприходования иногда не находит
    товар, который в базе есть, и строка приезжает в новинки зря.
    """
    catalog = load_catalog()
    by_id = {item["ms_id"]: item for item in catalog}
    index = build_index(catalog)
    art_index = build_article_index(catalog)
    hits_by_article = {}
    for row in rows:
        row["kind"] = kind(row["name"])
        row.update(F.parse(row["name"], row["article"]))
        if row["chip"] is None:
            row["chip"] = default_chip
        seen, shown = set(), []
        for hit in by_article(row, art_index, article_re) + match(row, index, by_id):
            key = hit["item"]["num"] or hit["item"]["ms_id"]
            if key in seen:
                continue
            seen.add(key)
            shown.append(hit)
            if len(shown) >= TOP_VARIANTS:
                break
        hits_by_article[row["article"]] = shown
    return hits_by_article


def sync(rows, supplier_key, default_chip=None, article_re=None):
    """Сверить новинки прогона с каталогом и положить во вкладку «Новинки».

    -> (сколько строк получили варианты, сколько закрылось само по артикулу).
    Строки — {name, article, price}; решение человека по уже разобранным строкам не трогаем.
    """
    if not rows:
        return 0, 0
    hits = analyze(rows, default_chip, article_re)
    auto = save(rows, supplier_key, hits)
    return sum(1 for r in rows if hits.get(r["article"])), auto


def pending_rows(supplier_key=None):
    """Строки, которые ждут решения человека. Пересобирать подсказки им можно и нужно."""
    sql = ("select supplier_key, article, name, price_rub price from prc_novelty "
           "where decision = 'pending'")
    params = ()
    if supplier_key:
        sql += " and supplier_key = %s"
        params = (supplier_key,)
    return [dict(r) for r in query(sql + " order by supplier_key, article", params)]


def all_rows(supplier_key=None):
    """ВСЕ строки новинок, включая разобранные. Для пересборки признаков задним числом.

    Решения человека это не трогает (`save` их не переписывает) — пересобираются только
    признаки, варианты и флаги сверки. Нужно после правки правил матчинга: иначе новые
    признаки появились бы лишь у строк из будущих прайсов, а разобранные так и остались бы
    со старой сверкой — и уже сделанная ошибка не всплыла бы никогда.
    """
    sql = "select supplier_key, article, name, price_rub price from prc_novelty"
    params = ()
    if supplier_key:
        sql += " where supplier_key = %s"
        params = (supplier_key,)
    return [dict(r) for r in query(sql + " order by supplier_key, article", params)]


def rematch(supplier_key=None, include_decided=False):
    """Пересобрать подсказки для висящих строк — по правилам поставщика из профиля.

    Отдельно от прогона прайса: правила матчинга правятся чаще, чем приходят прайсы, и
    ждать нового письма, чтобы человек увидел исправленные подсказки, незачем.

    `include_decided` берёт и разобранные строки тоже.
    """
    from .profiles import get_profile
    stats = {}
    rows = all_rows(supplier_key) if include_decided else pending_rows(supplier_key)
    for key in sorted({r["supplier_key"] for r in rows}):
        profile = get_profile(key)
        mine = [r for r in rows if r["supplier_key"] == key]
        found, auto = sync(mine, key, profile.default_chip, profile.article_re)
        stats[key] = (found, auto, len(mine))
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser(description="Сверка новинок поставщика с каталогом МС")
    ap.add_argument("--file", help="файл новинок (*_unmatched.txt)")
    ap.add_argument("--rematch", action="store_true",
                    help="пересобрать подсказки для висящих строк во вкладке «Новинки»")
    ap.add_argument("--recheck", action="store_true",
                    help="то же, но по ВСЕМ строкам, включая разобранные "
                         "(решения человека не трогает)")
    ap.add_argument("--supplier-chip", default="chip",
                    help="чип по умолчанию для поставщика (chip|chip_free|nochip|unknown)")
    ap.add_argument("--out", help="куда писать отчёт (CSV)")
    ap.add_argument("--save", action="store_true",
                    help="положить новинки и варианты в БД (вкладка «Новинки» на дашборде)")
    ap.add_argument("--supplier", help="ключ поставщика для БД (по умолчанию — из имени файла)")
    args = ap.parse_args(argv)

    if args.rematch or args.recheck:
        what = "строк" if args.recheck else "висящих строк"
        for key, (found, auto, total) in rematch(args.supplier, args.recheck).items():
            print(f"{key}: нашли пару {found} из {total} {what}; "
                  f"закрыто по артикулу (уже в МС) — {auto}")
        return 0
    if not args.file:
        ap.error("нужен --file, --rematch или --recheck")

    default_chip = None if args.supplier_chip == "unknown" else args.supplier_chip
    article_re = None
    if args.supplier:
        from .profiles import get_profile
        article_re = get_profile(args.supplier).article_re
    hits_by_article = analyze(rows := read_novelties(args.file), default_chip, article_re)

    out_path = Path(args.out) if args.out else Path(args.file).with_name(
        Path(args.file).name.replace("_unmatched.txt", "_match.csv"))
    found = full = 0
    with out_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(["артикул", "наименование поставщика", "цвет", "ресурс/объём", "чип",
                         "код МС", "наименование МС", "цвет МС", "ресурс/объём МС", "чип МС",
                         "общий код", "вердикт"])
        for row in rows:
            shown = hits_by_article[row["article"]]
            if shown:
                found += 1
                if shown[0]["confirmed"] == 3:      # цвет, ресурс и чип — все подтверждены
                    full += 1
            base = [row["article"], row["name"], F.COLOR_NAMES.get(row["color"], ""),
                    measure(row) or "", F.CHIP_NAMES[row["chip"]]]
            if not shown:
                writer.writerow(base + ["", "НЕ НАЙДЕНО В КАТАЛОГЕ", "", "", "", "", ""])
                continue
            for hit in shown:
                item = hit["item"]
                writer.writerow(base + [item["code"], item["name"],
                                        F.COLOR_NAMES.get(item["color"], ""),
                                        measure(item) or "", F.CHIP_NAMES[item["chip"]],
                                        hit["code"], verdict(hit)])
    if args.save:
        supplier = args.supplier or Path(args.file).name.split("_")[0]
        save(rows, supplier, hits_by_article)
        print(f"в БД: prc_novelty, поставщик «{supplier}» — {len(rows)} строк")
    print(f"строк новинок: {len(rows)}")
    print(f"нашлись в каталоге: {found} (из них по всем четырём признакам: {full})")
    print(f"не нашлись: {len(rows) - found}")
    print(f"отчёт: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
