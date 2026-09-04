"""collectors/launch_track.py — поток: prc. Трекер запуска новинок: МС → витрины МП.

Отвечает на один вопрос: КАК СКОРО заведённая в МойСкладе модель появляется в продаже
на площадках и где она застряла. Единица учёта — ВНЕШНИЙ КОД (4 цифры), потому что код
и есть модель: карточек МС на нём столько, сколько брендов-поставщиков (`7205bt`, `7205at`),
а карточка на витрине — одна. Поэтому «новых моделей» и «новых карточек МС» — разные числа,
и в отчёте они стоят рядом.

Дата заведения. Своей истории у нас нет, но `raw_moysklad_product.loaded_at` хранит момент
ПЕРВОГО попадания карточки в слепок (upsert не трогает столбец), а слепок снимается ежедневно
в `run_daily`. Значит дата первого появления кода = min(loaded_at) по его карточкам, с точностью
до дня. 13.06.2026 — день первой полной загрузки (44 284 карточки): всё, что пришло тогда,
заведено ДО начала наблюдений, у таких кодов даты нет и в статистику запусков они не идут.

Присутствие на витрине снимается из каталогов, которые и так собираются ежедневно:
  Ozon   — `ozon_product` (оффер есть / в архиве) + отдельный запрос списка visibility=VISIBLE:
           «в продаже» у Озона это не «не архив», а именно видимость покупателю.
  WB     — `wb_card_presence` (в кабинете / в корзине).
  Маркет — `raw_yandex_offer.payload.offer.cardStatus`: HAS_CARD* — карточка Маркета готова.
Витрин пять: у Дисквэра Маркета нет (решение Сергея 04.09.2026).

Даты появления карточек фиксируются С ПЕРВОГО ПРОГОНА и только по переходам, которые мы
видели своими глазами: если в первый же снимок карточка уже была, ставится `seeded` и дата
остаётся пустой — выдумывать «появилась сегодня» для того, что висит с июля, нельзя. Для
кодов с известной датой заведения отчёт в таком случае показывает оценку «≤ N дней».

Запуск:  ./venv/bin/python -m collectors.launch_track
"""
import sys
import time
import pathlib
import datetime

import requests

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                                    # noqa: E402

# Витрины отчёта: ключ аккаунта → как называть человеку. Порядок = порядок колонок в отчёте.
SHOWCASES = [("oz_acc1", "Ozon Цифровой"), ("oz_acc2", "Ozon Дисквэр"),
             ("wb_acc1", "WB Цифровой"), ("wb_acc2", "WB Дисквэр"),
             ("ya_acc1", "Маркет Цифровой")]

FIRST_LOAD = datetime.date(2026, 6, 13)   # день первой полной загрузки слепка МС

DDL = """
CREATE TABLE IF NOT EXISTS prc_launch (
  external_code text PRIMARY KEY,
  ms_created    date,
  ms_source     text NOT NULL,
  ms_name       text,
  supplier_key  text,
  cards         int  NOT NULL DEFAULT 0,
  archived      boolean NOT NULL DEFAULT false,
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS prc_launch_mp (
  external_code text NOT NULL,
  showcase      text NOT NULL,
  state         text NOT NULL,
  card_at       date,
  selling_at    date,
  seeded        boolean NOT NULL DEFAULT false,
  offer         text,
  note          text,
  checked_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (external_code, showcase)
);
CREATE INDEX IF NOT EXISTS prc_launch_created_idx ON prc_launch (ms_created);
"""


def ms_models():
    """Модели МС: внешний код → имя, число карточек, дата первого появления в слепке."""
    rows = db.query("""
        SELECT p.external_code ext,
               min(p.name)                     AS name,
               count(*)                        AS cards,
               bool_and(p.archived)            AS archived,
               min(r.loaded_at)::date          AS first_seen
          FROM ms_product p
          LEFT JOIN raw_moysklad_product r ON r.ms_id = p.ms_id
         WHERE p.external_code ~ '^[0-9]{4}$'
         GROUP BY 1""")
    return {r["ext"]: r for r in rows}


