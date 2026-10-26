"""ops/ozon_promo_guard.py — поток: mkt
Сторож акций Ozon: вытаскивает из акций то, что торгуется ниже пола. Пара к ops/wb_promo_guard.py.

Зачем: Ozon сам, «на своё усмотрение», добавляет товары в акции ночью (проверено 23.08.2026,
между 02:58 и 12:06 МСК), отключить это нельзя. К утру в акциях могут стоять позиции с ценой,
на которой мы теряем деньги.

Что смотрит: ВСЕ акции, где аккаунт участвует, КРОМЕ белого списка робота
ops/ozon_stock_action.py («Распродажа стока», «Акция для склад…») — там цену и состав ведёт
лестница с тем же полом (решение Сергея 16.09.2026: белые пропускаем).

Решения Сергея 16.09.2026 (себестоимость — цепочкой сторожа ВБ, см. cost_map):
  пол   = (себест + общая цель прибыли: 200 ₽ до 150 ₽/ед., иначе max(300 ₽, 10 %)) / доля продавца
          (формула ВБ-сторожа, общая с лестницей — ozon_stock_action.floor_price);
  ход   = цена в действующей акции ниже пола → одна общая предельная цена на товар;
          если она не выше потолка акции, товар остаётся; иначе выходит;
          явный отказ площадки поднять цену → один общий вывод из действующих акций;
  нет себестоимости по всей цепочке → СНЯТЬ, как на ВБ.

Будущие акции проверяются по их собственному `action_price`: если лимит ниже пола,
сигнализируем и сверяем общую цену, но до начала акции цену автоматически не меняем.

Страховки: себестоимость нашлась меньше чем у COGS_MIN_SHARE участников — аккаунт пропускаем
целиком и шлём тревогу (падение цепочки себеста иначе сняло бы все акции); --limit режет число
правок за прогон. После отправки состав акции перечитывается: sent → confirmed | error.
Неполная выдача помечается явно; отсутствие в ней не подтверждает снятия (остаётся sent).

Запуск (по умолчанию расчёт, в кабинет ничего не уходит):
    ./venv/bin/python -m ops.ozon_promo_guard                        # dry-run, оба аккаунта
    ./venv/bin/python -m ops.ozon_promo_guard --csv /tmp/g.csv       # + построчно
    ./venv/bin/python -m ops.ozon_promo_guard --apply --wave morning # исполнение (только по «да»)
Журнал: oz_promo_guard_log (migrations/124_oz_promo_guard.sql).
"""
import argparse
from collections import defaultdict
import json
import math
import pathlib
import re
import sys
import time
from datetime import datetime, timezone

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from ops import ozon_stock_action as oz  # noqa: E402

COGS_MIN_SHARE = 0.5     # себест известен меньше чем у половины участников → цепочка сломана
COGS_MIN_ROWS = 20       # …проверяем только на заметной выборке
PRICE_EPS = 0.5          # цена ≥ пол − 0,5 ₽ — проходит
VERIFY_WAIT = 30         # сек до перечитки состава акции


def _issue(issues, stage, action_id, detail):
    item = dict(stage=stage, action_id=action_id, detail=detail)
    if issues is not None and item not in issues:
        issues.append(item)
    print('⚠️ Ozon guard: ' + json.dumps(item, ensure_ascii=False), flush=True)


def _save_run(account, args, rows, issues):
    fatal = any(i['stage'] == 'account' for i in issues)
    errors = sum(r.get('status') == 'error' for r in rows)
    unconfirmed = sum(args.apply and r.get('status') in ('sent', 'retry') for r in rows)
    status = 'failed' if fatal else ('partial' if issues else ('error' if errors or unconfirmed else 'ok'))
    record = dict(at=datetime.now(timezone.utc).isoformat(), account=account,
                  wave=args.wave, apply=args.apply, status=status, rows=len(rows),
                  errors=errors, unconfirmed=unconfirmed, issues=issues)
    print('run result: ' + json.dumps(record, ensure_ascii=False), flush=True)
    if not args.no_log:
        path = BASE_DIR / 'logs' / 'oz_promo_guard_runs.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + '\n')
    return status


