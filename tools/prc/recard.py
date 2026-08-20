# поток: prc
# -*- coding: utf-8 -*-
"""Перевод карточки МС на правильный внешний код: архив старой + новая карточка.

Зачем так, а не правкой поля. Внешний код — ключ, по которому остаток уезжает из МС в ТК,
и его история тянется за карточкой (продажи, оприходования, связки площадок). Менять код
у живой карточки — значит задним числом переписать, чем она была (решение Сергея
20.08.2026). Поэтому:

    1) старая карточка уходит в АРХИВ, и с неё снимаются артикул, код, внешний код и
       штрихкод — они уникальны в МС и нужны новой карточке (МС подставит свои значения);
    2) создаётся НОВАЯ карточка с тем же наполнением и ПРАВИЛЬНЫМ внешним кодом,
       поля «Модель / Доп. название модели / Цвет / Чип / Ресурс (поставщика)» заполняются
       по правилам `tools/prc/tc_fields.py` уже от новой карточки ТК.

Архивируем ТОЛЬКО с нулевым остатком на НАШИХ складах (Звездный, Дисквер и прочие свои;
«Удаленный склад» — виртуальный остаток поставщика, он не в счёт). Карточка с остатком
остаётся как есть и попадает в отчёт отдельной строкой.

Порядок шагов внутри карточки обязателен: сначала освободить код/артикул/штрихкод на
старой, потом создавать новую — иначе МС ответит «уже занято». Полный слепок старой
карточки пишется в `backups/` ДО первой правки.

    ./venv/bin/python tools/prc/recard.py                 # отчёт, ничего не пишем
    ./venv/bin/python tools/prc/recard.py --apply --only 3587sk   # одна карточка
    ./venv/bin/python tools/prc/recard.py --apply         # весь список
    ./venv/bin/python tools/prc/recard.py --retire --apply # в архив без замены

Свод правил по полям, архивации и созданию карточки — `docs/PRC_TC_FIELDS_RULES.md`.
"""
import sys
import json
import time
import pathlib
import argparse
from datetime import date

BASE_DIR = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))

from core import db, ms_api                     # noqa: E402
from tools.prc import tc_fields                 # noqa: E402

PLAN_FILE = BASE_DIR / "docs" / "prc_recard_plan.csv"
# Второй режим: карточка вообще не про тот товар, правильной модели ТК в каталоге нет.
# Убираем её в архив по ТОМУ ЖЕ правилу (снять артикул, код, внешний код, штрихкоды) и НЕ
# создаём новую: товар вернётся сам «новинкой» при следующем оприходовании, там его и
# привяжут (решение Сергея 20.08.2026 — раньше планировалось только снять внешний код).
UNLINK_FILE = BASE_DIR / "docs" / "prc_unlink_plan.csv"
PAUSE = 0.3
# «Удаленный склад» — остатки поставщиков, «Транзит» — товар в пути: не наши свободные штуки.
NOT_OURS = ("Удаленный склад", "Транзит")


def stores():
    return {s["id"]: s["name"] for s in ms_api.get("/entity/store", {"limit": 100})["rows"]}


def our_stock(product_id, names):
    """Остаток на НАШИХ складах. Фильтр `product` у отчёта принимает ровно одно значение."""
    total = 0.0
    rows = ms_api.get("/report/stock/bystore", {
        "limit": 100, "filter": f"product={ms_api.BASE}/entity/product/{product_id}"}).get("rows", [])
    for row in rows:
        for x in row.get("stockByStore", []):
            sid = x["meta"]["href"].rsplit("/", 1)[-1].split("?")[0]
            if names.get(sid) not in NOT_OURS:
                total += float(x.get("stock") or 0)
    return total


def plan_rows(path=None, need="новый внешний код"):
    """План правок: код МС, наименование (код не уникален), новый внешний код."""
    import csv
    with (path or PLAN_FILE).open(encoding="utf-8") as f:
        return [r for r in csv.DictReader(f, delimiter=";") if not need or r.get(need)]


