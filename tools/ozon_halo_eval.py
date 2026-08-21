# поток: mkt
"""ozon_halo_eval.py — P1-HALO: ореол снятого хвоста. ТОЛЬКО ЧТЕНИЕ.

Наблюдательное квазиэкспериментальное исследование, НЕ рекламный эксперимент: воздействие
(снятие 8 407 SKU 09.08 и возврат половины 19.08) принадлежит историческим E2/E8, мы его
не назначали и не рандомизировали. Причинные утверждения запрещены контрактом
(`docs/experiments/ozon_observational_studies.json`, `causal_claim_allowed: false`).

  build-cohorts   заморозить CORE_A_STABLE_ADVERTISED и BASELINE_NEVER_ADVERTISED (CSV, один раз)
  clusters        дизайн CLUSTER_SPILLOVER: кластеры, баланс, мощность (без оценки исходов)
  dose            фактическая дневная экспозиция хвоста (доза воздействия)
  eval            режимы A / B / C + переходные дни, отчёт в docs/reports/

Что зашито в методику:
  * состав когорт замораживается ТОЛЬКО по данным до 09.08 (`FREEZE_DAY`) — иначе отбор
    шёл бы по последствиям воздействия (post-treatment selection bias). Единственное
    исключение — списки назначений вмешательств (кто был снят волной 1, кто в E3–E8):
    это назначение, а не исход, и оно проходит через `_intervention_assignment()`;
  * остаток, цена и собственная реклама ПОСЛЕ 09.08 — это дневные признаки анализа
    (`eligible_daily`) и sensitivity-разрез, а не основание удалить SKU задним числом;
  * 09.08 (снятие) и 19.08 (возврат) — переходные дни: воздействие менялось внутри дня,
    поэтому они не входят ни в один режим и печатаются отдельными строками;
  * воздействие меряется дозой (показы/клики/расход/доля хвоста), а не флагом «включён»:
    присутствие в снимке `ozon_bids` не доказывает получения трафика;
  * общие поисковые показы (`unique_view_users`) НЕ называются органическими;
  * прибыль по SKU-когорте не считается: нераспределённые расходы канон BI по SKU
    не разносит. По когорте печатается «вклад» — выручка и собственный рекламный
    расход; каноническая реконструкция Финансы→Баланс идёт только по аккаунту.
"""
import sys, csv, json, argparse, datetime as dt

sys.path.insert(0, '/opt/mp-analytics')
from core import db

ACC = 'oz_acc1'
COHORTS = '/opt/mp-analytics/docs/experiments/cohorts'
REPORTS = '/opt/mp-analytics/docs/reports'

# --- границы, за которые отбор состава выходить не имеет права -------------------------
FREEZE_DAY = '2026-08-08'          # последний день ДО снятия хвоста
ADS_BASE = ('2026-07-27', FREEZE_DAY)     # витрина рекламы раньше 27.07 не существует
SALES_BASE = ('2026-06-01', FREEZE_DAY)   # продажи из постингов, глубина есть с 01.05
PRICE_TOL = 0.05                   # >5 % размаха цены на baseline — нестабильная цена
MIN_AD_DAYS = 7                    # из 13 дней baseline-окна рекламы

# --- режимы --------------------------------------------------------------------------
REMOVAL_DAY = '2026-08-09'
RESTORE_DAY = '2026-08-19'
TRANSITION = {REMOVAL_DAY: 'снятие 8 407 SKU хвоста произошло внутри дня',
              RESTORE_DAY: 'возврат половины хвоста (E8) произошёл внутри дня'}
REGIME_A = ('2026-07-27', '2026-08-08')   # хвост в рекламе
REGIME_B = ('2026-08-10', '2026-08-18')   # хвост снят
REGIME_C_START = '2026-08-20'             # половина хвоста возвращена; 19.08 исключён

CORE_A_CSV = COHORTS + '/HALO_CORE_A_STABLE_ADVERTISED_2026-08-21.csv'
BASE_B_CSV = COHORTS + '/HALO_BASELINE_NEVER_ADVERTISED_2026-08-21.csv'
ZERO_AD_CSV = COHORTS + '/HALO_PERSISTENT_ZERO_AD_CORE_2026-08-21.csv'
FUNNEL_CSV = COHORTS + '/HALO_cohort_funnel_2026-08-21.csv'
CLUSTER_CSV = COHORTS + '/HALO_CLUSTER_SPILLOVER_design_2026-08-21.csv'

# Полные имена когорт. «CORE_B» больше не употребляется: у SKU этой когорты после 09.08
# появилась собственная реклама, поэтому «ядро без рекламы» — неверное имя. Baseline-
# свойство («до 09.08 реклама его не касалась») от этого не пострадало, оно и в названии.
NAMES = {'CORE_A': 'CORE_A_STABLE_ADVERTISED',
         'BASE_B': 'BASELINE_NEVER_ADVERTISED',
         'ZERO_AD': 'PERSISTENT_ZERO_AD_CORE'}

MIN_POST_DAYS = 7                  # меньше полной недели после возврата — вердикта нет

# --- зрелость данных ------------------------------------------------------------------
# День нельзя сравнивать, пока источники за него не догрузились. Наблюдаемые лаги:
#   рекламная витрина  — приезжает на следующее утро, ~09:20–12:20 UTC;
#   постинги           — два прогона в сутки (~02:1x и ~19:1x UTC), сутки закрывает
#                        только прогон следующего дня;
#   цены и остатки     — суточный срез в тот же день;
#   транзакции         — месячный период, перезагружается ночью (~02:17 UTC).
# Сутки D считаются закрытыми, если загрузчик отработал уже ПОСЛЕ полуночи D+1 по UTC:
# ночной прогон идёт ~02:1x и подбирает всё, что случилось накануне.
POSTING_CLOSE_UTC = 0              # час UTC дня D+1, после которого сутки D закрыты
NEED_TO_COMPARE = ('ads', 'posting', 'price', 'stock')   # для сравнения когорт
NEED_FOR_ACCOUNT = NEED_TO_COMPARE + ('transaction',)    # плюс канон BI по аккаунту


def _d(x):
    return x if isinstance(x, dt.date) else dt.date.fromisoformat(x)


def assert_baseline_only(*days):
    """Гард против post-treatment selection: критерий отбора не смотрит после 09.08."""
    for x in days:
        if x is not None and _d(x) > _d(FREEZE_DAY):
            raise AssertionError(
                f'критерий отбора смотрит на {x} — позже FREEZE_DAY {FREEZE_DAY}; '
                'признаки после снятия хвоста могут быть только дневными, не отборочными')
    return True


def last_full_day(today=None):
    """Последний день с полной рекламной витриной: она приезжает на следующее утро."""
    r = db.query("SELECT max(stat_date)::text d FROM mkt_ozon_ads_sku_daily WHERE account=%s", (ACC,))
    return r[0]['d']


def maturity(day):
    """Готов ли день к сравнению: догрузились ли за него все нужные источники.

    Возвращает {источник: (готов, пояснение)}. Недогруженный день выглядит как провал
    продаж или как исчезнувшая реклама — это артефакт загрузки, а не эффект.
    """
    d = _d(day)
    close = dt.datetime.combine(d + dt.timedelta(days=1),
                                dt.time(POSTING_CLOSE_UTC), tzinfo=dt.timezone.utc)
    out = {}

    r = db.query("SELECT max(stat_date)::text x FROM mkt_ozon_ads_sku_daily WHERE account=%s", (ACC,))
    last_ads = r[0]['x']
    out['ads'] = (bool(last_ads) and _d(last_ads) >= d,
                  f'витрина доехала до {last_ads}')

    r = db.query("SELECT max(loaded_at) x FROM raw_ozon_posting WHERE account=%s", (ACC,))
    lp = r[0]['x']
    out['posting'] = (bool(lp) and lp >= close,
                      (f'последняя загрузка {lp:%Y-%m-%d %H:%M} UTC; сутки закрывает любой '
                       f'прогон после {close:%Y-%m-%d %H:%M}') if lp else 'загрузок нет')

    for src, sql in (('price', "SELECT count(*) n FROM ozon_price_index WHERE account=%s AND collected_on=%s"),
                     ('stock', "SELECT count(*) n FROM supplier_stock WHERE captured_at::date=%s")):
        n = db.query(sql, (ACC, day) if src == 'price' else (day,))[0]['n']
        out[src] = (n > 0, f'строк за день: {n}')

    r = db.query("""SELECT max(loaded_at) x FROM raw_ozon_transaction
                    WHERE account=%s AND period_from::date<=%s AND period_to::date>=%s""", (ACC, day, day))
    lt = r[0]['x']
    out['transaction'] = (bool(lt) and lt >= close,
                          f'период с этим днём загружен {lt:%Y-%m-%d %H:%M} UTC' if lt
                          else 'периода с этим днём нет')
    return out


def is_mature(day, need=None):
    m = maturity(day)
    return all(m[k][0] for k in (need or NEED_TO_COMPARE))


def mature_through(start=None):
    """Последний день подряд, начиная со start, который зрел по всем источникам сравнения."""
    r = db.query("SELECT max(stat_date)::text x FROM mkt_ozon_ads_sku_daily WHERE account=%s", (ACC,))
    last = r[0]['x']
    if not last:
        return None
    d, ok = _d(last), None
    for _ in range(14):
        if is_mature(d.isoformat()):
            ok = d.isoformat()
            break
        d -= dt.timedelta(days=1)
    return ok


def regime_c(today=None):
    """Режим C кончается последним ЗРЕЛЫМ днём: недогруженный день сравнивать нельзя."""
    end = mature_through()
    return (REGIME_C_START, end) if end and _d(end) >= _d(REGIME_C_START) else None


def days_of(win):
    a, b = _d(win[0]), _d(win[1])
    out, x = [], a
    while x <= b:
        if x.isoformat() not in TRANSITION:
            out.append(x.isoformat())
        x += dt.timedelta(days=1)
    return out


# ======================= назначения вмешательств (не исходы) ==========================
def _intervention_assignment():
    """Списки НАЗНАЧЕНИЙ, известные аналитику: кого снимали волной 1 и кто в E3–E8.

    Это единственное место, где отбор состава смотрит после FREEZE_DAY, и смотрит он
    на назначение вмешательства, а не на его результат. Волна 1 определяется как
    разница снимков состава 08.08 → 09.08 (полный список снятых не сохранялся).
    """
    tail = {r['s'] for r in db.query(
        """SELECT DISTINCT sku::text s FROM ozon_bids WHERE account=%s AND captured_at::date=%s
           EXCEPT SELECT DISTINCT sku::text FROM ozon_bids WHERE account=%s AND captured_at::date=%s""",
        (ACC, FREEZE_DAY, ACC, REMOVAL_DAY))}
    jrn = {}
    for r in db.query("""SELECT sku::text s, string_agg(DISTINCT action, ',') a
                         FROM mkt_ozon_bid_journal WHERE account=%s GROUP BY 1""", (ACC,)):
        jrn[r['s']] = r['a']
    return tail, jrn


# ============================== baseline-признаки =====================================
def _bids_membership():
    assert_baseline_only(FREEZE_DAY)
    prev = (_d(FREEZE_DAY) - dt.timedelta(days=1)).isoformat()
    return {r['s']: int(r['n']) for r in db.query(
        """SELECT sku::text s, count(DISTINCT captured_at::date) n FROM ozon_bids
           WHERE account=%s AND captured_at::date IN (%s, %s) GROUP BY 1""", (ACC, prev, FREEZE_DAY))}


def _ads_baseline():
    assert_baseline_only(*ADS_BASE)
    return {r['s']: r for r in db.query(
        """SELECT sku::text s, count(DISTINCT stat_date) FILTER (WHERE views>0) ad_days,
                  sum(views) views, sum(money_spent) spend
           FROM mkt_ozon_ads_sku_daily WHERE account=%s AND stat_date BETWEEN %s AND %s
           GROUP BY 1""", (ACC, *ADS_BASE))}


