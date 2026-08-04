# поток: inv
"""invoice_bot/sber_payment_draft.py — отправка НЕПОДПИСАННЫХ черновиков платёжек в Сбер
по очереди payment_draft_queue. Организация — ООО «ДИСКВЭР» (`ORG_INN`).

ДРАЙВЕР БАНКА, зеркало `invoice_bot/alfa_payment_draft.py`: сборка платёжки (реквизиты из МС,
счета-основания, НДС, назначение, статусы очереди) — в общем ядре `invoice_bot/payment_draft.py`;
здесь только сберовское. Выбор банка по юрлицу черновика — `invoice_bot/payment_send.py`.

`POST /fintech/api/v1/payments` БЕЗ объекта `digestSignatures` → документ создаётся в статусе
«черновик», человек подписывает его в СберБизнес (дословно из спецификации; см.
`docs/SBER_BANK_API.md` §2). Автоподписание не реализуем — запрет потока inv.

Отличия от контура Альфы:
* **Платежи живут на `v1`**, тогда как выписка — на `v2` (спецификация Сбера врёт в путях,
  маршрут подтверждён разведкой 02.08.2026).
* `urgencyCode` — `"INTERNAL"` (внутрибанковский срочный перевод у Сбера), у Альфы `"NORMAL"`.
* НДС, как и у Альфы, пишем ТЕКСТОМ в `purpose`: блок `vat` модель не требует (снято с живой
  схемы). Останется проверить, не потребует ли банк НДС на этапе подписания человеком.
* **Песочницы у Сбера НЕТ.** Любой живой POST создаёт документ в промышленном контуре, поэтому
  статус отправки всегда `sent_prod`, а гейт `SBER_PAYMENT_PROD_READY` здесь не формальность.

Двойной предохранитель (зеркало ALFA_PAYMENT_*):
  • SBER_PAYMENT_APPLY=1      — иначе dry-run (собираем payload, банк не трогаем).
  • SBER_PAYMENT_PROD_READY=1 — разрешение слать в банк вообще. ⚠️ Первое включение — только
    с явным ОК владельца: не проверено, каким статусом ложится документ без подписи
    (`docs/SBER_BANK_API.md`, вопрос 9), а песочницы для проверки нет.

Запуск:
  ./venv/bin/python invoice_bot/sber_payment_draft.py                 # dry-run всей очереди
  ./venv/bin/python invoice_bot/sber_payment_draft.py --draft 21      # только черновик 21
"""
import os
import sys
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/opt/mp-analytics")          # фолбэк (canonical checkout может быть на другой ветке)
sys.path.insert(0, os.path.dirname(_HERE))       # корень ЭТОГО чекаута/worktree — приоритет
sys.path.insert(0, _HERE)                        # invoice_bot/ (для голого `import ms`)
# ВАЖНО: collectors.* импортировать ДО `payment_draft` — ms.py при импорте сам делает
# sys.path.insert(0, "/opt/mp-analytics") и может подставить канонический чекаут вместо нашего.
from collectors import sber_auth                          # noqa: E402
from collectors import sber_statement                     # noqa: E402
import payment_draft as pd                                # noqa: E402  банконезависимое ядро

PAYMENTS_PATH = "/fintech/api/v1/payments"
NAME = "Сбер"
ORG_INN = os.getenv("SBER_ORG_INN", "7811803918")   # ООО «ДИСКВЭР»
EXTID_NS = uuid.uuid5(uuid.NAMESPACE_URL, "mp-analytics/sber/payment_draft")


def apply_enabled():
    return os.getenv("SBER_PAYMENT_APPLY") == "1"


def payer_account():
    """Наш счёт списания. `.env` (`SBER_ACCOUNT`) — приоритет; иначе единственный действующий
    счёт из `client-info` (у Дисквэра он один, 7 закрытых депозитов коллектор отсекает сам).
    Несколько действующих счетов без явного указания — ОШИБКА: угадывать, с какого платить,
    нельзя."""
    acc = os.getenv("SBER_ACCOUNT")
    if acc:
        return acc
    open_accs = [a["number"] for a in sber_statement.accounts(only_open=True) if a.get("number")]
    if len(open_accs) == 1:
        return open_accs[0]
    if not open_accs:
        raise RuntimeError("у организации нет действующих счетов в client-info")
    raise RuntimeError(f"действующих счетов {len(open_accs)} — задай SBER_ACCOUNT в .env, "
                       f"с какого счёта платим")


def _payer_block(buyer_inn):
    return pd.payer_block(
        buyer_inn, payer_account(),
        bic=os.getenv("SBER_PAYER_BANK_BIC"),
        corr=os.getenv("SBER_PAYER_BANK_CORR_ACCOUNT"))


def build_payment(draft, cfg=None):
    """cfg — для единообразия сигнатуры с драйвером Альфы, Сберу не нужен."""
    payer = _payer_block(pd.buyer_inn_for_draft(draft))
    return {
        **pd.base_payment(draft, EXTID_NS),
        "urgencyCode": "INTERNAL",
        **payer,
    }


