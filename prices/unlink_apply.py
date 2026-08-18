# поток: prc
"""prices/unlink_apply.py — «Свести» несопоставленную карточку с нашим внешним кодом.

Задача. В оприходованиях на «Удаленном складе» лежат карточки, у которых есть только артикул
поставщика: внешнего кода (наши 4 цифры) нет, поэтому остаток видит МойСклад и не видит
TheCartridge — товар не продаётся. Карточку НЕ дублируем и НЕ переписываем документы: она
остаётся той же, ей проставляется наш внешний код (решение Сергея 18.08.2026).

Что пишем в карточку:
  externalCode  — наш 4-значный код товара (из вкладки «Несопоставлено», выбор человека);
  code          — внутренний код `<внешний код><аббревиатура бренда>`. Бренд читаем ИЗ НАЗВАНИЯ
                  товара (`ms_import.abbr_by_name`, правило Сергея 18.08.2026), не из артикула:
                  у Булата под одним поставщиком едут s-Line / e-Line / Булат, у ВТТ —
                  Hi-Black / NetProduct. Бренда в названии нет → правило поставщика
                  (`CODE_SUFFIX`), нет и его → код НЕ придумываем, карточка уезжает без
                  внутреннего кода и попадает в сводку — доназначает человек;
  «Название WB» — доп. поле по формуле `tools/prc/wb_fill.compose` из каталога ТК; заполняем,
                  только если поле пустое, а внешний код есть в `prc_tc_model`.

Проверки до записи (строка отбраковывается, ничего не пишем):
  · целевой код не 4 цифры;
  · карточка уже несёт наш 4-значный внешний код (сведена раньше);
  · под этим внешним кодом уже живёт карточка с ТАКОЙ ЖЕ аббревиатурой бренда — это дубль
    того же поставщика, значит настоящая пара уже есть и решает человек.

    ./venv/bin/python -m prices.unlink_apply --dry            # что бы записали (по decision=matched)
    ./venv/bin/python -m prices.unlink_apply --apply
"""
import re
import sys
import time
import pathlib
import argparse

BASE_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from core import db, ms_api                                    # noqa: E402
from prices import ms_import                                   # noqa: E402
from tools.prc import wb_fill                                  # noqa: E402

OUR_CODE_RE = re.compile(r"^\d{4}$")
PAUSE = 0.3


def _suffix(article, supplier_key, name=""):
    """Аббревиатура бренда или None — незнакомый бренд не выдумываем.

    Сначала БРЕНД ИЗ НАЗВАНИЯ (`ms_import.abbr_by_name`) — так делает человек: у Булата и ВТТ
    под одним поставщиком едут разные бренды (s-Line / e-Line / Булат, Hi-Black / NetProduct),
    и артикул их не различает. Не нашли бренда — падаем на правило поставщика (Кактус, Сакура,
    Колортек, Одиссей: там бренд один или задан префиксом артикула). Нет и его — код не ставим.
    """
    found = ms_import.abbr_by_name(name)
    if found:
        return found
    try:
        return ms_import.suffix(article or "", supplier_key or "", name)
    except KeyError:
        return None


def _twins(codes):
    """внешний код → {аббревиатура: код} живой родни (чтобы не сделать дубль поставщика)."""
    out = {}
    for code in sorted(set(codes)):
        rows = db.query("SELECT code FROM ms_product WHERE external_code = %s AND NOT archived",
                        (code,))
        seen = {}
        for r in rows:
            ms_code = (r["code"] or "").strip()
            if ms_code.startswith(code):
                seen[ms_code[4:].strip().lower()] = ms_code
        out[code] = seen
    return out