def snapshot(account, *, issues=None):
    """Все участники небелых акций: одна строка на товар×акцию, с себестом и полом."""
    keep, keep_src = oz.keep_ratio(account)
    acts = [a for a in oz.actions(account)
            if a.get("is_participating") and not oz.is_whitelisted(a.get("title"))]
    rows = []
    observed_at = datetime.now(timezone.utc)
    for a in acts:
        try:
            members = oz.participants(account, a["id"], allow_partial=True)
        except Exception as exc:
            _issue(issues, 'snapshot', a['id'], str(exc))
            continue
        if not getattr(members, 'complete', True):
            _issue(issues, 'snapshot', a['id'], members.diagnostic)
        starts = a.get("date_start")
        ends = a.get("date_end")
        starts_at = datetime.fromisoformat(starts.replace("Z", "+00:00")) if starts else None
        ends_at = datetime.fromisoformat(ends.replace("Z", "+00:00")) if ends else None
        is_future = starts_at is not None and starts_at > observed_at
        is_current = not is_future and (ends_at is None or ends_at > observed_at)
        for p in members:
            rows.append({"account": account, "action_id": a["id"], "action_title": a.get("title"),
                         "product_id": int(p["id"]),
                         "action_price": float(p.get("action_price") or 0),
                         "max_price": float(p.get("max_action_price") or 0),
                         "stock": int(p.get("stock") or 0), "min_stock": int(p.get("min_stock") or 0),
                         "action_type": a.get("action_type"), "action_price_limit": p.get("action_price_limit"),
                         "action_start": starts, "is_future": is_future, "is_current": is_current})
    pid2 = oz.offer_of(account, sorted({r["product_id"] for r in rows})) if rows else {}
    quantities = {off: oz.bundle_quantity(off, name) for off, name in pid2.values() if off}
    cogs = cost_map(sorted(quantities), quantities=quantities)
    for r in rows:
        r["offer_id"], r["name"] = pid2.get(r["product_id"], (None, ""))
        r["cogs"], r["cogs_source"] = cogs.get(r["offer_id"], (None, "НЕТ")) if r["offer_id"] else (None, "нет offer_id")
        r["keep_ratio"] = str(keep)  # exact Decimal, JSON-safe; no four-decimal truncation
    return rows, acts, keep, keep_src


def cost_map(offers, quantities=None):
    """{offer_id: (себест, источник)} — ЦЕПОЧКА СТОРОЖА ВБ (решение Сергея 16.09.2026:
    площадки считают одинаково). Себестоимость есть, только если товар СЕЙЧАС где-то есть:
      свой склад (Звёздный/Дисквер) хватает на кратность → средневзвешенный cost_seb;
      иначе поставщик с остатком ≥ MIN_SUP_STOCK → минимальная цена (Удалённый склад МС + прайсы);
      поставщик есть, цены нет → прошлая закупка ТК; нигде нет → себестоимости нет, снимаем.
    Наборы: наличие по компонентам, цена — своя закупка комплекта, иначе сумма компонентов.
    Бандл 4248X10 → цена единицы × кратность.

    Помощники берутся из ops/wb_promo_guard.py, чтобы правило жило в одном месте. Цепочка
    ozon_stock_action.cogs_map (история отгрузок на Ozon) сюда НЕ годится: она не смотрит
    наличие и не видит прайсы — 16.09 из-за неё сняты 3203 (Колортек 75 шт по 952 ₽) и 6578.
    Лестница «Распродажи стока» остаётся на ней: там нужна цена лежащего на Ozon лота."""
    from ops import wb_promo_guard as w
    sets_, links, stock = w.sets_map(), w.links_map(), w.stock_map()
    out = {}
    for off in offers:
        raw = (off or "").strip()
        mult, m = 1, re.match(r"^(\d{4})[Xх](\d{1,2})$", raw, re.I)
        if m:
            mult = int(m.group(2))
        if quantities is not None:
            # Ozon передаёт проверенную кратность с полным названием карточки.
            # Другие площадки без этого аргумента сохраняют свой прежний расчёт.
            mult = max(1, int(quantities.get(off, 1)))
        code = w.norm_code(raw)
        if not code:
            out[off] = (None, "нет кода")
            continue
        self_rec = stock.get(code) or {}
        comps = sets_.get(code) or {code: 1}
        if len(comps) == 1:
            price, src, branch = w.unit_cost(code, links, stock, need=mult)
            if price is None:
                out[off] = (None, "нет в наличии нигде" if branch == "none" else "поставщик без цены")
            else:
                out[off] = (float(price) * mult, src + (f"_x{mult}" if mult > 1 else ""))
            continue
        # набор
        if self_rec.get("qty_own", 0) >= mult and self_rec.get("cost_own"):
            out[off] = (float(self_rec["cost_own"]) * mult, "set_own_stock" + (f"_x{mult}" if mult > 1 else ""))
            continue
        total, branches, gap = 0.0, set(), False
        for comp, qty in comps.items():
            price, src, branch = w.unit_cost(comp, links, stock, need=qty * mult)
            branches.add(branch)
            if price is None:
                gap = True
            else:
                total += price * qty
        if "none" in branches:
            out[off] = (None, "набор: компонента нет в наличии")
            continue
        own_price = self_rec.get("tc_price") or self_rec.get("supplier_min")
        if own_price:
            v, src = float(own_price), "set_own_price"
        elif not gap:
            v, src = total, "set_components"
        else:
            out[off] = (None, "набор: нет цены компонента")
            continue
        out[off] = (v * mult, src + (f"_x{mult}" if mult > 1 else ""))
    return out


