# поток: prc — добор карточек в АКТУАЛЬНОЕ оприходование поставщика.
"""Положить карточку товара в текущий документ прихода, не создавая партию заново.

Логика вынесена из `tools/prc/enter_new.py` (она там обкатана с 27.08.2026) и разведена
на две функции, чтобы её звал не только человек из консоли, но и кнопка «➕ карточка
поставщика», и автозаведение (`prices/auto_card.py`): карточка и позиция в приходе — один
шаг, а не два, где второй забывают.

Источник карточек у вызова разный — аудит МС за день (консоль) или список только что
созданных карточек (кнопка, автомат), — а раскладка и запись общие.

Ничего не выдумываем: количество и цена берутся из ПОСЛЕДНЕЙ удачной загрузки прайса этого
поставщика, а не из карточки и не из головы. Нет строки в прайсе, нет остатка, нет цены —
позиция не добирается, причина попадает в отчёт.
"""
import datetime as dt

from core import ms_api
from core.db import query
from prices import blacklist, loader
from prices.profiles import get_identity, STORE_REMOTE, ORG_DIGITAL, POSITIONS_PER_DOC

# Поставщик без прайса тоже здесь: Солюшнс принт грузит внешний загрузчик, документы он
# кладёт на тот же «Удаленный склад» и под тем же именем `<ключ>_<дата>_pNN`, а карточки
# несопоставленным строкам заводим мы — их и добираем (команда Сергея 27.08.2026).
PROC = ["colortek", "odissey", "sakura", "kaktus_msk", "s_print_msk"]
SUP2KEY = {'ООО "КОМПАНИЯ ФЕРРЕТ"': "kaktus_msk", 'ООО "ОДИССЕЙ"': "odissey",
           'ООО "ОДИССЕЙ" WB': "odissey", 'ООО "ПОЗИТИВ"': "sakura",
           'ООО "КОЛОРТЕК РУС"': "colortek",
           'ООО "Солюшнс принт" МСК': "s_print_msk"}


def cards_created_on(day, log=print):
    """Карточки наших поставщиков, заведённые в этот день (МС не отдаёт `created` у товара)."""
    new = []
    for ev in ms_api.get("/audit", {"filter": f"entityType=product;eventType=create;"
                                              f"moment>={day} 00:00:00", "limit": 100}).get("rows", []):
        for e in ms_api.get(ev["events"]["meta"]["href"].replace(ms_api.BASE, "")).get("rows", []):
            pid = ((e.get("entity") or {}).get("meta") or {}).get("href", "").rsplit("/", 1)[-1]
            if pid:
                new.append(pid)
    cards = []
    for pid in new:
        try:
            p = ms_api.get(f"/entity/product/{pid}", {"expand": "supplier"})
        except Exception as exc:                       # карточку могли удалить тем же днём
            log(f"  карточка {pid} не читается: {type(exc).__name__}")
            continue
        cards.append(card_of(p))
    return [c for c in cards if c["key"] in PROC and not c["archived"]], len(new)


def card_of(product):
    """Ответ `/entity/product` → запись карточки в том виде, в каком её ждёт `plan_add`."""
    sup = (product.get("supplier") or {}).get("name")
    return {"id": product["id"], "code": product.get("code"), "ec": product.get("externalCode"),
            "article": product.get("article"), "name": product.get("name"), "sup": sup,
            "key": SUP2KEY.get(sup), "archived": bool(product.get("archived")),
            "meta": product["meta"]}


def by_ids(ms_ids, log=print):
    """Карточки по их id в МС — для кнопки и автозаведения (только что созданные)."""
    out = []
    for pid in ms_ids:
        try:
            out.append(card_of(ms_api.get(f"/entity/product/{pid}", {"expand": "supplier"})))
        except Exception as exc:
            log(f"  карточка {pid} не читается: {type(exc).__name__}")
    return out


def _price_row(key, article):
    rows = query("""SELECT r.qty, r.price_src, r.price_rub, l.rate, l.id load_id, l.load_date
                      FROM prc_price_row r JOIN prc_price_load l ON l.id = r.load_id
                     WHERE l.supplier_key = %s AND NOT l.dry_run AND l.status = 'ok'
                       AND r.article = %s ORDER BY l.id DESC LIMIT 1""", (key, article))
    return rows[0] if rows else None


