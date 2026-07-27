# поток: inv
"""invoice_bot/po_payment_watch.py — поллер «Заказ поставщику» → очередь черновиков оплаты.

Только чтение МойСклад + запись в Postgres (payment_draft_queue/po_payment_status). Никаких
обращений к банку на ЗАПИСЬ — только read-only проверка живого остатка (get_balance из
alfa_payment_draft.py) для метода «отсрочка». Дневной крон, отдельный лог.

Три метода (supplier_payment_terms.method), см. CLAUDE.md/докстринг миграции 200:
  • deferred              — пул неоплаченных заказов; при наступлении срока хотя бы одного —
                            ОДНА пачка (старые вперёд), урезанная по живому остатку на р/с.
  • prepayment_per_order  — каждый новый заказ сразу отдельным черновиком (без пачек/остатка).
  • prepayment_balance    — аванс наперёд; баланс считается на лету (Σ отправленных авансов −
                            Σ сумм заказов с момента последнего аванса), пополняем при просадке
                            ниже порога.

Запуск:
  ./venv/bin/python invoice_bot/po_payment_watch.py            # прогон по всем активным
  ./venv/bin/python invoice_bot/po_payment_watch.py --inn 7722341813   # один поставщик (тест)
"""
import os
import sys
import time
import argparse
import urllib.error
import urllib.parse
from datetime import date, datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/opt/mp-analytics")          # фолбэк (canonical checkout может быть на другой ветке)
sys.path.insert(0, os.path.dirname(_HERE))       # корень ЭТОГО чекаута/worktree — приоритет
sys.path.insert(0, _HERE)                        # invoice_bot/ (для голого `import ms`)
from ms import get, MS as MSU          # noqa: E402
from core import db                    # noqa: E402

LOOKBACK_DAYS = 45   # горизонт заказов, за которым не гоняемся (закрытые старые долги руками)


def get_r(path, tries=6):
    """get() с ретраем/backoff на 429 (rate limit МС) — как в invoice_to_po.py."""
    for a in range(tries):
        try:
            return get(path)
        except urllib.error.HTTPError as e:
            if e.code == 429 and a < tries - 1:
                time.sleep(1.5 * (a + 1)); continue
            raise


def _counterparty_id(inn):
    rows = get_r(f"/entity/counterparty?filter=inn={inn}").get("rows", [])
    return rows[0]["id"] if rows else None


def _fetch_purchase_orders(inn, since):
    """Проведённые заказы поставщику (agent=inn) с moment>=since. Постранично (limit=100)."""
    cid = _counterparty_id(inn)
    if not cid:
        print(f"[{inn}] контрагент не найден в МС — пропуск")
        return []
    href = f"{MSU}/entity/counterparty/{cid}"
    flt = urllib.parse.quote(f"agent={href};applicable=true;moment>={since:%Y-%m-%d} 00:00:00", safe="=;:")
    out, offset = [], 0
    while True:
        page = get_r(f"/entity/purchaseorder?filter={flt}&limit=100&offset={offset}")
        rows = page.get("rows", [])
        out.extend(rows)
        if len(rows) < 100:
            break
        offset += 100
    return out


def _sync_pending(inn, deferral_days):
    """Новые заказы поставщика → po_payment_status(status='pending'), если их там ещё нет.
    Возвращает список НОВЫХ po_id (антидубль по po_id PRIMARY KEY)."""
    since = date.today() - timedelta(days=LOOKBACK_DAYS)
    orders = _fetch_purchase_orders(inn, since)
    if not orders:
        return []
    existing = {r["po_id"] for r in db.query(
        "SELECT po_id FROM po_payment_status WHERE inn=%s", (inn,))}
    new_rows, new_ids = [], []
    for o in orders:
        po_id = o["id"]
        if po_id in existing:
            continue
        order_date = o.get("moment", "")[:10]
        if not order_date:
            continue
        d = datetime.strptime(order_date, "%Y-%m-%d").date()
        due = d + timedelta(days=deferral_days) if deferral_days else d
        new_rows.append({
            "po_id": po_id, "inn": inn, "order_date": d, "due_date": due,
            "amount": round(o.get("sum", 0) / 100, 2),
        })
        new_ids.append(po_id)
    if new_rows:
        db.upsert("po_payment_status", new_rows, conflict_cols=["po_id"],
                  update_cols=[])   # DO NOTHING на конфликте — не трогаем уже отслеживаемые
    return new_ids


