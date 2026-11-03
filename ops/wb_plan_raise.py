"""ops/wb_plan_raise.py — поток: mkt
ВБ: подъём цены до ПЛАНОВОЙ цены акции — забираем скидку, которую отдаём зря.

Не сторож (`ops/wb_promo_guard.py` спасает от убытка), а рычаг прибыли. ВБ в автоакции режет
цену глубже, чем требует само участие: у 12 659 карточек цена ниже плановой, то есть скидка
больше необходимой. Поднимаем цену покупателя ДО плановой — участие сохраняется, разница
остаётся нам.

Правила (решения Сергея и замечания Натальи, 19–21.09.2026):
  • потолок = МИНИМУМ плановых цен по всем акциям, где карточка участвует. Карточка сидит
    в 2–3 акциях сразу, скидка на всех одна: поднимешь выше минимума — выпадешь из соседней;
  • только случай «хватает уменьшить скидку» (плановая ≤ розничной). Если плановая выше
    розничной, подъём требует переоценки товара — это ценовая политика, здесь не трогаем;
  • цену поднимаем ЗА СЧЁТ СКИДКИ, база не меняется (правило сторожа ВБ от 10.09);
  • скидка округляется ВВЕРХ: цена покупателя выходит не выше плановой, иначе выпадем из акции;
  • ниже пола не опускаемся никогда (пол считает сторож: (себест + общая цель прибыли: 200 ₽ до 150 ₽/ед., иначе max(300 ₽, 10 %)) / доля).
Плановая цена держится всю акцию — проверено 19→21.09 на «Осенних скидках»: 22 719 карточек,
ни одного изменения. Источник плановых цен — выгрузки ЛК (`ops/wb_promo_plan_import.py`).

Запуск (по умолчанию расчёт, в кабинет ничего не уходит):
    ./venv/bin/python -m ops.wb_plan_raise --csv /tmp/raise.csv
    ./venv/bin/python -m ops.wb_plan_raise --apply --limit 50        # первая партия
Журнал: `wb_promo_guard_log`, wave='plan_raise'.
"""
import argparse
import math
import pathlib
import json
import sys

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                                              # noqa: E402
from ops import wb_promo_guard as w                              # noqa: E402

MIN_GAIN = 10.0          # ₽ на единицу: возиться с меньшим не стоит
MAX_STEP = 0.15          # не больше +15 % цены покупателя за прогон. ВБ режет глубже, чем надо
                         # для участия: у части карточек плановая вдвое выше текущей цены, и
                         # разовый подъём в 2 раза — это и обвал спроса, и риск карантина цен.
                         # Идём ступенями: каждый прогон ближе к плановой, пока она держится.
BATCH = 1000             # локальная волна; общий writer отправляет задачи по 100 товаров


def verify_calendar_coverage(acc):
    """New auto campaigns need a matching export before the scheduled price raise."""
    from ops.wb_auto_entry import Client
    from core.wb_auto_entry import instant
    from datetime import datetime, timezone
    import re
    config = BASE_DIR / 'runtime/wb_plan_raise_coverage.json'
    if not config.exists():
        raise RuntimeError('Нет подтверждённого полного набора выгрузок акций; отправка запрещена')
    known = json.loads(config.read_text())['generations'][acc]
    def key(value):
        return re.sub(r'[^а-яa-z0-9]', '', value.lower().replace('скидки', ''))
    loaded = {key(r['promo_key']) for r in db.query('''select distinct promo_key
        from wb_promo_plan_price where account=%s
        and valid_from <= (now() at time zone 'Europe/Moscow')::date
        and valid_to >= (now() at time zone 'Europe/Moscow')::date''', (acc,))}
    now = datetime.now(timezone.utc)
    missing = []
    for p in Client(acc, 90).calendar().values():
        generation = [int(p['id']), p['startDateTime'], p['endDateTime']]
        if (p['type'] == 'auto' and instant(p['startDateTime']) <= now <= instant(p['endDateTime'])
                and generation not in known and key(p['name']) not in loaded):
            missing.append(p['name'])
    if missing:
        raise RuntimeError('Нужны выгрузки новых автоакций: ' + '; '.join(missing))