def retire(rows, names, apply_, backup):
    """Убрать карточку в архив по общему правилу, новую не создавать."""
    out, held, miss = [], [], []
    for r in rows:
        old = card_of(r["код"], r["наименование МС"], r.get("внешний код"))
        if not old:
            miss.append((r["код"], "карточка не найдена в МС (или их несколько)")); continue
        left = our_stock(old["id"], names)
        if left:
            held.append((r["код"], f"остаток на наших складах {left:g} — не архивируем")); continue
        (backup / f'retire_{r["код"]}_{old["id"]}.json').write_text(
            json.dumps(old, ensure_ascii=False, indent=1), encoding="utf-8")
        if apply_:
            ms_api.put(f'/entity/product/{old["id"]}', strip_body(old))
            time.sleep(PAUSE)
        out.append((r["код"], f'артикул {old.get("article")} / внешний код {old.get("externalCode")} '
                              f'сняты, карточка в архив{"" if apply_ else " [проба]"}'))
    return out, held, miss


def card_of(code, name, ext=None):
    """Код в МС НЕ уникален: сверяем наименование, а если и его мало (у 2651sf две карточки
    с одним именем) — добираем текущим внешним кодом из плана."""
    hits = [c for c in ms_api.get("/entity/product", {"limit": 100, "filter": f"code={code}"}).get("rows", [])
            if (c.get("name") or "").strip() == (name or "").strip()]
    if len(hits) > 1 and ext:
        hits = [c for c in hits if (c.get("externalCode") or "").strip() == ext.strip()] or hits
    return hits[0] if len(hits) == 1 else None


def code128_of(code):
    """Штрихкод code128 принадлежит ТОВАРУ, а не карточке: у всех карточек с одним внешним
    кодом он один и тот же (`DS` + 3 буквы товара + внешний код с нулями, проверено на
    44 828 живых карточках — совпало 37 817, разошлось 16). Сами мы его НЕ генерируем:
    берём готовый у карточки с таким же внешним кодом (решение Сергея 20.08.2026)."""
    for params in ({"filter": f"externalCode={code}"},
                   {"filter": f"externalCode={code};archived=true"}):
        for c in ms_api.get("/entity/product", {"limit": 100, **params}).get("rows", []):
            for b in c.get("barcodes") or []:
                if b.get("code128"):
                    return b["code128"]
    # Парный обмен кодов (6358↔6359): единственный носитель штрихкода — карточка, которую мы
    # сами только что обезличили. Достаём его из слепка, сделанного до правки.
    for f in sorted((BASE_DIR / "backups").glob("prc_recard_*/*.json")):
        old = json.loads(f.read_text(encoding="utf-8"))
        if (old.get("externalCode") or "").strip() == code:
            for b in old.get("barcodes") or []:
                if b.get("code128"):
                    return b["code128"]
    return None


def new_body(old, code, tc_row):
    """Новая карточка = наполнение старой + правильный внешний код + поля из ТК."""
    keep = ("name", "description", "article", "vat", "vatEnabled", "useParentVat",
            "uom", "country", "supplier", "productFolder", "weight", "volume", "buyPrice",
            "minPrice", "salePrices", "trackingType", "paymentItemType")
    body = {k: old[k] for k in keep if old.get(k) not in (None, "")}
    body["externalCode"] = code
    # Код карточки = внешний код + суффикс поставщика: 3588 + sk. Расходиться они не могут.
    body["code"] = code + (old.get("code") or "")[4:]
    # ean8/ean13 у карточки свои — переносим; code128 берём у товара с этим внешним кодом.
    body["barcodes"] = [b for b in (old.get("barcodes") or []) if not b.get("code128")]
    bar = code128_of(code)
    if bar:
        body["barcodes"].append({"code128": bar})
    fresh, _ = tc_fields.plan_card({**old, "externalCode": code, "attributes": []}, tc_row)
    body["attributes"] = [{
        "meta": {"href": f"{ms_api.BASE}/entity/product/metadata/attributes/{tc_fields.ATTRS[f][0]}",
                 "type": "attributemetadata", "mediaType": "application/json"},
        "type": tc_fields.ATTRS[f][1], "value": v} for f, v in fresh.items()]
    return body


