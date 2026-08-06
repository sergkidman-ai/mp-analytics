# поток: mkt
"""reports/ozon_red_zone.py — красная зона индекса цен Ozon в деньгах.

Что считает:
  1) распределение зон с ЧЕСТНЫМ знаменателем — WITHOUT_INDEX значит «конкурента не нашли»,
     зоны у такой карточки нет вообще, и держать её в знаменателе = занижать долю красной;
  2) где реально проходит граница зон — по нашим же снимкам, а не по документации;
  3) красную зону × продажи (margin_by_sku, поток fin, read-only) — во что обходится выход
     из красной зоны и по каким SKU выходить нельзя (уйдут в минус).

Мост к продажам: ozon_price_index.offer_id → ozon_product.sku → margin_by_sku.article.
Прямого совпадения price_index.sku (product_id) с margin_by_sku.article НЕТ — проверено, 0 строк.

Результат: docs/reports/ozon_red_zone.md + .csv. В чат — только сводка (CLAUDE.md, правило 8).

Запуск: ./venv/bin/python reports/ozon_red_zone.py
"""
import csv
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from core import db  # noqa: E402

OUT = pathlib.Path(__file__).resolve().parent.parent / 'docs' / 'reports'
OUT.mkdir(parents=True, exist_ok=True)
L = []
p = L.append
fmt = lambda v: f"{v:,.0f}".replace(',', ' ')

# 1. Зоны с честным знаменателем: WITHOUT_INDEX = конкурента не нашли, зоны нет вообще.
z = db.query("""
select account, color_index, count(*) c
from ozon_price_index where collected_on = current_date group by 1,2 order by 1,2
""")
byacc = {}
for r in z:
    byacc.setdefault(r['account'], {})[r['color_index']] = r['c']
p("## 1. Распределение зон (снимок сегодня)\n")
p("| Аккаунт | Всего | Без индекса | С индексом | RED | RED % от «с индексом» |")
p("|---|---|---|---|---|---|")
for acc, d in sorted(byacc.items()):
    tot = sum(d.values()); wo = d.get('WITHOUT_INDEX', 0); wi = tot - wo
    p(f"| {acc} | {tot} | {wo} | {wi} | {d.get('RED',0)} | {100*d.get('RED',0)/wi:.1f}% |")
p("")
p("| Аккаунт | SUPER | GREEN | YELLOW | RED |")
p("|---|---|---|---|---|")
for acc, d in sorted(byacc.items()):
    wi = sum(d.values()) - d.get('WITHOUT_INDEX', 0)
    p("| %s | %s | %s | %s | %s |" % (acc, *[f"{d.get(k,0)} ({100*d.get(k,0)/wi:.0f}%)"
                                              for k in ('SUPER','GREEN','YELLOW','RED')]))

# 2. Где проходит граница зон — не по докам, а по нашим же 36k строк.
b = db.query("""
select color_index, count(*) c, min(external_index) mn, max(external_index) mx,
       percentile_cont(0.5) within group (order by external_index) med
from ozon_price_index
where collected_on = current_date and external_index > 0
group by 1 order by med
""")
p("\n## 2. Эмпирические границы индекса (по внешнему индексу, наши данные)\n")
p("| Зона | Карточек | min | медиана | max |")
p("|---|---|---|---|---|")
for r in b:
    p(f"| {r['color_index']} | {r['c']} | {r['mn']:.2f} | {r['med']:.2f} | {r['mx']:.2f} |")

# 3. Красная зона × реальные продажи (июнь+июль 2026, полные месяцы) через мост offer_id.
rows = db.query("""
with pi as (
  select account, sku, offer_id, price, external_min_price, external_index,
         commission_fbo_pct, min_price
  from ozon_price_index
  where collected_on = current_date and color_index = 'RED'
),
bridge as (
  select distinct p.account, p.offer_id, op.sku as oz_sku
  from pi p join ozon_product op on op.account = p.account and op.offer_id = p.offer_id
),
sales as (
  -- qty у Ozon в margin_by_sku НЕ заполнен (только суммы) — считаем в рублях, не в штуках
  select account, article, sum(revenue_buyer) rev, sum(net_profit) net, sum(cogs) cogs
  from margin_by_sku
  where platform = 'ozon' and period_from >= date '2026-06-01' and period_to <= date '2026-07-31'
  group by 1,2
)
select pi.account, pi.offer_id, pi.sku, pi.price, pi.external_min_price, pi.external_index,
       pi.commission_fbo_pct, sum(s.rev) rev, sum(s.net) net, sum(s.cogs) cogs
from pi
join bridge b on b.account = pi.account and b.offer_id = pi.offer_id
join sales s on s.account = b.account and s.article = b.oz_sku
group by 1,2,3,4,5,6,7
having sum(s.rev) > 0
order by sum(s.rev) desc
""")
p("\n## 3. Красная зона × продажи (июнь+июль 2026)\n")
tot_rev = sum(float(r['rev'] or 0) for r in rows)
tot_net = sum(float(r['net'] or 0) for r in rows)
p(f"Красных SKU, которые реально продавались: **{len(rows)}**. "
  f"Их выручка за 2 месяца **{fmt(tot_rev)} ₽**, чистая **{fmt(tot_net)} ₽**.")

