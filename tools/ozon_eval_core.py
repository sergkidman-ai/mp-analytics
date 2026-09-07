#!/usr/bin/env python3
# поток: mkt
"""H-009 — ядро автоматической оценки экспериментов Ozon (теневой режим).

Замыкает разрыв MEASURE -> EVALUATE -> KEEP/ROLLBACK/INCONCLUSIVE -> MEMORY, который до
21.08.2026 закрывался руками: человек сам ловил дату сверки, сам решал, дозрели ли витрины,
сам выбирал оценщик, сам держал в голове загрязнения и сам записывал вывод.

Модуль СЧИТАЕТ и ЗАПИСЫВАЕТ ВЫВОД. Он ничего не применяет: кампании, цены, бюджеты, cron
и внешние API остаются нетронутыми. KEEP и ROLLBACK здесь — рекомендации, а не действия.

Четыре сущности разведены и никогда не склеиваются:
    measurement    — что произошло с метриками (факт из витрин);
    verdict        — доказан эффект или нет (метод, а не бизнес);
    recommendation — что предлагается сделать (бизнес, но не решение);
    decision       — что разрешил человек (в этой версии всегда null);
    execution      — что фактически принял API (в этой версии всегда null).

Обращения к БД — только чтение, и только через класс ЖивыеДанные. Логика вердикта от БД
не зависит вовсе: тесты подставляют свой источник данных и работают без Postgres.
"""
import csv
import datetime as dt
import hashlib
import json
import math
import os
import sys

sys.path.insert(0, '/opt/mp-analytics')

БАЗА = '/opt/mp-analytics'
КОНТРАКТЫ = БАЗА + '/docs/experiments/ozon_evaluator_contracts.json'
ОЦЕНКИ = БАЗА + '/docs/experiments/ozon_evaluations.jsonl'
КОГОРТЫ = БАЗА + '/docs/experiments/cohorts'

EVALUATOR_VERSION = 'EVAL_V1_0'
РЕЖИМ = 'SHADOW_MODE_V0_1'
TODAY = (os.environ.get('OZON_EVAL_TODAY')
         or dt.datetime.now(dt.timezone.utc).date().isoformat())
# дата прогона: реальная. Прибитая константа держала evaluate-ready в BEFORE_CHECK_DATE
# и штамповала evaluated_at прошлым числом даже при явном --today.
ACC = 'oz_acc1'

Z_A, Z_B = 1.96, 0.84          # 95 % значимость и 80 % мощность — как в ozon_hypo.мощность()

# --- типы контрактов оценки ----------------------------------------------------------
# Один общий контракт с подключаемым типом; отдельного жёстко прошитого сценария на каждую
# гипотезу нет — иначе каждая новая гипотеза требовала бы нового кода.
ТИПЫ = (
    'TREATMENT_CONTROL_DID',      # две группы, разность разностей — причинный вывод разрешён
    'ITT',                        # назначенные, независимо от применения (основной разрез)
    'PER_PROTOCOL',               # только фактически изменённые (вторичный разрез)
    'BEFORE_AFTER_NO_CAUSAL',     # своя история, контроля нет — причинность запрещена
    'INTERRUPTED_TIME_SERIES',    # ряд до/после с трендом, контроля нет
    'GUARDRAIL_ONLY',             # только сторож (G6), основного показателя нет
    'CAPABILITY_GAP',             # способность системы, деньгами не измеряется
    'VOID_NEVER_APPLIED',         # воздействия не было — оценивать нечего и нельзя
    'POWER_BLOCKED',              # дизайн бессилен, эксперимент не запускался
)

ВЕРДИКТЫ = (
    'CONTINUE_MEASURING', 'INCONCLUSIVE', 'NO_CAUSAL_CLAIM', 'CONTAMINATED',
    'EFFECT_CONFIRMED_POSITIVE', 'EFFECT_CONFIRMED_NEGATIVE',
    'VOID', 'BLOCKED_BY_POWER', 'CAPABILITY_PRESENT', 'CAPABILITY_ABSENT',
)

РЕКОМЕНДАЦИИ = ('KEEP', 'ROLLBACK', 'CONTINUE_MEASURING', 'INCONCLUSIVE',
                'CONTAMINATED', 'NO_CAUSAL_CLAIM')

