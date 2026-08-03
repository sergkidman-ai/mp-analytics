# поток: inv
"""invoice_bot/alfa_payment_draft.py — отправка НЕПОДПИСАННЫХ черновиков платёжек в Альфа-Банк
по очереди payment_draft_queue (см. миграцию 200_supplier_payment_terms.sql).

ДРАЙВЕР БАНКА: сборка платёжки (реквизиты из МС, счета-основания, НДС, назначение, статусы
очереди) живёт в банконезависимом ядре `invoice_bot/payment_draft.py` — вынесена туда
2026-08-03, когда рядом появился контур Сбера (`invoice_bot/sber_payment_draft.py`,
ООО «ДИСКВЭР»). Здесь остаётся только альфовое: транспорт, путь, поля Альфы, предохранители.
Организация — ООО «Цифровой Квадрат» (`ORG_INN`); черновик чужого юрлица драйвер отвергает.

POST /api/jp/v2/payments БЕЗ digestSignatures → черновик, виден в Альфа-Бизнес веб
(«Платежи в работе» → «На подпись»), подписывает человек. Реквизиты получателя — из карточки
контрагента МС (invoice_bot/supplier_requisites.py уже наполняет default-счёт); плательщик —
организация-покупатель из МС (name/inn/kpp) + наш р/с/БИК/корсчёт из .env.

Двойной предохранитель (зеркало ALFA_MS_APPLY для выписки, см. collectors/alfa_ms.py):
  • ALFA_PAYMENT_APPLY=1     — иначе dry-run (только печатает, что бы отправил).
  • ALFA_PAYMENT_PROD_READY=1 — прод ЖЁСТКО заблокирован программно, пока не выставлен
    вручную (прод-скоуп `signature` банком пока НЕ выдан — см. docs/HANDOFF.md).

КОНТРАКТ ТЕЛА (подобран живьём в песочнице 2026-07-29, ответ 201; до этого слали неверный формат
и получили бы 400 даже с выданным скоупом). Обязательны:
  date, amount
  externalId    — UUID; банк возвращает его эхом. Детерминированный (uuid5 от id черновика) —
                  см. `payment_draft.base_payment`.
  purpose       — назначение платежа. Именно `purpose`, НЕ `paymentPurpose`.
  operationCode — "01" (платёжное поручение), priority — "5" (очередность по ст. 855 ГК)
  urgencyCode, deliveryKind + ПЛОСКИЕ payer*/payee* (вложенные payer{}/payee{} не нужны).
`number` НЕ шлём (решение Сергея 2026-07-29): банк нумерует платёжки сам своей сквозной нумерацией,
и наш номер только конфликтовал бы с ней в бухгалтерии. Проверено — тело без `number` принимается
(201). Если банк передумает и потребует поле — оно должно быть ТОЛЬКО ЦИФРАМИ (прежний `D{id}`
отвергался: «Parameter number is not valid»).
Ответ содержит `digestSignatures: []` — платёжка не подписана, лежит в вебе банка «На подпись».
⚠️ Песочница — ЗАГЛУШКА: эхом отдаёт только externalId и amount, остальное подменяет фикстурой
(дата 2022 г., чужой счёт получателя, bankStatus IMPLEMENTED). По её ответу можно судить ТОЛЬКО
о том, что схема валидна и права есть, но НЕ о правильности реквизитов.

get_balance() — read-only GET /pp/v1/accounts. НЕ ИСПОЛЬЗУЕТСЯ И НЕ БУДЕТ: поддержка Альфы
(2026-07-29) сообщила, что метод для ФИЗЛИЦ, для ЮЛ скоуп `accounts` не выдаётся — здесь навсегда
403. Пачки в po_payment_watch.process_deferred режутся ТОЛЬКО по payment_cap (решение Сергея
2026-07-29). Функция оставлена на случай, если банк когда-нибудь откроет метод для ЮЛ.

Запуск:
  ALFA_ENV=sandbox ALFA_PAYMENT_APPLY=1 ./venv/bin/python invoice_bot/alfa_payment_draft.py
  ALFA_ENV=sandbox ALFA_PAYMENT_APPLY=1 ./venv/bin/python invoice_bot/alfa_payment_draft.py --draft 6
      # --draft N — отправить ТОЛЬКО черновик N (остальные 'planned' не трогать)
"""
import os
import sys
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/opt/mp-analytics")          # фолбэк (canonical checkout может быть на другой ветке)
sys.path.insert(0, os.path.dirname(_HERE))       # корень ЭТОГО чекаута/worktree — приоритет
sys.path.insert(0, _HERE)                        # invoice_bot/ (для голого `import ms`)
# ВАЖНО: collectors.* импортировать ДО `ms` — ms.py сам делает sys.path.insert(0, "/opt/mp-analytics")
# при импорте (побочный эффект), что отодвигает наш worktree и может подставить canonical-чекаут
# (он бывает на другой ветке без этого файла) вместо текущего.
from collectors.alfa_statement import _cfg, _session      # noqa: E402
import payment_draft as pd                                # noqa: E402  банконезависимое ядро

