# поток: card
"""tools/card_wb_weekly.py — еженедельная сводка по карточкам ВБ Наталии в бот PRC.

Два блока. Чтение площадки везде, ЕДИНСТВЕННАЯ запись — обнуление остатка FBS (см. ниже).

1. КОРЗИНА. ВБ держит удалённую карточку в корзине и даёт стереть её вручную не раньше чем
   через 30 дней после удаления — и продаёт её, пока есть остаток. ТК такие карточки уже не
   обновляет, поэтому по каждой показываем остаток FBS (склад продавца, marketplace-api) и FBO
   (склады ВБ, `wb_stocks`) и срок: «30 дней прошло — можно удалять» / «ждать до …» / «есть
   остаток — продаётся». Правило Сергея 14.09.2026: остаток на складе ВБ пусть распродаётся.

   ЗАПИСЬ: с `--zero-stock` (разрешение Сергея 16.09.2026) остаток FBS у карточки корзины
   ставится в 0 — карточка в корзине покупаемой быть не должна. Ставим складу, на котором
   остаток лежит, партиями по 100, и проверяем ПЕРЕЧИТКОЙ, а не кодом ответа. Склад ВБ (FBO)
   так не обнулить, оттуда только вывоз, — его не трогаем. Цены и контент не трогаем вообще.

2. ЗАДАЧА ДЛЯ ТК по черновикам (несозданным карточкам), которые ВБ отбил за «телефонный
   номер в Наименовании»: это номер модели картриджа, а не телефон. Берём
   `content/v2/cards/error/list`; артикулы, по которым карточка всё-таки создана
   (`wb_cards_live`), в задачу не идут — они и так созданы. Отдельная задача на кабинет.
   Поддержка ВБ 20.09.2026: название править НЕ нужно, отбивку снимает замена ГЛАВНОГО фото
   на белый фон без надписей. До этого тут было письмо в поддержку — больше не нужно.

Остаток FBS читается токеном WB_TOKEN_RETURNS_ACC* — скоуп «Маркетплейс» есть только у него.

Запуск:  venv/bin/python -m tools.card_wb_weekly                    (сухой прогон, ничего не пишет)
         venv/bin/python -m tools.card_wb_weekly --send --zero-stock (крон: обнулить + отправить)
"""
import argparse
import collections
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))
load_dotenv(BASE_DIR / ".env")

from core import db                                     # noqa: E402
from collectors.wb_card_content import _content_token   # noqa: E402

TG_TOKEN = os.getenv("TG_PRC_BOT_TOKEN", "").strip()
NATALIA_ID = os.getenv("TG_PRC_NATALIA_ID", "1231747786").strip()

ACCOUNTS = {"wb_acc1": ("Цифровой", "WB_TOKEN_RETURNS_ACC1"),
            "wb_acc2": ("Дисквэр", "WB_TOKEN_RETURNS_ACC2")}
CONTENT = "https://content-api.wildberries.ru/content/v2"
TRASH_URL = f"{CONTENT}/get/cards/trash"
ERRORS_URL = f"{CONTENT}/cards/error/list"
MP = "https://marketplace-api.wildberries.ru/api/v3"
TRASH_DAYS = 30
PHONE_MARK = "телефонные номера"


def _post(url, headers, body):
    for _ in range(5):
        r = requests.post(url, headers=headers, json=body, timeout=120)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "20")) + 1)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"{url}: 429 пять раз подряд")


