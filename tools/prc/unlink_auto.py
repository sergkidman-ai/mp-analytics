# поток: prc
"""tools/prc/unlink_auto.py — отбор бесспорных пар «карточка поставщика → наш внешний код».

Во вкладке «Несопоставлено» 361 карточка оприходований без нашего кода. Кандидаты подобраны
по признакам (`prc_unlinked_candidate`), но кликать каждую руками — работа на день. Правило
Сергея 18.08.2026: «там, где нет сомнений, сделаем автоматом, остальное отдадим человеку».

БЕССПОРНО (уровень A) — три условия сразу:
  1. все кандидаты с максимальным баллом указывают на ОДИН внешний код (несколько карточек
     под одним кодом — это просто разные поставщики того же товара, спора нет);
  2. сошлись все опорные признаки: модель, тип расходника, бренд принтера, цвет;
  3. сошёлся ресурс/объём — то, чем отличаются XL-версии одного и того же кода.
Уровень B — то же, но ресурс не сверился (в ТК его нет или он не указан): пара похожа,
но XL-версию от обычной так не отличить — по умолчанию НЕ трогаем.
Уровень C — лидер один, но опорные признаки неполные. D — лидеры указывают на РАЗНЫЕ коды.
E — кандидатов нет вовсе. C/D/E — человеку, автоматом не сводим.

Аббревиатура внутреннего кода берётся по бренду ИЗ НАЗВАНИЯ (`ms_import.abbr_by_name`).

    ./venv/bin/python tools/prc/unlink_auto.py                 # отчёт, ничего не меняет
    ./venv/bin/python tools/prc/unlink_auto.py --mark A        # пометить уровень A «Свести»
    ./venv/bin/python -m prices.unlink_apply --apply           # и записать это в МойСклад
"""
import re
import sys
import csv
import pathlib
import argparse
import collections

BASE_DIR = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))

from core import db                                      # noqa: E402
from prices import ms_import                             # noqa: E402
from tools.prc import wb_fill                            # noqa: E402

REPORT_MD = BASE_DIR / "docs/reports/prc_unlinked_auto.md"
REPORT_CSV = BASE_DIR / "docs/reports/prc_unlinked_auto.csv"

SQL = """
WITH top AS (SELECT c.*, (SELECT max(score) FROM prc_unlinked_candidate x
                           WHERE x.ms_id = c.ms_id) AS mx
             FROM prc_unlinked_candidate c),
     lead AS (SELECT ms_id, count(DISTINCT external_code) AS codes, min(external_code) AS code,
                     min(cand_name) AS cand_name, max(mx) AS score,
                     bool_and(coalesce(model_ok, false)) AS m,
                     bool_and(coalesce(kind_ok, false)) AS k,
                     bool_and(coalesce(brand_ok, false)) AS b,
                     bool_and(coalesce(color_ok, false)) AS col,
                     bool_and(coalesce(resource_ok, false)) AS res
              FROM top WHERE score = mx GROUP BY ms_id)
SELECT u.ms_id, u.supplier_key, u.article, u.name, u.qty, u.decision,
       l.code, l.cand_name, l.score, l.codes, l.m, l.k, l.b, l.col, l.res
FROM prc_unlinked u LEFT JOIN lead l ON l.ms_id = u.ms_id
ORDER BY u.supplier_key, u.article
"""


