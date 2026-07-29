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
  • АВАНС (номеров в назначении нет вообще: «Авансовый платеж за картриджи») → приёмки этого
    поставщика по возрастанию даты, пока хватает денег (см. `plan_advance`).
Поставщик не хардкодится: сначала ищем приёмку с таким номером, затем заказ с таким номером,
и всё это ТОЛЬКО у контрагента самого платежа — чужой документ с похожим номером не подхватится.

ПРИВЯЗКА — НЕ ОДНОРАЗОВОЕ ДЕЙСТВИЕ. Предоплата уходит РАНЬШЕ поставки: платёж Феррету по счёту
6325 прошёл 28.07, а приёмка 6325 появилась 29.07 — в момент записи платежа привязывать было не
к чему («привязано к приёмкам 0» в крон-логе 29.07). Поэтому `run_inv.py` после разбора дня
делает ДОБОР по непривязанным платежам за последние `LINK_LOOKBACK_DAYS` дней.

ГЛАВНЫЙ ПРЕДОХРАНИТЕЛЬ: привязываем, только если сумма платежа раскладывается по найденным
приёмкам ПОЛНОСТЬЮ (остаток 0). Частичная привязка молча исказит учёт — такой платёж честно
остаётся непривязанным и попадает в лог на ручной разбор.

Запуск отдельно (добор ранее созданных платежей):
    ./venv/bin/python collectors/alfa_link.py 2026-07-01            # dry-run с даты
    ./venv/bin/python collectors/alfa_link.py 2026-07-01 --apply    # записать
