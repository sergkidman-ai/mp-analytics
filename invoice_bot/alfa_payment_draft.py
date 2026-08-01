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

КОНТРАКТ ТЕЛА (подобран живьём в песочнице 2026-07-29, ответ 201; до этого слали неверный формат
и получили бы 400 даже с выданным скоупом). Обязательны:
  date, amount
  externalId    — UUID; банк возвращает его эхом. Делаем ДЕТЕРМИНИРОВАННЫМ (uuid5 от id черновика),
                  чтобы повторная отправка того же черновика не плодила вторую платёжку в банке.
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
    if not account:
        raise RuntimeError("нет в .env: ALFA_ACCOUNT" + ("_PROD" if prod else ""))
    # БИК/корсчёт берём из карточки организации в МС — по ТОМУ ЖЕ номеру счёта, с которого платим
    # (МС — источник правды по реквизитам, правило 1; заодно исключает рассинхрон, когда счёт
    # поменяли, а .env забыли). .env оставлен как ручной override/фолбэк.
    accs = get(f"/entity/organization/{org['id']}/accounts").get("rows", [])
    ms_acc = next((a for a in accs if (a.get("accountNumber") or "") == account), None)
    if not ms_acc and not prod:
        # В песочнице номер счёта тестовый и в МС его нет по определению — берём реквизиты
        # основного счёта организации, чтобы проверить сборку payload. В проме такой подмены
        # НЕТ: там несовпадение счёта = ошибка (чужой БИК на реальном платеже недопустим).
        ms_acc = next((a for a in accs if a.get("isDefault")), None)
    bic = (os.getenv("ALFA_PAYER_BANK_BIC_PROD" if prod else "ALFA_PAYER_BANK_BIC")
           or (ms_acc or {}).get("bic"))
    corr = (os.getenv("ALFA_PAYER_BANK_CORR_ACCOUNT_PROD" if prod else "ALFA_PAYER_BANK_CORR_ACCOUNT")
            or (ms_acc or {}).get("correspondentAccount"))
    if not prod and ms_acc and bic == ms_acc.get("bic"):
        # Предупреждаем ТОЛЬКО когда реквизиты реально взяты из МС: у тестового счёта песочницы
        # БИК свой (044525593), боевой БИК из МС банк отвергает — «payerBankBic is not valid».
        print(f"[песочница] счёт {account} в МС не найден, БИК/корсчёт взяты с основного счёта "
              f"организации — банк такой платёж отклонит; задай ALFA_PAYER_BANK_BIC/"
              f"ALFA_PAYER_BANK_CORR_ACCOUNT песочницы в .env")
    if not bic or not corr:
        raise RuntimeError(
            f"нет БИК/корсчёта плательщика: счёт {account} не найден в карточке «{org.get('name')}» "
            f"в МС (или у него пустые реквизиты). Либо добавь счёт в МС, либо задай "
            f"ALFA_PAYER_BANK_BIC/ALFA_PAYER_BANK_CORR_ACCOUNT в .env")
    return {
        "payerName": org.get("legalTitle") or org.get("name"),
        "payerInn": org.get("inn"), "payerKpp": org.get("kpp"),
        "payerAccount": account, "payerBankBic": bic, "payerBankCorrAccount": corr,
    }


