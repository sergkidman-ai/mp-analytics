# поток: prc
# -*- coding: utf-8 -*-
"""
Файл импорта новых карточек в МойСклад по строкам, сопоставленным с нашим каталогом.

Главное правило: новая карточка — брат-близнец тех, что уже заведены с тем же ВНЕШНИМ КОДОМ.
Всё, что описывает ТОВАР (группа, вес, штрихкод Code128, «Название WB»), берём у родни;
всё, что описывает ПОСТАВКУ (наименование, артикул, цена, поставщик), — из прайса.
Ничего не сочиняем: если у родни поле пустое, строка уходит в отчёт на ручное заполнение,
а не заполняется догадкой.

Что подтверждено фактом МС (инструкция «Новинки в прайсах поставщиков» местами устарела):
  • Код = внешний код + аббревиатура. У ВСЕГО, что приходит от Феррета, аббревиатура «cs»:
    Cactus 3049, G&G 729 из 731, Print-Rite 57, Cet 44 карточки. Обещанных инструкцией
    «gg» и «pr» в базе по 2 и 0 штук — это не правило, а случайность.
  • Штрихкод Code128 = DS + 3 буквы + внешний код с ведущими нулями и ОДИН И ТОТ ЖЕ у всех
    карточек внешнего кода (у 1191 — DSNXV0001191 на 28 карточках). ean8 МС генерирует сам,
    его в файл не пишем.
  • «Название WB» тоже общее на внешний код: это название модели, а не поставщика.
    Живой формат каталога — «Картридж TK-520C Kyocera голубой», «Картридж CF283X HP 83X XL
    ресурс»: тип + модель + бренд принтера + цвет + XL ресурс + с чипом. Предлога «для»
    в 93% значений нет — берём формат каталога, а не шаблон из инструкции.
  • НДС 22 (34645 карточек против 20 у 2069), единица «шт», гарантия 365 — как в инструкции.

Импорт в МС делает человек: МС → Товары → Импорт → импорт из excel, «Искать = по Артикулу».
Отсюда ничего в МС не пишется.
"""
import argparse
import re
from collections import Counter
from datetime import date
from pathlib import Path

from core.db import query
from .profiles import get_profile

# Порядок колонок — как в файле загрузки от 30.07 («МС шаблон для новинок»).
COLUMNS = [
    "Группы", "Код", "Внешний код", "Наименование", "Описание", "Артикул",
    "Доп. поле: Код поставщика", "Единица измерения", "Закупочная цена", "НДС",
    "Поставщик", "Вес", "Страна", "Доп. поле: Гарантия/ Срок службы",
    "Доп. поле: Название WB", "Штрихкод Code128", "Доп. поле: Связь",
]

UOM = "шт"
VAT = 22
WARRANTY = 365
COUNTRY = "Китай"

# Аббревиатура кода по артикулу поставщика: (регулярка, аббревиатура). Первое совпадение.
# Пустая регулярка в конце — значение по умолчанию для поставщика.
CODE_SUFFIX = {
    "kaktus_msk": [(r"^CSP-", "csp"), (r"", "cs")],
}
# Контрагент для колонки «Поставщик» — как он назван в МС.
MS_SUPPLIER = {
    "kaktus_msk": 'ООО "КОМПАНИЯ ФЕРРЕТ"',
}

# Ресурс в названии: «(9000стр.)», «9200 стр.», «23600 копий», «69K». Цифры не должны быть
# продолжением кода модели («006R01828» — это не 1828 страниц), поэтому слева граница.
PAGES_RE = re.compile(r"(?<![0-9A-Za-zА-Яа-я])(\d[\d ]{1,8}?)\s*(?:стр|копий)", re.IGNORECASE)
PAGES_K_RE = re.compile(r"(?<![0-9A-Za-zА-Яа-я])(\d{1,3})\s*[kK]\b")

