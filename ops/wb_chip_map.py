#!/usr/bin/env python3
# поток: mkt
"""ops/wb_chip_map.py — чип каждой карточки ВБ по правилу Сергея (22.09.2026):
на один внешний код обычно 10+ товаров поставщиков, и у них чип прописан чётко. Поэтому главный источник —
названия ВСЕХ товаров МойСклада этого внешнего кода (+ строки прайсов поставщиков):
  кто-то пишет «чип без счётчика»  → «чип без счётчика»
  кто-то пишет «с чипом»           → «с чипом»
  кто-то пишет «без чипа»          → «без чипа»
  все молчат, товаров ≥ 3          → «без чипа»   (у Q2612A 39 товаров — ни слова, чипа у модели нет)
  все молчат, товаров < 3          → TheCartridge, иначе «неизвестно» (у новых кодов 1–2 поставщика — молчание не довод)
  одни «с чипом», другие «без»     → «противоречие» — к одному коду привязан чужой товар, это ошибка МС
Сверка с карточкой ВБ («Комплектация», описание) и с TheCartridge — отдельными колонками, в список проблем.
Результат: docs/reports/mkt_wb_chip_latest.json  {"acc|nm": [чип, источник]} и problems CSV по аккаунту.
  ./venv/bin/python -m ops.wb_chip_map --account wb_acc1
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

REP = BASE_DIR / "docs" / "reports"


def chip_of(s):
    s = (s or "").lower().replace("ё", "е")
    if re.search(r"не\s+требуется\s+чип|чип\s+не\s+требуется", s):
        return "без чипа"
    if re.search(r"без\s*сч[её]тчик", s):
        return "чип без счётчика"
    if re.search(r"без\s+чип|б/чип|без\s*chip|no\s*chip|chipless", s):
        return "без чипа"
    if re.search(r"(\bс|\bc)\s+чип|\bчип\b|чипом|with\s*chip|\bchip\b", s):
        return "с чипом"
    return None


def ch(p, n):
    for c in p.get("characteristics") or []:
        if c.get("name") == n:
            v = c.get("value")
            return " ".join(map(str, v)) if isinstance(v, list) else v
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="wb_acc1", choices=["wb_acc1", "wb_acc2"])
    a = ap.parse_args()
    names = collections.defaultdict(list); msid2ec = {}
    for r in db.query("select ms_id, code, name, external_code from ms_product where external_code is not null"):
        names[r["external_code"]].append((r["code"] or "", r["name"] or ""))
        msid2ec[r["ms_id"]] = r["external_code"]
    for r in db.query("select distinct ms_id, name from prc_price_row where ms_id is not null"):
        ec = msid2ec.get(r["ms_id"])
        if ec:
            names[ec].append(("прайс", r["name"] or ""))
    tc = {r["external_code"]: {"chip": "с чипом", "chip_free": "чип без счётчика", "nochip": "без чипа"}.get(r["chip"])
          for r in db.query("select external_code, chip from prc_tc_model")}
    code2ext = {r["code"]: r["external_code"] for r in db.query("select code, external_code from prc_tc_code")}

    def consensus(ec):
        L = names.get(ec, [])
        marks = collections.Counter(chip_of(n) for _, n in L)
        marks.pop(None, None)
        if not L:
            return "?", "кода нет в МС", 0, marks
        if marks.get("с чипом") and marks.get("без чипа"):
            return "противоречие", "поставщики расходятся", len(L), marks
        for k in ("чип без счётчика", "с чипом", "без чипа"):
            if marks.get(k):
                return k, f"поставщики ({marks[k]} из {len(L)})", len(L), marks
        if tc.get(ec):
            return tc[ec], f"поставщики молчат ({len(L)} товаров), взято из TheCartridge", len(L), marks
        if len(L) >= 3:
            return "без чипа", f"поставщики молчат ({len(L)} товаров) — модель без чипа", len(L), marks
        return "?", f"поставщиков мало ({len(L)}) и все молчат, в TheCartridge пусто", len(L), marks

    out = {}; prob = []; stat = collections.Counter()
    for r in db.query("""select distinct on (nm_id) nm_id, vendor_code, payload from raw_wb_card_content
                          where account=%s order by nm_id, collected_at desc""", (a.account,)):
        p = r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"])
        m = re.match(r"^(\d+)", r["vendor_code"] or "")
        code = m.group(1) if m else ""
        ec = code2ext.get(code) or (code if code in names else code[:4])
        chip, src, n, marks = consensus(ec)
        out[f"{a.account}|{r['nm_id']}"] = [chip, src]
        stat[chip] += 1
        kit = chip_of(ch(p, "Комплектация"))
        d = (p.get("description") or "").lower()
        mm = re.search(r"не\s+требуется\s+чип|(c|с)\s+чип\w*\s+без\s+сч[её]тчик\w*|без\s+чип\w*|(c|с)\s+чип\w*", d)
        desc = chip_of(mm.group(0)) if mm else None
        t = (p.get("title") or "")[:90]
        link = f"https://www.wildberries.ru/catalog/{r['nm_id']}/detail.aspx"
        base = {"nm_id": r["nm_id"], "ссылка": link, "артикул": r["vendor_code"], "внешний_код": ec, "название": t,
                "чип_по_поставщикам": chip, "основание": src,
                "поставщики_с_чипом": marks.get("с чипом", 0), "поставщики_без_чипа": marks.get("без чипа", 0),
                "поставщики_без_счётчика": marks.get("чип без счётчика", 0)}
        if chip == "противоречие":
            prob.append(dict(base, проблема="поставщики расходятся — к коду привязан товар с другим чипом", на_ВБ=kit or desc or ""))
            stat["П: поставщики расходятся"] += 1
            continue
        if chip == "?":
            continue
        for fld, val in (("«Комплектация» на ВБ", kit), ("описание на ВБ", desc)):
            if val and val != chip:
                prob.append(dict(base, проблема=f"{fld}: «{val}», а по поставщикам «{chip}»", на_ВБ=val))
                stat[f"П: {fld} ≠ поставщики"] += 1
        if tc.get(ec) and tc[ec] != chip:
            prob.append(dict(base, проблема=f"TheCartridge: «{tc[ec]}», а по поставщикам «{chip}»", на_ВБ=kit or desc or ""))
            stat["П: TheCartridge ≠ поставщики"] += 1
    (REP / "mkt_wb_chip_latest.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    f = REP / f"mkt_wb_chip_problems_{datetime.date.today():%Y-%m-%d}_{a.account}.csv"
    with open(f, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(prob[0].keys()), delimiter=";")
        w.writeheader(); w.writerows(prob)
    for k, v in stat.most_common():
        print(f"  {v:6}  {k}")
    print("файлы:", "mkt_wb_chip_latest.json,", f.name)


if __name__ == "__main__":
    main()
