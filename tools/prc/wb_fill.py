# поток: prc
"""tools/prc/wb_fill.py — заполнение пустого доп. поля «Название WB» в карточках МойСклада.

Поле печатается на этикетке WB, у 9.3 тыс. живых карточек оно пустое — карточка уезжает
на маркетплейс безымянной. Значение НЕ выдумываем и НЕ копируем у соседних карточек того же
внешнего кода («родни»): их заполняли руками и с ошибками. Собираем по формуле из каталога
TheCartridge (`prc_tc_model`, слепок ночного таймера) — там наш товар заведён один раз, полями.

ФОРМУЛА (согласована с Сергеем 16.08.2026):

    тип + модель + «для» + бренд принтера + доп. название + цвет + чип + «XL ресурс»

  тип        `consumable_type` дословно, но «Комплект …» → «Набор …» (этикетке нужна краткость);
  модель     `title`, значок «№» выкидываем;
  бренд      преобладающий бренд из `printer_models` (у модели их бывает два — берём тот,
             под который в компат-листе больше принтеров: 92298X = Canon 7 / HP 8 → HP);
             сокращения: Konica Minolta → Konica, F+ Imaging → F+, Fuji Xerox → Xerox.
             Бренд уже стоит внутри модели («Чернила универсальные Epson 100 мл») — «для …»
             НЕ добавляем: на этикетке дважды одно и то же не пишем (решение Сергея 16.08);
  доп.назв.  `additional_title` (синоним кода, «05A»), тоже без «№». Не пишем вовсе, если
             это ПЕРЕЧЕНЬ (запятая внутри: у DK-6305 там восемь артикулов подряд) и если
             без него название превышает ориентир 50 знаков;
  цвет       `ink_colors` строчными, несколько цветов — через «/»; У НАБОРОВ ЦВЕТ НЕ ПИШЕМ
             (любой тип со словом «Комплект»: там цвет — это перечень содержимого);
  чип        `chip`: «С чипом» → «с чипом», «C чипом без счетчика» → «с чипом без счётчика»,
             «Без чипа» → «без чипа». ПУСТО В ТК → В НАЗВАНИИ НИЧЕГО (чип не придумываем);
  XL         `capacity = Увеличенная` → «XL ресурс» ровно в конце. «Эконом» не пишем.

Чего не делаем: не трогаем уже заполненные поля; не заполняем карточки, чьего внешнего кода
нет в каталоге ТК (данных нет — догадок не пишем); цвет вне словаря останавливает строку,
а не превращается в отсебятину.

    ./venv/bin/python tools/prc/wb_fill.py                  # отчёт (живой опрос МС)
    ./venv/bin/python tools/prc/wb_fill.py --from-db        # отчёт по слепку raw_moysklad_product
    ./venv/bin/python tools/prc/wb_fill.py --apply [--limit N] [--only 0011,0054]
"""
import re
import sys
import csv
import time
import json
import pathlib
import argparse
import collections
from datetime import date

BASE_DIR = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))

from core import db, ms_api                     # noqa: E402
from prices.ms_import import ATTRS              # noqa: E402
from prices.wb_label import Parts, Result, shorten, MAX_CHARS  # noqa: E402

WB_ATTR = "Название WB"
ATTR_ID, ATTR_TYPE = ATTRS[f"Доп. поле: {WB_ATTR}"]

TYPE_RENAME = {
    "Комплект картриджей": "Набор картриджей",
    "Комплект фотобарабанов": "Набор фотобарабанов",
    "Комплект чернил": "Набор чернил",
}
# Цвет не пишем у всего, что является комплектом: там ink_colors — перечень содержимого.
SET_MARK = "омплект"

BRAND_SHORT = {"Konica Minolta": "Konica", "F+ Imaging": "F+", "Fuji Xerox": "Xerox"}

CHIP_WORD = {"С чипом": "с чипом", "C чипом": "с чипом",
             "С чипом без счетчика": "с чипом без счётчика",
             "C чипом без счетчика": "с чипом без счётчика",
             "Без чипа": "без чипа"}

# Словарь цветов. Значение вне словаря — не повод сочинять слово: строка уходит в отчёт.
COLOR_OK = {
    "черный", "голубой", "пурпурный", "желтый", "серый", "белый", "красный", "синий",
    "зеленый", "оранжевый", "фиолетовый", "розовый", "коричневый", "серебряный", "золотой",
    "прозрачный", "цветной", "черный матовый", "светло-голубой", "светло-пурпурный",
    "светло-черный", "светло-серый", "фото-черный", "фото-голубой", "фото-пурпурный",
    "фото-серый", "усилитель глянца",
}

