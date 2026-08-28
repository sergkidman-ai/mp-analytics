# поток: prc — добор карточек, заведённых сегодня, в актуальные оприходования поставщиков
import sys, argparse, datetime as dt
sys.path.insert(0, "/opt/mp-analytics")
from dotenv import load_dotenv; load_dotenv("/opt/mp-analytics/.env")
from core import ms_api
from core.db import query
from prices import blacklist, loader
from prices.profiles import get_identity, STORE_REMOTE, ORG_DIGITAL, POSITIONS_PER_DOC

# Поставщик без прайса тоже здесь: Солюшнс принт грузит внешний загрузчик, документы он
# кладёт на тот же «Удаленный склад» и под тем же именем `<ключ>_<дата>_pNN`, а карточки
# несопоставленным строкам заводим мы — их и добираем (команда Сергея 27.08.2026).
# Поэтому ниже `get_identity`, а не `get_profile`: `existing_docs` берёт из профиля только
# `key`, а прайса у такого поставщика нет и профиля ему не завести.
PROC = ["colortek", "odissey", "sakura", "kaktus_msk", "s_print_msk"]
SUP2KEY = {'ООО "КОМПАНИЯ ФЕРРЕТ"': "kaktus_msk", 'ООО "ОДИССЕЙ"': "odissey",
           'ООО "ОДИССЕЙ" WB': "odissey", 'ООО "ПОЗИТИВ"': "sakura",
           'ООО "КОЛОРТЕК РУС"': "colortek",
           'ООО "Солюшнс принт" МСК': "s_print_msk"}

ap = argparse.ArgumentParser()
ap.add_argument("--day", default=str(dt.date.today()))
ap.add_argument("--apply", action="store_true")
a = ap.parse_args()

# 1. карточки, созданные сегодня (аудит МС — /entity/product поле created не отдаёт)
new = []
for ev in ms_api.get("/audit", {"filter": f"entityType=product;eventType=create;moment>={a.day} 00:00:00",
                                "limit": 100}).get("rows", []):
    for e in ms_api.get(ev["events"]["meta"]["href"].replace(ms_api.BASE, "")).get("rows", []):
        pid = ((e.get("entity") or {}).get("meta") or {}).get("href", "").rsplit("/", 1)[-1]
        if pid:
            new.append(pid)
cards = []
for pid in new:
    try:
        p = ms_api.get(f"/entity/product/{pid}", {"expand": "supplier"})
    except Exception:
        continue
    sup = (p.get("supplier") or {}).get("name")
    key = SUP2KEY.get(sup)
    if key in PROC and not p.get("archived"):
        cards.append({"id": pid, "code": p.get("code"), "ec": p.get("externalCode"),
                      "article": p.get("article"), "name": p.get("name"), "sup": sup,
                      "key": key, "meta": p["meta"]})

# 2. актуальные оприходования каждого профиля + их позиции
docs_by_key, in_docs = {}, {}
for key in sorted({c["key"] for c in cards}):
    docs = loader.existing_docs(get_identity(key))
    # rsplit, а не split: ключ поставщика сам содержит «_p» (`s_print_msk`), и разрез по
    # ПЕРВОМУ вхождению давал партию «s» — под неё подходили документы всех дат сразу,
    # и добор мог уехать в позавчерашний документ.
    last = max(d["name"].rsplit("_p", 1)[0] for d in docs)      # актуальная дата загрузки
    docs = [d for d in docs if d["name"].startswith(last + "_p")]
    docs.sort(key=lambda d: int(d["name"].rsplit("_p", 1)[1]))
    docs_by_key[key] = docs
    ids = set()
    for d in docs:
        for pos in ms_api.get(f"/entity/enter/{d['id']}/positions", {"limit": 1000}).get("rows", []):
            ids.add(ms_api.meta_id(pos, "assortment"))
    in_docs[key] = ids

