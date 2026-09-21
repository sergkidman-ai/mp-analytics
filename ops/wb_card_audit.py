#!/usr/bin/env python3
# поток: mkt
"""ops/wb_card_audit.py — проверка карточек ВБ на несоответствия и опечатки. ТОЛЬКО ЧТЕНИЕ.

ЗАЧЕМ. При подборе склеек (сентябрь 2026) всплыли ошибки в самих карточках: шаблон описания
«не требуется чип» у картриджей с чипом, «C070H» вместо 070H, Canon 712 с цветом «голубой» в склейке
HP, код МС с чужим названием. Прогон сверяет каждую карточку с первоисточником — каталогом
TheCartridge (`prc_tc_model`, одна запись на внешний код) — и сам с собой (название ↔ характеристики).

ПРОВЕРКИ (каждая — свой тип строки в отчёте):
  чип        — ВБ («Комплектация», затем описание) ≠ TheCartridge; «Комплектация» ≠ описание
  модель     — поле «Модель» ≠ модель TheCartridge (без пробелов, №, регистра)
  цвет       — поле «Цвет» ≠ цвет TheCartridge; поле «Цвет» ≠ цвет в названии
  ресурс     — «Максимальный ресурс» отличается от TheCartridge больше чем на 15 % (кроме бандлов и наборов)
  принтеры   — ни один принтер из «Совместимость» не совпал с TheCartridge
  количество — «N шт» в названии ≠ «Количество предметов в упаковке»
  МС         — название товара МойСклада по коду не содержит модель TheCartridge
  склейка    — в одной склейке позиции для принтеров разных брендов
Связь карточки с ТК: код (цифры в начале нашего артикула) → prc_tc_code → внешний код; иначе первые 4 цифры.

  ./venv/bin/python -m ops.wb_card_audit --account wb_acc1
Результат: docs/reports/mkt_wb_card_audit_<дата>_<акк>.csv и сводка в консоль.
"""
import sys
import re
import csv
import json
import argparse
import datetime
import pathlib
import collections

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

COLW = [("светло-голуб", "LC"), ("светло-пурпур", "LM"), ("черн", "K"), ("black", "K"), ("голуб", "C"), ("cyan", "C"),
        ("пурпур", "M"), ("малин", "M"), ("magenta", "M"), ("желт", "Y"), ("жёлт", "Y"), ("yellow", "Y"),
        ("набор", "SET"), ("трехцвет", "CL"), ("трёхцвет", "CL"), ("цветн", "CL"), ("сер", "GY")]
TC_COL = {"BK": "K", "K": "K", "C": "C", "M": "M", "Y": "Y", "LC": "LC", "LM": "LM", "CMYK": "SET", "CMY": "CL",
          "COLOR": "CL", "GY": "GY", "PBK": "K", "MBK": "K"}
TC_CHIP = {"chip": "с чипом", "chip_free": "чип без счётчика", "nochip": "без чипа"}


COLRX = [(r"светло-голуб", "LC"), (r"светло-пурпур", "LM"), (r"\bч[её]рн(ый|ая|ое|ые|ого)\b|\bblack\b", "K"),
         (r"\bголуб", "C"), (r"\bcyan\b", "C"), (r"\bпурпур", "M"), (r"\bмалин", "M"), (r"\bmagenta\b", "M"),
         (r"\bж[её]лт", "Y"), (r"\byellow\b", "Y"), (r"\bнабор", "SET"), (r"тр[её]хцвет", "CL"), (r"\bцветн", "CL"), (r"\bсер(ый|ая)\b", "GY")]


def colour(s):
    s = (s or "").lower()
    return next((k for w, k in COLRX if re.search(w, s)), None)


def chip_of(s):
    s = (s or "").lower().replace("ё", "е")
    if re.search(r"не\s+требуется\s+чип|чип\s+не\s+требуется", s):
        return "чип не требуется"
    if re.search(r"без\s*сч[её]тчик", s):
        return "чип без счётчика"
    if re.search(r"без\s+чип", s):
        return "без чипа"
    if re.search(r"(\bс|\bc)\s+чип|\bчип\b|чипом", s):
        return "с чипом"
    return None


def norm(s):
    return re.sub(r"[\s№\-_/.,+]", "", (s or "").upper())


def pnorm(s):
    return re.sub(r"[\s\-_]", "", (s or "").lower())


def ch(p, n):
    for c in p.get("characteristics") or []:
        if c.get("name") == n:
            return c.get("value")
    return None