# Флакон: у сыпучего и жидкого объём — часть товара, «Тонер TK-1130 для Kyocera черный»
# без него не отличает 300 г от 1 кг (решение Сергея 26.08). Берём `volume_ml` — это ровно
# то число, что поставщик пишет в названии («300гр.», «135мл»); `weight` брать НЕЛЬЗЯ,
# он брутто с флаконом (у TK-1170 volume_ml 300 против weight 350). Единица — по типу.
VOLUME_UNIT = {"Тонер": "г", "Чернила": "мл"}

# Общие правила этикетки: ориентир 50, жёсткий предел 55. Проверка двух строк
# в исходном шаблоне остаётся отдельным обязательным этапом перед массовой записью.
LABEL_LIMIT = MAX_CHARS

# МС днём занят рабочими процессами — идём с паузой между страницами чтения и пачками записи.
PAUSE = 1.0

REPORT_DIR = BASE_DIR / "docs" / "reports"


def _clean(value):
    """Строка без «№» и лишних пробелов."""
    return re.sub(r"\s+", " ", (value or "").replace("№", "")).strip()


def brand_of(tc):
    """Преобладающий бренд принтера + все варианты (для отчёта)."""
    counts = collections.Counter(p["brand"] for p in (tc.get("printer_models") or [])
                                 if p.get("brand"))
    if not counts:
        return None, []
    top = counts.most_common()
    return BRAND_SHORT.get(top[0][0], top[0][0]), [b for b, _ in top]


def compose_details(tc, price_name=""):
    """One semantic formula plus shared label rules, including supplier XL."""
    kind = (tc.get("consumable_type") or "").strip()
    model = _clean(tc.get("title"))
    brand, brands = brand_of(tc)
    missing = [n for n, v in (("тип", kind), ("модель", model), ("бренд", brand)) if not v]
    if missing:
        return Result(reason=f"в ТК нет {', '.join(missing)}", issue="missing_source")

    parts = [TYPE_RENAME.get(kind, kind), model]
    if not re.search(rf"(?<![0-9A-Za-zА-Яа-я]){re.escape(brand)}(?![0-9A-Za-zА-Яа-я])",
                     model, re.IGNORECASE):
        parts += ["для", brand]
    extra = _clean(tc.get("additional_title"))
    if "," in extra:
        extra = ""

    colors = []
    if not re.search(r"комплект|набор", kind, re.IGNORECASE):
        colors = [c.strip().lower() for c in (tc.get("ink_colors") or []) if c and c.strip()]
        unknown = [c for c in colors if c not in COLOR_OK]
        if unknown:
            return Result(reason=f"цвет вне словаря: {'/'.join(unknown)}", issue="color")

    chip = (tc.get("chip") or "").strip()
    word = CHIP_WORD.get(chip, "")
    if chip and not word:
        return Result(reason=f"чип вне словаря: {chip}", issue="chip")

    # Supplier XL must participate in shortening, not be appended afterwards.
    source_xl = re.search(r"\bxl\b|увеличен", price_name or "", re.IGNORECASE)
    enlarged = ((tc.get("capacity") or "").strip() == "Увеличенная"
                or bool(source_xl and "XL" not in " ".join(parts + [extra]).upper()))
    content = ""
    unit = VOLUME_UNIT.get(kind)
    if unit:
        vol = str(tc.get("volume_ml") or "").strip()
        if vol:
            import math
            try:
                number = float(vol)
                if not math.isfinite(number) or number <= 0:
                    raise ValueError
            except ValueError:
                return Result(reason="некорректная масса/объём содержимого в ТК", issue="content")
            content = f"{number:g} {unit}"
    return shorten(Parts(prefix=tuple(parts), extra=extra, colors=tuple(colors),
                         chip=word, xl="XL ресурс" if enlarged else "", content=content))


def compose(tc, price_name=""):
    """(Final WB name, reason). Same formula for novelties and catalogue updates."""
    result = compose_details(tc, price_name)
    return result.name, result.reason


def tc_models():
    """Каталог ТК: внешний код → сырьё модели."""
    rows = db.query("SELECT external_code, raw FROM prc_tc_model WHERE gone_at IS NULL")
    return {r["external_code"]: r["raw"] for r in rows}


def _wb_value(payload):
    for a in (payload.get("attributes") or []):
        if a.get("name") == WB_ATTR:
            return (a.get("value") or "").strip()
    return ""


def cards_from_db():
    rows = db.query("SELECT ms_id, payload FROM raw_moysklad_product")
    return [r["payload"] for r in rows]


