# поток: gab
"""Разрез покрытия габаритами ПО ПРОДАЖАМ за 12 месяцев (авг 2025 — июл 2026).

Что считает:
  1) карточки хотя бы с одной продажей за год + их выручка;
  2) из них покрытые (есть реальный короб → лист «К загрузке»/волны) — в карточках и в доле выручки;
  3) продающиеся БЕЗ источника — в карточках и в доле выручки;
  4) docs/selling_uncovered.csv — артикул, название, шт/год, выручка/год, OEM-код, сорт. по выручке;
  5) сколько УНИКАЛЬНЫХ моделей-матерей стоит за непокрытыми продажами + накопительный итог
     выручки (сколько моделей закрывают 50 / 80 / 95 % непокрытой выручки).

Источники: sales (wb, помесячно) + wb_cards + docs/FINAL_gabarity.xlsx.
Ничего никуда не отправляет, в БД не пишет — только читает и кладёт CSV.
"""
import sys
import csv
import pathlib
from collections import defaultdict

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "tools"))

import openpyxl  # noqa: E402
from core import db  # noqa: E402
from gab_sp_title_verify import card_models  # noqa: E402

FROM, TO = "2025-08-01", "2026-07-31"
OUT = BASE / "docs/selling_uncovered.csv"
ACC = {"wb_acc1": "Цифровой квадрат", "wb_acc2": "Дисквэр"}
WRITE = True


def sheet(path, name):
    ws = openpyxl.load_workbook(path, read_only=True)[name]
    it = ws.iter_rows(values_only=True)
    head = next(it)
    hi = {v: i for i, v in enumerate(head)}
    return [r for r in it if any(x is not None for x in r)], hi