def strip_body(old):
    """Освободить у старой карточки уникальные поля и убрать её в архив."""
    return {"meta": old["meta"], "archived": True,
            "article": "", "code": "", "externalCode": "", "barcodes": []}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="писать в МС (по умолчанию — отчёт)")
    ap.add_argument("--only", help="только эти коды МС через запятую")
    ap.add_argument("--retire", "--unlink", dest="retire", action="store_true",
                    help="режим «в архив без замены» по docs/prc_unlink_plan.csv")
    args = ap.parse_args(argv)

    rows = plan_rows(UNLINK_FILE, need="") if args.retire else plan_rows()
    if args.only:
        keep = {c.strip() for c in args.only.split(",")}
        rows = [r for r in rows if r["код"] in keep]
    tc = tc_fields.catalog()          # тот же разбор каталога, что и при заливке полей
    names = stores()

    stamp = date.today().isoformat()
    backup = BASE_DIR / "backups" / f"prc_recard_{stamp}"
    backup.mkdir(parents=True, exist_ok=True)
    done, held, miss = [], [], []

    if args.retire:
        done, held, miss = retire(rows, names, args.apply, backup)
        print(f'{"ЗАПИСЬ" if args.apply else "ПРОБА"}: архив без замены, карточек {len(rows)}')
        print(f"  в архив: {len(done)}")
        for code, why in held:
            print(f"  ДЕРЖИМ  {code}: {why}")
        for code, why in miss:
            print(f"  ПРОПУСК {code}: {why}")
        print(f"  слепки: {backup}")
        return

    for r in rows:
        old = card_of(r["код"], r["наименование МС"], r.get("старый внешний код"))
        if not old:
            miss.append((r["код"], "карточка не найдена в МС (или их несколько)")); continue
        target = tc.get(r["новый внешний код"])
        if not target:
            miss.append((r["код"], f'внешнего кода {r["новый внешний код"]} нет в каталоге ТК')); continue
        left = our_stock(old["id"], names)
        if left:
            held.append((r["код"], f"остаток на наших складах {left:g} — не архивируем")); continue

        (backup / f'{r["код"]}_{old["id"]}.json').write_text(
            json.dumps(old, ensure_ascii=False, indent=1), encoding="utf-8")
        body = new_body(old, r["новый внешний код"], target)
        if not any("code128" in b for b in body["barcodes"]):
            miss.append((r["код"], f'нет карточки с внешним кодом {r["новый внешний код"]}, '
                                   f'штрихкод code128 взять неоткуда — руками')); continue
        if not args.apply:
            done.append((r["код"], f'{old.get("externalCode")} → {r["новый внешний код"]} '
                                   f'({target["title"]}) [проба]'))
            continue
        ms_api.put(f'/entity/product/{old["id"]}', strip_body(old))
        time.sleep(PAUSE)
        try:
            made = ms_api.post("/entity/product", body)
        except Exception as exc:                      # старая уже обезличена — вернуть как было
            ms_api.put(f'/entity/product/{old["id"]}',
                       {"archived": False, "article": old.get("article", ""),
                        "code": old.get("code", ""), "externalCode": old.get("externalCode", ""),
                        "barcodes": old.get("barcodes", [])})
            miss.append((r["код"], f"создание не прошло, старая карточка возвращена: {exc}"))
            continue
        done.append((r["код"], f'{old.get("externalCode")} → {r["новый внешний код"]} '
                               f'({target["title"]}), новая {made["id"]}'))
        time.sleep(PAUSE)

    print(f'{"ЗАПИСЬ" if args.apply else "ПРОБА"}: карточек в плане {len(rows)}')
    print(f'  переведено: {len(done)}')
    for code, why in held:
        print(f'  ДЕРЖИМ  {code}: {why}')
    for code, why in miss:
        print(f'  ПРОПУСК {code}: {why}')
    print(f'  слепки старых карточек: {backup}')


if __name__ == "__main__":
    main()
