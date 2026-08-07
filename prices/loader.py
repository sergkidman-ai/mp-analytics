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

MSK = dt.timezone(dt.timedelta(hours=3))
# Имя документа: «<группа>_<дата>_p<N>», плюс возможный хвост «_old<время>» — так помечаются
# прошлые документы на время замены (см. apply_to_ms). Хвост в шаблоне обязателен: иначе
# документ, оставшийся от прерванного прогона, следующий прогон не увидит и не уберёт.
DOC_NAME_RE = "^{key}_\\d{{4}}-\\d{{2}}-\\d{{2}}_p\\d+(_old\\d+)*$"

SKIP_REASONS = {
    "not_found": "нет товара в МС по артикулу",
    "archived": "товар архивный",
    "defective": "«брак» в наименовании МС",
    "no_price": "нет цены в прайсе",
    "no_stock": "нет остатка / формулировка не разобрана",
    "ambiguous": "артикул в МС неоднозначен",
    "not_stockable": "тип позиции не приходуется на склад",
    "duplicate": "артикул повторяется в прайсе с разными ценой/остатком",
    "price_absurd": "цена вне разумного коридора (проверка аномалий)",
    "category_off": "категория прайса нам не нужна",
    "blacklisted": "артикул в чёрном списке (забраковали раньше)",
    "bulk_toner": "тонер больше 150 г (берём только разовую заправку)",
    "bulk_ink": "чернила больше 150 мл (берём только разовую заправку)",
    "cleaning": "промывочная жидкость — не наш товар",
    "refillable": "картридж перезаправляемый — не наш товар",
    "set_incomplete": "неполный цветовой комплект — ждём остальные цвета",
}


def drop_duplicates(rows):
    """Один артикул — одна строка. Полные повторы схлопываем, противоречивые снимаем.

    Одиссей присылает часть позиций дважды. Складывать остатки нельзя (это может быть один
    и тот же товар в двух разделах — остаток задвоится), выбирать «какую-нибудь» — тоже:
    если строки расходятся ценой или остатком, решать должен человек, а не загрузчик.
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
        if first in out:
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


def pick_card(cards, supplier_ids):
    """Одна карточка из найденных по артикулу. При неоднозначности — та, что от нашей группы."""
    if len(cards) == 1:
        return cards[0], None
    own = [c for c in cards if ms_api.meta_id(c, "supplier") in supplier_ids]
    if len(own) == 1:
        return own[0], None
    return None, "ambiguous"


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


def classify(rows, profile, rate):
    """Строки прайса -> (позиции к загрузке, пропущенные с причиной)."""
    rows, off = filter_categories(rows, profile)
    rows, skipped = drop_duplicates(rows)
    skipped += off
    cards = lookup_by_article([r["article"] for r in rows])
    ready = []
    for row in rows:
        found = cards.get(row["article"], [])
        if not found:
            skipped.append({**row, "reason": "not_found"})
            continue
        card, problem = pick_card(found, profile.supplier_ids)
        if problem:
            skipped.append({**row, "reason": problem, "ms_candidates": len(found)})
            continue
        ms_name = card.get("name") or ""
        if card.get("archived"):
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
        if not row["qty"]:
            skipped.append({**row, "reason": "no_stock", "ms_name": ms_name})
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
    """Позиции -> тела документов по POSITIONS_PER_DOC штук."""
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
    updates = []
    for item in ready:
        card = item["card"]
        if card["meta"]["type"] != "product":
            continue
        if ms_api.meta_id(card, "supplier") not in profile.supplier_ids:
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
        "docs": len(docs),
        "stale_docs": len(stale),
        "card_updates": len(updates),
        "sum_rub": float(total_sum),
    }
