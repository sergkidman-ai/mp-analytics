# поток: prc
"""tools/prc/tc_bench.py — насколько хорошо матчер находит товар, проверенный человеком.

Эталон — строки новинок, по которым решение уже принято руками (`decision in matched/exists`
и проставлен `ms_code`). Человек сказал, какая карточка верна; матчер прогоняется на тех же
строках заново, и мы смотрим, попал ли он.

Сравниваем по ВНЕШНЕМУ КОДУ, а не по коду карточки: у одного товара в МС лежат карточки
нескольких поставщиков, и «другая карточка того же внешнего кода» — это попадание, а не промах.
По коду карточки бенч врал в обе стороны (плюс числовой префикс кода вида `00357q` не равен
внешнему коду `0035`).

Три числа на выходе:
  варианты есть        — строка вообще не осталась без кандидатов;
  верный код среди них — человеческий ответ есть в списке (можно выбрать руками);
  он же первый         — предвыбранный вариант верен (человеку остаётся нажать «ок»).

Правку матчинга проверяют так: прогнать ДО, прогнать ПОСЛЕ, сравнить третье число.

Запуск:  ./venv/bin/python -m tools.prc.tc_bench
"""
import pathlib
import sys

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

from prices import catalog as C   # noqa: E402
from core.db import query         # noqa: E402


def load():
    """Строки с подтверждённым человеком товаром → (строка прайса, верный внешний код)."""
    return query("""
        select n.article, n.name, p.external_code
          from prc_novelty n
          join ms_product p on p.code = n.ms_code
         where n.decision in ('matched', 'exists')
           and n.ms_code is not null and p.external_code is not null
    """)


def main():
    rows = load()
    hits = C.analyze([{"article": r["article"], "name": r["name"]} for r in rows])

    found = right = top = 0
    for r in rows:
        variants = hits.get(r["article"]) or []
        if not variants:
            continue
        found += 1
        codes = [h["item"]["external_code"] for h in variants]
        if r["external_code"] in codes:
            right += 1
            top += codes[0] == r["external_code"]

    n = len(rows) or 1
    print(f"[tc-bench] строк {len(rows)}")
    print(f"[tc-bench] варианты есть        {found} ({found * 100 // n}%)")
    print(f"[tc-bench] верный код среди них {right} ({right * 100 // n}%)")
    print(f"[tc-bench] он же первый         {top} ({top * 100 // n}%)")


if __name__ == "__main__":
    main()