# Папки-исключения при выборе группы. Карточка живёт в папке БРЕНДА ПРИНТЕРА
# («Картриджи/Картриджи Kyocera Mita»), а не бренда поставщика: папки G&G и BULAT
# завёл когда-то один поставщик, и родня иногда лежит там. Оригиналы — чужой ассортимент.
GROUP_SUPPLIER = ("Картриджи/Картриджи G&G", "Картриджи/Картриджи BULAT")
GROUP_PREFIX = "Картриджи/"


def suffix(article, supplier_key):
    """Аббревиатура поставщика для колонки «Код»."""
    for pattern, abbr in CODE_SUFFIX.get(supplier_key, []):
        if not pattern or re.search(pattern, article or "", re.IGNORECASE):
            return abbr
    raise KeyError(f"нет правила аббревиатуры кода для поставщика '{supplier_key}'")


def _attrs(payload):
    return {a.get("name"): a.get("value") for a in (payload.get("attributes") or [])}


def family(codes):
    """Родня по внешнему коду: живые карточки МС со всем, что нужно новой карточке."""
    rows = query(
        """SELECT p.external_code ec, p.code, p.article, p.name, p.updated_at,
                  r.payload payload
             FROM ms_product p JOIN raw_moysklad_product r ON r.ms_id = p.ms_id
            WHERE p.external_code = ANY(%s) AND NOT p.archived""",
        (list(codes),))
    out = {}
    for row in rows:
        payload, attrs = row["payload"], _attrs(row["payload"])
        code128 = next((b["code128"] for b in (payload.get("barcodes") or []) if b.get("code128")), None)
        weight = payload.get("weight")
        out.setdefault(row["ec"], []).append({
            "code": row["code"], "article": row["article"], "name": row["name"],
            "updated": row["updated_at"], "path": payload.get("pathName"),
            "weight": float(weight) if weight else None, "code128": code128,
            "wb": (attrs.get("Название WB") or "").strip() or None,
        })
    return out


# Префикс поставщика в артикуле: «CS-KMTNP80C» и «CR-KMTNP80C» — один и тот же товар
# у двух брендов одного завода. Отрезаем только префикс с разделителем, чтобы не съесть
# кусок кода модели («SACF226X» так и остаётся целым).
SUPPLIER_PREFIX = re.compile(
    r"^(?:CSP|CS|CR|GG|PR|GP|PL|HB|NP|NV|SF|OEM|TC|LX|SA|BS|BT|SL|EL|EP|T2|UT|UJ|UN|MY|IT|ML|ATM|SP|ST|AT|WB|SK)"
    r"[-_ ]", re.IGNORECASE)
_DIMS_CACHE = None


def _core(article):
    """Код модели без префикса поставщика и разделителей: CS-KMTNP80C -> KMTNP80C."""
    return re.sub(r"[^0-9A-Z]", "", SUPPLIER_PREFIX.sub("", str(article or "").upper()))


def weight_from_dims(cards):
    """Вес из прайсов поставщиков (`supplier_dims`), когда у родни в МС веса нет.

    Это РЕАЛЬНЫЙ вес из прайса, а не расчёт: тот же товар продаётся под кодами разных
    брендов одного завода (CS-KMTNP80C = CR-KMTNP80C). Если источники расходятся больше
    чем на 20% — не выбираем ничего, пусть человек посмотрит.
    """
    global _DIMS_CACHE
    cores = {_core(c["article"]) for c in cards}
    cores = {c for c in cores if len(c) >= 6}
    if not cores:
        return None, None
    if _DIMS_CACHE is None:
        _DIMS_CACHE = [(r["supplier"], r["article"], float(r["weight_kg"]), _core(r["article"]))
                       for r in query("SELECT supplier, article, weight_kg FROM supplier_dims "
                                      "WHERE weight_kg IS NOT NULL AND weight_kg > 0")]
    hits = [h for h in _DIMS_CACHE if h[3] in cores]
    if not hits:
        return None, None
    values = [h[2] for h in hits]
    if max(values) > min(values) * 1.2:
        return None, f"вес в прайсах поставщиков расходится: {sorted(set(values))}"
    best = max(hits, key=lambda h: h[2])
    return best[2], f"вес не у родни, а из прайса {best[0]} (арт. {best[1]}) — проверить"


