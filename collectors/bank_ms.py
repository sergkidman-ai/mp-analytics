# поток: inv
"""collectors/bank_ms.py — банконезависимое ядро «выписка → МойСклад» (paymentin/paymentout).

Вынесено из `alfa_ms.py` 2026-08-02, когда рядом с Альфой (ООО «Цифровой Квадрат») появился
Сбер (ООО «ДИСКВЭР»). Ядро зависит ТОЛЬКО от нормализованной операции выписки — той самой
плоской записи, которую одинаково отдают `alfa_statement.normalize` и `sber_statement.normalize`.
Банк-специфичное (откуда брать выписку, чья организация, привязка к приёмкам) живёт
в тонких обёртках: `alfa_ms.py`, `sber_ms.py`.

CREDIT (приход) → paymentin, DEBIT (расход) → paymentout.

Идемпотентность: банковский `uuid` операции — готовый GUID, кладём его в МС `syncId`;
нет uuid — детерминированный uuid5 от transactionId. Повторный прогон за тот же период
не плодит дублей (правило 3).

ОРГАНИЗАЦИЯ — не декорация: в одном аккаунте МС живут обе фирмы. Антидубль обязан
фильтровать по организации, иначе платёж одной фирмы «съедает» платёж другой на ту же сумму
в тот же день. Замер 2026-08-02 по май–авг: 7 таких пар (день+сумма) между Цифровым
Квадратом и Дисквэром среди paymentout. Поэтому `existing_index` ходит с org_id всегда.

БЕЗОПАСНОСТЬ: `sync(..., apply=False)` — только чтение МС и план; писать может лишь обёртка,
явно передавшая apply=True.
"""
import os
import re
import sys
import uuid as _uuid
import pathlib
import urllib.parse
import urllib.error

HERE = pathlib.Path(__file__).resolve().parent           # каталог collectors/
BASE_DIR = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(BASE_DIR))                        # core.db — выбор карточки контрагента
sys.path.insert(0, "/opt/mp-analytics/invoice_bot")
from ms import get, post, MS                             # noqa: E402  invoice_bot/ms.py
import core.db as db                                     # noqa: E402

_NS = _uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # namespace для uuid5

# ── что НЕ заводим в МС из выписки (решение Сергея 2026-07-29) ────────────────────────────
# Зарплата/отпускные и выплаты самозанятым в МС не ведутся — это не движение товара и не
# расчёты с поставщиками; их учёт идёт вне МС. Плюс поимённый список ИНН.
IGNORE_INN = {"280803483344"}
# Признак «расход физлицу»: ИНН 12 знаков И в названии нет организационно-правовой формы.
# ИП тоже 12-значный, поэтому ловим форму по названию — платёж ИП-поставщику НЕ должен попасть
# под правило (в выписке он приходит как «ИП Иванов Иван Иванович»).
_LEGAL_FORM = re.compile(
    r"(?i)(?:^|[\s\"«(])(ООО|ОАО|ЗАО|ПАО|АО|НАО|НКО|ИП|КФХ|ПК|АНО|ФГУП|ГУП|МУП|"
    r"УФК|ФНС|БАНК|ФИЛИАЛ|ГУФССП|УФССП)(?:$|[\s\".»)])")


def skip_reason(op):
    """→ причина, по которой операцию не заводим в МС, либо None (заводим).

    Гейт стоит ПЕРЕД антидублем и созданием контрагента: игнорируемые операции не должны ни
    плодить карточки контрагентов, ни попадать в счётчики прихода/расхода.
    """
    inn = (op.get("counterparty_inn") or "").strip()
    if inn in IGNORE_INN:
        return "ИНН в списке игнора"
    if op.get("direction") == "DEBIT" and len(inn) == 12 and not _LEGAL_FORM.search(
            op.get("counterparty_name") or ""):
        return "выплата физлицу (зарплата/самозанятый)"
    return None


def _meta(ent, i, t=None):
    return {"meta": {"href": f"{MS}/entity/{ent}/{i}", "type": t or ent,
                     "mediaType": "application/json"}}


def _norm(s):
    return " ".join((s or "").lower().split())


def _err_text(resp):
    """Короткий человекочитаемый текст ошибки МС из тела ответа."""
    try:
        errs = resp.get("errors") or []
        return "; ".join(str(e.get("error") or e)[:160] for e in errs[:2]) or str(resp)[:200]
    except Exception:
        return str(resp)[:200]


