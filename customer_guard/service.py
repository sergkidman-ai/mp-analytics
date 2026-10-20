# поток: fin
"""Explicit one-shot commands; importing this module never starts a worker."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import time

import psycopg2
from core import db
from core.ozon_expense_basis import shipment_rule, BasisUnavailable
from shipment_guard.ms import SourceError
from shipment_guard.worker import LOCK_ID
from customer_guard import store
from customer_guard.domain import ACTIVE, READY, composition, economics, sale, utcnow
from customer_guard.sources import CustomerMS, get_posting

MESSAGES = {
    "no_confirmed_cost": "Отгрузка ещё не создана; подтверждённой стоимости нет",
    "partial_demand_coverage": "Отгрузки покрывают не весь состав заказа; полный итог не рассчитан",
    "incomplete_fifo": "FIFO отсутствует или покрывает не весь состав",
    "supplier_receipt_not_unique": "Не найдено единственное оприходование выбранного товара",
    "supplier_currency_unverified": "Валюта оприходования не подтверждена",
    "supplier_overhead_unverified": "Требуется проверка накладных расходов оприходования",
}


@contextmanager
def exclusive():
    conn = psycopg2.connect(db.DATABASE_URL, connect_timeout=5)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_ID,))
            if not cur.fetchone()[0]:
                raise RuntimeError("shipment_or_customer_worker_busy")
        def pulse():
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        yield pulse
    finally:
        conn.close()


def sync(days=2):
    if not 1 <= days <= 7:
        raise ValueError("pilot_window_1_to_7_days")
    from collectors.ozon_postings import fetch_postings
    now = utcnow()
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    with exclusive() as pulse:
        pulse()
        try:
            posts = fetch_postings("oz_acc2", "fbs", start, now,
                                   limit=20, max_pages=10, timeout=120)
        except Exception as exc:
            db.execute("""INSERT INTO customer_guard_sync_state(scope,started_at,error)
              VALUES('ozon/oz_acc2/fbs',%s,%s) ON CONFLICT(scope)
              DO UPDATE SET started_at=excluded.started_at,error=excluded.error""",
              (now, type(exc).__name__))
            raise
        pulse()
        store.ingest(posts, now, utcnow(), start, now)
    return dict(received=len(posts), account="oz_acc2")


def refresh(limit=5, older_than=None):
    """Point checks cover late changes outside the new-order creation window."""
    if not 1 <= limit <= 10:
        raise ValueError("refresh_limit_1_to_10")
    with exclusive() as pulse:
        rows = db.query("""SELECT posting_number FROM customer_guard_orders
          WHERE mp_status=ANY(%s) AND observed_at<%s ORDER BY observed_at,id LIMIT %s""",
          (list(ACTIVE), older_than or utcnow(), limit))
        for index, row in enumerate(rows):
            pulse()
            if index:
                time.sleep(2)
            post = get_posting(row["posting_number"])
            now = utcnow()
            with store.transaction() as cur:
                store.observe(cur, post, now, now, "fbs/get/v3")
    return dict(refreshed=len(rows))


def evaluate(post, document, client, rule, now):
    price, units = sale(post)
    result = dict(price=str(price), units=str(units), cost=None, floor=None, gap=None,
                  cost_kind=None, signal="unknown", note="", positions=[])
    if document is None:
        result.update(signal="missing_ms", note="Рабочее отправление не найдено в МС")
        return result
    result["ms_price"] = str(Decimal(str(document["sum"])) / 100) if isinstance(document.get("sum"), (int, float)) else None
    state = document.get("state", {}).get("name")
    if state not in READY and not document.get("demands"):
        result.update(signal="draft" if state == "Ожидает подтверждения" else "unknown",
                      note="Заказ есть в МС; ожидание подходящего статуса")
        return result
    positions = client.positions("customerorder", document["id"])
    result["positions"] = [dict(name=p.get("assortment", {}).get("name"),
        code=p.get("assortment", {}).get("code"), external_code=p.get("assortment", {}).get("externalCode"),
        quantity=p.get("quantity")) for p in positions]
    try:
        cost, kind, sources = client.cost(document, positions)
        from shipment_guard.bundles import Catalogue
        catalogue = Catalogue(None)
        proof = composition(post, positions, lambda code: catalogue.resolve(code, fetch=False))
        result["composition"] = proof
        result.update(economics(post, cost, kind, rule, now,
                               allocation_verified=proof["verified"] and len(post["products"]) == 1))
        result["cost_sources"] = sources
    except ValueError as exc:
        code = str(exc)
        result.update(signal="unknown", reason=code, note=MESSAGES.get(code, "Недостаточно данных для расчёта"))
    return result


def work(limit=3, budget=60, clock=utcnow, client=None, get=get_posting, posting_number=None,
         notify=False):
    if not 1 <= limit <= 10 or not 1 <= budget <= 100:
        raise ValueError("pilot_run_budget")
    completed = 0
    notification_errors = []
    with exclusive() as pulse:
        if not db.query("""SELECT 1 FROM customer_guard_jobs WHERE
          (state IN ('pending','retry') AND due_at<=%s) OR
          (state='running' AND lease_until<%s) LIMIT 1""", (clock(),clock())):
            return dict(completed=0, ms_requests=0)
        client = client or CustomerMS(budget=budget)
        client.pulse = pulse
        client.verify_identity()
        for _ in range(limit):
            job = store.claim(clock(), posting_number)
            if not job:
                break
            try:
                pulse()
                post = get(job["order"]["posting_number"])
                if not store.refresh(job, post, clock()):
                    continue
                client.evidence = []
                document = client.find_order(post["posting_number"])
                try:
                    rule = shipment_rule("oz_acc2", as_of=clock().date())
                except BasisUnavailable:
                    rule = None
                result = evaluate(post, document, client, rule, clock())
                if document:
                    latest = client.get(f"/entity/customerorder/{document['id']}")
                    if latest.get("updated") != document.get("updated"):
                        raise SourceError("ms_order_changed_during_check", 600)
                if notify and (result["signal"]=="missing_ms" or
                    result["signal"]=="below_floor" and post["status"]!="delivering"
                    and (document or {}).get("state",{}).get("name") in READY):
                    # Reconfirm price/composition/status after cost reads, before an external alert.
                    if not store.refresh(job,get(post["posting_number"]),clock()):
                        continue
                if store.finish(job, result, clock(), document, client.evidence):
                    completed += 1
                    if notify:
                        from customer_guard.notifications import send_current
                        try:
                            send_current(job["order_id"])
                        except Exception as exc:
                            notification_errors.append(type(exc).__name__)
            except SourceError as exc:
                store.fail(job, str(exc), clock(), exc.retry_after)
                # Avoid a burst of failures on shared quota/permissions/budget.
                break
            except Exception as exc:
                store.fail(job, "source_or_calculation_error:" + type(exc).__name__, clock())
                break  # Exception text can contain credentials from request URLs.
    result=dict(completed=completed, ms_requests=client.requests)
    if notification_errors:
        result["notification_errors"]=notification_errors
    return result


def bootstrap(days=90):
    """Bounded initial active-order coverage, separate from the fresh-order checkpoint."""
    if days != 90:
        raise ValueError("pilot_backfill_90_days")
    from collectors.ozon_postings import fetch_postings
    now = utcnow()
    start = now-timedelta(days=days)
    with exclusive():
        posts = fetch_postings("oz_acc2","fbs",start,now,limit=20,max_pages=20,
                               timeout=120,statuses=sorted(ACTIVE))
        store.ingest(posts,now,utcnow(),start,now,scope="ozon/oz_acc2/backfill")
    return dict(received=len(posts),coverage_days=days)


def reprice(refresh_composition=False):
    """New local calculations; never present stored MS evidence as newly fetched."""
    now = utcnow()
    rule = shipment_rule("oz_acc2", as_of=now.date())
    resolver = None
    if refresh_composition:
        from shipment_guard.bundles import Catalogue
        catalogue = Catalogue(None)
        resolver = lambda code: catalogue.resolve(code, fetch=False)
    with exclusive():
        return dict(recalculated=store.reprice(rule, now, resolver), ms_requests=0,
                    rule_version=rule["version"])


def main():
    parser = argparse.ArgumentParser(description="Ozon Дисквэр: однократный запуск, без расписания")
    subs = parser.add_subparsers(dest="command", required=True)
    sync_parser = subs.add_parser("sync")
    sync_parser.add_argument("--days", type=int, default=2)
    worker = subs.add_parser("work")
    worker.add_argument("--limit", type=int, default=3)
    worker.add_argument("--budget", type=int, default=60)
    worker.add_argument("--posting", help="Проверить конкретное уже наступившее задание")
    refresher = subs.add_parser("refresh")
    refresher.add_argument("--limit", type=int, default=5)
    repricer = subs.add_parser("reprice", help="Пересчитать сохранённую стоимость по новой базе, без запросов МС")
    repricer.add_argument("--composition", action="store_true", help="Также сверить сохранённый состав по локальному ТК")
    args = parser.parse_args()
    try:
        if args.command == "sync":
            result = sync(args.days)
        elif args.command == "refresh":
            result = refresh(args.limit)
        elif args.command == "reprice":
            result = reprice(args.composition)
        else:
            result = work(args.limit, args.budget, posting_number=args.posting)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__, "message": "Запуск не завершён; секреты скрыты"}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
