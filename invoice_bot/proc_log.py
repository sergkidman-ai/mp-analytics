# -*- coding: utf-8 -*-
"""
proc_log.py — общий журнал обработки (для /report в боте): одна JSON-строка на файл в
processing_log.jsonl. Пишут mail_poller.py и tg_bot.py сразу после engine.process().

Журнал ведётся с нуля с момента деплоя этой фичи — истории до этого нет.
"""
import os, sys, json, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "processing_log.jsonl")
UNRESOLVED_SHOW = 20

_SUPPLIER_BY_INN = None


def _supplier_name(inn):
    global _SUPPLIER_BY_INN
    if _SUPPLIER_BY_INN is None:
        import invoice_to_po as inv
        _SUPPLIER_BY_INN = {k: v["name"] for k, v in inv.SUPPLIERS.items()}
    return _SUPPLIER_BY_INN.get(inn, inn or "?")


def log_event(engine_kind, source, fn, frm, res):
    """engine_kind: 'invoice'|'upd'. source: 'mail'|'tg'. Дописывает одну строку в LOG_PATH.

    Кроме статуса пишем ключ связки счёт↔УПД — заказ поставщику (order_id/order_name):
    счёт создаёт заказ (имя заказа = номер счёта), УПД цепляет приёмку к тому же заказу.
    Это позволяет отчёту спаривать счёт и его УПД и подсвечивать «счёт есть — УПД ещё нет».
    """
    if engine_kind == "invoice":
        number = res.get("number")
        supplier = res.get("supplier")
        order_name = res.get("name")            # имя заказа = номер счёта (возм. с суффиксом года)
        order_id = res.get("order_id")          # есть только если заказ создан
        supplier_inn = res.get("supplier_inn")
        doc_date = res.get("inv_date")          # дд.мм.гггг
    else:
        u = res.get("upd") or {}
        o = res.get("order") or {}
        number = u.get("number") or o.get("name")
        supplier = _supplier_name(u.get("seller_inn"))
        order_name = o.get("name")              # заказ, к которому привязалась приёмка
        order_id = o.get("id")
        supplier_inn = u.get("seller_inn")
        doc_date = u.get("date")                # iso
    ev = {
        "ts": time.time(), "source": source, "kind": engine_kind, "fn": fn, "from": frm,
        "number": number, "supplier": supplier, "supplier_inn": supplier_inn,
        "order_name": order_name, "order_id": order_id, "doc_date": doc_date,
        "ok": res.get("ok"), "created": res.get("created"), "stop": res.get("stop"),
        "error": res.get("error") or res.get("stop_msg"),
    }
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception:
        pass  # журнал — вспомогательный отчёт, не должен ронять основной конвейер


PENDING_SHOW = 40      # сколько «ждущих УПД» и ошибок показывать поимённо
_ok = lambda ev: bool(ev.get("ok") and ev.get("created"))


def _dedup_latest(events, ident):
    """Оставить по одной (последней по ts) записи на логическую сущность (счёт или УПД)."""
    latest = {}
    for ev in events:
        k = ident(ev)
        cur = latest.get(k)
        if cur is None or ev.get("ts", 0) >= cur.get("ts", 0):
            latest[k] = ev
    return list(latest.values())


def _order_key(ev):
    """Ключ заказа поставщику для связки счёт↔УПД: сначала order_id, потом имя заказа."""
    if ev.get("order_id"):
        return ("id", ev["order_id"])
    if ev.get("order_name"):
        return ("name", ev["order_name"])
    return None


def _short_date(ev):
    d = ev.get("doc_date") or ""
    if not d:
        return ""
    if len(d) >= 10 and d[4] == "-":              # iso 2026-07-21 → 21.07
        return f"{d[8:10]}.{d[5:7]}"
    return d[:5]                                    # уже дд.мм(.гггг)