ACCOUNTS_PATH = "/pp/v1/accounts"
PAYMENTS_PATH = "/jp/v2/payments"
NAME = "Альфа-Банк"
ORG_INN = os.getenv("ALFA_ORG_INN", "7807355364")   # ООО «ЦИФРОВОЙ КВАДРАТ»
DEFAULT_BUYER_INN = ORG_INN                         # историческое имя, используется извне
EXTID_NS = uuid.uuid5(uuid.NAMESPACE_URL, "mp-analytics/alfa/payment_draft")


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


def apply_enabled():
    return os.getenv("ALFA_PAYMENT_APPLY") == "1"


def _payer_block(buyer_inn, cfg):
    prod = cfg["env"] != "sandbox"
    suffix = "_PROD" if prod else ""
    account = os.getenv(f"ALFA_ACCOUNT{suffix}")
    if not account:
        raise RuntimeError("нет в .env: ALFA_ACCOUNT" + suffix)
    return pd.payer_block(
        buyer_inn, account,
        bic=os.getenv(f"ALFA_PAYER_BANK_BIC{suffix}"),
        corr=os.getenv(f"ALFA_PAYER_BANK_CORR_ACCOUNT{suffix}"),
        sandbox=not prod)


def build_payment(draft, cfg):
    payer = _payer_block(pd.buyer_inn_for_draft(draft), cfg)
    return {
        **pd.base_payment(draft, EXTID_NS),
        "urgencyCode": "NORMAL",
        **payer,
    }


def _check_org(draft):
    """Рубеж против кросс-банка: черновик Дисквэра не имеет права уйти через Альфу (и наоборот).
    Разрез по нашему юрлицу — несущая конструкция всего контура (миграция 207)."""
    org = draft.get("org_inn") or ORG_INN
    if org != ORG_INN:
        raise RuntimeError(f"черновик {draft.get('id')} принадлежит юрлицу {org}, а это контур "
                           f"{NAME} (ИНН {ORG_INN}) — отправка запрещена")


