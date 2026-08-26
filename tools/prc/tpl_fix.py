# поток: prc — из шаблона человека собрать `*_fixed.xlsx`: поля по НАШИМ правилам.
#
# Присланный файл не трогаем (правило 35) — правки идут в копию.
#
# Человек заполняет ТРИ поля: «Код», «Внешний код», «Вес» (плюс приходящие из прайса
# «Наименование» и «Артикул»). Всё остальное — работа конвейера (решение Сергея 21.08.2026):
# отсутствующие колонки шаблона дописываются и заполняются по правилам —
#   Группа и Code128 и «Связь» — как у родни по внешнему коду (правила 22, 23);
#   Поставщик — контрагент, на котором сидит родня, а если её нет — носители того же
#     суффикса кода;
#   Код = внешний код + суффикс поставщика, суффикс берём по ФОРМАТУ АРТИКУЛА (под каким
#     суффиксом такие артикулы живут в МС), а не по бренду в названии: название пишет
#     поставщик, и бренд там принтерный (`5692sk` при артикуле Solutions Print);
#   «Название WB» — только формулой `wb_fill.compose()` (правило 41), из каталога ТК;
#   Описание = наименование, «Код поставщика» = артикул, НДС 22, шт, Китай, гарантия 365;
#   вес — человек → родня → каталог ТК (граммы) ;
#   закупочная цена — из последнего прайса по артикулу, нет прайса → 0 и строка в логе.
# НАИМЕНОВАНИЕ ПОСТАВЩИКА НЕ ПРАВИМ (решение Сергея 19.08.2026), даже когда в нём ошибочный
# ресурс: это его товар и его название, наши правила касаются полей карточки.
import sys, argparse, collections, json
sys.path.insert(0, "/opt/mp-analytics")
from dotenv import load_dotenv; load_dotenv("/opt/mp-analytics/.env")
import re
import openpyxl
from core import ms_api
from prices.ms_import import abbr_by_name, wb_compose, query

FULL = ["Группы", "Код", "Внешний код", "Наименование", "Описание", "Артикул",
        "Доп. поле: Код поставщика", "Единица измерения", "Закупочная цена", "НДС",
        "Поставщик", "Вес", "Страна", "Доп. поле: Гарантия/ Срок службы",
        "Доп. поле: Название WB", "Штрихкод Code128", "Доп. поле: Связь"]

ap = argparse.ArgumentParser()
ap.add_argument("src")
ap.add_argument("--new-ec", default="", help="внешний код для строки, где он пуст")
ap.add_argument("--set", default="{}", help='JSON: {"код": {"колонка": "значение"}} — поверх всего')
a = ap.parse_args()
SET = json.loads(a.set)
dst = a.src.replace(".xlsx", "_fixed.xlsx")

wb = openpyxl.load_workbook(a.src)
ws = wb.active
col = {name: i + 1 for i, name in enumerate(c.value for c in ws[1]) if name}
added = []
for name in FULL:                       # шаблон бывает урезанным — дописываем колонки
    if name not in col:
        col[name] = ws.max_column + 1
        ws.cell(1, col[name]).value = name
        added.append(name)
kin_cache, folder_cache, cp_cache, sfx_cache, log = {}, {}, {}, {}, []


def kin(ec):
    if ec not in kin_cache:
        kin_cache[ec] = ms_api.get("/entity/product",
                                   {"filter": f"externalCode={ec}", "limit": 50}).get("rows", [])
    return kin_cache[ec]


def folder_path(fid):
    if fid not in folder_cache:
        f = ms_api.get(f"/entity/productfolder/{fid}")
        folder_cache[fid] = (f.get("pathName") + "/" if f.get("pathName") else "") + f.get("name")
    return folder_cache[fid]


def cp_name(cid):
    if cid not in cp_cache:
        cp_cache[cid] = ms_api.get(f"/entity/counterparty/{cid}").get("name")
    return cp_cache[cid]


