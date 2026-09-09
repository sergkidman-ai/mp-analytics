# поток: prc
# -*- coding: utf-8 -*-
"""
Разбор писем «Необработанные товары МС» — то, что внешний загрузчик не смог сопоставить.

    ./venv/bin/python -m prices.unprocessed --supplier bulat            # проба, в БД не пишет
    ./venv/bin/python -m prices.unprocessed --supplier bulat --apply    # писать в «Новинки»
    ./venv/bin/python -m prices.unprocessed --supplier bulat --file p.txt

Зачем отдельный вход. `prices.run` работает там, где прайс приходит НАМ (профиль в
`prices/profiles.py`). У Булата, ВТТ, Рамис и Блоссома прайса нет вовсе: их грузит внешний
загрузчик по API и присылает письмом только список несопоставленного — по файлу на поставщика,
несколько раз в день, в папку «Прайсы Поставщиков|Необработанные товары МС». В файле от товара
есть один артикул, цена и количество; ни наименования, ни `msId`. Имя добираем у поставщика
(`bulat_site`), дальше — та же дорога, что у всех: ЧС, правила новинок, `catalog.sync`.

В МойСклад отсюда НЕ пишется ничего: строки ложатся во вкладку «Новинки», карточки заводит
человек.

Разобранное письмо помечается в почте ПРОЧИТАННЫМ (09.09.2026) — и только после того, как
строки записаны, то есть при `--apply`. В папке сразу видно, какие файлы уже разобраны, а
какие ждут; проба и разбор файла с диска флаг не ставят.
"""
import argparse
import csv
import io
import sys
import datetime as dt
from pathlib import Path

from . import blacklist, bulat_site, catalog, novelty

FOLDER = "Прайсы Поставщиков|Необработанные товары МС"
RAW_DIR = Path(__file__).resolve().parent.parent / "mail_unprocessed"

# supplier_key -> (имя файла во вложении, чем добираем наименование)
# Резолвер нужен только там, где в письме пустая колонка `name`: у Булата внешний загрузчик
# отдаёт голый артикул, и имя приходится добирать с сайта поставщика. Солюшнс принт МСК шлёт
# наименование прямо в файле (11/11 строк письма 27.08), поэтому резолвера у него нет —
# `None` значит «имя берём из письма как есть».
SUPPLIERS = {
    "bulat": (r"^bulat\.txt$", bulat_site.resolve),
    "s_print_msk": (r"^s_print_msk\.txt$", None),
    # Рапид (SuperFine, ООО «КВК ТРЕЙД» + та же организация «OEM») шлёт письмо 5 раз в сутки,
    # и обычно оно пустое: за август 116 писем и всего 44 разных артикула. Имя приходит в
    # файле (41/41 на разборе 28.08), поэтому резолвера нет. Разбор 28.08 показал, что все 44
    # артикула УЖЕ имеют живую карточку МС с точным совпадением — новинок здесь пока не было
    # ни одной, и строки означают промах внешнего загрузчика, а не новый товар
    # (`docs/reports/prc_rapid_unprocessed_2026-08-28.md`).
    "rapid": (r"^rapid\.txt$", None),
    # Тонероптторг (Изипринт) — тоже две организации в МС («Изипринт» и «Изипринт T2»,
    # суффиксы кода `ep` и `t2`), письмо 5 раз в сутки. Имя в файле есть. В отличие от Рапида
    # хвост здесь ПОСТОЯННЫЙ: за август 10 артикулов, каждый в 19–50 письмах подряд, и у 7 из
    # них карточки в МС нет вовсе (6 чипов Kyocera/Samsung по 8.77 ₽ и картридж IE-T1115).
    "easy_print": (r"^easy_print\.txt$", None),
    # Профилайн — группа юрлиц «ООО КОМПАНИЯ ПРОФИЛАЙН» (закрыто) + «ООО КОМПАНИЯ РМ»
    # (живое). Везёт три бренда, и бренд читается из АРТИКУЛА однозначно: `GP…` ГалаПринт,
    # `PL…` ProfiLine, `CG…` ColorGraf (см. `ms_import.CODE_SUFFIX`). Имя в письме есть —
    # 47/47 строк августа, резолвер не нужен.
    "profiline": (r"^profiline\.txt$", None),
    # ВТТ («ООО КПД (ВТТ)») — два бренда, но артикул у них ОБЩИЙ числовой (`220095936`),
    # бренд стоит только в наименовании: Hi-Black (11 строк августа) и NetProduct (5).
    # Поэтому аббревиатура кода берётся из имени — `ms_import.NAME_SUFFIX`, как у Булата.
    "vtt": (r"^vtt\.txt$", None),
    # Солюшнс принт СПб ЗАКРЫТ 09.09.2026 (решение Сергея): прайс этого юрлица мы больше не
    # грузим и несопоставленное по нему не разбираем. Ключ оставлен в комментарии, а не в
    # списке, чтобы разбор нельзя было запустить по привычке; последняя загрузка 29.08.2026,
    # последнее письмо 20.08.2026, все письма папки помечены прочитанными.
    #   "s_print_spb": (r"^s_print_spb\.txt$", None),   # было: юрлицо той же марки, что МСК
    # Рамис — письмо приходит наравне со всеми (117 раз за август), но НИ РАЗУ не было ни
    # одной строки: внешний загрузчик сопоставляет всё. Карточек этого контрагента в МС нет
    # вовсе (0 живых, 0 в архиве), поэтому аббревиатуры кода у него нет и придумывать её
    # нельзя — первая же реальная строка упрётся в понятную ошибку `suffix()`, и код выберет
    # человек. Разбор письма при этом работает: строка попадёт в «Новинки» и будет видна.
    "ramis": (r"^ramis\.txt$", None),
}