# ──────────────────────────────────────────────────────────────────────────
# СВЕРКА С МОЙСКЛАД на момент формирования отчёта.
# Журнал помнит проваленную/дублирующую попытку, но в МС кейс мог быть уже закрыт
# (приёмка/заказ созданы вручную или повтором). Раздел ошибок должен показывать
# только АКТУАЛЬНЫЕ дыры → сверяемся с МС и молча убираем закрытое. Подтверждённое
# закрытие кэшируем в sidecar, чтобы /report не дёргал МС по уже закрытым.
# ──────────────────────────────────────────────────────────────────────────
_RESOLVED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_resolved_cache.json")
_ms_calls = 0            # счётчик обращений к МС за прогон (для отладки/тестов)


def _number_from_fn(fn):
    """Достать вероятный номер заказа/счёта из имени файла, когда парсинг упал и
    number=null (типовой кейс: успешный .xls + не распарсившийся дубль .pdf/.xlsx
    того же документа с номером в имени, напр. «…KV00009784 от 22_07_26.pdf»).
    Консервативно: префиксные буквенно-цифровые (KV/TC/ОД…), форма 326868/И,
    номер после «№» (там он однозначен — «Счет на оплату № 10684 от 29.07.2026.xls»,
    иначе 5-значный номер не ловился и ошибка висела вечно), длинные числа ≥6 —
    чтобы не поймать короткий мусор (даты, fio-05342)."""
    import re
    if not fn:
        return None
    fn = _norm_fn(fn)          # иначе «\d{6,}» ловит таймстамп-префикс инбокса, а не номер
    for pat in (r"[A-ZА-Я]{2}\d{4,}", r"\d{5,6}/[А-Яа-яA-Za-z]",
                r"№\s*([0-9A-Za-zА-Яа-я][0-9A-Za-zА-Яа-я/\-]{2,})", r"\d{6,}"):
        m = re.search(pat, fn)
        if m:
            return (m.group(1) if m.groups() else m.group(0)).strip(" -/")
    return None


def _norm_fn(fn):
    """Имя файла без ведущего таймстамп-префикса, который бот добавляет при сохранении
    в inbox («1785157098_СЧЕТ-ДОГОВОР-6307.pdf» → «СЧЕТ-ДОГОВОР-6307.pdf»). Один документ,
    пришедший разными путями (TG-оригинал без префикса vs сохранённая копия / CLI-реран
    с префиксом), должен схлопываться при дедупе успех↔провал — иначе провал висит в
    ошибках, хотя тот же файл уже создал заказ (напр. 4-значные номера Феррета, которые
    _number_from_fn не ловит, а number/inn у провального события = null)."""
    import re
    if not fn:
        return fn
    return re.sub(r"^\d{9,}_", "", fn)


def _load_resolved():
    try:
        with open(_RESOLVED_PATH, encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_resolved(keys):
    try:
        tmp = _RESOLVED_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(keys), f, ensure_ascii=False)
        os.replace(tmp, _RESOLVED_PATH)
    except Exception:
        pass  # кэш вспомогательный — молча


