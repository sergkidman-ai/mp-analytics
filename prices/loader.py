# поток: prc
# -*- coding: utf-8 -*-
"""
Загрузка прайса поставщика в МойСклад: Оприходование на «Удаленный склад».

Порядок и правила согласованы 05.08.2026 и восстановлены из ручной загрузки Колортека:

  * матчинг СТРОГО «артикул поставщика == поле Артикул в МС», ничего не угадываем;
  * количество — из прайса (текстовые диапазоны разворачиваются таблицей профиля);
  * цена — прайс × курс ЦБ на дату загрузки × надбавка, округление до копейки;
  * не грузим: категория прайса нам не нужна (профиль `keep_categories` — отсев ДО матчинга),
    нет товара в МС, товар архивный, «брак» в наименовании МС, нет цены, нет остатка,
    неоднозначный артикул;
  * документы бьются по 100 позиций, имя «<группа>_<дата>_p<N>», описание — группа;
  * СНАЧАЛА создаём новые документы, ПОТОМ удаляем прошлые — чтобы при сбое остаток
    остался задвоенным (это видно и чинится), а не обнулился. Имя документа в МС уникально,
    поэтому прошлые сперва помечаются суффиксом «_old<ЧЧММСС>» и удаляются последним шагом;
  * удаляем только документы этой группы И только на «Удаленном складе»: у части групп
    есть документы на других складах (Кантемировская, Звездный), они не наши;
  * описание и закупочную цену карточки обновляем ТОЛЬКО если поставщик карточки входит
    в группу этого прайса — иначе последний загруженный прайс перетирал бы чужие данные.
"""
import re
import datetime as dt
from decimal import Decimal

from core import ms_api
from .cbr import effective_rate, to_kopecks
from .profiles import ORG_DIGITAL, POSITIONS_PER_DOC, STORE_REMOTE
from .supplier_group import own_ids

MSK = dt.timezone(dt.timedelta(hours=3))
# Имя документа: «<группа>_<дата>_p<N>», плюс возможный хвост «_old<время>» — так помечаются
# прошлые документы на время замены (см. apply_to_ms). Хвост в шаблоне обязателен: иначе
# документ, оставшийся от прерванного прогона, следующий прогон не увидит и не уберёт.
DOC_NAME_RE = "^{key}_\\d{{4}}-\\d{{2}}-\\d{{2}}_p\\d+(_old\\d+)*$"
# Наш внешний код — ровно 4 цифры (правило 24). Всё остальное в поле — автогенерация МС.
OUR_CODE_RE = re.compile(r"^\d{4}$")

SKIP_REASONS = {
    "not_found": "нет товара в МС по артикулу",
    "archived": "карточка в архиве, живой родни того же кода нет — решает человек",
    "defective": "«брак» в наименовании МС",
    "no_price": "нет цены в прайсе",
    "no_stock": "нет остатка / формулировка не разобрана",
    "ambiguous": "артикул в МС неоднозначен",
    "foreign": "карточка МС от другого поставщика — заводим свою",
    "not_stockable": "тип позиции не приходуется на склад",
    "duplicate": "артикул повторяется в прайсе, остатки равны — цены разные",
    "duplicate_smaller": "артикул повторяется в прайсе, взята строка с бо́льшим остатком",
    "price_absurd": "цена вне разумного коридора (проверка аномалий)",
    "category_off": "категория прайса нам не нужна",
    "blacklisted": "артикул в чёрном списке (забраковали раньше)",
    # правило снято 09.08.2026, подписи нужны для строк прошлых прогонов
    "bulk_toner": "тонер больше 150 г — правило снято 09.08",
    "bulk_ink": "чернила больше 150 мл — правило снято 09.08",
    "cleaning": "промывочная жидкость — не наш товар",
    "refillable": "картридж перезаправляемый — не наш товар",
    "set_incomplete": "неполный цветовой комплект — ждём остальные цвета",
}


