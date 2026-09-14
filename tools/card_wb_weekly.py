# поток: card
"""tools/card_wb_weekly.py — еженедельная сводка по карточкам ВБ Наталии в бот PRC.

Два блока, оба только чтение площадки:

1. КОРЗИНА. ВБ держит удалённую карточку в корзине и даёт стереть её вручную не раньше чем
   через 30 дней после удаления — и продаёт её, пока есть остаток. ТК такие карточки уже не
   обновляет, поэтому по каждой показываем остаток FBS (склад продавца, marketplace-api) и FBO
   (склады ВБ, `wb_stocks`) и срок: «30 дней прошло — можно удалять» / «ждать до …» / «есть
   остаток — продаётся». Правило Сергея 14.09.2026: остаток на складе ВБ пусть распродаётся.

2. ПИСЬМО В ПОДДЕРЖКУ по черновикам (несозданным карточкам), которые ВБ отбил за «телефонный
   номер в Наименовании»: это номер модели картриджа, а не телефон. Берём
   `content/v2/cards/error/list`; артикулы, по которым карточка всё-таки создана
   (`wb_cards_live`), в письмо не идут — там спорить не о чем. Отдельное письмо на кабинет:
   обращение пишется из кабинета того юрлица.

Остаток FBS читается токеном WB_TOKEN_RETURNS_ACC* — скоуп «Маркетплейс» есть только у него.

Запуск:  venv/bin/python -m tools.card_wb_weekly           (сухой прогон, печатает тексты)
         venv/bin/python -m tools.card_wb_weekly --send    (отправить Наталии)
"""
import argparse
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


def _fbs(token_env, chrt_ids):
    """chrtID → остаток на складах продавца. Нет ответа по chrtID = None (неизвестно, не ноль)."""
    tok = os.getenv(token_env)
    if not tok or not chrt_ids:
        return {}
    h = {"Authorization": tok, "Content-Type": "application/json"}
    r = requests.get(f"{MP}/warehouses", headers=h, timeout=60)
    r.raise_for_status()
    out = {}
    for w in r.json():
        for i in range(0, len(chrt_ids), 1000):
            for s in _post(f"{MP}/stocks/{w['id']}", h, {"chrtIds": chrt_ids[i:i + 1000]}).get("stocks") or []:
                out[s["chrtId"]] = out.get(s["chrtId"], 0) + (s.get("amount") or 0)
    return out


def _fbo(acc, nm_ids):
    if not nm_ids:
        return {}
    rows = db.query("""SELECT nm_id, sum(quantity) q FROM wb_stocks
                        WHERE account=%s AND nm_id = ANY(%s)
                          AND captured_at = (SELECT max(captured_at) FROM wb_stocks WHERE account=%s)
                        GROUP BY 1""", (acc, nm_ids, acc))
    return {r["nm_id"]: int(r["q"] or 0) for r in rows}


def trash_block(acc, label, token_env, today):
    cards = _trash(acc)
    if not cards:
        return f"{label}: корзина пуста."
    chrt = [s["chrtID"] for c in cards for s in c.get("sizes") or []]
    fbs = _fbs(token_env, chrt)
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
            verdict = "⚠ есть остаток на своём складе — карточку можно купить, обнулить"
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
    return f"{label}: в корзине {len(cards)}\n" + "\n".join(lines)


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


def support_letter(label, drafts):
    codes = "\n".join(f"{vc} (попытка создания {d[8:10]}.{d[5:7]}.{d[:4]})" for vc, d in drafts)
    return (f"Письмо в поддержку ВБ — кабинет «{label}» (копировать целиком):\n\n"
            "Здравствуйте! Карточки товаров не создаются с ошибкой «Запрещено указывать телефонные "
            "номера в поле Наименование». В наименовании нет телефонного номера: цифры, которые "
            "система принимает за телефон, — это номер модели товара (картриджа) или совместимой "
            "модели принтера, обязательная часть названия, без неё покупатель не найдёт товар. "
            "Просим снять ограничение и пропустить карточки на создание.\n\n"
            f"Артикулы продавца ({len(drafts)}):\n{codes}")


def _tg(text):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": NATALIA_ID, "text": text[:3900],
                            "disable_web_page_preview": "true"}, timeout=120)
    if not r.ok:
        raise RuntimeError(f"telegram: {r.status_code} {r.text[:200]}")


def run(send=False):
    today = datetime.now(timezone(timedelta(hours=3))).date()
    msgs = ["Карточки ВБ в корзине (еженедельно). ВБ даёт удалить вручную через 30 дней "
            "после удаления и при нулевом остатке.\n\n"
            + "\n\n".join(trash_block(acc, label, tok, today) for acc, (label, tok) in ACCOUNTS.items())]
    for acc, (label, _) in ACCOUNTS.items():
        drafts = phone_drafts(acc)
        print(f"{label}: черновиков с «телефоном» без созданной карточки {len(drafts)}", flush=True)
        if drafts:
            msgs.append(support_letter(label, drafts))
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
    run(send=ap.parse_args().send)


if __name__ == "__main__":
    main()