def decide(r):
    """Заполняет mode; будущие акции проверяет по их лимиту без записи цены."""
    r["floor_price"] = r["new_price"] = None
    if not r.get("is_current", True) and not r.get("is_future"):
        r["mode"], r["reason"] = "ok_past", "акция завершилась"
        return r
    if r.get("is_future") and r.get("action_type") not in ("STOCK_DISCOUNT", "ELASTIC_BOOSTING"):
        r["mode"], r["reason"] = "ok_future_special", "особая механика будущей акции"
        return r
    if r["cogs"] is None:
        r["mode"] = "watch_future" if r.get("is_future") else "remove"
        r["reason"] = f"себестоимость не найдена ({r['cogs_source']})"
        return r
    r["bundle_qty"] = oz.bundle_quantity(r.get("offer_id"), r.get("name"))
    floor = oz.floor_price(float(r["cogs"]), r["keep_ratio"], r["bundle_qty"])
    r["floor_price"] = round(floor, 2)
    if r.get("is_future"):
        limit = float(r.get("action_price_limit") or 0)
        if limit <= 0 or limit < floor - PRICE_EPS:
            r["mode"], r["reason"] = "watch_future", f"лимит будущей акции {limit:.0f} < пол {floor:.0f}"
        else:
            r["mode"], r["reason"] = "ok_future", "лимит будущей акции не ниже пола"
        return r
    if r["action_price"] >= floor - PRICE_EPS:
        r["mode"], r["reason"] = "ok", "проходит по полу"
    elif r["max_price"] > 0 and math.ceil(floor) <= r["max_price"]:
        r["mode"], r["new_price"] = "raise", float(math.ceil(floor))
        r["reason"] = f"цена {r['action_price']:.0f} < пол {floor:.0f}, поднимаем"
    else:
        r["mode"] = "remove"
        r["reason"] = f"пол {floor:.0f} > потолок {r['max_price']:.0f}"
    return r


def coordinate_shared_prices(rows):
    """Одна запись общей цены на SKU, даже если он встречается в нескольких акциях."""
    by_product = defaultdict(list)
    for r in rows:
        by_product[r["product_id"]].append(r)
    for group in by_product.values():
        current = [r for r in group if r.get("is_current", True) and not r.get("is_future")]
        if len(group) < 2 or not any(r["mode"] in ("raise", "remove") for r in current):
            continue
        unsafe = (any(r.get("action_type") not in ("STOCK_DISCOUNT", "ELASTIC_BOOSTING") for r in current)
                  or any(float(r.get("max_price") or 0) <= 0 for r in current)
                  or len({round(float(r["action_price"]), 2) for r in current}) > 1
                  or len({r.get("cogs") for r in group}) > 1)
        if unsafe:
            for r in current:
                if r["mode"] in ("raise", "remove"):
                    r["mode"], r["reason"] = "review_shared", r["reason"] + " · неоднозначное пересечение акций"
            continue
        floor = current[0]["floor_price"]
        if floor is None:
            # Нет себестоимости: выводим из текущих акций; будущие требуют проверки.
            target = math.ceil(max(max(float(r.get("max_price") or 0) for r in current),
                                   max(float(r["action_price"]) for r in current))) + 1
        else:
            target = math.ceil(max(float(r["floor_price"]) for r in current))
        leader = max(current, key=lambda r: (r["max_price"], -r["action_id"]))
        for r in current:
            r["shared_price"] = float(target)
            r["shared_leader"] = r is leader
            r["mode"] = "raise" if target <= r["max_price"] else "remove"
            r["new_price"] = float(target) if r["mode"] == "raise" else None
            r["reason"] = f"общая предельная цена {target:.0f}; " + (
                "остаётся в акции" if r["mode"] == "raise" else "выходит из акции")
    return rows