def plan_to_discount(base, target):
    """Цена и скидка, чтобы цена покупателя стала НЕ ВЫШЕ плановой.

    Зеркало `wb_promo_guard.raise_plan`: там floor (цель — не упасть ниже пола), здесь ceil
    (цель — не превысить потолок участия). База не трогается."""
    from core.wb_auto_entry import number
    from decimal import ROUND_CEILING
    base, target = number(base), number(target)
    if base < target:
        return None, None                       # база и так ниже плановой — поднимать нечем
    disc = int((100 * (1 - target / base)).to_integral_value(rounding=ROUND_CEILING))
    return int(math.ceil(base)), max(disc, 0)


def candidates(acc, *, goods=None, costs=None, max_step=MAX_STEP):
    from core.wb_auto_entry import number, sale
    from core.profit_floor import minimum_profit
    # Потолок — минимум планов ТОЛЬКО среди акций, где карточка СЕЙЧАС проходит по цене
    # (plan >= текущая цена покупателя). Акции с планом ниже нашей цены мы уже покинули —
    # учитывать их в минимуме нельзя: 5422 на acc2 из-за этого выпал из кандидатов, хотя в
    # своих акциях у него 1 415 ₽ запаса (разбор Натальи 23.09.2026).
    if goods is None:
        from ops.wb_auto_entry import Client
        goods, _ = Client(acc, 300).goods()
    plans = db.query("""
        with latest as (
          select distinct on (promo_key, nm_id) * from wb_promo_plan_price
          where account = %s and valid_from <= (now() at time zone 'Europe/Moscow')::date
          order by promo_key, nm_id, valid_from desc, loaded_at desc
        ) select nm_id, plan_price, promo_key from latest
          where in_promo and valid_to >= (now() at time zone 'Europe/Moscow')::date
        """, (acc,))
    grouped = {}
    for p in plans:
        nm = int(p['nm_id'])
        g = goods.get(nm)
        if g and 0 < g[2] <= float(p['plan_price']):
            grouped.setdefault(nm, []).append(p)
    rows = [dict(nm_id=nm, cap=min(float(p['plan_price']) for p in ps), promos=len(ps),
                 vendor_code=goods[nm][3], base=goods[nm][0], disc=goods[nm][1], now=goods[nm][2])
            for nm, ps in grouped.items()]
    # Та же себестоимость и проверенная кратность, что у основного сторожа WB.
    goods = {int(r["nm_id"]): (r["base"], r["disc"], r["now"], r["vendor_code"])
             for r in rows}
    cost = costs if costs is not None else (w.cogs_map(acc, goods=goods) if rows else {})
    keep = 1 - w.RETENTION[acc]
    out = []
    for r in rows:
        if r['cap'] > r['base']:
            continue
        rec = cost.get(int(r["nm_id"])) or {}
        v, src = rec.get("cogs"), rec.get("source")
        if v is None:
            continue                                     # нет себеста = нет наличия, не трогаем
        quantity = rec.get("bundle", 1)
        floor = (number(v) + minimum_profit(number(v), quantity)) / (1 - number(w.RETENTION[acc]))
        target = number(r["cap"])
        if target < floor:                               # потолок ниже пола — дело сторожа, не наше
            continue
        step_cap = number(r["now"]) * (1 + number(max_step))
        target = min(target, step_cap)               # шаг не больше MAX_STEP за прогон
        price, disc = plan_to_discount(r["base"], target)
        if price is None:
            continue
        new_buyer = sale(price, disc)
        gain = new_buyer - number(r["now"])
        if gain < MIN_GAIN or new_buyer > float(r["cap"]) or new_buyer < floor:
            continue
        out.append({"account": acc, "nm_id": int(r["nm_id"]), "vendor_code": r["vendor_code"],
                    "external_code": None, "wave": "plan_raise", "promo_ids": None,
                    "price_before": float(r["base"]), "disc_before": float(r["disc"] or 0),
                    "buyer_before": float(r["now"]), "prepromo_price": None,
                    "cogs": v, "cogs_source": src, "stock_branch": None,
                    "retention": w.RETENTION[acc], "floor_net": None, "floor_price": float(round(floor, 2)),
                    "net_before": round(float(r["now"]) * keep - v, 2),
                    "target_price": float(new_buyer), "status": "dry", "reason": None,
                    "err": None, "price_after": None,
                    "send_price": price, "send_disc": disc, "cap": float(r["cap"]),
                    "promos": int(r["promos"]), "gain": float(round(gain, 2))})
    if costs is None and out:
        from ops.promo_ordinary_scope import ordinary_codes, verified_set_codes, confirmed_wb_cost
        allowed, known_sets = ordinary_codes(), verified_set_codes()
        out = [r for r in out if confirmed_wb_cost(r['vendor_code'], cost.get(r['nm_id'], {}),
                                                 allowed, known_sets)]
    out.sort(key=lambda x: -x["gain"])
    return out


