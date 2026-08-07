# поток: mkt
"""reports/ozon_buyer_price.py — цена покупателя Ozon по каждому SKU.

Прямого источника в Seller API нет (проверено на пяти эндпоинтах, см. миграцию 113
и docs/MKT_OZON_PLAN.md §1.4). Восстанавливаем: buyer = our_price × k, где k измерен
по фактическим продажам — `financial_data.customer_price / financial_data.price`.

Почему это законно, а не подгонка:
  * субсидия Озона — процент от цены, а не фиксированная сумма (корреляция суммы
    субсидии с ценой +0.97, корреляция k с уровнем цены −0.09);
  * k устойчив во времени (|Δk| июнь→июль медиана 0.032, у 91 % SKU < 0.10);
  * снижение нашей цены доходит до покупателя (передача Δ медиана 1.17, корреляция +0.76),
    то есть субсидия не гасит наши изменения цены.

k берём по самому SKU, если он продавался (k_source='факт'); иначе — медиану аккаунта
(k_source='аккаунт'). Строки с qty>1 не используем: `price` там на штуку, а `payout`
и `commission_amount` — на строку (грабля задокументирована в плане, §0).

Целевая цена: our_new = our_now × target / external_index. Она НЕ требует k — раз
передача ≈ 1, индекс двигается пропорционально нашей цене.

Результат: таблица `mkt_ozon_buyer_price` + сводка в чат (≤20 строк, CLAUDE.md правило 8).
Запуск: ./venv/bin/python reports/ozon_buyer_price.py [oz_acc1|oz_acc2|all]
"""
import pathlib
import statistics as st
import sys

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

ACCOUNTS = ["oz_acc1", "oz_acc2"]
TARGET_INDEX = 1.05      # верх зелёной зоны по нашим же данным (GREEN max 1.05, RED от 1.06)
K_WINDOW_DAYS = 90       # окно продаж для замера k
K_MIN_SALES = 2          # меньше двух продаж — шум, берём медиану аккаунта
K_FLOOR, K_CEIL = 0.15, 1.30   # отсекаем битые строки (встречались k=0.02 и k=47)
HOLDOUT_DAYS = 14        # свежие продажи, на которых проверяем модель (в расчёт k не идут)


def measure_k(account, since_days=K_WINDOW_DAYS, until_days=0):
    """k по каждому SKU из фактических продаж + медиана аккаунта как запасной вариант.

    Окно — [сегодня−since_days, сегодня−until_days). until_days>0 даёт holdout:
    k считается по старым продажам и проверяется на свежих, которые его не видели.
    """
    rows = db.query("""
    select fd->>'product_id' sku,
           sum((fd->>'customer_price')::numeric) / nullif(sum((fd->>'price')::numeric), 0) k,
           count(*) n
    from raw_ozon_posting p,
         lateral jsonb_array_elements(
             coalesce(p.payload->'financial_data'->'products', '[]'::jsonb)) fd
    where p.account = %s and p.status = 'delivered' and (fd->>'quantity')::int = 1
      and p.in_process_at >= current_date - %s and p.in_process_at < current_date - %s
      and (fd->>'customer_price')::numeric > 0 and (fd->>'price')::numeric > 0
    group by 1 having count(*) >= %s
    """, (account, since_days, until_days, K_MIN_SALES))
    per_sku = {}
    for r in rows:
        k = float(r['k'])
        if K_FLOOR <= k <= K_CEIL:          # битые строки в медиану аккаунта не пускаем
            per_sku[r['sku']] = (k, r['n'])
    med = st.median([v[0] for v in per_sku.values()]) if per_sku else None
    return per_sku, med