def _ever_advertised_before_freeze():
    """Кого реклама вообще касалась до снятия: снимок ставок или ненулевая витрина."""
    assert_baseline_only(FREEZE_DAY)
    return {r['s'] for r in db.query(
        """SELECT DISTINCT sku::text s FROM ozon_bids WHERE account=%s AND captured_at::date<=%s
           UNION SELECT DISTINCT sku::text FROM mkt_ozon_ads_sku_daily
           WHERE account=%s AND stat_date<=%s AND (views>0 OR money_spent>0)""",
        (ACC, FREEZE_DAY, ACC, FREEZE_DAY))}


def _sales_baseline():
    assert_baseline_only(*SALES_BASE)
    return {r['s']: r for r in db.query(
        """SELECT (p->>'sku')::text s, sum((p->>'quantity')::numeric) units,
                  count(DISTINCT r.in_process_at::date) sale_days
           FROM raw_ozon_posting r, jsonb_array_elements(r.payload->'products') p
           WHERE r.account=%s AND r.in_process_at::date BETWEEN %s AND %s GROUP BY 1""",
        (ACC, *SALES_BASE))}


def _stock_baseline():
    assert_baseline_only(FREEZE_DAY)
    return {r['s']: float(r['q'] or 0) for r in db.query(
        """WITH snap AS (SELECT max(captured_at) c FROM supplier_stock WHERE captured_at::date<=%s),
                st AS (SELECT external_code, sum(stock) q FROM supplier_stock, snap
                       WHERE captured_at=snap.c GROUP BY 1)
           SELECT op.sku::text s, st.q FROM ozon_product op JOIN st ON st.external_code=op.offer_id
           WHERE op.account=%s""", (FREEZE_DAY, ACC))}


def _price_baseline():
    """Средняя цена и относительный размах цены на baseline (по дням сбора до 09.08)."""
    assert_baseline_only(*ADS_BASE)
    return {r['s']: r for r in db.query(
        """WITH p AS (SELECT offer_id, collected_on, avg(price) pr FROM ozon_price_index
                      WHERE account=%s AND collected_on BETWEEN %s AND %s GROUP BY 1,2)
           SELECT op.sku::text s, avg(p.pr)::float avg_price, min(p.pr)::float min_price,
                  max(p.pr)::float max_price, count(*) price_days
           FROM ozon_product op JOIN p USING(offer_id)
           WHERE op.account=%s GROUP BY 1""", (ACC, *ADS_BASE, ACC))}


def _offer_map():
    return {r['s']: r['o'] for r in db.query(
        "SELECT sku::text s, offer_id o FROM ozon_product WHERE account=%s", (ACC,))}


# ================================ построение когорт ===================================
def _price_unstable(pb):
    if not pb or not pb.get('price_days'):
        return None
    lo, hi = pb['min_price'], pb['max_price']
    if not lo:
        return None
    return (hi - lo) / lo > PRICE_TOL


def build_cohorts(force=False):
    import os
    if not force and (os.path.exists(CORE_A_CSV) or os.path.exists(BASE_B_CSV)):
        print('Состав уже заморожен. Пересборка только с --force (и это меняет исследование).')
        return
    tail, jrn = _intervention_assignment()
    bids, ads, sales = _bids_membership(), _ads_baseline(), _sales_baseline()
    stock, price, offer = _stock_baseline(), _price_baseline(), _offer_map()
    ever_ads = _ever_advertised_before_freeze()

    rows, funnel = [], {}

    def step(core, name):
        funnel.setdefault(core, []).append(name)

    # ---- CORE_A: был в рекламе оба дня перед снятием и остался нетронутым
    cand_a = [s for s, n in bids.items() if n == 2]
    for s in cand_a:
        a = ads.get(s, {})
        reason = None
        if s in tail:
            reason = 'назначен_в_снятие_волны_1'
        elif s in jrn:
            reason = 'вмешательство_в_журнале:' + jrn[s]
        elif int(a.get('ad_days') or 0) < MIN_AD_DAYS:
            reason = f'рекламная_экспозиция_меньше_{MIN_AD_DAYS}_дней_на_baseline'
        elif stock.get(s, 0) <= 0:
            reason = 'нет_остатка_на_baseline'
        elif _price_unstable(price.get(s)) is True:
            reason = 'цена_нестабильна_на_baseline'
        rows.append({'core': 'CORE_A', 'sku': s, 'offer_id': offer.get(s, ''),
                     'ad_days_baseline': int(a.get('ad_days') or 0),
                     'ad_views_baseline': int(a.get('views') or 0),
                     'spend_baseline': round(float(a.get('spend') or 0), 2),
                     'units_baseline': float((sales.get(s) or {}).get('units') or 0),
                     'stock_baseline': stock.get(s, 0),
                     'price_baseline': round((price.get(s) or {}).get('avg_price') or 0, 2),
                     'price_swing_baseline': round(((price.get(s) or {}).get('max_price') or 0) -
                                                   ((price.get(s) or {}).get('min_price') or 0), 2),
                     'в_когорте': 0 if reason else 1, 'причина_исключения': reason or ''})

    # ---- BASELINE_NEVER_ADVERTISED: до 09.08 реклама его не касалась, но он продавался
    for s, sv in sales.items():
        if s in ever_ads:
            continue
        reason = None
        if s in tail:
            reason = 'назначен_в_снятие_волны_1'
        elif s in jrn:
            reason = 'вмешательство_в_журнале:' + jrn[s]
        elif stock.get(s, 0) <= 0:
            reason = 'нет_остатка_на_baseline'
        elif _price_unstable(price.get(s)) is True:
            reason = 'цена_нестабильна_на_baseline'
        rows.append({'core': 'BASE_B', 'sku': s, 'offer_id': offer.get(s, ''),
                     'ad_days_baseline': 0, 'ad_views_baseline': 0, 'spend_baseline': 0.0,
                     'units_baseline': float(sv.get('units') or 0),
                     'stock_baseline': stock.get(s, 0),
                     'price_baseline': round((price.get(s) or {}).get('avg_price') or 0, 2),
                     'price_swing_baseline': round(((price.get(s) or {}).get('max_price') or 0) -
                                                   ((price.get(s) or {}).get('min_price') or 0), 2),
                     'в_когорте': 0 if reason else 1, 'причина_исключения': reason or ''})

    a_in = [r['sku'] for r in rows if r['core'] == 'CORE_A' and r['в_когорте']]
    b_in = [r['sku'] for r in rows if r['core'] == 'BASE_B' and r['в_когорте']]
    assert not (set(a_in) & set(b_in)), 'CORE_A и BASELINE_NEVER_ADVERTISED пересекаются'
    assert not (set(a_in) & tail) and not (set(b_in) & tail), 'когорта пересекается с хвостом волны 1'

    cols = list(rows[0].keys())
    for path, core in ((CORE_A_CSV, 'CORE_A'), (BASE_B_CSV, 'BASE_B')):
        with open(path, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, lineterminator='\n', fieldnames=cols); w.writeheader()
            for r in rows:
                if r['core'] == core and r['в_когорте']:
                    w.writerow(r)
    with open(FUNNEL_CSV, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, lineterminator='\n', fieldnames=cols); w.writeheader()
        for r in rows:
            w.writerow(r)

    z = build_zero_ad_core(b_in, offer)
    print(funnel_text(rows, tail, jrn))
    print(f'\n{NAMES["CORE_A"]}: {len(a_in)} SKU → {CORE_A_CSV}')
    print(f'{NAMES["BASE_B"]}: {len(b_in)} SKU → {BASE_B_CSV}')
    print(f'{NAMES["ZERO_AD"]}: {len(z)} SKU → {ZERO_AD_CSV}')
    print(f'воронка поимённо: {FUNNEL_CSV}')


def build_zero_ad_core(base_b, offer):
    """PERSISTENT_ZERO_AD_CORE: нулевая собственная реклама во ВСЕХ наблюдаемых режимах.

    ВНИМАНИЕ на статус этой когорты. Она определяется по данным ПОСЛЕ 09.08, то есть
    post-hoc. Отбор идёт по величине воздействия (была ли своя реклама), а не по исходу
    (сколько продал), поэтому это не классический post-treatment selection по результату,
    но и не замороженный на baseline состав. Отсюда её роль: вторичный разрез
    устойчивости рядом с основным ITT по BASELINE_NEVER_ADVERTISED, а не замена ему.
    """
    if not base_b:
        return []
    end = last_full_day()
    rows = {r['s']: r for r in db.query(
        """SELECT sku::text s, sum(views) v, sum(clicks) k, sum(money_spent)::float sp,
                  min(stat_date)::text f, max(stat_date)::text l, string_agg(DISTINCT campaign_id, ',') camp
           FROM mkt_ozon_ads_sku_daily WHERE account=%s AND sku::text = ANY(%s)
             AND stat_date >= %s AND stat_date <= %s AND (views>0 OR money_spent>0)
           GROUP BY 1""", (ACC, base_b, REMOVAL_DAY, end))}
    out = []
    with open(ZERO_AD_CSV, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, lineterminator='\n', fieldnames=['sku', 'offer_id', 'в_когорте', 'показы_после_09_08',
                                          'клики_после_09_08', 'расход_после_09_08',
                                          'первый_день_рекламы', 'последний_день_рекламы',
                                          'кампании', 'причина_исключения'])
        w.writeheader()
        for s in sorted(base_b):
            r = rows.get(s)
            ok = r is None
            if ok:
                out.append(s)
            w.writerow({'sku': s, 'offer_id': offer.get(s, ''), 'в_когорте': 1 if ok else 0,
                        'показы_после_09_08': int((r or {}).get('v') or 0),
                        'клики_после_09_08': int((r or {}).get('k') or 0),
                        'расход_после_09_08': round(float((r or {}).get('sp') or 0), 2),
                        'первый_день_рекламы': (r or {}).get('f', ''),
                        'последний_день_рекламы': (r or {}).get('l', ''),
                        'кампании': (r or {}).get('camp', ''),
                        'причина_исключения': '' if ok else 'появилась_собственная_реклама_после_09_08'})
    return out


ORDER_A = ['назначен_в_снятие_волны_1', 'вмешательство_в_журнале',
           f'рекламная_экспозиция_меньше_{MIN_AD_DAYS}_дней_на_baseline',
           'нет_остатка_на_baseline', 'цена_нестабильна_на_baseline']


def funnel_text(rows, tail, jrn):
    out = []
    for core, title in (('CORE_A', NAMES['CORE_A']), ('BASE_B', NAMES['BASE_B'])):
        rs = [r for r in rows if r['core'] == core]
        out.append(f'\n### Воронка {title}')
        out.append(f'| шаг | SKU выбыло | осталось |')
        out.append('|---|---:|---:|')
        left = len(rs)
        out.append(f'| кандидаты (baseline-критерии до {FREEZE_DAY}) | — | {left} |')
        for step in ORDER_A:
            n = len([r for r in rs if r['причина_исключения'].startswith(step)])
            if core == 'BASE_B' and step.startswith('рекламная_экспозиция'):
                continue
            left -= n
            out.append(f'| {step} | {n} | {left} |')
        out.append(f'| **финальная когорта** | — | **{len([r for r in rs if r["в_когорте"]])}** |')
    return '\n'.join(out)