def drop_duplicates(rows):
    """Один артикул — одна строка: точный повтор схлопываем, разные остатки — берём больший.

    Одиссей присылает часть позиций дважды (`ATR-PG440XL`: 65 шт и 15 шт). Складывать остатки
    нельзя — это один товар в двух разделах прайса, сумма задвоит склад. Решение Сергея
    11.08.2026: «в прайсе у поставщика так идёт, берём позицию с большим остатком» — вместе с
    её ценой, строка берётся целиком. Ручная загрузка делает то же самое.

    Остаётся один нерешаемый случай: остатки равны, а цены разные — большего остатка нет,
    выбирать монеткой мы не будем, обе строки уходят человеку в отчёт.
    """
    seen, out, dupes = {}, [], []
    for row in rows:
        key = row["article"]
        if key not in seen:
            seen[key] = row
            out.append(row)
            continue
        first = seen[key]
        if (row["qty"], row["price_raw"]) == (first["qty"], first["price_raw"]):
            continue                                  # точный повтор — молча схлопываем
        best, worse = ((row, first) if (row["qty"] or 0) > (first["qty"] or 0)
                       else (first, row) if (first["qty"] or 0) > (row["qty"] or 0) else (None, None))
        if best is not None:
            if first in out:
                out[out.index(first)] = best
            seen[key] = best
            dupes.append({**worse, "reason": "duplicate_smaller"})
            continue
        if first in out:                              # остатки равны, цены разные — решает человек
            out.remove(first)
            dupes.append({**first, "reason": "duplicate"})
        dupes.append({**row, "reason": "duplicate"})
    return out, dupes


def now_msk():
    return dt.datetime.now(MSK)


def lookup_by_article(articles, batch=80):
    """{артикул -> [карточки МС]}. Батчами: повтор одного поля в filter МС трактует как ИЛИ."""
    found = {}
    clean = [a for a in articles if ";" not in a and "=" not in a]
    odd = [a for a in articles if a not in clean]
    for i in range(0, len(clean), batch):
        chunk = clean[i:i + batch]
        rows = ms_api.get("/entity/assortment", {
            "limit": 1000,
            "filter": [f"article={a}" for a in chunk],
        }).get("rows", [])
        for row in rows:
            found.setdefault((row.get("article") or "").strip(), []).append(row)
    for article in odd:                       # артикулы со служебными символами — поштучно
        rows = ms_api.get("/entity/assortment", {"limit": 10, "filter": f"article={article}"}).get("rows", [])
        for row in rows:
            found.setdefault((row.get("article") or "").strip(), []).append(row)
    return found


def lookup_archived(articles, batch=80):
    """{артикул -> [АРХИВНЫЕ карточки МС]}. Отдельным запросом, потому что иначе их не видно.

    Грабли МС, на которых легко потерять товар: `/entity/assortment` и `/entity/product`
    архивные карточки НЕ отдают вовсе — ни по `id`, ни по `article`, ни по `code`. Без флага
    `archived=true` архивная карточка выглядит как «товара в МС нет», строка уезжает в новинки,
    и под товар, который у нас уже заведён, заводится второй. Проверено 18.08.2026:
    `article=MRV-X28179` без флага — пусто, с флагом — карточка есть.

    Спрашиваем только про артикулы, по которым живой карточки не нашлось: лишний запрос на
    каждый прайс ни к чему, а таких строк единицы.
    """
    found = {}
    clean = [a for a in articles if ";" not in a and "=" not in a]
    for i in range(0, len(clean), batch):
        chunk = clean[i:i + batch]
        rows = ms_api.get("/entity/assortment", {
            "limit": 1000,
            "filter": [f"article={a}" for a in chunk] + ["archived=true"],
        }).get("rows", [])
        for row in rows:
            found.setdefault((row.get("article") or "").strip(), []).append(row)
    return found


def live_twins(codes, batch=40):
    """{внешний код -> [живые карточки того же кода]} — родня архивной карточки.

    Карточек одного внешнего кода у нас столько, сколько поставщиков (`3223sp` в архиве,
    а `3223spb`, `3223dsk`, `3223msk` живы). Товар при этом ОДИН, и остаток поставщика
    честно ложится на живую карточку его группы — заводить новую не нужно.
    """
    out = {}
    codes = [c for c in codes if c]
    for i in range(0, len(codes), batch):
        rows = ms_api.get("/entity/assortment", {
            "limit": 1000,
            "filter": [f"externalCode={c}" for c in codes[i:i + batch]],
        }).get("rows", [])
        for row in rows:
            if row.get("archived"):
                continue
            out.setdefault((row.get("externalCode") or "").strip(), []).append(row)
    return out


