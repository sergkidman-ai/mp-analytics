# поток: prc
# -*- coding: utf-8 -*-
"""Аудит оприходований: в документе поставщика должен лежать только ЕГО товар.

Владельца проверяем по ГРУППЕ юрлиц (`prices/supplier_group.py`), а не по одному контрагенту:
у каждого поставщика за годы сменилось несколько ООО/ИП, и карточки висят на старых именах
той же группы. Чужая карточка в оприходовании — это чужой остаток на нашем складе и, дальше,
чужая себестоимость, поэтому проверка отдельная от загрузчика.

    ./venv/bin/python -m tools.prc.enter_audit            # все профили
    ./venv/bin/python -m tools.prc.enter_audit kaktus_msk # один

Итог — сводка в чат и файл `reports/data/prc_enter_audit.txt` со списком чужих позиций.
"""
import sys
from pathlib import Path

from core import ms_api
from prices.profiles import PROFILES, STORE_REMOTE, get_profile
from prices.supplier_group import group_of, name_of, own_ids

OUT = Path(__file__).resolve().parents[2] / "reports" / "data" / "prc_enter_audit.txt"


def docs_of(key):
    """Оприходования группы на «Удаленном складе» — только они наши."""
    rows = ms_api.get("/entity/enter", {
        "limit": 1000,
        "filter": [f"name~={key}_", f"store={ms_api.BASE}/entity/store/{STORE_REMOTE}"],
    }).get("rows", [])
    return [d for d in rows if ms_api.meta_id(d, "store") == STORE_REMOTE]


def positions_of(doc):
    """Позиции документа отдельным запросом: `expand=positions` в списке отдаёт только meta."""
    return ms_api.get(f"/entity/enter/{doc['id']}/positions", {"limit": 1000}).get("rows", [])


def suppliers_of(ids, batch=40):
    """{id карточки -> id контрагента}. Батчами: повтор поля в filter МС трактует как ИЛИ."""
    out = {}
    ids = list(ids)
    for i in range(0, len(ids), batch):
        rows = ms_api.get("/entity/assortment", {
            "limit": 1000, "filter": [f"id={x}" for x in ids[i:i + batch]],
        }).get("rows", [])
        for row in rows:
            out[row["id"]] = ms_api.meta_id(row, "supplier")
    return out


def audit(key, log):
    profile = get_profile(key)
    own = own_ids(profile)
    positions = []
    for doc in docs_of(key):
        for pos in positions_of(doc):
            href = pos.get("assortment", {}).get("meta", {}).get("href", "")
            positions.append((doc["name"], href.rsplit("/", 1)[-1].split("?")[0]))
    sup = suppliers_of({p[1] for p in positions})
    foreign = [(doc, pid, sup.get(pid)) for doc, pid in positions
               if sup.get(pid) not in own]
    log(f"{key}: группа «{group_of(profile) or '—'}», позиций {len(positions)}, "
        f"чужих {len(foreign)}")
    for doc, pid, sid in foreign:
        log(f"    {doc} {pid} — «{name_of(sid) if sid else 'поставщик не указан'}»")
    return len(positions), len(foreign)


def main(argv=None):
    keys = (argv or sys.argv[1:]) or list(PROFILES)
    lines = []
    total = bad = 0
    for key in keys:
        try:
            n, f = audit(key, lines.append)
        except Exception as exc:                       # профиль без документов/без группы — не стоп
            lines.append(f"{key}: ошибка — {exc}")
            continue
        total, bad = total + n, bad + f
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(l for l in lines if not l.startswith("    ")))
    print(f"ИТОГО позиций {total}, чужих {bad} → {OUT}")


if __name__ == "__main__":
    main()
