"""collectors/ozon_card_status.py — поток: card. Детектор здоровья карточек Ozon.

Снимает состояние проблемных карточек и раскладывает их на классы, которые лечатся
принципиально по-разному:

  A — «Не обновлён» (status_failed=imported, ERROR-ошибок нет, модерация approved).
      Причины в API НЕ СУЩЕСТВУЕТ: Озон отсылает в «Историю обновлений» по task_id,
      а task_id остался у ТК. Лечится повторной отправкой того же значения — этим
      занимается tools/ozon_card_push.py.
  C — контентный ERROR модерации (бренд в хештегах, чужой бренд в описании, спецсимволы).
      Повтор бесполезен, правится только на стороне ТК. Автомат такие не трогает,
      они уходят в отчёт для программиста.
  W — «На доработку» (PARTIAL_APPROVED): ERROR-ошибок нет, карточка торгует, но висит
      замечание из БЕЛОГО СПИСКА HEAL_WARN — значение в карточке ЕСТЬ, а площадка держит
      устаревший вердикт. Снимается тем же повтором. Список закрыт ОПЫТОМ 23.08.2026
      (tools/ozon_card_partial_push_test.py, 15 карточек acc1), а не догадкой:
        attribute_hierarchy_fail 3/3, pics_http_error + some_image_failed 5/5 — лечатся;
        warning_attribute_values_empty 0/5, warning_attribute_values_out_of_range 0/2 —
        НЕ лечатся: значения нет вовсе, повтор его не создаст, это работа ТК.
      Граница проходит по КОДУ замечания, а не по группе замечаний.
  O — прочее (warning-и вне белого списка и т. п.) — копим статистику, не трогаем.

Источник: POST /v3/product/list с filter.visibility=STATE_FAILED (классы A/C) и
PARTIAL_APPROVED (класс W) → POST /v3/product/info/list.
Фильтры BANNED и IMAGE_ABSENT непригодны — возвращают ВЕСЬ каталог (проверено 22.08.2026).

Идемпотентность: upsert по (platform, account, offer_id). Строка не удаляется после лечения —
закрывается флагом is_open и датой healed_at, чтобы видеть рецидивы: карточка, которую ТК
роняет раз за разом, это сигнал программисту, а не наша работа.

Запуск:  ./venv/bin/python collectors/ozon_card_status.py [oz_acc1|oz_acc2|all]
"""
import sys
from datetime import datetime, timezone

import requests

sys.path.insert(0, "/opt/mp-analytics")
from core.db import query, upsert, execute            # noqa: E402
from collectors.ozon import _headers, PRODUCT_LIST_URL, PRODUCT_INFO_URL   # noqa: E402

ACCOUNTS = ["oz_acc1", "oz_acc2"]
PLATFORM = "ozon"
PAGE = 1000
SELLING_NAMES = ("Продается", "Готов к продаже")
VISIBILITIES = ("STATE_FAILED", "PARTIAL_APPROVED")

# Коды замечаний «На доработку», ДОКАЗАННО снимаемые повторной отправкой того же значения.
# Пополнять только по факту опыта: отправили → замечание исчезло → карточка ушла из списка
# «На доработку» → продажа цела. Проверка по перечитке карточки И по счётчику площадки.
# cover_unprocessed добавлен 26.08.2026: карточка 0104 (oz_acc1) — ушёл атрибут 9024 со своим же
# значением, задача imported, при перечитке через минуту замечание исчезло, «Продается»
# и approved сохранились. До этого код лежал вне списка только потому, что в опыт 23.08 не попал
# ни одной такой карточки (все 5 картиночных подопытных были pics_http_error/some_image_failed).
HEAL_WARN = {
    "attribute_hierarchy_fail",     # «Совместимые модели принтеров» не бьётся со справочником
    "pics_http_error",              # картинка не скачалась
    "some_image_failed",            # часть картинок не обработалась
    "double_without_merger_offer",  # дубль без объединения в «похожие товары»
    "erased_attribute_value",       # значение атрибута стёрто площадкой
    "cover_unprocessed",            # видеообложка не обработалась площадкой
}


def _ids(H, visibility):
    """Все product_id в заданной витрине проблем."""
    out, last = [], ""
    while True:
        r = requests.post(PRODUCT_LIST_URL, headers=H,
                          json={"filter": {"visibility": visibility},
                                "last_id": last, "limit": PAGE}, timeout=120)
        r.raise_for_status()
        res = r.json()["result"]
        items = res.get("items") or []
        out += [i["product_id"] for i in items]
        last = res.get("last_id") or ""
        if len(items) < PAGE:
            return out