def novelty_meta():
    """Поставщик и дата решения со вкладки «Новинки» — чем дополняем строку кода."""
    rows = db.query("""
        SELECT left(ms_code, 4) ext, min(decided_at)::date d,
               (array_agg(supplier_key ORDER BY decided_at))[1] supplier
          FROM prc_novelty
         WHERE ms_code ~ '^[0-9]{4}' AND decided_at IS NOT NULL
         GROUP BY 1""")
    return {r["ext"]: r for r in rows}


def ozon_visible(account):
    """Офферы Ozon, видимые покупателю (visibility=VISIBLE). Пусто = запрос не удался."""
    from collectors.ozon import _headers, PRODUCT_LIST_URL
    H, offers, last = _headers(account), set(), ""
    while True:
        r = requests.post(PRODUCT_LIST_URL, headers=H,
                          json={"filter": {"visibility": "VISIBLE"}, "last_id": last, "limit": 1000},
                          timeout=120)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "5")) + 1)
            continue
        r.raise_for_status()
        res = r.json()["result"]
        items = res.get("items") or []
        offers |= {i["offer_id"] for i in items if i.get("offer_id")}
        last = res.get("last_id") or ""
        if len(items) < 1000:
            return offers
        time.sleep(0.2)


def state_ozon(account):
    """Состояние моделей на Ozon. Оффер = внешний код + суффикс набора («0621X10»)."""
    try:
        visible = ozon_visible(account)
        known = True
    except Exception as e:                     # витрина без ответа не должна ронять трекер
        print(f"  [{account}] список VISIBLE не получен ({str(e)[:60]}), статус «в продаже» пропускаю")
        visible, known = set(), False
    err = {r["offer_id"]: r for r in db.query(
        "SELECT offer_id, status_name, err_texts FROM card_status WHERE account = %s AND is_open",
        (account,))}
    out = {}
    for r in db.query("SELECT offer_id, is_archived FROM ozon_product WHERE account = %s", (account,)):
        ext, offer = r["offer_id"][:4], r["offer_id"]
        if not ext.isdigit():
            continue
        cur = out.get(ext)
        if offer in visible:
            out[ext] = {"state": "selling", "offer": offer, "note": ""}
            continue
        if cur and cur["state"] == "selling":
            continue
        bad = err.get(offer) or {}
        note = ("в архиве" if r["is_archived"] else
                (bad.get("status_name") or (bad.get("err_texts") or [""])[0]
                 or ("не в продаже: модерация или нет остатка" if known else "статус не получен")))
        out[ext] = {"state": "card", "offer": offer, "note": note[:200]}
    return out


def state_wb(account):
    """Состояние моделей на WB: карточка в кабинете и не в корзине — считаем «в продаже»."""
    out = {}
    for r in db.query("""SELECT vendor_code, nm_id, in_cabinet, in_trash
                           FROM wb_card_presence WHERE account = %s""", (account,)):
        ext = (r["vendor_code"] or "")[:4]
        if not ext.isdigit():
            continue
        if r["in_cabinet"] and not r["in_trash"]:
            out[ext] = {"state": "selling", "offer": str(r["nm_id"]), "note": ""}
        elif out.get(ext, {}).get("state") != "selling":
            out[ext] = {"state": "card" if (r["in_cabinet"] or r["in_trash"]) else "none",
                        "offer": str(r["nm_id"]),
                        "note": "в корзине" if r["in_trash"] else "нет в кабинете"}
    return {k: v for k, v in out.items() if v["state"] != "none"}