def suffix_by_article(art, ec):
    """Суффикс кода по формату артикула: под каким суффиксом такие артикулы живут в МС."""
    pref = re.match(r"^[A-Za-zА-Яа-я]+", art or "")
    if not pref:
        return None, "в артикуле нет буквенного префикса"
    key = pref.group(0)
    if key not in sfx_cache:
        # Префикс обязан кончаться на границе буквенного токена — иначе КОРОТКИЙ префикс
        # ловит чужой бренд и МОЛЧА переписывает верный код человека. У NetProduct артикул
        # `N-CF350A`, у NV Print — `NV-CF350A`; `^N` топило 29 карточек `np` полутора тысячами
        # `nv`, и `2245np` превращался в `2245nv`. Та же правка — в `tpl_check.py`.
        rows = query("""SELECT regexp_replace(code, '^' || external_code, '') s, count(*) n
                          FROM ms_product
                         WHERE NOT archived
                           AND article ~ ('^' || %s || '($|[^A-Za-zА-Яа-я])')
                           AND external_code <> '' AND code LIKE external_code || '%%'
                         GROUP BY 1 ORDER BY 2 DESC""", (key,))
        sfx_cache[key] = [(x["s"], x["n"]) for x in rows if x["s"]]
    top = sfx_cache[key]
    if not top:
        return None, f"артикулов «{key}…» в МС нет — суффикс решает человек"
    return top[0][0], f"суффикс по артикулам «{key}…»: `{top[0][0]}` ×{top[0][1]}"


def supplier_name(family, sfx, art):
    """Контрагент карточки — по НОСИТЕЛЯМ СУФФИКСА кода, а не по родне внешнего кода.

    Родня внешнего кода — это все поставщики одной модели (у 3565 их четырнадцать), её
    большинство к нашему поставщику отношения не имеет. Суффикс же и есть поставщик.
    Хвост артикула («…_MSK») разводит юрлица одного поставщика — если он есть, сначала
    смотрим карточки с таким же хвостом.
    """
    tail = re.search(r"_[A-Za-z]+$", art or "")
    tail = tail.group(0) if tail else ""
    cnt, src = collections.Counter(), ""
    if sfx:
        rows = query("""SELECT ms_id, article FROM ms_product
                         WHERE NOT archived AND code ~ ('^[0-9]+' || %s || '$') LIMIT 300""", (sfx,))
        same = [x for x in rows if (x["article"] or "").endswith(tail)] if tail else rows
        pick = (same or rows)[:12]
        for x in pick:
            cid = ms_api.meta_id(ms_api.get(f"/entity/product/{x['ms_id']}"), "supplier")
            if cid:
                cnt[cid] += 1
        src = (f"поставщик у карточек суффикса `{sfx}`"
               + (f" с хвостом артикула «{tail}»" if tail and same else ""))
    if not cnt:                                   # суффикс новый — последняя опора родня
        cnt = collections.Counter(cid for p in family if (cid := ms_api.meta_id(p, "supplier")))
        src = "поставщик у родни внешнего кода (суффикс новый) — ПРОВЕРИТЬ"
    if not cnt:
        return None, "поставщика взять неоткуда"
    top = cnt.most_common()
    note = src + (f"; у носителей суффикса ещё {len(top) - 1} контрагент(а) — проверить"
                  if len(top) > 1 else "")
    return cp_name(top[0][0]), note


def tc_weight(ec):
    r = query("SELECT raw->>'weight' w FROM prc_tc_model WHERE external_code=%s AND gone_at IS NULL", (ec,))
    try:
        return round(float(r[0]["w"]) / 1000, 3) if r and r[0]["w"] else None
    except (TypeError, ValueError):
        return None


def price_of(art):
    r = query("""SELECT p.price_rub FROM prc_price_row p JOIN prc_price_load l ON l.id = p.load_id
                  WHERE upper(p.article) = upper(%s) AND p.price_rub IS NOT NULL
                  ORDER BY l.load_date DESC LIMIT 1""", (art,))
    return float(r[0]["price_rub"]) if r else None


def put(r, name, val, ch, why):
    if ws.cell(r, col[name]).value != val:
        ch.append(f"{why}: «{ws.cell(r, col[name]).value}» → «{val}»")
        ws.cell(r, col[name]).value = val