def main():
    # ---------- продажи за 12 месяцев ----------
    rows = db.query("""
        select article, account, sum(qty) qty, sum(revenue_buyer) rev,
               count(*) months, min(period_from) f, max(period_to) t
        from sales
        where platform='wb' and period_from >= %s and period_to <= %s
        group by article, account""", (FROM, TO))
    months = db.query("""select distinct period_from, period_to from sales
        where platform='wb' and period_from >= %s and period_to <= %s
        order by 1""", (FROM, TO))
    print(f"=== ОКНО: {FROM} … {TO}, месяцев в данных: {len(months)}")
    for m in months:
        print(f"    {m['period_from']} … {m['period_to']}")

    per_nm = defaultdict(lambda: {"qty": 0.0, "rev": 0.0, "acc": set()})
    for r in rows:
        d = per_nm[str(r["article"])]
        d["qty"] += float(r["qty"] or 0)
        d["rev"] += float(r["rev"] or 0)
        d["acc"].add(r["account"])

    selling = {nm: d for nm, d in per_nm.items() if d["qty"] > 0}
    zero = {nm: d for nm, d in per_nm.items() if d["qty"] <= 0}
    rev_all = sum(d["rev"] for d in selling.values())
    print(f"\n=== 1. ПРОДАВАЛОСЬ ЗА ГОД")
    print(f"    карточек с продажами (qty>0): {len(selling)}")
    print(f"    выручка за год:               {rev_all:,.0f} ₽")
    print(f"    (в ноль/минус после возвратов, не считаем: {len(zero)} карточек)")
    for a in sorted(ACC):
        s = {nm: d for nm, d in selling.items() if a in d["acc"]}
        mm = db.query("""select count(distinct period_from) m from sales where platform='wb'
                         and account=%s and period_from>=%s and period_to<=%s""", (a, FROM, TO))[0]["m"]
        print(f"      {ACC[a]:<18} {len(s):5d} карт.  {sum(d['rev'] for d in s.values()):14,.0f} ₽"
              f"   месяцев в окне: {mm}")

    # ---------- покрытие ----------
    kz, hz = sheet(BASE / "docs/FINAL_gabarity.xlsx", "К загрузке")
    wv, hw = sheet(BASE / "docs/FINAL_gabarity.xlsx", "Волны")
    covered = {str(r[hz["nmID"]]) for r in kz}
    wave_of = {str(r[hw["nmID"]]): str(r[hw["волна"]]) for r in wv}
    src_of = {str(r[hz["nmID"]]): str(r[hz["источник"]] or "") for r in kz}

    cards = {str(c["nm_id"]): dict(c) for c in db.query(
        "select nm_id, account, vendor_code, title from wb_cards")}
    # карточки, которых уже нет в контент-выгрузке (архив/удалены), но продажи за год были:
    # артикул и название достаём из сырья финотчёта
    lost = [nm for nm in per_nm if nm not in cards]
    if lost:
        for r in db.query("""select distinct on (payload->>'nm_id') payload->>'nm_id' nm,
                    payload->>'sa_name' sa, payload->>'subject_name' subj, account
                 from raw_wb_report where payload->>'nm_id' = any(%s)
                 order by payload->>'nm_id', rrd_id desc""", (lost,)):
            cards[r["nm"]] = {"nm_id": r["nm"], "account": r["account"],
                              "vendor_code": r["sa"] or "", "title": "",
                              "subject": r["subj"] or "", "_from_report": True}

    # мать = артикул ровно из 4 цифр; дети = артикул длиннее, первые 4 знака те же.
    # модель матери берём из названия любой карточки семейства, где она есть.
    fam_model = {}
    for c in db.query("select vendor_code, title from wb_cards where vendor_code is not null"):
        vc = str(c["vendor_code"] or "")
        if len(vc) < 4 or not vc[:4].isdigit():
            continue
        ms = card_models(c["title"] or "")
        if not ms:
            continue
        key = vc[:4]
        # приоритет — карточка-мать (ровно 4 знака), иначе первая найденная
        if key not in fam_model or len(vc) == 4:
            fam_model[key] = ms[0]

    def family_of(vc):
        vc = str(vc or "")
        return vc[:4] if len(vc) >= 4 and vc[:4].isdigit() else ""

    sel_cov = {nm: d for nm, d in selling.items() if nm in covered}
    sel_unc = {nm: d for nm, d in selling.items() if nm not in covered}
    rev_cov = sum(d["rev"] for d in sel_cov.values())
    rev_unc = sum(d["rev"] for d in sel_unc.values())

    print(f"\n=== 2. ПОКРЫТО (есть реальный короб)")
    print(f"    карточек: {len(sel_cov)} из {len(selling)} = {100*len(sel_cov)/len(selling):.1f} %")
    print(f"    выручка:  {rev_cov:,.0f} ₽ = {100*rev_cov/rev_all:.1f} % годовой")
    byw = defaultdict(lambda: [0, 0.0])
    for nm, d in sel_cov.items():
        k = wave_of.get(nm, "(нет в волнах)")[:9]
        byw[k][0] += 1
        byw[k][1] += d["rev"]
    for k in sorted(byw):
        n, rv = byw[k]
        print(f"      {k:<12} {n:5d} карт.  {rv:14,.0f} ₽  {100*rv/rev_all:5.1f} %")

    print(f"\n=== 3. ПРОДАЁТСЯ БЕЗ ИСТОЧНИКА")
    print(f"    карточек: {len(sel_unc)} из {len(selling)} = {100*len(sel_unc)/len(selling):.1f} %")
    print(f"    выручка:  {rev_unc:,.0f} ₽ = {100*rev_unc/rev_all:.1f} % годовой")
    print(f"    СВЕРКА: {len(sel_cov)}+{len(sel_unc)}={len(sel_cov)+len(sel_unc)} карточек, "
          f"{rev_cov:,.0f}+{rev_unc:,.0f}={rev_cov+rev_unc:,.0f} ₽ (расхождение "
          f"{rev_all-rev_cov-rev_unc:.2f} ₽)")
    nocard = [nm for nm in sel_unc if (cards.get(nm) or {}).get("_from_report")]
    unknown = [nm for nm in sel_unc if nm not in cards]
    print(f"    из них карточки нет в контент-выгрузке (архив/удалена), артикул восстановлен "
          f"из финотчёта: {len(nocard)} ({sum(sel_unc[nm]['rev'] for nm in nocard):,.0f} ₽)")
    if unknown:
        print(f"    не опознано вовсе: {len(unknown)} "
              f"({sum(sel_unc[nm]['rev'] for nm in unknown):,.0f} ₽)")

    # ---------- CSV ----------
    out = []
    for nm, d in sel_unc.items():
        c = cards.get(nm) or {}
        title = c.get("title") or ""
        vc = c.get("vendor_code") or ""
        ms = card_models(title)
        fam = family_of(vc)
        model = ms[0] if ms else fam_model.get(fam, "")
        out.append({
            "артикул": vc,
            "nmID": nm,
            "кабинет": ACC.get(c.get("account"), ""),
            "название": title or (f"[нет карточки; предмет: {c.get('subject','')}]"
                                  if c.get("_from_report") else ""),
            "шт_год": round(d["qty"], 1),
            "выручка_год_₽": round(d["rev"]),
            "OEM_модель": model,
            "откуда_модель": ("название" if ms else ("мать " + fam if model else "не определена")),
            "семья_мать": fam,
        })
    out.sort(key=lambda x: -x["выручка_год_₽"])
    if WRITE:
        with open(OUT, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(out[0].keys()), delimiter=";")
            w.writeheader()
            w.writerows(out)
        print(f"\n=== 4. CSV: {OUT} — строк {len(out)}")
    else:
        print(f"\n=== 4. CSV не пишем (--no-write), строк было бы {len(out)}")

    # ---------- модели-матери ----------
    # группировка по СЕМЬЕ (4-значная мать); если семьи нет — по коду модели из названия
    by_model = defaultdict(lambda: {"rev": 0.0, "qty": 0.0, "cards": 0, "name": ""})
    nomodel = {"rev": 0.0, "cards": 0, "qty": 0.0}
    for r in out:
        key = r["семья_мать"] or r["OEM_модель"].upper().strip()
        if not key:
            nomodel["rev"] += r["выручка_год_₽"]
            nomodel["qty"] += r["шт_год"]
            nomodel["cards"] += 1
            continue
        d = by_model[key]
        d["rev"] += r["выручка_год_₽"]
        d["qty"] += r["шт_год"]
        d["cards"] += 1
        if not d["name"] and r["OEM_модель"]:
            d["name"] = r["OEM_модель"]
    ranked = sorted(by_model.items(), key=lambda kv: -kv[1]["rev"])
    rev_model = sum(d["rev"] for _, d in ranked)
    tot = rev_model + nomodel["rev"]
    named = sum(1 for _, d in ranked if d["name"])
    print(f"\n=== 5. МОДЕЛИ-МАТЕРИ ЗА НЕПОКРЫТЫМИ ПРОДАЖАМИ")
    print(f"    уникальных моделей-матерей: {len(ranked)}  (из них с распознанным OEM-кодом {named})")
    print(f"    выручка на них: {rev_model:,.0f} ₽ = {100*rev_model/tot:.1f} % непокрытой")
    print(f"    вне семей (модель не определена): {nomodel['cards']} карточек, "
          f"{nomodel['rev']:,.0f} ₽ = {100*nomodel['rev']/tot:.1f} %")
    print(f"    СВЕРКА: {rev_model:,.0f}+{nomodel['rev']:,.0f}={tot:,.0f} ₽ против "
          f"непокрытой выручки {rev_unc:,.0f} ₽ (расхождение {tot-rev_unc:.0f} ₽ — округление)")
    print(f"\n    накопительный итог (база — выручка внутри семей, {rev_model:,.0f} ₽):")
    acc, marks, hit = 0.0, [0.5, 0.8, 0.95], {}
    for i, (m, d) in enumerate(ranked, 1):
        acc += d["rev"]
        for p in marks:
            if p not in hit and acc >= p * rev_model:
                hit[p] = (i, acc)
    for p in marks:
        if p in hit:
            i, a = hit[p]
            print(f"      {int(p*100)} % ({a:,.0f} ₽) закрывают {i} моделей "
                  f"({100*i/len(ranked):.1f} % списка)")
    print("\n    топ-20 моделей-матерей по непокрытой выручке:")
    for m, d in ranked[:20]:
        print(f"      {m:<6} {(d['name'] or '—'):<18} {d['rev']:12,.0f} ₽  "
              f"{d['qty']:7.0f} шт  {d['cards']:4d} карт.")
    # приложение: рейтинг моделей-матерей
    if not WRITE:
        return
    with open(BASE / "docs/selling_uncovered_models.csv", "w",
              encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["ранг", "семья_мать", "OEM_модель", "карточек",
                    "шт_год", "выручка_год_₽", "накопл_%"])
        a = 0.0
        for i, (m, d) in enumerate(ranked, 1):
            a += d["rev"]
            w.writerow([i, m, d["name"], d["cards"], round(d["qty"], 1),
                        round(d["rev"]), round(100 * a / rev_model, 2)])


if __name__ == "__main__":
    # --from / --to — окно; --no-write — не трогать CSV (контрольные прогоны)
    a = sys.argv[1:]
    if "--from" in a:
        FROM = a[a.index("--from") + 1]
    if "--to" in a:
        TO = a[a.index("--to") + 1]
    WRITE = "--no-write" not in a
    main()