ПРИЧИНЫ = {
    'BEFORE_CHECK_DATE': 'дата сверки ещё не наступила',
    'DATA_NOT_MATURE': 'источник за нужный день не догрузился',
    'WINDOW_TOO_SHORT': 'окно измерения короче минимальной длительности',
    'BASELINE_UNAVAILABLE': 'витрина не покрывает базисное окно — сравнивать не с чем',
    'TOO_FEW_EVENTS': 'событий меньше минимального числа',
    'UNDERPOWERED': 'наблюдаемый эффект меньше MDE — отличить от нуля нельзя',
    'NO_CONTROL_BY_DESIGN': 'контрольной группы нет по дизайну',
    'MISSING_FROZEN_CONTROL': 'состав контроля не заморожен до вмешательства — '
                             'причинное сравнение опирается на изменяемый источник',
    'COHORT_DRIFT': 'состав когорты разошёлся с зафиксированным снимком',
    'COHORT_OVERLAP': 'когорта пересекается с другим экспериментом',
    'CONTAMINATION_DECLARED': 'загрязнение объявлено в контракте до вмешательства',
    'GUARDRAIL_BREACH': 'сторож пробит',
    'GUARDRAIL_UNAVAILABLE': 'сторож не проверяется машинно на этих данных',
    'EFFECT_POSITIVE': 'основной показатель вырос',
    'EFFECT_NEGATIVE': 'основной показатель упал',
    'EXPERIMENT_VOID': 'эксперимент аннулирован',
    'NEVER_APPLIED': 'воздействие не доходило ни до одной строки',
    'DO_NOT_REPEAT': 'зафиксированное знание: этот рычаг в этом виде не повторять',
    'CAUSAL_CLAIM_FORBIDDEN': 'контракт запрещает причинное утверждение',
    'POWER_BLOCKED_DESIGN': 'дизайн не набирает мощность, эксперимент не запускался',
    'CAPABILITY_SELF_CHECK': 'проверка наличия способности системы, не денежная метрика',
    'MEMORY_ONLY': 'вывод пишется в память, действие не предлагается',
}


# ===== служебное ======================================================================
def _д(x):
    return x if isinstance(x, dt.date) else dt.date.fromisoformat(str(x)[:10])


def _дней(a, b):
    return (_д(b) - _д(a)).days + 1