def plan(ids=None):
    """Строки к записи и отбраковка. Источник — решения «Свести» во вкладке «Несопоставлено»."""
    sql = ("SELECT ms_id, article, name, supplier_key, target_code, ms_code "
           "FROM prc_unlinked WHERE decision = 'matched' AND target_code ~ '^[0-9]{4}$'")
    params = ()
    if ids:
        sql += " AND ms_id = ANY(%s)"
        params = (list(ids),)
    rows = db.query(sql, params)
    if not rows:
        return [], []

    catalog = wb_fill.tc_models()
    twins = _twins([r["target_code"] for r in rows])
    todo, drop = [], []
    for r in rows:
        code = r["target_code"]
        try:
            card = ms_api.get(f"/entity/product/{r['ms_id']}")
        except Exception as exc:                       # карточка удалена/нет прав
            drop.append({**r, "why": f"карточка не читается: {exc}"})
            continue
        have = (card.get("externalCode") or "").strip()
        if OUR_CODE_RE.match(have):
            drop.append({**r, "why": f"карточка уже сведена: внешний код {have}"})
            continue
        abbr = _suffix(r["article"], r["supplier_key"], r["name"])
        if abbr and abbr in twins.get(code, {}):
            drop.append({**r, "why": f"под кодом {code} уже есть карточка «{twins[code][abbr]}» "
                                     f"того же бренда — это дубль, решает человек"})
            continue
        item = {"ms_id": r["ms_id"], "article": r["article"], "name": r["name"],
                "supplier_key": r["supplier_key"], "external_code": code,
                "code": f"{code}{abbr}" if abbr else None,
                "attributes": card.get("attributes") or [], "wb_name": None, "wb_why": ""}
        if wb_fill._wb_value(card):
            item["wb_why"] = "поле уже заполнено"
        elif code not in catalog:
            item["wb_why"] = "внешнего кода нет в каталоге ТК"
        else:
            value, why = wb_fill.compose(catalog[code])
            item["wb_name"], item["wb_why"] = (value or None), why
        todo.append(item)
        time.sleep(PAUSE)
    return todo, drop


def apply(todo, dry=True, log=print):
    """Запись пачками по 100: bulk POST /entity/product обновляет карточки по meta."""
    done = 0
    for start in range(0, len(todo), 100):
        chunk = todo[start:start + 100]
        body = []
        for r in chunk:
            payload = {"meta": {"href": f"{ms_api.BASE}/entity/product/{r['ms_id']}",
                                "type": "product", "mediaType": "application/json"},
                       "externalCode": r["external_code"]}
            if r["code"]:
                payload["code"] = r["code"]
            if r["wb_name"]:
                attrs = [a for a in r["attributes"] if a.get("name") != wb_fill.WB_ATTR]
                attrs.append({
                    "meta": {"href": f"{ms_api.BASE}/entity/product/metadata/attributes/"
                                     f"{wb_fill.ATTR_ID}",
                             "type": "attributemetadata", "mediaType": "application/json"},
                    "type": wb_fill.ATTR_TYPE, "value": r["wb_name"]})
                payload["attributes"] = attrs
            body.append(payload)
        if dry:
            log(f"[проба] пачка {start // 100 + 1}: {len(body)} карточек — ничего не записано")
            continue
        ms_api.post("/entity/product", body)
        done += len(body)
        log(f"[запись] {done} из {len(todo)}")
        if start + 100 < len(todo):
            time.sleep(PAUSE)
    return done


def note(todo, drop):
    """Короткая сводка для чата/UI."""
    lines = [f"к записи {len(todo)}, отбраковано {len(drop)}"]
    no_code = sum(1 for i in todo if not i["code"])
    if no_code:
        lines.append(f"без внутреннего кода {no_code} — нет правила аббревиатуры бренда")
    no_wb = [i["wb_why"] for i in todo if not i["wb_name"] and i["wb_why"]]
    if no_wb:
        lines.append(f"без «Названия WB» {len(no_wb)}: {', '.join(sorted(set(no_wb))[:3])}")
    return "; ".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Свести несопоставленные карточки с нашим кодом")
    ap.add_argument("--apply", action="store_true", help="писать в МойСклад (по умолчанию проба)")
    ap.add_argument("--id", action="append", help="только эти ms_id")
    args = ap.parse_args(argv)

    todo, drop = plan(args.id)
    for d in drop:
        print(f"  ✗ {d['article']} → {d['target_code']}: {d['why']}")
    for i in todo[:10]:
        print(f"  · {i['article']} → {i['external_code']} "
              f"код {i['code'] or '—'} | WB: {i['wb_name'] or '— ' + (i['wb_why'] or '')}")
    if len(todo) > 10:
        print(f"  … ещё {len(todo) - 10}")
    print(" ", note(todo, drop))
    apply(todo, dry=not args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