def _ms_dt(iso):
    # "2026-07-22T00:00:00Z" → "2026-07-22 00:00:00"
    # Голая дата "2026-07-22" (так приходит documentDate выписки) → добиваем полночью:
    # МС принимает только полный дата-время, иначе 400 «не соответствует типу дата-время».
    s = (iso or "").replace("T", " ").replace("Z", "")[:19]
    if not s:
        return None
    return f"{s} 00:00:00" if len(s) == 10 else s


def _sync_id(op):
    if op.get("uuid"):
        return op["uuid"]
    seed = op.get("transaction_id") or f"{op.get('operation_date')}|{op.get('amount')}"
    return str(_uuid.uuid5(_NS, seed))


def _day_bounds(day):
    return f"{day} 00:00:00", f"{day} 23:59:59"


def _kop(amount):
    """Сумма операции → копейки. Сбер отдаёт Decimal, Альфа — float; round() корректен для обоих."""
    return int(round((amount or 0) * 100))


def existing_index(typ, day, org_id=None):
    """Индекс платежей, УЖЕ лежащих в МС за этот день у ЭТОЙ организации.

    Зачем: выписку по счёту в МС годами заводили руками (загрузка банк-файла), у таких
    документов нет нашего `syncId` — по нему мы их не увидим и завели бы вторые копии.
    Поэтому перед записью сверяемся ещё и «по-человечески»: сумма + номер банковского
    документа + день. Ключ `sync` — наши собственные документы, их PUT просто обновит.

    org_id обязателен по смыслу (в аккаунте две фирмы), необязателен по сигнатуре только
    ради обратной совместимости старых вызовов. Фильтр собирается штатным `quote()` со
    служебным safe='/': МС принимает percent-encoded `;`, `=`, `>` — а вот оставленный
    сырым `>` даёт HTTP 400 (проверено 2026-08-02).
    """
    lo, hi = _day_bounds(day)
    terms = [f"moment>={lo}", f"moment<={hi}"]
    if org_id:
        terms.insert(0, f"organization={MS}/entity/organization/{org_id}")
    flt = urllib.parse.quote(";".join(terms))
    idx = {"by_num": {}, "by_sum": {}, "sync": set(), "total": 0}
    offset = 0
    while True:
        r = get(f"/entity/{typ}?filter={flt}&limit=100&offset={offset}")
        rows = r.get("rows", [])
        for d in rows:
            s = d.get("sum")
            if d.get("syncId"):
                idx["sync"].add(d["syncId"])
            for num in {str(d.get("name") or "").strip(),
                        str(d.get("incomingNumber") or "").strip()}:
                if num:
                    idx["by_num"].setdefault((s, num), []).append(d)
            idx["by_sum"].setdefault(s, []).append(d)
        idx["total"] += len(rows)
        if len(rows) < 100:
            break
        offset += 100
    return idx


def find_existing(idx, op, sync_id):
    """→ (документ|None, причина). None = такого платежа в МС ещё нет."""
    if sync_id in idx["sync"]:
        return None, "ours"                       # наш же документ, PUT его обновит
    s = _kop(op.get("amount"))
    num = str(op.get("document_number") or "").strip()
    if num and (s, num) in idx["by_num"]:
        return idx["by_num"][(s, num)][0], "номер+сумма"
    same_sum = idx["by_sum"].get(s) or []
    if same_sum:
        # Номер не сошёлся, но сумма за тот же день уже есть. Осознанно считаем дублем:
        # пропущенный платёж виден при сверке, задвоенный — портит учёт молча.
        return same_sum[0], "сумма+день"
    return None, ""


def get_opt(path):
    """GET, который на 404 отдаёт None вместо исключения (проверка «есть ли объект»)."""
    try:
        return get(path)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def resolve_org(inn):
    rows = get("/entity/organization")["rows"]
    for o in rows:
        if o.get("inn") == inn:
            return o
    raise SystemExit(f"организация с ИНН {inn} не найдена в МС")


_expense_cache = {}


def resolve_expense_item(want):
    """Статья расходов для paymentout — в этом аккаунте МС поле ОБЯЗАТЕЛЬНО (POST иначе 412/3000).
    Имя задаёт обёртка банка (у Альфы — «Закупка товаров»: так проставлены 100 из 100 исходящих
    платежей июля-2026, заведённых руками, включая банковские комиссии и эквайринг, т.е. это
    фактическая учётная практика владельца, а не наше допущение)."""
    if want in _expense_cache:
        return _expense_cache[want]
    for r in get("/entity/expenseitem?limit=100")["rows"]:
        if _norm(r.get("name")) == _norm(want):
            _expense_cache[want] = r
            return r
    raise SystemExit(f"статья расходов «{want}» не найдена в МС")


