# поток: prc
"""tools/prc/unlink_fill.py — дозаполнить карточки, сведённые кнопкой «Свести».

`prices/unlink_apply.py` ставит карточке поставщика наш внешний код, `code` с аббревиатурой
бренда и «Название WB». Остальное у такой карточки пустое: она пришла из оприходования и,
кроме имени и артикула поставщика, ничего не несёт. Здесь добираем то, что человек заполняет
руками (задача Сергея 18.08.2026):

  Описание            — наименование товара КАК У ПОСТАВЩИКА, то есть имя самой карточки
                        (так же устроены живые карточки родни: описание = их имя из прайса);
  Группа              — папка родни по НАШЕМУ внешнему коду («Картриджи Canon», «струйные epson»…),
                        берём ту, что стоит у живых карточек этого же кода;
  Поставщик           — контрагент по поставщику карточки (`SUPPLIER`), проверено по живой родне:
                        булат (sl/el/bt) → ИП Капшук, ВТТ (hb/np) → ООО «КПД»(ВТТ),
                        кактус (cs) → ООО «КОМПАНИЯ ФЕРРЕТ»;
  Единица измерения   — «шт»;
  Штрихкод Code128    — тот же, что у родни по внешнему коду: code128 у нас общий на ТОВАР
                        (одно значение на всех поставщиков), в отличие от личного ean8;
  Вес                 — как у родни того же кода, заведённой ПОСЛЕДНЕЙ (задача Сергея 18.08.2026).
                        Даты создания в карточке МС нет, но id карточки — UUID v1, и в нём зашито
                        время создания: сверено с журналом изменений (`/audit`, событие `create`)
                        — совпадает до секунды. Берём последнюю по этому времени карточку, У КОТОРОЙ
                        вес не нулевой: копировать ноль бессмысленно, а нули в родне встречаются.

Пустые поля только дозаполняем: что уже стоит на карточке — не трогаем. Родни с папкой или
code128 нет → строка уезжает в отбраковку, к человеку.

    ./venv/bin/python tools/prc/unlink_fill.py           # проба, ничего не пишем
    ./venv/bin/python tools/prc/unlink_fill.py --apply
"""
import sys
import time
import uuid
import datetime
import pathlib
import argparse
import collections

BASE_DIR = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))

from core import db, ms_api                                    # noqa: E402

# Контрагент «Поставщик» по поставщику карточки. Значения сверены по живым карточкам той же
# аббревиатуры (12 случайных на каждую: sl 11/12, el 12/12, bt 11/12, hb 12/12, np 12/12, cs 12/12).
SUPPLIER = {
    "bulat": ("0b9561fe-1468-11ec-0a80-05dc0004fec6", "ИП Капшук Наталья Сергеевна"),
    "vtt": ("3e1d1aab-5f56-11e9-9109-f8fc00164628", 'ООО "КПД"(ВТТ)'),
    "kaktus_msk": ("0b14dc16-c51e-11ef-0a80-0c79000af987", 'ООО "КОМПАНИЯ ФЕРРЕТ"'),
    # блоссом (bs): 10 из 12 живых карточек бренда стоят на «КОМПАНИЯ БЛОССОМ», остальные —
    # на «КАРТРИДЖ ТРЕЙД» (Блоссом); берём преобладающего.
    "blossom": ("141a0940-99f7-11e7-7a34-5acf0005e7dc", 'ООО "КОМПАНИЯ БЛОССОМ"'),
}
UOM_NAME = "шт"
UUID_EPOCH = datetime.datetime(1582, 10, 15)
PAUSE = 0.05


def uom_ref():
    """Ссылка на единицу измерения «шт» из справочника МС."""
    rows = (ms_api.get("/entity/uom", params={"filter": f"name={UOM_NAME}", "limit": 5})
            or {}).get("rows") or []
    for r in rows:
        if (r.get("name") or "").strip() == UOM_NAME:
            return {"meta": r["meta"]}
    raise RuntimeError(f"в справочнике МС нет единицы измерения «{UOM_NAME}»")