for r in range(2, ws.max_row + 1):
    art = str(ws.cell(r, col["Артикул"]).value or "").strip()
    if not art:
        continue
    ch, name = [], ws.cell(r, col["Наименование"]).value or ""
    ec = str(ws.cell(r, col["Внешний код"]).value or "").strip() or a.new_ec
    assert ec, f"строка {r}: внешний код пуст, задайте --new-ec"
    put(r, "Внешний код", ec, ch, "внешний код")
    family = kin(ec)

    sfx, why = suffix_by_article(art, ec)
    if not sfx:                                   # артикул формата, которого в МС нет
        sfx = abbr_by_name(name)
        why = f"{why}; суффикс по бренду в названии: `{sfx}`" if sfx else why
    if sfx:
        put(r, "Код", f"{ec}{sfx}", ch, why)
    else:
        ch.append(f"код «{ws.cell(r, col['Код']).value}» оставлен: {why}")

    paths = collections.Counter(fid for p in family if (fid := ms_api.meta_id(p, "productFolder")))
    if paths:
        put(r, "Группы", folder_path(paths.most_common(1)[0][0]), ch, "группа")
    elif not ws.cell(r, col["Группы"]).value:
        ch.append("группа пуста: родни по внешнему коду нет — ставит человек")

    sup, swhy = supplier_name(family, sfx, art)
    if sup:
        put(r, "Поставщик", sup, ch, swhy)
    else:
        ch.append(swhy + " — ставит человек")

    for cname, val in (("Единица измерения", "шт"), ("Страна", "Китай"), ("НДС", 22),
                       ("Доп. поле: Гарантия/ Срок службы", 365)):
        put(r, cname, val, ch, cname)

    if not str(ws.cell(r, col["Описание"]).value or "").strip():
        put(r, "Описание", name, ch, "описание = наименование")

    nm, note = wb_compose(ec, name)               # правило 41: только формула, родню не смотрим
    if nm:
        put(r, "Доп. поле: Название WB", nm, ch, "название WB по формуле")
        if note:                                  # собрано не из каталога ТК — человеку сверить
            ch.append(f"название WB: {note}")
    else:
        ch.append(f"название WB НЕ заполнено: {note}")

    bc = sorted({b["code128"] for p in family for b in p.get("barcodes") or [] if b.get("code128")})
    if bc:
        put(r, "Штрихкод Code128", bc[0], ch, "code128 у родни")
    elif not ws.cell(r, col["Штрихкод Code128"]).value:
        ch.append("code128 пуст: родни по внешнему коду нет — новый штрихкод заводит человек")

    if not ws.cell(r, col["Вес"]).value:
        w = [p.get("weight") for p in family if p.get("weight")]
        if w:
            put(r, "Вес", max(set(w), key=w.count), ch, "вес пуст, взят у родни")
        elif (tw := tc_weight(ec)):
            put(r, "Вес", tw, ch, "вес пуст, взят из каталога ТК")
        else:
            ch.append("вес пуст, у родни и в ТК его нет")

    if not ws.cell(r, col["Закупочная цена"]).value:
        p = price_of(art)
        if p:
            put(r, "Закупочная цена", p, ch, "цена из прайса")
        else:
            put(r, "Закупочная цена", 0, ch, f"прайса по артикулу «{art}» нет, цена 0")

    links = sorted({str(x["value"]).strip() for p in family for x in p.get("attributes") or []
                    if x.get("name") == "Связь" and x.get("value")})
    if links and not ws.cell(r, col["Доп. поле: Связь"]).value:
        put(r, "Доп. поле: Связь", links[0], ch,
            "связь у родни" + (f" (варианты {links})" if len(links) > 1 else ""))

    ws.cell(r, col["Артикул"]).value = art
    ws.cell(r, col["Доп. поле: Код поставщика"]).value = art
    for cname, val in SET.get(str(ws.cell(r, col["Код"]).value), {}).items():
        put(r, cname, val, ch, f"вручную ({cname})")
    log.append(f"{ws.cell(r, col['Код']).value:9} | " + ("; ".join(ch) if ch else "без правок"))

wb.save(dst)
if added:
    print("дописаны колонки: " + ", ".join(added))
print("\n".join(log))
print(f"\nстрок {len(log)}, с правками {sum(1 for l in log if 'без правок' not in l)} → {dst}")