def terms_card(inn, org_inn=None):
    """→ (ms_agent_id, name) карточки поставщика из условий оплаты, либо (None, None).

    Условия оплаты (`supplier_payment_terms`, разрез по нашему юрлицу — миграция 207) — это
    и есть наш реестр «какой карточкой ведём этого поставщика»: на неё же смотрит гейт
    `alfa_link.supplier_agents()` при привязке платежей к приёмкам."""
    q = "SELECT ms_agent_id, name FROM supplier_payment_terms WHERE inn=%s AND coalesce(ms_agent_id,'') <> ''"
    args = [inn]
    if org_inn:
        q += " AND org_inn=%s"
        args.append(org_inn)
    rows = db.query(q + " ORDER BY updated_at DESC", tuple(args))
    if not rows and org_inn:                              # у этого юрлица условий нет — берём общие
        return terms_card(inn, None)
    return (rows[0]["ms_agent_id"], rows[0]["name"]) if rows else (None, None)


def resolve_agent(inn, name, apply, org_inn=None):
    """→ (agent_dict|None, статус). Матч по ИНН → по имени → создание (в apply).

    ИНН карточку НЕ определяет: у одного юрлица в МС бывает несколько карточек. У «Солюшнс принт»
    (ИНН 7806486149) их две — питерская `ООО "Солюшнс принт"` и московская `ООО "Солюшнс принт" МСК`,
    и товар идёт по МСК (правило Сергея 13.08.2026: платежи Солюшнс всегда на МСК и привязывать
    к её приёмкам). МС отдаёт карточки в порядке создания, поэтому прежний `rows[0]` брал СТАРУЮ:
    платёж садился на карточку без приёмок, а гейт `supplier_agents()` не находил её в условиях
    оплаты и вообще не пытался привязать — отсюда «платёж есть, приёмки висят неоплаченными».

    Выбираем так же, как черновики платёжек (`invoice_bot/payment_draft.payee_card`): карточка
    из условий оплаты НАШЕГО юрлица. Одна карточка на ИНН — вопроса нет; несколько и в условиях
    ни одной — берём по имени из выписки, а если и оно не совпало, честно возвращаем `ambiguous`,
    а не «первую попавшуюся»: платёж не туда дороже разобрать, чем не завести."""
    if inn:
        rows = get(f"/entity/counterparty?filter=inn={inn}")["rows"]
        if len(rows) == 1:
            return rows[0], "inn"
        if rows:
            want_id, want_name = terms_card(inn, org_inn)
            cp = next((r for r in rows if r["id"] == want_id), None)
            if cp:
                return cp, "inn+условия"
            cp = next((r for r in rows if _norm(r.get("name")) == _norm(want_name or "")), None)
            if cp:
                return cp, "inn+условия(имя)"
            cp = next((r for r in rows if _norm(r.get("name")) == _norm(name or "")), None)
            if cp:
                return cp, "inn+имя из выписки"
            return None, f"ambiguous: {len(rows)} карточек у ИНН {inn}, нет в supplier_payment_terms"
    if name:
        q = urllib.parse.quote(name)
        for r in get(f"/entity/counterparty?search={q}&limit=5")["rows"]:
            if _norm(r["name"]) == _norm(name):
                return r, "name"
    # не нашли
    if not apply:
        return None, "would-create"
    body = {"name": name or "Без наименования"}
    if inn:
        body["inn"] = inn
    st, resp = post("/entity/counterparty", body)
    if st not in (200, 201):
        return None, f"create-fail:{st}"
    return resp, "created"


def build_payment(op, org, agent, expense_item):
    typ = "paymentin" if op["direction"] == "CREDIT" else "paymentout"
    body = {
        "organization": _meta("organization", org["id"]),
        "agent": _meta("counterparty", agent["id"], "counterparty"),
        "sum": _kop(op.get("amount")),                       # МС хранит в копейках
        "moment": _ms_dt(op["operation_date"]),
        "paymentPurpose": op["purpose"] or "",
        "syncId": _sync_id(op),
    }
    if typ == "paymentin":                                  # у paymentout нет incoming*
        if op.get("document_number"):
            body["incomingNumber"] = str(op["document_number"])
        idate = _ms_dt(op.get("document_date") or op["operation_date"])
        if idate:
            body["incomingDate"] = idate
    else:                                                   # paymentout: статья расходов обязательна
        item = resolve_expense_item(expense_item)
        body["expenseItem"] = _meta("expenseitem", item["id"], "expenseitem")
    return typ, body


