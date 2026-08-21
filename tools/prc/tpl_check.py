# поток: prc — проверка шаблона загрузки карточек в МС (файл человека из дропбокса)
import sys, re, collections
sys.path.insert(0, "/opt/mp-analytics")
from dotenv import load_dotenv; load_dotenv("/opt/mp-analytics/.env")
import openpyxl
from core import ms_api
from prices.ms_import import family, wb_name, wb_compose, query, next_external_codes
from prices import features as F
from prices.catalog import compare, FEATURE_NAMES
from prices.novelty import kind
from prices.article_key import art_key, clash as art_clash, describe

SIGNS = ("model_ok", "kind_ok", "brand_ok", "color_ok", "resource_ok", "chip_ok")


def signs_of(name, article, wb):
    """Признаки для сравнения. Название МС + «Название WB»: чип пишут только во втором."""
    text = f"{name} | {wb or ''}"
    item = {"name": text, "article": str(article or "")}
    item["kind"] = kind(text)
    item.update(F.parse(text, item["article"]))
    return item

SRC = sys.argv[1]
OUT = sys.argv[2]

# Полный шаблон загрузки в МС (образец 20.08.2026). Человек выгружает из МС разный набор
# колонок; отсутствие колонки — не повод падать, но и не «поле в порядке»: чего в файле нет,
# то при загрузке останется ПУСТЫМ, а правило 41 требует 11 обязательных полей. Поэтому
# недостающие колонки идём в «Замечания» отдельным блоком, а проверки по ним пропускаем.
FULL = ["Группы", "Код", "Внешний код", "Наименование", "Описание", "Артикул",
        "Доп. поле: Код поставщика", "Единица измерения", "Закупочная цена", "НДС",
        "Поставщик", "Вес", "Страна", "Доп. поле: Гарантия/ Срок службы",
        "Доп. поле: Название WB", "Штрихкод Code128", "Доп. поле: Связь"]

ws = openpyxl.load_workbook(SRC, data_only=True).active
hdr = [c.value for c in ws[1]]
cols = {c for c in hdr if c}
missing = [c for c in FULL if c not in cols]
extra = [c for c in cols if c not in FULL]
rows = []
for r in range(2, ws.max_row + 1):
    rec = dict(zip(hdr, [ws.cell(r, c).value for c in range(1, ws.max_column + 1)]))
    if any(v not in (None, "") for v in rec.values()):
        rec["Код"] = str(rec["Код"] or "").strip()
        rec["Внешний код"] = str(rec["Внешний код"] or "").strip()
        rec["Артикул"] = str(rec["Артикул"] or "").strip()
        rows.append(rec)

def ms_by(field, values):
    out = {}
    for v in values:
        if not v:
            continue
        res = ms_api.get("/entity/product", {"filter": f"{field}={v}", "limit": 100})
        out[v] = res.get("rows", [])
    return out

ecs   = {r["Внешний код"] for r in rows}
by_ec   = ms_by("externalCode", ecs)
by_code = ms_by("code", {r["Код"] for r in rows})
kin     = family(ecs)

last = int(next_external_codes(1)[0]) - 1        # первый свободный минус один = последний занятый

# суффикс кода → бренд/поставщик живых карточек
sfx = {r["Код"][len(r["Внешний код"]):] for r in rows if r["Код"].startswith(r["Внешний код"])}
sfx_owner = {}
for s in sfx:
    rs = query("""SELECT left(name, 45) n FROM ms_product
                   WHERE NOT archived AND code ~ ('^[0-9]+' || %s || '$') LIMIT 200""", (s,))
    sfx_owner[s] = collections.Counter(re.sub(r"^(Тонер-|Драм-|Фото)?[Кк]артридж\w*\s*", "", x["n"])[:22]
                                       for x in rs).most_common(3)

