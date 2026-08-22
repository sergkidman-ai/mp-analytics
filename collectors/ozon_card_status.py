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
  O — прочее (только warning-и и т. п.) — копим статистику, не трогаем.

Источник: POST /v3/product/list с filter.visibility=STATE_FAILED → POST /v3/product/info/list.
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


def _failed_ids(H):
    """Все product_id, которые площадка считает проблемными."""
    out, last = [], ""
    while True:
        r = requests.post(PRODUCT_LIST_URL, headers=H,
                          json={"filter": {"visibility": "STATE_FAILED"},
                                "last_id": last, "limit": PAGE}, timeout=120)
        r.raise_for_status()
        res = r.json()["result"]
        items = res.get("items") or []
        out += [i["product_id"] for i in items]
        last = res.get("last_id") or ""
        if len(items) < PAGE:
            return out


def _classify(item):
    """(класс, коды ERROR, тексты ERROR). Класс C бьёт класс A: контент важнее флага."""
    st = item.get("statuses") or {}
    hard = [e for e in (item.get("errors") or []) if e.get("level") == "ERROR_LEVEL_ERROR"]
    if hard:
        codes = " ".join(sorted({e.get("code", "") for e in hard if e.get("code")}))
        texts = " | ".join(sorted({(e.get("texts") or {}).get("description")
                                   or (e.get("texts") or {}).get("message") or ""
                                   for e in hard})).strip(" |")
        return "C", codes, texts[:2000]
    if st.get("status_failed") == "imported":
        return "A", "", ""
    return "O", "", ""


def scan(account):
    H = _headers(account)
    pids = _failed_ids(H)
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
              f"C {by.get('C', 0)} | O {by.get('O', 0)} | закрыто с прошлого раза {closed}")
    old = query(
        "SELECT count(*) AS n FROM card_status WHERE is_open AND err_class = 'A' "
        "AND first_seen < now() - interval '7 days'")[0]["n"]
    if old:
        print(f"ВНИМАНИЕ: {old} карточек класса A висят в ошибке дольше недели — дожим не работает")


if __name__ == "__main__":
    main(sys.argv)
