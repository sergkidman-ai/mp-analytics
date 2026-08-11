# поток: inv
"""invoice_bot/payment_draft.py — банконезависимое ЯДРО сборки платёжного поручения
по очереди `payment_draft_queue` (миграция 200 + `org_inn` из 207).

Тот же приём, которым 2026-08-02 развязали «выписка → МойСклад»: ядро `collectors/bank_ms.py`
плюс тонкие обёртки `alfa_ms.py` / `sber_ms.py`. Здесь ядро сборки платёжки, а банки —
`invoice_bot/alfa_payment_draft.py` (Альфа, ООО «Цифровой Квадрат») и
`invoice_bot/sber_payment_draft.py` (Сбер, ООО «ДИСКВЭР»). Выбор банка по юрлицу черновика —
`invoice_bot/payment_send.py`.

Что здесь: реквизиты плательщика и получателя из МойСклада, счета-основания, доля НДС,
назначение платежа и его ужимка, чтение очереди и простановка статусов.
Что в банках: транспорт (сессия, mTLS, авторизация), путь эндпоинта, поля, специфичные для
банка, и предохранители `*_PAYMENT_APPLY` / `*_PAYMENT_PROD_READY`.

**Разрез по нашему юрлицу обязателен** (миграция 207): поставщики у двух фирм ОБЩИЕ, приёмки и
условия оплаты — свои у каждой. Всё, что смотрит в `supplier_payment_terms` или в очередь,
режется по `draft["org_inn"]`; `DEFAULT_BUYER_INN` остался только фолбэком для древних строк.
"""
import os
import sys
import uuid
from datetime import date

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/opt/mp-analytics")          # фолбэк (canonical checkout может быть на другой ветке)
sys.path.insert(0, os.path.dirname(_HERE))       # корень ЭТОГО чекаута/worktree — приоритет
sys.path.insert(0, _HERE)                        # invoice_bot/ (для голого `import ms`)
from ms import get                               # noqa: E402
from core import db                              # noqa: E402

DEFAULT_BUYER_INN = "7807355364"   # ООО «ЦИФРОВОЙ КВАДРАТ» — фолбэк, когда юрлицо не указано
PURPOSE_MAX = 210                  # ограничение поля «Назначение платежа» в платёжном поручении РФ


# ── реквизиты сторон ─────────────────────────────────────────────────────────────────────────
def org_cache():
    return {o["inn"]: o for o in get("/entity/organization")["rows"] if o.get("inn")}


def payer_block(org_inn, account, bic=None, corr=None, sandbox=False):
    """Блок плательщика: наша организация из МС по ИНН + БИК/корсчёт по ТОМУ ЖЕ номеру счёта,
    с которого платим (МС — источник правды по реквизитам, правило 1; заодно исключает рассинхрон,
    когда счёт поменяли, а `.env` забыли). `bic`/`corr` — ручной override из `.env` банка.

    `sandbox=True` (есть только у Альфы) разрешает подставить реквизиты основного счёта
    организации, когда тестового счёта в МС нет по определению. В проме такой подмены НЕТ:
    там несовпадение счёта = ошибка (чужой БИК на реальном платеже недопустим)."""
    org = org_cache().get(org_inn)
    if not org:
        raise RuntimeError(f"организация с ИНН {org_inn} не найдена в МС")
    if not account:
        raise RuntimeError(f"не задан номер счёта плательщика (ИНН {org_inn})")
    accs = get(f"/entity/organization/{org['id']}/accounts").get("rows", [])
    ms_acc = next((a for a in accs if (a.get("accountNumber") or "") == account), None)
    if not ms_acc and sandbox:
        ms_acc = next((a for a in accs if a.get("isDefault")), None)
    bic = bic or (ms_acc or {}).get("bic")
    corr = corr or (ms_acc or {}).get("correspondentAccount")
    if sandbox and ms_acc and bic == ms_acc.get("bic"):
        # Предупреждаем ТОЛЬКО когда реквизиты реально взяты из МС: у тестового счёта песочницы
        # БИК свой (044525593), боевой БИК из МС банк отвергает — «payerBankBic is not valid».
        print(f"[песочница] счёт {account} в МС не найден, БИК/корсчёт взяты с основного счёта "
              f"организации — банк такой платёж отклонит; задай ALFA_PAYER_BANK_BIC/"
              f"ALFA_PAYER_BANK_CORR_ACCOUNT песочницы в .env")
    if not bic or not corr:
        raise RuntimeError(
            f"нет БИК/корсчёта плательщика: счёт {account} не найден в карточке «{org.get('name')}» "
            f"в МС (или у него пустые реквизиты). Либо добавь счёт в МС, либо задай "
            f"БИК/корсчёт плательщика в .env")
    return {
        "payerName": org.get("legalTitle") or org.get("name"),
        "payerInn": org.get("inn"), "payerKpp": org.get("kpp"),
        "payerAccount": account, "payerBankBic": bic, "payerBankCorrAccount": corr,
    }


