# поток: prc
"""tools/prc/cat_fix.py — порядок в категориях (группах товаров) МойСклада.

Две беды, обе от ручного заведения карточек:
  1) одна и та же категория заведена несколько раз в разном написании («Картриджи Avision»,
     «Картриджи для Avision», корневая «Картриджи для Avison» с опечаткой; Катюша — трижды);
  2) 3.5 тыс. карточек лежат вне дерева, а ещё 2.3 тыс. — в мусорных папках выгрузки сайта
     («Товары интернет-магазинов/…exchange1c.php»).

Категорию НЕ угадываем по названию карточки: берём бренд принтера из каталога TheCartridge
(`prc_tc_model`, тот же источник, что и «Название WB»). Работаем только по карточкам, у которых
внешний код — ровно 4 цифры (наш картридж) и код есть в каталоге ТК; всё остальное не трогаем.

Правило одно для всех: где бы карточка ни лежала (вне дерева, в мусорной папке сайта или
в чужой брендовой папке), она должна оказаться в папке бренда своего принтера. Поэтому
инструмент заодно чинит и уже разложенное — в том числе расформировывает `Картриджи G&G`:
G&G — производитель совместимок, а не бренд принтера, папка выбивалась из логики дерева.

Решения Сергея 17.08.2026:
  · наборы («Комплект картриджей») кладём по бренду вместе с одиночными — отдельной ветки нет;
  · дерево «Оригинальные картриджи/…» не трогаем совсем: это другой товар (оригиналы);
  · мусорные папки интернет-магазина раскладываем на общих правилах;
  · «Картриджи Pantum совм.» переименовать в «Картриджи Pantum» (оригиналов Pantum больше нет);
  · завести «Картриджи Hyundai»; матричные и ризограф — по бренду, своих папок не заводим.

    ./venv/bin/python tools/prc/cat_fix.py            # отчёт, ничего не меняет
    ./venv/bin/python tools/prc/cat_fix.py --apply
"""
import re
import sys
import csv
import time
import pathlib
import argparse
import collections
from datetime import date

BASE_DIR = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))

from core import db, ms_api                                    # noqa: E402
from tools.prc.wb_fill import brand_of, PAUSE, REPORT_DIR      # noqa: E402

ROOT = "Картриджи"

# Слить дубли: откуда → куда. Опустевшие папки удаляем в конце прогона.
MERGE = [
    ("Картриджи/Картриджи для Avision", "Картриджи/Картриджи Avision"),
    ("Картриджи для Avison", "Картриджи/Картриджи Avision"),
    ("Картриджи/Картриджи для Katusha", "Картриджи/Картриджи Катюша"),
    ("Картриджи для Katusha", "Картриджи/Картриджи Катюша"),
    ("Картриджи/Картриджи для Deli", "Картриджи/Картриджи Deli"),
    ("Картриджи/Картриджи для принтеров Deli", "Картриджи/Картриджи Deli"),
    ("Картриджи/Картриджи для F+", "Картриджи/Картриджи F+"),
    ("Картриджи/Картриджи для Huawei", "Картриджи/Картриджи Huawei"),
    ("Картриджи Oki", "Картриджи/Картриджи Oki"),
    ("Картриджи Lexmark", "Картриджи/Картриджи Lexmark"),
    ("Ленточные картриджи", "Картриджи/Ленточные картриджи"),
]

RENAME = {"Картриджи/Картриджи Pantum совм.": "Картриджи Pantum"}
CREATE = ["Картриджи/Картриджи Hyundai", "Картриджи/Картриджи Т1000"]

# Бренд принтера из ТК (уже сокращённый в brand_of) → папка МС. Имена папок историчные
# («Kyocera Mita», «BULAT», «Primera Bravo») — переименовывать их Сергей не просил.
BRAND_FOLDER = {
    "HP": "Картриджи HP", "Canon": "Картриджи Canon", "Epson": "Картриджи Epson",
    "Brother": "Картриджи Brother", "Xerox": "Картриджи Xerox", "Samsung": "Картриджи Samsung",
    "Kyocera": "Картриджи Kyocera Mita", "Konica": "Картриджи Konica Minolta",
    "Ricoh": "Картриджи Ricoh", "OKI": "Картриджи Oki", "Lexmark": "Картриджи Lexmark",
    "Pantum": "Картриджи Pantum", "Sharp": "Картриджи Sharp", "Toshiba": "Картриджи Toshiba",
    "Panasonic": "Картриджи Panasonic", "Катюша": "Картриджи Катюша", "Deli": "Картриджи Deli",
    "Avision": "Картриджи Avision", "Huawei": "Картриджи Huawei", "Sindoh": "Картриджи Sindoh",
    "F+": "Картриджи F+", "G&G": "Картриджи G&G", "Bulat": "Картриджи BULAT",
    "Primera": "Картриджи Primera Bravo", "Dell": "Картриджи Dell",
    "Hyundai": "Картриджи Hyundai", "Т1000": "Картриджи Т1000",
}
# У этих брендов струйное живёт в своей подпапке — она уже заведена и наполнена.
INK_SUB = {"HP": "струйные hp", "Canon": "струйные canon",
           "Epson": "струйные epson", "Brother": "струйные brother"}
