# поток: inv
"""collectors/alfa_link.py — привязка исходящих платежей выписки к ПРИЁМКАМ МойСклада.

Замыкает контур закупки: счёт → Заказ поставщику → УПД → Приёмка → черновик платёжки →
оплата в банке → выписка → Исходящий платёж → **связь платежа с Приёмкой** (этот модуль).
Итог: по каждой поставке видно Заказ (сумма X) = Приёмка (X) = Оплата (X).

Как в МС: связь живёт в `paymentout.operations` — массив
`{meta: .../entity/supply/{id}, linkedSum: <копейки>}`, пишется `PUT /entity/paymentout/{id}`.
После привязки `supply.payedSum` растёт до `supply.sum` (приёмка закрыта). Ровно так заведены
ручные платежи владельца (61 из 100 июльских исходящих; 215 ссылок на supply).

Мост «платёж → документы» — номера из НАЗНАЧЕНИЯ платежа (`paymentPurpose` выписки):
  • предоплата по счёту (Феррет): «...по СЧЕТ-ДОГОВОР 6307 от 27.07.2026» → приёмка №6307;
  • пачка по отсрочке (КВК ТРЕЙД): «Оплата по KV00009220, 9200, 9267…» → номера ЗАКАЗОВ,
    берём приёмки этих заказов. Первый номер полный, остальные сокращены — разворачиваем
    по образцу первого (KV0000|9220 → 9200 ⇒ KV00009200).
Поставщик не хардкодится: сначала ищем приёмку с таким номером, затем заказ с таким номером,
и всё это ТОЛЬКО у контрагента самого платежа — чужой документ с похожим номером не подхватится.

ГЛАВНЫЙ ПРЕДОХРАНИТЕЛЬ: привязываем, только если сумма платежа раскладывается по найденным
приёмкам ПОЛНОСТЬЮ (остаток 0). Частичная привязка молча исказит учёт — такой платёж честно
остаётся непривязанным и попадает в лог на ручной разбор.

Запуск отдельно (добор ранее созданных платежей):
    ./venv/bin/python collectors/alfa_link.py 2026-07-01            # dry-run с даты
    ./venv/bin/python collectors/alfa_link.py 2026-07-01 --apply    # записать
"""
import re
import sys
import time
import pathlib
import urllib.error
import urllib.parse

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, "/opt/mp-analytics/invoice_bot")
from ms import get as _ms_get, put, MS                   # noqa: E402

# токен-кандидат: необязательный буквенный префикс + разделитель + 3..12 цифр (KV00009220, 6307)
_TOKEN = re.compile(r"\b([A-Za-zА-Яа-я]{0,6})([- ]?)(\d{3,12})\b")
_DATE = re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{2,4}\b")     # 27.07.2026 — не номер документа
_MONEY = re.compile(r"\b\d[\d\s]*[.,]\d{2}\b")           # 3998.31 — сумма, не номер
_CYR = re.compile(r"[А-Яа-я]")
MAX_TOKENS = 20                                          # предохранитель от мусорных назначений


def get(path, tries=5):
    """GET с бэкоффом: МС лимитирует частоту (429), а мы бьём по номеру в цикле."""
    for i in range(tries):
        try:
            return _ms_get(path)
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < tries - 1:
                time.sleep(2 * (i + 1))
                continue
            raise


def purpose_tokens(purpose):
    """Номера документов из назначения платежа. Хвост «в том числе НДС…» отбрасываем —
    там проценты и суммы, которые иначе читаются как номера."""
    s = (purpose or "")
    cut = re.split(r"(?i)\bв\s+том\s+числе\b", s)[0]
    cut = _MONEY.sub(" ", _DATE.sub(" ", cut))
    out, pattern = [], None
    for pref, sep, digits in _TOKEN.findall(cut):
        pref = pref.strip()
        if not pref and len(digits) == 4 and digits.startswith(("19", "20")):
            continue                                     # похоже на год
        if pref and not sep and _CYR.search(pref):
            continue                                     # «карте220015», «счет1234» — слово+число,
            #                                              номера документов так не пишут
        sep = sep if pref else ""                        # разделитель значим только при префиксе
        if pref and pattern is None:
            pattern = (pref, sep, len(digits))           # образец полного номера (KV, '', 8)
        out.append((pref, sep, digits))
    tokens = []
    for pref, sep, digits in out:
        tokens.append(f"{pref}{sep}{digits}")            # «СП-1234» — дефис часть номера
        if not pref and pattern and len(digits) < pattern[2]:
            # сокращённый номер в перечислении → разворачиваем по образцу первого
            tokens.append(f"{pattern[0]}{pattern[1]}{digits.zfill(pattern[2])}")
    seen, uniq = set(), []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq[:MAX_TOKENS]


def _agent_href(payment):
    return ((payment.get("agent") or {}).get("meta") or {}).get("href")


def _find(entity, name, agent_href):
    """Документ с таким номером у ЭТОГО контрагента (иначе можно схватить чужой)."""
    flt = urllib.parse.quote(f"name={name};agent={agent_href}")
    rows = get(f"/entity/{entity}?filter={flt}&limit=5").get("rows", [])
    return rows


