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
from collections import Counter
from datetime import datetime, timezone

from core import db
from returns_bot import pending

HEAD_COLS = [
    "platform", "account", "return_id", "campaign", "order_number", "return_type", "scheme",
    "status_raw", "status_name", "stage", "pvz_id", "pvz_name", "pvz_address",
    "pvz_instruction", "where_now", "barcode", "created_at", "arrived_at", "deadline_at",
    "storage_days", "storage_sum", "amount",
]
KEY = ["platform", "account", "return_id"]


def _sources(only=None):
    from returns_bot.sources import ozon, yandex
    src = {"ozon": ozon.collect, "yandex": yandex.collect}
    try:
        from returns_bot.sources import wb
        src["wb"] = wb.collect
    except ImportError:
        pass          # WB подключается после выдачи токена со скоупом «Возвраты»
    if only:
        return {k: v for k, v in src.items() if k in only}
    return src


def gather(only=None):
    rows, errors = [], []
    for name, fn in _sources(only).items():
        try:
            rows += fn()
        except Exception as e:                      # одна площадка легла — остальные собираем
            errors.append(f"{name}: {type(e).__name__} {str(e)[:150]}")
    return rows, errors


def store(rows):
    """Запись в БД. Возвращает счётчики."""
    now = datetime.now(timezone.utc)
    raw_rows, head_rows, item_rows = [], [], []
    seen = {}                                        # (platform, account) -> set(return_id)
    for head, items, raw in rows:
        raw_rows.append({
            "platform": head["platform"], "account": head["account"],
            "return_id": head["return_id"],
            "payload": json.dumps(raw, ensure_ascii=False), "loaded_at": now,
        })
        # gone_at сбрасываем: возврат снова в выдаче — значит снова живой
        head_rows.append({c: head.get(c) for c in HEAD_COLS} | {"last_seen": now, "gone_at": None})
        item_rows += items
        seen.setdefault((head["platform"], head["account"]), set()).add(head["return_id"])

    db.upsert("raw_mp_returns", raw_rows, KEY, update_cols=["payload", "loaded_at"])
    # first_seen сознательно НЕ в update_cols — по нему считаем, сколько дней возврат висит
    db.upsert("mp_returns", head_rows, KEY,
              update_cols=[c for c in HEAD_COLS if c not in KEY] + ["last_seen", "gone_at"])
    if item_rows:
        db.upsert("mp_return_items", item_rows, KEY + ["seq"])

    # то, чего больше нет в выдаче площадки — закрыто (забрали/отменили)
    gone = 0
    for (platform, account), ids in seen.items():
        gone += db.execute(
            "UPDATE mp_returns SET gone_at = now() "
            "WHERE platform = %s AND account = %s AND gone_at IS NULL "
            "  AND NOT (return_id = ANY(%s))",
            (platform, account, list(ids)))
    return {"raw": len(raw_rows), "heads": len(head_rows), "items": len(item_rows), "gone": gone}


def summary(rows):
    c = Counter((h["platform"], h["account"], h["stage"]) for h, _, _ in rows)
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
    ap.add_argument("--only", nargs="*", help="ozon | yandex | wb")
    a = ap.parse_args(argv)

    rows, errors = gather(a.only)
    print(f"получено возвратов: {len(rows)}")
    for line in summary(rows):
        print(" ", line)
    for e in errors:
        print("  ОШИБКА", e)

    if a.dry_run:
        print("dry-run: в БД не писали")
    elif rows:
        st = store(rows)
        print(f"записано: raw={st['raw']} шапок={st['heads']} позиций={st['items']} "
              f"закрыто(пропали из API)={st['gone']}")
    return 1 if errors and not rows else 0


if __name__ == "__main__":
    sys.exit(main())