def _reconcile_open(pending, bad_inv, bad_upd):
    """Сверить «открытые» кейсы с МС; вернуть отфильтрованные списки.

    Возвращает (pending, bad_inv, bad_upd, n_pending_resolved, ms_partial).
    Разрешённые (в МС уже закрыты) убираются молча; pending-разрешённые считаются
    как «закрыто». Ошибка обращения к МС → кейс остаётся показанным, ms_partial=True.
    """
    global _ms_calls
    resolved = _load_resolved()
    new_resolved = set()
    ms_partial = False

    try:
        import ms
        import invoice_to_po as inv
    except Exception:
        # нет доступа к МС-хелперам — применяем только кэш, МС не трогаем
        pend = [s for s in pending if f"po:{(s['inv'].get('order_id') or s['inv'].get('order_name'))}" not in resolved]
        bi = [e for e in bad_inv if f"inv:{e.get('supplier_inn')}:{e.get('order_name') or e.get('number')}" not in resolved]
        bu = [e for e in bad_upd if f"upd:{e.get('supplier_inn')}:{e.get('number')}" not in resolved]
        return pend, bi, bu, len(pending) - len(pend), True

    has_ms = bool(getattr(ms, "TOK", None))
    cp_cache = {}   # inn -> counterparty href (или None), кэш на прогон

    def _get(path):
        global _ms_calls
        _ms_calls += 1
        return ms.get(path)

    def _po_has_supply(order_id=None, order_name=None):
        """True/False — есть ли у заказа привязанная приёмка. None при ошибке МС."""
        try:
            if order_id:
                r = _get(f"/entity/purchaseorder/{order_id}")
                return bool(r.get("supplies"))
            if order_name:
                import urllib.parse as up
                r = _get(f"/entity/purchaseorder?filter=name={up.quote(order_name, safe='')}&limit=1")
                rows = r.get("rows") or []
                return bool(rows and rows[0].get("supplies"))
        except Exception:
            return None
        return False

    def _digits(s):
        """Цифровое ядро номера: буквы, разделители и ведущие нули долой.
        «НР-010684» и «10684» — один документ, «106841» — другой."""
        d = "".join(ch for ch in (s or "") if ch.isdigit())
        return d.lstrip("0") or d

    def _po_exists(name):
        """True/False — заведён ли уже заказ по этому счёту. None при ошибке.

        Точного имени мало: бот называет заказ номером счёта, а руками его заводят
        со своим префиксом («НР-010684» вместо «10684») — из-за этого ошибка по счёту
        10684 висела в отчёте неделю, хотя заказ и приёмка давно были. Поэтому при
        промахе ищем по вхождению цифр и подтверждаем совпадением цифрового ядра."""
        try:
            import urllib.parse as up
            r = _get(f"/entity/purchaseorder?filter=name={up.quote(name, safe='')}&limit=1")
            if (r.get("meta", {}).get("size", 0) or 0) >= 1:
                return True
            num = _digits(name)
            if len(num) < 3:                 # слишком короткий хвост — поиск по вхождению шумит
                return False
            r = _get(f"/entity/purchaseorder?filter=name~{up.quote(num, safe='')}&limit=50")
            return any(_digits(x.get("name")) == num for x in (r.get("rows") or []))
        except Exception:
            return None

    def _agent_href(inn):
        if inn in cp_cache:
            return cp_cache[inn]
        href = None
        try:
            ov = inv.AGENT_OVERRIDE.get(inn) if getattr(inv, "AGENT_OVERRIDE", None) else None
            if ov:
                href = f"{ms.MS}/entity/counterparty/{ov}"
            else:
                r = _get(f"/entity/counterparty?filter=inn={inn}&limit=1")
                rows = r.get("rows") or []
                if rows:
                    href = f"{ms.MS}/entity/counterparty/{rows[0]['id']}"
        except Exception:
            href = None
        cp_cache[inn] = href
        return href

    def _supply_by_incnum(inn, number):
        """True/False — есть ли приёмка этого поставщика с incomingNumber==number. None при ошибке."""
        href = _agent_href(inn)
        if not href:
            return None
        try:
            r = _get(f"/entity/supply?filter=agent={href}&order=updated,desc&limit=100")
            for s in r.get("rows") or []:
                if (s.get("incomingNumber") or "").strip() == (number or "").strip():
                    return True
            return False
        except Exception:
            return None

    # ── pending: счёт есть, приёмки нет → закрыт, если у заказа появилась приёмка ──
    pend_out = []
    n_pend_resolved = 0
    for slot in pending:
        inv_ev = slot["inv"]
        oid = inv_ev.get("order_id")
        oname = inv_ev.get("order_name") or inv_ev.get("number")
        key = f"po:{oid or oname}"
        if key in resolved:
            n_pend_resolved += 1
            continue
        if not has_ms:
            pend_out.append(slot); continue
        r = _po_has_supply(oid, oname)
        if r is True:
            new_resolved.add(key); n_pend_resolved += 1
        elif r is None:
            ms_partial = True; pend_out.append(slot)
        else:
            pend_out.append(slot)

    # ── bad_inv: заказ не создан → снят, если заказ с таким именем уже есть ──
    bi_out = []
    for ev in bad_inv:
        name = ev.get("order_name") or ev.get("number") or _number_from_fn(ev.get("fn"))
        key = f"inv:{ev.get('supplier_inn')}:{name}"
        if key in resolved:
            continue
        if not has_ms or not name:
            bi_out.append(ev); continue
        r = _po_exists(name)
        if r is True:
            new_resolved.add(key)
        elif r is None:
            ms_partial = True; bi_out.append(ev)
        else:
            bi_out.append(ev)

    # ── bad_upd: заказ не найден → снят, если приёмка с этим incomingNumber уже есть ──
    bu_out = []
    for ev in bad_upd:
        number = ev.get("number")
        inn = ev.get("supplier_inn")
        key = f"upd:{inn}:{number}"
        if key in resolved:
            continue
        if not has_ms or not number:
            bu_out.append(ev); continue
        r = _supply_by_incnum(inn, number)
        if r is True:
            new_resolved.add(key)
        elif r is None:
            ms_partial = True; bu_out.append(ev)
        else:
            bu_out.append(ev)

    if new_resolved:
        _save_resolved(resolved | new_resolved)
    return pend_out, bi_out, bu_out, n_pend_resolved, ms_partial