def _pick(cards, field, keep=None, tie=None):
    """Значение поля у родни: что чаще; при равенстве — правило tie, иначе свежая карточка.

    keep отсеивает заведомо неподходящие варианты (чужая папка, кривое написание), но
    только если после отсева что-то осталось. Возвращает (значение, [все варианты] —
    в отчёт: расхождение внутри внешнего кода это вопрос к человеку, а не повод молчать).
    """
    values = [c[field] for c in cards if c.get(field) not in (None, "", 0)]
    if not values:
        return None, []
    counts = Counter(values)
    pool = [v for v in counts if keep(v)] if keep else list(counts)
    pool = pool or list(counts)
    top = max(counts[v] for v in pool)
    best = [v for v in pool if counts[v] == top]
    if len(best) == 1:
        chosen = best[0]
    elif tie:
        chosen = sorted(best, key=tie, reverse=True)[0]
    else:
        fresh = sorted((c for c in cards if c.get(field) in best),
                       key=lambda c: c["updated"] or date.min, reverse=True)
        chosen = fresh[0][field]
    return chosen, [v for v, _ in counts.most_common()]


def _wb_score(value):
    """Насколько вариант «Название WB» похож на живой формат каталога.

    Формат каталога — «Картридж TK-520C Kyocera голубой»: без предлога «для» (его нет
    в 93% значений) и без аббревиатуры поставщика в начале модели («Картридж SP TN-221C»).
    Слипшееся «106R03766для Xerox» — просто опечатка, такой вариант берём последним.
    """
    return (0 if re.search(r"\S(?:для|Для)\b", value) else 1,
            0 if re.match(r"^\S+\s+(SP|CS|GG|NV|PL|GP|T2|HB)\s", value) else 1,
            0 if " для " in value else 1,
            len(value))


def _pages(text):
    """Ресурс из названия. Меньше сотни страниц картриджей не бывает — такое отсеиваем."""
    out = {int(re.sub(r"\s+", "", raw)) for raw in PAGES_RE.findall(text or "")}
    out |= {int(raw) * 1000 for raw in PAGES_K_RE.findall(text or "")}
    return {v for v in out if 100 <= v <= 2_000_000}