def payee_card(inn, draft=None):
    """Карточка контрагента, которому платим. ИНН её НЕ определяет: у одного юрлица в МС бывает
    несколько карточек (у «Солюшнс принт» — питерская с 2012 и московская «МСК» с 2025), и
    основной счёт у них РАЗНЫЙ. `rows[0]` брать нельзя: порядок — по дате создания, то есть
    всегда старая карточка → платёж уйдёт на её счёт. Определяем по делу, а не по ИНН:

    1) агент заказов черновика — за что платим, тому и платим (единственный надёжный признак);
    2) `supplier_payment_terms.name` — имя карточки, на которую заведены условия оплаты
       НАШЕГО юрлица (нужно авансам: заказов под ними нет);
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

    # Условия оплаты — СВОИ у каждого нашего юрлица (миграция 207): у общего поставщика имя
    # карточки может отличаться, поэтому спрашиваем условия именно плательщика этого черновика.
    org_inn = (draft or {}).get("org_inn") or DEFAULT_BUYER_INN
    want = db.query("SELECT name FROM supplier_payment_terms WHERE org_inn=%s AND inn=%s",
                    (org_inn, inn))
    want = (want[0]["name"] or "").strip() if want else ""
    if want:
        cp = next((r for r in rows if (r.get("name") or "").strip() == want), None)
        if cp:
            return cp

    raise RuntimeError(
        f"у ИНН {inn} в МС {len(rows)} карточек ({', '.join(r.get('name') or '?' for r in rows)}) "
        f"— непонятно, чей счёт брать. Заведи имя нужной карточки в supplier_payment_terms.name")


def payee_block(inn, draft=None):
    cp = payee_card(inn, draft)
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


def buyer_inn_for_draft(draft):
    """ИНН организации-покупателя: по первому po_id пачки (все заказы одного поставщика —
    как правило один и тот же покупатель); нет PO (advance) — юрлицо черновика (`org_inn`
    с миграции 207; до неё — дефолтный покупатель)."""
    fallback = draft.get("org_inn") or DEFAULT_BUYER_INN
    po_ids = draft.get("covers_po_ids") or []
    if not po_ids:
        return fallback
    po = get(f"/entity/purchaseorder/{po_ids[0]}?expand=organization")
    return (po.get("organization") or {}).get("inn") or fallback


# ── основания платежа, НДС, назначение ───────────────────────────────────────────────────────
def po_docs(po_ids):
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


def vat_clause(amount, docs, vat_rate=None):
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


def purpose(draft, docs):
    """Назначение платежа: за что и по какому документу. Без документа-основания бухгалтерия
    поставщика не разнесёт платёж, поэтому номера счетов подставляем всегда, когда они есть.

    Слово «Аванс» — маркер метода, а не вежливость (решение Сергея 2026-07-29). Оно стоит ТОЛЬКО
    у `advance` (аванс/баланс поставщика: деньги вперёд, без привязки к документу) — по нему платёж
    опознаётся как авансовый и бухгалтерией поставщика, и нашим разносом привязок
    (`collectors/alfa_link.is_advance` ищет ровно это слово, дальше раскладывает FIFO).
    Предоплата по КОНКРЕТНОМУ счёту (как у Феррета) авансом не считается: у неё есть основание,
    и привязывается она по номеру счёта — точно, а не FIFO.

    Пачка «отсрочки» бывает на десяток счетов и в 210 знаков не влезает. Ужимаем ступенями
    (см. `refs_variants`), СНАЧАЛА жертвуя датами и общим префиксом номеров и только в последнюю
    очередь — самими номерами: номер счёта нужен бухгалтерии поставщика, чтобы разнести платёж,
    дата — нет, она у него и так есть по номеру."""
    # Аренда сюда доходит только аварийно: у её черновиков назначение задано текстом
    # (`purpose_text`), и `base_payment` берёт его. Строки ниже — страховка от пустого поля,
    # чтобы платёж ушёл хоть с каким-то основанием, а не с пустым назначением.
    kind_note = {"deferred_batch": "Оплата по счетам",
                 "prepayment_order": "Предоплата по счёту",
                 "advance": "Авансовый платеж за картриджи",
                 "rent": "Оплата за аренду помещения",
                 "rent_utility": "Компенсация потребляемых коммунальных услуг"}.get(
                     draft["kind"], "Оплата")
    vat = vat_clause(draft["amount"], docs, draft.get("vat_rate"))
    if not docs:
        return f"{kind_note}. {vat}"
    for refs in refs_variants(docs):
        text = f"{kind_note} {refs}. {vat}"
        if len(text) <= PURPOSE_MAX:
            return text
    # не влезло даже в самом сжатом виде — режем ПЕРЕЧЕНЬ счетов, а не хвост:
    # НДС-оговорка обязана уцелеть целиком, иначе платёж придётся уточнять письмом
    head = f"{kind_note} {refs}"
    keep = PURPOSE_MAX - len(vat) - len(" и др. ")
    return f"{head[:max(keep, 0)].rstrip(' ,.')} и др. {vat}"


def refs_variants(docs):
    """Перечень оснований от самого полного к самому сжатому — берётся первый, что влезает:
      1. `№ 6337 от 28.07.2026, № 6338 от 29.07.2026` — как есть;
      2. без дат: `№ 6337, 6338`;
      3. общий префикс номеров один раз: `№ KV00009220, 9200, 9267` (так пишет и сам владелец
         в ручных платежах) — режем только ОБЩУЮ часть, различающий хвост цел у каждого номера.
    """
    nums = [str(d["number"] or "б/н") for d in docs]
    yield ", ".join(f"№ {d['number']} от {ru_date(d['date'])}" for d in docs)
    yield "№ " + ", ".join(nums)
    if len(nums) > 1:
        pref = os.path.commonprefix(nums)
        # префикс режем только если после него у КАЖДОГО номера остаётся непустой хвост,
        # иначе счёт схлопнется в пустоту и поставщик не поймёт, что оплачено
        if len(pref) >= 2 and all(len(n) > len(pref) for n in nums):
            yield "№ " + ", ".join([nums[0]] + [n[len(pref):] for n in nums[1:]])


def ru_date(iso):
    return f"{iso[8:10]}.{iso[5:7]}.{iso[0:4]}" if len(iso) >= 10 else iso


# ── тело платёжки (общая часть) ──────────────────────────────────────────────────────────────
def base_payment(draft, extid_ns, docs=None):
    """Плоское тело РПП без блока плательщика и без полей, специфичных для банка.

    `number` НЕ шлём (решение Сергея 2026-07-29): банк нумерует платёжки своей сквозной
    нумерацией, наш номер только конфликтовал бы с ней в бухгалтерии.
    `externalId` ДЕТЕРМИНИРОВАННЫЙ (uuid5 от id черновика в пространстве имён банка): повторная
    отправка того же черновика = тот же id в банке, а не вторая платёжка на те же деньги.

    Арендные черновики (миграция 215) приносят своё: `payee` — реквизиты ИЗ СЧЁТА арендодателя
    (сверенные с карточкой МС ещё на разборе, см. `rent_core.payee_mismatch`), `purpose_text` —
    назначение, продиктованное договором. Обоих полей нет у платежей поставщикам — там прежний
    путь: реквизиты из МС, назначение собирается из заказов."""
    docs = po_docs(draft.get("covers_po_ids")) if docs is None else docs
    payee = draft.get("payee") or payee_block(draft["inn"], draft)
    return {
        "date": date.today().isoformat(),
        "amount": float(draft["amount"]),
        "externalId": str(uuid.uuid5(extid_ns, str(draft["id"]))),
        "purpose": draft.get("purpose_text") or purpose(draft, docs),
        "operationCode": "01",      # платёжное поручение
        "priority": "5",            # очередность платежа (ст. 855 ГК: расчёты с поставщиками)
        "deliveryKind": "электронно",
        **payee,
    }


# ── очередь: чтение и статусы ────────────────────────────────────────────────────────────────
_Q = """SELECT q.*, t.vat_rate FROM payment_draft_queue q
        LEFT JOIN supplier_payment_terms t USING (org_inn, inn)"""


def load_drafts(org_inn=None, draft_ids=None, only_planned=True):
    """Черновики очереди со ставкой НДС поставщика.

    vat_rate подтягиваем из условий оплаты: ставка НДС — свойство поставщика, а не черновика,
    в очереди её не дублируем. LEFT JOIN — поставщика может не быть в таблице условий (тогда
    NULL и прежний вывод НДС из заказа МС). JOIN по (org_inn, inn), а не по одному inn:
    с миграции 207 у ИНН поставщика может быть две строки условий (Цифровой Квадрат / Дисквэр) —
    join по одному inn задвоил бы черновик.

    `org_inn=None` — без разреза по юрлицу (так читает диспетчер `payment_send`, которому юрлицо
    как раз и надо узнать); банковский драйвер ВСЕГДА передаёт своё."""
    where, params = [], []
    if only_planned:
        where.append("q.status='planned'")
    if org_inn:
        where.append("q.org_inn=%s")
        params.append(org_inn)
    if draft_ids:
        where.append("q.id = ANY(%s)")
        params.append(list(draft_ids))
    sql = f"{_Q} WHERE {' AND '.join(where)}" if where else _Q
    sql += " ORDER BY q.id" if draft_ids else " ORDER BY q.created_at"
    return db.query(sql, tuple(params))


def mark_sent(draft_id, status, ext_id):
    """ЗАКАЗЫ НЕ ПОМЕЧАЕМ 'paid' ПРИ ОТПРАВКЕ. Отправлен НЕПОДПИСАННЫЙ черновик — деньги уйдут
    только когда человек подпишет его в вебе банка, а может и не подписать. Факт оплаты приходит
    из МС (payedSum) — его ловит гейт в `po_payment_watch._sync_pending` и сам переводит заказ
    в 'paid'. Раньше здесь стоял 'paid' на отправке: заказ пропадал из пула, даже если черновик
    так и остался неподписанным (тихая потеря долга)."""
    db.execute("UPDATE payment_draft_queue SET status=%s, alfa_external_id=%s WHERE id=%s",
               (status, ext_id, draft_id))


def mark_error(draft_id, note, po_ids=None):
    """Черновик в 'error' + возврат его заказов в пул: иначе они висят `queued` под мёртвым
    черновиком и не попадут в следующую пачку."""
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE payment_draft_queue SET status='error', note=%s WHERE id=%s",
                        (str(note)[:500], draft_id))
            if po_ids:
                cur.execute("UPDATE po_payment_status SET status='pending', draft_id=NULL "
                            "WHERE po_id = ANY(%s)", (list(po_ids),))
