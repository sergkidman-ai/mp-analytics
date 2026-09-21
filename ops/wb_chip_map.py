#!/usr/bin/env python3
# поток: mkt
"""One chip value per WB card: WB «Комплектация» → WB description → TheCartridge (prc_tc_model) → MoySklad name →
supplier names (only if they agree). Also records conflicts between sources. Output chipmap.json."""
import sys, re, json, collections
sys.path.insert(0, "/opt/mp-analytics")
from core import db
SC = "/opt/mp-analytics/docs/reports/"


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


def ch(p, n):
    for c in p.get("characteristics") or []:
        if c.get("name") == n:
            v = c.get("value"); return " ".join(v) if isinstance(v, list) else v
    return None


TC = {r["external_code"]: {"chip": "с чипом", "chip_free": "чип без счётчика", "nochip": "без чипа"}.get(r["chip"])
      for r in db.query("select external_code, chip from prc_tc_model where chip is not null")}
MS = {r["external_code"]: chip_of(r["name"]) for r in db.query("select external_code, name from ms_product where external_code is not null")}
SUP = collections.defaultdict(set)
for r in db.query("""select distinct external_code, name from supplier_stock where captured_at > now() - interval '10 days'
                      and external_code is not null"""):
    c = chip_of(r["name"])
    if c:
        SUP[r["external_code"]].add(c)
out = {}; src = collections.Counter(); conflict = collections.Counter(); ex_conf = []
for r in db.query("""select distinct on (account, nm_id) account, nm_id, vendor_code, payload from raw_wb_card_content
                      order by account, nm_id, collected_at desc"""):
    p = r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"])
    m = re.match(r"^(\d+)", r["vendor_code"] or "")
    code = m.group(1)[:4] if m else None
    kit = chip_of(ch(p, "Комплектация"))
    d = (p.get("description") or "").lower()
    mm = re.search(r"не\s+требуется\s+чип|(c|с)\s+чип\w*\s+без\s+сч[её]тчик\w*|без\s+чип\w*|(c|с)\s+чип\w*", d)
    desc = chip_of(mm.group(0)) if mm else None
    tc = TC.get(code) if code else None
    ms = MS.get(code) if code else None
    sp = SUP.get(code, set()) if code else set()
    sup = next(iter(sp)) if len(sp) == 1 else None
    val, s = next(((v, n) for v, n in ((kit, "ВБ комплектация"), (tc, "TheCartridge"), (desc, "ВБ описание"),
                                         (ms, "МойСклад"), (sup, "поставщики")) if v), (None, "нигде нет"))
    wb = kit or desc
    if wb and tc and wb != tc:
        conflict[f"ВБ «{wb}» ≠ TheCartridge «{tc}»"] += 1
        if len(ex_conf) < 8:
            ex_conf.append((r["nm_id"], r["vendor_code"], wb, tc, (p.get("title") or "")[:60]))
    if kit and desc and kit != desc:
        conflict["комплектация ≠ описание (внутри карточки ВБ)"] += 1
    src[s] += 1
    out[f"{r['account']}|{r['nm_id']}"] = [val or "?", s]
json.dump(out, open(SC + "mkt_wb_chip_latest.json", "w", encoding="utf-8"), ensure_ascii=False)
print("источник чипа:", dict(src))
print("расхождения:", dict(conflict))
for e in ex_conf:
    print("  ", e)
