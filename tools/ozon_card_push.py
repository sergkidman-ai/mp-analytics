"""tools/ozon_card_push.py — поток: card. Дожиматель карточек Ozon: классы A и W.

Что делает: отправляет карточке обратно ОДИН атрибут с ЕГО ЖЕ ТЕКУЩИМ значением. Контент
не меняется ни на байт — меняется только факт «пришёл свежий апдейт», и площадка пересматривает
свой устаревший вердикт. Один и тот же приём лечит две разные болезни:

  A — «Ошибка / Не обновлён» (status_failed=imported, ошибок в API нет). Путь Не обновлён →
      Обновляется → Готов к продаже за ~2 минуты. Проверено 22–23.08.2026: 408 карточек
      двух аккаунтов, не вылечилась одна, повторная модерация не запускалась ни разу.
  W — «На доработку» (PARTIAL_APPROVED) с замечанием из белого списка HEAL_WARN: значение
      в карточке ЕСТЬ, а площадка держит устаревшее замечание. Опыт 23.08.2026, 25 карточек acc1
      в двух прогонах: attribute_hierarchy_fail 3/3, картиночные 5/5, double_without_merger_offer
      5/5, erased_attribute_value 5/5 — снялись, все 18 ушли из списка «На доработку», продажу
      не потеряла ни одна. Замечания «значения нет вовсе» (warning_attribute_values_empty,
      …_out_of_range) повтором НЕ лечатся 0/7 и в белый список не входят — это работа ТК.

Успех у классов проверяется РАЗНЫМ признаком: у A — очистившийся status_failed, у W — исчезнувший
код замечания. Общего признака нет: у карточки класса W status_failed пуст с самого начала,
и наивная проверка засчитала бы вылеченными все подряд.

ПОЧЕМУ ЭТО НЕ «ПРАВКА КАРТОЧКИ»: контент карточки принадлежит ТК. Мы отправляем ровно то,
что уже лежит в карточке — иначе первый же пуш ТК затрёт нашу правку, а мы получим войну
двух систем за одно поле.

Отправка на площадку по умолчанию ВЫКЛЮЧЕНА: без --apply это сухой прогон (план + причины
пропусков). Так задумано, правило CLAUDE.md: запись на площадки — по прямой команде.

  ./venv/bin/python tools/ozon_card_push.py                    # сухой прогон, ничего не шлёт
  ./venv/bin/python tools/ozon_card_push.py --apply            # реальная отправка (A и W)
  ./venv/bin/python tools/ozon_card_push.py --classes A --apply   # только «Ошибка»
  ./venv/bin/python tools/ozon_card_push.py --tk-report        # выгрузка класса C для ТК

Предохранители (все включены всегда):
  * потолок попыток на карточку и backoff между ними — не долбим одну и ту же бесконечно;
  * пропуск карточки со свежим апдейтом ТК — иначе гонка: мы затрём то, что ТК шлёт сейчас;
  * живая перепроверка статуса перед отправкой — состояние в БД могло устареть;
  * ворота после первых GATE_N карточек: не лечатся — прогон останавливается;
  * стоп при declined или уходе из продажи — это уже потеря выручки, а не наша ошибка формата;
  * журнал card_push_log: одна строка на отправку, прогон возобновляем и не шлёт дважды.

Признак успеха — НЕ HTTP 200, а task_status=imported плюс очистившийся status_failed
при перечитке. Ozon отдаёт 200 и на задачу, которую не принял к исполнению (status=skipped).
"""
import sys
import time
import argparse
import csv
from datetime import datetime, timezone

import requests

sys.path.insert(0, "/opt/mp-analytics")
from core.db import query, execute                     # noqa: E402
from collectors.ozon import _headers, PRODUCT_INFO_URL  # noqa: E402
from collectors.ozon_card_status import HEAL_WARN       # noqa: E402

ATTR_URL = "https://api-seller.ozon.ru/v4/product/info/attributes"
UPD_URL = "https://api-seller.ozon.ru/v1/product/attributes/update"
TASK_URL = "https://api-seller.ozon.ru/v1/product/import/info"
PLATFORM = "ozon"

# Атрибуты, безопасные для отправки самих себе, по приоритету: короткие, обязательные,
# не участвуют в модерации текста. 9024 Артикул → 4389 Страна → 10400 Гарантия.
SAFE_ATTRS = [9024, 4389, 10400]

MAX_ATTEMPTS = 3        # потолок попыток на карточку, дальше — человеку
BACKOFF_H = 20          # не трогать карточку чаще раза в сутки
FRESH_H = 2             # апдейт ТК свежее этого — не лезем, гонка
PAUSE = 1.5             # темп отправки
GATE_N = 20             # сколько карточек проверить, прежде чем пускать остальные
GATE_MIN_HEALED = 0.5   # доля вылечившихся, ниже которой прогон останавливается
SETTLE = 480            # сколько ждать перед перечиткой статусов
SELLING = ("Продается", "Готов к продаже")


