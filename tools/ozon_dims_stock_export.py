# поток: mkt
# Выгрузка для страницы «Габариты Ozon acc1: что в наличии, разложенное по шаблонам».
# Наличие = свой склад (Звездный + Озон FBO) из supplier_stock, снимок на сегодня.
# Реальный короб — только из чистых источников поставщиков (правило габаритов №1: не считаем).
import sys, json, decimal; sys.path.insert(0, '.')
from core import db

RUB_PER_L = 7.3
OUT = '/tmp/claude-0/-opt-mp-analytics--claude-worktrees-mkt-ozon/' \
      '1258757b-c5aa-4324-90f0-605097d04789/scratchpad/ozon_dims_stock.json'

stock = {}
for r in db.query("""
  select external_code code, sum(stock) qty, sum(in_transit) transit, sum(sold_30d) sold30,
         max(cost_seb) seb
  from supplier_stock
  where captured_at=(select max(captured_at) from supplier_stock)
    and store in ('Звездный','Озон') and stock > 0 and external_code ~ '^[0-9]{3,5}$'
  group by 1"""):
    stock[r['code']] = r

real = {}
for r in db.query("""
  select p.external_code code, s.supplier, s.volume_l::float v,
         s.length_cm::float l, s.width_cm::float w, s.height_cm::float h
  from ms_product p join supplier_dims s on upper(s.article)=upper(p.article)
  where p.external_code ~ '^[0-9]{3,5}$' and s.volume_l > 0
    and s.supplier in ('cactus','rapid','sakura','изи')"""):
    real.setdefault(r['code'], []).append(r)

sales = {r['sku']: r for r in db.query("""
  select (pr->>'sku') sku, sum((pr->>'quantity')::int) qty
  from raw_ozon_posting, lateral jsonb_array_elements(payload->'products') pr
  where account='oz_acc1' and status<>'cancelled' and in_process_at >= now()-interval '90 days'
  group by 1""")}

rows = []
for d in db.query("""select sku, offer_id, name, depth_mm, width_mm, height_mm, weight_g,
                            volume_l::float vol, updated_at::text upd
                     from ozon_dims where account='oz_acc1' and volume_l > 0"""):
    st = stock.get(d['offer_id'])
    if not st:
        continue
    cand = sorted((x['v'] for x in real.get(d['offer_id'], [])))
    rv = None
    if cand:
        if len(cand) > 1 and cand[-1] > 3 * cand[0]:   # мастер-картон отбрасываем
            cand = cand[:-1]
        rv = cand[-1]                                   # из согласующихся берём БОЛЬШИЙ
    sold90 = int(sales.get(d['sku'], {}).get('qty') or 0)
    rows.append(dict(
        sku=d['sku'], code=d['offer_id'], name=(d['name'] or '')[:90],
        tpl=f"{d['depth_mm']}×{d['width_mm']}×{d['height_mm']}",
        vol=round(d['vol'], 2), weight=d['weight_g'],
        real=round(rv, 2) if rv else None,
        srcs=sorted({x['supplier'] for x in real.get(d['offer_id'], [])}),
        delta=round(d['vol'] - rv, 2) if rv else None,
        stock=int(st['qty'] or 0), transit=int(st['transit'] or 0),
        sold30=int(st['sold30'] or 0), sold90=sold90,
        money=round((d['vol'] - rv) * RUB_PER_L * sold90) if rv and sold90 else 0))

groups = {}
for r in rows:
    g = groups.setdefault(r['tpl'], dict(tpl=r['tpl'], vol=r['vol'], n=0, stock=0, sold90=0,
                                         known=0, deltas=[], money=0, items=[]))
    g['n'] += 1; g['stock'] += r['stock']; g['sold90'] += r['sold90']; g['money'] += r['money']
    if r['real'] is not None:
        g['known'] += 1; g['deltas'].append(r['delta'])
    g['items'].append(r)
for g in groups.values():
    g['items'].sort(key=lambda x: (-x['sold90'], -x['stock']))
    ds = sorted(g['deltas'])
    g['delta_med'] = ds[len(ds)//2] if ds else None
    g.pop('deltas')

meta = db.query("""select (select max(updated_at)::text from ozon_dims where account='oz_acc1') dims,
                          (select max(captured_at)::text from supplier_stock) stock,
                          (select max(loaded_at)::text from supplier_dims) sdims,
                          (select max(in_process_at)::text from raw_ozon_posting where account='oz_acc1') post""")[0]
data = dict(meta=meta, rub_per_l=RUB_PER_L,
            groups=sorted(groups.values(), key=lambda g: (-g['stock'], -g['n'])))
json.dump(data, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False,
          default=lambda o: float(o) if isinstance(o, decimal.Decimal) else str(o))
tot = sum(g['n'] for g in data['groups'])
print(f"карточек в наличии: {tot}, шаблонов: {len(data['groups'])}, "
      f"остаток {sum(g['stock'] for g in data['groups'])} шт, "
      f"с реальным коробом {sum(g['known'] for g in data['groups'])}")
for g in data['groups'][:8]:
    print(f"{g['tpl']:>16} {g['vol']:>6} л | SKU {g['n']:>4} | остаток {g['stock']:>5} | "
          f"продано 90д {g['sold90']:>4} | замерено {g['known']:>3} | медиана дельты {g['delta_med']}")
print("JSON:", OUT)