def _full_supply(s):
    """У заказа приёмки приходят кратко — догружаем документ ради sum/payedSum."""
    if "sum" in s and "payedSum" in s:
        return s
    href = (s.get("meta") or {}).get("href")
    return get(href.replace(MS, "")) if href else None


def resolve_supplies(payment):
    """→ (список приёмок, список нерезолвленных номеров). Номер = приёмка, либо заказ→его приёмки."""
    agent_href = _agent_href(payment)
    if not agent_href:
        return [], []
    found, missed, seen = [], [], set()
    for tok in purpose_tokens(payment.get("paymentPurpose")):
        rows = _find("supply", tok, agent_href)
        if not rows:
            for po in _find("purchaseorder", tok, agent_href):
                po_full = get(f"/entity/purchaseorder/{po['id']}?expand=supplies")
                rows.extend(po_full.get("supplies") or [])
        if not rows:
            missed.append(tok)
            continue
        for r in rows:
            full = _full_supply(r)
            if full and full.get("id") and full["id"] not in seen:
                seen.add(full["id"])
                found.append(full)
    return found, missed


def distribute(total, supplies, paid_override=None):
    """Разложить сумму платежа по приёмкам: каждой — её неоплаченный остаток, пока деньги есть.
    → (operations, нераспределённый остаток). `paid_override` {supply_id: payedSum} нужен тестам,
    чтобы посчитать распределение «как если бы платёж ещё не был привязан»."""
    ops, left = [], total
    for s in supplies:
        paid = (paid_override or {}).get(s["id"], s.get("payedSum") or 0)
        unpaid = (s.get("sum") or 0) - paid
        take = min(unpaid, left)
        if take <= 0:
            continue
        ops.append({"meta": {"href": f"{MS}/entity/supply/{s['id']}", "type": "supply",
                             "mediaType": "application/json"},
                    "linkedSum": take})
        left -= take
    return ops, left


def plan(payment):
    """→ (operations, остаток_копеек, пояснение). Остаток ≠ 0 ⇒ привязывать НЕЛЬЗЯ."""
    supplies, missed = resolve_supplies(payment)
    if not supplies:
        return [], payment["sum"], f"документы не найдены (номера: {missed or '—'})"
    ops, left = distribute(payment["sum"], supplies)
    note = f"приёмок {len(ops)}"
    if missed:
        note += f", не найдено по номерам: {', '.join(missed)}"
    return ops, left, note


def link_payment(payment, apply=False):
    """→ (статус, пояснение). Статусы: linked / would-link / already / no-match / partial / error."""
    if payment.get("operations"):
        return "already", "уже привязан"
    ops, left, note = plan(payment)
    if not ops:
        return "no-match", note
    if left != 0:
        # платёж не раскладывается по приёмкам без остатка — учёт важнее «хоть как-то привязать»
        return "partial", f"{note}; не распределено {left/100:.2f} ₽ — нужен ручной разбор"
    if not apply:
        return "would-link", note
    st, resp = put(f"/entity/paymentout/{payment['id']}", {"operations": ops})
    if st in (200, 201):
        return "linked", note
    return "error", f"HTTP {st}: {str(resp)[:200]}"


def link_new(payments, apply=False):
    """Привязка пачки платежей (вызывается из alfa_ms.sync). → (stats, строки лога)."""
    stats, lines = {"linked": 0, "would_link": 0, "already": 0,
                    "no_match": 0, "partial": 0, "errors": 0}, []
    for p in payments:
        status, note = link_payment(p, apply=apply)
        key = {"linked": "linked", "would-link": "would_link", "already": "already",
               "no-match": "no_match", "partial": "partial", "error": "errors"}[status]
        stats[key] += 1
        if status in ("partial", "error"):
            lines.append(f"платёж №{p.get('name')} {p['sum']/100:.2f} ₽: {note}")
    return stats, lines


def main(argv):
    since = next((a for a in argv if not a.startswith("--")), None)
    apply = "--apply" in argv
    if not since:
        raise SystemExit("укажи дату начала: alfa_link.py 2026-07-01 [--apply]")
    flt = urllib.parse.quote(f"moment>={since} 00:00:00")
    rows = get(f"/entity/paymentout?filter={flt}&expand=agent,operations&limit=100").get("rows", [])
    print(f"исходящих платежей с {since}: {len(rows)} — {'ЗАПИСЬ' if apply else 'DRY-RUN'}")
    for p in rows:
        status, note = link_payment(p, apply=apply)
        if status == "already":
            continue
        mark = {"linked": "✓", "would-link": "•", "partial": "⚠", "error": "✗", "no-match": "·"}[status]
        print(f"  {mark} №{p.get('name'):>8} {p['sum']/100:>11.2f} ₽ "
              f"{((p.get('agent') or {}).get('name') or '')[:26]:26} {status}: {note}")


if __name__ == "__main__":
    main(sys.argv[1:])
