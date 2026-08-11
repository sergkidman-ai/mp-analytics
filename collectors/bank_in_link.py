# поток: inv
"""collectors/bank_in_link.py — привязка ВХОДЯЩИХ платежей выписки к ЗАКАЗАМ ПОКУПАТЕЛЕЙ.

Зеркало `alfa_link` для денег, которые приходят нам: счёт покупателю → оплата в банке →
выписка → Входящий платёж → **связь платежа с Заказом покупателя** (этот модуль).
Итог: по каждой продаже видно Счёт (сумма X) = Заказ (X) = Оплата (X), и «График оплаты»
по покупателям перестаёт врать.

Как в МС: связь живёт в `paymentin.operations` — массив
`{meta: .../entity/customerorder/{id}, linkedSum: <копейки>}`, пишется `PUT /entity/paymentin/{id}`.
Ровно так владелец заводит их руками: замер 11.08.2026 — 17 привязанных приходов с 01.07,
все на `customerorder` (контроль: платёж 56850 «ДОРОГА ЗАКОНА» 12 546 ₽ → счёт 56845 → заказ 56845).

Мост «платёж → документ» — НОМЕР СЧЁТА из назначения платежа, и только он:
    «Оплата по счёту № 56845 от 04.08.2026 г. за картриджи…» → `invoiceout` 56845 → его заказ;
    «ОПЛАТА ПО СЧ.№ 00064 ОТ 16.07.2026Г…»                   → `invoiceout` 00064 → его заказ.
Номер ищется ТОЛЬКО рядом со словом «счёт/сч.» — в назначении хватает других чисел (договор,
дата, сумма, НДС), и свободный поиск цифр ловил бы их. Номер длиннее 6 знаков не берём:
это уже расчётный счёт («по счету 40702810…» в комиссиях банка), а не документ.

Сверка перед привязкой — четыре ключа (постановка Сергея 11.08.2026): ИНН плательщика =
ИНН контрагента счёта, наше юрлицо платежа = организация счёта, номер счёта, сумма.

ГЛАВНЫЙ ПРЕДОХРАНИТЕЛЬ (как у исходящих): привязываем, только если сумма платежа
раскладывается по найденным документам ПОЛНОСТЬЮ (остаток 0). Частичная привязка молча
исказит учёт — такой платёж честно остаётся непривязанным и попадает в лог на ручной разбор.

ДОБОРА НЕТ. У исходящих он нужен: предоплата уходит раньше поставки. У входящих порядок
обратный и жёсткий (решение Сергея 11.08.2026): сначала счёт — потом оплата. Значит, документ
существует уже в момент записи платежа, и второго захода не требуется. Разовый разбор задним
числом делается руками через CLI (см. ниже).

Фолбэк «без номера» (ровно один неоплаченный счёт этого контрагента на точную сумму) по
умолчанию ВЫКЛЮЧЕН: на замере июнь–август это 1 случай из 23, а цена ошибки — деньги на чужом
заказе. Включается `BANK_IN_LINK_BY_AMOUNT=1`.

ПРОВЕРЕНО НА ЖИВЫХ ДАННЫХ (11.08.2026): 99 входящих с 01.06.2026, из них 30 привязано владельцем
руками. У 18 из этих 30 в назначении есть номер счёта — движок нашёл ТОТ ЖЕ документ во всех 18.
Остальные 12 — розница без номера (движок их не трогает), 69 непривязанных — маркетплейсы,
эквайринг и переводы между своими счетами, там номера нет вовсе. Ложных привязок 0.
Именно эта сверка вскрыла, что номер документа в МС НЕ уникален во времени (см. `_pick`).

Запуск отдельно (разовый разбор ранее созданных приходов):
    ./venv/bin/python collectors/bank_in_link.py 2026-08-01            # dry-run с даты
    ./venv/bin/python collectors/bank_in_link.py 2026-08-01 --apply    # записать
    ./venv/bin/python collectors/bank_in_link.py 2026-08-01 --all      # + отсеянные строки
"""
import os
import re
import sys
import pathlib
import urllib.parse

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, "/opt/mp-analytics/invoice_bot")
from ms import get as _get, put, MS                       # noqa: E402

MAX_TOKENS = 10                                           # номеров из одного назначения
BY_AMOUNT = os.getenv("BANK_IN_LINK_BY_AMOUNT") == "1"    # фолбэк «одна точная сумма»

# Номер счёта: только рядом со словом «счёт/сч.», 3–6 цифр, и после цифр не идёт ещё цифра
# (иначе «по счету 40702810…» даёт огрызок «407028» — так ловились комиссии Сбера).
_INV_NUM = re.compile(r"(?:сч[её]т\w*|сч\.)\s*(?:№|N|#)?\s*(\d{3,6})(?!\d)", re.I)
# Перечисление после первого номера: «по счетам № 123, 124 и 125».
_TAIL_NUM = re.compile(r"[,и]\s*(?:№\s*)?(\d{3,6})(?!\d)", re.I)