def _pool(account, limit, classes, offers=None):
    """Кандидаты из БД: открытые, попытки не исчерпаны, backoff выдержан.

    Порядок: сначала класс A (карточка сломана и часто не торгует), потом W (карточка торгует,
    страдает только видимость); внутри класса — сначала неторгующие и самые давние. Если что-то
    пойдёт не так, первыми под удар попадают карточки, которые и так не приносят денег.
    """
    return query(
        "SELECT offer_id, product_id, attempts, is_selling, err_class, err_codes "
        "FROM card_status "
        "WHERE platform = %s AND account = %s AND is_open AND err_class = ANY(%s) "
        "  AND (%s IS NULL OR offer_id = ANY(%s)) "
        "  AND NOT needs_human AND attempts < %s "
        "  AND (last_attempt_at IS NULL OR last_attempt_at < now() - make_interval(hours => %s)) "
        "ORDER BY err_class, is_selling, first_seen LIMIT %s",
        (PLATFORM, account, list(classes), offers, offers, MAX_ATTEMPTS, BACKOFF_H, limit))


def _live_states(H, product_ids):
    """Состояние карточек прямо сейчас: БД могла устареть, а мы собираемся писать."""
    out = {}
    for i in range(0, len(product_ids), 1000):
        r = requests.post(PRODUCT_INFO_URL, headers=H,
                          json={"product_id": product_ids[i:i + 1000]}, timeout=180)
        r.raise_for_status()
        for it in r.json().get("items", []):
            st = it.get("statuses") or {}
            errs = it.get("errors") or []
            st["_hard_errors"] = [e for e in errs if e.get("level") == "ERROR_LEVEL_ERROR"]
            st["_codes"] = {e.get("code") for e in errs if e.get("code")}
            out[it.get("offer_id")] = st
    return out


def _too_fresh(st):
    raw = st.get("status_updated_at") or ""
    try:
        u = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - u).total_seconds() < FRESH_H * 3600


def _send(H, offer_id):
    """Читаем карточку, выбираем безопасный атрибут, отправляем его обратно как есть."""
    a = requests.post(ATTR_URL, headers=H,
                      json={"filter": {"offer_id": [offer_id], "visibility": "ALL"},
                            "limit": 1, "sort_dir": "ASC"}, timeout=60)
    a.raise_for_status()
    res = a.json().get("result") or []
    if not res:
        return None, None, None, "карточка не отдалась на чтение"
    attrs = {x["id"]: x for x in res[0].get("attributes", [])}   # поле id, НЕ attribute_id
    pick = next((attrs[i] for i in SAFE_ATTRS if i in attrs and attrs[i].get("values")), None)
    if not pick:
        return None, None, None, "нет безопасного атрибута для no-op"
    body = {"items": [{"offer_id": offer_id, "attributes": [{
        "id": pick["id"], "complex_id": pick.get("complex_id", 0),
        "values": [{k: v for k, v in val.items() if k in ("dictionary_value_id", "value")}
                   for val in pick["values"]]}]}]}
    u = requests.post(UPD_URL, headers=H, json=body, timeout=60)
    # task_id приходит В КОРНЕ ответа, не в result — на этом первый прогон потерял все id.
    tid = u.json().get("task_id") if u.status_code == 200 else None
    return pick["id"], u.status_code, tid, None if tid else f"HTTP {u.status_code}, задачи нет"


def _task_status(H, task_id, tries=8, wait=15):
    for _ in range(tries):
        t = requests.post(TASK_URL, headers=H, json={"task_id": task_id}, timeout=60)
        if t.status_code == 200:
            for item in (t.json().get("result") or {}).get("items", []):
                if item.get("status") and item["status"] != "pending":
                    return item["status"]
        time.sleep(wait)
    return "pending"


def _log(rec):
    execute(
        "INSERT INTO card_push_log (platform, account, offer_id, product_id, rung, attr_id, "
        "http_code, task_id, task_status, healed, note) "
        "VALUES (%(platform)s, %(account)s, %(offer_id)s, %(product_id)s, %(rung)s, %(attr_id)s, "
        "%(http_code)s, %(task_id)s, %(task_status)s, %(healed)s, %(note)s)", rec)


def _mark_attempt(account, offer_id, needs_human=False):
    execute("UPDATE card_status SET attempts = attempts + 1, last_attempt_at = now(), "
            "needs_human = %s WHERE platform = %s AND account = %s AND offer_id = %s",
            (needs_human, PLATFORM, account, offer_id))