def _check_org(draft):
    """Рубеж против кросс-банка: черновик Цифрового Квадрата не имеет права уйти через Сбер
    (и наоборот). Разрез по нашему юрлицу — несущая конструкция контура (миграция 207)."""
    org = draft.get("org_inn") or ORG_INN
    if org != ORG_INN:
        raise RuntimeError(f"черновик {draft.get('id')} принадлежит юрлицу {org}, а это контур "
                           f"{NAME} (ИНН {ORG_INN}) — отправка запрещена")


def _err_text(r):
    """Ошибка Сбера человеческим текстом: банк кладёт причину в JSON (`cause`,
    `internalErrorCode`, `message`), как в `sber_statement`."""
    try:
        b = r.json() or {}
        parts = [str(b.get(k)) for k in ("internalErrorCode", "cause", "message") if b.get(k)]
        if parts:
            return f"HTTP {r.status_code}: " + " / ".join(parts)
    except Exception:
        pass
    return f"HTTP {r.status_code}: {r.text[:300]}"


def send_one(draft, dry_run=True, cfg=None, session=None):
    """Отправить ОДИН черновик. → {ok, stage, status, external_id, error, payload}.
    Ничего не печатает: печать — дело вызывающего (CLI печатает, веб отдаёт JSON)."""
    # Гейт стоит только на РЕАЛЬНОЙ отправке: dry-run банк не трогает, а проверить сборку
    # боевого payload (реквизиты плательщика/получателя) нужно ДО первого живого документа.
    if not dry_run and os.getenv("SBER_PAYMENT_PROD_READY") != "1":
        raise RuntimeError(
            "отправка платёжек в Сбер ЗАБЛОКИРОВАНА программно: песочницы у банка нет, любой POST "
            "создаёт документ в промышленном контуре. Разблокировка — SBER_PAYMENT_PROD_READY=1 "
            "в .env, только по решению владельца. См. docs/SBER_BANK_API.md §2.")
    _check_org(draft)
    try:
        payload = build_payment(draft)
    except Exception as e:
        if not dry_run:
            # в dry-run очередь НЕ трогаем: проверка сборки не должна оставлять реальные
            # черновики в статусе 'error'
            pd.mark_error(draft["id"], str(e))
        return {"ok": False, "stage": "build", "status": None, "external_id": None,
                "error": str(e), "payload": None}
    if dry_run:
        return {"ok": True, "stage": "dry_run", "status": "dry_run",
                "external_id": payload["externalId"], "error": None, "payload": payload}

    r = sber_auth.api("POST", PAYMENTS_PATH, json=payload, timeout=60)
    if r.status_code in (200, 201):
        body = r.json() or {}
        ext_id = body.get("externalId") or body.get("id") or payload["externalId"]
        pd.mark_sent(draft["id"], "sent_prod", ext_id)
        return {"ok": True, "stage": "sent", "status": "sent_prod", "external_id": ext_id,
                "error": None, "payload": payload}
    note = _err_text(r)
    pd.mark_error(draft["id"], note, draft.get("covers_po_ids"))
    return {"ok": False, "stage": "http", "status": "error", "external_id": None,
            "error": note, "payload": payload}


def send_planned(dry_run=True, cfg=None, draft_ids=None):
    """draft_ids — отправить ТОЛЬКО эти черновики (остальные 'planned' не трогать)."""
    planned = pd.load_drafts(org_inn=ORG_INN, draft_ids=draft_ids)
    if draft_ids:
        missing = set(draft_ids) - {r["id"] for r in planned}
        if missing:
            print(f"нет в статусе 'planned': {sorted(missing)} — пропущены")
    if not planned:
        print("очередь пуста — нечего отправлять")
        return
    for draft in planned:
        res = send_one(draft, dry_run=dry_run)
        if res["stage"] == "build":
            print(f"[draft {draft['id']}] не смог собрать payload: {res['error']}")
        elif res["stage"] == "dry_run":
            p = res["payload"]
            print(f"[DRY-RUN] draft {draft['id']} ({draft['kind']}, {draft['amount']}₽) → "
                  f"{p['payeeName']} ({p['payeeAccount']}), покупатель {p['payerName']}\n"
                  f"          от {p['date']}, externalId {p['externalId']} (№ присвоит банк)\n"
                  f"          назначение: {p['purpose']}")
        elif res["ok"]:
            print(f"[draft {draft['id']}] отправлен ({res['status']}) externalId={res['external_id']}; "
                  f"черновик лежит в СберБизнес НЕПОДПИСАННЫМ, заказы остаются в пуле до факта "
                  f"оплаты в МС")
        else:
            print(f"[draft {draft['id']}] ОШИБКА: {res['error']}")


def main():
    draft_ids = None
    if "--draft" in sys.argv:
        draft_ids = [int(x) for x in sys.argv[sys.argv.index("--draft") + 1].split(",")]
    try:
        send_planned(dry_run=not apply_enabled(), draft_ids=draft_ids)
    except RuntimeError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
