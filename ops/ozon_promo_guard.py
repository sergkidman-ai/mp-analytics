"""ops/ozon_promo_guard.py — поток: mkt
Сторож акций Ozon: вытаскивает из акций то, что торгуется ниже пола. Пара к ops/wb_promo_guard.py.

Зачем: Ozon сам, «на своё усмотрение», добавляет товары в акции ночью (проверено 23.08.2026,
между 02:58 и 12:06 МСК), отключить это нельзя. К утру в акциях могут стоять позиции с ценой,
на которой мы теряем деньги.

Что смотрит: ВСЕ акции, где аккаунт участвует, КРОМЕ белого списка робота
ops/ozon_stock_action.py («Распродажа стока», «Акция для склад…») — там цену и состав ведёт
лестница с тем же полом (решение Сергея 16.09.2026: белые пропускаем).

Решения Сергея 16.09.2026:
  пол   = (себест + max(300 ₽, 10 % себеста)) / доля выручки, что остаётся нам
          (формула ВБ-сторожа, общая с лестницей — ozon_stock_action.floor_price);
  ход   = цена в акции ниже пола → ПОДНЯТЬ action_price до пола, если пол не выше потолка
          акции (товар остаётся в акции, видимость сохраняется); иначе СНЯТЬ поштучно;
          площадка отказала в подъёме (часто «DiscountPercent must be ≥ N») → СНЯТЬ;
  нет себестоимости по всей цепочке → СНЯТЬ, как на ВБ.

Страховки: себестоимость нашлась меньше чем у COGS_MIN_SHARE участников — аккаунт пропускаем
целиком и шлём тревогу (падение цепочки себеста иначе сняло бы все акции); --limit режет число
правок за прогон. После отправки состав акции перечитывается: sent → confirmed | error.

Запуск (по умолчанию расчёт, в кабинет ничего не уходит):
    ./venv/bin/python -m ops.ozon_promo_guard                        # dry-run, оба аккаунта
    ./venv/bin/python -m ops.ozon_promo_guard --csv /tmp/g.csv       # + построчно
    ./venv/bin/python -m ops.ozon_promo_guard --apply --wave morning # исполнение (только по «да»)
Журнал: oz_promo_guard_log (migrations/124_oz_promo_guard.sql).
"""
import argparse
import math
import pathlib
import re
import sys
import time

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from ops import ozon_stock_action as oz  # noqa: E402

COGS_MIN_SHARE = 0.5     # себест известен меньше чем у половины участников → цепочка сломана
COGS_MIN_ROWS = 20       # …проверяем только на заметной выборке
PRICE_EPS = 0.5          # цена ≥ пол − 0,5 ₽ — проходит
VERIFY_WAIT = 30         # сек до перечитки состава акции


def snapshot(account):
    """Все участники небелых акций: одна строка на товар×акцию, с себестом и полом."""
    keep, keep_src = oz.keep_ratio(account)
    acts = [a for a in oz.actions(account)
            if a.get("is_participating") and not oz.is_whitelisted(a.get("title"))]
    rows = []
    for a in acts:
        for p in oz.participants(account, a["id"]):
            rows.append({"account": account, "action_id": a["id"], "action_title": a.get("title"),
                         "product_id": int(p["id"]),
                         "action_price": float(p.get("action_price") or 0),
                         "max_price": float(p.get("max_action_price") or 0),
                         "stock": int(p.get("stock") or 0), "min_stock": int(p.get("min_stock") or 0)})
    pid2 = oz.offer_of(account, sorted({r["product_id"] for r in rows})) if rows else {}
    cogs = oz.cogs_map(sorted({v[0] for v in pid2.values() if v[0]}))
    for r in rows:
        r["offer_id"], r["name"] = pid2.get(r["product_id"], (None, ""))
        r["cogs"], r["cogs_source"] = cogs.get(r["offer_id"], (None, "НЕТ")) if r["offer_id"] else (None, "нет offer_id")
        r["keep_ratio"] = keep
    return rows, acts, keep, keep_src