out = [f"# Проверка шаблона МС — {SRC.split('/')[-1]}\n",
       f"Строк: {len(rows)}. " +
       (f"Поставщики: {sorted({str(r['Поставщик']) for r in rows})}. " if "Поставщик" in cols else
        "Колонки «Поставщик» в файле нет. ") +
       f"Последний занятый внешний код: **{last}**\n",
       f"Колонок в файле {len(cols)} из {len(FULL)}." +
       (f" НЕТ: {', '.join('«' + m + '»' for m in missing)}." if missing else " Полный шаблон.") +
       (f" Лишние: {', '.join(extra)}." if extra else "") + "\n"]
problems = []
for r in rows:
    code, ec, art = r["Код"], r["Внешний код"], r["Артикул"]
    tag = code or art
    out.append(f"\n## {code or '(без кода)'} / вн.код {ec or '—'} / {art}\n- {r['Наименование']}")
    live_ec = by_ec.get(ec, [])
    out.append(f"- Внешний код в МС: {len(live_ec)} карточек: " +
               ", ".join(f"{c.get('code')}({c.get('article')})" for c in live_ec[:14]))
    if not ec:
        problems.append(f"{tag}: внешнего кода нет в файле (последний занятый {last})")
    elif not live_ec:
        problems.append(f"{tag}: внешний код {ec} в МС свободен — это НОВЫЙ код (правило 25: вверх от {last})")
    # Код повторяться МОЖЕТ (правило 44): под одним кодом живут разные артикулы одного
    # поставщика. Проблема — не занятый код, а занятая ПАРА «код + артикул» и занятый артикул.
    if by_code.get(code):
        twins = [c for c in by_code[code] if not c.get("archived")]
        out.append(f"- Код занят у {len(twins)} карточек (норма, правило 44): " +
                   ", ".join(f"«{c.get('article')}»" for c in twins[:6]))
        if [c for c in twins if (c.get("article") or "") == art]:
            problems.append(f"{tag}: пара «код {code} + артикул {art}» в МС уже есть — это дубль")
    # Суффикс кода = аббревиатура ПОСТАВЩИКА (`prices/ms_import.py:CODE_SUFFIX`). Формат
    # артикула поставщика узнаваем по буквенному префиксу: если такие артикулы в МС живут
    # под ДРУГИМ суффиксом — код в шаблоне приписывает товар чужому поставщику.
    pref = re.match(r"^[A-Za-zА-Яа-я]+", art)
    my_sfx = code[len(ec):] if code.startswith(ec) else ""
    if pref and my_sfx:
        # суффикс режем по ВНЕШНЕМУ коду карточки, а не жадным '^[0-9]+': у кода `35657q`
        # внешний код `3565`, суффикс `7q`, а жадная регулярка съедала и семёрку.
        owners = query("""SELECT regexp_replace(code, '^' || external_code, '') s, count(*) n
                            FROM ms_product
                           WHERE NOT archived AND article ~ ('^' || %s)
                             AND external_code <> '' AND code LIKE external_code || '%%'
                           GROUP BY 1 ORDER BY 2 DESC""", (pref.group(0),))
        top = [(x["s"], x["n"]) for x in owners if x["s"]]
        out.append(f"- Артикулы «{pref.group(0)}…» в МС живут под суффиксами: " +
                   (", ".join(f"`{s}` ×{n}" for s, n in top[:3]) or "таких нет"))
        mine_n = dict(top).get(my_sfx, 0)
        # Единичные карточки чужого суффикса — это прошлые ошибки, а не второй поставщик:
        # считаем суффикс своим, только если он не тонет на фоне ведущего (порог ×20).
        if top and mine_n * 20 <= top[0][1] and my_sfx != top[0][0]:
            problems.append(f"{tag}: суффикс кода `{my_sfx}` — у {mine_n} карточек с артикулом "
                            f"«{pref.group(0)}…», а ведущий суффикс `{top[0][0]}` ×{top[0][1]}: "
                            f"код скорее должен быть {ec}{top[0][0]} — проверить поставщика")

    exact, near = art_clash(art)   # локальная `clash` ниже — другое, не путать
    if exact:
        problems.append(f"{tag}: артикул {art} уже на живой карточке (правило 43, уникален): "
                        + describe(exact))
    if near:
        problems.append(f"{tag}: СТОП (правило 46) — в МС живёт похожий артикул, отличается только "
                        f"регистром/буквой-двойником: {describe(near)}. Ключ {art_key(art)}. "
                        f"Решает человек: тот же товар (дубль) или правда разные артикулы поставщика")
    fam = kin.get(ec, [])
    paths = sorted({c["path"] for c in fam if c["path"]})
    b128  = sorted({c["code128"] for c in fam if c["code128"]})
    fresh = max(fam, key=lambda c: c["created"]) if fam else None
    out.append(f"- Родня: {len(fam)}; группы {paths}; Code128 {b128}; "
               f"свежайшая {fresh['code'] if fresh else '—'} "
               f"({fresh['created'].strftime('%d.%m.%Y') if fresh else '—'}) вес {fresh['weight'] if fresh else '—'}")
    out.append(f"- WB родни: {sorted({c['wb'] for c in fam if c['wb']})[:4]}")
    if "Группы" in cols and paths and r["Группы"] not in paths:
        problems.append(f"{tag}: группа «{r['Группы']}» не у родни {paths}")
    if "Штрихкод Code128" in cols and b128 and str(r["Штрихкод Code128"] or "") not in b128:
        problems.append(f"{tag}: Code128 «{r['Штрихкод Code128']}» ≠ Code128 родни {b128}")
    if "Вес" in cols and fresh and fresh["weight"] and float(r["Вес"] or 0) != float(fresh["weight"]):
        problems.append(f"{tag}: вес {r['Вес']} ≠ вес свежайшей родни {fresh['code']} = {fresh['weight']}")
    for k, v in {"Единица измерения": "шт", "НДС": 22, "Страна": "Китай",
                 "Доп. поле: Гарантия/ Срок службы": 365}.items():
        if k in cols and str(r.get(k)).strip() != str(v):
            problems.append(f"{tag}: {k} = «{r.get(k)}», ожидалось «{v}»")
    if "Доп. поле: Код поставщика" in cols and str(r.get("Доп. поле: Код поставщика") or "").strip() != art:
        problems.append(f"{tag}: «Код поставщика» = «{r.get('Доп. поле: Код поставщика')}» ≠ артикул {art}")
    # «Название WB» — поле формулы (правило 41). Есть колонка — сверяем значение с формулой,
    # нет колонки — показываем, что формула даёт, чтобы человек видел, чем закрывать пустоту.
    formula, why = wb_compose(ec, r["Наименование"])
    out.append(f"- «Название WB» по формуле: {formula or '— (' + str(why) + ')'}")
    if "Доп. поле: Название WB" in cols:
        fixed, note = wb_name(r["Доп. поле: Название WB"])
        if fixed != str(r["Доп. поле: Название WB"] or "").strip():
            problems.append(f"{tag}: «Название WB» → «{fixed}» (в файле «{r['Доп. поле: Название WB']}»)")
        if note:
            problems.append(f"{tag}: {note}")
        if formula and fixed and formula != fixed:
            problems.append(f"{tag}: «Название WB» в файле «{fixed}» ≠ формула «{formula}»")
    elif why:
        problems.append(f"{tag}: {why}")
    if "Описание" in cols and not str(r["Описание"] or "").strip():
        problems.append(f"{tag}: пустое описание")

    # «Связь» — вторые внешние коды той же универсальной модели. Берётся у родни: если родня
    # связь называет, а в шаблоне пусто — карточка выпадет из универсальной модели молча.
    link_mine = {x for x in re.split(r"[;,\s]+", str(r.get("Доп. поле: Связь") or "")) if x}
    link_known = "Доп. поле: Связь" in cols
    link_kin = collections.Counter()
    for c in live_ec:
        v = {a["name"]: a.get("value") for a in c.get("attributes", [])}.get("Связь")
        for x in re.split(r"[;,\s]+", str(v or "")):
            if x:
                link_kin[x] += 1
    if link_kin and not link_mine and link_known:
        problems.append(f"{tag}: «Связь» пуста, у родни — {dict(link_kin)}; заполнить")
    elif link_kin and link_mine - set(link_kin):
        problems.append(f"{tag}: «Связь» {sorted(link_mine)} ≠ связь родни {dict(link_kin)}")
    for other in link_mine:
        if not by_ec.get(other) and not ms_api.get("/entity/product",
                                                   {"filter": f"externalCode={other}", "limit": 1}).get("rows"):
            problems.append(f"{tag}: «Связь» {other} — такого внешнего кода в МС нет")

    # шесть признаков (правило 30) — наша строка против КАЖДОЙ карточки родни
    mine = signs_of(r["Наименование"], art, r.get("Доп. поле: Название WB"))
    tally = {s: {True: 0, False: 0, None: 0} for s in SIGNS}
    clash = []
    for c in live_ec:
        wb = {a["name"]: a.get("value") for a in c.get("attributes", [])}.get("Название WB")
        flags = compare(mine, signs_of(c.get("name") or "", c.get("article"), wb))
        for s in SIGNS:
            tally[s][flags[s]] += 1
        bad = [FEATURE_NAMES[s] for s in SIGNS if flags[s] is False]
        if bad:
            clash.append(f"{c.get('code')} — {', '.join(bad)} — {(c.get('name') or '')[:70]}")
    if live_ec:
        out.append("- 6 признаков (да/нет/молчит по родне): " +
                   " · ".join(f"{FEATURE_NAMES[s]} {tally[s][True]}/{tally[s][False]}/{tally[s][None]}" for s in SIGNS))
        for c in clash:
            out.append(f"  - расходится: {c}")
        for s in SIGNS:
            # признак противоречит ВСЕЙ родне — это не разнобой названий, это другой товар
            if tally[s][False] and not tally[s][True]:
                problems.append(f"{tag}: {FEATURE_NAMES[s]} противоречит ВСЕЙ родне ({tally[s][False]} карточек)")
            elif tally[s][False]:
                problems.append(f"{tag}: {FEATURE_NAMES[s]} расходится с {tally[s][False]} из {len(live_ec)} родни — глазами")
        # чип: обоюдное молчание = совпадение (решение 10.08). Просим заполнить только там,
        # где родня про чип ГОВОРИТ, а в шаблоне пусто — это и есть «вводим чип в шаблон».
        if mine["chip"] is None and tally["chip_ok"][None]:
            said = sorted({F.CHIP_NAMES[c] for c in
                           (signs_of(x.get("name") or "", x.get("article"),
                                     {a["name"]: a.get("value") for a in x.get("attributes", [])}.get("Название WB"))["chip"]
                            for x in live_ec) if c})
            problems.append(f"{tag}: чип в шаблоне не указан, у родни — {', '.join(said)}; заполнить")

out.append("\n## Суффиксы кода — кто их носит в МС\n")
for s, top in sfx_owner.items():
    out.append(f"- `{s}` → " + (", ".join(f"«{n}» ×{c}" for n, c in top) or "НЕТ таких карточек"))
out.append("\n## Все поля строк\n")
for r in rows:
    out.append(f"\n### {r['Код'] or '(без кода)'} — {r['Наименование']}")
    out += [f"- **{k}**: {r[k]}" for k in hdr]
if missing:
    problems.append("в файле НЕТ колонок: " + ", ".join(f"«{m}»" for m in missing) +
                    " — при загрузке эти поля останутся пустыми (правило 41: 11 обязательных полей)")
out.append("\n## Замечания\n")
out += [f"- {p}" for p in problems] or ["- нет"]
open(OUT, "w", encoding="utf-8").write("\n".join(out))
print(f"строк {len(rows)}, замечаний {len(problems)}, последний занятый вн.код {last}")
print("файл:", OUT)
for p in problems:
    print(" *", p)
