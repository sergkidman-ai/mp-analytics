# поток: inv
"""invoice_bot/alfa_payment_draft.py — отправка НЕПОДПИСАННЫХ черновиков платёжек в Альфа-Банк
по очереди payment_draft_queue (см. миграцию 200_supplier_payment_terms.sql).

POST /api/jp/v2/payments БЕЗ digestSignatures → черновик, виден в Альфа-Бизнес веб
(«Платежи в работе» → «На подпись»), подписывает человек. Реквизиты получателя — из карточки
контрагента МС (invoice_bot/supplier_requisites.py уже наполняет default-счёт); плательщик —
организация-покупатель из МС (name/inn/kpp) + наш р/с/БИК/корсчёт из .env.

Двойной предохранитель (зеркало ALFA_MS_APPLY для выписки, см. collectors/alfa_ms.py):
  • ALFA_PAYMENT_APPLY=1     — иначе dry-run (только печатает, что бы отправил).
  • ALFA_PAYMENT_PROD_READY=1 — прод ЖЁСТКО заблокирован программно, пока не выставлен
    вручную (прод-скоуп `signature` банком пока НЕ выдан — см. docs/HANDOFF.md).

get_balance() — read-only GET /pp/v1/accounts (scope `customer profile inn role eio`, тоже
пока не выдан на проме) — используется поллером (po_payment_watch.py) для урезания пачки
«отсрочки» по живому остатку. В песочнице должен отвечать тестовым балансом.

Запуск:
  ALFA_ENV=sandbox ALFA_PAYMENT_APPLY=1 ./venv/bin/python invoice_bot/alfa_payment_draft.py
"""
import os
import sys
from datetime import date

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/opt/mp-analytics")          # фолбэк (canonical checkout может быть на другой ветке)
sys.path.insert(0, os.path.dirname(_HERE))       # корень ЭТОГО чекаута/worktree — приоритет
sys.path.insert(0, _HERE)                        # invoice_bot/ (для голого `import ms`)
# ВАЖНО: collectors.* импортировать ДО `ms` — ms.py сам делает sys.path.insert(0, "/opt/mp-analytics")
# при импорте (побочный эффект), что отодвигает наш worktree и может подставить canonical-чекаут
# (он бывает на другой ветке без этого файла) вместо текущего.
from collectors.alfa_statement import _cfg, _session      # noqa: E402
from ms import get                                       # noqa: E402
from core import db                                      # noqa: E402

ACCOUNTS_PATH = "/pp/v1/accounts"
PAYMENTS_PATH = "/jp/v2/payments"
DEFAULT_BUYER_INN = "7807355364"   # ООО «ЦИФРОВОЙ КВАДРАТ» — используется для advance (нет привязки к PO)


def get_balance(cfg=None, session=None):
    """Живой остаток на нашем р/с (₽). Бросает исключение, если банк не ответил/скоуп не выдан —
    вызывающий код (поллер) обязан отловить и НЕ пачковать без остатка (fail-closed)."""
    cfg = cfg or _cfg()
    s = session or _session(cfg)
    r = s.get(f"{cfg['base']}{ACCOUNTS_PATH}", timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]!r}")
    accounts = r.json().get("accounts") or []
    acc_number = os.getenv("ALFA_ACCOUNT_PROD" if cfg["env"] != "sandbox" else "ALFA_ACCOUNT")
    match = next((a for a in accounts if a.get("number") == acc_number), accounts[0] if accounts else None)
    if not match:
        raise RuntimeError("GET /pp/v1/accounts вернул пустой список счетов")
    bal = (match.get("balance") or {}).get("amount")
    if bal is None:
        raise RuntimeError(f"нет balance.amount в ответе для счёта {match.get('number')}")
    return float(bal)


def _org_cache():
    return {o["inn"]: o for o in get("/entity/organization")["rows"] if o.get("inn")}


def _payer_block(buyer_inn, cfg):
    orgs = _org_cache()
    org = orgs.get(buyer_inn)
    if not org:
        raise RuntimeError(f"организация с ИНН {buyer_inn} не найдена в МС")
    prod = cfg["env"] != "sandbox"
    account = os.getenv("ALFA_ACCOUNT_PROD" if prod else "ALFA_ACCOUNT")
    bic = os.getenv("ALFA_PAYER_BANK_BIC_PROD" if prod else "ALFA_PAYER_BANK_BIC")
    corr = os.getenv("ALFA_PAYER_BANK_CORR_ACCOUNT_PROD" if prod else "ALFA_PAYER_BANK_CORR_ACCOUNT")
    missing = [n for n, v in [("ALFA_ACCOUNT", account), ("ALFA_PAYER_BANK_BIC", bic),
                              ("ALFA_PAYER_BANK_CORR_ACCOUNT", corr)] if not v]
    if missing:
        raise RuntimeError("нет в .env (реквизиты плательщика): " + ", ".join(missing))
    return {
        "payerName": org.get("legalTitle") or org.get("name"),
        "payerInn": org.get("inn"), "payerKpp": org.get("kpp"),
        "payerAccount": account, "payerBankBic": bic, "payerBankCorrAccount": corr,
    }