def build(supplier_key, decisions=("matched",), limit=None):
    """Строки файла импорта + замечания по каждой строке."""
    profile = get_profile(supplier_key)
    rows = query(
        """SELECT id, article, name, ms_code, price_rub, link
             FROM prc_novelty
            WHERE supplier_key = %s AND decision = ANY(%s) AND ms_code IS NOT NULL
            ORDER BY id""",
        (profile.key, list(decisions)))
    if limit:
        rows = rows[:limit]
    kin = family({(r["ms_code"] or "")[:4] for r in rows})
    known = {r["article"].strip().upper() for r in query(
        "SELECT article FROM ms_product WHERE article IS NOT NULL AND NOT archived")}

    records, notes = [], []
    for row in rows:
        ext = (row["ms_code"] or "")[:4]
        cards = kin.get(ext) or []
        flags = []
        if not cards:
            notes.append((row["id"], row["article"], ["нет живой родни по внешнему коду — проверить код"]))
            continue
        path, path_all = _pick(
            cards, "path",
            keep=lambda p: p.startswith(GROUP_PREFIX) and not p.startswith(GROUP_SUPPLIER))
        # Вес: при равенстве голосов берём БОЛЬШИЙ — занижение веса дороже завышения.
        weight, weight_all = _pick(cards, "weight", tie=lambda w: w)
        code128, bc_all = _pick(cards, "code128")
        wb, wb_all = _pick(cards, "wb", tie=_wb_score)

        if len(path_all) > 1:
            flags.append(f"группа у родни разная: {' / '.join(path_all)} → взял «{path}»")
        if len(bc_all) > 1:
            flags.append(f"штрихкод у родни разный: {' / '.join(bc_all)} → взял {code128}")
        if not code128:
            flags.append("у родни нет Code128 — заполнить вручную")
        if len(wb_all) > 1:
            flags.append(f"«Название WB» у родни разное: {' / '.join(wb_all)} → взял «{wb}»")
        if not wb:
            flags.append("у родни нет «Название WB» — заполнить вручную")
        if weight_all and max(weight_all) > min(weight_all) * 1.2:
            flags.append(f"вес у родни расходится: {sorted(weight_all)} → взял {weight}, проверить")
        if not weight:
            weight, said = weight_from_dims(cards)
            flags.append(said or "веса нет ни у родни, ни в прайсах поставщиков — заполнить по nix.ru")
        if row["price_rub"] is None:
            flags.append("нет цены в прайсе")
        if row["article"].strip().upper() in known:
            flags.append("артикул уже есть в МС — импорт по артикулу ПЕРЕПИШЕТ ту карточку")
        if row["link"]:
            flags.append(f"есть связь {row['link']} — решить, писать ли её в «Связь»")
        new_pages, kin_pages = _pages(row["name"]), set().union(*(_pages(c["name"]) for c in cards))
        if new_pages and kin_pages and not (new_pages & kin_pages):
            flags.append(f"ресурс отличается от родни: {sorted(new_pages)} против {sorted(kin_pages)}")

        records.append({
            "Группы": path or "",
            "Код": f"{ext}{suffix(row['article'], profile.key)}",
            "Внешний код": ext,
            "Наименование": row["name"],
            "Описание": row["name"],
            "Артикул": row["article"],
            "Доп. поле: Код поставщика": row["article"],
            "Единица измерения": UOM,
            "Закупочная цена": float(row["price_rub"]) if row["price_rub"] is not None else "",
            "НДС": VAT,
            "Поставщик": MS_SUPPLIER.get(profile.key, profile.title),
            "Вес": weight or "",
            "Страна": COUNTRY,
            "Доп. поле: Гарантия/ Срок службы": WARRANTY,
            "Доп. поле: Название WB": wb or "",
            "Штрихкод Code128": code128 or "",
            "Доп. поле: Связь": "",
        })
        if flags:
            notes.append((row["id"], row["article"], flags))
    return records, notes


def write_xlsx(records, path):
    from openpyxl import Workbook
    wb = Workbook()
    sheet = wb.active
    sheet.title = "Товары"
    sheet.append(COLUMNS)
    for rec in records:
        sheet.append([rec.get(col, "") for col in COLUMNS])
    for col, width in zip("ABCDEFGHIJKLMNOPQ", (28, 10, 12, 70, 70, 24, 24, 8, 14, 6, 26, 8, 10, 10, 46, 16, 10)):
        sheet.column_dimensions[col].width = width
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def write_notes(notes, path, total):
    lines = [f"# Файл импорта в МС: что проверить\n",
             f"Строк в файле: {total}. Строк с замечаниями: {len(notes)}.\n"]
    for novelty_id, article, flags in notes:
        lines.append(f"\n## {article} (строка новинок {novelty_id})\n")
        lines += [f"- {f}\n" for f in flags]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")
    return path


def main():
    ap = argparse.ArgumentParser(description="файл импорта новых карточек в МойСклад")
    ap.add_argument("supplier", help="ключ поставщика (kaktus, colortek, …)")
    ap.add_argument("--decision", default="matched", help="какие строки новинок брать")
    ap.add_argument("--limit", type=int, help="взять только первые N строк")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "docs" / "prc"))
    args = ap.parse_args()

    profile = get_profile(args.supplier)
    records, notes = build(profile.key, tuple(args.decision.split(",")), args.limit)
    stamp = f"{date.today():%Y-%m-%d}"
    out = Path(args.out)
    xlsx = write_xlsx(records, out / f"{profile.key}_{stamp}_ms_import.xlsx")
    md = write_notes(notes, out / f"{profile.key}_{stamp}_ms_import_notes.md", len(records))
    print(f"строк в файле: {len(records)}; с замечаниями: {len(notes)}")
    print(f"файл импорта: {xlsx}")
    print(f"замечания:    {md}")


if __name__ == "__main__":
    main()
