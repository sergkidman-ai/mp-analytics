# поток: prc
# -*- coding: utf-8 -*-
"""
Загрузка прайса поставщика в МойСклад: Оприходование на «Удаленный склад».

Порядок и правила согласованы 05.08.2026 и восстановлены из ручной загрузки Колортека:

  * матчинг СТРОГО «артикул поставщика == поле Артикул в МС», ничего не угадываем;
  * количество — из прайса (текстовые диапазоны разворачиваются таблицей профиля);
  * цена — прайс × курс ЦБ на дату загрузки × надбавка, округление до копейки;
  * не грузим: нет товара в МС, товар архивный, «брак» в наименовании МС,
    нет цены, нет остатка, неоднозначный артикул;
  * документы бьются по 100 позиций, имя «<группа>_<дата>_p<N>», описание — группа;
  * СНАЧАЛА создаём новые документы, ПОТОМ удаляем прошлые — чтобы при сбое остаток
    остался задвоенным (это видно и чинится), а не обнулился;
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
DOC_NAME_RE = "^{key}_\\d{{4}}-\\d{{2}}-\\d{{2}}_p\\d+$"

SKIP_REASONS = {
    "not_found": "нет товара в МС по артикулу",
    "archived": "товар архивный",
    "defective": "«брак» в наименовании МС",
    "no_price": "нет цены в прайсе",
    "no_stock": "нет остатка / формулировка не разобрана",
    "ambiguous": "артикул в МС неоднозначен",
    "not_stockable": "тип позиции не приходуется на склад",
}


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


def classify(rows, profile, rate):
    """Строки прайса -> (позиции к загрузке, пропущенные с причиной)."""
    cards = lookup_by_article([r["article"] for r in rows])
    ready, skipped = [], []
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


def apply_to_ms(docs, stale, updates, log=print):
    """Запись в МойСклад: создать новое -> обновить карточки -> удалить прошлое."""
    created = []
    for doc in docs:
        created.append(ms_api.post("/entity/enter", doc))
        log(f"    создан {doc['name']} ({len(doc['positions'])} позиций)")
    if len(created) != len(docs):
        raise RuntimeError("создались не все документы — прошлые НЕ удаляем")

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