def _mark_healed(account, offer_id):
    execute("UPDATE card_status SET is_open = false, healed_at = now(), status_failed = NULL, "
            "needs_human = false WHERE platform = %s AND account = %s AND offer_id = %s",
            (PLATFORM, account, offer_id))


def _mark_warn_cleared(account, offer_id):
    """Замечание снято. Строку НЕ закрываем: на карточке могут висеть другие замечания,
    которые повтором не лечатся. Класс меняем на O — из пула она уходит сразу, а закроет
    её детектор, когда площадка уберёт карточку из «На доработку»."""
    execute("UPDATE card_status SET err_class = 'O', err_codes = '', healed_at = now(), "
            "needs_human = false WHERE platform = %s AND account = %s AND offer_id = %s",
            (PLATFORM, account, offer_id))


def run(account, limit, apply_, classes=("A", "W"), offers=None):
    H = _headers(account)
    probe_c = set(classes) == {"C"}   # разовая проба: повторная модерация контентных
    pool = _pool(account, limit, classes, offers)
    if not pool:
        print(f"{account}: дожимать нечего")
        return
    live = _live_states(H, [int(c["product_id"]) for c in pool if c["product_id"]])

    todo, skip, meta = [], {}, {}
    for c in pool:
        st = live.get(c["offer_id"])
        cls = c["err_class"]
        # что именно должно исчезнуть, чтобы засчитать успех
        want = set((c["err_codes"] or "").split()) & HEAL_WARN if cls == "W" else set()
        if st is None:
            skip["исчезла из выдачи"] = skip.get("исчезла из выдачи", 0) + 1
            continue
        if st.get("_hard_errors") and not probe_c:
            skip["стала контентной (класс C)"] = skip.get("стала контентной (класс C)", 0) + 1
            continue
        if cls == "A" and st.get("status_failed") != "imported":
            skip["уже здорова"] = skip.get("уже здорова", 0) + 1
            _mark_healed(account, c["offer_id"])
            continue
        if cls == "W" and not (want & st["_codes"]):
            skip["замечание уже снято"] = skip.get("замечание уже снято", 0) + 1
            _mark_warn_cleared(account, c["offer_id"])
            continue
        if _too_fresh(st):
            k = f"свежий апдейт ТК (<{FRESH_H} ч)"
            skip[k] = skip.get(k, 0) + 1
            continue
        meta[c["offer_id"]] = (cls, want)
        todo.append(c)

    by_cls = {}
    for c in todo:
        by_cls[c["err_class"]] = by_cls.get(c["err_class"], 0) + 1
    print(f"{account}: кандидатов {len(pool)} | к отправке {len(todo)} "
          f"({' '.join(f'{k}:{v}' for k, v in sorted(by_cls.items())) or '—'}) | "
          f"торгующих среди них {sum(1 for c in todo if c['is_selling'])}")
    for k, v in skip.items():
        print(f"  пропуск — {k}: {v}")
    if not apply_:
        print("СУХОЙ ПРОГОН: на площадку ничего не ушло. Реальная отправка — с флагом --apply")
        return
    if not todo:
        return

    sent, stopped = [], None
    for n, c in enumerate(todo, 1):
        attr_id, http, tid, note = _send(H, c["offer_id"])
        rec = {"platform": PLATFORM, "account": account, "offer_id": c["offer_id"],
               "product_id": c["product_id"], "rung": 1, "attr_id": attr_id,
               "http_code": http, "task_id": tid, "task_status": None,
               "healed": None, "note": " ".join(x for x in (f"class:{c['err_class']}", note) if x)}
        if tid:
            rec["task_status"] = _task_status(H, tid, tries=2, wait=5)
        _log(rec)
        # skipped = Ozon задачу не принял; повтор той же ступени не поможет — сразу человеку.
        _mark_attempt(account, c["offer_id"],
                      needs_human=(rec["task_status"] == "skipped"
                                   or c["attempts"] + 1 >= MAX_ATTEMPTS))
        sent.append(rec)
        time.sleep(PAUSE)

        if n == GATE_N and len(todo) > GATE_N and not probe_c:
            print(f"ворота: проверяю первые {GATE_N} перед остальными {len(todo) - GATE_N}")
            time.sleep(SETTLE)
            ok, bad = _verify(H, account, sent, meta)
            if bad:
                stopped = f"ворота закрыты: {bad}"
                break
            if ok / GATE_N < GATE_MIN_HEALED:
                stopped = f"ворота закрыты: вылечилось {ok} из {GATE_N}"
                break
            print(f"ворота открыты: вылечено {ok} из {GATE_N}")

    if stopped:
        print(stopped + " — остальные карточки не тронуты")
        return

    time.sleep(SETTLE)
    if probe_c:
        print(f"{account}: отправлено {len(sent)} | класс C — вердикт модерации асинхронный, "
              f"итог покажет детектор следующим утром; строки card_status не закрываю")
        return
    ok, bad = _verify(H, account, sent, meta)
    print(f"{account}: отправлено {len(sent)} | вылечено {ok} | "
          f"осталось {len(sent) - ok}" + (f" | ТРЕВОГА: {bad}" if bad else ""))