def _cut(rows, limit):
    todo = defaultdict(list)
    for r in rows:
        if r["mode"] in ("raise", "remove"):
            todo[r["product_id"]].append(r)
    if limit is not None and len(todo) > limit:
        # в первую очередь самые убыточные: больше всего рублей под полом
        # (без себеста — первыми: убыток не оценить)
        ranked = sorted(todo.values(), key=lambda group: -max(
            math.inf if r["floor_price"] is None else r["floor_price"] - r["action_price"]
            for r in group))
        for group in ranked[limit:]:
            for r in group:
                r["reason"] += f" · отложено --limit {limit}"
                r["mode"] = "ok_deferred"
    return rows


def push(account, rows):
    """New Money update; removal respects action mechanics. Verify separately."""
    for aid in sorted({r["action_id"] for r in rows if r["mode"] in ("raise", "remove")
                       and not r.get("shared_price")}):
        part = [r for r in rows if r["action_id"] == aid and not r.get("shared_price")]
        up = [r for r in part if r["mode"] == "raise"]
        if up:
            try:
                result = oz.update_products(account, aid,
                    [{"product_id": r["product_id"], "action_price": r["new_price"],
                      "stock": max(r["stock"], r["min_stock"])} for r in up])
            except Exception as exc:
                for r in up:
                    r["status"], r["note"] = "error", f"update failed: {type(exc).__name__}: {exc}"
                result = None
            for r in up:
                if result is None:
                    continue
                pid = r["product_id"]
                r["note"] = str(result['warnings']) if result['warnings'] else None
                if pid in result["inactive"]:
                    r["mode"], r["status"] = "remove", "sent"
                    r["reason"] += " → update исключил товар"
                elif pid in result["active"]:
                    r["status"] = "sent"
                else:
                    r["note"] = f"подъём отклонён: {result['rejected'].get(pid, 'нет в ответе')}"
                    r["mode"], r["new_price"] = "remove", None
                    r["reason"] += " → отказ площадки, снимаем"
        rm = [r for r in part if r["mode"] == "remove" and r.get("status") != "sent"]
        if rm:
            try:
                result = oz.remove_products(account, {"id":aid,"action_type":part[0].get("action_type")},rm)
                for r in rm:
                    r["status"] = "sent" if r["product_id"] in result["inactive"] else "error"
                    if result.get("warnings"):
                        r["note"] = ((r.get("note") or "") + "; " + str(result["warnings"])).strip("; ")
                    if r["status"] == "error":
                        r["note"] = f"снятие не принято: {result['rejected'].get(r['product_id'], 'нет в ответе')}"
            except Exception as exc:
                for r in rm:
                    r["status"], r["note"] = "error", f"снять не удалось: {type(exc).__name__}: {exc}"


