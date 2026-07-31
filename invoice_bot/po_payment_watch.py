# поток: inv
"""invoice_bot/po_payment_watch.py — поллер «Заказ поставщику» → очередь черновиков оплаты.

Факт оплаты берём из МС (`payedSum` заказа) — счета платят и вручную, поэтому в пул попадают
только недоплаченные заказы, на сумму остатка; закрывшиеся снимаются в 'paid' (см. _sync_pending).

Работаем ТОЛЬКО по организации «Цифровой квадрат» (BUYER_INN): счета на Дисквэр в этот контур
не входят — его р/с в Сбере, Альфа-интеграция его не видит.

Только чтение МойСклад + запись в Postgres (payment_draft_queue/po_payment_status). К банку не
обращается вообще. Дневной крон, отдельный лог.

Три метода (supplier_payment_terms.method), см. CLAUDE.md/докстринг миграции 200:
  • deferred              — платим за ПРИНЯТЫЙ товар: заказ без проведённой приёмки в пачку не
                            идёт (status='waiting_receipt', гейт `require_receipt`); далее пул
                            неоплаченных заказов; при наступлении срока хотя бы одного —
                            ОДНА пачка (старые вперёд), урезанная по лимиту платежа из
                            договорённости (payment_cap, миграция 201; NULL = вся сумма
                            задолженности). Гейта по живому остатку на р/с НЕТ: банк не даёт
                            ЮЛ метод остатка (см. alfa_payment_draft.get_balance).
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

# Платим ТОЛЬКО за «Цифровой квадрат» (решение Сергея 2026-07-27): счета на Дисквэр в этот
# контур не входят — у Дисквэра счёт в Сбере, интеграция с Альфой его в принципе не видит.
# Заказы второй организации отсекаются на стороне МС-фильтра, а не постфактум.
BUYER_INN = os.getenv("ALFA_BUYER_INN", "7807355364")   # ООО «ЦИФРОВОЙ КВАДРАТ»
_ORG_ID = None


def _org_id():
    """id организации-плательщика в МС (кэш на процесс)."""
    global _ORG_ID
    if _ORG_ID is None:
        rows = [o for o in get_r("/entity/organization").get("rows", []) if o.get("inn") == BUYER_INN]
        if not rows:
            raise RuntimeError(f"организация с ИНН {BUYER_INN} не найдена в МС")
        _ORG_ID = rows[0]["id"]
    return _ORG_ID


def get_r(path, tries=6):
    """get() с ретраем/backoff на 429 (rate limit МС) — как в invoice_to_po.py."""
    for a in range(tries):
        try:
            return get(path)
        except urllib.error.HTTPError as e:
            if e.code == 429 and a < tries - 1:
                time.sleep(1.5 * (a + 1)); continue
            raise


def _counterparty_id(inn, agent_id=None):
    """id карточки контрагента. ИНН в МС НЕ уникален (у Солюшнс принт две карточки, заказы
    только в «МСК»), поэтому явный ms_agent_id из supplier_payment_terms имеет приоритет."""
    if agent_id:
        return agent_id
    rows = get_r(f"/entity/counterparty?filter=inn={inn}").get("rows", [])
    if len(rows) > 1:
        print(f"[{inn}] ВНИМАНИЕ: карточек в МС несколько ({len(rows)}), ms_agent_id не задан — "
              f"беру «{rows[0]['name']}». Проставьте карточку в условиях оплаты.")
    return rows[0]["id"] if rows else None


def _fetch_purchase_orders(inn, since, agent_id=None):
    """Проведённые заказы поставщику (agent=inn) ОТ НАШЕЙ организации (BUYER_INN), moment>=since.
    Постранично (limit=100)."""
    cid = _counterparty_id(inn, agent_id)
    if not cid:
        print(f"[{inn}] контрагент не найден в МС — пропуск")
        return []
    href = f"{MSU}/entity/counterparty/{cid}"
    org = f"{MSU}/entity/organization/{_org_id()}"
    flt = urllib.parse.quote(
        f"agent={href};organization={org};applicable=true;moment>={since:%Y-%m-%d} 00:00:00", safe="=;:")
    out, offset = [], 0
    while True:
        page = get_r(f"/entity/purchaseorder?filter={flt}&limit=100&offset={offset}")
        rows = page.get("rows", [])
        out.extend(rows)
        if len(rows) < 100:
            break
        offset += 100
    return out


def _sync_pending(inn, deferral_days, agent_id=None, require_receipt=False):
    """Новые заказы поставщика → po_payment_status(status='pending'), если их там ещё нет.
    Возвращает список НОВЫХ po_id (антидубль по po_id PRIMARY KEY).

    Источник правды об оплате — `payedSum` заказа в МойСкладе (счета платят и вручную, и до
    внедрения этого модуля). Поэтому:
      • в пул берём только НЕДОплаченные заказы, сумма = остаток (sum − payedSum);
      • уже отслеживаемые заказы, которые в МС закрылись оплатой, снимаем в status='paid'.
    Без этого гейта первый прогон планирует повторную оплату всего исторического хвоста
    (проверено на Феррете: 54 из 56 заказов за 45 дней уже оплачены на 428 505 ₽).

    `require_receipt` — ГЕЙТ ПРИЁМКИ (решение Сергея 2026-07-30, только для отсрочки): по отсрочке
    платим за ПРИНЯТЫЙ товар, поэтому база платежа — проведённая приёмка, а не сам заказ.
    Мера приёмки — `shippedSum` заказа: он считает ТОЛЬКО ПРОВЕДЁННЫЕ приёмки (проверено живьём —
    у заказов с приёмкой-черновиком `shippedSum = 0`), поэтому отдельный запрос за приёмками не
    нужен. `applicable=true` в фильтре заказов — это проведённость ЗАКАЗА, товар по нему может
    быть ещё не принят (на 30.07 таких в пуле было 3 шт).
      • товар не принят  → заказ заводится/висит в status='waiting_receipt' и в пачку НЕ идёт;
      • принят частично  → к оплате идёт принятая часть (min(sum, shippedSum) − payedSum);
      • приёмку провели  → на следующем прогоне 'waiting_receipt' → 'pending' с суммой по приёмке.
    Для предоплаты (prepayment_*) гейт ВЫКЛЮЧЕН: там платёж по определению идёт ДО приёмки."""
    since = date.today() - timedelta(days=LOOKBACK_DAYS)
    orders = _fetch_purchase_orders(inn, since, agent_id)
    if not orders:
        return []
    tracked = {r["po_id"]: r for r in db.query(
        "SELECT po_id, status, amount::float amount FROM po_payment_status WHERE inn=%s", (inn,))}
    new_rows, new_ids, closed, held, released, repriced, stale = [], [], [], [], [], [], []
    for o in orders:
        po_id = o["id"]
        total = round(o.get("sum", 0) / 100, 2)
        payed = round(o.get("payedSum", 0) / 100, 2)
        received = round(o.get("shippedSum", 0) / 100, 2)
        # база платежа: по отсрочке — принятое, по предоплате — весь заказ
        base = min(total, received) if require_receipt else total
        due_amount = round(base - payed, 2)
        if po_id in tracked:
            st, was = tracked[po_id]["status"], tracked[po_id]["amount"]
            if round(total - payed, 2) <= 0 and st != "paid":
                closed.append(po_id)     # оплатили (в т.ч. вручную) — снимаем из пула
            elif require_receipt and st == "pending" and due_amount <= 0:
                held.append(po_id)       # приёмку ещё не провели (или отменили) — придержать
            elif require_receipt and st == "waiting_receipt" and due_amount > 0:
                released.append((po_id, due_amount))   # приёмку провели — в пул на сумму принятого
            elif st == "pending" and abs(was - due_amount) >= 0.01:
                # ЧАСТИЧНАЯ оплата пришла ПОСЛЕ того, как заказ попал в пул: сумма в пуле
                # застыла на момент заведения и завышена на уже оплаченное. Пересчитываем по
                # живому остатку (иначе переплата — поймано вручную 30.07 на KV00009784: пул
                # держал 45840.11 при уже привязанных 5049.96).
                repriced.append((po_id, due_amount))
            elif st == "queued" and abs(was - due_amount) >= 0.01:
                stale.append(po_id)      # черновик уже собран на старую сумму — предупредить
            continue
        if round(total - payed, 2) <= 0:
            continue                     # оплачен ещё до того, как попал к нам в отслеживание
        order_date = o.get("moment", "")[:10]
        if not order_date:
            continue
        d = datetime.strptime(order_date, "%Y-%m-%d").date()
        due = d + timedelta(days=deferral_days) if deferral_days else d
        waiting = due_amount <= 0        # только при require_receipt: товар ещё не принят
        new_rows.append({
            "po_id": po_id, "inn": inn, "order_date": d, "due_date": due,
            "amount": due_amount if not waiting else round(total - payed, 2),
            "status": "waiting_receipt" if waiting else "pending",
        })
        if not waiting:
            new_ids.append(po_id)
    if held:
        db.execute("UPDATE po_payment_status SET status='waiting_receipt' WHERE po_id = ANY(%s)",
                   (held,))
        print(f"[{inn}] придержано до приёмки: {len(held)}")
    for po_id, amt in released:
        db.execute("UPDATE po_payment_status SET status='pending', amount=%s WHERE po_id=%s",
                   (amt, po_id))
    if released:
        print(f"[{inn}] приёмка проведена, в пул: {len(released)}")
    for po_id, amt in repriced:
        db.execute("UPDATE po_payment_status SET amount=%s WHERE po_id=%s", (amt, po_id))
    if repriced:
        print(f"[{inn}] сумма пересчитана по живому остатку: {len(repriced)}")
    if stale:
        # черновик на эти заказы уже собран — сам он не пересчитается, помечаем для человека
        db.execute("""UPDATE payment_draft_queue SET note = coalesce(note,'') ||
                      ' | ВНИМАНИЕ: остаток по части заказов изменился в МС — сверить сумму перед подписью'
                      WHERE status='planned' AND id IN (SELECT draft_id FROM po_payment_status
                      WHERE po_id = ANY(%s) AND draft_id IS NOT NULL)""", (stale,))
        print(f"[{inn}] у {len(stale)} заказ(ов) в уже собранных черновиках изменился остаток — помечено")
    if new_rows:
        n_wait = sum(1 for r in new_rows if r["status"] == "waiting_receipt")
        if n_wait:
            print(f"[{inn}] новых заказов без проведённой приёмки: {n_wait} (ждут, в пачку не идут)")
    if closed:
        # у 'queued' черновик уже создан — помечаем его, чтобы человек не подписал повторную оплату
        db.execute("""UPDATE payment_draft_queue SET note = coalesce(note,'') ||
                      ' | ВНИМАНИЕ: часть заказов оплачена вне системы — проверить перед подписью'
                      WHERE status='planned' AND id IN (SELECT draft_id FROM po_payment_status
                      WHERE po_id = ANY(%s) AND status='queued' AND draft_id IS NOT NULL)""", (closed,))
        db.execute("UPDATE po_payment_status SET status='paid' WHERE po_id = ANY(%s)", (closed,))
        print(f"[{inn}] снято как оплаченные в МС: {len(closed)}")
    if new_rows:
        db.upsert("po_payment_status", new_rows, conflict_cols=["po_id"],
                  update_cols=[])   # DO NOTHING на конфликте — не трогаем уже отслеживаемые
    return new_ids


def process_prepayment_per_order(inn, agent_id=None):
    new_ids = _sync_pending(inn, deferral_days=0, agent_id=agent_id)
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


def process_deferred(inn, deferral_days, payment_cap=None, agent_id=None):
    # require_receipt=True: по отсрочке платим только за принятый товар (см. _sync_pending).
    # Заказы в status='waiting_receipt' в выборку 'pending' ниже не попадают by design.
    _sync_pending(inn, deferral_days, agent_id, require_receipt=True)
    pending = db.query("""SELECT po_id, amount FROM po_payment_status
        WHERE inn=%s AND status='pending' ORDER BY due_date, order_date""", (inn,))
    if not pending:
        return 0
    due_now = db.query("""SELECT count(*) n FROM po_payment_status
        WHERE inn=%s AND status='pending' AND due_date<=%s""", (inn, date.today()))[0]["n"]
    if not due_now:
        return 0   # ни по одному заказу срок ещё не наступил — ждём
    # Ограничитель ОДИН — договорённость с поставщиком (payment_cap). Гейт по живому остатку на
    # р/с убран (решение Сергея 2026-07-29): банк не даёт ЮЛ метод остатка (`accounts` — только
    # для физлиц), а fail-closed на его отсутствии глушил метод «отсрочка» целиком.
    # ВАЖНО: пачка больше НЕ ограничена живыми деньгами. Защита осталась в том, что черновик
    # НЕПОДПИСАННЫЙ — человек видит сумму в вебе банка и не подпишет то, чего нет на счету.
    if not payment_cap:
        limit, binding = float("inf"), "не урезано (лимит платежа не задан)"
    else:
        limit, binding = float(payment_cap), "лимит платежа"
    picked, total = [], 0.0
    for r in pending:
        amt = float(r["amount"])
        if total + amt > limit and picked:
            break   # первый заказ включаем всегда (иначе пачка никогда не соберётся при малом лимите)
        picked.append(r["po_id"]); total += amt
    if not picked:
        return 0
    over = total > limit   # сработало правило «первый заказ всегда»: один заказ дороже лимита
    if len(picked) == len(pending) and not over:
        binding = "не урезано (весь долг влез)"
    note = (f"кап: {binding}" + (f" {limit:.0f}₽" if limit != float("inf") else "") +
            (f"; ПРЕВЫШЕН на {total-limit:.0f}₽ — один заказ дороже лимита" if over else ""))
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO payment_draft_queue(inn, kind, amount, covers_po_ids, status, note)
                VALUES (%s, 'deferred_batch', %s, %s, 'planned', %s) RETURNING id""",
                (inn, round(total, 2), picked, note))
            draft_id = cur.fetchone()[0]
            cur.execute("""UPDATE po_payment_status SET status='queued', draft_id=%s
                WHERE po_id = ANY(%s)""", (draft_id, picked))
    cap_s = f"{float(payment_cap):.0f}₽" if payment_cap else "вся сумма"
    print(f"[{inn}] отсрочка: пачка {len(picked)}/{len(pending)} заказ(ов) на {round(total,2)}₽ "
          f"(лимит платежа {cap_s}, режет: {binding}"
          f"{'; ПРЕВЫШЕНИЕ — один заказ дороже лимита' if over else ''}); "
          f"не влезло {len(pending)-len(picked)}")
    return len(picked)


def process_prepayment_balance(inn, advance_amount, balance_threshold, agent_id=None):
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
        orders = _fetch_purchase_orders(inn, since.date(), agent_id)
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
        agent_id = t.get("ms_agent_id")
        try:
            if method == "deferred":
                process_deferred(inn, t.get("deferral_days") or 0,
                                 payment_cap=t.get("payment_cap"), agent_id=agent_id)
            elif method == "prepayment_per_order":
                process_prepayment_per_order(inn, agent_id=agent_id)
            elif method == "prepayment_balance":
                process_prepayment_balance(inn, t.get("advance_amount"),
                                           t.get("balance_threshold"), agent_id=agent_id)
        except Exception as e:
            print(f"[{inn}] ОШИБКА ({method}): {type(e).__name__}: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inn", help="прогнать только одного поставщика (тест)")
    a = ap.parse_args()
    run(only_inn=a.inn)


if __name__ == "__main__":
    main()