def read_cohort(path):
    """SKU замороженного состава. Файл может нести и исключённые строки — тогда есть
    колонка `в_когорте`, и берутся только помеченные: диагностика лежит рядом с составом."""
    with open(path, encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    if rows and 'в_когорте' in rows[0]:
        rows = [r for r in rows if str(r['в_когорте']).lower() in ('да', 'true', '1', 'yes')]
    return [r['sku'] for r in rows]


# =============================== дневные метрики ======================================
MEASURES = """Единые определения, используемые ВО ВСЕХ таблицах отчёта:

* **постинги** — DISTINCT `posting_number`, в составе которых есть хотя бы один SKU когорты;
  все статусы, включая отменённые. Постинг с двумя SKU когорты — это ОДИН постинг.
* **отменённые** — из них со статусом `cancelled`.
* **завершённые** = постинги − отменённые.
* **штук** и **выручка, ₽** — по позициям когорты в НЕотменённых постингах
  (`quantity` и `price × quantity`). Позиции чужих SKU в том же постинге не считаются.
* **отменённые штуки / выручка** — то же по отменённым постингам, отдельной строкой.
* **SKU-дни в наличии** — дни, в которые остаток МС по `external_code` был > 0.
"""


def daily_sales(skus, d0, d1):
    """Продажи по дням в единых определениях (см. MEASURES)."""
    if not skus:
        return {}
    rows = db.query(
        """SELECT r.in_process_at::date::text d,
                  count(DISTINCT r.posting_number) postings,
                  count(DISTINCT r.posting_number) FILTER (WHERE r.status='cancelled') cancelled,
                  sum((p->>'quantity')::numeric) FILTER (WHERE r.status<>'cancelled') units,
                  sum((p->>'price')::numeric*(p->>'quantity')::numeric)
                      FILTER (WHERE r.status<>'cancelled') gmv,
                  sum((p->>'quantity')::numeric) FILTER (WHERE r.status='cancelled') units_canc,
                  sum((p->>'price')::numeric*(p->>'quantity')::numeric)
                      FILTER (WHERE r.status='cancelled') gmv_canc,
                  count(DISTINCT (p->>'sku')) FILTER (WHERE r.status<>'cancelled') sku_sold
           FROM raw_ozon_posting r, jsonb_array_elements(r.payload->'products') p
           WHERE r.account=%s AND (p->>'sku')::text = ANY(%s)
             AND r.in_process_at::date BETWEEN %s AND %s GROUP BY 1""", (ACC, skus, d0, d1))
    return {r['d']: {k: float(r[k] or 0) for k in
                     ('postings', 'cancelled', 'units', 'gmv', 'units_canc', 'gmv_canc', 'sku_sold')}
            for r in rows}


def daily_ads(skus, d0, d1):
    if not skus:
        return {}
    rows = db.query(
        """SELECT stat_date::text d, sum(views) views, sum(clicks) clicks, sum(money_spent) spend,
                  count(DISTINCT sku) FILTER (WHERE views>0) sku_shown
           FROM mkt_ozon_ads_sku_daily WHERE account=%s AND sku::text = ANY(%s)
             AND stat_date BETWEEN %s AND %s GROUP BY 1""", (ACC, skus, d0, d1))
    return {r['d']: {k: float(r[k] or 0) for k in ('views', 'clicks', 'spend', 'sku_shown')} for r in rows}


def daily_available(skus, d0, d1):
    """SKU-дни в наличии: остаток МС на снимок этого дня (ФБС-only, источник supplier_stock)."""
    if not skus:
        return {}
    rows = db.query(
        """WITH sk AS (SELECT op.sku::text sku, op.offer_id FROM ozon_product op
                       WHERE op.account=%s AND op.sku::text = ANY(%s)),
                snap AS (SELECT captured_at::date d, max(captured_at) c FROM supplier_stock
                         WHERE captured_at::date BETWEEN %s AND %s GROUP BY 1),
                st AS (SELECT snap.d, s.external_code, sum(s.stock) q FROM supplier_stock s
                       JOIN snap ON s.captured_at=snap.c GROUP BY 1,2)
           SELECT st.d::text d, count(DISTINCT sk.sku) FILTER (WHERE st.q>0) sku_avail
           FROM st JOIN sk ON sk.offer_id=st.external_code GROUP BY 1""",
        (ACC, skus, d0, d1))
    return {r['d']: float(r['sku_avail'] or 0) for r in rows}


def daily_price_shift(skus, d0, d1, base_price):
    """Доля SKU, у которых цена дня отличается от baseline более чем на 5 %."""
    if not skus:
        return {}
    rows = db.query(
        """WITH sk AS (SELECT op.sku::text sku, op.offer_id FROM ozon_product op
                       WHERE op.account=%s AND op.sku::text = ANY(%s))
           SELECT pi.collected_on::text d, sk.sku, avg(pi.price)::float pr
           FROM ozon_price_index pi JOIN sk ON sk.offer_id=pi.offer_id
           WHERE pi.account=%s AND pi.collected_on BETWEEN %s AND %s GROUP BY 1,2""",
        (ACC, skus, ACC, d0, d1))
    agg = {}
    for r in rows:
        b = base_price.get(r['sku'])
        if not b:
            continue
        a = agg.setdefault(r['d'], [0, 0])
        a[1] += 1
        if abs(r['pr'] - b) / b > PRICE_TOL:
            a[0] += 1
    return {d: (v[0] / v[1] if v[1] else None) for d, v in agg.items()}


def account_daily_ads(d0, d1):
    rows = db.query(
        """SELECT stat_date::text d, sum(views) views, sum(clicks) clicks, sum(money_spent) spend,
                  count(DISTINCT sku) FILTER (WHERE views>0) sku_shown
           FROM mkt_ozon_ads_sku_daily WHERE account=%s AND stat_date BETWEEN %s AND %s GROUP BY 1""",
        (ACC, d0, d1))
    return {r['d']: {k: float(r[k] or 0) for k in ('views', 'clicks', 'spend', 'sku_shown')} for r in rows}


def search_windows(skus, d0, d1):
    """Общие поисковые показы и позиция. НЕ органика: витрина не делит платный и бесплатный трафик."""
    if not skus:
        return []
    return db.query(
        """SELECT period_start::text a, period_end::text b, count(DISTINCT sku) skus,
                  sum(unique_view_users) view_users, sum(unique_search_users) search_users,
                  avg(position)::float pos, sum(order_count) orders, sum(gmv)::float gmv
           FROM ozon_search_product WHERE account=%s AND sku::text = ANY(%s)
             AND period_end >= %s AND period_start <= %s GROUP BY 1,2 ORDER BY 1""",
        (ACC, skus, d0, d1))


# ================================ доза хвоста =========================================
def tail_dose(d0, d1):
    tail, _ = _intervention_assignment()
    tail = list(tail)
    t, acc = daily_ads(tail, d0, d1), account_daily_ads(d0, d1)
    out = []
    for d in sorted(set(list(t) + list(acc))):
        td, ad = t.get(d, {}), acc.get(d, {})
        out.append({'день': d, 'реж': regime_of(d),
                    'sku_хвоста_с_показами': int(td.get('sku_shown', 0)),
                    'показы_хвоста': int(td.get('views', 0)),
                    'клики_хвоста': int(td.get('clicks', 0)),
                    'расход_хвоста': round(td.get('spend', 0), 0),
                    'доля_в_расходе': round(td.get('spend', 0) / ad['spend'], 3) if ad.get('spend') else None,
                    'доля_в_показах': round(td.get('views', 0) / ad['views'], 3) if ad.get('views') else None})
    return out


def rest_dose(d0, d1):
    """Экспозиция ОСТАЛЬНОЙ рекламы аккаунта — всё, что не хвост волны 1."""
    tail, _ = _intervention_assignment()
    rows = db.query(
        """SELECT stat_date::text d, sum(views) v, sum(clicks) k, sum(money_spent)::float sp,
                  count(DISTINCT sku) FILTER (WHERE views>0) n
           FROM mkt_ozon_ads_sku_daily
           WHERE account=%s AND stat_date BETWEEN %s AND %s AND NOT (sku::text = ANY(%s))
           GROUP BY 1""", (ACC, d0, d1, list(tail)))
    return {r['d']: {'views': float(r['v'] or 0), 'clicks': float(r['k'] or 0),
                     'spend': float(r['sp'] or 0), 'sku_shown': float(r['n'] or 0)} for r in rows}


def rvi_windows(skus, d0, d1):
    """Индекс относительной поисковой видимости.

    Показы когорты в поиске нормируются на изменение спроса по её же релевантным
    запросам: `unique_search_users` — это пользователи, искавшие запросы, по которым
    товар вообще показывался. RVI = (показы_W/показы_база) ÷ (спрос_W/спрос_база).
    RVI > 1 — видимость выросла быстрее спроса; RVI < 1 — отстала от него.
    База — первое недельное окно, целиком лежащее в режиме A.
    `unique_view_users` НЕ является органикой: витрина не делит платный и бесплатный трафик.
    """
    ws = search_windows(skus, d0, d1)
    out, base = [], None
    for r in ws:
        rg = sorted({regime_of(x) for x in days_of((r['a'], r['b']))} - {'—'})
        views, demand = float(r['view_users'] or 0), float(r['search_users'] or 0)
        row = {'окно': f"{r['a']}..{r['b']}", 'режимы': rg, 'sku': r['skus'],
               'показы': views, 'спрос': demand,
               'доля_показа_в_спросе': views / demand if demand else None,
               'позиция': r['pos'], 'заказы': float(r['orders'] or 0)}
        if base is None and rg == ['A'] and demand and views:
            base = row
        out.append(row)
    for row in out:
        if base and base['показы'] and base['спрос'] and row['спрос']:
            row['RVI'] = (row['показы'] / base['показы']) / (row['спрос'] / base['спрос'])
        else:
            row['RVI'] = None
        row['база'] = bool(base) and row is base
    return out, base


def price_diag(skus, bp, day):
    """Распределение отклонения цены от baseline на конкретный день + следы акций Ozon."""
    if not skus:
        return None
    rows = db.query(
        """WITH sk AS (SELECT op.sku::text sku, op.offer_id FROM ozon_product op
                       WHERE op.account=%s AND op.sku::text = ANY(%s))
           SELECT sk.sku, avg(pi.price)::float pr, avg(pi.marketing_price)::float mkt,
                  bool_or(pi.auto_action_enabled) auto_act
           FROM ozon_price_index pi JOIN sk ON sk.offer_id=pi.offer_id
           WHERE pi.account=%s AND pi.collected_on=%s GROUP BY 1""", (ACC, skus, ACC, day))
    B = [('ниже −20 %', -9, -.20), ('−20…−10 %', -.20, -.10), ('−10…−5 %', -.10, -.05),
         ('в пределах ±5 %', -.05, .05), ('+5…10 %', .05, .10), ('+10…20 %', .10, .20),
         ('выше +20 %', .20, 9)]
    buckets = {b[0]: 0 for b in B}
    dev, auto, mkt_n = [], 0, 0
    for r in rows:
        b = bp.get(r['sku'])
        if not b or not r['pr']:
            continue
        x = (r['pr'] - b) / b
        dev.append(x)
        for name, lo, hi in B:
            if lo < x <= hi or (name == 'ниже −20 %' and x <= -.20):
                buckets[name] += 1
                break
        if r['auto_act']:
            auto += 1
        if r['mkt']:
            mkt_n += 1
    dev.sort()
    return {'день': day, 'n': len(dev), 'buckets': buckets,
            'медиана': dev[len(dev) // 2] if dev else None,
            'доля_вне_5': sum(1 for x in dev if abs(x) > PRICE_TOL) / len(dev) if dev else None,
            'доля_вверх': sum(1 for x in dev if x > PRICE_TOL) / len(dev) if dev else None,
            'доля_вниз': sum(1 for x in dev if x < -PRICE_TOL) / len(dev) if dev else None,
            'auto_action_enabled': auto, 'marketing_price_заполнен': mkt_n}


def posting_vs_price(skus, d0, d1):
    """Цена постинга против прайса: косвенный след скидок, не видимых в ozon_price_index."""
    if not skus:
        return None
    rows = db.query(
        """WITH sk AS (SELECT op.sku::text sku, op.offer_id FROM ozon_product op
                       WHERE op.account=%s AND op.sku::text = ANY(%s)),
                pos AS (SELECT r.in_process_at::date d, (p->>'sku')::text sku, (p->>'price')::float pp
                        FROM raw_ozon_posting r, jsonb_array_elements(r.payload->'products') p
                        WHERE r.account=%s AND r.status<>'cancelled'
                          AND r.in_process_at::date BETWEEN %s AND %s
                          AND (p->>'sku')::text = ANY(%s))
           SELECT pos.d::text d, pos.sku, pos.pp, avg(pi.price)::float ip
           FROM pos JOIN sk ON sk.sku=pos.sku
           JOIN ozon_price_index pi ON pi.offer_id=sk.offer_id AND pi.account=%s
                                   AND pi.collected_on=pos.d
           GROUP BY 1,2,3""", (ACC, skus, ACC, d0, d1, skus, ACC))
    dv = sorted((r['pp'] - r['ip']) / r['ip'] for r in rows if r['ip'])
    if not dv:
        return None
    return {'позиций': len(dv), 'медиана': dv[len(dv) // 2],
            'ниже_прайса_на_5': sum(1 for x in dv if x < -PRICE_TOL),
            'выше_прайса_на_5': sum(1 for x in dv if x > PRICE_TOL)}


def campaign_shift():
    """Почему у «никогда не рекламировавшихся» появилась реклама: смена состава кампаний.

    Сравниваются снимки ставок 08.08 и 09.08. Кампании, впервые появившиеся на снятии,
    и есть источник новой экспозиции у той самой популяции, из которой набран BASELINE.
    """
    prev, cur = FREEZE_DAY, min(TRANSITION)   # снимок заморозки против дня снятия
    snap = {}
    for r in db.query("""SELECT captured_at::date::text d, sku::text s, campaign_id::text ci
                         FROM ozon_bids WHERE account=%s AND captured_at::date IN (%s,%s)""",
                      (ACC, prev, cur)):
        snap.setdefault(r['d'], set()).add((r['s'], r['ci']))
    a = {x[0] for x in snap.get(prev, set())}
    b = {x[0] for x in snap.get(cur, set())}
    new = b - a
    bycamp = {}
    for sku, ci in snap.get(cur, set()):
        if sku in new:
            bycamp[ci] = bycamp.get(ci, 0) + 1
    names = {r['ci']: r['n'] for r in db.query(
        """SELECT DISTINCT campaign_id::text ci, campaign_title n FROM ozon_bids
           WHERE account=%s AND campaign_id::text = ANY(%s)""", (ACC, list(bycamp)))} if bycamp else {}
    nl = list(new)
    pre = db.query("""SELECT count(DISTINCT sku) n, sum(views) v, sum(money_spent)::float m
                      FROM mkt_ozon_ads_sku_daily WHERE account=%s AND sku::text = ANY(%s)
                        AND stat_date < %s AND views>0""", (ACC, nl, cur))[0] if nl else None
    post = db.query("""SELECT count(DISTINCT sku) n, sum(views) v, sum(money_spent)::float m
                       FROM mkt_ozon_ads_sku_daily WHERE account=%s AND sku::text = ANY(%s)
                         AND stat_date >= %s AND views>0""", (ACC, nl, cur))[0] if nl else None
    # записей о САМОМ появлении в кампании: журнал на день снятия и раньше
    jr = db.query("""SELECT count(*) n FROM mkt_ozon_bid_journal
                     WHERE sku::text = ANY(%s) AND decided_on <= %s""", (nl, cur))[0]['n'] if nl else 0
    jr_after = db.query("""SELECT min(decided_on)::text d, count(DISTINCT sku) s
                           FROM mkt_ozon_bid_journal WHERE sku::text = ANY(%s)
                             AND decided_on > %s""", (nl, cur))[0] if nl else None
    return {'prev': prev, 'cur': cur, 'ушло': len(a - b), 'пришло': len(new), 'по_кампаниям': bycamp, 'имена': names,
            'до': pre, 'после': post, 'в_журнале': jr, 'журнал_потом': jr_after, 'new': new}


def regime_of(day):
    if day in TRANSITION:
        return 'TRANSITION_DAY'
    c = regime_c()
    if _d(REGIME_A[0]) <= _d(day) <= _d(REGIME_A[1]):
        return 'A'
    if _d(REGIME_B[0]) <= _d(day) <= _d(REGIME_B[1]):
        return 'B'
    if c and _d(c[0]) <= _d(day) <= _d(c[1]):
        return 'C'
    return '—'


# ================================== оценка ============================================
def cohort_block(skus, days, base_price):
    """Сводка когорты за набор дней. Определения — MEASURES, одни и те же везде."""
    if not days:
        return {}
    d0, d1 = days[0], days[-1]
    sl, a, av = daily_sales(skus, d0, d1), daily_ads(skus, d0, d1), daily_available(skus, d0, d1)
    ps = daily_price_shift(skus, d0, d1, base_price)
    g = {'дней': len(days), 'sku_в_когорте': len(skus)}
    for k in ('postings', 'cancelled', 'units', 'gmv', 'units_canc', 'gmv_canc'):
        g[k] = sum(sl.get(d, {}).get(k, 0) for d in days)
    g['завершённые'] = g['postings'] - g['cancelled']
    g['sku_дней_в_наличии'] = sum(av.get(d, 0) for d in days)
    g['sku_продававшихся'] = len({r['sku'] for r in db.query(
        """SELECT DISTINCT (p->>'sku')::text sku FROM raw_ozon_posting r,
                  jsonb_array_elements(r.payload->'products') p
           WHERE r.account=%s AND (p->>'sku')::text = ANY(%s) AND r.status<>'cancelled'
             AND r.in_process_at::date = ANY(%s::date[])""",
        (ACC, skus, days))}) if skus else 0
    for k in ('views', 'clicks', 'spend'):
        g['ads_' + k] = sum(a.get(d, {}).get(k, 0) for d in days)
    g.update(ad_efficiency(g['ads_views'], g['ads_clicks'], g['ads_spend']))
    sd = g['sku_дней_в_наличии']
    g['завершённых_на_100_sku_дней'] = 100 * g['завершённые'] / sd if sd else None
    g['выручка_на_100_sku_дней'] = 100 * g['gmv'] / sd if sd else None
    g['завершённых_в_день'] = g['завершённые'] / len(days)
    g['выручка_в_день'] = g['gmv'] / len(days)
    g['штук_в_день'] = g['units'] / len(days)
    g['доля_отмен'] = g['cancelled'] / g['postings'] if g['postings'] else None
    g['расход_в_день'] = g['ads_spend'] / len(days)
    g['показы_в_день'] = g['ads_views'] / len(days)
    pv = [ps[d] for d in days if ps.get(d) is not None]
    g['доля_sku_с_ценой_±>5%'] = sum(pv) / len(pv) if pv else None
    g['будни'] = len([d for d in days if _d(d).weekday() < 5])
    g['выходные'] = len(days) - g['будни']
    return g


def ad_efficiency(views, clicks, spend):
    """CPM, CPC, CTR. Пустые знаменатели дают None, а не ноль."""
    return {'CPM': 1000 * spend / views if views else None,
            'CPC': spend / clicks if clicks else None,
            'CTR': 100 * clicks / views if views else None}


def weekday_split(skus, days, base_price):
    wd = [d for d in days if _d(d).weekday() < 5]
    we = [d for d in days if _d(d).weekday() >= 5]
    return cohort_block(skus, wd, base_price), cohort_block(skus, we, base_price)


def base_prices(skus):
    pb = _price_baseline()
    return {s: (pb.get(s) or {}).get('avg_price') for s in skus if (pb.get(s) or {}).get('avg_price')}


def account_canonical(win):
    """Финрезультат аккаунта по канонической логике BI (reconstruction Финансы→Баланс).

    Только аккаунтный уровень: нераспределённые расходы по SKU канон не разносит,
    поэтому переносить эту цифру на когорту нельзя. Методика BI не меняется —
    вызывается существующая функция отчёта.
    """
    try:
        sys.path.insert(0, '/opt/mp-analytics')
        from reports.ozon_mp_report import _balance_range
        d2 = (_d(win[1]) + dt.timedelta(days=1)).isoformat()
        mags, sub, _det, _sc = _balance_range(ACC, win[0], d2)
        return mags
    except Exception as e:
        return {'ОШИБКА': str(e)[:120]}


def fmt(v, nd=0):
    if v is None:
        return '—'
    if isinstance(v, float):
        return f'{v:,.{nd}f}'.replace(',', ' ')
    return str(v)


# ======================= CLUSTER_SPILLOVER: дизайн, баланс, мощность ==================
PRE_RESTORE = '2026-08-18'          # признаки кластеров не смотрят на возврат и позже
E8_A_CSV = COHORTS + '/E8_treatment_2026-08-20.csv'
E8_B_CSV = COHORTS + '/E8_control_2026-08-20.csv'
MAX_QUERY_SKU = 40                  # запрос шире — головной («принтер»), он не различает товар
MIN_CLUSTER_E8 = 2                  # кластер без хотя бы двух E8-SKU не даёт разброса доли
Z_A, Z_B = 1.96, 0.84               # двусторонняя α = 5 %, мощность 80 %


def assert_pre_restore_only(*days):
    """Гард кластеризации: признаки формируются только по данным до возврата 19.08."""
    for x in days:
        if x is not None and _d(x) > _d(PRE_RESTORE):
            raise AssertionError(
                f'признак кластера смотрит на {x} — позже PRE_RESTORE {PRE_RESTORE}; '
                'состав кластеров обязан быть независим от того, кого вернули')
    return True


def _primary_queries():
    """Различающий поисковый запрос SKU по неделям ДО возврата — основа общности кластера.

    Головной запрос («принтер», 3 906 SKU) объединяет весь ассортимент и кластером не является:
    берём самый спросовый из запросов, привязанных не более чем к MAX_QUERY_SKU карточкам.
    """
    assert_pre_restore_only(PRE_RESTORE)
    rows = db.query(
        """SELECT sku::text s, lower(btrim(query)) q, sum(unique_search_users) u
           FROM ozon_search_query WHERE account=%s AND period_end <= %s
           GROUP BY 1,2""", (ACC, PRE_RESTORE))
    width = {}
    for r in rows:
        width[r['q']] = width.get(r['q'], 0) + 1
    best = {}
    for r in rows:
        if width[r['q']] > MAX_QUERY_SKU:
            continue
        cur = best.get(r['s'])
        if not cur or (r['u'] or 0) > cur[1]:
            best[r['s']] = (r['q'], r['u'] or 0)
    return {s: v[0] for s, v in best.items()}


def _printer_models():
    """Модели принтеров из названия карточки: «… для принтеров <модели>»."""
    import re
    out = {}
    for r in db.query("SELECT sku::text s, name FROM ozon_product WHERE account=%s AND name IS NOT NULL", (ACC,)):
        n = r['name']
        m = re.split(r'для принтеров', n, flags=re.I)
        models = m[1] if len(m) > 1 else ''
        toks = [t.strip().lower() for t in re.split(r'[,;/]', models) if len(t.strip()) > 2]
        code = re.match(r'^\s*(?:Комплект картриджей|Заправочный комплект|Картридж|Тонер-картридж)?\s*([A-Z0-9][A-Z0-9\-\. ]{2,14})', n, re.I)
        out[r['s']] = {'модели': set(toks[:6]),
                       'код': (code.group(1).strip().lower() if code else ''),
                       'набор': bool(re.search(r'\(\s*\d+\s*шт', n)), }
    return out


def build_clusters():
    """Кластеры = SKU с общим главным поисковым запросом (данные только до 19.08).

    Совместимость и модель принтера идут вторым признаком: они проверяют, что кластер
    по запросу не склеил разнородный товар. Возврат E8-A внутри кластера случаен —
    именно это и есть источник экзогенной вариации экспозиции.
    """
    q = _primary_queries()
    meta = _printer_models()
    a = set(read_cohort(E8_A_CSV))
    b = set(read_cohort(E8_B_CSV))
    tail, jrn = _intervention_assignment()
    cl = {}
    for sku, query in q.items():
        c = cl.setdefault(query, {'sku': set(), 'e8a': set(), 'e8b': set(), 'receivers': set()})
        c['sku'].add(sku)
        if sku in a:
            c['e8a'].add(sku)
        elif sku in b:
            c['e8b'].add(sku)
        elif sku not in jrn and sku not in tail:
            # receiver: своей рекламы ему не назначали ни E8, ни журнал, ни волна 1
            c['receivers'].add(sku)
    for k, c in cl.items():
        c['e8'] = len(c['e8a']) + len(c['e8b'])
        c['доля_A'] = len(c['e8a']) / c['e8'] if c['e8'] else None
        c['модели'] = len({m for s in c['sku'] for m in meta.get(s, {}).get('модели', set())})
        c['наборов'] = sum(1 for s in c['sku'] if meta.get(s, {}).get('набор'))
    return cl


def sku_baseline_bulk(skus, win):
    """Ковариаты ДО возврата по каждому SKU разом: 4 запроса на всё, а не 5 на кластер.

    Считаются ПОЗИЦИИ в незавершённых-неотменённых постингах, а не постинги: постинг с двумя
    SKU кластера иначе засчитался бы дважды (та же ловушка, что MEASURES закрывает в основных
    таблицах). Это ковариата баланса, а не исход исследования.
    """
    assert_pre_restore_only(win[1])
    d0, d1 = win
    out = {s: {'выручка': 0.0, 'позиций': 0, 'показы': 0.0, 'расход': 0.0, 'спрос': 0.0} for s in skus}
    for r in db.query(
            """SELECT (p->>'sku')::text sku, count(*) n,
                      sum((p->>'price')::numeric * (p->>'quantity')::int) gmv
               FROM raw_ozon_posting r, jsonb_array_elements(r.payload->'products') p
               WHERE r.account=%s AND (p->>'sku')::text = ANY(%s) AND r.status<>'cancelled'
                 AND r.in_process_at::date BETWEEN %s AND %s GROUP BY 1""",
            (ACC, skus, d0, d1)):
        if r['sku'] in out:
            out[r['sku']].update({'выручка': float(r['gmv'] or 0), 'позиций': int(r['n'] or 0)})
    for r in db.query(
            """SELECT sku::text s, sum(views) v, sum(money_spent) m FROM mkt_ozon_ads_sku_daily
               WHERE account=%s AND sku::text = ANY(%s) AND stat_date BETWEEN %s AND %s
               GROUP BY 1""", (ACC, skus, d0, d1)):
        if r['s'] in out:
            out[r['s']]['показы'] = float(r['v'] or 0); out[r['s']]['расход'] = float(r['m'] or 0)
    for r in db.query(
            """SELECT sku::text s, sum(unique_search_users) u FROM ozon_search_query
               WHERE account=%s AND sku::text = ANY(%s) AND period_end <= %s GROUP BY 1""",
            (ACC, skus, PRE_RESTORE)):
        if r['s'] in out:
            out[r['s']]['спрос'] = float(r['u'] or 0)
    return out


def cluster_power(rows, win):
    """Мощность на уровне кластера: разброс дневной выручки на receiver в режиме B."""
    import math
    n_days = len(days_of(win))
    xs = sorted(r['baseline_выручка_receivers_₽'] / (r['receivers'] * n_days) for r in rows)
    if len(xs) < 4:
        return {'кластеров': len(xs), 'MDE_доля': None,
                'вывод': 'кластеров слишком мало, мощность не оценивается'}
    mean = sum(xs) / len(xs)
    sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (len(xs) - 1))
    n1 = len(xs) // 2
    mde = (Z_A + Z_B) * sd * math.sqrt(2 / n1)
    return {'кластеров': len(xs), 'дней_в_базисе': n_days, 'групп_по': n1,
            'среднее_₽_на_receiver_в_день': mean, 'sd_между_кластерами': sd,
            'MDE_₽_на_receiver_в_день': mde,
            'MDE_доля': mde / mean if mean else None}


def cluster_balance(rows, win):
    """Баланс: высокая и низкая доля случайно возвращённых E8-A против ковариат ДО возврата.

    Стандартизованная разность |d| > 0,25 считается дисбалансом: экспозиция тогда связана
    с самим кластером, и разница исходов уже не приписывается возврату.
    """
    import math, statistics as st
    if len(rows) < 8:
        return {'строк': len(rows), 'вывод': 'кластеров слишком мало для проверки баланса'}, []
    med = st.median([r['доля_A'] for r in rows])
    # ничьи по медиане отбрасываются: при жеребьёвке 50/50 на доле ровно 0,5 сидит масса
    # кластеров, и отнесение их в одну сторону само создаёт видимость дисбаланса
    hi = [r for r in rows if r['доля_A'] > med]
    lo = [r for r in rows if r['доля_A'] < med]
    nd = len(days_of(win))
    cov = {'sku_в_кластере': lambda r: r['sku_в_кластере'],
           'E8_всего': lambda r: r['E8_всего'],
           'receivers': lambda r: r['receivers'],
           'моделей_принтеров': lambda r: r['моделей_принтеров'],
           'наборов': lambda r: r['наборов'],
           '₽_на_receiver_в_день': lambda r: r['baseline_выручка_receivers_₽'] / (r['receivers'] * nd),
           'показов_на_receiver_в_день': lambda r: r['baseline_показы_receivers'] / (r['receivers'] * nd),
           'спрос_на_receiver': lambda r: r['спрос_receivers'] / r['receivers']}
    out = []
    worst = 0.0
    for name, f in cov.items():
        xh, xl = [f(r) for r in hi], [f(r) for r in lo]
        mh, ml = sum(xh) / len(xh), sum(xl) / len(xl)
        vh = st.pvariance(xh); vl = st.pvariance(xl)
        pooled = math.sqrt((vh + vl) / 2)
        d = (mh - ml) / pooled if pooled else 0.0
        worst = max(worst, abs(d))
        out.append({'ковариата': name, 'доля_A_выше_медианы': mh, 'доля_A_ниже': ml,
                    'станд_разность': d, 'дисбаланс': 'да' if abs(d) > 0.25 else 'нет'})
    return {'медиана_доли_A': med, 'кластеров_высоких': len(hi), 'кластеров_низких': len(lo),
            'ничьих_отброшено': len(rows) - len(hi) - len(lo),
            'макс_|d|': worst, 'баланс': 'достаточен' if worst <= 0.25 else 'недостаточен'}, out


def cmd_clusters():
    """Только дизайн: состав кластеров, баланс и мощность. Исходы НЕ считаются."""
    cl = build_clusters()
    usable = {k: c for k, c in cl.items() if c['e8'] >= MIN_CLUSTER_E8 and c['receivers']}
    win = REGIME_B
    allr = sorted({s for c in usable.values() for s in c['receivers']})
    bl = sku_baseline_bulk(allr, win) if allr else {}
    rows = []
    for k, c in usable.items():
        rc = list(c['receivers'])
        agg = {m: sum(bl.get(s, {}).get(m, 0) for s in rc)
               for m in ('выручка', 'позиций', 'показы', 'расход', 'спрос')}
        rows.append({'кластер': k[:60], 'sku_в_кластере': len(c['sku']), 'E8_всего': c['e8'],
                     'E8_A': len(c['e8a']), 'доля_A': round(c['доля_A'], 3),
                     'receivers': len(rc), 'моделей_принтеров': c['модели'], 'наборов': c['наборов'],
                     'baseline_выручка_receivers_₽': round(agg['выручка'], 0),
                     'baseline_позиций_receivers': agg['позиций'],
                     'baseline_показы_receivers': agg['показы'],
                     'baseline_расход_receivers_₽': round(agg['расход'], 0),
                     'спрос_receivers': agg['спрос']})
    rows.sort(key=lambda r: (-r['доля_A'], -r['receivers']))
    if rows:
        with open(CLUSTER_CSV, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, lineterminator='\n', fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    live = [r for r in rows if r['baseline_выручка_receivers_₽'] > 0]
    diag = {'кластеров': len(rows), 'с_ненулевой_выручкой_receivers': len(live),
            'доля_вырожденных': 1 - len(live) / len(rows) if rows else None}
    bal, baltab = cluster_balance(rows, win)
    return {'clusters': cl, 'usable': usable, 'rows': rows, 'diag': diag,
            'power_all': cluster_power(rows, win), 'power_live': cluster_power(live, win),
            'balance': bal, 'balance_tab': baltab}


def cmd_eval(as_of=None):
    day = as_of or dt.date.today().isoformat()
    c = regime_c()
    core_a, core_b = read_cohort(CORE_A_CSV), read_cohort(BASE_B_CSV)
    zero_ad = read_cohort(ZERO_AD_CSV)
    bp_a, bp_b = base_prices(core_a), base_prices(core_b)
    bp_z = base_prices(zero_ad)
    regimes = [('A', REGIME_A, 'хвост в рекламе'), ('B', REGIME_B, 'хвост снят')]
    if c:
        regimes.append(('C', c, 'половина хвоста возвращена'))
    post_days = len(days_of(c)) if c else 0
    verdict = 'INSUFFICIENT_POST_PERIOD' if post_days < MIN_POST_DAYS else 'READY_FOR_EVALUATION'

    o = [f'# P1-HALO — ореол снятого хвоста, прогон {day}', '',
         f'**Статус: {verdict}.** Полных дней в режиме C: {post_days} из {MIN_POST_DAYS} '
         f'минимально нужных. Причинный вывод контрактом запрещён '
         f'(`causal_claim_allowed: false`): воздействие не рандомизировалось нами, '
         f'а параллельно менялись сезонность, спрос и другие части аккаунта.', '',
         '**Что здесь можно и чего нельзя.** Ниже — описательные показатели трёх естественных '
         'режимов. Это НЕ доказательство ореола и не его опровержение: сегодня печатается '
         'измерительная способность, а не вердикт.', '',
         '## Режимы', '',
         '| режим | окно | дней | что происходило |', '|---|---|---:|---|']
    for name, win, what in regimes:
        o.append(f'| {name} | {win[0]}..{win[1]} | {len(days_of(win))} | {what} |')
    for d, why in sorted(TRANSITION.items()):
        o.append(f'| TRANSITION_DAY | {d} | 1 | {why} — не входит ни в один режим |')

    # --- зрелость данных
    o += ['', '## Зрелость данных: какой день вообще можно сравнивать', '',
          'День не считается готовым, пока не прошли лаги загрузки: рекламная витрина приходит '
          'на следующее утро, постинги грузятся дважды в сутки, транзакции — периодом, '
          'перечитываемым ночью. Недогруженный день выглядит как провал продаж или как '
          'исчезнувшая реклама, и это артефакт загрузки, а не эффект. Правый край режима C '
          'ставится автоматически по последнему зрелому дню.', '',
          '| день | ' + ' | '.join(NEED_FOR_ACCOUNT) + ' | готов к сравнению |',
          '|---|' + '---|' * (len(NEED_FOR_ACCOUNT) + 1)]
    probe = [( _d(mature_through() or last_full_day()) + dt.timedelta(days=k)).isoformat()
             for k in (-2, -1, 0, 1)]
    for pd_ in probe:
        m = maturity(pd_)
        cells = ' | '.join('✓' if m[k][0] else '✗' for k in NEED_FOR_ACCOUNT)
        o.append(f'| {pd_} | {cells} | ' +
                 ('**да**' if all(m[k][0] for k in NEED_TO_COMPARE) else 'нет') + ' |')
    o.append('')
    mlast = maturity(probe[-1])
    o.append('Почему последний день не готов: ' + '; '.join(
        f'`{k}` — {mlast[k][1]}' for k in NEED_FOR_ACCOUNT if not mlast[k][0]) or 'готов.')

    # --- доза воздействия
    o += ['', '## Доза воздействия: фактическая экспозиция хвоста',
          '', 'Присутствие в снимке `ozon_bids` не доказывает получения трафика, поэтому '
          'воздействие меряется фактом показов и расхода.', '',
          '| режим | SKU хвоста с показами (в среднем за день) | показы хвоста | клики | расход, ₽ | доля в расходе аккаунта | доля в показах аккаунта |',
          '|---|---:|---:|---:|---:|---:|---:|']
    dose_all = tail_dose(REGIME_A[0], c[1] if c else REGIME_B[1])
    dose_by = {}
    for r in dose_all:
        dose_by.setdefault(r['реж'], []).append(r)
    for name, win, _w in regimes:
        rs = [r for r in dose_by.get(name, []) if r['день'] in days_of(win)]
        if not rs:
            continue
        n = len(rs)
        sv, vv = sum(r['расход_хвоста'] for r in rs), sum(r['показы_хвоста'] for r in rs)
        acc = account_daily_ads(win[0], win[1])
        asp = sum(acc.get(d, {}).get('spend', 0) for d in days_of(win))
        avw = sum(acc.get(d, {}).get('views', 0) for d in days_of(win))
        o.append(f'| {name} | {fmt(sum(r["sku_хвоста_с_показами"] for r in rs)/n)} | {fmt(float(vv))} | '
                 f'{fmt(float(sum(r["клики_хвоста"] for r in rs)))} | {fmt(sv)} | '
                 f'{fmt(100*sv/asp,1) if asp else "—"} % | {fmt(100*vv/avw,1) if avw else "—"} % |')
    for d in sorted(TRANSITION):
        r = next((x for x in dose_all if x['день'] == d), None)
        if r:
            o.append(f'| TRANSITION_DAY {d} | {r["sku_хвоста_с_показами"]} | {fmt(float(r["показы_хвоста"]))} | '
                     f'{fmt(float(r["клики_хвоста"]))} | {fmt(r["расход_хвоста"])} | '
                     f'{fmt(100*r["доля_в_расходе"],1) if r["доля_в_расходе"] else "—"} % | '
                     f'{fmt(100*r["доля_в_показах"],1) if r["доля_в_показах"] else "—"} % |')

    # --- эффективность рекламы: хвост против остальной
    o += ['', '## Эффективность рекламы по режимам: хвост и всё остальное', '',
          'CPM = 1000 × расход / показы, CPC = расход / клики, CTR = 100 × клики / показы. '
          '«Остальная реклама» — весь аккаунт минус SKU волны 1. Хвост и ядро делят общий '
          'бюджет кампаний, поэтому эти две строки читаются только вместе.', '',
          '| режим | часть | показы в день | клики в день | расход в день, ₽ | CPM, ₽ | CPC, ₽ | CTR, % |',
          '|---|---|---:|---:|---:|---:|---:|---:|']
    tail_d = daily_ads(list(_intervention_assignment()[0]), REGIME_A[0], c[1] if c else REGIME_B[1])
    rest_d = rest_dose(REGIME_A[0], c[1] if c else REGIME_B[1])
    for name, win, _w in regimes:
        dd = days_of(win)
        for part, src in (('хвост волны 1', tail_d), ('остальная реклама', rest_d)):
            v = sum(src.get(x, {}).get('views', 0) for x in dd)
            k = sum(src.get(x, {}).get('clicks', 0) for x in dd)
            sp = sum(src.get(x, {}).get('spend', 0) for x in dd)
            e = ad_efficiency(v, k, sp)
            o.append(f'| {name} | {part} | {fmt(v/len(dd))} | {fmt(k/len(dd))} | {fmt(sp/len(dd))} | '
                     f'{fmt(e["CPM"],2)} | {fmt(e["CPC"],2)} | {fmt(e["CTR"],2)} |')
    for d in sorted(TRANSITION):
        for part, src in (('хвост волны 1', tail_d), ('остальная реклама', rest_d)):
            v, k = src.get(d, {}).get('views', 0), src.get(d, {}).get('clicks', 0)
            sp = src.get(d, {}).get('spend', 0)
            e = ad_efficiency(v, k, sp)
            o.append(f'| TRANSITION {d} | {part} | {fmt(v)} | {fmt(k)} | {fmt(sp)} | '
                     f'{fmt(e["CPM"],2)} | {fmt(e["CPC"],2)} | {fmt(e["CTR"],2)} |')

    # --- когорты
    for cname, skus, bp, title in (
            ('BASE_B', core_b, bp_b,
             'BASELINE_NEVER_ADVERTISED — на заморозке не рекламировался (после 09.08 реклама появилась)'),
            ('ZERO_AD', zero_ad, bp_z,
             'PERSISTENT_ZERO_AD_CORE — нулевая своя экспозиция во ВСЕХ наблюдаемых режимах'),
            ('CORE_A', core_a, bp_a,
             'CORE_A_STABLE_ADVERTISED — своя реклама не менялась')):
        o += ['', f'## {title} ({len(skus)} SKU)', '',
              '| показатель | ' + ' | '.join(n for n, _, _ in regimes) + ' |',
              '|---|' + '---:|' * len(regimes)]
        blocks = {n: cohort_block(skus, days_of(w), bp) for n, w, _ in regimes}
        met = [('дней в режиме', 'дней', 0),
               ('постинги (заказы), шт', 'postings', 0),
               ('из них отменённые', 'cancelled', 0),
               ('завершённые заказы', 'завершённые', 0),
               ('доля отмен', 'доля_отмен', 3),
               ('штук продано', 'units', 0), ('выручка, ₽', 'gmv', 0),
               ('штук в отменённых', 'units_canc', 0),
               ('выручка отменённых, ₽', 'gmv_canc', 0),
               ('SKU с продажами', 'sku_продававшихся', 0),
               ('SKU-дней в наличии', 'sku_дней_в_наличии', 0),
               ('завершённых на 100 SKU-дней', 'завершённых_на_100_sku_дней', 2),
               ('выручка на 100 SKU-дней, ₽', 'выручка_на_100_sku_дней', 0),
               ('**завершённых в день**', 'завершённых_в_день', 2),
               ('**выручка в день, ₽**', 'выручка_в_день', 0),
               ('**штук в день**', 'штук_в_день', 2),
               ('свои рекламные показы', 'ads_views', 0), ('свои клики', 'ads_clicks', 0),
               ('свой расход, ₽', 'ads_spend', 0),
               ('**показов в день**', 'показы_в_день', 0),
               ('**расход в день, ₽**', 'расход_в_день', 0),
               ('CPM своей рекламы, ₽', 'CPM', 2),
               ('CPC своей рекламы, ₽', 'CPC', 2),
               ('CTR своей рекламы, %', 'CTR', 2),
               ('доля SKU с ценой ±>5 % к baseline', 'доля_sku_с_ценой_±>5%', 3),
               ('будних дней', 'будни', 0), ('выходных дней', 'выходные', 0)]
        for label, k, nd in met:
            o.append(f'| {label} | ' + ' | '.join(fmt(blocks[n].get(k), nd) for n, _, _ in regimes) + ' |')
        # будни/выходные раздельно
        o += ['', f'### {NAMES[cname]}: будни и выходные раздельно', '',
              '| режим | будни: завершённых в день | будни: выручка в день | выходные: завершённых в день | выходные: выручка в день |',
              '|---|---:|---:|---:|---:|']
        for n, w, _ in regimes:
            wd, we = weekday_split(skus, days_of(w), bp)
            o.append(f'| {n} | {fmt(wd.get("завершённых_в_день"),2)} | {fmt(wd.get("выручка_в_день"))} | '
                     f'{fmt(we.get("завершённых_в_день"),2)} | {fmt(we.get("выручка_в_день"))} |')
        # переходные дни
        o += ['', f'### {NAMES[cname]}: переходные дни отдельной строкой', '',
              '| день | постинги | завершённые | выручка, ₽ | свои показы |', '|---|---:|---:|---:|']
        for d in sorted(TRANSITION):
            b = cohort_block(skus, [d], bp)
            o.append(f'| {d} (TRANSITION_DAY) | {fmt(b.get("postings"))} | {fmt(b.get("завершённые"))} | '
                     f'{fmt(b.get("gmv"))} | {fmt(b.get("ads_views"))} |')
        o += sensitivity_section(cname, skus, bp, regimes)

    # --- почему у BASELINE появилась реклама
    o += ['', '## Почему у BASELINE_NEVER_ADVERTISED появилась реклама', '']
    try:
        CSH = campaign_shift()
        inb = len(CSH['new'] & set(core_b)); ina = len(CSH['new'] & set(core_a))
        o += [f'Состав кампаний менялся, и это не записано в журнал ставок. Между снимками '
              f'{CSH["prev"]} и {CSH["cur"]} из рекламы ушло {CSH["ушло"]} SKU и пришло {CSH["пришло"]} — '
              'то есть снятие хвоста было не только снятием.', '',
              '| кампания | название | новых SKU на снимке |', '|---|---|---:|']
        for ci, n in sorted(CSH['по_кампаниям'].items(), key=lambda kv: -kv[1]):
            o.append(f'| {ci} | {CSH["имена"].get(ci, "—")} | {n} |')
        pre, post = CSH['до'], CSH['после']
        o += ['', f'До {CSH["cur"]} эти карточки рекламы практически не видели: '
              f'{pre["n"]} SKU, {fmt(float(pre["v"] or 0))} показов, {fmt(pre["m"] or 0)} ₽. '
              f'После — {post["n"]} SKU, {fmt(float(post["v"] or 0))} показов, {fmt(post["m"] or 0)} ₽. '
              f'Записей в `mkt_ozon_bid_journal` на {CSH["cur"]} и раньше по ним — {CSH["в_журнале"]}: '
              f'само появление в кампаниях не задокументировано. Журнал подхватывает эти SKU '
              f'только с {CSH["журнал_потом"]["d"]} ({CSH["журнал_потом"]["s"]} SKU) и уже как '
              'изменения ставок — то есть к моменту первой записи кампании были укомплектованы.', '',
              f'В CORE_A из этих SKU — {ina}, в BASELINE — {inb}. Загрязнение **систематическое, '
              'а не случайное**: кампания «Вне РК — спрос без рекламы» по названию собрана ровно '
              'из той популяции, из которой набран BASELINE. Поэтому слой переименован '
              '(«никогда не рекламировался» — утверждение о заморозке, а не о будущем), '
              'а рядом сформирован PERSISTENT_ZERO_AD_CORE с нулевой экспозицией во всех режимах. '
              'PERSISTENT_ZERO_AD_CORE отобран по ФАКТУ экспозиции, то есть постфактум: это '
              'вторичный срез устойчивости, а не замороженный состав, и подменять им BASELINE нельзя.']
    except Exception as e:
        o.append(f'⚠ диагностика смены состава кампаний не построена: {str(e)[:200]}')

    # --- matched weekday
    o += ['', '## Matched-weekday: сопоставимые дни недели', '',
          'Сравниваются только одинаковые дни недели. Дни недели, для которых в режиме C '
          'ещё нет пары, в таблицу не попадают.', '']
    cd = days_of(c) if c else []
    if not cd:
        o.append('Режим C пуст — сравнивать не с чем.')
    else:
        o += ['| день недели | A | B | C | BASELINE: завершённых в день A / B / C | BASELINE: выручка в день A / B / C | CORE_A: завершённых в день A / B / C |',
              '|---|---|---|---|---|---|---|']
        for cday in cd:
            wd = _d(cday).weekday()
            adays = [d for d in days_of(REGIME_A) if _d(d).weekday() == wd]
            bdays = [d for d in days_of(REGIME_B) if _d(d).weekday() == wd]
            nm = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'][wd]
            bb = [cohort_block(core_b, x, bp_b) for x in (adays, bdays, [cday])]
            ba = [cohort_block(core_a, x, bp_a) for x in (adays, bdays, [cday])]
            o.append(f'| {nm} | {", ".join(adays) or "—"} | {", ".join(bdays) or "—"} | {cday} | ' +
                     ' / '.join(fmt(x.get('завершённых_в_день'), 2) for x in bb) + ' | ' +
                     ' / '.join(fmt(x.get('выручка_в_день')) for x in bb) + ' | ' +
                     ' / '.join(fmt(x.get('завершённых_в_день'), 2) for x in ba) + ' |')
        o.append('')
        o.append('Дни недели без пары в C: ' + (', '.join(
            sorted({['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'][_d(d).weekday()]
                    for d in days_of(REGIME_B)} -
                   {['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'][_d(d).weekday()] for d in cd})) or '—'))

    # --- поиск
    o += ['', '## Общие поисковые показы и позиция (НЕ органика)', '',
          'Витрина `ozon_search_product` недельная, собирается по средам; она не делит платный '
          'и бесплатный трафик. Неделя после возврата хвоста ещё не собрана.', '',
          '| когорта | неделя | режим | SKU | показы (unique_view_users) | средняя позиция | заказы | GMV поиска, ₽ |',
          '|---|---|---|---:|---:|---:|---:|---:|']
    for cname, skus in (('BASE_B', core_b), ('ZERO_AD', zero_ad), ('CORE_A', core_a)):
        for r in search_windows(skus, REGIME_A[0], c[1] if c else REGIME_B[1]):
            rg = {regime_of(x) for x in days_of((r['a'], r['b']))} - {'—'}
            lab = '+'.join(sorted(rg)) or '—'
            if len(rg) > 1:
                lab += ' (неделя накрывает границу режимов)'
            o.append(f'| {NAMES[cname]} | {r["a"]}..{r["b"]} | {lab} | {r["skus"]} | {fmt(float(r["view_users"] or 0))} | '
                     f'{fmt(r["pos"],1)} | {fmt(float(r["orders"] or 0))} | {fmt(r["gmv"] or 0)} |')

    # --- индекс относительной поисковой видимости
    o += ['', '## Индекс относительной поисковой видимости (RVI)', '',
          'Показы когорты в поиске сами по себе ничего не говорят: спрос по нашим фразам за '
          'период вырос. RVI = (показы окна / показы базы) ÷ (спрос окна / спрос базы), '
          'где спрос — `unique_search_users` по тем же запросам. RVI > 1 — видимость росла '
          'быстрее спроса, RVI < 1 — отставала. База — первое недельное окно целиком в режиме A. '
          'Ни `unique_view_users`, ни RVI не являются органикой: витрина не делит платный '
          'и бесплатный трафик.', '',
          '| когорта | окно | режимы | показы | спрос | доля показа в спросе | позиция | RVI |',
          '|---|---|---|---:|---:|---:|---:|---:|']
    for cname, skus in (('BASE_B', core_b), ('ZERO_AD', zero_ad), ('CORE_A', core_a)):
        rws, base = rvi_windows(skus, REGIME_A[0], c[1] if c else REGIME_B[1])
        for r in rws:
            mark = ' (база)' if r['база'] else ''
            o.append(f'| {NAMES[cname]} | {r["окно"]}{mark} | {"+".join(r["режимы"]) or "—"} | '
                     f'{fmt(r["показы"])} | {fmt(r["спрос"])} | '
                     f'{fmt(r["доля_показа_в_спросе"],4)} | {fmt(r["позиция"],1)} | {fmt(r["RVI"],3)} |')

    # --- аккаунт по канону BI
    o += ['', '## Результат аккаунта по канонической логике BI (дополнительный показатель)', '',
          'Источник истины по финансовому результату — BI-дашборд; здесь вызывается его же '
          'реконструкция Финансы→Баланс из `raw_ozon_transaction` без изменения методики. '
          'Цифра аккаунтная: нераспределённые расходы канон по SKU не разносит, поэтому '
          'переносить её на когорту нельзя — по когортам выше даны выручка и рекламный расход.', '',
          '| строка | ' + ' | '.join(n for n, _, _ in regimes) + ' |', '|---|' + '---:|' * len(regimes)]
    bal = {n: account_canonical(w) for n, w, _ in regimes}
    nd = {n: len(days_of(w)) for n, w, _ in regimes}
    keys = [k for k in (bal[regimes[0][0]] or {}) if k != 'ОШИБКА']
    o.append('| дней в окне | ' + ' | '.join(str(nd[n]) for n, _, _ in regimes) + ' |')
    for k in keys:
        o.append(f'| {k} | ' + ' | '.join(fmt(float(bal[n].get(k) or 0)) for n, _, _ in regimes) + ' |')
    o.append('| **продажи в день, ₽** | ' + ' | '.join(
        fmt(float(bal[n].get('sales') or 0) / nd[n]) for n, _, _ in regimes) + ' |')
    o.append('| **комиссия в день, ₽** | ' + ' | '.join(
        fmt(float(bal[n].get('commission') or 0) / nd[n]) for n, _, _ in regimes) + ' |')
    o.append('')
    o.append('⚠ Транзакции последних дней догружаются: окно C короткое, суммы по нему '
             'сравнивать с A и B в лоб нельзя — только нормированно на день.')

    # --- диагностика цен (только чтение)
    o += ['', '## Почему цена уехала: диагностика (ничего не менялось)', '',
          'Поле — `ozon_price_index.price`, наша цена в карточке. Это диагностика ковариаты, '
          'а не работа с ценой: ни одна цена в ходе исследования не трогалась.', '',
          '| когорта | день | n | медиана отклонения | доля &#124;>5 %&#124; | вверх | вниз | '
          'auto_action_enabled | marketing_price заполнен |', '|---|---|---:|---:|---:|---:|---:|---:|---:|']
    pdays = [x for x in (REGIME_B[0], REGIME_B[1], c[1] if c else None) if x]
    pd_rows = {}
    for cname, skus, bp in (('BASE_B', core_b, bp_b), ('ZERO_AD', zero_ad, bp_z), ('CORE_A', core_a, bp_a)):
        for x in pdays:
            r = price_diag(skus, bp, x)
            if not r:
                continue
            pd_rows[(cname, x)] = r
            o.append(f'| {NAMES[cname]} | {x} | {r["n"]} | {fmt(r["медиана"],4)} | {fmt(r["доля_вне_5"],3)} | '
                     f'{fmt(r["доля_вверх"],3)} | {fmt(r["доля_вниз"],3)} | {r["auto_action_enabled"]} | '
                     f'{r["marketing_price_заполнен"]} |')
    last_pd = pd_rows.get(('CORE_A', pdays[-1]))
    if last_pd:
        o += ['', f'### Распределение отклонений CORE_A на {pdays[-1]}', '',
              '| корзина | SKU |', '|---|---:|']
        for k, v in last_pd['buckets'].items():
            o.append(f'| {k} | {v} |')
    pv = posting_vs_price(core_a, REGIME_A[0], c[1] if c else REGIME_B[1])
    o += ['', '**Акции Ozon как объяснение не подтверждаются.** `auto_action_enabled` равен нулю '
          'у всех SKU обеих когорт, `marketing_price` пуст целиком — автоакции площадки в этой '
          'витрине не отражены и сдвиг не объясняют. Дрейф постепенный и преимущественно вверх, '
          'то есть это наша собственная переоценка, идущая параллельно исследованию.', '']
    if pv:
        o.append(f'Цена постинга против прайса (CORE_A, {pv["позиций"]} позиций): медиана '
                 f'{fmt(pv["медиана"],4)}, ниже прайса более чем на 5 % — {pv["ниже_прайса_на_5"]} позиций, '
                 f'выше — {pv["выше_прайса_на_5"]}. Покупатель платит меньше нашего прайса, '
                 f'и механика этой скидки в `ozon_price_index` не видна.')
    o.append('')
    o.append('⚠ `ozon_price_index` начинается 06.08, поэтому «baseline 27.07–08.08» фактически '
             'опирается на 3 дня. Это ослабляет сам порог ±5 %, а не только его интерпретацию.')

    # --- разложение ореола
    o += ['', '## Разделение эффектов: прямой, локальный ореол, глобальный ореол', '',
          '| эффект | что это | на ком меряется | чем меряется здесь | статус |',
          '|---|---|---|---|---|',
          '| **прямой эффект хвоста** | продажи самих SKU, которым вернули или сняли рекламу | '
          'SKU волны 1 и группы E8 | рандомизированный A/B E8 по ITT | единственный причинный '
          'контраст в контуре; меряет хвост, не ореол |',
          '| **локальный ореол** | переток внутри товарного соседства: хвост тянет карточки, '
          'с которыми делит поисковую выдачу и модель принтера | receivers внутри кластера, '
          'сами не в E8 и без назначенной своей рекламы | CLUSTER_SPILLOVER: доля случайно '
          'возвращённых E8-A в кластере | дизайн готов, оценка запрещена до накопления данных |',
          '| **глобальный ореол аккаунта** | подъём аккаунта целиком: рейтинг продавца, общая '
          'видимость, поведение алгоритма | замороженные когорты BASELINE / PERSISTENT_ZERO_AD / CORE_A | '
          'прерванный временной ряд A/B/C + RVI | описательно; контрольной группы нет, '
          'причинный вывод запрещён контрактом |',
          '',
          'Разделение существенно: прямой эффект и локальный ореол конкурируют за одну и ту же '
          'выдачу, поэтому положительный локальный ореол одного кластера может быть '
          'каннибализацией соседнего. Глобальный ореол этого дефекта не имеет, но и '
          'отделить его от сезонности без контрольной группы нечем.']

    # --- дизайн CLUSTER_SPILLOVER
    o += ['', '## CLUSTER_SPILLOVER: дизайн, баланс и мощность (оценка НЕ проводится)', '']
    try:
        CS = cmd_clusters()
        o += [f'Кластеры формируются только по данным до {PRE_RESTORE} — раньше возврата 19.08, '
              'поэтому состав кластера не может зависеть от того, кого вернули. Признак общности — '
              f'различающий поисковый запрос (не шире {MAX_QUERY_SKU} SKU: головной «принтер» '
              'охватывает 3 906 карточек и кластером не является), контроль — модель принтера '
              'и структура набора из названия. Экзогенная вариация — доля случайно возвращённых '
              'E8-A внутри кластера.', '',
              f'Кластеров всего {len(CS["clusters"])}, пригодных (≥{MIN_CLUSTER_E8} SKU из E8 '
              f'и хотя бы один receiver) — {CS["diag"]["кластеров"]}; в них '
              f'{sum(r["E8_всего"] for r in CS["rows"])} SKU из E8 и '
              f'{sum(r["receivers"] for r in CS["rows"])} receivers.', '',
              '### Баланс кластеров', '',
              '| ковариата до возврата | доля E8-A выше медианы | ниже медианы | станд. разность | дисбаланс |',
              '|---|---:|---:|---:|---|']
        for r in CS['balance_tab']:
            o.append(f'| {r["ковариата"]} | {fmt(r["доля_A_выше_медианы"],3)} | {fmt(r["доля_A_ниже"],3)} | '
                     f'{fmt(r["станд_разность"],3)} | {r["дисбаланс"]} |')
        b = CS['balance']
        o += ['', f'Медиана доли E8-A — {b["медиана_доли_A"]}; кластеров выше {b["кластеров_высоких"]}, '
              f'ниже {b["кластеров_низких"]}, ничьих отброшено {b["ничьих_отброшено"]} '
              '(на доле ровно 0,5 сидит масса кластеров, и отнесение их в одну сторону само '
              f'создаёт видимость дисбаланса). Максимальная |d| = {fmt(b["макс_|d|"],3)}, '
              f'баланс **{b["баланс"]}**.', '',
              '### Мощность', '',
              '| рамка | кластеров | ₽ на receiver в день | sd между кластерами | MDE, ₽ | MDE в долях среднего |',
              '|---|---:|---:|---:|---:|---:|']
        for lab, pw in (('все пригодные', CS['power_all']), ('только с ненулевой выручкой', CS['power_live'])):
            o.append(f'| {lab} | {pw.get("кластеров")} | {fmt(pw.get("среднее_₽_на_receiver_в_день"),2)} | '
                     f'{fmt(pw.get("sd_между_кластерами"),2)} | {fmt(pw.get("MDE_₽_на_receiver_в_день"),2)} | '
                     f'{fmt(pw.get("MDE_доля"),3)} |')
        o += ['', f'**Мощность недостаточна.** У {CS["diag"]["с_ненулевой_выручкой_receivers"]} кластеров '
              f'из {CS["diag"]["кластеров"]} выручка receivers в базисе ненулевая — '
              f'{fmt(CS["diag"]["доля_вырожденных"],3)} кластеров вырождены. Минимально '
              'обнаружимый эффект превышает само среднее, то есть дизайн способен поймать только '
              'нереалистично большой ореол. Причинный вывод по CLUSTER_SPILLOVER запрещён; до '
              f'накопления {MIN_POST_DAYS} дней публикуются только контракт, баланс и эта проверка.',
              '', f'Состав кластеров заморожен в `{CLUSTER_CSV}`.']
    except Exception as e:
        o.append(f'⚠ дизайн кластеров не построен: {str(e)[:200]}')

    # --- что доказуемо
    o += ['', '## Что доказуемо, что квазиэксперимент, а что корреляция', '',
          '**Причинно доказуемо сегодня — ничего по ореолу.** Единственный рандомизированный '
          'контраст в контуре — внутренний A/B E8 (возврат половины хвоста), и он меряет '
          'возврат самого хвоста, а не ореол: treatment и control сидят в одном аккаунте '
          'и оба могут двигать продажи третьих SKU.', '',
          '**Квазиэксперимент.** Сравнение режимов A/B/C на замороженных когортах — '
          'прерванный временной ряд без контрольной группы. Он станет читаемым, когда в C '
          f'наберётся полная неделя (сейчас {post_days} дн.), и всё равно останется '
          'уязвимым к сезонности и к параллельным вмешательствам 17–19.08.', '',
          '**Корреляция и только.** Любая связь «показы хвоста ↔ продажи ядра» на трёх точках: '
          'три уровня дозы не дают наклона, а спрос по нашим фразам за то же время вырос '
          'на 31 % (п. 15 брифа).', '',
          '## Известные загрязнения', '']
    for x in ('19.08 — переходный день: возврат шёл внутри дня; вынесен в TRANSITION_DAY.',
              '09.08 — переходный день снятия; тоже вне режимов.',
              '18.08 в режим B входит, но в этот день прошли откат E4 и подъём ядра E5 — '
              'режим B к концу окна уже не «чистое снятие».',
              'Рекламная витрина начинается 27.07: режим A наблюдается 13 дней, не «всегда».',
              'Неделя поиска после возврата будет собрана только 26.08 — по режиму C видимости нет.',
              'Общий бюджет кампаний: ореол и переток бюджета между хвостом и ядром '
              'наблюдательно неразличимы.',
              'BASELINE мал по построению — редкий товар, который продаётся без рекламы; '
              'дневные числа шумные.',
              '«Никогда не рекламировался» проверяется только с 27.07 — раньше витрины нет.',
              '08.08 запущены кампании 35269704 «Вне РК — спрос без рекламы» и 35269713 '
              '«Бандлы x2/x6 — старт»; в журнале ставок их появления нет, а первая собрана '
              'из популяции BASELINE — загрязнение систематическое.',
              'Витрина `ozon_price_index` начинается 06.08: ценовой baseline опирается на 3 дня, '
              'а не на всё окно 27.07–08.08.',
              'Цена ядра дрейфует вверх параллельно исследованию (CORE_A: медиана +2,8 % к 20.08, '
              'у 34,3 % SKU сдвиг более 5 %) — это наша переоценка, не автоакции Ozon.',
              'Цена постинга ниже прайса: покупатель платит меньше карточки, механика скидки '
              'в `ozon_price_index` не видна, поэтому ценовой признак неполон.'):
        o.append(f'- {x}')

    path = f'{REPORTS}/ozon_halo_{day}.md'
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(o) + '\n')
    print('\n'.join(o[:12]))
    print(f'\nотчёт: {path}')
    return path


def cmd_dose(d0, d1):
    rows = tail_dose(d0, d1)
    print('| день | режим | SKU хвоста с показами | показы | клики | расход, ₽ | доля расхода | доля показов |')
    print('|---|---|---:|---:|---:|---:|---:|---:|')
    for r in rows:
        print(f'| {r["день"]} | {r["реж"]} | {r["sku_хвоста_с_показами"]} | {fmt(float(r["показы_хвоста"]))} | '
              f'{fmt(float(r["клики_хвоста"]))} | {fmt(r["расход_хвоста"])} | '
              f'{fmt(100*r["доля_в_расходе"],1) if r["доля_в_расходе"] else "—"} % | '
              f'{fmt(100*r["доля_в_показах"],1) if r["доля_в_показах"] else "—"} % |')


def main():
    ap = argparse.ArgumentParser(description='P1-HALO: ореол снятого хвоста — только чтение')
    ap.add_argument('cmd', choices=['build-cohorts', 'dose', 'eval', 'funnel', 'clusters'])
    ap.add_argument('--force', action='store_true', help='build-cohorts: пересобрать замороженный состав')
    ap.add_argument('--from', dest='d0', default=REGIME_A[0])
    ap.add_argument('--to', dest='d1', default=None)
    ap.add_argument('--as-of', default=None)
    a = ap.parse_args()
    if a.cmd == 'build-cohorts':
        build_cohorts(a.force)
    elif a.cmd == 'dose':
        cmd_dose(a.d0, a.d1 or last_full_day())
    elif a.cmd == 'clusters':
        R = cmd_clusters()
        print('диагностика:', R['diag'])
        print('баланс:', R['balance'])
        print('мощность (все):', R['power_all'])
        print('мощность (ненулевые):', R['power_live'])
        print('состав:', CLUSTER_CSV)
    elif a.cmd == 'funnel':
        with open(FUNNEL_CSV, encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
        tail, jrn = _intervention_assignment()
        print(funnel_text(rows, tail, jrn))
    else:
        cmd_eval(a.as_of)




# ===================== eligible_daily и sensitivity ===================================
def panel(skus, days):
    """Панель SKU × день: остаток, цена, своя реклама, продажи. Признаки, а не фильтр состава.

    Всё, что происходит после 09.08, попадает сюда как ДНЕВНОЙ признак: SKU из
    замороженного состава не удаляется задним числом, но день, в который он был без
    остатка / с уехавшей ценой / со своей рекламой, можно исключить в разрезе
    `eligible_daily`. Основной разрез — ITT-подобный, по всему замороженному составу.
    """
    d0, d1 = days[0], days[-1]
    P = {}
    for r in db.query(
            """WITH sk AS (SELECT op.sku::text sku, op.offer_id FROM ozon_product op
                           WHERE op.account=%s AND op.sku::text = ANY(%s)),
                    snap AS (SELECT captured_at::date d, max(captured_at) c FROM supplier_stock
                             WHERE captured_at::date BETWEEN %s AND %s GROUP BY 1),
                    st AS (SELECT snap.d, s.external_code, sum(s.stock) q FROM supplier_stock s
                           JOIN snap ON s.captured_at=snap.c GROUP BY 1,2)
               SELECT sk.sku, st.d::text d, st.q::float q FROM st JOIN sk ON sk.offer_id=st.external_code""",
            (ACC, skus, d0, d1)):
        P.setdefault((r['sku'], r['d']), {})['stock'] = r['q']
    for r in db.query(
            """WITH sk AS (SELECT op.sku::text sku, op.offer_id FROM ozon_product op
                           WHERE op.account=%s AND op.sku::text = ANY(%s))
               SELECT sk.sku, pi.collected_on::text d, avg(pi.price)::float pr
               FROM ozon_price_index pi JOIN sk ON sk.offer_id=pi.offer_id
               WHERE pi.account=%s AND pi.collected_on BETWEEN %s AND %s GROUP BY 1,2""",
            (ACC, skus, ACC, d0, d1)):
        P.setdefault((r['sku'], r['d']), {})['price'] = r['pr']
    for r in db.query(
            """SELECT sku::text sku, stat_date::text d, sum(views) v, sum(money_spent) sp
               FROM mkt_ozon_ads_sku_daily WHERE account=%s AND sku::text = ANY(%s)
                 AND stat_date BETWEEN %s AND %s GROUP BY 1,2""", (ACC, skus, d0, d1)):
        P.setdefault((r['sku'], r['d']), {})['own_views'] = float(r['v'] or 0)
        P[(r['sku'], r['d'])]['own_spend'] = float(r['sp'] or 0)
    # Номера постингов, а не их счётчик: один постинг может содержать два SKU когорты,
    # и при суммировании по SKU он иначе засчитался бы дважды (расхождение 72/76 и 39/44).
    for r in db.query(
            """SELECT (p->>'sku')::text sku, r.in_process_at::date::text d, r.posting_number pn,
                      (r.status='cancelled') canc,
                      sum((p->>'quantity')::numeric)::float units,
                      sum((p->>'price')::numeric*(p->>'quantity')::numeric)::float gmv
               FROM raw_ozon_posting r, jsonb_array_elements(r.payload->'products') p
               WHERE r.account=%s AND (p->>'sku')::text = ANY(%s)
                 AND r.in_process_at::date BETWEEN %s AND %s GROUP BY 1,2,3,4""", (ACC, skus, d0, d1)):
        c = P.setdefault((r['sku'], r['d']), {})
        c.setdefault('posts', []).append((r['pn'], bool(r['canc'])))
        if not r['canc']:
            c['units'] = (c.get('units') or 0) + (r['units'] or 0)
            c['gmv'] = (c.get('gmv') or 0) + (r['gmv'] or 0)
    return P


def _eligible(cell, base, core):
    """День SKU пригоден: есть остаток, цена в ±5 % от baseline, и — для CORE_B — своей рекламы нет."""
    if (cell.get('stock') or 0) <= 0:
        return False
    pr = cell.get('price')
    if base and pr and abs(pr - base) / base > PRICE_TOL:
        return False
    if core == 'BASE_B' and (cell.get('own_views') or 0) > 0:
        return False
    return True


def variant_stats(skus, days, bp, core, P, variant):
    """variant: 'itt' — весь замороженный состав; 'eligible' — только пригодные SKU-дни;
    'clean' — состав без SKU, загрязнённых после 09.08 (своя реклама или сдвиг цены)."""
    keep = set(skus)
    if variant == 'clean':
        bad = set()
        for (s, d), c in P.items():
            if _d(d) <= _d(FREEZE_DAY):
                continue
            if (c.get('own_views') or 0) > 0 and core == 'BASE_B':
                bad.add(s)
            b = bp.get(s)
            if b and c.get('price') and abs(c['price'] - b) / b > PRICE_TOL:
                bad.add(s)
        keep = keep - bad
    n_days, units, gmv, sold = 0, 0.0, 0.0, set()
    all_posts, canc_posts = set(), set()
    for s in keep:
        for d in days:
            c = P.get((s, d), {})
            ok = _eligible(c, bp.get(s), core) if variant in ('eligible',) else (c.get('stock') or 0) > 0
            if not ok:
                continue
            n_days += 1
            units += c.get('units') or 0
            gmv += c.get('gmv') or 0
            for pn, canc in c.get('posts', []):
                all_posts.add(pn)
                if canc:
                    canc_posts.add(pn)
                else:
                    sold.add(s)
    done = len(all_posts) - len(canc_posts)
    return {'sku': len(keep), 'sku_дней': n_days, 'постинги': len(all_posts),
            'отменённые': len(canc_posts), 'завершённые': done,
            'штуки': units, 'выручка': gmv, 'sku_с_продажами': len(sold),
            'завершённых_на_100': 100 * done / n_days if n_days else None,
            'выручка_на_100': 100 * gmv / n_days if n_days else None}


def sensitivity_section(cname, skus, bp, regimes):
    all_days = sorted({d for _n, w, _t in regimes for d in days_of(w)})
    P = panel(skus, all_days)
    o = ['', f'### {NAMES[cname]}: eligible_daily и sensitivity', '',
         'Основной разрез — ITT-подобный: замороженный состав целиком, исключаются только '
         'дни без остатка. `eligible_daily` дополнительно требует цену в ±5 % от baseline'
         + (' и отсутствие собственной рекламы в этот день.' if cname == 'BASE_B' else '.') +
         ' `clean` — состав без SKU, у которых после 09.08 появилась своя реклама или уехала цена; '
         'это удаление задним числом, поэтому оно показано только как проверка устойчивости.', '',
         '| разрез | режим | SKU | SKU-дней | постинги | отменённые | завершённые | штук | выручка, ₽ | завершённых на 100 SKU-дней | выручка на 100 SKU-дней |',
         '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for variant, label in (('itt', 'ITT (весь состав)'), ('eligible', 'eligible_daily'), ('clean', 'clean (sensitivity)')):
        for n, w, _t in regimes:
            v = variant_stats(skus, days_of(w), bp, cname, P, variant)
            o.append(f'| {label} | {n} | {v["sku"]} | {fmt(float(v["sku_дней"]))} | {fmt(float(v["постинги"]))} | '
                     f'{fmt(float(v["отменённые"]))} | {fmt(float(v["завершённые"]))} | {fmt(v["штуки"])} | '
                     f'{fmt(v["выручка"])} | {fmt(v["завершённых_на_100"],2)} | {fmt(v["выручка_на_100"])} |')
    return o

if __name__ == '__main__':
    main()