def pick_card(cards, own):
    """Одна карточка из найденных по артикулу — только СВОЯ, по группе юрлиц поставщика.

    В оприходовании поставщика может лежать только его собственный товар. Артикулы у
    поставщиков пересекаются (CET, OEM-коды), поэтому карточку чужой группы не берём вовсе:
    строка уходит в новинки, и под неё заводится своя карточка (`prices/ms_import.py`).
    Раньше единственная найденная карточка бралась без проверки владельца — так в
    оприходования попадало 19 чужих позиций из 6156.

    Карточку с НЕзаполненным поставщиком отбрасывать нельзя: пустое поле чужого владельца
    не доказывает, а таких карточек в базе много (заводились до правила). Берём её.
    """
    mine = [c for c in cards if ms_api.meta_id(c, "supplier") in own]
    if len(mine) == 1:
        return mine[0], None
    if mine:
        return None, "ambiguous"
    blank = [c for c in cards if not ms_api.meta_id(c, "supplier")]
    if len(blank) == 1:
        return blank[0], None
    if blank:
        return None, "ambiguous"
    return None, "foreign"


def filter_categories(rows, profile):
    """Отсев ненужных категорий ДО матчинга.

    Прайс поставщика может быть каталогом целиком (у Феррета 46 групп — вплоть до сейфов и
    стульев). Отсеиваем сразу: не дёргаем МС лишними артикулами и, главное, не тащим чужой
    ассортимент в список новинок — иначе шаг 2 предложит завести карточку на офисный стул.
    """
    if not profile.keep_categories:
        return rows, []
    keep, dropped = [], []
    for row in rows:
        (keep if profile.keep_category(row) else dropped).append(row)
    return keep, [{**r, "reason": "category_off"} for r in dropped]


def archive_swaps(rows, cards, own):
    """Что делать с товаром, чья карточка МС ушла в архив.

    -> ({артикул -> ЖИВАЯ карточка на замену}, {артикул -> архивная карточка без замены}).

    Правило (задача Сергея 18.08.2026): архивная карточка в оприходовании недопустима —
    остаток встаёт на мёртвый товар и дальше в ТК не уходит. Но и молча выбрасывать строку
    нельзя: до этой правки архивная карточка вообще не находилась (МС её не отдаёт без флага
    `archived=true`), строка уезжала в новинки с причиной `not_found`, и под уже заведённый
    товар заводился дубль.

    Порядок: живая родня того же ВНЕШНЕГО кода и той же группы поставщика → берём её и
    приходуем на неё. Родни нет — строка не пропадает молча, а уходит в причину `archived`,
    в отчёт прогона и в уведомление PRC-бота: решение «достать из архива или завести новую»
    принимает человек, а не загрузчик.
    """
    misses = {r["article"] for r in rows if r["qty"] and not cards.get(r["article"])}
    if not misses:
        return {}, {}
    archived = {}
    for article, found in lookup_archived(misses).items():
        card, _ = pick_card(found, own)
        if card is not None:
            archived[article] = card
    if not archived:
        return {}, {}
    twins = live_twins({(c.get("externalCode") or "").strip() for c in archived.values()})
    swaps, orphans = {}, {}
    for article, card in archived.items():
        code = (card.get("externalCode") or "").strip()
        live, problem = (None, "no_code")
        if OUR_CODE_RE.match(code):
            live, problem = pick_card(twins.get(code, []), own)
        if live is not None and not problem:
            swaps[article] = live
        else:
            orphans[article] = card
    return swaps, orphans