# 3. цена и количество из последней загрузки прайса профиля
plan, skip = [], []
for c in cards:
    key = c["key"]
    if c["id"] in in_docs.get(key, ()):
        skip.append({**c, "why": "уже в документе"}); continue
    # Бракующие правила поставщика действуют и здесь, а не только при разборе письма:
    # карточка набора Солюшнс принта может быть заведена руками задолго до правила, и
    # добор обязан её обойти — иначе остаток набора ляжет поверх остатка составляющих.
    if blacklist.in_rules(c["article"], key):
        skip.append({**c, "why": "правило поставщика (набор)"}); continue
    row = query("""SELECT r.qty, r.price_src, r.price_rub, l.rate, l.id load_id, l.load_date
                     FROM prc_price_row r JOIN prc_price_load l ON l.id = r.load_id
                    WHERE l.supplier_key = %s AND NOT l.dry_run AND l.status = 'ok'
                      AND r.article = %s ORDER BY l.id DESC LIMIT 1""", (key, c["article"]))
    if not row:
        skip.append({**c, "why": "нет строки в прайсе"}); continue
    r = row[0]
    qty = float(r["qty"] or 0)
    price = float(r["price_rub"] if r["price_rub"] is not None
                  else float(r["price_src"] or 0) * float(r["rate"] or 1))
    if not qty:
        skip.append({**c, "why": "нет остатка"}); continue
    if not price:
        skip.append({**c, "why": "нет цены"}); continue
    plan.append({**c, "qty": qty, "price": round(price, 2), "load_id": r["load_id"],
                 "load_date": str(r["load_date"])})

# 4. отчёт
lines = [f"# Добор новых карточек в оприходования — {a.day}", "",
         f"Карточек создано сегодня: {len(new)}; наших поставщиков: {len(cards)}; "
         f"к добору: {len(plan)}; пропущено: {len(skip)}", ""]
for key in sorted(docs_by_key):
    d = docs_by_key[key]
    lines.append(f"- **{key}**: актуальная партия {d[0]['name'].rsplit('_p', 1)[0]}, документов {len(d)}, "
                 f"позиций в них {len(in_docs[key])}")
# раскладка: добираем в последний документ партии до POSITIONS_PER_DOC, остаток — новым p{N+1}
layout = {}                                     # key -> [(doc | new-name, [позиции])]
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
doc_of = {p["id"]: d["name"] for st in layout.values() for d, g in st for p in g}

lines += ["", "## К добору", "", "| поставщик | код | вн. | артикул | кол-во | цена ₽ | сумма ₽ | документ | наименование |",
          "|---|---|---|---|---|---|---|---|---|"]
for p in sorted(plan, key=lambda x: (x["key"], x["code"] or "")):
    lines.append(f"| {p['sup']} | {p['code']} | {p['ec']} | {p['article']} | {p['qty']:.0f} | "
                 f"{p['price']:.2f} | {p['qty']*p['price']:.2f} | {doc_of[p['id']]} | {p['name'][:60]} |")
lines += ["", "## Пропущено", "", "| код | артикул | причина | наименование |", "|---|---|---|---|"]
for s in sorted(skip, key=lambda x: (x["why"], x["code"] or "")):
    lines.append(f"| {s['code']} | {s['article']} | {s['why']} | {s['name'][:60]} |")

# 5. применение
if a.apply and plan:
    lines += ["", "## Применено", ""]
    for key in sorted(layout):
        moment = docs_by_key[key][-1]["moment"]          # партия одна — момент берём у неё
        for doc, group in layout[key]:
            body = [{"quantity": p["qty"], "price": int(round(p["price"] * 100)),
                     "assortment": {"meta": p["meta"]}} for p in group]
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

out = f"/opt/mp-analytics/docs/reports/prc_enter_new_{a.day}.md"
open(out, "w").write("\n".join(lines) + "\n")
print("\n".join(lines[:6]))
print("отчёт:", out, "| режим:", "APPLY" if a.apply else "dry-run")