def _payee_block(inn):
    rows = get(f"/entity/counterparty?filter=inn={inn}").get("rows", [])
    if not rows:
        raise RuntimeError(f"контрагент с ИНН {inn} не найден в МС")
    cp = rows[0]
    accs = get(f"/entity/counterparty/{cp['id']}/accounts").get("rows", [])
    acc = next((a for a in accs if a.get("isDefault")), accs[0] if accs else None)
    if not acc:
        raise RuntimeError(f"у контрагента {cp.get('name')} (ИНН {inn}) нет банковских реквизитов в МС "
                           f"— прогони invoice_bot/supplier_requisites.py --apply")
    return {
        "payeeName": cp.get("name"), "payeeInn": inn, "payeeKpp": cp.get("kpp"),
        "payeeAccount": acc.get("accountNumber"), "payeeBankBic": acc.get("bic"),
        "payeeBankCorrAccount": acc.get("correspondentAccount"),
    }


def _buyer_inn_for_draft(draft):
    """ИНН организации-покупателя: по первому po_id пачки (все заказы одного поставщика —
    как правило один и тот же покупатель); advance — нет PO, берём дефолтного покупателя."""
    po_ids = draft.get("covers_po_ids") or []
    if not po_ids:
        return DEFAULT_BUYER_INN
    po = get(f"/entity/purchaseorder/{po_ids[0]}?expand=organization")
    return (po.get("organization") or {}).get("inn") or DEFAULT_BUYER_INN


def build_payment(draft, cfg):
    payer = _payer_block(_buyer_inn_for_draft(draft), cfg)
    payee = _payee_block(draft["inn"])
    today = date.today().isoformat()
    number = f"D{draft['id']}"
    kind_note = {"deferred_batch": "оплата по графику (отсрочка)",
                 "prepayment_order": "предоплата по заказу поставщику",
                 "advance": "аванс поставщику"}.get(draft["kind"], draft["kind"])
    return {
        "number": number, "date": today, "amount": float(draft["amount"]),
        "urgencyCode": "NORMAL", "deliveryKind": "электронно",
        "paymentPurpose": f"{kind_note}, заказ(ы) {draft.get('covers_po_ids') or '—'}",
        **payer, **payee,
    }


def send_planned(dry_run=True, cfg=None):
    cfg = cfg or _cfg()
    prod = cfg["env"] != "sandbox"
    if prod and os.getenv("ALFA_PAYMENT_PROD_READY") != "1":
        sys.exit("ALFA_ENV=prod для платежей ЗАБЛОКИРОВАН программно: банк ещё не выдал прод-скоуп "
                 "`signature`. Разблокировка вручную ТОЛЬКО после подтверждения банком — "
                 "выставить ALFA_PAYMENT_PROD_READY=1 в .env. См. docs/HANDOFF.md.")
    session = _session(cfg)
    planned = db.query("SELECT * FROM payment_draft_queue WHERE status='planned' ORDER BY created_at")
    if not planned:
        print("очередь пуста — нечего отправлять")
        return
    for draft in planned:
        try:
            payload = build_payment(draft, cfg)
        except Exception as e:
            print(f"[draft {draft['id']}] не смог собрать payload: {e}")
            db.execute("UPDATE payment_draft_queue SET status='error', note=%s WHERE id=%s",
                      (str(e)[:500], draft["id"]))
            continue
        if dry_run:
            print(f"[DRY-RUN] draft {draft['id']} ({draft['kind']}, {draft['amount']}₽) → "
                  f"{payload['payeeName']} ({payload['payeeAccount']}), покупатель {payload['payerName']}")
            continue
        r = session.post(f"{cfg['base']}{PAYMENTS_PATH}", json=payload, timeout=60)
        status = "sent_prod" if prod else "sent_sandbox"
        if r.status_code in (200, 201):
            ext_id = (r.json() or {}).get("externalId") or (r.json() or {}).get("id")
            # ЗАКАЗЫ НЕ ПОМЕЧАЕМ 'paid' ПРИ ОТПРАВКЕ. Отправлен НЕПОДПИСАННЫЙ черновик — деньги
            # уйдут только когда человек подпишет его в вебе банка, а может и не подписать.
            # Факт оплаты приходит из МС (payedSum) — его ловит гейт в po_payment_watch._sync_pending
            # и сам переводит заказ в 'paid'. Раньше здесь стоял 'paid' на отправке: заказ пропадал
            # из пула, даже если черновик так и остался неподписанным (тихая потеря долга), а в
            # песочнице тестовый прогон портил реальный пул.
            db.execute("UPDATE payment_draft_queue SET status=%s, alfa_external_id=%s WHERE id=%s",
                       (status, ext_id, draft["id"]))
            print(f"[draft {draft['id']}] отправлен ({status}) externalId={ext_id}"
                  f"{' — ПЕСОЧНИЦА, реальных денег нет' if not prod else ''}; "
                  f"заказы остаются в пуле до факта оплаты в МС")
        else:
            note = f"HTTP {r.status_code}: {r.text[:300]}"
            with db.get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE payment_draft_queue SET status='error', note=%s WHERE id=%s",
                               (note, draft["id"]))
                    po_ids = draft.get("covers_po_ids") or []
                    if po_ids:
                        cur.execute("UPDATE po_payment_status SET status='pending', draft_id=NULL WHERE po_id = ANY(%s)",
                                   (po_ids,))
            print(f"[draft {draft['id']}] ОШИБКА: {note}")


def main():
    apply_on = os.getenv("ALFA_PAYMENT_APPLY") == "1"
    send_planned(dry_run=not apply_on)


if __name__ == "__main__":
    main()
