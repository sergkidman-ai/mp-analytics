# поток: mkt
"""reports/ozon_query_econ.py — витрина «фраза × SKU × позиция × маржа» (миграция 117).

Отвечает на вопрос «за какие фразы имеет смысл платить»: сводит спрос и нашу позицию
из сборщика фраз с запасом маржи из контроля маржи и с текущими ставками.

Решение (`action`) по каждой паре «фраза × SKU», порядок проверок важен:
    нерелевантно     — фраза про технику (принтер/МФУ/ноутбук), а не про расходник;
    широкая_фраза    — одно значащее слово («набор», «тонер», «4070»): спрос огромный,
                       намерение нулевое, Ozon показывает по ней десятки наших SKU сразу;
    мелочь           — спрос ниже порога за неделю: не тратить внимание;
    мимо_темы        — Ozon показывает нас в чужом запросе: слова фразы в названии товара
                       не сходятся («rtx 4070» → Sharp MX-4070, «dior b22», «булат 4»).
                       Платить нельзя; это же счётчик того, куда уходят пустые показы;
    платим_за_топ    — мы в топ-3 И SKU в кампании: кандидат снять ставку, топ и так наш;
    держим_органикой — топ-10 без рекламы: трогать нечего, это бесплатный трафик;
    нет_маржи        — по SKU нет маржи-live (нет себеста/цены): сначала закрыть данные;
    маржа_ниже_KPI   — маржа под 25 процентов: разгонять нельзя, реклама доест остаток;
    поднять_ставку   — позиция 4-50, спрос есть, маржа есть: главный рабочий список;
    не_видны         — позиция 0 или 50+: одной ставкой может не решиться, нужен и контент.

Релевантность фразы нашему товару (`rel_ok`) — два пути, оба грубые, но проверяемые:
слово-маркер расходника в самой фразе, либо ПОЛНОЕ совпадение слов фразы с названием
товара (`name_overlap = 1`). Частичное совпадение не годится: у «rtx 4070» совпадает
ровно половина — и этой половины хватает, чтобы видеокарта притянула картридж Sharp.

ГРАБЛИ: `unique_search_users` — свойство ФРАЗЫ. Одна фраза приходит строкой на каждый
наш SKU в выдаче, поэтому по строкам спрос НЕ суммируется (кратное завышение) —
в сводках берётся max() по фразе.

Запуск:  ./venv/bin/python reports/ozon_query_econ.py [oz_acc1|oz_acc2|all] [period_start]
"""
import pathlib
import sys

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                                          # noqa: E402

DEMAND_MIN = 10          # уникальных искавших за неделю: ниже — шум (p90 по фразам = 8)
KPI_MARGIN = 25.0        # целевая маржа от нашей цены, ниже неё рекламу не разгоняем

# Ставка «Продвижения с оплатой за заказ». ЕДИНА на весь ассортимент аккаунта (5/7/9 %),
# по SKU не выбирается. acc1 подняли 5 → 7 % в июле-2026, acc2 остался на 5 %.
# Меняете ставку в кабинете — правьте здесь, иначе потолок CPC будет считаться не от той
# экономики (процент вычитается из маржи заказа ДО того, как остаток тратится на клики).
PPO_RATE = {"oz_acc1": 0.07, "oz_acc2": 0.05}

RASHOD = r'(картридж|тонер|чернил|фотобараб|драм|барабан|снпч|пзк|заправ|девелопер|'  \
         r'термоплен|термоплён|чип|ролик|печк|краск|расходник)'
TEHNIKA = r'(принтер|мфу|сканер|плоттер|3d|3д|ноутбук|телефон|компьютер|монитор|'      \
          r'наушник|видеокарт|планшет|телевизор)'
STOP = ('для', 'под', 'как', 'или', 'the', 'and', 'купить', 'цена', 'цены', 'оригинал')

