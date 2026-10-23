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
from core.ozon_expense_basis import BasisUnavailable
from customer_guard.rules import get_rule as shipment_rule
from shipment_guard.ms import SourceError
from shipment_guard.worker import LOCK_ID
from customer_guard import store
from customer_guard.domain import ACTIVE, READY, composition, economics, sale, utcnow, non_positive_profit, market, ACCOUNTS
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


def sync(days=2, account="oz_acc2"):
    if ACCOUNTS.get(account)!="ozon": raise ValueError("unsupported_guard_account")
    scope=f"ozon/{account}/fbs"
    if not 1 <= days <= 7:
        raise ValueError("pilot_window_1_to_7_days")
    from collectors.ozon_postings import fetch_postings
    now = utcnow()
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    with exclusive() as pulse:
        pulse()
        try:
            posts = fetch_postings(account, "fbs", start, now,
                                   limit=20, max_pages=30, timeout=120)
        except Exception as exc:
            db.execute("""INSERT INTO customer_guard_sync_state(scope,started_at,error)
              VALUES(%s,%s,%s) ON CONFLICT(scope)
              DO UPDATE SET started_at=excluded.started_at,error=excluded.error""",
              (scope, now, type(exc).__name__))
            raise
        pulse()
        store.ingest([dict(p,account=account,platform="ozon") for p in posts], now, utcnow(), start, now, scope=scope)
    return dict(received=len(posts), account=account)


def refresh(limit=5, older_than=None, account="oz_acc2"):
    """Point checks cover late changes outside the new-order creation window."""
    if not 1 <= limit <= 10:
        raise ValueError("refresh_limit_1_to_10")
    with exclusive() as pulse:
        rows = db.query("""SELECT posting_number FROM customer_guard_orders
          WHERE account=%s AND mp_status=ANY(%s) AND observed_at<%s ORDER BY observed_at,id LIMIT %s""",
          (account, list(ACTIVE), older_than or utcnow(), limit))
        for index, row in enumerate(rows):
            pulse()
            if index:
                time.sleep(2)
            post = get_posting(row["posting_number"], account=account)
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
        wb = market(post)[0] in ('wb','yandex')
        if wb and (result.get('ms_price') is None or Decimal(result['ms_price'])<=0):
            raise ValueError('invalid_ms_price')
        result.update(economics(post, cost, kind, rule, now,
                               allocation_verified=proof["verified"] and len(post["products"]) == 1,
                               price_override=result.get('ms_price') if wb else None))
        result['price_source']='ms' if wb else 'marketplace'
        result["cost_sources"] = sources
    except ValueError as exc:
        code = str(exc)
        result.update(signal="unknown", reason=code, note=MESSAGES.get(code, "Недостаточно данных для расчёта"))
    return result