def link_supplies(payment, stats):
    """Привязать созданный исходящий платёж к приёмкам (движок — `collectors/alfa_link`).

    Имя движка историческое (родился в контуре Альфы), но сам он банконезависим: разрез идёт
    по НАШЕМУ юрлицу платежа (`alfa_link.org_inn`, миграция 207), а приёмки, черновики и
    условия оплаты берутся внутри этого же юрлица. Поэтому колбэк общий для обоих контуров.

    Привязка — вторичный шаг: её сбой НЕ должен ронять уже записанный платёж, поэтому любое
    исключение уходит в счётчик и лог, а не наружу."""
    try:
        from alfa_link import link_payment                # сосед по каталогу
        status, note = link_payment(payment, apply=True)
    except Exception as e:                                # noqa: BLE001 — см. докстринг
        stats["link_errors"] += 1
        stats["link_msgs"].append(f"платёж №{payment.get('name')}: {type(e).__name__}: {e}")
        return
    if status == "linked":
        stats["linked"] += 1
    elif status in ("partial", "error"):
        stats["link_errors"] += 1
        stats["link_msgs"].append(
            f"платёж №{payment.get('name')} {payment.get('sum', 0)/100:.2f} ₽ → {note}")
    # no-match (комиссии банка, авансы без номеров в назначении) — штатно, молча


def link_orders(payment, stats):
    """Привязать созданный ВХОДЯЩИЙ платёж к заказам покупателей (движок — `bank_in_link`).

    Зеркало `link_supplies` для денег покупателей: мост «номер счёта в назначении → счёт
    покупателю → его заказ». Сбой привязки так же не роняет уже записанный платёж."""
    try:
        from bank_in_link import link_payment                # сосед по каталогу
        status, note = link_payment(payment, apply=True)
    except Exception as e:                                # noqa: BLE001 — см. link_supplies
        stats["link_in_errors"] += 1
        stats["link_msgs"].append(f"приход №{payment.get('name')}: {type(e).__name__}: {e}")
        return
    if status == "linked":
        stats["linked_in"] += 1
    elif status in ("partial", "error"):
        stats["link_in_errors"] += 1
        stats["link_msgs"].append(
            f"приход №{payment.get('name')} {payment.get('sum', 0)/100:.2f} ₽ → {note}")
    # no-match (маркетплейсы, переводы между своими счетами, эквайринг) — штатно, молча