def main(account="oz_acc1"):
    snap = db.query("select max(collected_on) d from ozon_price_index where account = %s",
                    (account,))[0]['d']
    if not snap:
        print(f"{account}: снимка индекса нет — сначала collectors/ozon_price_index.py")
        return
    per_sku, k_med = measure_k(account)
    if k_med is None:
        print(f"{account}: продаж за {K_WINDOW_DAYS} дней нет, k измерить не на чем")
        return

    # sku в ozon_price_index — из ответа прайсового эндпоинта и с реальным sku продаж
    # НЕ совпадает (проверено: 0 пересечений из 1183). Мост только через offer_id.
    # У одного offer_id бывает несколько sku (FBO/FBS) — берём тот, по которому есть
    # продажи, иначе самый свежий.
    offer_skus = {}
    for r in db.query("""
    select offer_id, sku::text sku from ozon_product
    where account = %s order by updated_at desc nulls last
    """, (account,)):
        offer_skus.setdefault(r['offer_id'], []).append(r['sku'])

    src = db.query("""
    select pi.offer_id, pi.price, pi.external_min_price, pi.external_index, pi.color_index
    from ozon_price_index pi
    where pi.account = %s and pi.collected_on = %s and pi.price > 0
    """, (account, snap))

    rows, n_fact = [], 0
    for r in src:
        cand = offer_skus.get(r['offer_id'], [])
        sku = next((s for s in cand if s in per_sku), cand[0] if cand else '')
        hit = per_sku.get(sku)
        k, k_src, k_n = (hit[0], 'факт', hit[1]) if hit else (k_med, 'аккаунт', 0)
        n_fact += 1 if hit else 0
        price = float(r['price'])
        idx = float(r['external_index'] or 0)
        # Целевая цена считается ТОЛЬКО когда есть индекс: без конкурента цели нет.
        target = round(price * TARGET_INDEX / idx, 2) if idx > TARGET_INDEX else None
        rows.append(dict(
            account=account, offer_id=r['offer_id'], snapshot_date=snap,
            sku=int(sku) if sku.isdigit() else None,
            our_price=price, k=round(k, 4), k_source=k_src, k_sales=k_n,
            buyer_price=round(price * k, 2), external_min_price=r['external_min_price'],
            external_index=r['external_index'], color_index=r['color_index'],
            price_for_target=target, target_index=TARGET_INDEX))

    db.upsert("mkt_ozon_buyer_price", rows, ["account", "offer_id", "snapshot_date"])
    db.execute("update mkt_ozon_buyer_price set built_at = now() "
               "where account = %s and snapshot_date = %s", (account, snap))

    # Контроль ЧЕСТНЫЙ (holdout): k считаем по продажам старше 14 дней, а проверяем
    # на свежих, которых модель не видела. Цену берём из самой продажи, чтобы мерить
    # ошибку модели, а не дрейф нашей цены между продажей и снимком.
    hold, hold_med = measure_k(account, K_WINDOW_DAYS, HOLDOUT_DAYS)
    fresh = db.query("""
    select fd->>'product_id' sku, (fd->>'price')::numeric our,
           (fd->>'customer_price')::numeric cust
    from raw_ozon_posting p,
         lateral jsonb_array_elements(
             coalesce(p.payload->'financial_data'->'products', '[]'::jsonb)) fd
    where p.account = %s and p.status = 'delivered' and (fd->>'quantity')::int = 1
      and p.in_process_at >= current_date - %s
      and (fd->>'customer_price')::numeric > 0 and (fd->>'price')::numeric > 0
    """, (account, HOLDOUT_DAYS))
    ratios = []
    for r in fresh:
        kk = hold.get(r['sku'], (hold_med, 0))[0] if hold_med else None
        if kk:
            ratios.append(float(r['our']) * kk / float(r['cust']))
    ratios.sort()
    med_r = st.median(ratios) if ratios else None
    within = (sum(1 for x in ratios if 0.9 <= x <= 1.1) / len(ratios)) if ratios else None

    db.upsert("mkt_ozon_buyer_price_run", [dict(
        account=account, snapshot_date=snap, rows_total=len(rows), rows_k_fact=n_fact,
        k_median=round(k_med, 4), check_n=len(ratios),
        check_median=round(med_r, 4) if med_r else None,
        check_within10=round(within, 4) if within else None)],
        ["account", "snapshot_date"])

    tgt = [r for r in rows if r['price_for_target']]
    print(f"{account} (снимок {snap}): строк {len(rows)}, k по факту {n_fact}, "
          f"медиана k {k_med:.3f}")
    print(f"   цена покупателя восстановлена; сверка с фактом: n={len(ratios)}, "
          f"медиана {med_r:.3f}, в ±10 % {100*within:.0f}%" if ratios else "   сверять не на чем")
    print(f"   SKU выше целевого индекса {TARGET_INDEX}: {len(tgt)} "
          f"(им нужна цена ниже текущей)")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "oz_acc1"
    for a in (ACCOUNTS if arg == "all" else [arg]):
        main(a)