def read_rows(text):
    """`;`-CSV письма -> строки прайса. Пустые sku и служебные хвосты отбрасываем."""
    out = []
    for rec in csv.DictReader(io.StringIO(text), delimiter=";"):
        art = (rec.get("sku") or "").strip()
        if not art:
            continue
        try:
            price = float((rec.get("price") or "0").replace(",", "."))
        except ValueError:
            price = 0.0
        out.append({"article": art, "name": (rec.get("name") or "").strip(), "price": price,
                    "qty": (rec.get("quantity") or "").strip(),
                    # Строки письма — по определению ненайденные: у всех пустой msId.
                    # Причина нужна `blacklist.mark` и `novelty.screen`, они смотрят на неё.
                    "reason": "not_found"})
    return out


def source(supplier_key, file_path):
    """(текст файла, откуда взят, UID письма). UID нужен, чтобы пометить письмо прочитанным.

    Метим не здесь, а в конце разбора (`main`): прочитанное письмо означает «файл разобран,
    строки лежат в «Новинках»», и ставить флаг до записи в БД нельзя — упавший разбор оставил
    бы человека без единственного признака, что к письму надо вернуться. С диска (`--file`)
    метить нечего, UID пустой.
    """
    if file_path:
        return Path(file_path).read_text(encoding="utf-8"), str(file_path), None
    from .mailbox import fetch_latest_price
    pattern, _ = SUPPLIERS[supplier_key][0], None
    letter = fetch_latest_price(FOLDER, extensions=(".txt",), pattern=pattern)
    if not letter:
        raise SystemExit(f"в папке {FOLDER!r} нет письма с файлом по ключу {supplier_key}")
    RAW_DIR.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    path = RAW_DIR / f"{stamp}_{letter['filename']}"
    path.write_bytes(letter["content"])          # исходник кладём как есть и больше не трогаем
    return (letter["content"].decode("utf-8", "replace"),
            f"{letter['date']} → {path.name}", letter.get("imap_uid"))


def seen(imap_uid, apply_):
    """Пометить разобранное письмо прочитанным. Только после записи и только с `--apply`.

    Выборка писем идёт по ALL, а не по UNSEEN, поэтому флаг ни на что в коде не влияет —
    это сигнал человеку: в почте сразу видно, какие файлы несопоставленного уже разобраны,
    а какие ждут. Проба (`--apply` не задан) ничего не пишет и права метить не имеет.
    """
    if not imap_uid or not apply_:
        return
    from .mailbox import mark_seen
    ok = mark_seen(FOLDER, imap_uid)
    print("письмо помечено прочитанным" if ok else
          "письмо пометить прочитанным не удалось (почта — не рабочий инструмент, разбор цел)")


SOURCE_KIND = "unprocessed"