def born(ms_id):
    """Когда карточка заведена: время из UUID v1 (в самой карточке даты создания нет)."""
    f = uuid.UUID(ms_id).fields
    ticks = ((f[2] & 0x0fff) << 48) | (f[1] << 32) | f[0]
    return UUID_EPOCH + datetime.timedelta(microseconds=ticks // 10)


def kin(code, skip_id):
    """Папка, code128 и вес живой родни по внешнему коду. Пусто — значит образца нет."""
    folder, codes, weights = None, collections.Counter(), []
    for t in sorted(db.query("SELECT ms_id FROM ms_product WHERE external_code = %s "
                             "AND NOT archived AND ms_id <> %s", (code, skip_id)),
                    key=lambda t: born(t["ms_id"]), reverse=True):
        card = ms_api.get(f"/entity/product/{t['ms_id']}", params={"expand": "productFolder"})
        if folder is None and card.get("productFolder"):
            folder = {"meta": card["productFolder"]["meta"]}
        for b in card.get("barcodes") or []:
            if "code128" in b:
                codes[b["code128"]] += 1
        if card.get("weight"):
            weights.append((born(t["ms_id"]), float(card["weight"])))
        time.sleep(PAUSE)
        if folder and codes and weights:
            break
    weight = max(weights)[1] if weights else None
    return folder, (codes.most_common(1)[0][0] if codes else None), weight


def plan(ids=None):
    """Что дозаполним. Источник — строки, уже сведённые кнопкой «Свести»."""
    sql = ("SELECT ms_id, article, name, supplier_key, target_code FROM prc_unlinked "
           "WHERE decision = 'matched' AND target_code ~ '^[0-9]{4}$'")
    params = ()
    if ids:
        sql += " AND ms_id = ANY(%s)"
        params = (list(ids),)
    rows = db.query(sql, params)
    uom = uom_ref()
    todo, drop = [], []
    for r in rows:
        card = ms_api.get(f"/entity/product/{r['ms_id']}", params={"expand": "productFolder"})
        if (card.get("externalCode") or "") != r["target_code"]:
            drop.append({**r, "why": "внешний код на карточке не совпал — сначала «Свести»"})
            continue
        sup = SUPPLIER.get(r["supplier_key"])
        if not sup:
            drop.append({**r, "why": f"не знаем контрагента поставщика «{r['supplier_key']}»"})
            continue
        folder, c128, weight = kin(r["target_code"], r["ms_id"])
        if not folder or not c128:
            drop.append({**r, "why": "у родни по внешнему коду нет "
                                     + ("папки" if not folder else "штрихкода code128")})
            continue
        have = card.get("barcodes") or []
        item = {"ms_id": r["ms_id"], "article": r["article"], "name": r["name"],
                "code": r["target_code"],
                "supplier": None if card.get("supplier") else sup,
                "supplier_name": sup[1],
                "uom": None if card.get("uom") else uom,
                "folder": None if card.get("productFolder") else folder,
                "description": None if (card.get("description") or "").strip() else card["name"],
                "barcodes": None if any("code128" in b for b in have) else have + [{"code128": c128}],
                "c128": c128, "folder_had": bool(card.get("productFolder")),
                "weight": None if card.get("weight") else weight,
                "weight_had": bool(card.get("weight"))}
        todo.append(item)
        time.sleep(PAUSE)
    return todo, drop


def apply(todo, dry=True, log=print):
    """Запись пачками по 100 — bulk POST обновляет карточки по meta, поля мержатся."""
    done = 0
    for start in range(0, len(todo), 100):
        chunk = todo[start:start + 100]
        body = []
        for r in chunk:
            payload = {"meta": {"href": f"{ms_api.BASE}/entity/product/{r['ms_id']}",
                                "type": "product", "mediaType": "application/json"}}
            if r["supplier"]:                       # что на карточке уже стоит — не трогаем
                payload["supplier"] = ms_api.ref("counterparty", r["supplier"][0])
            if r["uom"]:
                payload["uom"] = r["uom"]
            if r["folder"]:
                payload["productFolder"] = r["folder"]
            if r["description"]:
                payload["description"] = r["description"]
            if r["barcodes"]:
                payload["barcodes"] = r["barcodes"]
            if r["weight"]:
                payload["weight"] = r["weight"]
            if len(payload) > 1:                    # нечего дозаполнять — карточку не дёргаем
                body.append(payload)
        if not body:
            continue
        if dry:
            log(f"[проба] пачка {start // 100 + 1}: {len(body)} карточек — ничего не записано")
            continue
        ms_api.post("/entity/product", body)
        done += len(body)
        log(f"[запись] {done} из {len(todo)}")
        time.sleep(PAUSE)
    return done


def note(todo, drop):
    """Короткая сводка для UI: что дозаполнили, что не смогли."""
    if not todo and not drop:
        return ""
    lines = []
    if todo:
        filled = []
        for field, key in (("описание", "description"), ("группа", "folder"),
                           ("поставщик", "supplier"), ("ед. изм.", "uom"),
                           ("вес", "weight"), ("code128", "barcodes")):
            n = sum(1 for i in todo if i.get(key))
            if n:
                filled.append(field)
        lines.append("дозаполнено: " + (", ".join(filled) if filled else "нечего, всё уже стояло"))
    if drop:
        lines.append("; ".join(f"{d['article']}: {d['why']}" for d in drop[:3]))
    return " | ".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Дозаполнить сведённые карточки поставщиков")
    ap.add_argument("--apply", action="store_true", help="писать в МойСклад (по умолчанию проба)")
    ap.add_argument("--id", action="append", help="только эти ms_id")
    args = ap.parse_args(argv)

    todo, drop = plan(args.id)
    for d in drop:
        print(f"  ✗ {d['article']} → {d['target_code']}: {d['why']}")
    by_sup = collections.Counter(i["supplier_name"] for i in todo)
    print(f"  к записи {len(todo)}, отбраковано {len(drop)}")
    print("  поставщик: " + ", ".join(f"{n} — {c}" for (n), c in by_sup.most_common()))
    print(f"  описание заполним у {sum(1 for i in todo if i['description'])}, "
          f"code128 добавим {sum(1 for i in todo if i['barcodes'])}, "
          f"папка уже стояла у {sum(1 for i in todo if i['folder_had'])}")
    no_w = [i["article"] for i in todo if not i["weight"] and not i["weight_had"]]
    print(f"  вес проставим у {sum(1 for i in todo if i['weight'])}, "
          f"уже стоял у {sum(1 for i in todo if i['weight_had'])}"
          + (f"; неоткуда взять (у родни ноль): {len(no_w)} — {', '.join(no_w[:5])}" if no_w else ""))
    apply(todo, dry=not args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
