# поток: inv
"""ГТД из письма текстом → проставить в уже созданную приёмку МойСклад.

    python gtd_text.py письмо.txt            # разбор и сверка, в МС не пишем
    python gtd_text.py письмо.txt --apply    # записать

У Солюшнс принта («Спринт») номера ГТД в УПД не приходят: раньше был отдельный Excel-реестр
(`upd_to_supply.parse_sprint_gtd`), с августа 2026 сменился менеджер и реестр присылают
текстом в теле письма. Формат — три поля на позицию: код КИС, наименование, номер ГТД.
Разбираем не по позиции в строке, а по форме: номер ГТД узнаётся регуляркой, код КИС —
чисто числовая строка. Поэтому шапка «Код товара КИС / Наименование / № ГТД», пустые строки
и перестановка полей внутри блока не ломают разбор.

Приёмку НЕ создаём: к моменту письма она уже заведена. Пишем только `gtd` в позиции и
«последний ГТД» / «Код поставщика» в карточку товара — количества, цены и суммы не трогаем.
"""
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import invoice_to_po as inv                       # get_r, meta
from ms import put
from upd_to_supply import CODE_ATTR, GTD_ATTR, _attr_meta

# Номер ГТД: 8/6/7 цифр, иногда с номером товарной подпозиции («…/0077532/1»).
GTD_RE = re.compile(r"\b\d{8}/\d{6}/\d{7}(?:/\d+)?\b")
KIS_RE = re.compile(r"^\d{4,8}$")
# Номер приёмки МС первой строкой: «328220/И». Пробелы по краям режем, регистр не трогаем.
SUPPLY_RE = re.compile(r"^[\w\-]{3,20}/[А-ЯЁA-Z]{1,3}$")
# Артикул товара Спринта в МС: SP<код КИС>_MSK. Суффикс склада может отличаться — не привязываемся.
ART_RE = re.compile(r"SP(\d+)")


def parse_text(text):
    """Текст письма → (номер приёмки | None, [{kis, name, gtd}], [замечания])."""
    lines = [l.strip() for l in (text or "").splitlines()]
    lines = [l for l in lines if l]
    number, notes = None, []
    if lines and SUPPLY_RE.match(lines[0]) and not KIS_RE.match(lines[0]):
        number = lines.pop(0)

    rows, cur = [], {}
    for line in lines:
        m = GTD_RE.search(line)
        if m:
            # Строка таблицы целиком («246633  Картридж…  10720010/…») — почта иногда так и
            # склеивает; тогда код и наименование берём из неё же, а не из предыдущих строк.
            one = re.match(r"^(\d{4,8})\s+(.*)$", line[:m.start()].strip())
            if one:
                cur = {"kis": one.group(1), "name": one.group(2).strip()}
            cur["gtd"] = m.group(0)
            if cur.get("kis"):
                rows.append(cur)
            else:                                  # ГТД без кода КИС — вручную не угадываем
                notes.append(f"ГТД {m.group(0)} без кода КИС — пропущен")
            cur = {}
        elif KIS_RE.match(line):
            if cur.get("kis"):
                notes.append(f"код КИС {cur['kis']} без номера ГТД — пропущен")
            cur = {"kis": line, "name": ""}
        elif cur.get("kis"):
            cur["name"] = (cur.get("name") + " " + line).strip()
    if cur.get("kis"):
        notes.append(f"код КИС {cur['kis']} без номера ГТД — пропущен")
    return number, rows, notes


def find_supply(number):
    flt = urllib.parse.quote(f"name={number}", safe="=")    # номер кириллический → percent-encoding
    return inv.get_r(f"/entity/supply?filter={flt}&expand=agent&limit=5").get("rows", [])