def decide(r):
    """Заполняет mode (ok | raise | remove), floor_price, new_price, reason."""
    r["floor_price"] = r["new_price"] = None
    if r["cogs"] is None:
        r["mode"], r["reason"] = "remove", f"себестоимость не найдена ({r['cogs_source']})"
        return r
    floor = oz.floor_price(float(r["cogs"]), r["keep_ratio"])
    r["floor_price"] = round(floor, 2)
    if r["action_price"] >= floor - PRICE_EPS:
        r["mode"], r["reason"] = "ok", "проходит по полу"
    elif r["max_price"] > 0 and math.ceil(floor) <= r["max_price"]:
        r["mode"], r["new_price"] = "raise", float(math.ceil(floor))
        r["reason"] = f"цена {r['action_price']:.0f} < пол {floor:.0f}, поднимаем"
    else:
        r["mode"] = "remove"
        r["reason"] = f"пол {floor:.0f} > потолок {r['max_price']:.0f}"
    return r


def _cut(rows, limit):
    todo = [r for r in rows if r["mode"] in ("raise", "remove")]
    if limit is not None and len(todo) > limit:
        # в первую очередь самые убыточные: больше всего рублей под полом
        # (без себеста — первыми: убыток не оценить)
        todo.sort(key=lambda r: -(math.inf if r["floor_price"] is None
                                   else r["floor_price"] - r["action_price"]))
        for r in todo[limit:]:
            r["reason"] += f" · отложено --limit {limit}"
            r["mode"] = "ok_deferred"
    return rows


def push(account, rows):
    """Подъём через activate (он же переустанавливает цену), снятие — deactivate.
    Отказ в подъёме → снимаем в том же прогоне."""
    for aid in sorted({r["action_id"] for r in rows if r["mode"] in ("raise", "remove")}):
        part = [r for r in rows if r["action_id"] == aid]
        up = [r for r in part if r["mode"] == "raise"]
        if up:
            prods = []
            for r in up:
                item = {"product_id": r["product_id"], "action_price": r["new_price"]}
                st = max(r["stock"], r["min_stock"])
                if st > 0:                               # лимит остатка в акции не меняем
                    item["stock"] = st
                prods.append(item)
            res = oz._req(account, "POST", "/v1/actions/products/activate",
                          {"action_id": aid, "products": prods}).get("result", {})
            ok_ids = {int(x) for x in (res.get("product_ids") or [])}
            rej = {int(x.get("product_id")): x.get("reason") for x in (res.get("rejected") or [])}
            for r in up:
                if r["product_id"] in ok_ids:
                    r["status"] = "sent"
                else:
                    r["note"] = f"подъём отклонён: {rej.get(r['product_id'], 'нет в ответе')}"
                    r["mode"], r["new_price"] = "remove", None
                    r["reason"] += " → отказ площадки, снимаем"
        rm = [r for r in part if r["mode"] == "remove"]
        if rm:
            res = oz._req(account, "POST", "/v1/actions/products/deactivate",
                          {"action_id": aid, "product_ids": [r["product_id"] for r in rm]}).get("result", {})
            ok_ids = {int(x) for x in (res.get("product_ids") or [])}
            rej = {int(x.get("product_id")): x.get("reason") for x in (res.get("rejected") or [])}
            for r in rm:
                if r["product_id"] in ok_ids:
                    r["status"] = "sent"
                else:
                    r["status"] = "error"
                    r["note"] = ((r.get("note") or "") + f" · снять не вышло: {rej.get(r['product_id'], 'нет в ответе')}").strip(" ·")


def verify(account, rows):
    """Перечитываем состав тронутых акций: снятого там нет, поднятый стоит не ниже пола."""
    sent = [r for r in rows if r.get("status") == "sent"]
    if not sent:
        return
    time.sleep(VERIFY_WAIT)
    for aid in sorted({r["action_id"] for r in sent}):
        try:
            now = {int(p["id"]): float(p.get("action_price") or 0) for p in oz.participants(account, aid)}
        except Exception as e:                                          # noqa: BLE001
            for r in sent:
                if r["action_id"] == aid:
                    r["note"] = ((r.get("note") or "") + f" · сверка не удалась: {e}").strip(" ·")
            continue
        for r in sent:
            if r["action_id"] != aid:
                continue
            pid = r["product_id"]
            if r["mode"] == "remove":
                good = pid not in now
            else:
                good = pid in now and now[pid] >= r["new_price"] - PRICE_EPS
            r["status"] = "confirmed" if good else "error"
            if not good:
                r["note"] = ((r.get("note") or "") + f" · сверка: в акции цена {now.get(pid, 'нет')}").strip(" ·")


