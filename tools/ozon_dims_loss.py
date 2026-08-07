# поток: mkt
"""Ozon acc1: во что обходятся шаблонные габариты карточек.

Мост: ozon_dims.offer_id (наш 4-значный код) = ms_product.external_code
      -> ms_product.article = supplier_dims.article -> реальный короб поставщика.

Реальный короб берём ТОЛЬКО из чистых источников (cactus / rapid / sakura / изи),
ничего не вычисляем и не усредняем (правило габаритов №1):
из согласующихся коробов берём БОЛЬШИЙ, подозрение на мастер-картон
(объём > 3x минимального в группе) отбрасываем.

Деньги: маржинальный тариф логистики Ozon acc1 = 7.3 руб/л
(замер по mkt_ozon_margin_control: 2 л -> 80.3 руб, 12 л -> 153.5 руб).

Результат: docs/reports/ozon_dims_loss.csv + сводка в stdout.
"""
import os
import csv
import psycopg2
from dotenv import load_dotenv

load_dotenv("/opt/mp-analytics/.env")
RUB_PER_L = 7.3
CLEAN = ('cactus', 'rapid', 'sakura', 'изи')
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "docs", "reports", "ozon_dims_loss.csv")

SQL_REAL = """
select p.external_code as code, s.supplier, s.volume_l::float,
       s.length_cm::float, s.width_cm::float, s.height_cm::float
from ms_product p
join supplier_dims s on upper(s.article) = upper(p.article)
where p.external_code ~ '^[0-9]{3,5}$' and s.volume_l > 0 and s.supplier = any(%s)
"""

SQL_CARDS = """
select d.sku::text, d.offer_id, d.name, d.volume_l::float,
       d.depth_mm, d.width_mm, d.height_mm
from ozon_dims d
where d.account = 'oz_acc1' and d.volume_l > 0
"""

SQL_SALES = """
select pr->>'sku' as sku,
       sum((pr->>'quantity')::int) as qty,
       sum((pr->>'price')::numeric * (pr->>'quantity')::int) as revenue
from raw_ozon_posting t,
     lateral jsonb_array_elements(t.payload->'products') pr
where t.account = 'oz_acc1'
  and t.status <> 'cancelled'
  and t.in_process_at >= now() - interval '90 days'
group by 1
"""

SQL_STOCK = """
select sku::text, sum(free_to_sell) from ozon_fbo_stock
where account = 'oz_acc1' and captured_at = (select max(captured_at) from ozon_fbo_stock)
group by 1
"""


def pick_real(vols):
    """Из реальных коробов поставщиков выбрать рабочий: больший из согласующихся,
    мастер-картон (>3x минимального) отбросить. Ничего не вычисляем."""
    vs = sorted(v for v in vols if v > 0)
    if not vs:
        return None
    if len(vs) > 1 and vs[-1] > 3 * vs[0]:
        vs = vs[:-1] or vs
    return vs[-1]


def main():
    c = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = c.cursor()

    cur.execute(SQL_REAL, (list(CLEAN),))
    real = {}
    for code, sup, vol, l, w, h in cur.fetchall():
        real.setdefault(code, []).append((vol, sup, l, w, h))

    cur.execute(SQL_SALES)
    sales = {str(sku): (int(q), float(rev)) for sku, q, rev in cur.fetchall()}
    cur.execute(SQL_STOCK)
    stock = {str(sku): int(q or 0) for sku, q in cur.fetchall()}

    cur.execute(SQL_CARDS)
    rows = []
    for sku, offer, name, vol, d, w, h in cur.fetchall():
        cand = real.get((offer or "").lstrip("0").rjust(4, "0")) or real.get(offer)
        rv = pick_real([x[0] for x in cand]) if cand else None
        qty, rev = sales.get(sku, (0, 0.0))
        rows.append(dict(
            sku=sku, offer_id=offer, name=(name or "")[:70],
            card_dims=f"{d}x{w}x{h}", card_vol=round(vol, 3),
            real_vol=round(rv, 3) if rv else "",
            delta_l=round(vol - rv, 3) if rv else "",
            suppliers=";".join(sorted({x[1] for x in cand})) if cand else "",
            qty90=qty, revenue90=round(rev),
            stock=stock.get(sku, 0),
            loss90=round((vol - rv) * RUB_PER_L * qty) if rv and vol > rv else 0,
        ))

    with open(OUT, "w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        wr.writeheader()
        for r in sorted(rows, key=lambda r: -(r["loss90"] or 0)):
            wr.writerow(r)

    known = [r for r in rows if r["real_vol"] != ""]
    sold = [r for r in rows if r["qty90"] > 0]
    ks = [r for r in known if r["qty90"] > 0]
    over = [r for r in ks if r["delta_l"] > 0]
    under = [r for r in ks if r["delta_l"] < 0]
    tot_units = sum(r["qty90"] for r in sold)
    print(f"карточек acc1: {len(rows)}, с продажами 90 дн: {len(sold)} ({tot_units} шт)")
    print(f"есть реальный короб поставщика: {len(known)} карточек, из них с продажами {len(ks)}"
          f" ({sum(r['qty90'] for r in ks)} шт = {100*sum(r['qty90'] for r in ks)/max(tot_units,1):.0f}% штук)")
    print(f"завышен короб: {len(over)} SKU, переплата 90 дн {sum(r['loss90'] for r in over):,} руб"
          f" = {sum(r['loss90'] for r in over)/3:,.0f} руб/мес")
    print(f"занижен короб (риск перемера): {len(under)} SKU,"
          f" {sum(r['qty90'] for r in under)} шт")
    # разрез по шаблонам
    tpl = {}
    for r in ks:
        t = tpl.setdefault(r["card_dims"], dict(n=0, qty=0, loss=0, dl=[]))
        t["n"] += 1
        t["qty"] += r["qty90"]
        t["loss"] += r["loss90"]
        t["dl"].append(r["delta_l"])
    print("\nшаблон карточки | SKU | шт 90дн | переплата 90дн | медиана дельты, л")
    for k, v in sorted(tpl.items(), key=lambda x: -x[1]["loss"])[:8]:
        m = sorted(v["dl"])[len(v["dl"]) // 2]
        print(f"{k} | {v['n']} | {v['qty']} | {v['loss']:,} | {m}")
    print(f"\nCSV: {OUT}")


if __name__ == "__main__":
    main()