def apply_rows(number, rows, apply=False):
    """Сверка (и запись при apply=True). Возвращает отчёт словарём, ничего не печатает."""
    res = {"number": number, "rows": len(rows), "hits": [], "miss": [], "extra": [],
           "warns": [], "written": 0, "cards": 0, "applied": apply}
    found = find_supply(number)
    if len(found) != 1:
        res["error"] = (f"приёмок с номером «{number}» найдено {len(found)} — "
                        f"уточните номер" if found else f"приёмка «{number}» не найдена")
        return res

    sup = found[0]
    res["supply"] = {"id": sup["id"], "name": sup["name"], "moment": (sup.get("moment") or "")[:10],
                     "agent": (sup.get("agent") or {}).get("name", "?"),
                     "sum": (sup.get("sum") or 0) / 100}
    res["url"] = "https://online.moysklad.ru/app/#supply/edit?id=" + sup["id"]

    pos = inv.get_r(f"/entity/supply/{sup['id']}/positions?expand=assortment&limit=1000").get("rows", [])
    by_kis, service = {}, 0
    for p in pos:
        a = p.get("assortment") or {}
        m = ART_RE.search((a.get("article") or "") + " " + (a.get("code") or ""))
        if m:
            by_kis.setdefault(m.group(1), []).append(p)
        else:
            service += 1                           # доставка и прочие услуги — ГТД им не нужен
    res["service"] = service

    seen = set()
    for r in rows:
        queue = by_kis.get(r["kis"])
        if not queue:
            res["miss"].append(r)
            continue
        seen.add(r["kis"])
        for p in queue:
            a = p["assortment"]
            was = (p.get("gtd") or {}).get("name")
            hit = {"kis": r["kis"], "art": a.get("article") or a.get("code") or "",
                   "name": a.get("name") or r["name"], "qty": p["quantity"],
                   "was": was, "now": r["gtd"], "pid": p["id"], "aid": a["id"]}
            res["hits"].append(hit)
            if not apply or was == r["gtd"]:
                continue
            # Решение Сергея 10.08.2026: письмо — источник истины, прежний номер перезаписываем,
            # но в отчёте показываем «было → стало», чтобы правку было видно.
            st, resp = put(f"/entity/supply/{sup['id']}/positions/{p['id']}",
                           {"gtd": {"name": r["gtd"]}})
            if st not in (200, 201):
                res["warns"].append(f"позиция {a.get('article') or p['id'][:8]}: HTTP {st}")
                continue
            res["written"] += 1
            card = {"attributes": [{"meta": _attr_meta(GTD_ATTR), "value": r["gtd"]},
                                   {"meta": _attr_meta(CODE_ATTR), "value": r["kis"]}]}
            st, _ = put(f"/entity/product/{a['id']}", card)
            if st in (200, 201):
                res["cards"] += 1

    res["extra"] = sorted(set(by_kis) - seen)      # в приёмке есть, в письме нет
    return res


def format_report(res):
    if res.get("error"):
        return "❌ " + res["error"]
    s = res["supply"]
    head = (f"{'Записал' if res['applied'] else 'Сверка (ничего не записано)'}: приёмка "
            f"{s['name']} от {s['moment']} · {s['agent']} · {s['sum']:.2f} ₽")
    changed = [h for h in res["hits"] if h["was"] != h["now"]]
    same = len(res["hits"]) - len(changed)
    out = [head, f"строк в письме {res['rows']}, легло на позиции {len(res['hits'])}, "
                 f"совпадало и так {same}, услуг без ГТД {res['service']}"]
    for h in changed[:40]:
        was = f"было {h['was']} → " if h["was"] else ""
        out.append(f"• {h['art']} {h['name'][:45]} ×{h['qty']:g}: {was}{h['now']}")
    if len(changed) > 40:
        out.append(f"…и ещё {len(changed) - 40}")
    for r in res["miss"]:
        out.append(f"✗ код {r['kis']} ({r['name'][:40]}) — в приёмке нет такой позиции")
    if res["extra"]:
        out.append(f"⚠️ в приёмке есть, в письме нет: {', '.join(res['extra'])}")
    for w in res["warns"]:
        out.append("⚠️ " + w)
    if res["applied"]:
        out.append(f"обновлено позиций {res['written']}, карточек товара {res['cards']}")
    out.append(res["url"])
    return "\n".join(out)


def process(text, apply=False):
    number, rows, notes = parse_text(text)
    if not number:
        return {"error": "Первой строкой нужен номер приёмки МойСклад, например «328220/И». "
                         "Ниже — код КИС, наименование и номер ГТД по каждой позиции."}
    if not rows:
        return {"error": f"Номер приёмки «{number}» вижу, а номеров ГТД в тексте нет."}
    res = apply_rows(number, rows, apply=apply)
    res["warns"] = notes + res.get("warns", [])
    return res


def main():
    args = [a for a in sys.argv[1:] if a]
    if not args:
        print("Использование: python gtd_text.py <файл с текстом письма> [--apply]")
        return
    with open(args[0], encoding="utf-8") as f:
        text = f.read()
    print(format_report(process(text, apply="--apply" in args)))


if __name__ == "__main__":
    main()