def work(limit=5, budget=100, clock=utcnow, client=None, get=get_posting, posting_number=None,
         notify=False, account=None, accounts=None):
    if not 1 <= limit <= 10 or not 1 <= budget <= 100:
        raise ValueError("pilot_run_budget")
    completed = 0
    notification_errors = []
    with exclusive() as pulse:
        if not db.query("""SELECT 1 FROM customer_guard_jobs j JOIN customer_guard_orders o ON o.id=j.order_id
          WHERE ((j.state IN ('pending','retry') AND j.due_at<=%s) OR
          (j.state='running' AND j.lease_until<%s)) AND (%s IS NULL OR o.account=%s) AND (%s IS NULL OR o.account=ANY(%s))
          LIMIT 1""", (clock(),clock(),account,account,accounts,accounts)):
            return dict(completed=0, ms_requests=0)
        client = client or CustomerMS(budget=budget)
        client.pulse = pulse
        client.verify_identity()
        enabled = list(dict.fromkeys(accounts or []))
        fair_order = None
        if posting_number is None and account is None and len(enabled) > 1:
            start = int(clock().timestamp() // 60) % len(enabled)
            fair_order = enabled[start:] + enabled[:start]
        for _ in range(limit):
            if fair_order is None:
                job = store.claim(clock(), posting_number, account, accounts)
            else:
                job = None
                for candidate in fair_order:
                    job = store.claim(clock(), account=candidate, accounts=[candidate])
                    if job:
                        position = fair_order.index(candidate)
                        fair_order = fair_order[position+1:] + fair_order[:position+1]
                        break
            if not job:
                break
            try:
                pulse()
                job_account=job['order']['account']
                client.account=job_account
                def read_post():
                    if ACCOUNTS[job_account]=='wb':
                        from customer_guard.wb import WBClient
                        return WBClient(account=job_account).refresh_posts([job['order']['mp_payload']])[0]
                    if job_account=='ya_acc1':
                        from customer_guard.yandex import YandexClient
                        return YandexClient().get_post(job['order']['mp_payload'])
                    return get(job['order']['posting_number']) if job_account=='oz_acc2' else get_posting(job['order']['posting_number'],account=job_account)
                post = read_post()
                if not store.refresh(job, post, clock()):
                    continue
                client.evidence = []
                document = client.find_order(post["posting_number"])
                try:
                    rule = shipment_rule(job_account, as_of=clock().date())
                except BasisUnavailable:
                    rule = None
                result = evaluate(post, document, client, rule, clock())
                if document:
                    latest = client.get(f"/entity/customerorder/{document['id']}")
                    if latest.get("updated") != document.get("updated"):
                        raise SourceError("ms_order_changed_during_check", 600)
                if notify and (result["signal"]=="missing_ms" or
                    non_positive_profit(result) and post["status"]!="delivering"
                    and (document or {}).get("state",{}).get("name") in READY):
                    # Reconfirm price/composition/status after cost reads, before an external alert.
                    if not store.refresh(job,read_post(),clock()):
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


def bootstrap(days=90, account="oz_acc2"):
    """Bounded initial active-order coverage, separate from the fresh-order checkpoint."""
    if days != 90:
        raise ValueError("pilot_backfill_90_days")
    from collectors.ozon_postings import fetch_postings
    now = utcnow()
    start = now-timedelta(days=days)
    with exclusive():
        posts = fetch_postings(account,"fbs",start,now,limit=20,max_pages=100,
                               timeout=120,statuses=sorted(ACTIVE))
        store.ingest([dict(p,account=account,platform="ozon") for p in posts],now,utcnow(),start,now,scope=f"ozon/{account}/backfill")
    return dict(received=len(posts),coverage_days=days)


def reprice(refresh_composition=False, account='oz_acc2'):
    """New local calculations; never present stored MS evidence as newly fetched."""
    now = utcnow()
    rule = shipment_rule(account, as_of=now.date())
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
    worker.add_argument("--limit", type=int, default=5)
    worker.add_argument("--budget", type=int, default=100)
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




def sync_wb(days=2, bootstrap=False, account="wb_acc2"):
    from customer_guard.wb import WBClient
    if not 1<=days<=30:
        raise ValueError('wb_window_1_to_30_days')
    now=utcnow();start=now-timedelta(days=days)
    scope=f'wb/{account}/backfill' if bootstrap else f'wb/{account}/fbs'
    with exclusive():
        try:
            posts=WBClient(account=account).fetch(start,now,max_pages=100 if bootstrap else 20)
            if bootstrap:
                posts=[p for p in posts if p['status'] not in ('cancelled','delivered')]
            store.ingest(posts,now,utcnow(),start,now,scope=scope)
        except Exception as exc:
            db.execute('''INSERT INTO customer_guard_sync_state(scope,started_at,error)
                VALUES(%s,%s,%s) ON CONFLICT(scope) DO UPDATE SET
                started_at=excluded.started_at,error=excluded.error''',(scope,now,type(exc).__name__))
            raise
    return dict(received=len(posts),account=account,coverage_days=days)


def refresh_wb(older_than=None, account="wb_acc2"):
    from customer_guard.wb import WBClient
    with exclusive():
        rows=db.query('''SELECT mp_payload FROM customer_guard_orders WHERE account=%s
            AND mp_status NOT IN ('cancelled','delivered') AND observed_at<%s
            ORDER BY observed_at,id LIMIT 1001''',(account,older_than or utcnow(),))
        if len(rows)>1000:
            raise SourceError('wb_refresh_order_budget',600)
        posts=WBClient(account=account).refresh_posts([r['mp_payload'] for r in rows])
        now=utcnow()
        with store.transaction() as cur:
            for post in posts:
                store.observe(cur,post,now,now,'fbs/status/v3')
    return dict(refreshed=len(posts))


def sync_yandex(days=2, bootstrap=False):
    from customer_guard.yandex import YandexClient
    if not 1<=days<=29: raise ValueError('yandex_window_1_to_29_days')
    now=utcnow();start=now-timedelta(days=days)
    scope='yandex/ya_acc1/backfill' if bootstrap else 'yandex/ya_acc1/fbs'
    with exclusive():
        try:
            posts=YandexClient().fetch(start,now,active_only=bootstrap)
            store.ingest(posts,now,utcnow(),start,now,scope=scope)
        except Exception as exc:
            db.execute("""INSERT INTO customer_guard_sync_state(scope,started_at,error)
              VALUES(%s,%s,%s) ON CONFLICT(scope) DO UPDATE SET
              started_at=excluded.started_at,error=excluded.error""",(scope,now,type(exc).__name__))
            raise
    return dict(received=len(posts),account='ya_acc1',coverage_days=days)


def refresh_yandex(limit=3, older_than=None):
    from customer_guard.yandex import YandexClient
    if not 1<=limit<=10: raise ValueError('refresh_limit_1_to_10')
    with exclusive():
        rows=db.query("""SELECT mp_payload FROM customer_guard_orders WHERE account='ya_acc1'
          AND mp_status NOT IN ('delivered','cancelled') AND observed_at<%s
          ORDER BY observed_at,id LIMIT %s""",(older_than or utcnow(),limit))
        client=YandexClient()
        for row in rows:
            post=client.get_post(row['mp_payload']);now=utcnow()
            with store.transaction() as cur: store.observe(cur,post,now,now,'business/orders/v1')
    return dict(refreshed=len(rows))


if __name__ == "__main__":
    main()