def process_prepayment_per_order(inn, deferral_days=0):
    new_ids = _sync_pending(inn, deferral_days=0)
    if not new_ids:
        return 0
    n = 0
    for po_id in new_ids:
        row = db.query("SELECT amount FROM po_payment_status WHERE po_id=%s", (po_id,))[0]
        with db.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO payment_draft_queue(inn, kind, amount, covers_po_ids, status)
                    VALUES (%s, 'prepayment_order', %s, %s, 'planned') RETURNING id""",
                    (inn, row["amount"], [po_id]))
                draft_id = cur.fetchone()[0]
                cur.execute("""UPDATE po_payment_status SET status='queued', draft_id=%s WHERE po_id=%s""",
                            (draft_id, po_id))
        n += 1
    print(f"[{inn}] prepayment_per_order: {n} новых черновиков")
    return n


def process_deferred(inn, deferral_days):
    _sync_pending(inn, deferral_days)
    pending = db.query("""SELECT po_id, amount FROM po_payment_status
        WHERE inn=%s AND status='pending' ORDER BY due_date, order_date""", (inn,))
    if not pending:
        return 0
    due_now = db.query("""SELECT count(*) n FROM po_payment_status
        WHERE inn=%s AND status='pending' AND due_date<=%s""", (inn, date.today()))[0]["n"]
    if not due_now:
        return 0   # ни по одному заказу срок ещё не наступил — ждём
    try:
        from alfa_payment_draft import get_balance
        balance = get_balance()
    except Exception as e:
        print(f"[{inn}] отсрочка: наступил срок по {due_now} заказ(ам), НО не смог узнать остаток "
              f"на р/с ({e}) — пропускаю прогон (безопасно: ничего не пачкуем без остатка).")
        return 0
    picked, total = [], 0.0
    for r in pending:
        amt = float(r["amount"])
        if total + amt > balance and picked:
            break   # первый заказ включаем всегда (иначе пачка никогда не соберётся при малом остатке)
        picked.append(r["po_id"]); total += amt
    if not picked:
        return 0
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO payment_draft_queue(inn, kind, amount, covers_po_ids, status)
                VALUES (%s, 'deferred_batch', %s, %s, 'planned') RETURNING id""",
                (inn, round(total, 2), picked))
            draft_id = cur.fetchone()[0]
            cur.execute("""UPDATE po_payment_status SET status='queued', draft_id=%s
                WHERE po_id = ANY(%s)""", (draft_id, picked))
    print(f"[{inn}] отсрочка: пачка {len(picked)}/{len(pending)} заказ(ов) на {round(total,2)}₽ "
          f"(остаток на р/с {balance}₽); не влезло {len(pending)-len(picked)}")
    return len(picked)


def process_prepayment_balance(inn, advance_amount, balance_threshold):
    if not advance_amount or not balance_threshold:
        print(f"[{inn}] аванс/баланс: не задана сумма аванса или порог — пропуск")
        return 0
    already_planned = db.query("""SELECT count(*) n FROM payment_draft_queue
        WHERE inn=%s AND kind='advance' AND status='planned'""", (inn,))[0]["n"]
    if already_planned:
        return 0   # уже есть неотправленный черновик аванса — не плодим второй
    last = db.query("""SELECT amount::float amount, created_at FROM payment_draft_queue
        WHERE inn=%s AND kind='advance' AND status IN ('sent_sandbox','sent_prod')
        ORDER BY created_at DESC LIMIT 1""", (inn,))
    if not last:
        balance = 0.0
    else:
        since = last[0]["created_at"]
        orders = _fetch_purchase_orders(inn, since.date())
        consumed = sum(o.get("sum", 0) / 100 for o in orders if o.get("moment", "") >= since.strftime("%Y-%m-%d %H:%M:%S"))
        balance = round(last[0]["amount"] - consumed, 2)
    if balance >= balance_threshold:
        return 0
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO payment_draft_queue(inn, kind, amount, covers_po_ids, status, note)
                VALUES (%s, 'advance', %s, NULL, 'planned', %s)""",
                (inn, advance_amount, f"баланс {balance}₽ < порог {balance_threshold}₽"))
    print(f"[{inn}] аванс/баланс: баланс {balance}₽ < порог {balance_threshold}₽ — запланирован аванс {advance_amount}₽")
    return 1


def run(only_inn=None):
    terms = db.query("SELECT * FROM supplier_payment_terms WHERE active")
    if only_inn:
        terms = [t for t in terms if t["inn"] == only_inn]
    if not terms:
        print("нет активных поставщиков в supplier_payment_terms")
        return
    for t in terms:
        inn, method = t["inn"], t["method"]
        try:
            if method == "deferred":
                process_deferred(inn, t.get("deferral_days") or 0)
            elif method == "prepayment_per_order":
                process_prepayment_per_order(inn)
            elif method == "prepayment_balance":
                process_prepayment_balance(inn, t.get("advance_amount"), t.get("balance_threshold"))
        except Exception as e:
            print(f"[{inn}] ОШИБКА ({method}): {type(e).__name__}: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inn", help="прогнать только одного поставщика (тест)")
    a = ap.parse_args()
    run(only_inn=a.inn)


if __name__ == "__main__":
    main()
