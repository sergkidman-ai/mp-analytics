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

    def by_supplier(items, pick):
        c = {}
        for it in items:
            s = (pick(it).get("supplier")) or "?"
            c[s] = c.get(s, 0) + 1
        return sorted(c.items(), key=lambda x: -x[1])

    def sup(ev):
        return ev.get("supplier") or ev.get("supplier_inn") or "?"

    L = []
    n_pending = len(pending)
    head = "📊 Отчёт по счетам и УПД"
    L.append(head)
    L.append(f"Заказов закрыто (счёт+УПД): {len(closed)}  ·  ждут УПД: {n_pending}"
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
        L.append(f"⚠️ Счёт с ошибкой — заказ не создан ({len(bad_inv)}):")
        for ev in bad_inv[-PENDING_SHOW:]:
            t = time.strftime("%d.%m %H:%M", time.localtime(ev.get("ts", 0)))
            L.append(f"   • [{t}] {sup(ev)} · {ev.get('number') or ev.get('fn')}: "
                     f"{(ev.get('error') or '—')[:90]}")

    # ── УПД, не привязавшиеся к заказу ──
    if bad_upd:
        bad_upd_d = _dedup_latest(bad_upd, ident)
        L.append("")
        L.append(f"⚠️ УПД без заказа — приёмка не создана ({len(bad_upd_d)}):")
        for ev in bad_upd_d[-PENDING_SHOW:]:
            t = time.strftime("%d.%m %H:%M", time.localtime(ev.get("ts", 0)))
            L.append(f"   • [{t}] {sup(ev)} · {ev.get('number') or ev.get('fn')}: "
                     f"{(ev.get('error') or '—')[:90]}")

    # ── закрытые пары (компактно) ──
    if closed:
        L.append("")
        L.append(f"✅ Закрыто полностью — счёт + УПД ({len(closed)}):")
        for s, n in by_supplier(closed, lambda sl: sl["inv"]):
            L.append(f"   • {s}: {n}")

    # ── УПД без счёта через бота (пришли сразу приёмкой) ──
    if solo_upd:
        L.append("")
        L.append(f"ℹ️ УПД без счёта через бота — приёмка загружена ({len(solo_upd)}):")
        for s, n in by_supplier(solo_upd, lambda ev: ev):
            L.append(f"   • {s}: {n}")

    if not (pending or bad_inv or bad_upd or closed or solo_upd):
        L.append("")
        L.append("Пока ни счетов, ни УПД в журнале не по чему собрать пары.")
    return "\n".join(L)


if __name__ == "__main__":
    import sys as _s
    print(build_report(_s.argv[1] if len(_s.argv) > 1 else None))
