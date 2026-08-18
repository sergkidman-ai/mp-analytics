# поток: prc
"""tools/prc/archive_originals.py — убрать в архив оригинальные картриджи из «Несопоставлено».

В оприходованиях «Удаленного склада» попадаются ОРИГИНАЛЫ (в названии «ориг.», «(о)»,
«оригинал»): мы ими не торгуем, свести их с нашим 4-значным кодом нельзя — наш код обозначает
совместимый товар, и автоподбор тянет оригинал на аналог (Q2613X → 0022, TK-160 → 0239).
Решение Сергея 18.08.2026: такие карточки архивировать, сняв код, внешний код и артикул;
остаток по ним значения не имеет — в продаже их нет.

Внешний код в МойСклад обязателен: пустое значение сервис заменяет своим служебным (та же
случайная строка, что стоит у карточки сейчас) — это и есть «нет нашего кода».

    ./venv/bin/python tools/prc/archive_originals.py            # проба, ничего не пишет
    ./venv/bin/python tools/prc/archive_originals.py --apply
"""
import re
import sys
import pathlib
import argparse

BASE_DIR = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))

from core import db, ms_api                                    # noqa: E402

ORIGINAL_RE = re.compile(r"ориг(инал|\.)?|\(\s*[оО]\s*\)|\bOEM\b", re.I)
OUR_CODE_RE = re.compile(r"^\d{4}$")


def plan():
    """Не сведённые строки «Несопоставлено», где в названии заявлен оригинал."""
    rows = db.query("SELECT ms_id, supplier_key, article, name, qty FROM prc_unlinked "
                    "WHERE decision <> 'matched' ORDER BY supplier_key, article")
    return [r for r in rows if ORIGINAL_RE.search(r["name"] or "")]


def apply(todo, dry=True, log=print):
    body = []
    for r in todo:
        card = ms_api.get(f"/entity/product/{r['ms_id']}")
        have = (card.get("externalCode") or "").strip()
        if OUR_CODE_RE.match(have):
            log(f"  ✗ {r['article']}: несёт наш код {have} — не трогаем, решает человек")
            continue
        body.append({"meta": {"href": f"{ms_api.BASE}/entity/product/{r['ms_id']}",
                              "type": "product", "mediaType": "application/json"},
                     "code": "", "article": "", "archived": True})
    if dry:
        log(f"[проба] в архив ушло бы {len(body)} карточек — ничего не записано")
        return 0
    if body:
        ms_api.post("/entity/product", body)
        db.execute("UPDATE prc_unlinked SET decision = 'skip' WHERE ms_id = ANY(%s)",
                   ([r["ms_id"] for r in todo],))
    log(f"[запись] в архив {len(body)} карточек, во вкладке помечены «пропустить»")
    return len(body)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Архивировать оригиналы из «Несопоставлено»")
    ap.add_argument("--apply", action="store_true", help="писать в МойСклад (по умолчанию проба)")
    args = ap.parse_args(argv)
    todo = plan()
    for r in todo:
        print(f"  · {r['supplier_key']:<12} {r['article']:<15} шт {r['qty']} | {r['name'][:60]}")
    apply(todo, dry=not args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