INK_MARK = "для струйного принтера"

SHOP_ROOT = "Товары интернет-магазинов"      # мусорные папки выгрузки сайта
EXT_RE = re.compile(r"^[0-9]{4}$")

# Не трогаем совсем: оригиналы — другой товар, там своё дерево (решение Сергея 17.08.2026).
ORIG_ROOT = "Оригинальные картриджи"
# Папки по ТИПУ расходника, а не по бренду принтера: несовпадение бренда там законно и
# исправлять его не надо (решение Сергея 17.08.2026). Сюда же попадают ленточные брендов,
# под которые брендовых папок нет вовсе (Dymo, Brady, Olivetti, DUPLO, Riso …).
TYPE_FOLDERS = {
    "Картриджи/Ленточные картриджи", "Картриджи/Мастер пленка",
    "Картриджи/Картриджи Panasonic/Термопленка", "Фотобумага", "Чипы", "Упаковка",
}


def folders():
    """Полный путь папки → её строка из МС."""
    out = {}
    for f in ms_api.iter_rows("/entity/productfolder", page=1000):
        path = (f["pathName"] + "/" if f.get("pathName") else "") + f["name"]
        out[path] = f
    return out


def target_path(tc):
    """Путь папки для карточки или (None, причина)."""
    brand, _ = brand_of(tc)
    if not brand:
        return None, "в ТК нет бренда принтера"
    folder = BRAND_FOLDER.get(brand)
    if not folder:
        return None, f"нет папки под бренд {brand}"
    path = f"{ROOT}/{folder}"
    if (tc.get("assignment") or "").strip() == INK_MARK and brand in INK_SUB:
        path += "/" + INK_SUB[brand]
    return path, ""


def catalog():
    rows = db.query("SELECT external_code, raw FROM prc_tc_model WHERE gone_at IS NULL")
    return {r["external_code"]: r["raw"] for r in rows}


def cards_live(pause=None):
    """Карточки МС постранично, с паузой (днём МС занят рабочими процессами).

    Отдаём генератором и только нужные поля: полный каталог карточек целиком в память
    не влезает — сервер ложился по OOM на 45 тыс. payload'ов.
    """
    offset = 0
    while True:
        chunk = ms_api.get("/entity/product", {"limit": 1000, "offset": offset})
        page = chunk.get("rows", [])
        for c in page:
            yield {"id": c["id"], "code": c.get("code"), "name": c.get("name"),
                   "externalCode": c.get("externalCode"), "pathName": c.get("pathName"),
                   "folder": c.get("productFolder") is not None}
        offset += len(page)
        if not page or offset >= chunk.get("meta", {}).get("size", 0):
            return
        time.sleep(PAUSE if pause is None else pause)


def plan(cards, cat, merge_map):
    """Список переездов + статистика отказов."""
    moves, skip = [], collections.Counter()
    ex = collections.defaultdict(list)
    seen = 0
    for card in cards:
        seen += 1
        path = card.get("pathName")
        has_folder = card["folder"]
        in_shop = has_folder and (path == SHOP_ROOT or (path or "").startswith(SHOP_ROOT + "/"))

        if has_folder and path in merge_map:              # слияние дублей — без каталога ТК
            moves.append({"card": card, "to": merge_map[path], "why": "слияние дублей"})
            continue
        if has_folder and (path == ORIG_ROOT or (path or "").startswith(ORIG_ROOT + "/")):
            skip["оригиналы — не трогаем"] += 1
            continue
        if has_folder and path in TYPE_FOLDERS:
            skip["папка по типу товара"] += 1
            continue

        ext = (card.get("externalCode") or "").strip()
        if not EXT_RE.match(ext):
            skip["внешний код не 4 цифры"] += 1
            continue
        tc = cat.get(ext)
        if tc is None:
            skip["кода нет в каталоге ТК"] += 1
            continue
        to, reason = target_path(tc)
        if reason:
            skip[reason] += 1
            if len(ex[reason]) < 5:
                ex[reason].append(f"{ext} {card.get('code')}")
            continue
        if to == path:
            skip["уже в нужной папке"] += 1
            continue
        moves.append({"card": card, "to": to,
                      "why": "из мусорной папки сайта" if in_shop else
                             "не в своей категории" if has_folder else "была без категории"})
    return moves, skip, ex, seen


def write_report(moves, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["код", "внешний код", "наименование МС", "было", "станет", "почему"])
        for m in sorted(moves, key=lambda x: (x["to"], x["card"].get("code") or "")):
            c = m["card"]
            w.writerow([c.get("code"), c.get("externalCode"), c.get("name"),
                        c.get("pathName") or "<без категории>", m["to"], m["why"]])
    return path