# Грубый `kind` из `prices.novelty` знает пять классов и «блок проявки» с «блоком фотобарабана»
# кладёт в один («cartridge»). На живом отборе это дало пару DV-130 → DK-1270 — девелопер
# свёлся бы с фотобарабаном. Поэтому у автоотбора свой СТРОГИЙ тип узла по словам названия;
# порядок важен: «блок фотобарабана» проверяем раньше «барабана», «тонер-картридж» раньше «тонера».
FINE_KIND = [
    ("девелопер", r"блок\s+проявк|девелопер|developer|\bdv-?\d"),
    ("фотобарабан", r"фотобарабан|драм-?картридж|drum|\bdk-?\d|\bop[cс]\b"),
    ("фотовал", r"фотовал|фоторолик"),
    ("термоузел", r"термоузел|термоблок|печк|фьюзер|fuser|\bfk-?\d"),
    ("бункер", r"бункер|отработ|waste"),
    ("ремень", r"ремень|belt"),
    ("ролик", r"ролик|ракель|лезви|\bвал\b"),
    ("чип", r"^чип|\bчип\b(?!\s*(есть|нет))"),
    ("чернила", r"чернил|\bink\b"),
    ("тонер-туба", r"туба"),
    ("тонер", r"тонер(?!-?картридж)"),
    ("картридж", r"картридж|cartridge"),
]
FINE_KIND = [(n, re.compile(rx, re.IGNORECASE)) for n, rx in FINE_KIND]


def fine_kind(name):
    """Строгий тип узла по названию или None (не опознали — сомнение, отдаём человеку)."""
    for label, rx in FINE_KIND:
        if rx.search(name or ""):
            return label
    return None


def tier(r):
    if r["code"] is None:
        return "E"
    if r["codes"] > 1:
        return "D"
    if not (r["m"] and r["k"] and r["b"] and r["col"]):
        return "C"
    ours, theirs = fine_kind(r["name"]), fine_kind(r["cand_name"])
    if not ours or not theirs or ours != theirs:
        return "C"
    return "A" if r["res"] else "B"


def collide(items):
    """Снять из автоотбора коды, на которые претендует больше одной карточка-сирота.

    Один внешний код = один товар. Если на него метятся две разные несопоставленные карточки,
    хотя бы одна из них ошибочна (живой случай: девелоперы DV-160 и DV-130 оба указали на 6859).
    Разбирать такое автоматом нельзя — обе строки уезжают человеку.
    """
    claims = collections.Counter(i["code"] for i in items if i["tier"] in ("A", "B"))
    hit = 0
    for i in items:
        if i["tier"] in ("A", "B") and claims[i["code"]] > 1:
            i["tier"], i["why"] = "D", f"на код {i['code']} метятся {claims[i['code']]} карточки"
            hit += 1
    return hit


def occupied(items):
    """Снять строки, где под кодом уже живёт карточка того же бренда — это дубль поставщика."""
    codes = sorted({i["code"] for i in items if i["tier"] in ("A", "B") and i["code"]})
    live = collections.defaultdict(set)
    for code in codes:
        for r in db.query("SELECT code FROM ms_product "
                          "WHERE external_code = %s AND NOT archived", (code,)):
            ms_code = (r["code"] or "").strip()
            if ms_code.startswith(code):
                live[code].add(ms_code[4:].strip().lower())
    hit = 0
    for i in items:
        if i["tier"] in ("A", "B") and i["abbr"] and i["abbr"] in live.get(i["code"], set()):
            i["tier"], i["why"] = "D", f"под {i['code']} уже есть карточка бренда «{i['abbr']}»"
            hit += 1
    return hit


def rows():
    catalog = wb_fill.tc_models()
    out = []
    for r in db.query(SQL):
        item = dict(r, tier=tier(r), why="")
        item["abbr"] = ms_import.abbr_by_name(r["name"])
        item["ms_code"] = f"{r['code']}{item['abbr']}" if (r["code"] and item["abbr"]) else ""
        tc = catalog.get(r["code"]) if r["code"] else None
        item["wb_name"] = wb_fill.compose(tc)[0] if tc else ""
        out.append(item)
    hit_c, hit_o = collide(out), occupied(out)
    return out, hit_c, hit_o