def plan_add(cards, log=print):
    """(раскладка по документам, к добору, отсев) — ничего не пишет.

    Раскладка: добираем в ПОСЛЕДНИЙ документ актуальной партии до `POSITIONS_PER_DOC`
    позиций, остаток — новым `_p{N+1}`. Партию ищем по имени документов поставщика.
    """
    cards = [c for c in cards if c.get("id")]
    docs_by_key, in_docs, skip = {}, {}, []
    for key in sorted({c["key"] for c in cards if c["key"]}):
        docs = loader.existing_docs(get_identity(key))
        if not docs:
            # Партии нет вовсе (первый прайс поставщика, документы удалены руками). Раньше
            # скрипт падал здесь на max() по пустому списку; теперь карточка просто уходит
            # человеку — создавать партию с нуля побочным эффектом кнопки нельзя.
            docs_by_key[key] = []
            continue
        # rsplit, а не split: ключ поставщика сам содержит «_p» (`s_print_msk`), и разрез по
        # ПЕРВОМУ вхождению давал партию «s» — под неё подходили документы всех дат сразу,
        # и добор мог уехать в позавчерашний документ.
        last = max(d["name"].rsplit("_p", 1)[0] for d in docs)      # актуальная дата загрузки
        docs = [d for d in docs if d["name"].startswith(last + "_p")]
        docs.sort(key=lambda d: int(d["name"].rsplit("_p", 1)[1]))
        docs_by_key[key] = docs
        ids = set()
        for d in docs:
            for pos in ms_api.get(f"/entity/enter/{d['id']}/positions",
                                  {"limit": 1000}).get("rows", []):
                ids.add(ms_api.meta_id(pos, "assortment"))
        in_docs[key] = ids

    plan = []
    for c in cards:
        key = c["key"]
        if key not in PROC:
            skip.append({**c, "why": "поставщик не идёт в оприходование"}); continue
        if not docs_by_key.get(key):
            skip.append({**c, "why": "нет актуального оприходования — положить руками"}); continue
        if c["id"] in in_docs.get(key, ()):
            skip.append({**c, "why": "уже в документе"}); continue
        # Бракующие правила поставщика действуют и здесь, а не только при разборе письма:
        # карточка набора Солюшнс принта может быть заведена руками задолго до правила, и
        # добор обязан её обойти — иначе остаток набора ляжет поверх остатка составляющих.
        if blacklist.in_rules(c["article"], key):
            skip.append({**c, "why": "правило поставщика (набор)"}); continue
        r = _price_row(key, c["article"])
        if not r:
            skip.append({**c, "why": "нет строки в прайсе"}); continue
        qty = float(r["qty"] or 0)
        price = float(r["price_rub"] if r["price_rub"] is not None
                      else float(r["price_src"] or 0) * float(r["rate"] or 1))
        if not qty:
            skip.append({**c, "why": "нет остатка"}); continue
        if not price:
            skip.append({**c, "why": "нет цены"}); continue
        plan.append({**c, "qty": qty, "price": round(price, 2), "load_id": r["load_id"],
                     "load_date": str(r["load_date"])})

    layout = {}                       # ключ -> [(документ | заготовка нового, [позиции])]
    for key in sorted({p["key"] for p in plan}):
        group = sorted([p for p in plan if p["key"] == key], key=lambda x: x["code"] or "")
        last = docs_by_key[key][-1]
        free = max(0, POSITIONS_PER_DOC - (last.get("positions", {}).get("meta", {}).get("size") or 0))
        steps = []
        if free:
            steps.append((last, group[:free]))
        rest, page = group[free:], int(last["name"].rsplit("_p", 1)[1])
        while rest:
            page += 1
            steps.append(({"name": f"{last['name'].rsplit('_p', 1)[0]}_p{page}", "id": None},
                          rest[:POSITIONS_PER_DOC]))
            rest = rest[POSITIONS_PER_DOC:]
        layout[key] = [s for s in steps if s[1]]
    return layout, plan, skip, docs_by_key, in_docs


