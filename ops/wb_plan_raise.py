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
  • ниже пола не опускаемся никогда (пол считает сторож: (себест + max(300 ₽, 10 %)) / доля).
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
import sys

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                                              # noqa: E402
from ops import ozon_promo_guard as g                            # noqa: E402
from ops import wb_promo_guard as w                              # noqa: E402

MIN_GAIN = 10.0          # ₽ на единицу: возиться с меньшим не стоит
MAX_STEP = 0.15          # не больше +15 % цены покупателя за прогон. ВБ режет глубже, чем надо
                         # для участия: у части карточек плановая вдвое выше текущей цены, и
                         # разовый подъём в 2 раза — это и обвал спроса, и риск карантина цен.
                         # Идём ступенями: каждый прогон ближе к плановой, пока она держится.
BATCH = 100              # столько строк в одной задаче ВБ (как у сторожа)


def plan_to_discount(base, target):
    """Цена и скидка, чтобы цена покупателя стала НЕ ВЫШЕ плановой.

    Зеркало `wb_promo_guard.raise_plan`: там floor (цель — не упасть ниже пола), здесь ceil
    (цель — не превысить потолок участия). База не трогается."""
    base, target = float(base), float(target)
    if base <= target:
        return None, None                       # база и так ниже плановой — поднимать нечем
    disc = int(math.ceil(100.0 * (1.0 - target / base)))
    return int(math.ceil(base)), max(disc, 0)


def candidates(acc):
    rows = db.query("""
        with m as (select account, nm_id, min(plan_price) cap, count(*) promos
                     from wb_promo_plan_price where in_promo group by 1, 2)
        select m.nm_id, m.cap, m.promos, pr.vendor_code, pr.price base,
               pr.discount_pct disc, pr.discounted_price now
          from m join wb_price pr on pr.account = m.account and pr.nm_id = m.nm_id
         where m.account = %s and pr.discounted_price > 0
           and pr.discounted_price < m.cap - 1          -- есть запас
           and m.cap <= pr.price + 1                    -- хватает уменьшить скидку
        """, (acc,))
    cost = g.cost_map(sorted({r["vendor_code"] for r in rows if r["vendor_code"]}))
    keep = 1 - w.RETENTION[acc]
    out = []
    for r in rows:
        v, src = cost.get(r["vendor_code"], (None, "НЕТ"))
        if v is None:
            continue                                     # нет себеста = нет наличия, не трогаем
        floor = (v + max(w.MIN_NET, w.MIN_NET_PCT * v)) / keep
        target = float(r["cap"])
        if target < floor:                               # потолок ниже пола — дело сторожа, не наше
            continue
        step_cap = float(r["now"]) * (1 + MAX_STEP)
        target = min(target, step_cap)               # шаг не больше MAX_STEP за прогон
        price, disc = plan_to_discount(r["base"], target)
        if price is None:
            continue
        new_buyer = float(price) * (1 - disc / 100.0)
        gain = new_buyer - float(r["now"])
        if gain < MIN_GAIN or new_buyer > float(r["cap"]) + 0.5 or new_buyer < floor - 0.5:
            continue
        out.append({"account": acc, "nm_id": int(r["nm_id"]), "vendor_code": r["vendor_code"],
                    "external_code": None, "wave": "plan_raise", "promo_ids": None,
                    "price_before": float(r["base"]), "disc_before": float(r["disc"] or 0),
                    "buyer_before": float(r["now"]), "prepromo_price": None,
                    "cogs": v, "cogs_source": src, "stock_branch": None,
                    "retention": w.RETENTION[acc], "floor_net": None, "floor_price": round(floor, 2),
                    "net_before": round(float(r["now"]) * keep - v, 2),
                    "target_price": round(new_buyer, 2), "status": "dry", "reason": None,
                    "err": None, "price_after": None,
                    "send_price": price, "send_disc": disc, "cap": float(r["cap"]),
                    "promos": int(r["promos"]), "gain": round(gain, 2)})
    out.sort(key=lambda x: -x["gain"])
    return out


def main():
    ap = argparse.ArgumentParser(description="ВБ: поднять цену до плановой цены акции")
    ap.add_argument("--account", choices=list(w.RETENTION), help="по умолчанию оба")
    ap.add_argument("--apply", action="store_true", help="отправить в ВБ (по умолчанию расчёт)")
    ap.add_argument("--limit", type=int, help="не больше N карточек на кабинет")
    ap.add_argument("--csv", help="построчный расчёт в файл")
    a = ap.parse_args()
    allrows = []
    for acc in ([a.account] if a.account else list(w.RETENTION)):
        rows = candidates(acc)
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
            w.save([{k: v for k, v in r.items() if k not in ("send_price", "send_disc", "cap", "promos", "gain")}
                    for r in take])
        allrows += take
    if a.csv and allrows:
        head = ["account", "nm_id", "vendor_code", "price_before", "disc_before", "buyer_before",
                "cap", "target_price", "send_disc", "floor_price", "cogs", "promos", "gain"]
        pathlib.Path(a.csv).write_text(
            "\n".join([";".join(head)] + [";".join(str(r.get(c, "")) for c in head) for r in allrows]),
            encoding="utf-8")
        print(f"построчно → {a.csv}", flush=True)


if __name__ == "__main__":
    main()
