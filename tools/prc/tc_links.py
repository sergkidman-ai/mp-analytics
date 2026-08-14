# поток: prc
"""tools/prc/tc_links.py — справочник связей универсальных моделей (каталог TheCartridge).

Универсальный картридж закрывает несколько наших позиций: 0002 (Q2612A/12A) — тот же товар,
что 5335 (FX-10), 5336 (703), 5334. Раньше это знание жило только в МойСкладе, доп. полем
«Связь» на карточке, заполненным руками и не у всех карточек одного внешнего кода. С 14.08.2026
связи приходят из каталога ТК (`prc_tc_link`, миграция 408) — фактом первоисточника.

Два режима:
  --code 0002   быстрый вопрос «с чем связан этот код» (вывод в чат, несколько строк);
  без ключей    выгрузка всех связей в CSV + сводка.

Направление сохраняем как отдал источник: `incoming` и `outgoing` у них не совпадают, и
«обе стороны сослались друг на друга» — более сильное утверждение, чем ссылка в одну сторону.

Запуск:  ./venv/bin/python -m tools.prc.tc_links [--code 0002] [--out <csv>]
"""
import argparse
import csv
import datetime
import pathlib
import sys

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

from core.db import query   # noqa: E402

FIELDS = ("внешний код", "модель ТК", "карточек МС", "связанный код", "связанная модель",
          "карточек МС у связанной", "направление")
WAY = {(True, True): "обе стороны", (True, False): "исходящая", (False, True): "входящая"}


def pairs():
    """Все связи парами, с названиями моделей и числом живых карточек МС по каждому коду."""
    return query("""
        select l.external_code, l.ref_code, l.outgoing, l.incoming,
               m.title  as model, r.title  as ref_model,
               coalesce(a.cards, 0) as cards, coalesce(b.cards, 0) as ref_cards
          from prc_tc_link l
          join prc_tc_model m on m.external_code = l.external_code
          left join prc_tc_model r on r.external_code = l.ref_code
          left join (select external_code, count(*) cards from ms_product
                      where not archived group by external_code) a
                 on a.external_code = l.external_code
          left join (select external_code, count(*) cards from ms_product
                      where not archived group by external_code) b
                 on b.external_code = l.ref_code
         order by l.external_code, l.ref_code
    """)


def show(code):
    """Связка одного кода — ответ на вопрос «а с чем связан 0002?»."""
    rows = [r for r in pairs() if r["external_code"] == code]
    if not rows:
        known = query("select 1 from prc_tc_model where external_code = %s", (code,))
        print(f"[tc-links] {code}: " + ("связей нет" if known else "нет такого кода в каталоге ТК"))
        return
    print(f"[tc-links] {code} {rows[0]['model']} (карточек МС {rows[0]['cards']}) связан с:")
    for r in rows:
        way = WAY[(r["outgoing"], r["incoming"])]
        model = r["ref_model"] or "НЕТ В СЛЕПКЕ ТК"
        print(f"[tc-links]   {r['ref_code']}  {model}  (карточек МС {r['ref_cards']}, {way})")


def main():
    day = datetime.date.today().isoformat()
    ap = argparse.ArgumentParser(description="Справочник связей универсальных моделей ТК")
    ap.add_argument("--code", help="показать связку одного внешнего кода")
    ap.add_argument("--out", default=f"docs/reports/tc_links_{day}.csv")
    args = ap.parse_args()

    if args.code:
        show(args.code.strip())
        return

    rows = pairs()
    path = BASE_DIR / args.out
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS, delimiter=";")
        writer.writeheader()
        for r in rows:
            writer.writerow({
                "внешний код": r["external_code"], "модель ТК": r["model"],
                "карточек МС": r["cards"], "связанный код": r["ref_code"],
                "связанная модель": r["ref_model"] or "",
                "карточек МС у связанной": r["ref_cards"],
                "направление": WAY[(r["outgoing"], r["incoming"])]})

    models = {r["external_code"] for r in rows}
    both = sum(1 for r in rows if r["outgoing"] and r["incoming"])
    lost = {r["ref_code"] for r in rows if r["ref_model"] is None}
    nocard = {r["ref_code"] for r in rows if r["ref_cards"] == 0}
    print(f"[tc-links] моделей со связями {len(models)}, ссылок {len(rows)} "
          f"(обе стороны {both}, односторонних {len(rows) - both})")
    print(f"[tc-links] ссылок на код, которого нет в слепке ТК: {len(lost)}")
    print(f"[tc-links] связанных кодов без живой карточки МС: {len(nocard)}")
    print(f"[tc-links] файл: {args.out}")


if __name__ == "__main__":
    main()