def push_shared(account, rows):
    """Отправляет одну общую цену на SKU и помечает все затронутые активные акции."""
    groups = defaultdict(list)
    for r in rows:
        if r.get("shared_price") and r["mode"] in ("raise", "remove"):
            groups[r["product_id"]].append(r)
    writers = defaultdict(list)
    for group in groups.values():
        leader = next(r for r in group if r["shared_leader"])
        writers[leader["action_id"]].append(leader)
    for aid, leaders in writers.items():
        try:
            result = oz.update_products(account, aid, [
                {"product_id": r["product_id"], "action_price": r["shared_price"],
                 "stock": max(r["stock"], r["min_stock"])} for r in leaders])
        except Exception as exc:
            for leader in leaders:
                for r in groups[leader["product_id"]]:
                    r["status"], r["note"] = "error", f"общая цена не отправлена: {type(exc).__name__}: {exc}"
                    # Тайм-аут не доказывает отказ: запасная запись без сверки опасна.
                    r["shared_write_failed"] = False
                    r["shared_request_accepted"] = False
            continue
        for leader in leaders:
            pid = leader["product_id"]
            accepted = pid in result["active"] or pid in result["inactive"]
            for r in groups[pid]:
                r["status"] = "sent" if accepted else "error"
                r["shared_write_failed"] = not accepted
                r["shared_request_accepted"] = accepted
                r["note"] = (str(result["warnings"]) if result["warnings"] else None) if accepted else (
                    f"общая цена отклонена: {result['rejected'].get(pid, 'нет в ответе')}")


def execute(account, rows, *, issues=None):
    push_shared(account, rows)
    push(account, rows)
    verify(account, rows, issues=issues)
    verify_shared_caps(account, rows, issues=issues)
    shared_groups = defaultdict(list)
    for r in rows:
        if r.get("shared_price") and r.get("is_current", True):
            shared_groups[r["product_id"]].append(r)
    retry_shared = []
    for group in shared_groups.values():
        if any(r.get("shared_cap_mismatch") for r in group):
            # Другое правило могло уже перезаписать общую цену: не устраивать цикл.
            continue
        if not any(r.get("shared_write_failed") or r.get("below_floor_verified") for r in group):
            continue
        # Как и для одиночной акции: отказ поднять цену или подтверждённая цена
        # ниже пола ведёт к исключению. Одна запасная запись на весь SKU.
        target = math.ceil(max(max(float(r["max_price"]) for r in group),
                               max(float(r.get("floor_price") or 0) for r in group),
                               max(float(r["action_price"]) for r in group))) + 1
        for r in group:
            r["shared_price"], r["mode"], r["new_price"] = float(target), "remove", None
            r["status"], r["verification_failed"], r["shared_write_failed"] = "retry", False, False
            r["below_floor_verified"] = False
            r["reason"] += " → общая цена не подтверждена, выводим из текущих акций"
            retry_shared.append(r)
    if retry_shared:
        push_shared(account, retry_shared)
        verify(account, retry_shared, issues=issues)
        verify_shared_caps(account, retry_shared, issues=issues)
    failed = [r for r in rows if r["mode"] == "raise" and r.get("status") == "error"
              and r.get("verification_failed") and not r.get("shared_price")]
    for r in failed:
        r["mode"], r["status"] = "remove", "retry"
        r["reason"] += " → фактическая цена после update ниже пола, снимаем"
    if failed:
        push(account,failed)
        verify(account,failed,issues=issues)


def verify(account, rows, *, issues=None):
    """Перечитываем состав тронутых акций: снятого там нет, поднятый стоит не ниже пола."""
    sent = [r for r in rows if r.get("status") == "sent"]
    if not sent:
        return
    time.sleep(VERIFY_WAIT)
    for aid in sorted({r["action_id"] for r in sent}):
        try:
            members = oz.participants(account, aid, allow_partial=True)
            complete = getattr(members, 'complete', True)
            now = {int(p["id"]): float(p.get("action_price") or 0) for p in members}
            if not complete:
                _issue(issues, 'verify', aid, members.diagnostic)
        except Exception as e:                                          # noqa: BLE001
            _issue(issues, 'verify', aid, str(e))
            for r in sent:
                if r["action_id"] == aid:
                    r["note"] = ((r.get("note") or "") + f" · сверка не удалась: {e}").strip(" ·")
            continue
        for r in sent:
            if r["action_id"] != aid:
                continue
            pid = r["product_id"]
            if not complete and pid not in now:
                # Neither removal nor a failed raise can be inferred from missing
                # IDs in partial Ozon results; keep the request unconfirmed.
                r['note'] = ((r.get('note') or '') + ' · неполная сверка: отсутствие не подтверждено').strip(' ·')
                continue
            if r["mode"] == "raise" and pid not in now:
                r["mode"] = "remove"
                r["reason"] += " → перечитка подтвердила отсутствие в акции"
            if r["mode"] == "remove":
                good = pid not in now
            else:
                good = pid in now and now[pid] >= r["new_price"] - PRICE_EPS
            r["status"] = "confirmed" if good else "error"
            if not good:
                r["verification_failed"] = r["mode"] == "raise"
                r["below_floor_verified"] = pid in now and r.get("floor_price") is not None and (
                    now[pid] < r["floor_price"] - PRICE_EPS)
                r["note"] = ((r.get("note") or "") + f" · сверка: в акции цена {now.get(pid, 'нет')}").strip(" ·")