# Сколько стоит выход из красной зоны.
# ВАЖНО: индекс НЕ равен price / external_min_price (проверено: 14890/5161 = 2.9 при индексе 1.41).
# external_min_price — самое дешёвое найденное предложение, а индекс Ozon считает от своей базы.
# Поэтому нужную скидку берём ИЗ САМОГО ИНДЕКСА: цена и индекс пропорциональны, значит
# скидка = 1 − TARGET/индекс. TARGET взят из раздела 2 по нашим же данным: GREEN до 1.05,
# RED начинается с 1.06.
# Считаем в долях выручки: выручка падает на долю скидки, COGS не меняется, комиссия падает
# пропорционально. Эластичность НЕ заложена (продажи после снижения вырастут — это плюс сверху).
TARGET_INDEX = 1.05
detail = []
for r in rows:
    price = float(r['price'] or 0); mn = float(r['external_min_price'] or 0)
    net = float(r['net'] or 0); rev = float(r['rev'] or 0)
    idx = float(r['external_index'] or 0)
    cut_share = max(0.0, 1 - TARGET_INDEX / idx) if idx else 0
    cut = price * cut_share
    comm = float(r['commission_fbo_pct'] or 36) / 100
    net_loss = rev * cut_share * (1 - comm)
    detail.append(dict(account=r['account'], offer_id=r['offer_id'], sku=r['sku'],
                       rev=rev, net=net, margin_pct=100 * net / rev if rev else 0,
                       price=price, new_price=price - cut, market_min=mn, index=idx,
                       cut=cut, cut_pct=100 * cut_share,
                       net_loss_2m=net_loss, net_after=net - net_loss,
                       survives=(net - net_loss) > 0))
detail.sort(key=lambda d: -d['rev'])
surv = [d for d in detail if d['survives'] and d['cut'] > 0]
dead = [d for d in detail if not d['survives'] and d['cut'] > 0]
already = [d for d in detail if d['cut'] <= 0]
p(f"\n- **Можно опустить до индекса {TARGET_INDEX} и остаться в плюсе: {len(surv)} SKU** "
  f"(выручка {fmt(sum(d['rev'] for d in surv))} ₽, чистая сейчас {fmt(sum(d['net'] for d in surv))} ₽, "
  f"после снижения ≈ {fmt(sum(d['net_after'] for d in surv))} ₽ — "
  f"цена выхода из красной зоны ≈ {fmt(sum(d['net_loss_2m'] for d in surv))} ₽ за 2 мес)")
p(f"- **Опустить нельзя — уйдём в минус: {len(dead)} SKU** "
  f"(выручка {fmt(sum(d['rev'] for d in dead))} ₽). Это кандидаты не на скидку, а на вывод "
  f"из ассортимента / смену поставщика.")
p(f"- Уже не дороже рынка, но всё равно RED: {len(already)} SKU "
  f"(индекс считается не только по цене — смотреть отдельно).")

p("\n### Топ-20 красных по выручке\n")
p("| Акк | Артикул | Цена | Индекс | Нужна цена | Скидка | Выручка 2 мес | Чистая | Маржа | Чистая после снижения |")
p("|---|---|---|---|---|---|---|---|---|---|")
for d in detail[:20]:
    p(f"| {d['account'][-1]} | {d['offer_id']} | {d['price']:.0f} | {d['index']:.2f} | "
      f"{d['new_price']:.0f} | {d['cut']:.0f} ({d['cut_pct']:.0f}%) | "
      f"{fmt(d['rev'])} | {fmt(d['net'])} | {d['margin_pct']:.0f}% | {fmt(d['net_after'])} |")

csv_path = OUT / 'ozon_red_zone.csv'
with open(csv_path, 'w', newline='', encoding='utf-8') as f:
    w = csv.DictWriter(f, fieldnames=list(detail[0].keys()))
    w.writeheader(); w.writerows(detail)

md = OUT / 'ozon_red_zone.md'
md.write_text("# Красная зона индекса цен Ozon — 06.08.2026\n\n"
              "> **СПИСОК НА СНИЖЕНИЕ ЦЕН НЕ ПРИМЕНЯТЬ.** Проверка юнит-экономики 06.08 показала:\n"
              "> индекс смотрит на цену, которую видит ПОКУПАТЕЛЬ (со скидкой за счёт Озона),\n"
              "> а не на нашу. Проверено на июле: у SKU со скидкой Озона >45% средний индекс 0.80\n"
              "> и RED всего 7%, при скидке 25–45% — индекс 1.02 и RED 31%. Значит часть красной\n"
              "> зоны — это не наша цена, а решение Озона не субсидировать данный товар, и снижение\n"
              "> нашей цены на X% НЕ обязано двигать индекс на X% (Озон может убрать свою скидку).\n"
              "> Колонки «нужна цена» и «скидка» ниже — ориентир направления, НЕ рабочий план.\n"
              "> Чтобы считать точно, нужна цена покупателя по каждому SKU: в этом эндпоинте\n"
              "> `marketing_price` пуст у всех 35 869 карточек — источник надо искать отдельно.\n\n"
              "Источник: `ozon_price_index` (снимок сегодня) × `margin_by_sku` (fin, read-only),\n"
              "мост `offer_id → ozon_product.sku → margin_by_sku.article`.\n"
              "Нужная скидка выведена ИЗ ИНДЕКСА (`1 − 1.05/индекс`), а не из `external_min_price`:\n"
              "индекс Ozon считает от своей базы, и `цена / min_price` ему не равно (проверено).\n"
              "Падение чистой = `выручка × доля скидки × (1 − комиссия)` — без учёта эластичности\n"
              "спроса (продажи после снижения цены вырастут, здесь это НЕ заложено, т.е. оценка\n"
              "консервативная).\n\n"
              + "\n".join(L) + f"\n\nПолный список: `{csv_path.name}`\n", encoding='utf-8')
print(f"отчёт: {md}")
print(f"csv:    {csv_path} ({len(detail)} строк)")
print("\n".join(L[:6]))