def classify(rows, profile, rate):
    """Строки прайса -> (позиции к загрузке, пропущенные с причиной)."""
    rows, off = filter_categories(rows, profile)
    rows, skipped = drop_duplicates(rows)
    skipped += off
    cards = lookup_by_article([r["article"] for r in rows])
    own = own_ids(profile)
    swaps, archived_only = archive_swaps(rows, cards, own)
    ready = []
    for row in rows:
        # Остаток проверяем ПЕРВЫМ, ещё до поиска карточки в МС. Позиции без остатка у
        # поставщика нет: приходовать нечего, и заводить под неё карточку — тоже. Раньше
        # проверка стояла после `not_found`, и позиция без остатка, которой нет в МС,
        # попадала в новинки: у Сакуры так набралось 411 строк из 477 «неразобранных».
        if not row["qty"]:
            skipped.append({**row, "reason": "no_stock"})
            continue
        found = cards.get(row["article"], [])
        if not found:
            # Архивная карточка не «не найдена»: товар у нас заведён. Отправить такую строку
            # в новинки — значит завести второй такой же товар, поэтому разбираем отдельно.
            if row["article"] in swaps:
                found = [swaps[row["article"]]]
                row = {**row, "archived_swap": archived_only.get(row["article"])
                       or swaps[row["article"]].get("externalCode")}
            elif row["article"] in archived_only:
                skipped.append({**row, "reason": "archived",
                                "ms_name": archived_only[row["article"]].get("name") or ""})
                continue
            else:
                skipped.append({**row, "reason": "not_found"})
                continue
        card, problem = pick_card(found, own)
        if problem:
            skipped.append({**row, "reason": problem, "ms_candidates": len(found),
                            "ms_name": (found[0].get("name") or "") if problem == "foreign" else ""})
            continue
        ms_name = card.get("name") or ""
        if card.get("archived"):
            # Досюда архивная карточка дойти уже не должна (её отсекает `archive_swaps`),
            # но проверка остаётся: архив в оприходовании — это остаток на мёртвой карточке.
            skipped.append({**row, "reason": "archived", "ms_name": ms_name})
            continue
        if "брак" in ms_name.lower():
            skipped.append({**row, "reason": "defective", "ms_name": ms_name})
            continue
        if card["meta"]["type"] not in ("product", "variant", "bundle"):
            skipped.append({**row, "reason": "not_stockable", "ms_name": ms_name})
            continue
        if row["price_raw"] in (None, "", 0):
            skipped.append({**row, "reason": "no_price", "ms_name": ms_name})
            continue
        ready.append({**row, "card": card, "ms_name": ms_name,
                      "price_kop": to_kopecks(row["price_raw"], rate)})
    return ready, skipped


def existing_docs(profile):
    """Прошлые оприходования группы на «Удаленном складе» — кандидаты на удаление."""
    pattern = re.compile(DOC_NAME_RE.format(key=re.escape(profile.key)))
    docs = ms_api.get("/entity/enter", {
        "limit": 1000,
        "filter": [f"name~={profile.key}_",
                   f"store={ms_api.BASE}/entity/store/{STORE_REMOTE}"],
    }).get("rows", [])
    # фильтр МС перепроверяем сами: удаление документов — не то место, где верят на слово
    return [d for d in docs
            if pattern.match(d.get("name", "")) and ms_api.meta_id(d, "store") == STORE_REMOTE]


def build_docs(ready, profile, moment):
    """Позиции -> тела документов по POSITIONS_PER_DOC штук.

    Перед сборкой — гейт по архиву. Отбор идёт в `classify`, но документ создаётся один раз
    и потом живёт в МС месяцами, поэтому проверяем ещё раз здесь: архивная позиция в
    оприходовании — это остаток на мёртвой карточке, который никуда дальше не уйдёт.
    """
    dead = [i for i in ready if (i.get("card") or {}).get("archived")]
    if dead:
        raise RuntimeError(
            f"{profile.key}: архивные карточки в позициях ({len(dead)}), "
            f"первая — {dead[0].get('article')} / {dead[0].get('ms_name')}")
    stamp = moment.strftime("%Y-%m-%d")
    docs = []
    for page, start in enumerate(range(0, len(ready), POSITIONS_PER_DOC), start=1):
        chunk = ready[start:start + POSITIONS_PER_DOC]
        docs.append({
            "name": f"{profile.key}_{stamp}_p{page}",
            "description": profile.key,
            "moment": moment.strftime("%Y-%m-%d %H:%M:%S"),
            "applicable": True,
            "organization": ms_api.ref("organization", ORG_DIGITAL),
            "store": ms_api.ref("store", STORE_REMOTE),
            "positions": [{
                "quantity": item["qty"],
                "price": item["price_kop"],
                "assortment": {"meta": item["card"]["meta"]},
            } for item in chunk],
        })
    return docs