def read_shared_caps(account, product_ids):
    """Читает общую предельную цену только затронутых товаров."""
    caps = {}
    ids = sorted(set(product_ids))
    for start in range(0, len(ids), 100):
        batch = ids[start:start + 100]
        data = oz._req(account, "POST", "/v5/product/info/prices", {
            "cursor": "", "limit": 1000,
            "filter": {"product_id": batch, "visibility": "ALL"}})
        for item in data.get("items") or []:
            pid = int(item["product_id"])
            if pid in batch:
                caps[pid] = float((item.get("price") or {})["price"])
    return caps


def verify_shared_caps(account, rows, *, issues=None):
    groups = defaultdict(list)
    for r in rows:
        if r.get("shared_price") and r.get("shared_request_accepted"):
            groups[r["product_id"]].append(r)
    if not groups:
        return
    try:
        caps = read_shared_caps(account, groups)
    except Exception as exc:
        _issue(issues, "verify_shared_cap", None, f"{type(exc).__name__}: {exc}")
        caps = {}
    for pid, group in groups.items():
        cap = caps.get(pid)
        if cap is None:
            for r in group:
                if r["status"] == "confirmed":
                    r["status"] = "sent"
                r["note"] = ((r.get("note") or "") + " · общая цена не подтверждена").strip(" ·")
            continue
        target = float(group[0]["shared_price"])
        if abs(cap - target) <= PRICE_EPS:
            continue
        for r in group:
            r["status"] = "error"
            r["shared_cap_mismatch"] = True
            r["note"] = ((r.get("note") or "") + f" · общая цена {cap:.0f} вместо {target:.0f}").strip(" ·")


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
             + (f"  · будущих без сигнала: {m['ok_future']}" if m.get("ok_future") else "")
             + (f"  · лимит будущей акции ниже пола: {m['watch_future']}" if m.get("watch_future") else "")
             + (f"  · проверить пересечение: {m['review_shared']}" if m.get("review_shared") else "")
             + (f"  · отложено: {m['ok_deferred']}" if m.get("ok_deferred") else "")]
    for r in [x for x in rows if x["mode"] == "watch_future"][:5]:
        floor_text = f"{r['floor_price']:.0f} ₽" if r.get("floor_price") is not None else "не найден"
        cap_text = f"{r['current_cap']:.0f} ₽" if r.get("current_cap") is not None else "не прочитана"
        lines.append(f"  – будущая акция: {r.get('offer_id') or r['product_id']} · "
                     f"лимит {float(r.get('action_price_limit') or 0):.0f} ₽, "
                     f"пол {floor_text}, общая цена {cap_text}")
    for r in [x for x in rows if x["mode"] == "review_shared"][:3]:
        lines.append(f"  – проверить пересечение: {r.get('offer_id') or r['product_id']}")
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