def report(items):
    by_tier = collections.Counter(i["tier"] for i in items)
    by_sup = collections.defaultdict(collections.Counter)
    for i in items:
        by_sup[i["supplier_key"]][i["tier"]] += 1
    no_abbr = [i for i in items if i["tier"] in ("A", "B") and not i["abbr"]]

    with REPORT_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["уровень", "поставщик", "артикул", "название карточки", "штук",
                    "внешний код", "наша карточка", "балл", "код в МС", "Название WB"])
        for i in sorted(items, key=lambda x: (x["tier"], x["supplier_key"], x["article"] or "")):
            w.writerow([i["tier"], i["supplier_key"], i["article"], i["name"], i["qty"],
                        i["code"] or "", i["cand_name"] or "", i["score"] or "",
                        i["ms_code"], i["wb_name"]])

    lines = ["# Несопоставленные карточки: что можно свести автоматом", "",
             f"Всего строк: {len(items)}. Уровни — см. шапку `tools/prc/unlink_auto.py`.", "",
             "| уровень | что это | строк |", "|---|---|---|",
             f"| A | один код, сошлись модель/тип/бренд/цвет **и ресурс** | {by_tier['A']} |",
             f"| B | то же, но ресурс не сверился | {by_tier['B']} |",
             f"| C | лидер один, опорные признаки неполные | {by_tier['C']} |",
             f"| D | лидеры указывают на разные коды, спор за код или код занят | {by_tier['D']} |",
             f"| E | кандидатов нет | {by_tier['E']} |", "",
             "## По поставщикам", "", "| поставщик | A | B | C | D | E |", "|---|---|---|---|---|---|"]
    for sup in sorted(by_sup, key=lambda s: -by_sup[s]["A"]):
        c = by_sup[sup]
        lines.append(f"| {sup} | {c['A']} | {c['B']} | {c['C']} | {c['D']} | {c['E']} |")
    lines += ["", f"Без аббревиатуры бренда в названии среди A+B: **{len(no_abbr)}** — "
                  "такая карточка получит внешний код и «Название WB», а внутренний код "
                  "останется пустым (бренд не выдумываем).", ""]
    for name, want in (("Примеры уровня A", "A"), ("Примеры уровня B", "B"),
                       ("Примеры уровня D (человеку)", "D")):
        lines += [f"## {name}", ""]
        for i in [x for x in items if x["tier"] == want][:8]:
            lines.append(f"- `{i['article']}` {i['name']} → **{i['code']}** "
                         f"«{i['cand_name']}», код `{i['ms_code'] or '—'}`, "
                         f"WB: {i['wb_name'] or '—'}"
                         + (f" — {i['why']}" if i.get('why') else ""))
        lines.append("")
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")
    return by_tier, by_sup, no_abbr


def mark(items, want):
    """Пометить строки уровня как «Свести» — решение ложится в нашу базу, МС не трогаем."""
    n = 0
    for i in items:
        if i["tier"] != want or i["decision"] != "pending":
            continue
        db.execute("""UPDATE prc_unlinked
                         SET decision = 'matched', target_code = %s, target_name = %s,
                             note = %s, decided_at = now()
                       WHERE ms_id = %s""",
                   (i["code"], i["cand_name"],
                    f"автоотбор уровня {want}: один код в лидерах, балл {i['score']}",
                    i["ms_id"]))
        n += 1
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description="Бесспорные пары для вкладки «Несопоставлено»")
    ap.add_argument("--mark", choices=["A", "B"], help="пометить уровень как «Свести» (только БД)")
    args = ap.parse_args(argv)

    items, hit_c, hit_o = rows()
    by_tier, by_sup, no_abbr = report(items)
    print(f"строк {len(items)}: " + ", ".join(f"{t} {by_tier[t]}" for t in "ABCDE"))
    print(f"  снято из автоотбора: спор за код {hit_c}, код уже занят своим брендом {hit_o}")
    print(f"  без аббревиатуры бренда среди A+B: {len(no_abbr)}")
    print(f"  отчёт: {REPORT_MD.relative_to(BASE_DIR)}, {REPORT_CSV.relative_to(BASE_DIR)}")
    if args.mark:
        print(f"  помечено «Свести»: {mark(items, args.mark)} (в МойСклад ничего не записано)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