def get(path, tries=5):
    """GET с бэкоффом: МС лимитирует частоту (429), а мы бьём по номеру в цикле."""
    import time
    import urllib.error
    for i in range(tries):
        try:
            return _get(path)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or i == tries - 1:
                raise
            time.sleep(2 ** i)
    return {}


def invoice_numbers(purpose):
    """Номера счетов из назначения платежа. → список строк без ведущих нулей не трогаем:
    в МС номера так и живут («00064»), а варианты дополнения перебираем при поиске."""
    s = purpose or ""
    out = []
    for m in _INV_NUM.finditer(s):
        out.append(m.group(1))
        tail = s[m.end():m.end() + 40]                     # хвост перечисления сразу за номером
        out += _TAIL_NUM.findall(tail)
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq[:MAX_TOKENS]


def _href_id(href):
    return (href or "").rsplit("/", 1)[-1].split("?")[0]


def _agent_href(payment):
    return ((payment.get("agent") or {}).get("meta") or {}).get("href")


def _org_href(payment):
    """Юрлицо платежа. Фильтр по организации обязателен: номера документов у Цифрового
    Квадрата и Дисквэра пересекаются, без него можно привязать деньги к чужому заказу."""
    return ((payment.get("organization") or {}).get("meta") or {}).get("href")


def _find(entity, name, agent_href, org_href):
    """Документ с таким номером у ЭТОГО контрагента И ЭТОГО юрлица."""
    flt = urllib.parse.quote(f"name={name};agent={agent_href};organization={org_href}")
    return get(f"/entity/{entity}?filter={flt}&limit=10&expand=customerOrder").get("rows", [])


def _pick(rows, payment):
    """Номер документа в МС не уникален ВО ВРЕМЕНИ: у одного покупателя счёт «00058» есть и
    от 2023, и от 2026 (реальный случай — платёж 56821 от 29.06.2026, деньги идут за свежий).
    Счёт всегда раньше оплаты, поэтому из одноимённых берём самый свежий, но не позже платежа;
    при прочих равных — тот, где остаток к оплате ещё не погашен."""
    day = (payment.get("moment") or "9999")[:10]          # ДЕНЬ, не время: у платежа из выписки
    fit = [r for r in rows                                # время 00:00, а счёт того же дня в 10:05
           if (r.get("moment") or "")[:10] <= day] or rows
    return sorted(fit, key=lambda r: (((r.get("sum") or 0) - (r.get("payedSum") or 0)) > 0,
                                      r.get("moment") or ""), reverse=True)[0]


def _name_variants(num):
    """«56845» → сам номер; «53» → ещё и «00053»/«0053». В МС имя документа — строка,
    фильтр `name=` точный, а покупатель в назначении пишет номер как ему удобно."""
    v = [num]
    for w in (4, 5, 6):
        if len(num) < w:
            v.append(num.zfill(w))
    bare = num.lstrip("0")
    if bare and bare != num:
        v.append(bare)
    return list(dict.fromkeys(v))


def _order_of(invoice):
    """Заказ покупателя, из которого выставлен счёт. Нет заказа — привязываем к самому счёту:
    `paymentin.operations` принимает и `invoiceout`, и счёт без заказа в МС бывает."""
    co = invoice.get("customerOrder")
    if co:
        return {"id": _href_id((co.get("meta") or {}).get("href")), "type": "customerorder",
                "name": co.get("name"), "sum": co.get("sum") or 0,
                "payedSum": co.get("payedSum") or 0}
    return {"id": invoice["id"], "type": "invoiceout", "name": invoice.get("name"),
            "sum": invoice.get("sum") or 0, "payedSum": invoice.get("payedSum") or 0}


def resolve_docs(payment):
    """→ (документы к оплате, нерезолвленные номера). Номер = счёт покупателю, из него заказ.
    Если номер сам по себе — номер заказа (новая нумерация 568xx: счёт и заказ одноимённые),
    берём заказ напрямую."""
    ah, oh = _agent_href(payment), _org_href(payment)
    if not (ah and oh):
        return [], []
    docs, missing, seen = [], [], set()
    for num in invoice_numbers(payment.get("paymentPurpose")):
        found = None
        for name in _name_variants(num):
            rows = _find("invoiceout", name, ah, oh)
            if rows:
                found = _order_of(_pick(rows, payment))
                break
            rows = _find("customerorder", name, ah, oh)
            if rows:
                d = _pick(rows, payment)
                found = {"id": d["id"], "type": "customerorder", "name": d.get("name"),
                         "sum": d.get("sum") or 0, "payedSum": d.get("payedSum") or 0}
                break
        if not found:
            missing.append(num)
        elif found["id"] not in seen:
            seen.add(found["id"])
            docs.append(found)
    return docs, missing


def by_amount(payment):
    """Фолбэк: ровно один НЕОПЛАЧЕННЫЙ счёт этого контрагента на точную сумму платежа.
    По умолчанию выключен (`BANK_IN_LINK_BY_AMOUNT`), см. шапку модуля."""
    ah, oh = _agent_href(payment), _org_href(payment)
    if not (ah and oh):
        return []
    flt = urllib.parse.quote(f"agent={ah};organization={oh}")
    rows = get(f"/entity/invoiceout?filter={flt}&limit=100&expand=customerOrder").get("rows", [])
    total = payment.get("sum") or 0
    hits = [_order_of(r) for r in rows if (r.get("sum") or 0) == total]
    hits = [d for d in hits if d["payedSum"] < d["sum"]]
    return hits if len(hits) == 1 else []