def _payee_card(inn, draft=None):
    """Карточка контрагента, которому платим. ИНН её НЕ определяет: у одного юрлица в МС бывает
    несколько карточек (у «Солюшнс принт» — питерская с 2012 и московская «МСК» с 2025), и
    основной счёт у них РАЗНЫЙ. `rows[0]` брать нельзя: порядок — по дате создания, то есть
    всегда старая карточка → платёж уйдёт на её счёт. Определяем по делу, а не по ИНН:

    1) агент заказов черновика — за что платим, тому и платим (единственный надёжный признак);
    2) `supplier_payment_terms.name` — имя карточки, на которую заведены условия оплаты
       (нужно авансам: заказов под ними нет);
    3) карточка одна — она и есть.

    Ничего не подошло — ОШИБКА, а не «возьмём первую»: молча уплаченные не туда деньги
    возвращать через банк дороже, чем разобрать черновик руками."""
    rows = get(f"/entity/counterparty?filter=inn={inn}").get("rows", [])
    if not rows:
        raise RuntimeError(f"контрагент с ИНН {inn} не найден в МС")
    if len(rows) == 1:
        return rows[0]

    po_ids = (draft or {}).get("covers_po_ids") or []
    if po_ids:
        po = get(f"/entity/purchaseorder/{po_ids[0]}?expand=agent")
        agent_id = ((po.get("agent") or {}).get("id"))
        cp = next((r for r in rows if r["id"] == agent_id), None)
        if cp:
            return cp

    want = db.query("SELECT name FROM supplier_payment_terms WHERE inn=%s", (inn,))
    want = (want[0]["name"] or "").strip() if want else ""
    if want:
        cp = next((r for r in rows if (r.get("name") or "").strip() == want), None)
        if cp:
            return cp

    raise RuntimeError(
        f"у ИНН {inn} в МС {len(rows)} карточек ({', '.join(r.get('name') or '?' for r in rows)}) "
        f"— непонятно, чей счёт брать. Заведи имя нужной карточки в supplier_payment_terms.name")


