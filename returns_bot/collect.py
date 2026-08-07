# поток: ret
"""Сбор возвратов со всех площадок → raw_mp_returns / mp_returns / mp_return_items.

    ./venv/bin/python -m returns_bot.collect --dry-run     # только посчитать, в БД не писать
    ./venv/bin/python -m returns_bot.collect               # собрать и записать
    ./venv/bin/python -m returns_bot.collect --only ozon   # одна площадка

Идемпотентно: upsert по (platform, account, return_id), `first_seen` не перезаписывается.
Возврат, пропавший из выдачи API, помечается `gone_at` и уходит из сводки.
"""
import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone

from core import db
from returns_bot import pending

HEAD_COLS = [
    "platform", "source", "account", "return_id", "campaign", "order_number", "return_type",
    "scheme", "status_raw", "status_name", "stage", "pvz_id", "pvz_name", "pvz_address",
    "pvz_instruction", "where_now", "barcode", "track_number", "created_at", "arrived_at",
    "deadline_at", "storage_days", "storage_sum", "amount",
]
KEY = ["platform", "account", "return_id"]


def _sources(only=None):
    # У Ozon три источника, и это не дубли: /v1/returns/list не отдаёт ни rFBS-возвраты,
    # ни коробки вывоза со склада FBO (проверено 06.08.2026).
    from returns_bot.sources import ozon, ozon_removal, ozon_rfbs, yandex
    src = {"ozon": ozon.collect, "ozon_rfbs": ozon_rfbs.collect,
           "ozon_removal": ozon_removal.collect, "yandex": yandex.collect}
    try:
        from returns_bot.sources import wb
        src["wb"] = wb.collect
    except ImportError:
        pass          # WB подключается после выдачи токена со скоупом «Возвраты»
    if only:
        return {k: v for k, v in src.items() if k in only}
    return src


def gather(only=None, quick=False):
    """quick — сбор «на сейчас»: у источников с окнами берём только свежее окно.
    Тогда старые строки в выдачу не попадают, поэтому `store(mark_gone=False)` обязателен."""
    rows, errors = [], []
    for name, fn in _sources(only).items():
        t0 = time.monotonic()
        try:
            got = fn(quick=quick)
        except Exception as e:                      # одна площадка легла — остальные собираем
            errors.append(f"{name}: {type(e).__name__} {str(e)[:150]}")
            continue
        print(f"  {name}: {len(got)} строк за {time.monotonic() - t0:.0f} с")
        for head, _items, _raw in got:
            head.setdefault("source", name)         # чем собрано — по этому считаем «пропал»
        rows += got
    return rows, errors


def pickup_keys():
    """Что прямо сейчас лежит и ждёт забора — снимок ДО сбора, чтобы поймать, что забрали."""
    return {(r["platform"], r["account"], r["return_id"]) for r in db.query(
        "SELECT platform, account, return_id FROM mp_returns "
        " WHERE gone_at IS NULL AND stage = 'pickup'")}


def received_since(snapshot):
    """Из тех, что лежали в снимке, площадка теперь показывает «получено нами».

    Считаем по статусу, а не по исчезновению из выдачи: в быстром сборе окно узкое,
    и пропажа строки означает лишь то, что она не попала в окно.
    """
    if not snapshot:
        return []
    out = []
    for r in db.query("SELECT platform, COALESCE(source, platform) AS source, account, "
                      "       return_id, status_raw, stage FROM mp_returns "
                      " WHERE stage <> 'pickup' AND gone_at IS NULL"):
        key = (r["platform"], r["account"], r["return_id"])
        if key in snapshot and pending.is_received(r["source"], r["status_raw"]):
            out.append(key)
    return out


def store(rows, mark_gone=True):
    """Запись в БД. Возвращает счётчики."""
    now = datetime.now(timezone.utc)
    raw_rows, head_rows, item_rows = [], [], []
    seen = {}                                # (platform, source, account) -> set(return_id)
    for head, items, raw in rows:
        raw_rows.append({
            "platform": head["platform"], "account": head["account"],
            "return_id": head["return_id"],
            "payload": json.dumps(raw, ensure_ascii=False), "loaded_at": now,
        })
        # gone_at сбрасываем: возврат снова в выдаче — значит снова живой
        head_rows.append({c: head.get(c) for c in HEAD_COLS} | {"last_seen": now, "gone_at": None})
        item_rows += items
        seen.setdefault((head["platform"], head.get("source") or head["platform"],
                         head["account"]), set()).add(head["return_id"])

    db.upsert("raw_mp_returns", raw_rows, KEY, update_cols=["payload", "loaded_at"])
    # first_seen сознательно НЕ в update_cols — по нему считаем, сколько дней возврат висит
    db.upsert("mp_returns", head_rows, KEY,
              update_cols=[c for c in HEAD_COLS if c not in KEY] + ["last_seen", "gone_at"])
    if item_rows:
        db.upsert("mp_return_items", item_rows, KEY + ["seq"])

    # То, чего больше нет в выдаче площадки, — закрыто (забрали/отменили). Считаем В ПРЕДЕЛАХ
    # ОДНОГО ИСТОЧНИКА: у Ozon их три (returns/list, rFBS, вывоз со склада), и упавший или
    # незапущенный (`--only`) источник иначе погасил бы живые строки соседнего.
    # В быстром сборе (узкое окно) этого делать НЕЛЬЗЯ: строка вне окна не «пропала», её просто
    # не спрашивали. Гасим только на полном прогоне — ночном или ручном `collect`.
    gone = 0
    for (platform, source, account), ids in (seen.items() if mark_gone else ()):
        gone += db.execute(
            "UPDATE mp_returns SET gone_at = now() "
            "WHERE platform = %s AND COALESCE(source, platform) = %s AND account = %s "
            "  AND gone_at IS NULL AND NOT (return_id = ANY(%s))",
            (platform, source, account, list(ids)))
    return {"raw": len(raw_rows), "heads": len(head_rows), "items": len(item_rows), "gone": gone}


def summary(rows):
    # в разрезе источника, а не площадки: у Ozon их три и путать их в отчёте прогона нельзя
    c = Counter((h.get("source") or h["platform"], h["account"], h["stage"])
                for h, _, _ in rows)
    out = []
    for stage in pending.STAGE_ORDER:
        tot = sum(n for (p, a, s), n in c.items() if s == stage)
        if not tot:
            continue
        by = ", ".join(f"{p}/{a}={n}" for (p, a, s), n in sorted(c.items()) if s == stage)
        out.append(f"{pending.STAGE_TITLE[stage]:12s} {tot:5d}  ({by})")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="сбор возвратов площадок")
    ap.add_argument("--dry-run", action="store_true", help="ничего не писать в БД")
    ap.add_argument("--only", nargs="*",
                    help="ozon | ozon_rfbs | ozon_removal | yandex | wb")
    ap.add_argument("--quick", action="store_true",
                    help="быстро: только свежее окно, без пометки «пропал из выдачи»")
    a = ap.parse_args(argv)

    rows, errors = gather(a.only, quick=a.quick)
    print(f"получено возвратов: {len(rows)}")
    for line in summary(rows):
        print(" ", line)
    for e in errors:
        print("  ОШИБКА", e)

    if a.dry_run:
        print("dry-run: в БД не писали")
    elif rows:
        st = store(rows, mark_gone=not a.quick)
        print(f"записано: raw={st['raw']} шапок={st['heads']} позиций={st['items']} "
              f"закрыто(пропали из API)={st['gone']}")
    return 1 if errors and not rows else 0


if __name__ == "__main__":
    sys.exit(main())