def journal_load(supplier_key, source_file, rows):
    """Снимок письма в `prc_price_load` / `prc_price_row` — иначе у строки нет остатка.

    Вкладка «Новинки» берёт остаток ТОЛЬКО из последней удачной загрузки поставщика
    (`catalog.NOVELTY_STOCK`), а неразобранную строку без остатка не показывает вовсе
    (`catalog.IN_STOCK`): заводить карточку не подо что. У поставщика без профиля загрузок
    нет ни одной, поэтому письмо и есть его загрузка — количество в нём настоящее, из
    внешнего загрузчика. Снимок даёт и выбытие: позиция, пропавшая из свежего письма,
    остаток теряет и уходит из работы сама.

    Помечаем `source_kind='unprocessed'` и не даём подменить настоящий прайс: если у
    поставщика есть загрузки другого рода, письмо-огрызок не смеет стать «последней».
    """
    from core.db import query
    from . import journal
    kinds = {r["k"] for r in query(
        "SELECT DISTINCT coalesce(source_kind, '') k FROM prc_price_load"
        "  WHERE supplier_key = %s AND status = 'ok'", (supplier_key,))}
    alien = kinds - {SOURCE_KIND}
    assert not alien, (f"{supplier_key}: есть настоящие загрузки прайса ({sorted(alien)}) — "
                       "письмо несопоставленного не должно их подменять")
    moment = dt.datetime.now().astimezone()
    snap = [{"row": i, "article": r["article"], "name": r["name"] or None,
             "stock_raw": r["qty"], "qty": r["qty"], "price_raw": r["price"],
             "ms_name": None, "reason": r["reason"]}
            for i, r in enumerate(rows, 1)]
    return journal.save({
        "supplier_key": supplier_key, "load_date": moment.date(), "moment": moment,
        "source_file": source_file, "source_kind": SOURCE_KIND, "currency": "RUB",
        "rate": None, "rate_date": None,
        "rows_total": len(rows), "rows_loaded": 0, "rows_skipped": len(rows),
        "docs": 0, "stale_docs": 0, "card_updates": 0,
        "sum_rub": None, "dry_run": False, "status": "ok", "error": None,
    }, [], snap)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--supplier", required=True, choices=sorted(SUPPLIERS))
    ap.add_argument("--file", help="разобрать файл с диска вместо письма")
    ap.add_argument("--apply", action="store_true", help="писать во вкладку «Новинки»")
    ap.add_argument("--no-site", action="store_true",
                    help="имена только из кэша, на сайт поставщика не ходить")
    a = ap.parse_args(argv)

    text, where, imap_uid = source(a.supplier, a.file)
    rows = read_rows(text)
    print(f"источник: {where}")
    print(f"строк в файле: {len(rows)}")
    if not rows:
        # Пустое письмо — тоже разобранное: у Рапида и Изипринта таких пять в сутки, и без
        # пометки они копятся непрочитанными вперемешку с теми, где действительно есть работа.
        seen(imap_uid, a.apply)
        return 0

    resolver = SUPPLIERS[a.supplier][1]
    if resolver is not None:
        arts = [r["article"] for r in rows if not r["name"]]
        if a.no_site:
            names = bulat_site.cached(arts)
        else:
            names = resolver(arts, progress=lambda i, n: print(f"  … имена {i}/{n}"))
        for r in rows:
            if not r["name"]:
                r["name"] = names.get(r["article"], "")
    named = [r for r in rows if r["name"]]
    blind = [r for r in rows if not r["name"]]
    print(f"наименование найдено: {len(named)} · не найдено: {len(blind)}")

    # Дальше только те, у кого есть имя: без него ни ЧС по бренду, ни правила новинок,
    # ни шесть признаков не работают — такая строка притворилась бы разобранной.
    named, black_hits = blacklist.mark(named, blacklist.load_set(), supplier_key=a.supplier)
    named, stats, watch = novelty.screen(named, named)
    left = [r for r in named if r["reason"] in ("not_found", "ambiguous")]
    print(f"в ЧС: {black_hits} · отсеяно правилами новинок: {sum(stats.values())}"
          + (" (" + "; ".join(f"{novelty.NOVELTY_REASONS[k][0]} — {v}"
                              for k, v in sorted(stats.items(), key=lambda kv: -kv[1])) + ")"
             if stats else ""))
    print(f"идёт в «Новинки»: {len(left)}")

    if not a.apply:
        print("\n[проба] в БД не писал; повтори с --apply")
        return 0
    load_id = journal_load(a.supplier, where, rows)
    matched, auto = catalog.sync(left, a.supplier, None, None)
    print(f"\nжурнал загрузки: prc_price_load #{load_id} (остаток строкам берётся отсюда)")
    print(f"записано во вкладку «Новинки»: {len(left)}")
    print(f"  закрылось само по артикулу (exists): {auto}")
    # Строка могла быть сопоставлена человеком ещё вчера, а карточка с его артикулом уже
    # лежит в МС: создавать нечего, но сама она не закроется — автозакрытие трогает только
    # `pending`. Подметаем после каждой загрузки, иначе такие строки копятся в «Сопоставлено».
    from . import ms_import
    swept, foreign = ms_import.sweep_matched(a.supplier)
    print(f"  подметено в «Сопоставлено» (карточка уже с этим артикулом): {swept}")
    print(f"  получило кандидатов на выбор:        {matched - auto}")
    print(f"  без вариантов (настоящая новинка):   {len(left) - matched}")
    seen(imap_uid, a.apply)
    if blind:
        print(f"\nбез наименования, в разбор не пошли ({len(blind)}): "
              + ", ".join(r["article"] for r in blind[:6])
              + (" …" if len(blind) > 6 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