def main():
    ap = argparse.ArgumentParser(description="ВБ: поднять цену до плановой цены акции")
    ap.add_argument("--account", choices=list(w.RETENTION), help="по умолчанию оба")
    ap.add_argument("--apply", action="store_true", help="отправить в ВБ (по умолчанию расчёт)")
    ap.add_argument("--limit", type=int, help="не больше N карточек на кабинет")
    ap.add_argument("--csv", help="построчный расчёт в файл")
    a = ap.parse_args()
    if a.limit is not None and a.limit <= 0:
        ap.error('--limit must be positive')
    allrows = []
    failed = False
    for acc in ([a.account] if a.account else list(w.RETENTION)):
        if a.apply:
            verify_calendar_coverage(acc)
        rows = candidates(acc)
        handled = {int(r['nm_id']) for r in db.query('''
            select distinct nm_id from wb_promo_guard_log
            where account=%s and status='confirmed'
              and ts >= date_trunc('day', now() at time zone 'Europe/Moscow') at time zone 'Europe/Moscow'
              and (wave='plan_raise' or wave like 'recovery_floor%%')''', (acc,))}
        rows = [r for r in rows if r['nm_id'] not in handled]
        take = rows[:a.limit] if a.limit else rows
        gain = sum(r["gain"] for r in take)
        print(f"{acc}: кандидатов {len(rows)}, берём {len(take)}, прибавка {gain:,.0f} ₽ "
              f"на одну штуку каждой".replace(",", " "), flush=True)
        if a.apply and take:
            for k in range(0, len(take), BATCH):
                batch = take[k:k + BATCH]
                ok, err = w.push(acc, batch)
                for r in batch:
                    r["status"] = "confirmed" if ok else "error"
                    r["err"] = None if ok else err[:200]
                    r["reason"] = (f"подъём до плановой {r['cap']:.0f} "
                                   f"(скидка {r['disc_before']:.0f}% → {r['send_disc']}%)")
                print(f"  пачка {k // BATCH + 1}: {len(batch)} шт — {'отправлена' if ok else 'ОШИБКА ' + err[:80]}",
                      flush=True)
                w.save([{key: value for key, value in r.items()
                         if key not in ('send_price', 'send_disc', 'cap', 'promos', 'gain')}
                        for r in batch])
                if not ok:
                    failed = True
                    break
        allrows += take
    if a.csv and allrows:
        head = ["account", "nm_id", "vendor_code", "price_before", "disc_before", "buyer_before",
                "cap", "target_price", "send_disc", "floor_price", "cogs", "promos", "gain", "status", "err"]
        pathlib.Path(a.csv).write_text(
            "\n".join([";".join(head)] + [";".join(str(r.get(c, "")) for c in head) for r in allrows]),
            encoding="utf-8")
        print(f"построчно → {a.csv}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    from core.wb_price_writer import legacy_main
    sys.exit(legacy_main(main))
