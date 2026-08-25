#!/usr/bin/env python3
# поток: mkt
"""ops/wb_stock_guard.py — не платить за клики по карточке, которую нельзя купить.

ЗАЧЕМ. Замер 24.08.2026 показал: 234 карточки, продававшиеся с 01.06, покупатель купить не мог,
и 170 из них продолжали откручивать рекламу (742 ₽/нед, у 101 ставка выше пола). Реклама
недоступной карточки — это оплаченный клик в пустоту: покупатель приходит и видит «нет в наличии».
Обратная ошибка не менее дорогая: когда остаток вернулся, карточка остаётся на полу и не получает
показов, хотя товар лежит на складе.

ЧТО ДЕЛАЕТ. Два симметричных правила по живому остатку карточки:
  нет остатка  → ставку в пол (author='stock-guard', прежняя ставка сохраняется в wb_bid_log.old_cpc)
  остаток есть → вернуть ту ставку, с которой мы её опустили (ровно ту, не пересчитанную)

ИСТОЧНИК ПРАВДЫ — `totalQuantity` карточки с публичного card.wb.ru, то есть то, что видит
покупатель. Витрина `wb_stocks` для этого НЕ годится: она FBO-only (карточка может продаваться
с ФБС при нулевом FBO) — грабли зафиксированы в брифе 17.08.2026.

ВОЗВРАТ ТОЛЬКО СВОЕГО. Поднимаем обратно исключительно те позиции, которые сами же и опустили
(последняя правка по номенклатуре — наша, author='stock-guard'). Если после нас ставку трогал
Рой, лестница или человек — не вмешиваемся: их решение свежее нашего.

По умолчанию DRY-RUN. Живая запись в ВБ только с --apply.

Запуск:
  ./venv/bin/python -m ops.wb_stock_guard                      # dry-run acc1
  ./venv/bin/python -m ops.wb_stock_guard --account wb_acc2
  ./venv/bin/python -m ops.wb_stock_guard --apply --notify
"""
import sys
import time
import argparse
import pathlib

import requests

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from ops.wb_bid_ladder import FLOOR, apply_step  # noqa: E402

V4 = "https://card.wb.ru/cards/v4/detail"
BATCH = 100


def live_stock(nms):
    """{nm_id: totalQuantity} — то, что видит покупатель. Неответившие батчи пропускаем:
    молчание card.wb.ru не должно читаться как «остатка нет» и ронять ставки в пол."""
    out = {}
    for i in range(0, len(nms), BATCH):
        chunk = nms[i:i + BATCH]
        for attempt in range(3):
            try:
                r = requests.get(V4, params={"appType": 1, "curr": "rub", "dest": -1257786,
                                             "spp": 30, "nm": ";".join(str(n) for n in chunk)},
                                 timeout=45)
                if r.status_code == 200:
                    for p in (r.json().get("products") or []):
                        out[int(p["id"])] = p.get("totalQuantity", 0)
                    break
            except Exception as exc:
                if attempt == 2:
                    print(f"  [!] батч {i // BATCH + 1} не ответил: {str(exc)[:80]}", flush=True)
            time.sleep(3)
        time.sleep(0.25)
    return out


def paying(account):
    """Позиции, за которые мы сейчас платим выше пола."""
    return db.query(
        """select nm_id, advert_id, cpc from wb_bid_override
             where account=%s and advert_id is not null and cpc > %s""", (account, FLOOR))


def ours_on_floor(account):
    """Позиции, которые в пол опустили МЫ и с тех пор их никто не трогал.

    Берём последнюю по времени применённую правку каждой номенклатуры: если её автор
    'stock-guard' — возврат наш; если после нас писал Рой или человек, позиция не наша.
    """
    return db.query(
        """with last as (
             select distinct on (nm_id) nm_id, author, old_cpc, advert_id
               from wb_bid_log
              where account=%s and applied
              order by nm_id, ts desc)
           select l.nm_id, coalesce(o.advert_id, l.advert_id) advert_id, l.old_cpc
             from last l join wb_bid_override o
               on o.account=%s and o.nm_id=l.nm_id
            where l.author='stock-guard' and l.old_cpc > %s and o.cpc <= %s""",
        (account, account, FLOOR, FLOOR))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--account', default='wb_acc1')
    ap.add_argument('--apply', action='store_true', help='живая запись в ВБ')
    ap.add_argument('--notify', action='store_true', help='итог в телеграм')
    a = ap.parse_args()

    pay = paying(a.account)
    back = ours_on_floor(a.account)
    nms = sorted({r['nm_id'] for r in pay} | {r['nm_id'] for r in back})
    if not nms:
        print(f"{a.account}: нечего проверять")
        return
    print(f"{a.account}: проверяю {len(nms)} номенклатур "
          f"(платим выше пола {len(pay)}, наших на полу {len(back)})", flush=True)
    stock = live_stock(nms)
    print(f"  card.wb.ru ответил по {len(stock)} из {len(nms)}")

    plan = []
    for r in pay:
        q = stock.get(r['nm_id'])
        if q is None or q > 0:          # нет ответа — не трогаем
            continue
        plan.append({'nm_id': r['nm_id'], 'advert_id': r['advert_id'],
                     'old_cpc': float(r['cpc']), 'new_cpc': FLOOR, 'rule': 'в пол'})
    for r in back:
        q = stock.get(r['nm_id'])
        if not q:
            continue
        plan.append({'nm_id': r['nm_id'], 'advert_id': r['advert_id'],
                     'old_cpc': FLOOR, 'new_cpc': round(float(r['old_cpc']), 2), 'rule': 'вернуть'})

    down = [r for r in plan if r['rule'] == 'в пол']
    up = [r for r in plan if r['rule'] == 'вернуть']
    saved = sum(r['old_cpc'] - FLOOR for r in down)
    print(f"\nПЛАН · {a.account}")
    print(f"  нет остатка → в пол : {len(down):>4} SKU в {len({r['advert_id'] for r in down})} кампаниях"
          f" (снимаем {saved:.2f} ₽ ставки суммарно)")
    print(f"  остаток вернулся    : {len(up):>4} SKU в {len({r['advert_id'] for r in up})} кампаниях")
    if not plan:
        print("  делать нечего")
        return
    if not a.apply:
        print("\nDRY-RUN. В ВБ не отправлено ничего. Живая запись: добавить --apply")
        return

    ok, bad = apply_step(a.account, plan, "stock guard", author='stock-guard')
    print(f"\nОТПРАВЛЕНО В ВБ: применено {ok}, отклонено {bad}")
    if a.notify:
        from ops.wb_daily_report import send
        send(f"*Сторож остатка ВБ · {a.account}*\n"
             f"В пол (нет наличия): *{len(down)}*\nВозврат ставки: *{len(up)}*\n"
             f"Записано {ok}" + (f", отклонено {bad}" if bad else ""))


if __name__ == '__main__':
    main()