def _report_window():
    """(ts_начала, date_начала) окна отчёта: предыдущий рабочий день 00:00 … сейчас.

    «Свежие данные за вчера и сегодня» = предыдущий рабочий день + текущий. Суббота —
    рабочая (приёмки идут → six=True), поэтому выходные между предыдущим рабочим днём и
    сегодня тоже попадают в окно (шагаем назад от вчера до ближайшего рабочего дня,
    всё от него до «сейчас» — внутри окна)."""
    import datetime
    today = datetime.date.today()
    d = today - datetime.timedelta(days=1)
    try:
        import workcal
        for _ in range(14):
            if workcal.is_working(d, six=True):
                break
            d -= datetime.timedelta(days=1)
    except Exception:
        for _ in range(14):                     # фолбэк без workcal: только воскресенье — не рабочее
            if d.weekday() != 6:
                break
            d -= datetime.timedelta(days=1)
    ts = time.mktime(datetime.datetime(d.year, d.month, d.day).timetuple())
    return ts, d


def build_report(path=None):
    """Отчёт по счетам и УПД: пары «счёт → УПД» по заказам поставщикам.

    Драйвер — счёт: если он проведён (создан заказ), к нему обязан прийти УПД без ошибок.
    Пока приёмки нет — заказ висит в списке «Ждут УПД». Плюс отдельно ошибки счетов/УПД,
    закрытые пары и УПД, пришедшие без счёта через бота.
    """
    path = path or LOG_PATH
    if not os.path.exists(path):
        return "Журнал пуст — обработок ещё не было."

    inv_ev, upd_ev = [], []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            (inv_ev if ev.get("kind") == "invoice" else upd_ev).append(ev)

    if not inv_ev and not upd_ev:
        return "Журнал пуст — обработок ещё не было."

    # одна запись на счёт и на УПД (последняя по времени) — стабильно по (номер, ИНН) или файлу
    ident = lambda ev: (ev.get("number") or ev.get("fn"), ev.get("supplier_inn"))
    invs = _dedup_latest(inv_ev, ident)
    upds = _dedup_latest(upd_ev, ident)

    orders = {}   # order_key -> {"inv":ev, "upd":ev}
    bad_inv = []  # счёт с ошибкой (заказ не создан)
    bad_upd = []  # УПД не привязана (заказ не найден/неоднозначно) — нет ключа заказа
    solo_upd = [] # УПД проведена, но заказ без ключа (легаси-записи без order_*)

    for ev in invs:
        k = _order_key(ev)
        if _ok(ev) and k:
            orders.setdefault(k, {})["inv"] = ev
        else:
            bad_inv.append(ev)

    for ev in upds:
        k = _order_key(ev)
        if k:
            orders.setdefault(k, {})["upd"] = ev      # и успешные, и «нашёл заказ, но не создал»
        elif _ok(ev):
            solo_upd.append(ev)                        # проведена, но без привязки к заказу (легаси)
        else:
            bad_upd.append(ev)

    pending, closed, upd_only = [], [], []
    for slot in orders.values():
        inv, upd = slot.get("inv"), slot.get("upd")
        if inv and upd and _ok(upd):
            closed.append(slot)
        elif inv:
            pending.append(slot)                       # счёт есть, УПД нет либо с ошибкой
        elif upd and _ok(upd):
            upd_only.append(slot)                      # УПД без счёта через бота
        elif upd:
            bad_upd.append(upd)                        # УПД нашёл заказ, но не создался (дубль/ошибка)
    solo_upd += [s["upd"] for s in upd_only]

    # Шум одного документа по нескольким каналам/форматам: если файл с таким именем
    # где-то отработал УСПЕШНО (напр. .xls через почту создал заказ), то провалы того
    # же файла (TG/PDF-дубль) — не дыра. Убираем их без обращения к МС.
    ok_fns = {_norm_fn(e.get("fn")) for e in (inv_ev + upd_ev) if _ok(e) and e.get("fn")}
    bad_inv = [e for e in bad_inv if _norm_fn(e.get("fn")) not in ok_fns]
    bad_upd = [e for e in bad_upd if _norm_fn(e.get("fn")) not in ok_fns]

    # Ошибки живут ОДИН день (правило Сергея 04.08.2026): разобранное вчера не должно
    # висеть в отчёте — вчерашние ошибки либо уже заведены руками, либо про них знают.
    # Сегодня показываем только сегодняшние. «Ждут УПД» под это правило НЕ попадает:
    # там дыра держится, пока не придёт приёмка.
    # PROC_LOG_ERR_DAYS — заглянуть глубже разово, не правя код: «=7» покажет ошибки
    # за неделю (со сверкой по МС, так что закрытые всё равно не всплывут).
    import datetime as _dt
    _err_days = int(os.getenv("PROC_LOG_ERR_DAYS") or 0)
    err_win = "сегодня" if not _err_days else f"за {_err_days + 1} дн."
    _t = _dt.date.today() - _dt.timedelta(days=_err_days)
    day_ts = time.mktime(_dt.datetime(_t.year, _t.month, _t.day).timetuple())
    n_old_err = len([e for e in bad_inv + bad_upd if (e.get("ts") or 0) < day_ts])
    bad_inv = [e for e in bad_inv if (e.get("ts") or 0) >= day_ts]
    bad_upd = [e for e in bad_upd if (e.get("ts") or 0) >= day_ts]

    # СВЕРКА С МС на момент отчёта: молча убрать кейсы, уже закрытые в МойСклад
    # (приёмка/заказ созданы вручную/повтором). Раздел ошибок — только реальные дыры.
    bad_upd = _dedup_latest(bad_upd, ident) if bad_upd else bad_upd
    pending, bad_inv, bad_upd, n_resolved, ms_partial = _reconcile_open(pending, bad_inv, bad_upd)

    def by_supplier(items, pick):
        c = {}
        for it in items:
            s = (pick(it).get("supplier")) or "?"
            c[s] = c.get(s, 0) + 1
        return sorted(c.items(), key=lambda x: -x[1])

    def sup(ev):
        return ev.get("supplier") or ev.get("supplier_inn") or "?"

    # Окно «свежих» закрытий: предыдущий рабочий день + сегодня (+ выходные между ними).
    # Раздел «Закрыто» больше не копит всю историю — только закрытые в этом окне.
    # «Ждут УПД» по дате НЕ режем: это актуальная дыра, сверяется с МС. Ошибки — за сегодня (выше).
    win_ts, win_date = _report_window()
    period = f"{win_date:%d.%m}–{_dt.date.today():%d.%m}"

    def _slot_ts(sl):
        return max((sl.get("inv") or {}).get("ts", 0) or 0,
                   (sl.get("upd") or {}).get("ts", 0) or 0)
    closed = [sl for sl in closed if _slot_ts(sl) >= win_ts]

    L = []
    n_pending = len(pending)
    L.append("📊 Отчёт по счетам и УПД")
    L.append(f"Заказов закрыто за {period}: {len(closed)}  ·  ждут УПД: {n_pending}"
             + (f"  ·  ошибок: {len(bad_inv) + len(bad_upd)}" if (bad_inv or bad_upd) else ""))

    # ── ГЛАВНОЕ: счёт есть, УПД ещё нет (держим, пока не придёт приёмка) ──
    if pending:
        L.append("")
        L.append(f"⏳ Ждут УПД — счёт проведён, приёмки нет ({n_pending}):")
        pend_sorted = sorted(
            pending, key=lambda s: (sup(s["inv"]), s["inv"].get("ts", 0)))
        for slot in pend_sorted[:PENDING_SHOW]:
            inv, upd = slot["inv"], slot.get("upd")
            d = _short_date(inv)
            line = f"   • {sup(inv)} · заказ {inv.get('order_name') or inv.get('number') or '?'}"
            if d:
                line += f" · счёт от {d}"
            if upd and not _ok(upd):
                line += f" · УПД с ошибкой: {(upd.get('error') or '—')[:80]}"
            else:
                line += " · УПД не поступал"
            L.append(line)
        if n_pending > PENDING_SHOW:
            L.append(f"   … и ещё {n_pending - PENDING_SHOW}")

    # ── ошибки счетов ──
    if bad_inv:
        L.append("")
        L.append(f"⚠️ Счёт с ошибкой — заказ не создан, {err_win} ({len(bad_inv)}):")
        for ev in bad_inv[-PENDING_SHOW:]:
            t = time.strftime("%d.%m %H:%M", time.localtime(ev.get("ts", 0)))
            L.append(f"   • [{t}] {sup(ev)} · {ev.get('number') or ev.get('fn')}: "
                     f"{(ev.get('error') or '—')[:90]}")

    # ── УПД, не привязавшиеся к заказу ──
    if bad_upd:
        bad_upd_d = _dedup_latest(bad_upd, ident)
        L.append("")
        L.append(f"⚠️ УПД без заказа — приёмка не создана, {err_win} ({len(bad_upd_d)}):")
        for ev in bad_upd_d[-PENDING_SHOW:]:
            t = time.strftime("%d.%m %H:%M", time.localtime(ev.get("ts", 0)))
            L.append(f"   • [{t}] {sup(ev)} · {ev.get('number') or ev.get('fn')}: "
                     f"{(ev.get('error') or '—')[:90]}")

    # Прошлые ошибки не показываем, но и не прячем факт: одна строка счётчиком,
    # чтобы «тихо исчезло» не выглядело как «ошибок не было».
    if n_old_err:
        L.append("")
        # Счётчик берётся ДО сверки с МС (её на старьё не гоняем — лишние запросы),
        # поэтому число завышено: часть этих кейсов давно закрыта руками.
        L.append(f"   (ошибок прошлых дней скрыто: {n_old_err}, без сверки с МС — "
                 f"часть уже закрыта; глубже: PROC_LOG_ERR_DAYS=7)")

    # ── закрытые пары за окно (компактно) ──
    if closed:
        L.append("")
        L.append(f"✅ Закрыто за {period} — счёт + УПД ({len(closed)}):")
        for s, n in by_supplier(closed, lambda sl: sl["inv"]):
            L.append(f"   • {s}: {n}")

    if not (pending or bad_inv or bad_upd or closed):
        L.append("")
        L.append("Пока ни счетов, ни УПД в журнале не по чему собрать пары.")

    if ms_partial:
        L.append("")
        L.append("⚠ Сверка с МойСклад частично недоступна — список ошибок мог не"
                 " отфильтроваться до конца (показаны в т.ч. возможно уже закрытые).")
    return "\n".join(L)


if __name__ == "__main__":
    import sys as _s
    print(build_report(_s.argv[1] if len(_s.argv) > 1 else None))