def apply_add(layout, docs_by_key, dry=True, log=print):
    """Записать позиции в МС. `dry=True` — только рассказать, что было бы.

    Возвращает строки отчёта: по одной на документ.
    """
    lines = []
    for key in sorted(layout):
        moment = docs_by_key[key][-1]["moment"]          # партия одна — момент берём у неё
        for doc, group in layout[key]:
            body = [{"quantity": p["qty"], "price": int(round(p["price"] * 100)),
                     "assortment": {"meta": p["meta"]}} for p in group]
            if dry:
                lines.append(f"- {doc['name']}{'' if doc['id'] else ' (был бы создан)'}: "
                             f"+{len(body)} поз. — сухой прогон, в МС не записано")
                continue
            if doc["id"]:
                ms_api.post(f"/entity/enter/{doc['id']}/positions", body)
                did = doc["id"]
            else:
                created = ms_api.post("/entity/enter", {
                    "name": doc["name"], "description": key, "moment": moment,
                    "applicable": True,
                    "organization": ms_api.ref("organization", ORG_DIGITAL),
                    "store": ms_api.ref("store", STORE_REMOTE), "positions": body})
                did = created["id"]
            after = ms_api.get(f"/entity/enter/{did}")
            lines.append(f"- {doc['name']}{'' if doc['id'] else ' (создан)'}: +{len(body)} поз., "
                         f"итого позиций {after.get('positions',{}).get('meta',{}).get('size')}, "
                         f"сумма документа {after.get('sum',0)/100:.2f} ₽")
    for ln in lines:
        log(ln)
    return lines


def add_cards(cards, dry=True, log=print):
    """Разложить и добрать одним вызовом. -> (строки отчёта, к добору, отсев, куда легло).

    `where` — код карточки → имя документа: кнопке нужно сказать человеку, куда именно
    уехала позиция, а не просто «готово».
    """
    layout, plan, skip, docs_by_key, _ = plan_add(cards, log=log)
    where = {p["code"]: d["name"] for st in layout.values() for d, g in st for p in g}
    lines = apply_add(layout, docs_by_key, dry=dry, log=log) if plan else []
    return lines, plan, skip, where


def report(day=None, cards=None, dry=True, log=print):
    """Отчёт добора за день в `docs/reports/prc_enter_new_<день>.md` (консольный сценарий)."""
    day = day or str(dt.date.today())
    if cards is None:
        cards, seen = cards_created_on(day, log=log)
    else:
        seen = len(cards)
    layout, plan, skip, docs_by_key, in_docs = plan_add(cards, log=log)
    lines = [f"# Добор новых карточек в оприходования — {day}", "",
             f"Карточек создано сегодня: {seen}; наших поставщиков: {len(cards)}; "
             f"к добору: {len(plan)}; пропущено: {len(skip)}", ""]
    for key in sorted(docs_by_key):
        d = docs_by_key[key]
        if not d:
            lines.append(f"- **{key}**: актуальной партии нет — добор невозможен")
            continue
        lines.append(f"- **{key}**: актуальная партия {d[0]['name'].rsplit('_p', 1)[0]}, "
                     f"документов {len(d)}, позиций в них {len(in_docs.get(key, ()))}")
    doc_of = {p["id"]: d["name"] for st in layout.values() for d, g in st for p in g}
    lines += ["", "## К добору", "",
              "| поставщик | код | вн. | артикул | кол-во | цена ₽ | сумма ₽ | документ | наименование |",
              "|---|---|---|---|---|---|---|---|---|"]
    for p in sorted(plan, key=lambda x: (x["key"], x["code"] or "")):
        lines.append(f"| {p['sup']} | {p['code']} | {p['ec']} | {p['article']} | {p['qty']:.0f} | "
                     f"{p['price']:.2f} | {p['qty']*p['price']:.2f} | {doc_of[p['id']]} | "
                     f"{p['name'][:60]} |")
    lines += ["", "## Пропущено", "", "| код | артикул | причина | наименование |", "|---|---|---|---|"]
    for s in sorted(skip, key=lambda x: (x["why"], x["code"] or "")):
        lines.append(f"| {s['code']} | {s['article']} | {s['why']} | {s['name'][:60]} |")
    if plan and not dry:
        lines += ["", "## Применено", ""]
        lines += apply_add(layout, docs_by_key, dry=False, log=lambda *_: None)
    out = f"/opt/mp-analytics/docs/reports/prc_enter_new_{day}.md"
    open(out, "w").write("\n".join(lines) + "\n")
    return out, plan, skip
