# -*- coding: utf-8 -*-
"""
tools/purge_old_trash.py — разовая чистка корзины МойСклад: безвозвратное удаление
документов в корзине (isDeleted=true) с датой документа (moment) раньше CUTOFF.
2025-2026 в корзине не трогаем.

Режимы:
  --list                 только посчитать/показать по типам, без удаления
  --test-one [--type T] [--id ID]
                         удалить ОДИН документ (по умолчанию — самый старый найденный)
                         и сразу проверить GET → 404 (подтверждение, что DELETE окончательно
                         чистит корзину, а не просто повторно помечает isDeleted)
  --run [--resume]       реальное массовое удаление всех найденных по всем типам

Лимиты МС (токен решения): 45 запросов/3 сек. Здесь троттлинг ~1 запрос/сек — большой запас,
плюс ретрай на 429 по X-Lognex-Retry-TimeInterval (как в collectors/moysklad.py) и стоп после
серии подряд идущих ошибок (защита от авто-отключения API за много неудачных запросов подряд).
"""
import os, sys, json, time, argparse, urllib.request, urllib.error, urllib.parse

sys.path.insert(0, "/opt/mp-analytics/invoice_bot")
import ms  # MS (base url), TOK (bearer), get()

CUTOFF = "2025-01-01 00:00:00"
TYPES = ["cashin", "cashout", "counterpartyadjustment", "customerorder", "demand", "enter",
         "internalorder", "inventory", "invoiceout", "loss", "move", "paymentin", "paymentout",
         "purchaseorder", "purchasereturn", "salesreturn", "supply"]

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "purge_trash_log.jsonl")
SLEEP = 1.0
MAX_CONSEC_ERR = 15


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def list_old_trashed(entity_type):
    """Все {id,type,name,moment} данного типа в корзине с moment<CUTOFF, постранично."""
    out, offset = [], 0
    filt = urllib.parse.quote(f"isDeleted=true;moment<{CUTOFF}", safe="")
    while True:
        q = f"filter={filt}&limit=1000&offset={offset}"
        time.sleep(SLEEP)
        d = ms.get(f"/entity/{entity_type}?{q}")
        rows = d.get("rows", [])
        for r in rows:
            out.append({"id": r["id"], "type": entity_type, "name": r.get("name"),
                        "moment": r.get("moment")})
        size = d.get("meta", {}).get("size", 0)
        offset += 1000
        if not rows or offset >= size:
            break
    return out