"""
import os
import re
import sys
import time
import pathlib
import datetime as dt
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

_ADVANCE = re.compile(r"(?i)аванс")                      # «Авансовый платеж за картриджи»
ADVANCE_LOOKBACK = 45                                    # приёмки ДО аванса: хвосты прошлых поставок
# Разнос авансов пишет в документы, которые владелец ведёт руками, поэтому по умолчанию ВЫКЛЮЧЕН.
# Включение — `ALFA_LINK_ADVANCE=1` в .env, после того как dry-run показан и согласован.
ADVANCE_ON = os.getenv("ALFA_LINK_ADVANCE") == "1"


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


def is_advance(payment):
    """Аванс = деньги вперёд, без ссылки на конкретный документ («Авансовый платеж за картриджи»).
    Маркер берём ТОЛЬКО из слова в назначении: «нет номеров» само по себе маркером быть не может —
    так выглядят и зарплата, и налоги, и эквайринг, а их к приёмкам привязывать нельзя."""
    return bool(_ADVANCE.search(payment.get("paymentPurpose") or ""))


def agent_supplies(agent_href, since):
    """Все приёмки контрагента с даты `since` (включая появившиеся ПОЗЖЕ платежа — аванс
    закрывает будущие поставки), по возрастанию даты."""
    flt = urllib.parse.quote(f"agent={agent_href};moment>={since} 00:00:00", safe="=;:")
    out, off = [], 0
    while True:
        page = get(f"/entity/supply?filter={flt}&limit=100&offset={off}")
        rows = page.get("rows", [])
        out.extend(rows)
        if len(rows) < 100:
            break
        off += 100
    return sorted(out, key=lambda s: s.get("moment") or "")


def plan_advance(payment):
    """Раскладка АВАНСА: неоплаченные приёмки этого поставщика по возрастанию даты, пока хватает
    денег; хвостовая закрывается частично. Правило снято с ручной практики владельца и сверено
    на его 11 авансах (июнь–июль 2026): 8 совпали до копейки, 3 «разошлись» только тем, что наш
    расчёт добирал свежие приёмки, которые он ещё не успел разнести.

    Отличия от привязки по номеру:
      • ОСТАТОК АВАНСА — НОРМА, а не ошибка: деньги ждут будущих поставок (у аванса №498 на
        22.07 так и висело 73 817 ₽). Поэтому «остаток ≠ 0 ⇒ не привязывать» здесь не действует.
      • Привязка ДОБИРАЕТСЯ: на каждом прогоне к уже привязанным приёмкам добавляются новые,
        пока аванс не израсходован. Поэтому возвращаем ПОЛНЫЙ массив operations (старое+новое),
        слитый по приёмке: МС принимает `operations` целиком, а не дельтой.

    → (operations, неизрасходованный_остаток_копеек, пояснение). operations == [] ⇒ добавить нечего.

    ГРАБЛЯ ЧТЕНИЯ DRY-RUN: остатки приёмок читаются из МС на момент вызова, поэтому в dry-run два
    аванса ОДНОГО поставщика могут «забрать» одну и ту же неоплаченную приёмку — на бумаге выйдет
    двойной счёт. В `--apply` этого не происходит: платежи обрабатываются последовательно, и второй
    уже видит `payedSum`, обновлённый записью первого.
    """
    agent_href = _agent_href(payment)
    if not agent_href:
        return [], payment["sum"], "у платежа нет контрагента"
    pdate = (payment.get("moment") or "")[:10]
    since = (dt.date.fromisoformat(pdate) - dt.timedelta(days=ADVANCE_LOOKBACK)).isoformat()
    # уже привязанное этим же платежом: и остаток аванса, и база для слияния
    own = {}
    for o in payment.get("operations") or []:
        sid = ((o.get("meta") or {}).get("href") or "").rsplit("/", 1)[-1].split("?")[0]
        own[sid] = own.get(sid, 0) + (o.get("linkedSum") or 0)
    left = payment["sum"] - sum(own.values())
    if left <= 0:
        return [], 0, "аванс уже разнесён полностью"
    merged, added, taken = dict(own), 0, 0
    for s in agent_supplies(agent_href, since):
        # payedSum уже включает наш собственный вклад — берём только реально неоплаченный остаток
        unpaid = (s.get("sum") or 0) - (s.get("payedSum") or 0)
        take = min(unpaid, left)
        if take <= 0:
            continue
        merged[s["id"]] = merged.get(s["id"], 0) + take
        left -= take
        added += 1
        taken += take
        if left <= 0:
            break
    if not added:
        return [], left, f"новых неоплаченных приёмок нет; не разнесено {left/100:,.2f} ₽"
    ops = [{"meta": {"href": f"{MS}/entity/supply/{sid}", "type": "supply",
                     "mediaType": "application/json"}, "linkedSum": v}
           for sid, v in merged.items()]
    note = (f"аванс: +{added} приёмк(и) на {taken/100:,.2f} ₽" +
            (f", остаётся нераспределённым {left/100:,.2f} ₽" if left > 0 else ", аванс израсходован"))
    return ops, left, note


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


def _ops_map(operations):
    """operations → {id приёмки: сумма привязки}. Для сравнения «а не то же ли самое уже стоит»."""
    out = {}
    for o in operations or []:
        sid = ((o.get("meta") or {}).get("href") or "").rsplit("/", 1)[-1].split("?")[0]
        out[sid] = out.get(sid, 0) + (o.get("linkedSum") or 0)
    return out


def _write(payment, ops, note):
    st, resp = put(f"/entity/paymentout/{payment['id']}", {"operations": ops})
    return ("linked", note) if st in (200, 201) else ("error", f"HTTP {st}: {str(resp)[:200]}")


def link_payment(payment, apply=False):
    """→ (статус, пояснение). Статусы: linked / would-link / already / no-match / partial / error.

    Порядок разбора — сначала точный, потом приблизительный:
      1. **по номерам документов** в назначении: раскладка обязана сойтись БЕЗ остатка;
      2. **аванс** (слово «аванс» в назначении): раскладка частичная по определению и
         ДОБИРАЕТСЯ на последующих прогонах.

    Номер идёт первым нарочно: назначение может содержать И слово «аванс», И номер основания
    («Авансовый платеж по счету № …» — так пишут некоторые поставщики). Когда номер есть и
    раскладка по нему сошлась, привязка по документу точнее любого FIFO. Наши собственные платёжки
    сюда не попадают: предоплата по конкретному счёту слово «Аванс» не пишет (решение Сергея
    2026-07-29) — «Аванс» только у метода аванс/баланс, где основания нет.
    """
    advance = is_advance(payment)
    if payment.get("operations") and not advance:
        return "already", "уже привязан"        # дёшево: без похода в МС за приёмками

    ops, left, note = plan(payment)              # 1) точный путь — по номерам документов
    if ops and left == 0:
        if _ops_map(payment.get("operations")) == _ops_map(ops):
            return "already", "уже привязан по номеру документа"
        return ("would-link", note) if not apply else _write(payment, ops, note)

    if advance:                                  # 2) аванс — FIFO по неоплаченным приёмкам
        if not ADVANCE_ON:
            return "advance-off", "аванс: разнос выключен (ALFA_LINK_ADVANCE≠1)"
        a_ops, a_left, a_note = plan_advance(payment)
        if not a_ops:
            return "no-match", a_note
        return ("would-link", a_note) if not apply else _write(payment, a_ops, a_note)

    if not ops:
        return "no-match", note
    # платёж не раскладывается по приёмкам без остатка — учёт важнее «хоть как-то привязать»
    return "partial", f"{note}; не распределено {left/100:.2f} ₽ — нужен ручной разбор"


def link_new(payments, apply=False):
    """Привязка пачки платежей (вызывается из alfa_ms.sync). → (stats, строки лога)."""
    stats, lines = {"linked": 0, "would_link": 0, "already": 0, "no_match": 0,
                    "partial": 0, "errors": 0, "advance_off": 0}, []
    for p in payments:
        status, note = link_payment(p, apply=apply)
        key = {"linked": "linked", "would-link": "would_link", "already": "already",
               "no-match": "no_match", "partial": "partial", "error": "errors",
               "advance-off": "advance_off"}[status]
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
        mark = {"linked": "✓", "would-link": "•", "partial": "⚠", "error": "✗",
                "no-match": "·", "advance-off": "○"}[status]
        print(f"  {mark} №{p.get('name'):>8} {p['sum']/100:>11.2f} ₽ "
              f"{((p.get('agent') or {}).get('name') or '')[:26]:26} {status}: {note}")


if __name__ == "__main__":
    main(sys.argv[1:])