def first(v):
    return (v[0] if isinstance(v, list) and v else v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="wb_acc1", choices=["wb_acc1", "wb_acc2"])
    a = ap.parse_args()
    acc = a.account
    tc = {r["external_code"]: r for r in db.query(
        """select external_code, title, additional_title, color, resource, chip, brand, printer_models from prc_tc_model""")}
    code2ext = {r["code"]: r["external_code"] for r in db.query("select code, external_code from prc_tc_code")}
    ms = {r["external_code"]: r["name"] or "" for r in db.query("select external_code, name from ms_product where external_code is not null")}
    sold = collections.Counter()
    for r in db.query("""select article, sum(qty) q from sales where platform='wb' and account=%s and period_from>='2026-06-01'
                          and qty>0 and article ~ '^[0-9]+$' group by 1""", (acc,)):
        sold[int(r["article"])] = float(r["q"])
    rows = []; cnt = collections.Counter(); checked = collections.Counter()
    groups = collections.defaultdict(list)

    def add(kind, c, wb, ref, src, note=""):
        cnt[kind] += 1
        rows.append({"тип": kind, "nm_id": c["nm"], "ссылка": f"https://www.wildberries.ru/catalog/{c['nm']}/detail.aspx",
                     "артикул": c["vc"], "продаж_с_0106": c["sold"], "на_ВБ": wb, "эталон": ref, "источник_эталона": src,
                     "примечание": note, "название": c["t"][:100]})

    for r in db.query("""select distinct on (nm_id) nm_id, vendor_code, payload from raw_wb_card_content
                          where account=%s order by nm_id, collected_at desc""", (acc,)):
        p = r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"])
        t = p.get("title") or ""; tl = t.lower()
        m = re.match(r"^(\d+)", r["vendor_code"] or "")
        code = m.group(1) if m else None
        ext = (code2ext.get(code) or (code if code in tc else None) or (code[:4] if code and code[:4] in tc else None)) if code else None
        ref = tc.get(ext) if ext else None
        c = {"nm": r["nm_id"], "vc": r["vendor_code"], "t": t, "sold": int(sold[r["nm_id"]])}
        qty_t = re.search(r"(\d{1,2})\s*шт", tl)
        qty_c = first(ch(p, "Количество предметов в упаковке"))
        try:
            qty_c = int(str(qty_c).strip())
        except Exception:
            qty_c = None
        is_multi = bool(qty_t and int(qty_t.group(1)) > 1) or (qty_c or 1) > 1 or tl.startswith(("картриджи", "комплект", "набор"))
        model = str(first(ch(p, "Модель")) or "")
        printer_card = bool(re.match(r"^(картридж|тонер-картридж|фотобарабан|чернила|тонер|лента для принтера|лента|красящая лента)\s+(ds\s+)?для\b", tl))
        # сломанная кодировка (UTF-8, прочитанный как cp1251: «РЎ287» вместо «С287»)
        blob = " ".join([t, p.get("description") or ""] + [" ".join(map(str, v)) if isinstance(v, list) else str(v)
                                                           for v in (x.get("value") for x in p.get("characteristics") or [])])
        bad = re.findall(r"[РС][ЂЃЄЅІЇЈЉЊЋЌЎЏђѓєѕіїјљњћќўџ]\S*", blob)
        if bad:
            add("кодировка: испорченный текст", {"nm": r["nm_id"], "vc": r["vendor_code"], "t": t, "sold": int(sold[r["nm_id"]])},
                ", ".join(sorted(set(bad))[:3]), "", "карточка ВБ")
        colc = colour(str(first(ch(p, "Цвет картриджа/чернил")) or ""))
        compat = ch(p, "Совместимость картриджа") or []
        compat = [x for x in (compat if isinstance(compat, list) else [compat]) if x]
        groups[p.get("imtID")].append((c, compat))
        # количество
        checked["количество"] += 1
        if qty_t and qty_c and int(qty_t.group(1)) != qty_c:
            add("количество: название ≠ характеристика", c, f"{qty_t.group(1)} шт в названии", f"{qty_c} в характеристике", "карточка ВБ")
        # цвет название ↔ характеристика
        colt = colour(re.sub(r"^.*?\bдля\b", "", t, count=1) if " для " in t else t)
        if colc and colt and colc != colt and not is_multi and "SET" not in (colc, colt):
            checked["цвет"] += 1
            add("цвет: название ≠ характеристика", c, f"в названии {colt}", f"в характеристике {colc}", "карточка ВБ")
        # чип внутри карточки
        kit = chip_of(" ".join(ch(p, "Комплектация") or []) if isinstance(ch(p, "Комплектация"), list) else ch(p, "Комплектация"))
        d = (p.get("description") or "").lower()
        mm = re.search(r"не\s+требуется\s+чип|(c|с)\s+чип\w*\s+без\s+сч[её]тчик\w*|без\s+чип\w*|(c|с)\s+чип\w*", d)
        desc = chip_of(mm.group(0)) if mm else None
        if kit and desc and kit != desc:
            add("чип: комплектация ≠ описание", c, f"комплектация «{kit}»", f"описание «{desc}»", "карточка ВБ")
        if not ref:
            cnt["нет в TheCartridge (не с чем сверить)"] += 1
            continue
        # чип против ТК
        tcc = TC_CHIP.get(ref["chip"])
        wbc = kit or desc
        if tcc and wbc:
            checked["чип"] += 1
            if wbc != tcc and not (wbc == "чип не требуется" and tcc == "без чипа"):
                add("чип: ВБ ≠ TheCartridge", c, wbc, tcc, "TheCartridge", "из комплектации" if kit else "из описания")
        # модель (у карточек «по принтеру» в поле «Модель» имя принтера — так заведены все, считаем справкой)
        if printer_card:
            cnt["справка: карточка по принтеру, в поле «Модель» имя принтера"] += 1
        elif model and ref["title"] and not is_multi:
            checked["модель"] += 1
            a_, b_ = norm(model), norm(ref["title"])
            a2 = re.sub(r"(BK|K|C|M|Y)$", "", a_); b2 = re.sub(r"(BK|K|C|M|Y)$", "", b_)
            alt = norm(ref.get("additional_title") or "")
            if a_ != b_ and a2 != b2 and a_ != alt and not (a_ in b_ or b_ in a_):
                add("модель: поле «Модель» ≠ TheCartridge", c, model, ref["title"], "TheCartridge",
                    f"доп. название ТК: {ref.get('additional_title') or ''}")
        # цвет против ТК
        tcol = TC_COL.get(str(ref["color"] or "").upper())
        if tcol and colc and not is_multi:
            checked["цвет"] += 1
            if tcol != colc and not (tcol == "CL" and colc in ("C", "M", "Y", "CL")):
                add("цвет: ВБ ≠ TheCartridge", c, str(first(ch(p, "Цвет картриджа/чернил"))), str(ref["color"]), "TheCartridge")
        # ресурс
        try:
            res = int(float(str(first(ch(p, "Максимальный ресурс"))).replace(" ", "")))
        except Exception:
            res = None
        if res and ref["resource"] and not is_multi:
            checked["ресурс"] += 1
            if abs(res - ref["resource"]) / ref["resource"] > 0.15:
                add("ресурс: ВБ ≠ TheCartridge", c, res, ref["resource"], "TheCartridge", f"{100*(res/ref['resource']-1):+.0f} %")
        # принтеры
        pm = ref["printer_models"] if isinstance(ref["printer_models"], list) else json.loads(ref["printer_models"] or "[]")
        tset = {pnorm(f"{x.get('brand','')} {x.get('title','')}") for x in pm} | {pnorm(x.get("title", "")) for x in pm}
        if compat and tset:
            checked["принтеры"] += 1
            tok = lambda z: {x for x in re.findall(r"[a-zа-я]*\d+[a-zа-я]*", z.lower().replace("-", "")) if len(x) >= 2}
            ttok = set().union(*(tok(f"{x.get('title','')}") for x in pm)) if pm else set()
            wn = {pnorm(x) for x in compat}
            hit = (wn & tset) or {w for w in compat for a_ in tok(w) for b_ in ttok
                                  if a_ == b_ or (len(a_) >= 4 and len(b_) >= 4 and (a_.startswith(b_) or b_.startswith(a_)))}
            if not hit:
                add("принтеры: ни один не совпал с TheCartridge" + (" (карточка по принтеру)" if printer_card else ""), c, "; ".join(compat[:4]),
                    "; ".join(f"{x.get('brand')} {x.get('title')}" for x in pm[:4]), "TheCartridge")
    # склейки со смешанными брендами принтеров
    BR = ("hp", "canon", "kyocera", "brother", "samsung", "xerox", "ricoh", "panasonic", "konica", "oki", "lexmark",
          "sharp", "epson", "pantum", "toshiba", "sindoh", "katusha", "minolta")
    for imt, mem in groups.items():
        if len(mem) < 2:
            continue
        brands = {}
        for c, compat in mem:
            b = {next((x for x in BR if pnorm(p_).startswith(x)), None) for p_ in compat} - {None}
            if b:
                brands[c["nm"]] = b
        allb = collections.Counter(x for b in brands.values() for x in b)
        if len(allb) > 1:
            main = allb.most_common(1)[0][0]
            for c, compat in mem:
                if c["nm"] in brands and main not in brands[c["nm"]]:
                    add("склейка: позиция для другого бренда принтеров", c, ", ".join(sorted(brands[c["nm"]])),
                        f"в склейке {imt} большинство — {main}", "склейка ВБ")
    out = BASE_DIR / "docs" / "reports" / f"mkt_wb_card_audit_{datetime.date.today():%Y-%m-%d}_{acc}.csv"
    with open(out, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()), delimiter=";")
        w.writeheader()
        w.writerows(sorted(rows, key=lambda x: (x["тип"], -x["продаж_с_0106"])))
    print(f"{acc}: проверено карточек {len(set(r_['nm_id'] for r_ in rows)) and sum(1 for _ in groups.values())} групп; строк с замечаниями {len(rows)}")
    print("проверок выполнено:", dict(checked))
    for k, v in cnt.most_common():
        print(f"  {v:6}  {k}")
    print("файл:", out.name)


if __name__ == "__main__":
    main()