def delete_one(entity_type, doc_id, _tries=0):
    """DELETE /entity/{type}/{id}. Возвращает (ok, http_status, error_body|None)."""
    req = urllib.request.Request(
        f"{ms.MS}/entity/{entity_type}/{doc_id}", method="DELETE",
        headers={"Authorization": f"Bearer {ms.TOK}", "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return True, r.status, None
    except urllib.error.HTTPError as e:
        if e.code == 429 and _tries < 6:
            wait_ms = int(e.headers.get("X-Lognex-Retry-TimeInterval", "1000"))
            time.sleep(wait_ms / 1000.0 + 0.2)
            return delete_one(entity_type, doc_id, _tries + 1)
        body = e.read().decode(errors="replace")
        return False, e.code, body


def get_one(entity_type, doc_id):
    """GET /entity/{type}/{id} → (status, body_or_none). 404 = документ полностью пропал."""
    req = urllib.request.Request(
        f"{ms.MS}/entity/{entity_type}/{doc_id}",
        headers={"Authorization": f"Bearer {ms.TOK}", "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            import gzip as _gz
            d = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                d = _gz.decompress(d)
            return r.status, json.loads(d)
    except urllib.error.HTTPError as e:
        return e.code, None


def already_done():
    """id, уже отмеченные ok=true в журнале (для --resume)."""
    done = set()
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, encoding="utf-8") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                    if ev.get("ok"):
                        done.add(ev["id"])
                except Exception:
                    pass
    return done


def append_log(ev):
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def cmd_list():
    total = 0
    for t in TYPES:
        items = list_old_trashed(t)
        total += len(items)
        log(f"{t}: {len(items)}")
    log(f"ИТОГО к удалению: {total}")


def cmd_test_one(type_arg, id_arg):
    if type_arg and id_arg:
        entity_type, doc_id, name, moment = type_arg, id_arg, None, None
    else:
        candidates = []
        for t in TYPES:
            candidates.extend(list_old_trashed(t))
        if not candidates:
            log("Нет документов, подходящих под критерий — тестировать нечего.")
            return
        candidates.sort(key=lambda x: x.get("moment") or "")
        pick = candidates[0]
        entity_type, doc_id, name, moment = pick["type"], pick["id"], pick["name"], pick["moment"]

    log(f"Тест: удаляю {entity_type}/{doc_id} (name={name}, moment={moment})")
    ok, status, err = delete_one(entity_type, doc_id)
    log(f"DELETE → ok={ok} status={status} err={err}")
    time.sleep(1.0)
    g_status, g_body = get_one(entity_type, doc_id)
    log(f"Повторный GET → status={g_status}")
    if g_status == 404:
        log("РЕЗУЛЬТАТ: документ полностью пропал (404) — DELETE окончательно чистит корзину. "
            "Можно запускать массовый прогон (--run).")
    elif g_status == 200:
        still_deleted = (g_body or {}).get("isDeleted")
        log(f"РЕЗУЛЬТАТ: документ всё ещё существует (isDeleted={still_deleted}) — гипотеза НЕ "
            f"подтвердилась. Массовый прогон НЕ запускать, разбираться отдельно.")
    else:
        log(f"РЕЗУЛЬТАТ: неожиданный статус {g_status} — разбираться отдельно, не запускать --run.")
    append_log({"ts": time.time(), "type": entity_type, "id": doc_id, "name": name,
                "moment": moment, "ok": ok, "status": status, "error": err, "test": True})


def cmd_run(resume):
    skip = already_done() if resume else set()
    if skip:
        log(f"--resume: пропускаю {len(skip)} уже удалённых id из журнала")

    all_items = []
    for t in TYPES:
        items = list_old_trashed(t)
        log(f"{t}: найдено {len(items)} к удалению")
        all_items.extend(items)
    log(f"ВСЕГО к удалению: {len(all_items)}")

    n_ok, n_err, consec_err = 0, 0, 0
    for i, item in enumerate(all_items, 1):
        if item["id"] in skip:
            continue
        time.sleep(SLEEP)
        ok, status, err = delete_one(item["type"], item["id"])
        append_log({"ts": time.time(), "type": item["type"], "id": item["id"],
                    "name": item["name"], "moment": item["moment"],
                    "ok": ok, "status": status, "error": err})
        if ok:
            n_ok += 1
            consec_err = 0
        else:
            n_err += 1
            log(f"ОШИБКА {item['type']}/{item['id']} ({item['name']}): status={status} {err}")
            if status == 409:
                # "объект уже используется" — ожидаемый бизнес-конфликт (на документ есть
                # ссылки), не системный сбой; не считаем его в цепочку подряд идущих ошибок
                consec_err = 0
            else:
                consec_err += 1
            if consec_err >= MAX_CONSEC_ERR:
                log(f"СТОП: {consec_err} ошибок подряд — прерываю прогон (защита от авто-бана API). "
                    f"Обработано {i}/{len(all_items)}, успешно {n_ok}, ошибок {n_err}.")
                return
        if i % 50 == 0:
            log(f"прогресс: {i}/{len(all_items)} (успешно {n_ok}, ошибок {n_err})")

    log(f"ГОТОВО: обработано {len(all_items)}, успешно удалено {n_ok}, ошибок {n_err}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--test-one", action="store_true")
    ap.add_argument("--type")
    ap.add_argument("--id")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--resume", action="store_true")
    a = ap.parse_args()

    if a.list:
        cmd_list()
    elif a.test_one:
        cmd_test_one(a.type, a.id)
    elif a.run:
        cmd_run(a.resume)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