def distribute(total, docs):
    """Разложить сумму платежа по документам: каждому — его неоплаченный остаток, пока деньги
    есть. → (operations, нераспределённый остаток)."""
    ops, left = [], total
    for d in docs:
        unpaid = (d.get("sum") or 0) - (d.get("payedSum") or 0)
        take = min(unpaid, left)
        if take <= 0:
            continue
        ops.append({"meta": {"href": f"{MS}/entity/{d['type']}/{d['id']}", "type": d["type"],
                             "mediaType": "application/json"},
                    "linkedSum": take})
        left -= take
    return ops, left


def _ops_map(operations):
    out = {}
    for o in operations or []:
        oid = _href_id((o.get("meta") or {}).get("href"))
        out[oid] = out.get(oid, 0) + (o.get("linkedSum") or 0)
    return out


def plan(payment):
    """→ (operations, остаток_копеек, пояснение). Остаток ≠ 0 ⇒ привязывать НЕЛЬЗЯ."""
    docs, missing = resolve_docs(payment)
    src = "по номеру счёта"
    if not docs and BY_AMOUNT:
        docs, src = by_amount(payment), "по точной сумме (номера в назначении нет)"
    if not docs:
        why = f"счёт(а) {', '.join(missing)} у этого покупателя не найдены" if missing \
            else "в назначении нет номера счёта"
        return [], payment.get("sum") or 0, why
    ops, left = distribute(payment.get("sum") or 0, docs)
    names = ", ".join(f"{d['type'][:1].upper()}{d['name']}" for d in docs)
    note = f"{src}: {names}"
    if missing:
        note += f"; не найдены: {', '.join(missing)}"
    if left:
        note += f"; НЕ РАЗЛОЖЕНО {left / 100:.2f} ₽"
    return ops, left, note


def link_payment(payment, apply=False):
    """→ (статус, пояснение). Статусы: linked / would-link / already / no-match / partial / error."""
    if not _org_href(payment):
        return "no-match", "у платежа не указано наше юрлицо"
    if payment.get("operations"):
        return "already", "уже привязан"
    ops, left, note = plan(payment)
    if not ops:
        return "no-match", note
    if left:
        return "partial", note                            # предохранитель: частичную не пишем
    if not apply:
        return "would-link", note
    st, resp = put(f"/entity/paymentin/{payment['id']}", {"operations": ops})
    return ("linked", note) if st in (200, 201) else ("error", f"HTTP {st}: {str(resp)[:200]}")


def payments_since(since, limit=100):
    """Входящие платежи с даты `since`, постранично."""
    out, off = [], 0
    flt = urllib.parse.quote(f"moment>={since} 00:00:00", safe="=;:")
    while True:
        page = get(f"/entity/paymentin?filter={flt}"
                   f"&expand=agent,organization,operations&limit={limit}&offset={off}")
        rows = page.get("rows", [])
        out.extend(rows)
        if len(rows) < limit:
            break
        off += limit
    return out


def link_new(payments, apply=False):
    """Разовый разбор пачки приходов. → (stats, строки на ручной разбор)."""
    stats = {"linked": 0, "would_link": 0, "already": 0, "no_match": 0,
             "partial": 0, "errors": 0}
    lines, quiet = [], []
    for p in payments:
        try:
            st, note = link_payment(p, apply=apply)
        except Exception as e:                            # noqa: BLE001
            st, note = "error", f"{type(e).__name__}: {e}"
        key = {"linked": "linked", "would-link": "would_link", "already": "already",
               "no-match": "no_match", "partial": "partial", "error": "errors"}[st]
        stats[key] += 1
        row = (f"{(p.get('moment') or '')[:10]} №{p.get('name')} "
               f"{(p.get('agent') or {}).get('name', '')[:28]} "
               f"{(p.get('sum') or 0) / 100:.2f} ₽ → {st}: {note}")
        # в «громкие» идёт всё, что требует внимания: сделанное/предлагаемое и разбор руками
        (lines if st in ("linked", "would-link", "partial", "error") else quiet).append(row)
    return stats, lines, quiet


def main(argv):
    since = next((a for a in argv if not a.startswith("--")), None)
    if not since:
        print(__doc__.strip().splitlines()[-4].strip())
        return 2
    apply = "--apply" in argv
    rows = payments_since(since)
    stats, lines, quiet = link_new(rows, apply=apply)
    print(f"[{'APPLY' if apply else 'DRY-RUN'}] входящие с {since}: {len(rows)}")
    for l in (lines + quiet if "--all" in argv else lines)[:40]:
        print(" ", l)
    print(f"привязано {stats['linked']}, привязалось бы {stats['would_link']}, "
          f"уже {stats['already']}, без счёта {stats['no_match']}, "
          f"частично {stats['partial']}, ошибок {stats['errors']}")
    return 1 if stats["errors"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