def send_one(draft, dry_run=True, cfg=None, session=None):
    """Отправить ОДИН черновик. → {ok, stage, status, external_id, error, payload}.
    Ничего не печатает: печать — дело вызывающего (CLI печатает, веб отдаёт JSON)."""
    cfg = cfg or _cfg()
    prod = cfg["env"] != "sandbox"
    # Гейт прода стоит только на РЕАЛЬНОЙ отправке: dry-run банк не трогает, а проверить сборку
    # боевого payload (реквизиты плательщика/получателя) нужно ДО того, как банк выдаст скоуп.
    if prod and not dry_run and os.getenv("ALFA_PAYMENT_PROD_READY") != "1":
        raise RuntimeError(
            "ALFA_ENV=prod для платежей ЗАБЛОКИРОВАН программно: банк ещё не выдал прод-скоуп "
            "`signature`. Разблокировка вручную ТОЛЬКО после подтверждения банком — "
            "выставить ALFA_PAYMENT_PROD_READY=1 в .env. См. docs/HANDOFF.md.")
    _check_org(draft)
    try:
        payload = build_payment(draft, cfg)
    except Exception as e:
        if not dry_run:
            # в dry-run очередь НЕ трогаем: проверка сборки не должна оставлять реальные
            # черновики в статусе 'error' (ловилось на себе)
            pd.mark_error(draft["id"], str(e))
        return {"ok": False, "stage": "build", "status": None, "external_id": None,
                "error": str(e), "payload": None}
    if dry_run:
        return {"ok": True, "stage": "dry_run", "status": "dry_run",
                "external_id": payload["externalId"], "error": None, "payload": payload}

    s = session or _session(cfg)
    r = s.post(f"{cfg['base']}{PAYMENTS_PATH}", json=payload, timeout=60)
    status = "sent_prod" if prod else "sent_sandbox"
    if r.status_code in (200, 201):
        body = r.json() or {}
        ext_id = body.get("externalId") or body.get("id")
        pd.mark_sent(draft["id"], status, ext_id)
        return {"ok": True, "stage": "sent", "status": status, "external_id": ext_id,
                "error": None, "payload": payload}
    note = f"HTTP {r.status_code}: {r.text[:300]}"
    pd.mark_error(draft["id"], note, draft.get("covers_po_ids"))
    return {"ok": False, "stage": "http", "status": "error", "external_id": None,
            "error": note, "payload": payload}


def send_planned(dry_run=True, cfg=None, draft_ids=None):
    """draft_ids — отправить ТОЛЬКО эти черновики (остальные 'planned' не трогать). Нужен для
    точечного прогона одного счёта; без него уходит вся очередь Цифрового Квадрата."""
    cfg = cfg or _cfg()
    prod = cfg["env"] != "sandbox"
    planned = pd.load_drafts(org_inn=ORG_INN, draft_ids=draft_ids)
    if draft_ids:
        missing = set(draft_ids) - {r["id"] for r in planned}
        if missing:
            print(f"нет в статусе 'planned': {sorted(missing)} — пропущены")
    if not planned:
        print("очередь пуста — нечего отправлять")
        return
    session = _session(cfg)
    for draft in planned:
        res = send_one(draft, dry_run=dry_run, cfg=cfg, session=session)
        if res["stage"] == "build":
            print(f"[draft {draft['id']}] не смог собрать payload: {res['error']}")
        elif res["stage"] == "dry_run":
            p = res["payload"]
            print(f"[DRY-RUN] draft {draft['id']} ({draft['kind']}, {draft['amount']}₽) → "
                  f"{p['payeeName']} ({p['payeeAccount']}), покупатель {p['payerName']}\n"
                  f"          от {p['date']}, externalId {p['externalId']} (№ присвоит банк)\n"
                  f"          назначение: {p['purpose']}")
        elif res["ok"]:
            print(f"[draft {draft['id']}] отправлен ({res['status']}) externalId={res['external_id']}"
                  f"{' — ПЕСОЧНИЦА, реальных денег нет' if not prod else ''}; "
                  f"заказы остаются в пуле до факта оплаты в МС")
        else:
            print(f"[draft {draft['id']}] ОШИБКА: {res['error']}")


def main():
    draft_ids = None
    if "--draft" in sys.argv:
        draft_ids = [int(x) for x in sys.argv[sys.argv.index("--draft") + 1].split(",")]
    try:
        send_planned(dry_run=not apply_enabled(), draft_ids=draft_ids)
    except RuntimeError as e:      # гейт прода: раньше был sys.exit прямо в send_planned
        sys.exit(str(e))


if __name__ == "__main__":
    main()