def cards_live(pause=PAUSE):
    """Живой обход каталога МС с паузой между страницами (МС занят рабочими процессами)."""
    rows, offset = [], 0
    while True:
        chunk = ms_api.get("/entity/product", {"limit": 1000, "offset": offset})
        page = chunk.get("rows", [])
        rows += page
        offset += len(page)
        if not page or offset >= chunk.get("meta", {}).get("size", 0):
            return rows
        time.sleep(pause)


def collect(cards, catalog, only=None):
    """Строки к заливке + статистика отказов."""
    todo, skip = [], collections.Counter()
    examples = collections.defaultdict(list)
    for card in cards:
        if card.get("archived"):
            skip["карточка архивная"] += 1
            continue
        if _wb_value(card):
            skip["поле уже заполнено"] += 1
            continue
        ext = (card.get("externalCode") or "").strip()
        if only and ext not in only:
            continue
        if not ext:
            skip["у карточки нет внешнего кода"] += 1
            continue
        tc = catalog.get(ext)
        if tc is None:
            skip["внешнего кода нет в каталоге ТК"] += 1
            continue
        value, reason = compose(tc)
        if reason:
            skip[reason] += 1
            if len(examples[reason]) < 5:
                examples[reason].append(f"{ext} {card.get('code')}")
            continue
        todo.append({"ms_id": card["id"], "code": card.get("code"), "external_code": ext,
                     "ms_name": card.get("name"), "wb_name": value,
                     "brands": ", ".join(brand_of(tc)[1]),
                     "attributes": card.get("attributes") or []})
    return todo, skip, examples


def write_report(todo, skip, examples, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["код", "внешний код", "наименование МС", "Название WB (соберём)",
                    "бренды ТК"])
        for r in sorted(todo, key=lambda x: (x["external_code"], x["code"] or "")):
            w.writerow([r["code"], r["external_code"], r["ms_name"], r["wb_name"], r["brands"]])
    return path


def apply(todo, dry=True, log=print):
    """Пишем поле пачками по 100 (bulk POST /entity/product обновляет по meta).

    Отправляем ПОЛНЫЙ список атрибутов карточки со своим значением — так соседние доп. поля
    не зависят от того, мерджит МС массив атрибутов или заменяет.
    """
    done = 0
    for start in range(0, len(todo), 100):
        chunk = todo[start:start + 100]
        body = []
        for r in chunk:
            attrs = [a for a in r["attributes"] if a.get("name") != WB_ATTR]
            attrs.append({
                "meta": {"href": f"{ms_api.BASE}/entity/product/metadata/attributes/{ATTR_ID}",
                         "type": "attributemetadata", "mediaType": "application/json"},
                "type": ATTR_TYPE, "value": r["wb_name"]})
            body.append({"meta": {"href": f"{ms_api.BASE}/entity/product/{r['ms_id']}",
                                  "type": "product", "mediaType": "application/json"},
                         "attributes": attrs})
        if dry:
            log(f"[проба] пачка {start // 100 + 1}: {len(body)} карточек")
            continue
        ms_api.post("/entity/product", body)
        done += len(body)
        log(f"[запись] {done} из {len(todo)}")
        if start + 100 < len(todo):
            time.sleep(PAUSE)
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="писать в МойСклад (иначе только отчёт)")
    ap.add_argument("--from-db", action="store_true", help="брать карточки из слепка, не из МС")
    ap.add_argument("--limit", type=int, help="ограничить число карточек")
    ap.add_argument("--only", help="только эти внешние коды через запятую")
    ap.add_argument("--out", help="путь отчёта")
    args = ap.parse_args()

    catalog = tc_models()
    cards = cards_from_db() if args.from_db else cards_live()
    only = {c.strip() for c in args.only.split(",")} if args.only else None
    todo, skip, examples = collect(cards, catalog, only)
    if args.limit:
        todo = todo[:args.limit]

    out = pathlib.Path(args.out) if args.out else \
        REPORT_DIR / f"prc_wb_name_fill_{date.today():%Y-%m-%d}.csv"
    write_report(todo, skip, examples, out)

    print(f"карточек в источнике: {len(cards)} ({'слепок' if args.from_db else 'живой МС'})")
    print(f"заполним: {len(todo)} карточек на {len({r['external_code'] for r in todo})} кодах")
    for reason, n in skip.most_common():
        tail = f"  (напр. {', '.join(examples[reason])})" if examples.get(reason) else ""
        print(f"   не берём — {reason}: {n}{tail}")
    lens = [len(r["wb_name"]) for r in todo] or [0]
    print(f"длина названия: медиана {sorted(lens)[len(lens) // 2]}, максимум {max(lens)}")
    print(f"отчёт: {out}")

    if args.apply:
        apply(todo, dry=False)
    elif todo:
        print("запись НЕ делалась — добавьте --apply")


if __name__ == "__main__":
    main()