def state_ya(account):
    """Состояние моделей на Маркете: HAS_CARD* — карточка Маркета готова и торгует."""
    out = {}
    for r in db.query("""SELECT offer_id, payload->'offer'->>'cardStatus' st
                           FROM raw_yandex_offer WHERE account = %s""", (account,)):
        ext = (r["offer_id"] or "")[:4]
        if not ext.isdigit():
            continue
        sell = (r["st"] or "").startswith("HAS_CARD")
        if sell:
            out[ext] = {"state": "selling", "offer": r["offer_id"], "note": ""}
        elif out.get(ext, {}).get("state") != "selling":
            out[ext] = {"state": "card", "offer": r["offer_id"],
                        "note": {"NO_CARD_ERRORS": "карточка Маркета с ошибками",
                                 "NO_CARD_PROCESSING": "карточка Маркета на проверке"}
                                .get(r["st"] or "", r["st"] or "статус не известен")}
    return out


def main():
    db.execute(DDL)
    today = datetime.date.today()
    seed = db.query("SELECT count(*) c FROM prc_launch")[0]["c"] == 0
    models, nov = ms_models(), novelty_meta()

    recs = []
    for ext, m in models.items():
        first = m["first_seen"]
        if first and first > FIRST_LOAD:
            created, src = first, "raw"           # видели появление карточки в слепке
        elif first:
            created, src = None, "before"         # пришло первой полной загрузкой 13.06.2026
        else:
            created, src = (None, "before") if seed else (today, "tracker")
        n = nov.get(ext) or {}
        recs.append({"external_code": ext, "ms_created": created, "ms_source": src,
                     "ms_name": m["name"], "supplier_key": n.get("supplier"),
                     "cards": m["cards"], "archived": m["archived"], "updated_at": "now()"})
    for r in recs:
        r.pop("updated_at")
    db.upsert("prc_launch", recs, conflict_cols=["external_code"],
              update_cols=["ms_created", "ms_source", "ms_name", "supplier_key", "cards", "archived"])
    print(f"  моделей МС: {len(recs)}, с датой заведения: {sum(1 for r in recs if r['ms_created'])}")

    prev = {(r["external_code"], r["showcase"]): r for r in db.query(
        "SELECT external_code, showcase, state, card_at, selling_at, seeded FROM prc_launch_mp")}
    for acc, title in SHOWCASES:
        cur = (state_ozon(acc) if acc.startswith("oz") else
               state_wb(acc) if acc.startswith("wb") else state_ya(acc))
        rows, new_card, new_sell = [], 0, 0
        for ext in models:
            st = cur.get(ext) or {"state": "none", "offer": None, "note": ""}
            old = prev.get((ext, acc))
            card_at = (old or {}).get("card_at")
            sell_at = (old or {}).get("selling_at")
            seeded = bool((old or {}).get("seeded"))
            if old is None:
                # Первый снимок этой пары: переход мы не наблюдали, дату не выдумываем.
                seeded = st["state"] != "none"
            else:
                if st["state"] in ("card", "selling") and not card_at and not seeded:
                    card_at = today
                    new_card += 1
                if st["state"] == "selling" and not sell_at and not (seeded and old["state"] == "selling"):
                    sell_at = today
                    new_sell += 1
            rows.append({"external_code": ext, "showcase": acc, "state": st["state"],
                         "card_at": card_at, "selling_at": sell_at, "seeded": seeded,
                         "offer": st["offer"], "note": st["note"]})
        db.upsert("prc_launch_mp", rows, conflict_cols=["external_code", "showcase"],
                  update_cols=["state", "card_at", "selling_at", "seeded", "offer", "note"])
        live = sum(1 for r in rows if r["state"] == "selling")
        card = sum(1 for r in rows if r["state"] == "card")
        print(f"  {title}: в продаже {live}, только карточка {card}, нет {len(rows) - live - card}"
              f"{f' | новых карточек за сегодня {new_card}, вышло в продажу {new_sell}' if not seed else ' | первичный снимок'}")
    return len(recs)


if __name__ == "__main__":
    main()