def _run_account(acc, a, issues, rows):
    print(f"\n=== {acc} ===", flush=True)
    observed, acts, keep, keep_src = snapshot(acc, issues=issues)
    rows.extend(observed)
    for r in rows:
        decide(r)
        r["status"], r["note"] = "dry", None
    watch = [r for r in rows if r["mode"] == "watch_future"]
    if watch:
        try:
            caps = read_shared_caps(acc, [r["product_id"] for r in watch])
        except Exception as exc:
            _issue(issues, "future_cap", None, f"{type(exc).__name__}: {exc}")
            caps = {}
        for r in watch:
            r["current_cap"] = caps.get(r["product_id"])
            if r["current_cap"] is not None and r["max_price"] > 0:
                r["future_expectation"] = ("exit_if_cap_unchanged" if r["current_cap"] > r["max_price"]
                                           else "may_enter_below_floor")
                r["reason"] += (" · при сохранении общей цены ожидается выход" if
                                r["future_expectation"] == "exit_if_cap_unchanged" else
                                " · может войти ниже пола")
    coordinate_shared_prices(rows)
    review = {r["product_id"] for r in rows if r["mode"] == "review_shared"}
    if review:
        _issue(issues, "shared_review", None, {"products": len(review)})
    known = sum(1 for r in rows if r["cogs"] is not None)
    if len(rows) >= COGS_MIN_ROWS and known < COGS_MIN_SHARE * len(rows):
        msg = (f"⚠️ *{acc}* · сторож акций Ozon: себестоимость найдена у {known} из {len(rows)} "
               f"позиций — цепочка себеста сломана, аккаунт пропущен")
        print(msg.replace("*", ""), flush=True)
        _issue(issues, 'cost_coverage', None, msg)
        return msg
    _cut(rows, a.limit)
    if a.apply:
        for r in rows:
            if r["mode"] in ("ok", "ok_future", "ok_future_special", "ok_past", "ok_deferred"):
                r["status"] = "skip"
            elif r["mode"] == "watch_future":
                r["status"] = "watch"
            elif r["mode"] == "review_shared":
                r["status"] = "manual"
        execute(acc, rows, issues=issues)
    if not a.no_log:
        save(rows, a.wave)
    txt = summary(acc, rows, acts, keep, keep_src, a.apply)
    print(txt.replace("*", ""), flush=True)
    return txt


def main():
    ap = argparse.ArgumentParser(description="Ozon: сторож акций — поднять или снять то, что ниже пола")
    ap.add_argument("--account", choices=oz.ACCOUNTS, help="по умолчанию оба")
    ap.add_argument("--apply", action="store_true", help="отправить в кабинет (по умолчанию расчёт)")
    ap.add_argument("--wave", default="manual", help="метка прогона в журнале")
    ap.add_argument("--limit", type=int, help="не больше N товаров с правками на аккаунт за прогон")
    ap.add_argument("--csv", help="построчный расчёт в файл")
    ap.add_argument("--no-log", action="store_true", help="не писать журнал (разведка)")
    ap.add_argument("--no-tg", action="store_true", help="не слать сводку в бот")
    a = ap.parse_args()
    out = []
    failed = False
    for acc in ([a.account] if a.account else oz.ACCOUNTS):
        issues, rows = [], []
        txt = f'{acc} · сторож акций Ozon'
        try:
            txt = _run_account(acc, a, issues, rows)
        except Exception as exc:
            _issue(issues, 'account', None, f'{type(exc).__name__}: {exc}')
        if a.apply and not a.no_tg and (issues or any(r.get('mode') in ('raise', 'remove', 'watch_future', 'review_shared') for r in rows)):
            notice = txt
            if issues:
                # Keep raw cursor/exception details in the local journal; plain
                # coverage counts cannot break the existing Telegram Markdown.
                notice += '\n⚠️ Проверка неполная; подробности в журнале запуска.'
                for issue in issues[:8]:
                    detail = issue['detail']
                    if isinstance(detail, dict):
                        notice += (f"\nАкция {issue['action_id']}: получено {detail.get('received')}, "
                                   f"total {detail.get('reported_totals')}")
                    else:
                        notice += f"\nАкция {issue['action_id'] or '—'}: ошибка чтения или обработки."
            try:
                oz.send(notice[:3900])
            except Exception as exc:
                _issue(issues, 'notification', None, type(exc).__name__)
        try:
            failed |= _save_run(acc, a, rows, issues) != 'ok'
        except Exception as exc:
            failed = True
            print(f'⚠️ {acc}: run journal failed: {type(exc).__name__}', flush=True)
        out += rows
    if a.csv and out:
        head = ["account", "action_id", "action_title", "product_id", "offer_id", "name",
                "is_current", "is_future", "action_start", "action_price", "action_price_limit",
                "current_cap", "future_expectation",
                "max_price", "cogs", "cogs_source", "floor_price", "new_price", "shared_price",
                "mode", "status", "reason", "note"]
        pathlib.Path(a.csv).write_text(
            "\n".join([";".join(head)] + [";".join("" if r.get(c) is None else str(r.get(c)) for c in head)
                                          for r in out]), encoding="utf-8")
        print(f"\nпострочно → {a.csv}", flush=True)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