def _trash(acc):
    """Все карточки корзины целиком (нужны sizes → chrtID для остатка FBS)."""
    h = {"Authorization": _content_token(acc), "Content-Type": "application/json"}
    cards, cursor = [], {"limit": 100}
    while True:
        data = _post(TRASH_URL, h, {"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}})
        page = data.get("cards") or []
        cards += page
        cur = data.get("cursor") or {}
        if len(page) < 100:
            return cards
        cursor = {"limit": 100, "trashedAt": cur.get("trashedAt"), "nmID": cur.get("nmID")}


def _mp_headers(token_env):
    tok = os.getenv(token_env)
    return {"Authorization": tok, "Content-Type": "application/json"} if tok else None


def _fbs(token_env, chrt_ids):
    """Остаток на складах продавца: (итог по chrtID, подробно по складам).

    Нет ответа по chrtID = None (неизвестно, а не ноль). Подробность нужна для обнуления:
    остаток ставится складу, на котором он лежит.
    """
    h = _mp_headers(token_env)
    if not h or not chrt_ids:
        return {}, {}
    r = requests.get(f"{MP}/warehouses", headers=h, timeout=60)
    r.raise_for_status()
    out, per_wh = {}, {}
    for w in r.json():
        for i in range(0, len(chrt_ids), 1000):
            for s in _post(f"{MP}/stocks/{w['id']}", h, {"chrtIds": chrt_ids[i:i + 1000]}).get("stocks") or []:
                out[s["chrtId"]] = out.get(s["chrtId"], 0) + (s.get("amount") or 0)
                if s.get("amount"):
                    per_wh[(w["id"], w.get("name"), s["chrtId"])] = s["amount"]
    return out, per_wh


def _zero_fbs(token_env, wh_id, skus):
    """Ставит остаток 0 указанным баркодам на складе. Успех проверяется перечиткой, не кодом."""
    h = _mp_headers(token_env)
    r = requests.put(f"{MP}/stocks/{wh_id}", headers=h,
                     json={"stocks": [{"sku": s, "amount": 0} for s in skus]}, timeout=120)
    return r.status_code, r.text[:200]


def _fbo(acc, nm_ids):
    if not nm_ids:
        return {}
    rows = db.query("""SELECT nm_id, sum(quantity) q FROM wb_stocks
                        WHERE account=%s AND nm_id = ANY(%s)
                          AND captured_at = (SELECT max(captured_at) FROM wb_stocks WHERE account=%s)
                        GROUP BY 1""", (acc, nm_ids, acc))
    return {r["nm_id"]: int(r["q"] or 0) for r in rows}


def zero_trash_fbs(acc, label, token_env, cards, per_wh, apply):
    """Карточка в корзине не должна быть покупаемой: остаток FBS ставим 0 (разрешение 16.09.2026).

    Склад ВБ (FBO) так не обнулить — оттуда только вывоз, поэтому его не трогаем.
    """
    sku = {s["chrtID"]: (s.get("skus") or [None])[0]
           for c in cards for s in c.get("sizes") or []}
    vc = {s["chrtID"]: c["vendorCode"] for c in cards for s in c.get("sizes") or []}
    todo = collections.defaultdict(list)
    for (wh_id, wh_name, chrt), amount in per_wh.items():
        if sku.get(chrt):
            todo[(wh_id, wh_name)].append((chrt, sku[chrt], amount))
    if not todo:
        return []
    out = []
    for (wh_id, wh_name), items in todo.items():
        if not apply:
            out.append(f"{label}: НЕ обнулял (сухой прогон) — {wh_name}: "
                       + ", ".join(f"{vc[c]} ×{a}" for c, _s, a in items))
            continue
        for i in range(0, len(items), 100):
            part = items[i:i + 100]
            code, body = _zero_fbs(token_env, wh_id, [s for _c, s, _a in part])
            time.sleep(1)
            left, _ = _fbs(token_env, [c for c, _s, _a in part])   # перечитка: HTTP 200 не доказательство
            bad = [f"{vc[c]}={left.get(c)}" for c, _s, _a in part if left.get(c)]
            out.append(f"{label}: обнулено на складе {wh_name} — {len(part)} поз. "
                       f"({', '.join(f'{vc[c]} было {a}' for c, _s, a in part)})"
                       + (f"; ⚠ остались: {', '.join(bad)} (HTTP {code} {body})" if bad else "; перечитка: 0"))
    return out


def trash_block(acc, label, token_env, today, apply=False):
    cards = _trash(acc)
    if not cards:
        return f"{label}: корзина пуста.", []
    chrt = [s["chrtID"] for c in cards for s in c.get("sizes") or []]
    fbs, per_wh = _fbs(token_env, chrt)
    zeroed = zero_trash_fbs(acc, label, token_env, cards, per_wh, apply)
    if zeroed and apply:
        fbs, per_wh = _fbs(token_env, chrt)
    fbo = _fbo(acc, [c["nmID"] for c in cards])
    lines = []
    for c in sorted(cards, key=lambda c: c.get("trashedAt") or ""):
        since = datetime.fromisoformat(c["trashedAt"].replace("Z", "+00:00")).date()
        days = (today - since).days
        sizes = [s["chrtID"] for s in c.get("sizes") or []]
        f_fbs = None if any(x not in fbs for x in sizes) else sum(fbs[x] for x in sizes)
        f_fbo = fbo.get(c["nmID"], 0)
        stock = f"FBS {'?' if f_fbs is None else f_fbs}, склад ВБ {f_fbo}"
        if f_fbs is None:
            verdict = "⚠ остаток FBS не прочитан — проверить в ЛК"
        elif f_fbs > 0:
            verdict = "⚠ остаток FBS не обнулился — проверить в ЛК"
        elif f_fbo > 0:
            verdict = "продаётся остаток со склада ВБ, удалить пока нельзя"
        elif days >= TRASH_DAYS:
            # 14.09.2026 ВБ не дал удалить 3900del/3901del при 54 днях и нулевых остатках —
            # причина неизвестна, поэтому «пробовать», а не «можно».
            verdict = "30 дней прошло — пробовать удалить; не даёт — в поддержку"
        else:
            verdict = f"ждать до {since + timedelta(days=TRASH_DAYS):%d.%m}"
        lines.append(f"• {c['vendorCode']} (nm {c['nmID']}) — в корзине с {since:%d.%m}, "
                     f"{days} дн.; {stock}; {verdict}")
    return f"{label}: в корзине {len(cards)}\n" + "\n".join(lines), zeroed


def phone_drafts(acc):
    """Черновики, отбитые за «телефон в Наименовании», по которым карточка так и не создана."""
    h = {"Authorization": _content_token(acc), "Content-Type": "application/json"}
    items, cursor = [], {"limit": 100}
    while True:
        data = _post(ERRORS_URL, h, {"cursor": cursor, "order": {"ascending": False}}).get("data") or {}
        items += data.get("items") or []
        cur = data.get("cursor") or {}
        if not cur.get("next"):
            break
        cursor = {"limit": 100, "updatedAt": cur.get("updatedAt"), "batchUUID": cur.get("batchUUID")}
    latest = {}
    for it in items:
        for vc in it.get("vendorCodes") or []:
            if any(PHONE_MARK in e for e in (it.get("errors") or {}).get(vc, [])):
                if vc not in latest or it["updatedAt"] > latest[vc]:
                    latest[vc] = it["updatedAt"]
    if not latest:
        return []
    live = {r["vendor_code"] for r in db.query(
        "SELECT vendor_code FROM wb_cards_live WHERE account=%s AND vendor_code = ANY(%s)",
        (acc, list(latest)))}
    return sorted((vc, latest[vc][:10]) for vc in latest if vc not in live)


def photo_task(label, drafts):
    """Задание ТК: ответ поддержки ВБ от 20.09.2026 — отбивку про «телефонный номер» снимает
    замена ГЛАВНОГО фото на белый фон без надписей. Спорить письмом больше не нужно."""
    codes = "\n".join(f"{vc} (попытка создания {d[8:10]}.{d[5:7]}.{d[:4]})" for vc, d in drafts)
    return (f"Задача для ТК — кабинет «{label}», карточки не создаются на ВБ.\n\n"
            "ВБ отбивает их с ошибкой «Запрещено указывать телефонные номера в поле Наименование» "
            "(на самом деле это номер модели). Поддержка ВБ ответила 20.09.2026: название менять "
            "не нужно, отбивка снимается заменой ГЛАВНОГО фото — нужно фото на белом фоне без "
            "надписей и наклеек. Просьба заменить главное фото и выгрузить карточки заново.\n\n"
            f"Артикулы продавца ({len(drafts)}):\n{codes}")


def _tg(text):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": NATALIA_ID, "text": text[:3900],
                            "disable_web_page_preview": "true"}, timeout=120)
    if not r.ok:
        raise RuntimeError(f"telegram: {r.status_code} {r.text[:200]}")


