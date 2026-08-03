# поток: inv
"""invoice_bot/payment_send.py — отправка ОДНОГО черновика платёжки в банк ЕГО юрлица.

Диспетчер для ручной кнопки в дашборде («Условия оплаты поставщиков» → «Очередь черновиков»):
черновик читается по id, банк выбирается по `payment_draft_queue.org_inn`, дальше работает
драйвер (`alfa_payment_draft` / `sber_payment_draft`) поверх общего ядра `payment_draft`.

Почему разрез по юрлицу важен: поставщики у двух наших фирм ОБЩИЕ, суммы совпадают запросто,
а счета списания и приёмки — свои у каждой (миграция 207). Кросс-банк закрыт ТРЕМЯ рубежами:
эндпоинт сверяет `org_inn` из запроса с БД, здесь банк выбирается по `org_inn` из БД, и сам
драйвер перед HTTP ещё раз проверяет, что юрлицо черновика — его (`_check_org`, fail-closed).

Отправляется НЕПОДПИСАННЫЙ черновик: подписывает человек в вебе банка. Автоподписание —
запрет потока inv.

Запуск (то же, что кнопка, но из консоли):
    ./venv/bin/python invoice_bot/payment_send.py 21          # dry-run
    ./venv/bin/python invoice_bot/payment_send.py 21 --apply  # реальная отправка
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/opt/mp-analytics")
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
import payment_draft as pd                      # noqa: E402
from core import db                             # noqa: E402

# Реестр банков по НАШЕМУ юрлицу-плательщику. Драйверы импортируются лениво: у каждого свой
# транспорт со своими секретами (mTLS-сертификаты, токены), и падение одного контура не должно
# мешать отправить платёж в другом.
BANKS = {
    "7807355364": ("alfa_payment_draft", "Альфа-Банк / ООО «ЦИФРОВОЙ КВАДРАТ»"),
    "7811803918": ("sber_payment_draft", "Сбер / ООО «ДИСКВЭР»"),
}

_LOCK_NS = 0x70646671        # произвольное пространство имён advisory-локов очереди платёжек


def driver_for(org_inn):
    entry = BANKS.get(org_inn)
    if not entry:
        raise RuntimeError(f"нет банковского драйвера для юрлица с ИНН {org_inn}")
    module, _title = entry
    return __import__(module)


def send_draft(draft_id, dry_run=None, actor="cli"):
    """Отправить черновик `draft_id` в банк его юрлица.

    → {ok, status, external_id, error, org_inn, bank, amount, payload}
      status: 'sent_prod' | 'sent_sandbox' | 'dry_run' | None (ошибка до отправки)

    `dry_run=None` → берётся из гейта банка (`*_PAYMENT_APPLY`), то есть по умолчанию кнопка
    БЕЗОПАСНА: пока гейт не выставлен, она только собирает payload и ничего не пишет.

    Двойной клик закрыт advisory-локом на время HTTP: второй запрос получит «уже отправляется»,
    а не вторую платёжку. Второй рубеж — детерминированный `externalId` (uuid5 от id черновика):
    даже если повтор прорвётся, банк увидит тот же документ."""
    rows = pd.load_drafts(draft_ids=[draft_id], only_planned=False)
    if not rows:
        return {"ok": False, "error": f"черновик {draft_id} не найден", "status": None,
                "external_id": None, "org_inn": None, "bank": None}
    draft = rows[0]
    base = {"org_inn": draft.get("org_inn"), "amount": float(draft["amount"]),
            "bank": (BANKS.get(draft.get("org_inn") or "") or (None, None))[1]}
    if draft["status"] != "planned":
        return {"ok": False, "error": f"черновик {draft_id} в статусе '{draft['status']}', "
                                      f"отправить можно только 'planned'",
                "status": draft["status"], "external_id": None, **base}
    try:
        bank = driver_for(draft.get("org_inn"))
    except RuntimeError as e:
        return {"ok": False, "error": str(e), "status": None, "external_id": None, **base}

    if dry_run is None:
        dry_run = not bank.apply_enabled()

    with db.get_conn() as conn:      # лок живёт, пока открыто ЭТО соединение
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s, %s)", (_LOCK_NS, int(draft_id)))
            if not cur.fetchone()[0]:
                return {"ok": False, "error": "черновик уже отправляется — дождись результата",
                        "status": None, "external_id": None, **base}
        try:
            res = bank.send_one(draft, dry_run=dry_run)
        except Exception as e:       # гейт банка, транспорт, протухший токен
            res = {"ok": False, "stage": "gate", "status": None, "external_id": None,
                   "error": f"{type(e).__name__}: {e}", "payload": None}
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s, %s)", (_LOCK_NS, int(draft_id)))

    print(f"[payment_send/{actor}] draft {draft_id} ({base['bank']}, {base['amount']}₽) → "
          f"{res.get('status') or 'ошибка'}: {res.get('error') or res.get('external_id')}")
    return {**base, "ok": res["ok"], "status": res["status"],
            "external_id": res["external_id"], "error": res["error"],
            "payload": res.get("payload")}


def main(argv):
    ids = [a for a in argv if not a.startswith("--")]
    if not ids:
        sys.exit("usage: payment_send.py <draft_id> [--apply]")
    dry = None if "--apply" not in argv else False
    res = send_draft(int(ids[0]), dry_run=dry, actor="cli")
    if res.get("payload") and res.get("status") == "dry_run":
        p = res["payload"]
        print(f"  получатель: {p['payeeName']} ({p['payeeAccount']}, БИК {p['payeeBankBic']})\n"
              f"  плательщик: {p['payerName']} ({p['payerAccount']}, БИК {p['payerBankBic']})\n"
              f"  назначение: {p['purpose']}\n"
              f"  externalId: {p['externalId']}")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