COLS = ["account", "wave", "action_id", "action_title", "product_id", "offer_id", "name",
        "action_price", "max_price", "cogs", "cogs_source", "keep_ratio", "floor_price",
        "new_price", "mode", "reason", "status", "note"]


def save(rows, wave):
    if not rows:
        return
    import psycopg2.extras
    with db.get_conn() as conn, conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur, f"insert into oz_promo_guard_log ({', '.join(COLS)}) values %s",
            [tuple({**r, "wave": wave}.get(c) for c in COLS) for r in rows], page_size=500)


def summary(account, rows, acts, keep, keep_src, apply):
    m = {}
    for r in rows:
        m[r["mode"]] = m.get(r["mode"], 0) + 1
    lines = [f"*{account}* · сторож акций Ozon · {'исполнение' if apply else 'расчёт'}",
             f"акций (без белых): {len(acts)}, позиций: {len(rows)}, остаётся нам {keep*100:.1f}% ({keep_src})",
             f"  · проходят: {m.get('ok', 0)}  · поднять: {m.get('raise', 0)}  · снять: {m.get('remove', 0)}"
             + (f"  · отложено: {m['ok_deferred']}" if m.get("ok_deferred") else "")]
    why = {}
    for r in rows:
        if r["mode"] in ("raise", "remove"):
            k = re.sub(r"\d+", "N", r["reason"].split(" (")[0])
            why[k] = why.get(k, 0) + 1
    for k, n in sorted(why.items(), key=lambda x: -x[1])[:4]:
        lines.append(f"  – {k}: {n}")
    if apply:
        st = {}
        for r in rows:
            if r["mode"] in ("raise", "remove"):
                st[r.get("status")] = st.get(r.get("status"), 0) + 1
        lines.append("сверка: " + (", ".join(f"{k} {v}" for k, v in st.items()) or "правок нет"))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Ozon: сторож акций — поднять или снять то, что ниже пола")
    ap.add_argument("--account", choices=oz.ACCOUNTS, help="по умолчанию оба")
    ap.add_argument("--apply", action="store_true", help="отправить в кабинет (по умолчанию расчёт)")
    ap.add_argument("--wave", default="manual", help="метка прогона в журнале")
    ap.add_argument("--limit", type=int, help="не больше N правок на аккаунт за прогон")
    ap.add_argument("--csv", help="построчный расчёт в файл")
    ap.add_argument("--no-log", action="store_true", help="не писать журнал (разведка)")
    ap.add_argument("--no-tg", action="store_true", help="не слать сводку в бот")
    a = ap.parse_args()
    out = []
    for acc in ([a.account] if a.account else oz.ACCOUNTS):
        print(f"\n=== {acc} ===", flush=True)
        rows, acts, keep, keep_src = snapshot(acc)
        for r in rows:
            decide(r)
            r["status"], r["note"] = "dry", None
        known = sum(1 for r in rows if r["cogs"] is not None)
        if len(rows) >= COGS_MIN_ROWS and known < COGS_MIN_SHARE * len(rows):
            msg = (f"⚠️ *{acc}* · сторож акций Ozon: себестоимость найдена у {known} из {len(rows)} "
                   f"позиций — цепочка себеста сломана, аккаунт пропущен")
            print(msg.replace("*", ""), flush=True)
            if a.apply and not a.no_tg:
                oz.send(msg)
            continue
        _cut(rows, a.limit)
        if a.apply:
            for r in rows:
                if r["mode"] in ("ok", "ok_deferred"):
                    r["status"] = "skip"
            push(acc, rows)
            verify(acc, rows)
        if not a.no_log:
            save(rows, a.wave)
        txt = summary(acc, rows, acts, keep, keep_src, a.apply)
        print(txt.replace("*", ""), flush=True)
        if a.apply and not a.no_tg and any(r["mode"] in ("raise", "remove") for r in rows):
            oz.send(txt)
        out += rows
    if a.csv and out:
        head = ["account", "action_title", "offer_id", "name", "action_price", "max_price", "cogs",
                "cogs_source", "floor_price", "new_price", "mode", "status", "reason", "note"]
        pathlib.Path(a.csv).write_text(
            "\n".join([";".join(head)] + [";".join("" if r.get(c) is None else str(r.get(c)) for c in head)
                                          for r in out]), encoding="utf-8")
        print(f"\nпострочно → {a.csv}", flush=True)


if __name__ == "__main__":
    main()