def _payee_block(inn, draft=None):
    cp = _payee_card(inn, draft)
    accs = get(f"/entity/counterparty/{cp['id']}/accounts").get("rows", [])
    acc = next((a for a in accs if a.get("isDefault")), accs[0] if accs else None)
    if not acc:
        raise RuntimeError(f"у контрагента {cp.get('name')} (ИНН {inn}) нет банковских реквизитов в МС "
                           f"— прогони invoice_bot/supplier_requisites.py --apply")
    return {
        # В поручение идёт ЮРИДИЧЕСКОЕ название, а не имя карточки: суффикс «МСК» — наша
        # внутренняя пометка, в реквизитах платежа ей не место.
        "payeeName": cp.get("legalTitle") or cp.get("name"), "payeeInn": inn, "payeeKpp": cp.get("kpp"),
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


PURPOSE_MAX = 210          # ограничение поля «Назначение платежа» в платёжном поручении РФ
EXTID_NS = uuid.uuid5(uuid.NAMESPACE_URL, "mp-analytics/alfa/payment_draft")


def _po_docs(po_ids):
    """Счета-основания из МС по заказам черновика: номер (= номер счёта поставщика), дата, НДС.
    Нужны и для назначения платежа, и для доли НДС в нём."""
    docs = []
    for po_id in po_ids or []:
        po = get(f"/entity/purchaseorder/{po_id}")
        docs.append({
            "number": po.get("name") or "б/н",
            "date": (po.get("moment") or "")[:10],
            "sum": round((po.get("sum") or 0) / 100, 2),
            "vat_sum": round((po.get("vatSum") or 0) / 100, 2),
            "vat_enabled": bool(po.get("vatEnabled")),
        })
    return docs


def _vat_clause(amount, docs, vat_rate=None):
    """Строка НДС для назначения платежа. Считаем ДОЛЮ НДС в оплачиваемой сумме (платёж может
    быть частью счёта или пачкой счетов), а не переписываем НДС счёта целиком.

    ИСТОЧНИК СТАВКИ — `supplier_payment_terms.vat_rate`, проверенный владельцем глазами
    (решение Сергея 2026-07-29). Ставка — свойство ПОСТАВЩИКА, а не документа: у аванса заказа
    нет вообще, и прежний вывод из МС давал «Без НДС» там, где поставщик на ОСНО. Формула —
    обычная «в том числе»: НДС = сумма × ставка / (100 + ставка); сверено с ручными авансами
    владельца (50 000 → 9016.39, 100 000 → 18032.79 — совпало до копейки).

    `vat_rate is None` (в таблице не заполнено) → прежнее поведение: НДС из заказа МС."""
    if vat_rate is not None:
        if vat_rate == 0:
            return "Без НДС."
        share = round(float(amount) * vat_rate / (100 + vat_rate), 2)
        return f"В т.ч. НДС {vat_rate}% - {share:.2f} руб."
    total = sum(d["sum"] for d in docs)
    vat = sum(d["vat_sum"] for d in docs)
    if not docs or not any(d["vat_enabled"] for d in docs) or vat <= 0 or total <= 0:
        return "Без НДС."
    share = round(float(amount) * vat / total, 2)
    rate = round(vat / (total - vat) * 100)      # 20% при обычной ставке; считаем, не хардкодим
    return f"В т.ч. НДС {rate}% - {share:.2f} руб."


def _purpose(draft, docs):
    """Назначение платежа: за что и по какому документу. Без документа-основания бухгалтерия
    поставщика не разнесёт платёж, поэтому номера счетов подставляем всегда, когда они есть.

    Слово «Аванс» — маркер метода, а не вежливость (решение Сергея 2026-07-29). Оно стоит ТОЛЬКО
    у `advance` (аванс/баланс поставщика: деньги вперёд, без привязки к документу) — по нему платёж
    опознаётся как авансовый и бухгалтерией поставщика, и нашим разносом привязок
    (`collectors/alfa_link.is_advance` ищет ровно это слово, дальше раскладывает FIFO).
    Предоплата по КОНКРЕТНОМУ счёту (как у Феррета) авансом не считается: у неё есть основание,
    и привязывается она по номеру счёта — точно, а не FIFO.

    Пачка «отсрочки» бывает на десяток счетов и в 210 знаков не влезает. Ужимаем ступенями
    (см. `_refs_variants`), СНАЧАЛА жертвуя датами и общим префиксом номеров и только в последнюю
    очередь — самими номерами: номер счёта нужен бухгалтерии поставщика, чтобы разнести платёж,
    дата — нет, она у него и так есть по номеру."""
    kind_note = {"deferred_batch": "Оплата по счетам",
                 "prepayment_order": "Предоплата по счёту",
                 "advance": "Авансовый платеж за картриджи"}.get(draft["kind"], "Оплата")
    vat = _vat_clause(draft["amount"], docs, draft.get("vat_rate"))
    if not docs:
        return f"{kind_note}. {vat}"
    for refs in _refs_variants(docs):
        purpose = f"{kind_note} {refs}. {vat}"
        if len(purpose) <= PURPOSE_MAX:
            return purpose
    # не влезло даже в самом сжатом виде — режем ПЕРЕЧЕНЬ счетов, а не хвост:
    # НДС-оговорка обязана уцелеть целиком, иначе платёж придётся уточнять письмом
    head = f"{kind_note} {refs}"
    keep = PURPOSE_MAX - len(vat) - len(" и др. ")
    return f"{head[:max(keep, 0)].rstrip(' ,.')} и др. {vat}"


def _refs_variants(docs):
    """Перечень оснований от самого полного к самому сжатому — берётся первый, что влезает:
      1. `№ 6337 от 28.07.2026, № 6338 от 29.07.2026` — как есть;
      2. без дат: `№ 6337, 6338`;
      3. общий префикс номеров один раз: `№ KV00009220, 9200, 9267` (так пишет и сам владелец
         в ручных платежах) — режем только ОБЩУЮ часть, различающий хвост цел у каждого номера.
    """
    nums = [str(d["number"] or "б/н") for d in docs]
    yield ", ".join(f"№ {d['number']} от {_ru_date(d['date'])}" for d in docs)
    yield "№ " + ", ".join(nums)
    if len(nums) > 1:
        pref = os.path.commonprefix(nums)
        # префикс режем только если после него у КАЖДОГО номера остаётся непустой хвост,
        # иначе счёт схлопнется в пустоту и поставщик не поймёт, что оплачено
        if len(pref) >= 2 and all(len(n) > len(pref) for n in nums):
            yield "№ " + ", ".join([nums[0]] + [n[len(pref):] for n in nums[1:]])


def _ru_date(iso):
    return f"{iso[8:10]}.{iso[5:7]}.{iso[0:4]}" if len(iso) >= 10 else iso


def build_payment(draft, cfg):
    payer = _payer_block(_buyer_inn_for_draft(draft), cfg)
    payee = _payee_block(draft["inn"], draft)
    docs = _po_docs(draft.get("covers_po_ids"))
    return {
        # number НЕ шлём — номер платёжки присваивает банк (см. докстринг модуля)
        "date": date.today().isoformat(),
        "amount": float(draft["amount"]),
        # детерминированный externalId: повтор отправки того же черновика = тот же id в банке,
        # а не вторая платёжка на те же деньги
        "externalId": str(uuid.uuid5(EXTID_NS, str(draft["id"]))),
        "purpose": _purpose(draft, docs),
        "operationCode": "01",      # платёжное поручение
        "priority": "5",            # очередность платежа (ст. 855 ГК: расчёты с поставщиками)
        "urgencyCode": "NORMAL",
        "deliveryKind": "электронно",
        **payer, **payee,
    }


def send_planned(dry_run=True, cfg=None, draft_ids=None):
    """draft_ids — отправить ТОЛЬКО эти черновики (остальные 'planned' не трогать). Нужен для
    точечного прогона одного счёта; без него уходит вся очередь."""
    cfg = cfg or _cfg()
    prod = cfg["env"] != "sandbox"
    # Гейт прода стоит только на РЕАЛЬНОЙ отправке: dry-run банк не трогает, а проверить сборку
    # боевого payload (реквизиты плательщика/получателя) нужно ДО того, как банк выдаст скоуп.
    if prod and not dry_run and os.getenv("ALFA_PAYMENT_PROD_READY") != "1":
        sys.exit("ALFA_ENV=prod для платежей ЗАБЛОКИРОВАН программно: банк ещё не выдал прод-скоуп "
                 "`signature`. Разблокировка вручную ТОЛЬКО после подтверждения банком — "
                 "выставить ALFA_PAYMENT_PROD_READY=1 в .env. См. docs/HANDOFF.md.")
    session = _session(cfg)
    # vat_rate подтягиваем из условий оплаты: ставка НДС — свойство поставщика, а не черновика,
    # в очереди её не дублируем. LEFT JOIN — поставщика может не быть в таблице условий (тогда
    # NULL и прежний вывод НДС из заказа МС).
    Q = """SELECT q.*, t.vat_rate FROM payment_draft_queue q
           LEFT JOIN supplier_payment_terms t USING (inn) WHERE q.status='planned'"""
    if draft_ids:
        planned = db.query(f"{Q} AND q.id = ANY(%s) ORDER BY q.id", (list(draft_ids),))
        missing = set(draft_ids) - {r["id"] for r in planned}
        if missing:
            print(f"нет в статусе 'planned': {sorted(missing)} — пропущены")
    else:
        planned = db.query(f"{Q} ORDER BY q.created_at")
    if not planned:
        print("очередь пуста — нечего отправлять")
        return
    for draft in planned:
        try:
            payload = build_payment(draft, cfg)
        except Exception as e:
            print(f"[draft {draft['id']}] не смог собрать payload: {e}")
            if not dry_run:
                # в dry-run очередь НЕ трогаем: проверка сборки не должна оставлять
                # реальные черновики в статусе 'error' (ловилось на себе)
                db.execute("UPDATE payment_draft_queue SET status='error', note=%s WHERE id=%s",
                           (str(e)[:500], draft["id"]))
            continue
        if dry_run:
            print(f"[DRY-RUN] draft {draft['id']} ({draft['kind']}, {draft['amount']}₽) → "
                  f"{payload['payeeName']} ({payload['payeeAccount']}), покупатель {payload['payerName']}\n"
                  f"          от {payload['date']}, externalId {payload['externalId']} (№ присвоит банк)\n"
                  f"          назначение: {payload['purpose']}")
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
    draft_ids = None
    if "--draft" in sys.argv:
        draft_ids = [int(x) for x in sys.argv[sys.argv.index("--draft") + 1].split(",")]
    send_planned(dry_run=not apply_on, draft_ids=draft_ids)


if __name__ == "__main__":
    main()