def _канон(o):
    return json.dumps(o, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _sha(s, n=16):
    return hashlib.sha256(s.encode('utf-8')).hexdigest()[:n]


def версия_контракта(c):
    """Версия = отпечаток самого контракта: правка контракта даёт новую версию оценки."""
    тело = {k: v for k, v in c.items() if not k.startswith('_')}
    return _sha(_канон(тело), 12)


def хеш_когорты(пары):
    """Отпечаток состава: смена хотя бы одного SKU меняет хеш и ловится как дрейф."""
    if not пары:
        return _sha('пусто', 16)
    return _sha('\n'.join(sorted(f'{a}:{s}' for a, s in пары)), 16)


def _отпечаток_зрелости(гот):
    return _sha(_канон({k: bool(v[0]) for k, v in sorted((гот or {}).items())}), 8)


def читать_контракты(путь=None):
    with open(путь or КОНТРАКТЫ, encoding='utf-8') as f:
        return json.load(f)['контракты']


def контракт_по_id(cid, путь=None):
    for c in читать_контракты(путь):
        if c['id'].upper() == str(cid).upper():
            return c
    raise KeyError(f'нет контракта {cid}')


# ===== статистика =====================================================================
def _норм(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def уэлч(a, b):
    """Сравнение средних при неравных дисперсиях (Уэлч) плюс MDE при мощности 80 %.

    Возвращает и p, и MDE: без MDE нулевой результат неотличим от «эффекта не хватило
    мощности», а это разные вещи — вторая не даёт права сказать «эффекта нет».
    """
    na, nb = len(a), len(b)
    out = {'n_treatment': na, 'n_control': nb, 'mean_treatment': None, 'mean_control': None,
           'diff': None, 'se': None, 't': None, 'p': None, 'mde_80': None, 'достаточно': False}
    if na < 2 or nb < 2:
        return out
    ma, mb = sum(a) / na, sum(b) / nb
    va = sum((x - ma) ** 2 for x in a) / (na - 1)
    vb = sum((x - mb) ** 2 for x in b) / (nb - 1)
    se = math.sqrt(va / na + vb / nb)
    out.update(mean_treatment=ma, mean_control=mb, diff=ma - mb, se=se)
    if se <= 0:
        return out
    t = (ma - mb) / se
    out.update(t=t, p=2 * (1 - _норм(abs(t))), mde_80=(Z_A + Z_B) * se)
    out['достаточно'] = abs(ma - mb) >= out['mde_80']
    return out


# ===== источник данных ================================================================
class ЖивыеДанные:
    """Единственное место, где модуль ходит в Postgres. Только выборки."""

    ИСТОЧНИКИ = ('ads', 'posting', 'price', 'stock', 'transaction', 'search', 'bids')

    def __init__(self, acc=ACC):
        from core import db
        self.db = db
        self.acc = acc

    # --- зрелость ---------------------------------------------------------------
    def зрелость(self, день, источники):
        """Готов ли день к оценке по каждому нужному источнику.

        Календарная дата сверки сама по себе ничего не значит: недогруженный день
        выглядит как провал продаж или как исчезнувшая реклама — это артефакт загрузки.
        Сутки D считаются закрытыми, если загрузчик отработал уже после полуночи D+1 UTC.
        """
        d = _д(день)
        закрытие = dt.datetime.combine(d + dt.timedelta(days=1), dt.time(0),
                                       tzinfo=dt.timezone.utc)
        out = {}
        q = self.db.query
        for src in источники:
            if src == 'ads':
                x = q("SELECT max(stat_date)::text x FROM mkt_ozon_ads_sku_daily "
                      "WHERE account=%s", (self.acc,))[0]['x']
                out[src] = (bool(x) and _д(x) >= d, f'витрина доехала до {x}')
            elif src == 'posting':
                x = q("SELECT max(loaded_at) x FROM raw_ozon_posting WHERE account=%s",
                      (self.acc,))[0]['x']
                out[src] = (bool(x) and x >= закрытие,
                            f'последняя загрузка {x:%Y-%m-%d %H:%M} UTC' if x else 'загрузок нет')
            elif src == 'price':
                n = q("SELECT count(*) n FROM ozon_price_index WHERE account=%s "
                      "AND collected_on=%s", (self.acc, день))[0]['n']
                out[src] = (n > 0, f'строк за день: {n}')
            elif src == 'stock':
                n = q("SELECT count(*) n FROM supplier_stock WHERE captured_at::date=%s",
                      (день,))[0]['n']
                out[src] = (n > 0, f'строк за день: {n}')
            elif src == 'transaction':
                x = q("""SELECT max(loaded_at) x FROM raw_ozon_transaction
                         WHERE account=%s AND period_from::date<=%s AND period_to::date>=%s""",
                      (self.acc, день, день))[0]['x']
                out[src] = (bool(x) and x >= закрытие,
                            f'период загружен {x:%Y-%m-%d %H:%M} UTC' if x else 'периода нет')
            elif src == 'search':
                x = q("SELECT max(period_end)::text x FROM ozon_search_product "
                      "WHERE account=%s", (self.acc,))[0]['x']
                out[src] = (bool(x) and _д(x) >= d, f'поисковая неделя закрыта по {x}')
            elif src == 'bids':
                x = q("SELECT max(captured_at)::text x FROM ozon_bids WHERE account=%s",
                      (self.acc,))[0]['x']
                out[src] = (bool(x) and _д(x) >= d, f'снимок состава кампаний от {x}')
            else:
                out[src] = (False, 'неизвестный источник')
        return out

    def последний_день_витрины(self):
        return self.db.query("SELECT max(stat_date)::text x FROM mkt_ozon_ads_sku_daily "
                             "WHERE account=%s", (self.acc,))[0]['x']

    # --- когорты ----------------------------------------------------------------
    def когорта(self, spec, режим='itt'):
        acc = spec.get('account', self.acc)
        acts = spec.get('actions') or []
        if not acts:
            return set()
        sql = ["SELECT account, sku::text sku, applied FROM mkt_ozon_bid_journal",
               "WHERE account=%s AND action = ANY(%s)"]
        p = [acc, acts]
        if spec.get('week_start'):
            sql.append("AND week_start=%s")
            p.append(spec['week_start'])
        rows = self.db.query(' '.join(sql), tuple(p))
        if режим == 'pp':
            rows = [r for r in rows if r['applied']]
        return {(r['account'], r['sku']) for r in rows}

    # --- измерение ---------------------------------------------------------------
    def чистая_по_sku(self, acc, skus, d0, d1):
        """Выручка минус расход на SKU в сутки. Отсутствие строк = ноль, а не пропуск."""
        if not skus:
            return {}
        дней = max(1, _дней(d0, d1))
        rows = self.db.query(
            """SELECT sku::text sku, sum(orders_money) rev, sum(money_spent) sp
               FROM mkt_ozon_ads_sku_daily
               WHERE account=%s AND sku::text = ANY(%s) AND stat_date BETWEEN %s AND %s
               GROUP BY 1""", (acc, list(skus), d0, d1))
        v = {r['sku']: (float(r['rev'] or 0) - float(r['sp'] or 0)) / дней for r in rows}
        return {s: v.get(s, 0.0) for s in skus}

    def свод(self, acc, skus, d0, d1):
        if not skus:
            return {}
        r = self.db.query(
            """SELECT count(DISTINCT sku) sku_с_данными, sum(views) views, sum(clicks) clicks,
                      sum(money_spent) spend, sum(orders_qty) orders, sum(orders_money) revenue
               FROM mkt_ozon_ads_sku_daily
               WHERE account=%s AND sku::text = ANY(%s) AND stat_date BETWEEN %s AND %s""",
            (acc, list(skus), d0, d1))[0]
        o = {k: float(v or 0) for k, v in r.items()}
        n = len(set(skus)) or 1
        o['sku_в_группе'] = len(set(skus))
        o['дней'] = _дней(d0, d1)
        o['выручка_минус_расход'] = o['revenue'] - o['spend']
        for k in ('views', 'clicks', 'spend', 'orders', 'revenue', 'выручка_минус_расход'):
            o[k + '_на_sku'] = o[k] / n
        o['drr'] = (o['spend'] / o['revenue']) if o['revenue'] else None
        return o

    def средняя_цена(self, acc, skus, день):
        if not skus:
            return None
        r = self.db.query(
            """WITH d AS (SELECT max(collected_on) c FROM ozon_price_index
                          WHERE account=%s AND collected_on <= %s),
                    p AS (SELECT offer_id, avg(price) price FROM ozon_price_index, d
                          WHERE account=%s AND collected_on = d.c GROUP BY 1)
               SELECT avg(p.price)::float x FROM ozon_product op JOIN p USING(offer_id)
               WHERE op.account=%s AND op.sku::text = ANY(%s)""",
            (acc, день, acc, acc, list(skus)))[0]['x']
        return float(r) if r is not None else None

    def доля_с_остатком(self, acc, skus, день):
        if not skus:
            return None
        r = self.db.query(
            """WITH snap AS (SELECT max(captured_at) c FROM supplier_stock
                             WHERE captured_at::date <= %s),
                    st AS (SELECT external_code, sum(stock) s FROM supplier_stock, snap
                           WHERE captured_at = snap.c GROUP BY 1)
               SELECT count(DISTINCT op.sku) n,
                      count(DISTINCT op.sku) FILTER (WHERE st.external_code IS NOT NULL) есть,
                      count(DISTINCT op.sku) FILTER (WHERE st.s > 0) pos
               FROM ozon_product op LEFT JOIN st ON st.external_code = op.offer_id
               WHERE op.account=%s AND op.sku::text = ANY(%s)""",
            (день, acc, list(skus)))[0]
        if not r['n'] or r['есть'] / r['n'] < 0.8:
            # мало какие SKU вообще находятся в прайсе поставщика: доля мерила бы качество
            # стыковки external_code, а не наличие товара — честнее сказать «не проверяется»
            return None
        return r['pos'] / r['n']

    def поисковые_показы(self, acc, skus, d0, d1):
        if not skus:
            return None
        r = self.db.query(
            """SELECT sum(unique_view_users) x FROM ozon_search_product
               WHERE account=%s AND sku::text = ANY(%s)
                 AND period_end >= %s AND period_start <= %s""",
            (acc, list(skus), d0, d1))[0]['x']
        return float(r) if r is not None else None


# ===== снимки когорт ==================================================================
def снимок_когорты(имя, каталог=None):
    """Состав, зафиксированный ДО вмешательства. Источник правды о неизменности когорты."""
    путь = os.path.join(каталог or КОГОРТЫ, имя)
    if not os.path.exists(путь):
        return None
    out = set()
    with open(путь, encoding='utf-8') as f:
        for row in csv.DictReader(f):
            acc = row.get('account') or ACC
            sku = row.get('sku')
            if sku:
                out.add((acc, str(sku)))
    return out


# ===== загрязнения ====================================================================
def пересечения(c, когорты_всех):
    """Пересечения когорты с когортами других экспериментов — по факту, не по декларации."""
    свои = когорты_всех.get(c['id'], set())
    out = []
    for cid, чужие in sorted(когорты_всех.items()):
        if cid == c['id'] or not свои or not чужие:
            continue
        общ = свои & чужие
        if общ:
            out.append({'с': cid, 'sku': len(общ), 'доля_своей': round(len(общ) / len(свои), 4)})
    return out


def загрязнения(c, дрейф, пересеч):
    out = []
    for т in c.get('известные_загрязнения', []):
        out.append({'код': 'CONTAMINATION_DECLARED', 'вес': 'известное', 'текст': т})
    for p in пересеч:
        вес = 'блокирующее' if p['доля_своей'] >= 0.5 else 'понижающее'
        out.append({'код': 'COHORT_OVERLAP', 'вес': вес,
                    'текст': (f"пересечение с {p['с']}: {p['sku']} SKU "
                              f"({p['доля_своей']:.0%} своей когорты)")})
    if дрейф:
        out.append({'код': 'COHORT_DRIFT', 'вес': 'блокирующее', 'текст': дрейф})
    return out


# ===== сторожа ========================================================================
def _сторож(код, значение, порог, направление):
    if значение is None:
        return {'код': код, 'значение': None, 'порог': порог, 'статус': 'UNAVAILABLE',
                'текст': 'данных для машинной проверки нет'}
    пробит = (значение > порог) if направление == 'больше' else (значение < порог)
    return {'код': код, 'значение': round(float(значение), 4), 'порог': порог,
            'статус': 'BREACH' if пробит else 'PASS', 'текст': ''}


def сторожа(c, data, acc, skus, base, окно):
    """Машинно проверяемые сторожа. Свободнотекстовый сторож честно помечается UNAVAILABLE."""
    out = []
    for g in c.get('guardrails_машинные', []):
        код, порог = g['код'], g['порог']
        if код == 'SPEND_X2':
            # окна разной длины сравнивать в лоб нельзя: приводим к расходу на SKU в сутки
            b = data.свод(acc, skus, *base) if base else {}
            w = data.свод(acc, skus, *окно)
            b_д = (b.get('spend_на_sku') or 0) / max(1, _дней(*base)) if base else 0
            w_д = (w.get('spend_на_sku') or 0) / max(1, _дней(*окно))
            out.append(_сторож(код, (w_д / b_д) if b_д else None, порог, 'больше'))
        elif код == 'PRICE_SHIFT_5':
            p0 = data.средняя_цена(acc, skus, base[0]) if base else None
            p1 = data.средняя_цена(acc, skus, окно[1])
            v = abs(p1 / p0 - 1) if (p0 and p1) else None
            out.append(_сторож(код, v, порог, 'больше'))
        elif код == 'STOCK_SHARE_MIN':
            out.append(_сторож(код, data.доля_с_остатком(acc, skus, окно[1]), порог, 'меньше'))
        elif код == 'SEARCH_VIEWS_DROP':
            b = data.поисковые_показы(acc, skus, *base) if base else None
            w = data.поисковые_показы(acc, skus, *окно)
            b_д = (b / max(1, _дней(*base))) if (b and base) else None
            w_д = (w / max(1, _дней(*окно))) if w is not None else None
            v = (1 - w_д / b_д) if (b_д and w_д is not None) else None
            out.append(_сторож(код, v, порог, 'больше'))
        else:
            out.append({'код': код, 'значение': None, 'порог': порог, 'статус': 'UNAVAILABLE',
                        'текст': 'сторож не реализован машинно'})
    for т in c.get('guardrails_текстовые', []):
        out.append({'код': 'TEXT', 'значение': None, 'порог': None, 'статус': 'UNAVAILABLE',
                    'текст': т})
    return out


# ===== журнал оценок (только дозапись) ================================================
def читать_оценки(путь=None):
    п = путь or ОЦЕНКИ
    if not os.path.exists(п):
        return []
    out = []
    with open(п, encoding='utf-8') as f:
        for s in f:
            s = s.strip()
            if s:
                out.append(json.loads(s))
    return out


def дописать_оценку(rec, путь=None):
    """Только дозапись. Повтор на тех же данных и той же версии оценщика дубля не создаёт.

    Идентичность оценки = (гипотеза, эксперимент, версия контракта, хеш когорты,
    data_as_of, отпечаток зрелости, версия оценщика, дата прогона). Дозагрузка данных
    меняет data_as_of и рождает НОВУЮ запись со ссылкой supersedes на прежнюю — прошлое
    не переписывается. Дата прогона в идентичности нужна потому, что гейт даты сверки
    меняет вердикт на тех же данных: без неё исправленная оценка гасится как «дубль».
    """
    п = путь or ОЦЕНКИ
    for старый in читать_оценки(п):
        if старый['evaluation_id'] == rec['evaluation_id']:
            return 'дубль', старый
    каталог = os.path.dirname(п)
    if каталог:
        os.makedirs(каталог, exist_ok=True)
    with open(п, 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + '\n')
    return 'добавлено', rec


def предыдущая(rec, журнал):
    """Последняя оценка того же эксперимента — для ссылки supersedes."""
    свои = [x for x in журнал if x.get('experiment_id') == rec['experiment_id']
            and x['evaluation_id'] != rec['evaluation_id']]
    return свои[-1]['evaluation_id'] if свои else None


# ===== вердикт ========================================================================
def _рек(вердикт):
    return {'EFFECT_CONFIRMED_POSITIVE': 'KEEP',
            'EFFECT_CONFIRMED_NEGATIVE': 'ROLLBACK',
            'CONTINUE_MEASURING': 'CONTINUE_MEASURING',
            'INCONCLUSIVE': 'INCONCLUSIVE',
            'BLOCKED_BY_POWER': 'INCONCLUSIVE',
            'CONTAMINATED': 'CONTAMINATED',
            'NO_CAUSAL_CLAIM': 'NO_CAUSAL_CLAIM',
            'VOID': 'NO_CAUSAL_CLAIM',
            'CAPABILITY_PRESENT': 'NO_CAUSAL_CLAIM',
            'CAPABILITY_ABSENT': 'CONTINUE_MEASURING'}[вердикт]


def оценить(c, data, today=TODAY, журнал=None, каталог_когорт=None):
    """Полная оценка одного контракта. Ничего не применяет и ничего не отправляет."""
    журнал = журнал if журнал is not None else []

    def _собрать(*a, **k):
        return _собрать_запись(*a, today=today, **k)

    acc = c.get('account', ACC)
    # зрелость и край витрины считаются по аккаунту контракта, а не по аккаунту, с которым
    # запустили прогон: E6 живёт на oz_acc2, а evaluate-ready стартует с oz_acc1 — иначе
    # готовность данных проверяется у чужих загрузчиков.
    if getattr(data, 'acc', acc) != acc:
        data = type(data)(acc)
    тип = c['тип']
    причины, загр = [], []
    base = c.get('baseline_window')
    изм_окно = c.get('measurement_window')

    # --- 1. когорты и их неизменность -------------------------------------------
    когорта_t = когорта_c = set()
    дрейф = None
    if c.get('когорта_из_снимка'):
        когорта_t = снимок_когорты(c['когорта_из_снимка'], каталог_когорт) or set()
    elif c.get('когорта_treatment'):
        когорта_t = data.когорта(c['когорта_treatment'], c.get('режим_разреза', 'itt'))
    if c.get('контроль_из_снимка'):
        когорта_c = снимок_когорты(c['контроль_из_снимка'], каталог_когорт) or set()
    elif c.get('когорта_control'):
        когорта_c = data.когорта(c['когорта_control'], c.get('режим_разреза', 'itt'))
    if c.get('снимок_treatment'):
        снимок = снимок_когорты(c['снимок_treatment'], каталог_когорт)
        if снимок is not None and снимок != когорта_t:
            дрейф = (f"treatment: снимок {len(снимок)} SKU, сейчас {len(когорта_t)}, "
                     f"расхождение {len(снимок ^ когорта_t)}")
    ch = хеш_когорты(когорта_t)

    # найденное загрязнение фиксируется здесь, а не на шаге 7: ранние ветки (дата сверки,
    # незрелость, короткое окно) возвращались с пустым списком и прятали дрейф когорты.
    загр = загрязнения(c, дрейф, [])
    for z in загр:
        if z['код'] not in причины:
            причины.append(z['код'])

    # --- 2. зрелость данных ------------------------------------------------------
    нужны = c.get('требуемые_источники', ['ads'])
    зрел_день, готовность = None, {}
    if тип not in ('VOID_NEVER_APPLIED', 'POWER_BLOCKED', 'CAPABILITY_GAP'):
        конец = data.последний_день_витрины()
        if конец:
            d = _д(конец)
            for _ in range(14):
                г = data.зрелость(d.isoformat(), нужны)
                if all(v[0] for v in г.values()):
                    зрел_день, готовность = d.isoformat(), г
                    break
                готовность = готовность or г
                d -= dt.timedelta(days=1)
        if not готовность:
            готовность = {s: (False, 'витрина пуста') for s in нужны}

    data_as_of = зрел_день
    окно_факт = None
    if изм_окно and data_as_of:
        конец_окна = min(_д(изм_окно[1]), _д(data_as_of)).isoformat()
        окно_факт = [изм_окно[0], конец_окна]

    # --- 3. ветки, где измерения нет по сути -------------------------------------
    if тип == 'VOID_NEVER_APPLIED':
        причины += ['EXPERIMENT_VOID', 'NEVER_APPLIED', 'CAUSAL_CLAIM_FORBIDDEN']
        return _собрать(c, 'VOID', причины, ch, data_as_of, готовность, base, окно_факт,
                        {}, {}, {}, [], загр, журнал, разрешена_причинность=False)
    if тип == 'POWER_BLOCKED':
        причины += ['POWER_BLOCKED_DESIGN', 'UNDERPOWERED', 'CAUSAL_CLAIM_FORBIDDEN']
        return _собрать(c, 'BLOCKED_BY_POWER', причины, ch, data_as_of, готовность, base,
                        окно_факт, {}, {}, c.get('мощность_факт', {}), [], загр, журнал,
                        разрешена_причинность=False)
    if тип == 'CAPABILITY_GAP':
        пути = [p if p.startswith('/') else os.path.join(БАЗА, p)
                for p in c.get('признаки_способности', [])]
        есть = bool(пути) and all(os.path.exists(p) for p in пути)
        причины += ['CAPABILITY_SELF_CHECK', 'MEMORY_ONLY']
        в = 'CAPABILITY_PRESENT' if есть else 'CAPABILITY_ABSENT'
        нет = [p for p in пути if not os.path.exists(p)]
        return _собрать(c, в, причины, ch, data_as_of, готовность, base, окно_факт,
                        {'признаков': len(пути), 'все_на_месте': есть, 'отсутствуют': нет},
                        {}, {}, [], загр, журнал, разрешена_причинность=False)

    # --- 4. дата сверки ----------------------------------------------------------
    if c.get('дата_сверки') and _д(today) < _д(c['дата_сверки']):
        причины.append('BEFORE_CHECK_DATE')
        return _собрать(c, 'CONTINUE_MEASURING', причины, ch, data_as_of, готовность, base,
                        окно_факт, {}, {}, {}, [], загр, журнал, разрешена_причинность=False)

    # --- 5. зрелость -------------------------------------------------------------
    if not data_as_of:
        причины.append('DATA_NOT_MATURE')
        return _собрать(c, 'CONTINUE_MEASURING', причины, ch, data_as_of, готовность, base,
                        окно_факт, {}, {}, {}, [], загр, журнал, разрешена_причинность=False)

    # --- 5а. только сторож: основного показателя нет, причинности нет ------------
    if тип == 'GUARDRAIL_ONLY':
        skus_g = sorted(s for _, s in когорта_t)
        # постоянный сторож живёт скользящим окном: фиксированные даты в нём протухают
        if окно_факт:
            окно_g, база_g = окно_факт, base
        else:
            ш = c.get('минимальная_мощность', {}).get('мин_дней', 7)
            к = _д(data_as_of)
            окно_g = [(к - dt.timedelta(days=ш - 1)).isoformat(), к.isoformat()]
            база_g = [(к - dt.timedelta(days=2 * ш - 1)).isoformat(),
                      (к - dt.timedelta(days=ш)).isoformat()]
        рез = сторожа(c, data, acc, skus_g, база_g, окно_g)
        пробит = [g for g in рез if g['статус'] == 'BREACH']
        причины += ['MEMORY_ONLY', 'CAUSAL_CLAIM_FORBIDDEN']
        if пробит:
            причины.append('GUARDRAIL_BREACH')
        if any(g['статус'] == 'UNAVAILABLE' for g in рез):
            причины.append('GUARDRAIL_UNAVAILABLE')
        return _собрать(c, 'CONTAMINATED' if пробит else 'NO_CAUSAL_CLAIM', причины, ch,
                        data_as_of, готовность, база_g, окно_g,
                        data.свод(acc, skus_g, *окно_g), {}, {}, рез, загр, журнал,
                        разрешена_причинность=False)

    дней = _дней(*окно_факт) if окно_факт else 0
    мин_дней = c.get('минимальная_мощность', {}).get('мин_дней', 7)
    if дней < мин_дней:
        причины.append('WINDOW_TOO_SHORT')
        return _собрать(c, 'CONTINUE_MEASURING', причины, ch, data_as_of, готовность, base,
                        окно_факт, {}, {}, {'дней_в_окне': дней, 'нужно_дней': мин_дней},
                        [], загр, журнал, разрешена_причинность=False)

    # --- 6. измерение ------------------------------------------------------------
    skus_t = sorted(s for _, s in когорта_t)
    skus_c = sorted(s for _, s in когорта_c)
    рез_t = data.свод(acc, skus_t, *окно_факт)
    рез_c = data.свод(acc, skus_c, *окно_факт) if skus_c else {}
    базис_есть = bool(base) and c.get('baseline_доступен', True)
    сторожа_рез = сторожа(c, data, acc, skus_t, base if базис_есть else None, окно_факт)

    эффект = {'показатель': c['основной_показатель']}
    стат = {}
    if базис_есть:
        b_t = data.чистая_по_sku(acc, skus_t, *base)
        w_t = data.чистая_по_sku(acc, skus_t, *окно_факт)
        дельты_t = [w_t[s] - b_t.get(s, 0.0) for s in skus_t]
        эффект['дельта_treatment_на_sku_в_сутки'] = (
            round(sum(дельты_t) / len(дельты_t), 2) if дельты_t else None)
        if skus_c:
            b_c = data.чистая_по_sku(acc, skus_c, *base)
            w_c = data.чистая_по_sku(acc, skus_c, *окно_факт)
            дельты_c = [w_c[s] - b_c.get(s, 0.0) for s in skus_c]
            эффект['дельта_control_на_sku_в_сутки'] = (
                round(sum(дельты_c) / len(дельты_c), 2) if дельты_c else None)
            стат = уэлч(дельты_t, дельты_c)
            эффект['did_на_sku_в_сутки'] = (round(стат['diff'], 2)
                                            if стат['diff'] is not None else None)
        else:
            стат = {'n_treatment': len(дельты_t), 'n_control': 0, 'достаточно': False,
                    'mde_80': None, 'p': None}
    else:
        причины.append('BASELINE_UNAVAILABLE')
        эффект['чистая_на_sku_в_сутки_в_окне'] = round(
            рез_t.get('выручка_минус_расход_на_sku', 0.0) / max(1, дней), 2)
        эффект['drr_в_окне'] = рез_t.get('drr')
        стат = {'n_treatment': len(skus_t), 'n_control': 0, 'достаточно': False,
                'mde_80': None, 'p': None}

    # --- 7. загрязнения ----------------------------------------------------------
    все_когорты = c.get('_когорты_всех', {})
    пересеч = пересечения(c, все_когорты) if все_когорты else []
    загр = загрязнения(c, дрейф, пересеч)      # пересчёт с пересечениями поверх раннего списка
    for z in загр:
        if z['код'] not in причины:
            причины.append(z['код'])

    # --- 8. вердикт --------------------------------------------------------------
    мин_зак = c.get('минимальная_мощность', {}).get('мин_заказов', 10)
    пробит = [g for g in сторожа_рез if g['статус'] == 'BREACH']
    if пробит:
        причины.append('GUARDRAIL_BREACH')
    if any(g['статус'] == 'UNAVAILABLE' for g in сторожа_рез):
        причины.append('GUARDRAIL_UNAVAILABLE')

    блок = any(z['вес'] == 'блокирующее' for z in загр)
    есть_контроль = bool(skus_c) and тип in ('TREATMENT_CONTROL_DID', 'ITT', 'PER_PROTOCOL')
    # Явно зафиксированный в контракте факт: замороженного до вмешательства состава
    # контроля не существует. Флаг ставится только по итогам поиска оригинального
    # источника; сравнение с изменяемым «контролем» причинным выводом не является.
    контроль_не_заморожен = есть_контроль and c.get('контроль_заморожен') is False
    разрешена = есть_контроль and not блок and базис_есть and not контроль_не_заморожен

    if контроль_не_заморожен:
        причины += ['MISSING_FROZEN_CONTROL', 'CAUSAL_CLAIM_FORBIDDEN']
        вердикт = 'NO_CAUSAL_CLAIM'
        разрешена = False
    elif блок:
        вердикт = 'CONTAMINATED'
        разрешена = False
    elif not есть_контроль:
        причины += ['NO_CONTROL_BY_DESIGN', 'CAUSAL_CLAIM_FORBIDDEN']
        if (эффект.get('дельта_treatment_на_sku_в_сутки') or
                эффект.get('чистая_на_sku_в_сутки_в_окне') or 0) < 0:
            причины.append('EFFECT_NEGATIVE')
        вердикт = 'NO_CAUSAL_CLAIM'
        разрешена = False
    elif рез_t.get('orders', 0) < мин_зак:
        причины.append('TOO_FEW_EVENTS')
        вердикт = 'INCONCLUSIVE'
        разрешена = False
    elif not стат.get('достаточно'):
        причины.append('UNDERPOWERED')
        вердикт = 'INCONCLUSIVE'
        разрешена = False
    else:
        полож = (стат['diff'] or 0) > 0
        причины.append('EFFECT_POSITIVE' if полож else 'EFFECT_NEGATIVE')
        вердикт = 'EFFECT_CONFIRMED_POSITIVE' if полож else 'EFFECT_CONFIRMED_NEGATIVE'
        if пробит and полож:
            # сторож пробит, а показатель вырос — рост оплачен риском, KEEP не выдаём
            вердикт = 'CONTAMINATED'
            разрешена = False

    for д in c.get('дополнительные_причины', []):
        if д not in причины:
            причины.append(д)

    return _собрать(c, вердикт, причины, ch, data_as_of, готовность, base, окно_факт,
                    рез_t, рез_c, {**стат, 'дней_в_окне': дней}, сторожа_рез, загр, журнал,
                    разрешена_причинность=разрешена, эффект=эффект)


def _собрать_запись(c, вердикт, причины, ch, data_as_of, готовность, base, окно, рез_t, рез_c,
                    стат, сторожа_рез, загр, журнал, разрешена_причинность, эффект=None,
                    today=None):
    """Сборка записи. Здесь и только здесь измерение, вердикт и рекомендация разводятся."""
    гот = {k: {'готов': bool(v[0]), 'пояснение': v[1]} for k, v in (готовность or {}).items()}
    cv = версия_контракта(c)
    дата = today or TODAY
    # дата прогона входит в идентичность: гейт даты сверки меняет вердикт на тех же
    # данных, и без неё исправленная оценка гасится как «дубль» прежней.
    ид = _sha(_канон([c.get('hypothesis_id'), c['id'], cv, ch, data_as_of,
                      _отпечаток_зрелости(готовность), EVALUATOR_VERSION, дата]), 16)
    rec = {
        'evaluation_id': ид,
        'evaluated_at': дата,
        'hypothesis_id': c.get('hypothesis_id'),
        'experiment_id': c['id'],
        'contract_version': cv,
        'cohort_hash': ch,
        'data_as_of': data_as_of,
        'data_readiness': гот,
        'baseline_window': base,
        'measurement_window': окно,
        'primary_metric': c['основной_показатель'],
        'treatment_result': рез_t,
        'control_result': рез_c,
        'effect': эффект or {},
        'confidence_or_power': стат,
        'guardrail_results': сторожа_рез,
        'contaminations': загр,
        'verdict': вердикт,
        'recommendation': _рек(вердикт),
        'causal_claim_allowed': bool(разрешена_причинность),
        'reason_codes': причины,
        'evaluator_version': EVALUATOR_VERSION,
        'mode': РЕЖИМ,
        # рекомендация — это ещё не решение и тем более не действие
        'decision': None,
        'decision_by': None,
        'execution': None,
    }
    rec['supersedes_evaluation_id'] = предыдущая(rec, журнал)
    assert rec['verdict'] in ВЕРДИКТЫ, rec['verdict']
    assert rec['recommendation'] in РЕКОМЕНДАЦИИ, rec['recommendation']
    return rec


# ===== прогон по всем контрактам ======================================================
def готовые(контракты, today=TODAY):
    """Контракты, у которых дата сверки наступила (или её нет — постоянные сторожа)."""
    return [c for c in контракты
            if not c.get('дата_сверки') or _д(c['дата_сверки']) <= _д(today)]


def прогон(data, today=TODAY, путь_контрактов=None, путь_оценок=None, каталог_когорт=None,
           только=None, писать=True):
    """MEASURE -> EVALUATE -> журнал. KEEP/ROLLBACK остаются рекомендацией."""
    cs = читать_контракты(путь_контрактов)
    if только:
        только = {x.upper() for x in только}
        cs = [c for c in cs if c['id'].upper() in только]
    # когорты всех контрактов — чтобы пересечения считались по факту, а не по декларации
    все = {}
    for c in cs:
        if c.get('когорта_treatment'):
            try:
                все[c['id']] = data.когорта(c['когорта_treatment'], 'itt')
            except Exception:
                все[c['id']] = set()
    журнал = читать_оценки(путь_оценок)
    итог = []
    for c in cs:
        cc = dict(c, _когорты_всех=все)
        rec = оценить(cc, data, today=today, журнал=журнал, каталог_когорт=каталог_когорт)
        статус = 'не_записано'
        if писать:
            статус, rec = дописать_оценку(rec, путь_оценок)
            журнал.append(rec)
        итог.append({'запись': rec, 'статус_записи': статус})
    return итог