def prepare_folders(tree, dry=True, log=print):
    """Переименования и новые папки. Возвращает обновлённое дерево."""
    for old, new_name in RENAME.items():
        f = tree.get(old)
        if not f or f["name"] == new_name:
            continue
        log(f"[папка] переименование: {old} → {new_name}")
        if not dry:
            ms_api.put(f"/entity/productfolder/{f['id']}", {"name": new_name})
            time.sleep(PAUSE)
    for path in CREATE:
        if path in tree:
            continue
        parent_path, name = path.rsplit("/", 1)
        log(f"[папка] создать: {path}")
        if not dry:
            ms_api.post("/entity/productfolder", {
                "name": name,
                "productFolder": {"meta": tree[parent_path]["meta"]}})
            time.sleep(PAUSE)
    return tree if dry else folders()


def apply_moves(moves, tree, dry=True, log=print):
    """Проставляем категорию пачками по 100 (bulk POST обновляет по meta)."""
    done = 0
    for start in range(0, len(moves), 100):
        chunk = moves[start:start + 100]
        body = []
        for m in chunk:
            body.append({
                "meta": {"href": f"{ms_api.BASE}/entity/product/{m['card']['id']}",
                         "type": "product", "mediaType": "application/json"},
                "productFolder": {"meta": tree[m["to"]]["meta"]}})
        if dry:
            log(f"[проба] пачка {start // 100 + 1}: {len(body)} карточек")
            continue
        ms_api.post("/entity/product", body)
        done += len(body)
        log(f"[перенос] {done} из {len(moves)}")
        if start + 100 < len(moves):
            time.sleep(PAUSE)
    return done


def drop_empty(tree, dry=True, log=print):
    """Удаляем слитые папки — только убедившись, что в них не осталось ни одной карточки."""
    killed = 0
    for src, dst in MERGE:
        f = tree.get(src)
        if not f:
            continue
        # У товаров нет фильтра productFolder (МС отвечает 412 «неизвестное поле фильтрации»),
        # зато есть pathName — полный путь папки товара.
        left = ms_api.get("/entity/product", {"limit": 1, "filter": f"pathName={src}"})
        n = left.get("meta", {}).get("size", 0)
        if n:
            log(f"[папка] НЕ удаляю {src}: осталось карточек {n}")
            continue
        # Архивных в общем списке не видно, но папку они держат: МС отвечает 409 «объект
        # уже используется». Переносим их туда же, куда ушли живые.
        arch = ms_api.get("/entity/product",
                          {"limit": 100, "filter": f"pathName={src};archived=true"}).get("rows", [])
        if arch:
            log(f"[папка] {src}: переношу архивных {len(arch)}")
            if not dry:
                ms_api.post("/entity/product", [
                    {"meta": {"href": f"{ms_api.BASE}/entity/product/{c['id']}",
                              "type": "product", "mediaType": "application/json"},
                     "productFolder": {"meta": tree[dst]["meta"]}} for c in arch])
                time.sleep(PAUSE)
        log(f"[папка] удалить пустую: {src}")
        if not dry:
            ms_api.delete(f"/entity/productfolder/{f['id']}")
            killed += 1
        time.sleep(PAUSE)
    return killed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="менять МойСклад (иначе только отчёт)")
    ap.add_argument("--out", help="путь отчёта")
    ap.add_argument("--pause", type=float, help="пауза между обращениями к МС, сек")
    args = ap.parse_args()
    dry = not args.apply
    if args.pause:
        global PAUSE
        PAUSE = args.pause

    tree = folders()
    tree = prepare_folders(tree, dry=dry)
    missing = [p for p in set(BRAND_FOLDER.values())
               if f"{ROOT}/{p}" not in tree and f"{ROOT}/{p}" not in CREATE]
    if missing and not dry:
        sys.exit("нет папок в МС: " + ", ".join(sorted(missing)))

    merge_map = {src: dst for src, dst in MERGE}
    cat = catalog()
    moves, skip, ex, seen = plan(cards_live(), cat, merge_map)

    out = pathlib.Path(args.out) if args.out else \
        REPORT_DIR / f"prc_cat_fix_{date.today():%Y-%m-%d}.csv"
    write_report(moves, out)

    print(f"карточек в МС: {seen}")
    print(f"переносим: {len(moves)}")
    for why, n in collections.Counter(m["why"] for m in moves).most_common():
        print(f"   {n:5}  {why}")
    for reason, n in skip.most_common():
        tail = f"  (напр. {', '.join(ex[reason])})" if ex.get(reason) else ""
        print(f"   не берём — {reason}: {n}{tail}")
    print(f"отчёт: {out}")

    if not args.apply:
        print("МойСклад НЕ менялся — добавьте --apply")
        return
    apply_moves(moves, tree, dry=False)
    print("удалено пустых папок:", drop_empty(tree, dry=False))


if __name__ == "__main__":
    main()