def card_updates(ready, profile):
    """Правки карточек: описание = наименование прайса, закупочная = цена прайса.

    Только для карточек, чей поставщик входит в группу прайса, и только тип product —
    у вариантов и комплектов своя закупочная механика, туда не лезем.
    """
    updates, own = [], own_ids(profile)
    for item in ready:
        card = item["card"]
        if card["meta"]["type"] != "product":
            continue
        if ms_api.meta_id(card, "supplier") not in own:
            continue
        patch = {"meta": card["meta"], "id": card["id"]}
        changed = False
        if item["name"] and (card.get("description") or "").strip() != item["name"]:
            patch["description"] = item["name"]
            changed = True
        if int((card.get("buyPrice") or {}).get("value") or 0) != item["price_kop"]:
            patch["buyPrice"] = {
                "value": item["price_kop"],
                "currency": (card.get("buyPrice") or {}).get("currency")
                            or ms_api.ref("currency", "20d3bd8c-bde9-47cc-ba24-b299ba80b6d7"),
            }
            changed = True
        if changed:
            updates.append(patch)
    return updates


def rename_stale(stale, moment, log=print):
    """Пометить прошлые документы суффиксом «_old<ЧЧММСС>» и вернуть их с новыми именами.

    Зачем: имя оприходования в МС уникально (HTTP 412, code 3006), а наши новые документы за
    тот же день называются так же, как прошлые. Удалять прошлые ПЕРЕД созданием нельзя — при
    сбое остаток обнулится. Поэтому старые сначала переименовываем: в любой момент на складе
    лежит либо старый комплект, либо оба, но не пустота.
    """
    suffix = f"_old{moment:%H%M%S}"
    renamed = []
    for i in range(0, len(stale), 100):
        batch = [{"meta": d["meta"], "name": d["name"] + suffix} for d in stale[i:i + 100]]
        renamed += ms_api.post("/entity/enter", batch)
    log(f"    прошлых документов помечено «{suffix}»: {len(renamed)}")
    return renamed


def apply_to_ms(docs, stale, updates, moment=None, log=print):
    """Запись в МойСклад: пометить прошлое -> создать новое -> обновить карточки -> удалить прошлое."""
    if stale:
        stale = rename_stale(stale, moment or now_msk(), log=log)
    created = []
    for doc in docs:
        created.append(ms_api.post("/entity/enter", doc))
        log(f"    создан {doc['name']} ({len(doc['positions'])} позиций)")
    if len(created) != len(docs):
        raise RuntimeError("создались не все документы — прошлые НЕ удаляем")
    # Сюда попадаем, только если создались ВСЕ новые документы: остаток на складе полный,
    # прошлые (уже помеченные «_old…») можно убирать.

    for i in range(0, len(updates), 100):
        ms_api.post("/entity/product", updates[i:i + 100])
    if updates:
        log(f"    карточек обновлено: {len(updates)}")

    if stale:
        for i in range(0, len(stale), 100):
            ms_api.post("/entity/enter/delete",
                        [{"meta": d["meta"]} for d in stale[i:i + 100]])
        log(f"    удалено прошлых документов: {len(stale)}")
    return created


def summarize(ready, skipped, docs, stale, updates, rate, rate_date, profile, source):
    counts = {}
    for row in skipped:
        counts[row["reason"]] = counts.get(row["reason"], 0) + 1
    total_sum = sum(Decimal(i["price_kop"]) * Decimal(str(i["qty"])) for i in ready) / 100
    return {
        "supplier": profile.key,
        "source": source,
        "rate": str(rate),
        "rate_date": rate_date,
        "rows_total": len(ready) + len(skipped),
        "rows_loaded": len(ready),
        "rows_skipped": len(skipped),
        "skipped_by_reason": counts,
        # Сколько строк спасено подменой архивной карточки на живую родню того же кода:
        # до правила 18.08.2026 они молча уезжали в новинки как «нет товара в МС».
        "archived_swapped": sum(1 for i in ready if i.get("archived_swap")),
        "docs": len(docs),
        "stale_docs": len(stale),
        "card_updates": len(updates),
        "sum_rub": float(total_sum),
    }