def _classify(item):
    """(класс, коды, тексты). Порядок жёсткий: C бьёт A, A бьёт W — контент важнее флага,
    сломанная карточка важнее замечания на торгующей."""
    st = item.get("statuses") or {}
    errs = item.get("errors") or []
    hard = [e for e in errs if e.get("level") == "ERROR_LEVEL_ERROR"]
    if hard:
        codes = " ".join(sorted({e.get("code", "") for e in hard if e.get("code")}))
        texts = " | ".join(sorted({(e.get("texts") or {}).get("description")
                                   or (e.get("texts") or {}).get("message") or ""
                                   for e in hard})).strip(" |")
        return "C", codes, texts[:2000]
    if st.get("status_failed") == "imported":
        return "A", "", ""
    heal = [e for e in errs if e.get("code") in HEAL_WARN]
    if heal:
        # В err_codes кладём ТОЛЬКО лечимые коды: по ним дожиматель и проверяет успех.
        codes = " ".join(sorted({e.get("code") for e in heal}))
        texts = " | ".join(sorted({(e.get("texts") or {}).get("attribute_name")
                                   or (e.get("texts") or {}).get("description") or ""
                                   for e in heal})).strip(" |")
        return "W", codes, texts[:2000]
    return "O", "", ""


def scan(account):
    H = _headers(account)
    pids, known = [], set()
    for vis in VISIBILITIES:                 # карточка может числиться в обеих витринах
        for p in _ids(H, vis):
            if p not in known:
                known.add(p)
                pids.append(p)
    rows, seen = [], set()
    for i in range(0, len(pids), PAGE):
        r = requests.post(PRODUCT_INFO_URL, headers=H,
                          json={"product_id": pids[i:i + PAGE]}, timeout=180)
        r.raise_for_status()
        for it in r.json().get("items", []):
            st = it.get("statuses") or {}
            cls, codes, texts = _classify(it)
            oid = it.get("offer_id")
            seen.add(oid)
            rows.append({
                "platform": PLATFORM, "account": account, "offer_id": oid,
                "product_id": it.get("id"), "name": (it.get("name") or "")[:500],
                "err_class": cls,
                "status": st.get("status"), "status_failed": st.get("status_failed"),
                "moderate_status": st.get("moderate_status"),
                "validation_status": st.get("validation_status"),
                "status_name": st.get("status_name"),
                "status_descr": (st.get("status_description") or "")[:1000],
                "err_codes": codes, "err_texts": texts,
                "is_selling": st.get("status_name") in SELLING_NAMES,
                "is_open": True, "healed_at": None,
                "card_updated_at": st.get("status_updated_at") or None,
                "last_seen": datetime.now(timezone.utc),
            })
    return rows, seen


def save(account, rows, seen):
    """Upsert живых проблем + закрытие тех, кого площадка больше не считает проблемной."""
    # first_seen проставляется только при вставке: у существующих строк его не трогаем,
    # иначе потеряем «сколько карточка висит» — главную цифру, которой нет у площадки.
    upsert("card_status", rows, ["platform", "account", "offer_id"],
           update_cols=["product_id", "name", "err_class", "status", "status_failed",
                        "moderate_status", "validation_status", "status_name", "status_descr",
                        "err_codes", "err_texts", "is_selling", "is_open", "healed_at",
                        "card_updated_at", "last_seen"])
    if seen:
        closed = execute(
            "UPDATE card_status SET is_open = false, healed_at = coalesce(healed_at, now()), "
            "needs_human = false, last_seen = now() "
            "WHERE platform = %s AND account = %s AND is_open AND NOT (offer_id = ANY(%s))",
            (PLATFORM, account, list(seen)))
    else:
        closed = execute(
            "UPDATE card_status SET is_open = false, healed_at = coalesce(healed_at, now()), "
            "needs_human = false, last_seen = now() "
            "WHERE platform = %s AND account = %s AND is_open", (PLATFORM, account))
    return closed


def main(argv):
    targets = ACCOUNTS if (len(argv) < 2 or argv[1] == "all") else [argv[1]]
    for acc in targets:
        rows, seen = scan(acc)
        closed = save(acc, rows, seen)
        by = {}
        for r in rows:
            by[r["err_class"]] = by.get(r["err_class"], 0) + 1
        sell_a = sum(1 for r in rows if r["err_class"] == "A" and r["is_selling"])
        print(f"{acc}: проблемных {len(rows)} | A {by.get('A', 0)} (торгуют {sell_a}) | "
              f"W {by.get('W', 0)} | C {by.get('C', 0)} | O {by.get('O', 0)} | "
              f"закрыто с прошлого раза {closed}")
    for r in query(
            "SELECT err_class, count(*) AS n FROM card_status WHERE is_open "
            "AND err_class IN ('A', 'W') AND first_seen < now() - interval '7 days' "
            "GROUP BY err_class ORDER BY err_class"):
        print(f"ВНИМАНИЕ: {r['n']} карточек класса {r['err_class']} висят дольше недели — "
              f"дожим не работает")


if __name__ == "__main__":
    main(sys.argv)