SQL = """
INSERT INTO mkt_ozon_query_econ (
    account, period_start, period_end, query, sku, offer_id, name,
    position, demand, views, view_conv, orders, gmv,
    rel_kind, query_kind, name_overlap,
    our_price, margin_own_live, discount_limit_pct, color_index, verdict,
    in_campaign, bid, campaigns, action, cr_bucket, bid_ceiling)
WITH mc AS (
    SELECT account, sku::text sku, offer_id, name, our_price, margin_own_live,
           discount_limit_pct, color_index, verdict
      FROM mkt_ozon_margin_control
     WHERE captured_date = (SELECT max(captured_date) FROM mkt_ozon_margin_control)
), cr AS (
    -- конверсия клика в заказ по ценовым бакетам: факт кампаний за последний ПОЛНЫЙ месяц.
    -- Бакет кампании определяем по её же среднему чеку, а не по названию: названия
    -- («Эксперимент1», «Пустые РК») ценовой диапазон не описывают.
    SELECT account, b, sum(orders)::numeric / nullif(sum(clicks), 0) cr
      FROM (SELECT account, clicks, orders,
                   CASE WHEN ad_revenue / nullif(orders, 0) < 1000  THEN 1
                        WHEN ad_revenue / nullif(orders, 0) < 2000  THEN 2
                        WHEN ad_revenue / nullif(orders, 0) < 5000  THEN 3
                        WHEN ad_revenue / nullif(orders, 0) < 10000 THEN 4
                        ELSE 5 END b
              FROM ozon_ads
             WHERE period = (SELECT max(period) FROM ozon_ads
                              WHERE period < date_trunc('month', current_date))
               AND pay_model <> 'Оплата за заказ' AND clicks > 0 AND orders > 0) x
     GROUP BY 1, 2
), bd AS (
    SELECT account, sku, max(bid) bid, count(DISTINCT campaign_id) campaigns
      FROM ozon_bids
     WHERE captured_at = (SELECT max(captured_at) FROM ozon_bids)
     GROUP BY 1, 2
), sp AS (
    SELECT DISTINCT ON (account, sku) account, sku, name, offer_id
      FROM ozon_search_product
     WHERE account = %(acc)s AND period_start = %(ps)s
     ORDER BY account, sku, period_end DESC
), src AS (
    SELECT q.*, coalesce(mc.name, sp.name) nm, coalesce(mc.offer_id, sp.offer_id) oid,
           mc.our_price, mc.margin_own_live, mc.discount_limit_pct, mc.color_index, mc.verdict,
           bd.bid, bd.campaigns, cr.cr,
           CASE WHEN lower(q.query) ~ %(rashod)s  THEN 'расходник'
                WHEN lower(q.query) ~ %(tehnika)s THEN 'техника'
                ELSE 'прочее' END rel_kind,
           tk.n_tok, tk.overlap
      FROM ozon_search_query q
      LEFT JOIN mc ON mc.account = q.account AND mc.sku = q.sku
      LEFT JOIN sp ON sp.account = q.account AND sp.sku = q.sku
      LEFT JOIN bd ON bd.account = q.account AND bd.sku = q.sku
      LEFT JOIN cr ON cr.account = q.account AND cr.b =
           CASE WHEN mc.our_price < 1000  THEN 1 WHEN mc.our_price < 2000  THEN 2
                WHEN mc.our_price < 5000  THEN 3 WHEN mc.our_price < 10000 THEN 4
                ELSE 5 END
      LEFT JOIN LATERAL (
           SELECT count(*) n_tok,
                  round(count(*) FILTER (
                        WHERE position(t IN lower(coalesce(mc.name, sp.name, ''))) > 0
                        )::numeric / greatest(count(*), 1), 2) overlap
             FROM unnest(string_to_array(
                    regexp_replace(lower(q.query), '[^a-zа-я0-9]+', ' ', 'g'), ' ')) t
            WHERE length(t) >= 2 AND NOT (t = ANY(%(stop)s))) tk ON true
     WHERE q.account = %(acc)s AND q.period_start = %(ps)s
), cls AS (
    SELECT s.*,
           CASE WHEN s.n_tok <= 1 THEN 'широкая' ELSE 'модельная' END query_kind,
           (s.rel_kind = 'расходник' OR (s.rel_kind = 'прочее' AND s.overlap >= 1)) rel_ok
      FROM src s
)
SELECT c.account, %(ps)s, %(pe)s, c.query, c.sku, c.oid, c.nm,
       c.position, c.unique_search_users, c.unique_view_users, c.view_conversion,
       c.order_count, c.gmv,
       c.rel_kind, c.query_kind, c.overlap,
       c.our_price, c.margin_own_live, c.discount_limit_pct, c.color_index, c.verdict,
       c.campaigns IS NOT NULL, c.bid, c.campaigns,
       CASE
         WHEN c.rel_kind = 'техника'                                 THEN 'нерелевантно'
         WHEN c.query_kind = 'широкая'                               THEN 'широкая_фраза'
         WHEN coalesce(c.unique_search_users, 0) < %(dmin)s          THEN 'мелочь'
         WHEN NOT c.rel_ok                                           THEN 'мимо_темы'
         WHEN c.position BETWEEN 1 AND 3 AND c.campaigns IS NOT NULL THEN 'платим_за_топ'
         WHEN c.position BETWEEN 1 AND 10                            THEN 'держим_органикой'
         WHEN c.margin_own_live IS NULL                              THEN 'нет_маржи'
         WHEN c.margin_own_live < %(kpi)s                            THEN 'маржа_ниже_KPI'
         WHEN c.position > 3 AND c.position <= 50                    THEN 'поднять_ставку'
         ELSE 'не_видны'
       END,
       c.cr,
       -- потолок CPC: сколько можно платить за клик, чтобы заказ ещё был в плюсе.
       -- Из маржи заказа сначала вычитается процент «оплаты за заказ» (он берётся с ЛЮБОГО
       -- заказа, в том числе пришедшего по клику), остаток делится на число кликов,
       -- нужных для одного заказа (то есть умножается на конверсию).
       round((c.our_price * c.margin_own_live / 100.0 - c.our_price * %(rate)s) * c.cr, 2)
  FROM cls c
"""