def run(send=False, zero=False):
    today = datetime.now(timezone(timedelta(hours=3))).date()
    blocks, zeroed = [], []
    for acc, (label, tok) in ACCOUNTS.items():
        b, z = trash_block(acc, label, tok, today, apply=zero)
        blocks.append(b); zeroed += z
    head = ("Карточки ВБ в корзине (еженедельно). ВБ даёт удалить вручную через 30 дней "
            "после удаления и при нулевом остатке. Остаток на своём складе (FBS) у карточек "
            "корзины обнуляем автоматически.\n\n")
    if zeroed:
        head += "Обнуление остатка FBS:\n" + "\n".join(f"• {z}" for z in zeroed) + "\n\n"
    msgs = [head + "\n\n".join(blocks)]
    for acc, (label, _) in ACCOUNTS.items():
        drafts = phone_drafts(acc)
        print(f"{label}: черновиков с «телефоном» без созданной карточки {len(drafts)}", flush=True)
        if drafts:
            msgs.append(photo_task(label, drafts))
    if not send:
        for m in msgs:
            print("-" * 60 + "\n" + m, flush=True)
        print(f"[сухой прогон] адресат {NATALIA_ID}, сообщений {len(msgs)}", flush=True)
        return
    if not TG_TOKEN:
        raise RuntimeError("нет TG_PRC_BOT_TOKEN — отправлять нечем")
    for m in msgs:
        _tg(m)
        time.sleep(1)
    print(f"{today}: отправлено Наталии сообщений {len(msgs)}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="реально отправить (иначе сухой прогон)")
    ap.add_argument("--zero-stock", action="store_true",
                    help="обнулять остаток FBS у карточек корзины (разрешение 16.09.2026)")
    a = ap.parse_args()
    run(send=a.send, zero=a.zero_stock)


if __name__ == "__main__":
    main()