def _verify(H, account, sent, meta):
    """Перечитка: закрываем вылеченные, ловим declined и уход из продажи.

    Признак успеха разный: A — очистился status_failed; W — исчез код замечания.
    Для W проверять status_failed нельзя: он пуст с самого начала, и «вылечены» оказались бы все.
    """
    ids = [int(s["product_id"]) for s in sent if s.get("task_id") and s["product_id"]]
    if not ids:
        return 0, None
    live = _live_states(H, ids)
    ok, alarm = 0, []
    for s in sent:
        st = live.get(s["offer_id"])
        if st is None:
            continue
        cls, want = meta.get(s["offer_id"], ("A", set()))
        healed = (not (want & st["_codes"])) if cls == "W" else (not st.get("status_failed"))
        if healed:
            ok += 1
            (_mark_warn_cleared if cls == "W" else _mark_healed)(account, s["offer_id"])
        execute("UPDATE card_push_log SET healed = %s WHERE id = ("
                "SELECT max(id) FROM card_push_log WHERE platform = %s AND account = %s "
                "AND offer_id = %s)", (healed, PLATFORM, account, s["offer_id"]))
        if st.get("moderate_status") == "declined":
            alarm.append(f"{s['offer_id']} declined")
        elif st.get("status_name") not in SELLING and (
                cls == "W" or st.get("status_name") != "Не продается"):
            # у W карточка торговала до нашей отправки — любой уход из продажи это ЧП
            alarm.append(f"{s['offer_id']} → {st.get('status_name')}")
    return ok, "; ".join(alarm[:5]) if alarm else None


def tk_report(path):
    """Класс C — то, что чинится только в ТК. Выгрузка для программиста."""
    rows = query(
        "SELECT account, offer_id, status_name, err_codes, err_texts, name, "
        "       date_trunc('minute', first_seen) AS first_seen "
        "FROM card_status WHERE platform = %s AND is_open AND err_class = 'C' "
        "ORDER BY account, err_codes, offer_id", (PLATFORM,))
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else
                           ["account", "offer_id", "status_name", "err_codes", "err_texts",
                            "name", "first_seen"])
        w.writeheader()
        w.writerows(rows)
    by = {}
    for r in rows:
        by[r["err_codes"]] = by.get(r["err_codes"], 0) + 1
    print(f"класс C: {len(rows)} карточек → {path}")
    for code, n in sorted(by.items(), key=lambda x: -x[1])[:8]:
        print(f"  {n:>4}  {code}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Дожим карточек Ozon: A («Не обновлён») и W («На доработку»)")
    p.add_argument("--account", default="oz_acc1")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--classes", default="A,W",
                   help="какие классы дожимать: A, W или A,W (по умолчанию оба)")
    p.add_argument("--offers", metavar="PATH",
                   help="файл со списком offer_id (по одному в строке) — точечный прогон")
    p.add_argument("--apply", action="store_true",
                   help="реально отправить на площадку (без флага — сухой прогон)")
    p.add_argument("--tk-report", metavar="PATH", nargs="?",
                   const="docs/reports/ozon_card_content_errors.csv",
                   help="выгрузить класс C для ТК и выйти")
    a = p.parse_args()
    if a.tk_report:
        tk_report(a.tk_report)
    else:
        cls = tuple(x.strip().upper() for x in a.classes.split(",") if x.strip())
        bad = [c for c in cls if c not in ("A", "W", "C")]
        if bad:
            sys.exit(f"дожимать можно только классы A, W и C, а не {bad}")
        # Класс C автоматом НЕ дожимается: повтор лечит флаг, а не контент. Разовая проба
        # повторной модерации разрешена только точечным списком и только отдельным классом.
        if "C" in cls and (len(cls) > 1 or not a.offers):
            sys.exit("класс C — только разовой пробой: --classes C вместе с --offers <файл>")
        offers = None
        if a.offers:
            offers = [x.strip() for x in open(a.offers, encoding="utf-8") if x.strip()]
            print(f"точечный список: {len(offers)} offer_id из {a.offers}")
        run(a.account, a.limit, a.apply, cls, offers)