def build(account, period_start=None):
    if period_start is None:
        row = db.query("""SELECT period_start, period_end FROM ozon_search_query
                          WHERE account=%s AND period_end - period_start >= 6
                          ORDER BY period_start DESC LIMIT 1""", (account,))
        if not row:
            print(f"  {account}: нет полных недель в ozon_search_query — пропуск", flush=True)
            return
        period_start, period_end = row[0]["period_start"], row[0]["period_end"]
    else:
        period_end = db.query("""SELECT max(period_end) pe FROM ozon_search_query
                                 WHERE account=%s AND period_start=%s""",
                              (account, period_start))[0]["pe"]

    db.execute("DELETE FROM mkt_ozon_query_econ WHERE account=%s AND period_start=%s",
               (account, period_start))
    db.execute(SQL, {"acc": account, "ps": period_start, "pe": period_end,
                     "rashod": RASHOD, "tehnika": TEHNIKA, "stop": list(STOP),
                     "dmin": DEMAND_MIN, "kpi": KPI_MARGIN,
                     "rate": PPO_RATE.get(account, 0.07)})

    print(f"  {account} {period_start}..{period_end}", flush=True)
    for r in db.query("""SELECT action, count(*) pairs, count(DISTINCT query) queries,
                                count(DISTINCT sku) skus, round(sum(gmv)) gmv
                           FROM mkt_ozon_query_econ
                          WHERE account=%s AND period_start=%s
                          GROUP BY 1 ORDER BY 2 DESC""", (account, period_start)):
        print(f"    {r['action']:<17} пар {r['pairs']:>6}  фраз {r['queries']:>6}  "
              f"SKU {r['skus']:>5}  GMV {int(r['gmv'] or 0):>8}", flush=True)


def main(account="oz_acc1", period_start=None):
    if account == "all":
        for a in ("oz_acc1", "oz_acc2"):
            main(a, period_start)
        return
    print(f"Витрина фраз Ozon {account}", flush=True)
    build(account, period_start)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "oz_acc1",
         sys.argv[2] if len(sys.argv) > 2 else None)