def sync(normalized, apply=False, org_inn=None, expense_item="Закупка товаров",
         since=None, link_fn=None, link_in_fn=None):
    """Нормализованные операции выписки → МойСклад.

    org_inn      — ИНН организации-владельца счёта (её же ставим фильтром антидубля);
    expense_item — имя статьи расходов для paymentout;
    since        — дата отсечки YYYY-MM-DD: операции раньше неё не пишем (до неё документы
                   заводились руками);
    link_fn      — необязательный колбэк (payment, stats) для привязки исходящего платежа
                   к приёмкам; штатное значение — `link_supplies` (см. выше), None выключает
                   привязку для контура;
    link_in_fn   — то же для входящего платежа (штатно `link_orders`): деньги покупателя →
                   его заказ.
    """
    org = resolve_org(org_inn)
    stats = {"paymentin": 0, "paymentout": 0, "matched": 0, "created": 0,
             "would_create": 0, "errors": 0, "existing": 0, "before_cutoff": 0, "ignored": 0,
             "error_msgs": [],         # тексты ошибок МС — иначе крон-лог немой (был «ошибок 3»)
             "linked": 0, "link_errors": 0, "link_msgs": [],   # привязка платежей к приёмкам
             "linked_in": 0, "link_in_errors": 0}              # …и приходов к заказам покупателей
    plan = []
    idx_cache = {}
    for op in normalized:
        why_skip = skip_reason(op)
        if why_skip:
            stats["ignored"] += 1
            plan.append({"dir": op["direction"], "sum": op["amount"],
                         "typ": "paymentin" if op["direction"] == "CREDIT" else "paymentout",
                         "agent": op["counterparty_name"], "agent_status": f"игнор: {why_skip}",
                         "written": False, "syncId": _sync_id(op)})
            continue

        typ = "paymentin" if op["direction"] == "CREDIT" else "paymentout"
        stats[typ] += 1                                    # намеченный тип всегда
        day = (op.get("operation_date") or "")[:10]
        sid = _sync_id(op)

        if since and day and day < since:
            stats["before_cutoff"] += 1
            plan.append({"dir": op["direction"], "sum": op["amount"], "typ": typ,
                         "agent": op["counterparty_name"], "agent_status": "до отсечки",
                         "written": False, "syncId": sid})
            continue

        if day:                                            # антидубль с ручной загрузкой
            key = (typ, day)
            if key not in idx_cache:
                idx_cache[key] = existing_index(typ, day, org_id=org["id"])
            dup, why = find_existing(idx_cache[key], op, sid)
            if dup is not None:
                stats["existing"] += 1
                plan.append({"dir": op["direction"], "sum": op["amount"], "typ": typ,
                             "agent": op["counterparty_name"],
                             "agent_status": f"уже есть ({why})", "written": False,
                             "syncId": sid})
                continue

        agent, ast = resolve_agent(op["counterparty_inn"], op["counterparty_name"], apply, org_inn)
        if agent is None:
            # без агента платёж в МС не создать — фиксируем в плане, но не пишем
            stats["would_create" if ast == "would-create" else "errors"] += 1
            plan.append({"dir": op["direction"], "sum": op["amount"],
                         "agent": op["counterparty_name"], "agent_status": ast,
                         "typ": typ, "written": False, "syncId": sid})
            continue
        stats["created" if ast == "created" else "matched"] += 1
        _, body = build_payment(op, org, agent, expense_item)
        written = False
        if apply:
            # Идемпотентность: PUT /entity/{typ}/syncid/{uuid} НЕ создаёт объект (404, code 1021 —
            # проверено на боевом прогоне 2026-07-28), это только обновление существующего.
            # Поэтому: есть по syncId → пропускаем, нет → создаём POST'ом.
            if get_opt(f"/entity/{typ}/syncid/{body['syncId']}") is not None:
                stats["existing"] += 1
                plan.append({"dir": op["direction"], "sum": op["amount"], "typ": typ,
                             "agent": agent["name"], "agent_status": "уже есть (syncId)",
                             "written": False, "syncId": body["syncId"]})
                continue
            st, resp = post(f"/entity/{typ}", body)
            if st in (200, 201):
                written = True
                if typ == "paymentout" and link_fn:
                    link_fn(resp, stats)                 # замкнуть контур: платёж → приёмка
                elif typ == "paymentin" and link_in_fn:
                    link_in_fn(resp, stats)              # …и обратный: приход → заказ покупателя
            else:
                stats["errors"] += 1
                stats["error_msgs"].append(
                    f"{typ} {op['amount']} {(op.get('counterparty_name') or '')[:24]}: "
                    f"HTTP {st} — {_err_text(resp)}")
        plan.append({"dir": op["direction"], "sum": op["amount"],
                     "agent": agent["name"], "agent_status": ast, "typ": typ,
                     "written": written, "syncId": body["syncId"]})
    return stats, plan


def print_plan(plan, limit=20):
    """Короткая печать плана (правило 9: в чат — не больше полусотни строк)."""
    print("--- план (напр, сумма, контрагент, тип, агент-статус, записан) ---")
    for p in plan[:limit]:
        print(f"{p['dir']:6} {str(p['sum']):>10} {(p['agent'] or '—')[:22]:22} "
              f"{p['typ']:11} {p['agent_status']:22} {'да' if p['written'] else '—'}")
    if len(plan) > limit:
        print(f"... ещё {len(plan) - limit} операций")


def print_stats(stats):
    print(f"paymentin(приход) {stats['paymentin']}  paymentout(расход) {stats['paymentout']}  "
          f"уже в МС {stats['existing']}  до отсечки {stats['before_cutoff']}  "
          f"игнор {stats['ignored']}  агент: matched {stats['matched']} / "
          f"created {stats['created']} / would-create {stats['would_create']}  "
          f"ошибок {stats['errors']}")
    for m in (stats.get("error_msgs") or [])[:5]:
        print(f"  ✗ {m}")
