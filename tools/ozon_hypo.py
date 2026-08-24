# поток: mkt
"""ozon_hypo.py — контур управления гипотезами Ozon. SHADOW MODE, ТОЛЬКО ЧТЕНИЕ.

Цикл: OBSERVE → DIAGNOSE → HYPOTHESIZE → PRIORITIZE → DESIGN → VALIDATE → APPROVE
      → ACT → MEASURE → EVALUATE → KEEP/ROLLBACK → MEMORY

Инструмент закрывает всё, кроме APPROVE и ACT: решение принимает человек, внешних
записей здесь нет вообще. SQL — только SELECT. Ozon-периметр; WB не читается.

Три вещи, ради которых это написано, а не сделано руками в очередной раз:

  1. Гипотеза живёт ДО эксперимента и ПОСЛЕ него. Реестр E1–E8 хранит дизайн, но
     не хранит ни наблюдения, из которого дизайн вырос, ни знания, которое осталось
     после отката. Поэтому E1 («разгон вглубь») можно предложить второй раз, и ничто
     в системе этому не помешает.
  2. Факты приходят из данных, а не из формулировки. Наблюдатели (`OBSERVERS`) —
     сменные capabilities: каждая читает свою витрину и возвращает кандидатов
     с уже посчитанным размером сегмента. LLM может предложить механизм и дизайн,
     но метрику, выборку и ограничение считает валидатор.
  3. Запуск блокируется ПО ПРИЧИНЕ, а не по ощущению. Шестнадцать валидаторов —
     это перечень уже совершённых на E1–E8 ошибок, переписанный в проверки.

Подкоманды:
  observe     наблюдения из витрин (что вообще происходит)
  propose     кандидаты-гипотезы из наблюдений + дедупликация
  validate    16 проверок: пересечения, контроль, мощность, экономика, откат
  prioritize  прозрачный балл с объяснением каждой составляющей
  plan        предварительный экспериментальный контракт для лучших
  status      состояние реестра и конечного автомата
  evaluate    что подлежит измерению и когда; фиксация решения человека
"""
import os, sys, json, argparse, hashlib, math, datetime as dt

sys.path.insert(0, '/opt/mp-analytics')
from core import db  # noqa: E402

ACC = 'oz_acc1'
BASE = '/opt/mp-analytics'
REGISTRY = BASE + '/docs/experiments/ozon_hypotheses.json'
LOG = BASE + '/docs/experiments/ozon_hypothesis_log.jsonl'
EXPERIMENTS = BASE + '/docs/experiments/ozon_experiments.json'
STUDIES = BASE + '/docs/experiments/ozon_observational_studies.json'
COHORTS = BASE + '/docs/experiments/cohorts'
REPORTS = BASE + '/docs/reports'

TODAY = '2026-08-21'          # дата прогона задаётся явно: скрытых now() в расчётах нет

# ============================== конечный автомат ======================================
СТАТУСЫ = ('OBSERVED', 'DRAFT', 'VALIDATED', 'READY', 'APPROVAL_REQUIRED', 'RUNNING',
           'MEASURING', 'EVALUATED', 'KEEP', 'ROLLBACK', 'INCONCLUSIVE', 'REJECTED')

ПЕРЕХОДЫ = {
    'OBSERVED': ('DRAFT', 'REJECTED'),
    'DRAFT': ('VALIDATED', 'REJECTED'),
    'VALIDATED': ('READY', 'DRAFT', 'REJECTED'),
    'READY': ('APPROVAL_REQUIRED', 'DRAFT', 'REJECTED'),
    'APPROVAL_REQUIRED': ('RUNNING', 'READY', 'REJECTED'),
    'RUNNING': ('MEASURING', 'ROLLBACK'),
    'MEASURING': ('EVALUATED', 'ROLLBACK'),
    'EVALUATED': ('KEEP', 'ROLLBACK', 'INCONCLUSIVE'),
    'KEEP': (), 'ROLLBACK': ('DRAFT',), 'INCONCLUSIVE': ('DRAFT',), 'REJECTED': (),
}
ТЕРМИНАЛЬНЫЕ = ('KEEP', 'REJECTED')


class ПереходЗапрещён(Exception):
    pass


def проверить_переход(было, стало):
    """Автомат валидируется программно: иначе статус становится подписью, а не состоянием."""
    if стало not in СТАТУСЫ:
        raise ПереходЗапрещён(f'нет такого статуса: {стало}')
    if было not in СТАТУСЫ:
        raise ПереходЗапрещён(f'нет такого статуса: {было}')
    if стало not in ПЕРЕХОДЫ[было]:
        raise ПереходЗапрещён(
            f'{было} → {стало} запрещён; допустимо: {", ".join(ПЕРЕХОДЫ[было]) or "ничего"}')
    return True


ПОЧЕМУ_ТАК = {
    'READY': 'без измеримой основной метрики, достаточной выборки и валидного контроля '
             'статус READY не выдаётся: иначе эксперимент нельзя будет прочитать',
    'RUNNING': 'вход в RUNNING возможен только из APPROVAL_REQUIRED — воздействие '
               'начинается после решения человека, а не после расчёта',
    'ROLLBACK': 'откат доступен из любого рабочего состояния: невозможность отката '
                'сама по себе основание не запускать',
}

# ============================== матрица автономности ==================================
УРОВНИ = ('AUTONOMOUS_READ', 'AUTONOMOUS_SHADOW', 'APPROVAL_REQUIRED',
          'FOUNDER_APPROVAL', 'BLOCKED')

АВТОНОМНОСТЬ = {
    'наблюдение и диагностика': ('AUTONOMOUS_READ', 'только SELECT, ничего не меняется'),
    'построение гипотез и расчёты': ('AUTONOMOUS_READ', 'вывод в файл, воздействия нет'),
    'отчёты и сверки': ('AUTONOMOUS_READ', 'чтение витрин и канона BI'),
    'дизайн эксперимента и dry-run': ('AUTONOMOUS_SHADOW',
                                      'план и GET-проверка допустимости; отправки нет'),
    'изменение ставки ≤10 % на ≤200 SKU': ('APPROVAL_REQUIRED',
                                           'обратимо, состояние снимается до отправки'),
    'изменение состава рекламы ≤200 SKU': ('APPROVAL_REQUIRED', 'обратимо, есть откат'),
    'массовое снятие или возврат рекламы': ('FOUNDER_APPROVAL',
                                            'затрагивает большую долю каталога'),
    'изменение цен': ('FOUNDER_APPROVAL', 'деньги покупателя и индекс цены'),
    'изменение бюджетов кампаний': ('FOUNDER_APPROVAL',
                                    'общий бюджет перераспределяет трафик всему аккаунту'),
    'действие без канонической экономики': ('BLOCKED', 'результат нечем прочитать'),
    'действие без контрольной группы': ('BLOCKED', 'эффект неотделим от сезонности'),
    'действие без отката': ('BLOCKED', 'ошибка станет постоянной'),
    'действие без наблюдаемости результата': ('BLOCKED', 'учиться будет не на чем'),
}

ПОТОЛОК_СЕССИИ = 'APPROVAL_REQUIRED'   # все внешние записи минимум здесь; сегодня их нет


def уровень_действия(рычаг, охват, обратимо, есть_контроль, экономика, откат_ок):
    """Куда попадает конкретное действие. Ужесточение всегда побеждает смягчение.

    `экономика` трёхзначна, и это важно: полное отсутствие оценки — BLOCKED, а канон,
    который есть, но грубее единицы анализа, — не запрет, а запрет на АВТОНОМНОСТЬ:
    решение принимает человек и берёт риск на себя."""
    if экономика == 'нет':
        return 'BLOCKED', 'финансовый результат нечем прочитать даже на уровне аккаунта'
    if not есть_контроль:
        return 'BLOCKED', 'нет контрольной группы'
    if not (обратимо and откат_ок):
        return 'BLOCKED', 'нет отката'
    if экономика == 'грубее':
        return 'FOUNDER_APPROVAL', ('канон BI не разложен на единицу анализа '
                                   '(BLOCKED_BY_ECONOMICS_GRANULARITY): автономно нельзя')
    if рычаг in ('цена', 'бюджет'):
        return 'FOUNDER_APPROVAL', f'рычаг «{рычаг}» затрагивает деньги напрямую'
    if охват > МАССОВЫЙ_ПОРОГ:
        return 'FOUNDER_APPROVAL', f'охват {охват} — массовое воздействие'
    return 'APPROVAL_REQUIRED', 'обратимое изменение в пределах порога'


# ============================== реестр и журнал =======================================
ПОЛЯ = ('hypothesis_id', 'created_at', 'source_observation', 'business_problem',
        'mechanism', 'target_segment', 'proposed_lever', 'expected_business_effect',
        'current_evidence', 'counter_evidence', 'primary_metric', 'secondary_metrics',
        'guardrails', 'required_sample', 'minimum_duration', 'interference_scope',
        'dependencies', 'risk_level', 'approval_level', 'estimated_upside_rub',
        'estimated_downside_rub', 'confidence', 'time_to_learn', 'priority_score',
        'status', 'experiment_id', 'result', 'decision', 'learning', 'next_hypotheses',
        'provenance', 'blocking_reason')

# Происхождение — не метаданные, а допуск. Гипотеза, которую никто не увидел в данных,
# не должна доходить до READY только потому, что красиво звучит.
ПРОИСХОЖДЕНИЕ = ('DETECTED_FROM_LIVE_DATA',        # наблюдатель нашёл отклонение в витрине
                 'SEEDED_FROM_HISTORY',            # перенесено из реестра экспериментов
                 'DERIVED_FROM_FAILED_EXPERIMENT', # выведено из отрицательного результата
                 'MANUALLY_DEFINED',               # поставлено человеком вручную
                 'LLM_SUGGESTED_WITHOUT_DATA')     # придумано без опоры на данные — не READY


def пустая(hid, **kw):
    h = {k: None for k in ПОЛЯ}
    h.update({'hypothesis_id': hid, 'created_at': TODAY, 'status': 'OBSERVED',
              'secondary_metrics': [], 'guardrails': [], 'dependencies': [],
              'next_hypotheses': [], 'counter_evidence': [],
              'provenance': 'MANUALLY_DEFINED', 'blocking_reason': None})
    h.update(kw)
    return h


def читать_реестр():
    try:
        with open(REGISTRY, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {'_': 'Реестр гипотез Ozon. Гипотеза живёт до эксперимента и после него.',
                '_версия': TODAY, 'гипотезы': []}


def писать_реестр(r):
    with open(REGISTRY, 'w', encoding='utf-8') as f:
        json.dump(r, f, ensure_ascii=False, indent=2)


def запись_в_журнал(hid, было, стало, причина, кто='claude-shadow'):
    """Журнал append-only: история решений не переписывается, иначе учиться не на чем."""
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(json.dumps({'ts': TODAY, 'hypothesis_id': hid, 'было': было,
                            'стало': стало, 'причина': причина, 'кто': кто},
                           ensure_ascii=False) + '\n')


РЕЖИМ = 'SHADOW_MODE_V0_1'
BLOCK_POWER = 'BLOCKED_BY_POWER'
ЗАКРЫТЫЕ_ДЛЯ_БЕССИЛЬНЫХ = ('READY', 'APPROVAL_REQUIRED', 'RUNNING')
ПУТИ_РАЗБЛОКИРОВКИ = (
    'более крупная единица анализа: кампания, семья или когорта вместо SKU',
    'кластерный outcome: измерять на кластере целиком, а не на его элементах',
    'существенно более длинное окно: месяцы вместо недель',
    'другой дизайн: не лифт заказов на SKU, а метрика с большей частотой событий')


# Зафиксировано решением сессии 2026-08-21. Это не описание намерений, а рамка, в которой
# контуру разрешено работать до следующего пересмотра.
ФИКСАЦИИ = (
    f'Статус контура: {РЕЖИМ}.',
    'Автоматически работают OBSERVE → HYPOTHESIZE → PRIORITIZE → DESIGN → VALIDATE → PLAN.',
    'ACT остаётся только после разрешения человека.',
    'EVALUATE → KEEP/ROLLBACK → MEMORY автоматически не замкнуты.',
    'Следующий главный capability gap — автоматический evaluator результатов. H-009 — '
    'задача развития способности системы, а не рекламный эксперимент.',
    'H-006, H-008 и H-004 сегодня не одобрены: решение DEFERRED.',
    f'H-002/W1 не одобряется: {BLOCK_POWER}.',
    'E5–E8 и H-001 продолжают только накапливать данные.',
    'Источник истины по финансам — BI; при нехватке детализации действует '
    'BLOCKED_BY_ECONOMICS_GRANULARITY.',
    'Никаких ставок, цен, кампаний, товаров, cron/systemd и внешних API-записей.',
)


def недостаточная_мощность(h):
    """Мощности нет — значит нет и эксперимента.

    Дробление на волны мощность не создаёт: волна меньше сегмента, и накопление когорты
    по волнам упирается в то же число заказов. H-002 даёт 4,2 заказа на плечо при нужных
    128 — такой замысел может храниться только как проект дизайна."""
    вал = {x['код']: x['вердикт'] for x in (h.get('_валидация') or [])}
    if вал.get('V12_POWER') == 'fail':
        return True
    m = h.get('_мощность') or {}
    нехватка = m.get('во_сколько_не_хватает')
    return bool(нехватка and нехватка > 1)


def почему_бессильна(h):
    m = h.get('_мощность') or {}
    if m.get('режим') == 'счётная':
        return (f'{m.get("λ_на_плечо")} заказов на плечо при необходимых '
                f'{m.get("нужно_заказов_на_плечо")}')
    if m.get('нужно_sku'):
        return f'{m.get("n")} SKU при необходимых {m.get("нужно_sku")}'
    return 'выборки не хватает даже для оценки MDE'


def перевести(h, стало, причина, кто='claude-shadow'):
    было = h['status']
    проверить_переход(было, стало)
    if стало in ЗАКРЫТЫЕ_ДЛЯ_БЕССИЛЬНЫХ and недостаточная_мощность(h):
        raise ПереходЗапрещён(
            f'{h.get("hypothesis_id")}: {BLOCK_POWER} ({почему_бессильна(h)}) — '
            f'{стало} недостижим. Пути разблокировки: ' + '; '.join(ПУТИ_РАЗБЛОКИРОВКИ))
    h['status'] = стало
    запись_в_журнал(h['hypothesis_id'], было, стало, причина, кто)
    return h


# ============================== окна и общие утилиты ==================================
def _d(s):
    return dt.date.fromisoformat(s[:10])


def последний_зрелый_день():
    """Витрина рекламы доезжает утром следующего дня — она и задаёт край наблюдений."""
    r = db.query("SELECT max(stat_date)::text d FROM mkt_ozon_ads_sku_daily WHERE account=%s", (ACC,))
    return r[0]['d'] or TODAY


def окна(дней=7):
    """Два сопоставимых окна подряд: текущее и предыдущее той же длины и тех же дней недели."""
    e1 = _d(последний_зрелый_день())
    s1 = e1 - dt.timedelta(days=дней - 1)
    e0 = s1 - dt.timedelta(days=1)
    s0 = e0 - dt.timedelta(days=дней - 1)
    return ((s1.isoformat(), e1.isoformat()), (s0.isoformat(), e0.isoformat()))


def _дельта(нов, ст):
    if not ст:
        return None
    return round(100.0 * (float(нов) - float(ст)) / float(ст), 1)


def наблюдение(код, заголовок, факт, размер, единица, рычаг, подробности=None, sku=None):
    return {'код': код, 'заголовок': заголовок, 'факт': факт, 'размер_сегмента': размер,
            'единица': единица, 'рычаг': рычаг, 'подробности': подробности or {},
            'sku': sku or []}


# ============================== наблюдатели (сменные capabilities) ====================
# Каждый наблюдатель — отдельная функция, читающая свою витрину и возвращающая список
# наблюдений с УЖЕ посчитанным размером сегмента. Добавление рычага (цены, поиск,
# ассортимент) = добавление функции в OBSERVERS, а не переписывание инструмента.

def obs_продажи(w1, w0):
    """Сдвиг заказов, штук и выручки: первый вопрос — «изменилось ли вообще что-нибудь»."""
    q = """SELECT count(DISTINCT r.posting_number) postings,
                  sum((p->>'quantity')::int) qty,
                  sum((p->>'quantity')::int * (p->>'price')::float) rub
           FROM raw_ozon_posting r, jsonb_array_elements(r.payload->'products') p
           WHERE r.account=%s AND r.in_process_at::date BETWEEN %s AND %s
             AND r.status <> 'cancelled'"""
    a = db.query(q, (ACC, *w1))[0]
    b = db.query(q, (ACC, *w0))[0]
    out = []
    for поле, имя in (('postings', 'заказов'), ('qty', 'штук'), ('rub', 'выручка, ₽')):
        d = _дельта(float(a[поле] or 0), float(b[поле] or 0))
        if d is not None and abs(d) >= 5:
            out.append(наблюдение(
                'SALES_SHIFT', f'{имя}: {d:+.1f} % неделя к неделе',
                f'{имя} {float(b[поле] or 0):.0f} → {float(a[поле] or 0):.0f}',
                0, 'аккаунт', 'наблюдение',
                {'окно_текущее': w1, 'окно_прошлое': w0, 'дельта_%': d}))
    return out


def det_поисковая_видимость(нов, ст, срез):
    """Чистая часть наблюдателя: агрегаты на входе, наблюдение или пусто на выходе.

    Разделение на det_* и obs_* сделано не ради красоты. Пока факт рождался внутри
    запроса, утверждение «генератор реагирует на данные» нельзя было проверить иначе
    как прогоном по проду. Теперь на подсунутых строках видно и обратное: отклонения
    нет — гипотезы нет."""
    dv = _дельта(float(нов['v'] or 0), float(ст['v'] or 0))
    du = _дельта(float(нов['u'] or 0), float(ст['u'] or 0))
    if dv is None or du is None or dv - du > -5:
        return []
    return [наблюдение(
        'SEARCH_VISIBILITY_LOSS',
        f'показы {dv:+.1f} % против спроса {du:+.1f} % за неделю',
        f'разрыв {dv - du:+.1f} п.п.; из {срез["всего"]} SKU среза {срез["немые"]} со спросом '
        f'и нулевым показом',
        срез['всего'], 'SKU', 'реклама/поиск',
        {'период': нов['pe'], 'предыдущий': ст['pe'], 'показы_%': dv, 'спрос_%': du})]


def obs_поисковая_видимость(w1, w0):
    """Показы в поиске падают быстрее спроса — это потеря видимости, а не спад рынка."""
    r = db.query("""SELECT period_end::text pe, sum(unique_view_users) v,
                           sum(unique_search_users) u
                    FROM ozon_search_product WHERE account=%s GROUP BY 1 ORDER BY 1 DESC LIMIT 2""",
                 (ACC,))
    if len(r) < 2:
        return []
    нов, ст = r[0], r[1]
    if not det_поисковая_видимость(нов, ст, {'всего': 0, 'немые': 0}):
        return []                       # отклонения нет — второй запрос не нужен
    c = db.query("""SELECT count(*) всего,
                           count(*) FILTER (WHERE unique_search_users>0 AND unique_view_users=0) немые
                    FROM ozon_search_product WHERE account=%s AND period_end=%s""",
                 (ACC, нов['pe']))[0]
    return det_поисковая_видимость(нов, ст, c)


def obs_спрос_по_фразам(w1, w0):
    """Спрос по фразе — экзогенная величина: если он вырос, а мы не выросли, это наша потеря."""
    try:
        r = db.query("""SELECT period_end::text pe, sum(unique_search_users) u
                        FROM ozon_search_query WHERE account=%s GROUP BY 1 ORDER BY 1 DESC LIMIT 2""",
                     (ACC,))
    except Exception as e:
        return [наблюдение('PHRASE_DEMAND_SHIFT', 'витрина фраз недоступна', str(e)[:80],
                           0, '—', 'наблюдение')]
    if len(r) < 2:
        return []
    d = _дельта(float(r[0]['u'] or 0), float(r[1]['u'] or 0))
    if d is None or abs(d) < 10:
        return []
    n = db.query("""SELECT count(DISTINCT lower(btrim(query))) n FROM ozon_search_query
                    WHERE account=%s AND period_end=%s""", (ACC, r[0]['pe']))[0]['n']
    return [наблюдение('PHRASE_DEMAND_SHIFT', f'спрос по фразам {d:+.1f} % за неделю',
                       f'{n} фраз в последнем срезе {r[0]["pe"]}', n, 'фраза', 'наблюдение',
                       {'период': r[0]['pe'], 'дельта_%': d})]


def det_неэффективный_расход(строки, окно=None, порог_руб=50):
    """Расход без единого заказа. Порог в рублях, а не в процентах: пятьдесят рублей —
    это цена решения, ниже неё вмешательство дороже своей пользы."""
    плох = [x for x in строки if (x['om'] or 0) == 0 and (x['sp'] or 0) >= порог_руб]
    if not плох:
        return []
    сумма = sum(x['sp'] for x in плох)
    всего = sum(x['sp'] for x in строки) or 1
    return [наблюдение(
        'INEFFICIENT_SPEND', f'{len(плох)} SKU потратили {сумма:,.0f} ₽ без единого заказа',
        f'{100 * сумма / всего:.1f} % недельного расхода рекламы', len(плох), 'SKU', 'ставка',
        {'окно': окно, 'расход_₽': round(сумма), 'весь_расход_₽': round(всего)},
        sku=[x['s'] for x in плох])]


def obs_неэффективный_расход(w1, w0):
    """Расход без выручки — деньги, которые можно перестать тратить сегодня же."""
    r = db.query("""SELECT sku::text s, sum(money_spent)::float sp, sum(orders_money)::float om,
                           sum(views) v, sum(clicks) k
                    FROM mkt_ozon_ads_sku_daily WHERE account=%s AND stat_date BETWEEN %s AND %s
                    GROUP BY 1 HAVING sum(money_spent)>0""", (ACC, *w1))
    return det_неэффективный_расход(r, w1)


def obs_каннибализация_бюджета(w1, w0):
    """Общий бюджет кампании: подъём ставок головы гасит показы хвоста — это уже случалось."""
    r = db.query("""SELECT campaign_id::text c, stat_date::text d, sum(views) v,
                           sum(money_spent)::float sp, count(DISTINCT sku) n
                    FROM mkt_ozon_ads_sku_daily WHERE account=%s AND stat_date BETWEEN %s AND %s
                    GROUP BY 1,2""", (ACC, w0[0], w1[1]))
    return det_каннибализация(r, w1)


def det_каннибализация(строки, w1):
    """Показы падают, расход держится — бюджет перетёк внутрь головы. Это и был E1."""
    по_кампаниям = {}
    for x in строки:
        k = по_кампаниям.setdefault(x['c'], {'v1': 0, 'v0': 0, 'sp1': 0.0, 'sp0': 0.0, 'n': 0})
        нов = w1[0] <= x['d'] <= w1[1]
        k['v1' if нов else 'v0'] += x['v'] or 0
        k['sp1' if нов else 'sp0'] += x['sp'] or 0.0
        k['n'] = max(k['n'], x['n'] or 0)
    плох = []
    for c, k in по_кампаниям.items():
        if k['v0'] < 1000 or k['sp0'] <= 0:
            continue
        dv, ds = _дельта(k['v1'], k['v0']), _дельта(k['sp1'], k['sp0'])
        if dv is not None and ds is not None and dv <= -20 and ds >= -5:
            плох.append({'кампания': c, 'показы_%': dv, 'расход_%': ds, 'sku': k['n']})
    if not плох:
        return []
    плох.sort(key=lambda x: x['показы_%'])
    сум = sum(x['sku'] for x in плох)
    return [наблюдение(
        'BUDGET_CANNIBALIZATION',
        f'{len(плох)} кампаний потеряли показы, не потеряв расход',
        'расход держится, охват падает — признак перераспределения общего бюджета внутрь головы',
        сум, 'SKU', 'ставка', {'кампании': плох[:10]})]


def obs_остаток_спрос_без_рекламы(w1, w0):
    """Товар есть, спрос есть, показов нет — самый дешёвый из возможных рычагов."""
    r = db.query("""WITH ad AS (SELECT DISTINCT sku::text s FROM mkt_ozon_ads_sku_daily
                                WHERE account=%s AND stat_date BETWEEN %s AND %s AND views>0),
                         dem AS (SELECT sku::text s, sum(unique_search_users) u
                                 FROM ozon_search_product WHERE account=%s
                                   AND period_end=(SELECT max(period_end) FROM ozon_search_product
                                                   WHERE account=%s)
                                 GROUP BY 1 HAVING sum(unique_search_users)>0),
                         st AS (SELECT DISTINCT op.sku::text s FROM ozon_product op
                                JOIN supplier_stock ss ON ss.external_code=op.offer_id
                                WHERE op.account=%s AND ss.captured_at::date=
                                      (SELECT max(captured_at::date) FROM supplier_stock)
                                  AND ss.stock>0)
                    SELECT dem.s, dem.u FROM dem JOIN st USING(s)
                    WHERE dem.s NOT IN (SELECT s FROM ad) ORDER BY dem.u DESC""",
                 (ACC, *w1, ACC, ACC, ACC))
    return det_остаток_спрос_без_рекламы(r)


def det_остаток_спрос_без_рекламы(строки):
    """Самый дешёвый рычаг: товар есть, спрос есть, показов нет.

    Здесь же снимаются веса спроса по каждому SKU — они нужны волновому режиму для
    стратификации, иначе в воздействие случайно уедет весь спрос сегмента."""
    if not строки:
        return []
    o = наблюдение(
        'STOCK_DEMAND_NO_AD',
        f'{len(строки)} SKU: остаток есть, спрос есть, рекламных показов нет',
        f'суммарный спрос {sum(x["u"] or 0 for x in строки):,.0f} пользователей за неделю',
        len(строки), 'SKU', 'состав рекламы',
        {'топ': [{'sku': x['s'], 'спрос': x['u']} for x in строки[:10]]},
        sku=[x['s'] for x in строки])
    o['веса'] = {x['s']: float(x['u'] or 0) for x in строки}
    return [o]


def obs_прибыльный_без_показов(w1, w0):
    """Маржинальный товар с конверсией, но без охвата: рычаг вверх, а не вниз."""
    try:
        r = db.query("""SELECT DISTINCT ON (sku) sku::text s, margin_pct::float m, cr::float cr,
                               search_pos, revenue90::float rev, our_price::float pr
                        FROM mkt_ozon_bid_plan WHERE account=%s AND margin_pct IS NOT NULL
                        ORDER BY sku, built_at DESC""", (ACC,))
    except Exception as e:
        return [наблюдение('PROFITABLE_LOW_REACH', 'витрина плана ставок недоступна',
                           str(e)[:80], 0, '—', 'ставка')]
    ad = {x['s']: x['v'] for x in db.query(
        """SELECT sku::text s, sum(views) v FROM mkt_ozon_ads_sku_daily
           WHERE account=%s AND stat_date BETWEEN %s AND %s GROUP BY 1""", (ACC, *w1))}
    кан = [x for x in r if (x['m'] or 0) >= 20 and (x['cr'] or 0) > 0
           and ad.get(x['s'], 0) < 100]
    if not кан:
        return []
    кан.sort(key=lambda x: -(x['rev'] or 0))
    return [наблюдение(
        'PROFITABLE_LOW_REACH', f'{len(кан)} SKU с маржой ≥20 % получают <100 показов в неделю',
        'экономика взята из вспомогательной витрины mkt_ozon_bid_plan, не из канона BI',
        len(кан), 'SKU', 'ставка',
        {'топ': [{'sku': x['s'], 'маржа_%': round(x['m'], 1), 'выручка90_₽': round(x['rev'] or 0)}
                 for x in кан[:10]], 'экономика': 'вспомогательная'},
        sku=[x['s'] for x in кан])]


def obs_цена(w1, w0):
    """Цена и индекс цены: движение цены ломает чтение любого рекламного эксперимента.

    `ozon_price_index.sku` — чужое пространство идентификаторов (не тот sku, что в рекламе
    и отправлениях). Единственный общий ключ — `offer_id`, через него и переводим."""
    r = db.query("""WITH p AS (SELECT offer_id, collected_on::text d, avg(price)::float pr,
                                      max(color_index) ci
                               FROM ozon_price_index WHERE account=%s
                                 AND collected_on IN (%s,%s) GROUP BY 1,2)
                    SELECT op.sku::text s,
                           max(p.pr) FILTER (WHERE p.d=%s) новая,
                           max(p.pr) FILTER (WHERE p.d=%s) старая,
                           max(p.ci) FILTER (WHERE p.d=%s) индекс
                    FROM ozon_product op JOIN p USING(offer_id)
                    WHERE op.account=%s GROUP BY 1""",
                 (ACC, w1[1], w0[0], w1[1], w0[0], w1[1], ACC))
    дв = [x for x in r if x['новая'] and x['старая']
          and abs(x['новая'] - x['старая']) / x['старая'] > 0.05]
    красн = [x for x in r if str(x['индекс'] or '').lower() in ('red', 'красный')]
    out = []
    if дв:
        вверх = sum(1 for x in дв if x['новая'] > x['старая'])
        out.append(наблюдение(
            'PRICE_SHIFT', f'{len(дв)} SKU сдвинули цену более чем на 5 % за неделю',
            f'вверх {вверх}, вниз {len(дв) - вверх}; поле `ozon_price_index.price`',
            len(дв), 'SKU', 'цена', {'окно': [w0[0], w1[1]]}, sku=[x['s'] for x in дв]))
    if красн:
        out.append(наблюдение(
            'PRICE_INDEX_RED', f'{len(красн)} SKU в красной зоне индекса цены',
            'Ozon режет выдачу и участие в акциях при красном индексе',
            len(красн), 'SKU', 'цена', {}, sku=[x['s'] for x in красн]))
    return out


def obs_новый_товар(w1, w0):
    """Новинка не имеет истории: любой её baseline фиктивен, и это надо знать заранее."""
    r = db.query("""WITH f AS (SELECT (p->>'sku')::text s, min(r.in_process_at::date) d0
                               FROM raw_ozon_posting r, jsonb_array_elements(r.payload->'products') p
                               WHERE r.account=%s GROUP BY 1)
                    SELECT count(*) n FROM f WHERE d0 >= %s::date - 30""", (ACC, w1[1]))
    n = r[0]['n']
    if not n:
        return []
    return [наблюдение('NEW_ITEM', f'{n} SKU впервые продались за последние 30 дней',
                       'у них нет baseline — в эксперименты входить не могут',
                       n, 'SKU', 'ассортимент', {'край': w1[1]})]


def obs_семья_наборов(w1, w0):
    """Набор, одиночка и мультипак одной семьи конкурируют между собой за один и тот же спрос."""
    r = db.query("""SELECT op.sku::text s, op.name, op.offer_id FROM ozon_product op
                    WHERE op.account=%s AND op.is_archived IS NOT TRUE""", (ACC,))
    семьи = {}
    for x in r:
        art = (x['offer_id'] or '').strip()
        корень = ''.join(ch for ch in art if ch.isdigit())[:4]
        if len(корень) < 4:
            continue
        имя = (x['name'] or '').lower()
        вид = ('набор' if 'набор' in имя or 'комплект' in имя
               else 'мультипак' if any(k in имя for k in ('x2', 'х2', 'x6', 'х6', 'x10', 'х10'))
               else 'одиночка')
        семьи.setdefault(корень, {}).setdefault(вид, []).append(x['s'])
    смеш = {k: v for k, v in семьи.items() if len(v) > 1}
    if not смеш:
        return []
    n = sum(len(s) for v in смеш.values() for s in v.values())
    return [наблюдение(
        'FAMILY_MIX', f'{len(смеш)} товарных семей содержат более одного вида упаковки',
        f'{n} SKU; внутри семьи реклама одного вида переносится на другой — единица решения '
        f'не SKU, а семья', len(смеш), 'семья', 'состав рекламы',
        {'пример': list(смеш.items())[0][0], 'видов': sorted({v for x in смеш.values() for v in x})})]


def obs_расхождение_с_прогнозом(w1, w0):
    """Прошлое решение обещало эффект — сверяем с фактом, иначе прогноз ничем не ограничен."""
    try:
        r = db.query("""SELECT count(*) всего,
                               count(*) FILTER (WHERE outcome IS NOT NULL) сверено,
                               count(*) FILTER (WHERE applied) применено
                        FROM mkt_ozon_bid_journal WHERE account=%s""", (ACC,))[0]
    except Exception as e:
        return [наблюдение('FORECAST_GAP', 'журнал решений недоступен', str(e)[:80],
                           0, '—', 'наблюдение')]
    несверено = (r['применено'] or 0) - (r['сверено'] or 0)
    if несверено <= 0:
        return []
    return [наблюдение(
        'FORECAST_GAP', f'{несверено} применённых решений по ставкам не сверены с фактом',
        f'всего записей {r["всего"]}, применено {r["применено"]}, сверено {r["сверено"]}',
        несверено, 'решение', 'наблюдение',
        {'источник': 'mkt_ozon_bid_journal', 'аккаунт': ACC, 'всего_записей': r['всего'],
         'применено': r['применено'], 'сверено': r['сверено'], 'не_сверено': несверено,
         'доля_сверенных_%': round(100.0 * (r['сверено'] or 0) / max(1, r['применено']), 1)})]


OBSERVERS = (
    ('продажи', obs_продажи),
    ('поисковая видимость', obs_поисковая_видимость),
    ('спрос по фразам', obs_спрос_по_фразам),
    ('неэффективный расход', obs_неэффективный_расход),
    ('каннибализация бюджета', obs_каннибализация_бюджета),
    ('остаток+спрос без рекламы', obs_остаток_спрос_без_рекламы),
    ('прибыльный без показов', obs_прибыльный_без_показов),
    ('цена и индекс', obs_цена),
    ('новинки', obs_новый_товар),
    ('семьи наборов', obs_семья_наборов),
    ('расхождение с прогнозом', obs_расхождение_с_прогнозом),
)


def собрать_наблюдения():
    w1, w0 = окна()
    факты, сбои = [], []
    for имя, f in OBSERVERS:
        try:
            факты.extend(f(w1, w0))
        except Exception as e:                      # наблюдатель падает молча только в отчёт
            сбои.append({'наблюдатель': имя, 'ошибка': str(e)[:160]})
    return {'окно_текущее': w1, 'окно_прошлое': w0, 'наблюдения': факты, 'сбои': сбои}


# ============================== контекст: что уже идёт ================================
import csv, os  # noqa: E402


def _когорта(файл):
    p = os.path.join(COHORTS, файл)
    if not os.path.exists(p):
        return []
    with open(p, encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    if rows and 'в_когорте' in rows[0]:
        rows = [r for r in rows if str(r['в_когорте']).lower() in ('да', 'true', '1', 'yes')]
    return [r['sku'] for r in rows if r.get('sku')]


ЗАНЯТЫЕ_КОГОРТЫ = {
    'E5': ('E5_treatment_2026-08-20.csv', 'E5_control_2026-08-20.csv'),
    'E6': ('E6_treatment_2026-08-20.csv', 'E6_control_2026-08-20.csv'),
    'E7': ('E7_treatment_2026-08-20.csv',),
    'E8': ('E8_treatment_2026-08-20.csv', 'E8_control_2026-08-20.csv'),
    'H-001': ('HALO_CORE_A_STABLE_ADVERTISED_2026-08-21.csv',
              'HALO_BASELINE_NEVER_ADVERTISED_2026-08-21.csv',
              'HALO_PERSISTENT_ZERO_AD_CORE_2026-08-21.csv'),
}


def контекст():
    """Всё, что валидаторам нужно знать о текущем состоянии периметра. Один раз за прогон."""
    занято, по_экспериментам = set(), {}
    for e, файлы in ЗАНЯТЫЕ_КОГОРТЫ.items():
        s = set()
        for f in файлы:
            s |= set(_когорта(f))
        по_экспериментам[e] = s
        занято |= s
    with open(EXPERIMENTS, encoding='utf-8') as f:
        exp = json.load(f)
    даты = {e['id']: {'вмешательство': e.get('дата_вмешательства'),
                      'сверка': e.get('дата_сверки')} for e in exp['эксперименты']}
    кампании = {str(x['c']) for x in db.query(
        """SELECT DISTINCT campaign_id::text c FROM ozon_bids
           WHERE account=%s AND state <> 'CAMPAIGN_STATE_ARCHIVED'""", (ACC,))}
    ставки = db.query("""SELECT DISTINCT ON (sku) sku::text s, bid::float b, campaign_id::text c
                         FROM ozon_bids WHERE account=%s ORDER BY sku, captured_at DESC""", (ACC,))
    остаток = {x['s'] for x in db.query(
        """SELECT DISTINCT op.sku::text s FROM ozon_product op
           JOIN supplier_stock ss ON ss.external_code=op.offer_id
           WHERE op.account=%s AND ss.captured_at::date=(SELECT max(captured_at::date)
                                                         FROM supplier_stock) AND ss.stock>0""",
        (ACC,))}
    журнал = {x['s'] for x in db.query(
        "SELECT DISTINCT sku::text s FROM mkt_ozon_bid_journal WHERE account=%s", (ACC,))}
    return {'занято': занято, 'по_экспериментам': по_экспериментам, 'даты': даты,
            'кампании': кампании, 'ставки': {x['s']: x for x in ставки},
            'остаток': остаток, 'журнал': журнал,
            'зрелый_день': последний_зрелый_день()}


# ============================== генерация гипотез =====================================
# Механизм и дизайн формулирует человек/LLM — здесь они записаны как шаблоны. Числа,
# сегмент и ограничения подставляются из наблюдения, а не из формулировки.

ШАБЛОНЫ = {
    'STOCK_DEMAND_NO_AD': dict(
        hid='H-002', problem='Товар с остатком и подтверждённым поисковым спросом не получает '
                             'ни одного рекламного показа — спрос уходит конкуренту.',
        mechanism='Включение минимального показа даёт первые клики; при положительной марже '
                  'заказ окупает клик уже на первой неделе.',
        lever='состав рекламы', metric='заказы на 100 доступных SKU-дней',
        secondary=['выручка', 'ДРР', 'показы'], upside=None, downside=None,
        unit='SKU', duration=14, control='рандомизированная половина сегмента'),
    'INEFFICIENT_SPEND': dict(
        hid='H-003', problem='Часть расхода уходит на SKU, не давшие ни одного заказа за неделю.',
        mechanism='Отключение или понижение ставки на нулевых конвертерах высвобождает общий '
                  'бюджет кампании в пользу тех, кто конвертирует.',
        lever='ставка', metric='расход на заказ по кампании',
        secondary=['показы хвоста', 'выручка кампании'], upside=None, downside=None,
        unit='SKU', duration=14, control='рандомизированная половина сегмента'),
    'BUDGET_CANNIBALIZATION': dict(
        hid='H-004', problem='Кампания теряет показы, не теряя расхода: бюджет перетекает внутрь '
                             'головы и гасит охват хвоста.',
        mechanism='Разделение общего бюджета между головой и хвостом снимает конкуренцию '
                  'внутри одной кампании.',
        lever='бюджет', metric='показы хвоста при неизменном расходе',
        secondary=['заказы хвоста', 'CPM'], upside=None, downside=None,
        unit='кампания', duration=21, control='кампания-двойник без разделения'),
    'PROFITABLE_LOW_REACH': dict(
        hid='H-005', problem='Маржинальные SKU с ненулевой конверсией получают меньше 100 показов '
                             'в неделю.',
        mechanism='Точечный подъём ставки на конвертерах с запасом маржи покупает показы там, '
                  'где клик окупается.',
        lever='ставка', metric='выручка на SKU-день в наличии',
        secondary=['ДРР', 'маржа после рекламы'], upside=None, downside=None,
        unit='SKU', duration=21, control='рандомизированная половина сегмента'),
    'SEARCH_VISIBILITY_LOSS': dict(
        hid='H-006', problem='Показы падают на четверть при растущем на треть спросе — теряется '
                             'видимость, а не рынок.',
        mechanism='Восстановление показа на фразах с растущим спросом возвращает долю '
                  'показов; проверяется индексом относительной видимости.',
        lever='состав рекламы', metric='индекс относительной видимости (RVI)',
        secondary=['позиция в поиске', 'заказы'], upside=None, downside=None,
        unit='запрос', duration=28, control='фразы без вмешательства'),
    'PRICE_INDEX_RED': dict(
        hid='H-007', problem='SKU в красной зоне индекса цены получают урезанную выдачу.',
        mechanism='Приведение цены к зелёной зоне возвращает участие в выдаче и акциях.',
        lever='цена', metric='показы и заказы на SKU-день',
        secondary=['маржа', 'индекс цены'], upside=None, downside=None,
        unit='SKU', duration=21, control='рандомизированная половина сегмента'),
    'FAMILY_MIX': dict(
        hid='H-008', problem='Внутри одной товарной семьи набор, одиночка и мультипак делят '
                             'один спрос; реклама одного переносится на другого.',
        mechanism='Единица решения — семья, а не SKU: реклама концентрируется на носителе '
                  'лучшей маржи семьи.',
        lever='состав рекламы', metric='выручка семьи на семью-день',
        secondary=['каннибализация внутри семьи'], upside=None, downside=None,
        unit='семья', duration=28, control='рандомизированная половина семей'),
    'FORECAST_GAP': dict(
        hid='H-009', problem='Применённые решения по ставкам не сверены с фактом — прогноз '
                             'ничем не ограничен.',
        mechanism='Обязательная сверка m_after по каждой применённой строке журнала делает '
                  'прогноз калибруемым.',
        lever='наблюдение', metric='доля сверенных решений',
        secondary=['ошибка прогноза'], upside=0, downside=0,
        unit='решение', duration=7, control='не требуется: это не вмешательство'),
    'PRICE_SHIFT': dict(
        hid='H-010', problem='Цены движутся более чем на 5 % у тысяч SKU — это ломает чтение '
                             'любого рекламного эксперимента.',
        mechanism='Фиксация цены на составе идущих экспериментов убирает главный источник '
                  'загрязнения.',
        lever='наблюдение', metric='доля SKU эксперимента с движением цены >5 %',
        secondary=['ширина сдвига'], upside=0, downside=0,
        unit='SKU', duration=7, control='не требуется: это ограничение, а не вмешательство'),
    'NEW_ITEM': dict(
        hid='H-011', problem='Новинки без истории попадают в выборки и портят baseline.',
        mechanism='Явное исключение SKU моложе 30 дней из любых когорт.',
        lever='наблюдение', metric='доля новинок в когортах',
        secondary=[], upside=0, downside=0,
        unit='SKU', duration=7, control='не требуется'),
}


def предложить(набл, ctx):
    """Наблюдение → черновик гипотезы. Одно наблюдение = максимум одна гипотеза."""
    t = ШАБЛОНЫ.get(набл['код'])
    if not t:
        return None
    sku = [s for s in набл['sku']]
    чистые = [s for s in sku if s not in ctx['занято']]
    пересечение = sorted(set(sku) & ctx['занято'])
    h = пустая(
        t['hid'],
        source_observation=f'{набл["код"]}: {набл["заголовок"]}',
        provenance='DETECTED_FROM_LIVE_DATA',
        business_problem=t['problem'], mechanism=t['mechanism'],
        target_segment={'единица': t['unit'], 'всего': набл['размер_сегмента'],
                        'свободных_sku': len(чистые) if sku else None,
                        'пересечение_с_экспериментами': len(пересечение)},
        proposed_lever=t['lever'],
        expected_business_effect=None,
        current_evidence=набл['факт'],
        counter_evidence=[],
        primary_metric=t['metric'], secondary_metrics=t['secondary'],
        guardrails=['ДРР не выше текущего по кампании', 'остаток не уходит в ноль',
                    'маржа после рекламы не отрицательная'],
        required_sample=None, minimum_duration=t['duration'],
        interference_scope='общий бюджет кампании' if t['lever'] in ('ставка', 'бюджет')
                           else 'поисковая выдача аккаунта',
        dependencies=[f'{e}: состав освободится после сверки '
                      f'{ctx["даты"].get(e, {}).get("сверка") or "—"}'
                      for e in sorted({e for e, x in ctx['по_экспериментам'].items()
                                       if set(пересечение) & x})],
        risk_level=None, approval_level=None,
        estimated_upside_rub=t['upside'], estimated_downside_rub=t['downside'],
        confidence=None, time_to_learn=t['duration'], priority_score=None,
    )
    h['_контроль'] = t['control']
    h['_sku'] = чистые
    h['_пересечение'] = пересечение
    h['_наблюдение'] = набл
    return h


def дедуп(гипотезы):
    """Одна гипотеза на (рычаг, сегмент, механизм): иначе одно наблюдение размножается."""
    видели, out, дубли = {}, [], []
    for h in гипотезы:
        ключ = (h['proposed_lever'], h['primary_metric'])
        if ключ in видели:
            дубли.append({'id': h['hypothesis_id'], 'дубль_к': видели[ключ]})
            continue
        видели[ключ] = h['hypothesis_id']
        out.append(h)
    return out, дубли


# ============================== валидаторы ============================================
# Это не абстрактный список качества. Каждая проверка — уже совершённая на E1–E8 ошибка,
# переписанная в код: ниже в скобках указано, где именно она обошлась дорого.

ПОЛ_СТАВКИ = 7.30       # ниже площадка не пускает; на acc2 весь хвост уже лежит на полу
ПОТОЛОК_ШАГА = 10.0     # % за одно решение — правило недельного шага, E3
МАССОВЫЙ_ПОРОГ = 200    # SKU; выше — FOUNDER_APPROVAL
E1_ПОРОГ = 1000         # SKU; разгон ставок шире этого = повтор E1


def мощность(sku, метрика='', дней_базы=14, дней_теста=14):
    """MDE. Без неё «нет эффекта» неотличимо от «не увидели».

    Режим считается по основной метрике, и это не формальность. Хвост Ozon продаётся
    редко: дневная выручка отдельного SKU — почти всегда ноль, и дисперсионная оценка
    на такой единице всегда скажет «не увидим». Но метрика вида «заказы на 100 SKU-дней»
    измеряется не на SKU, а на когорте целиком — там работает счётная (пуассоновская)
    оценка, и именно так читается E8. Ошибиться режимом = похоронить измеримый дизайн
    или объявить измеримым неизмеримое."""
    if not sku:
        return None
    r = db.query("""SELECT (p->>'sku')::text s, count(*) поз,
                           sum((p->>'quantity')::int * (p->>'price')::float) rub
                    FROM raw_ozon_posting r, jsonb_array_elements(r.payload->'products') p
                    WHERE r.account=%s AND r.status <> 'cancelled'
                      AND r.in_process_at::date > %s::date - %s
                      AND (p->>'sku')::text = ANY(%s) GROUP BY 1""",
                 (ACC, последний_зрелый_день(), дней_базы, list(sku)))
    выручка = {x['s']: float(x['rub'] or 0) for x in r}
    заказы = sum(int(x['поз'] or 0) for x in r)
    n = len(sku)
    оборот = sum(выручка.values()) / дней_базы
    счётная = 'заказ' in метрика.lower()
    out = {'n': n, 'режим': 'счётная' if счётная else 'дисперсионная',
           'оборот_₽_в_день': round(оборот, 1),
           'заказов_за_базу': заказы,
           'нулевых_%': round(100.0 * (n - len(выручка)) / n, 1)}
    if счётная:
        лямбда = (заказы / 2.0) * (дней_теста / дней_базы)
        out['λ_на_плечо'] = round(лямбда, 1)
        out['MDE_отн'] = round((Z_A + Z_B) * (2.0 / лямбда) ** 0.5, 2) if лямбда > 0 else None
        out['единица_MDE'] = 'доля от базового числа заказов плеча'
        нужно = ((Z_A + Z_B) / 0.35) ** 2 * 2.0          # чтобы MDE опустился до 35 %
        out['нужно_заказов_на_плечо'] = round(нужно)
        out['во_сколько_не_хватает'] = round(нужно / лямбда, 1) if лямбда > 0 else None
    else:
        знач = [выручка.get(x, 0.0) / дней_базы for x in sku]
        m = sum(знач) / n
        sd = (sum((x - m) ** 2 for x in знач) / (n - 1)) ** 0.5 if n > 1 else 0.0
        mde = (Z_A + Z_B) * sd * (2.0 / (n / 2.0)) ** 0.5 if n >= 4 else None
        out.update({'среднее_₽_в_день': round(m, 2), 'sd': round(sd, 2),
                    'MDE_₽_в_день': round(mde, 2) if mde else None,
                    'MDE_отн': round(mde / m, 2) if mde and m else None,
                    'единица_MDE': 'доля от средней дневной выручки SKU'})
        if m and sd:
            нужно = 2.0 * ((Z_A + Z_B) * sd / (0.30 * m)) ** 2
            out['нужно_sku'] = round(нужно)
            out['во_сколько_не_хватает'] = round(нужно / n, 1)
    return out


Z_A, Z_B = 1.96, 0.84


def v01_пересечение_sku(h, ctx):
    """Состав E5–E8 и H-001 заморожен: их SKU не входят ни в одну новую когорту."""
    убрано = len(h.get('_пересечение') or [])
    осталось = sorted(set(h.get('_sku') or []) & ctx['занято'])
    if осталось:
        где = [e for e, x in ctx['по_экспериментам'].items() if set(осталось) & x]
        return ('V01_SKU_INTERSECT', 'fail',
                f'{len(осталось)} SKU заняты экспериментами {", ".join(где)} и не исключены')
    if убрано:
        return ('V01_SKU_INTERSECT', 'ok',
                f'{убрано} SKU исключены из сегмента как занятые E5–E8/H-001; остаток чист')
    return ('V01_SKU_INTERSECT', 'ok', 'состав не пересекается с идущими экспериментами')


def v02_даты_и_кампании(h, ctx):
    конец = (_d(TODAY) + dt.timedelta(days=h['minimum_duration'] or 14)).isoformat()
    занятые = [f'{e}: сверка {d["сверка"]}' for e, d in ctx['даты'].items()
               if d.get('сверка') and d['сверка'] >= TODAY]
    if h['proposed_lever'] in ('ставка', 'бюджет', 'состав рекламы') and занятые:
        return ('V02_DATE_CAMPAIGN_INTERSECT', 'warn',
                f'окно {TODAY}…{конец} накладывается на: {"; ".join(занятые)}. '
                f'Допустимо только при непересекающемся составе (V01) и отдельной кампании')
    return ('V02_DATE_CAMPAIGN_INTERSECT', 'ok', f'окно {TODAY}…{конец} свободно')


def v03_один_рычаг(h, ctx):
    рычаги = {h['proposed_lever']}
    if h.get('_доп_рычаги'):
        рычаги |= set(h['_доп_рычаги'])
    if len(рычаги) > 1:
        return ('V03_ONE_LEVER', 'fail',
                f'одновременно меняются {len(рычаги)} рычага ({", ".join(sorted(рычаги))}) — '
                f'эффект будет неразделим')
    return ('V03_ONE_LEVER', 'ok', f'единственный рычаг: {h["proposed_lever"]}')


def v04_контроль(h, ctx):
    к = h.get('_контроль')
    if not к:
        return ('V04_CONTROL', 'fail', 'контрольная группа не объявлена')
    if 'не требуется' in к:
        return ('V04_CONTROL', 'ok', f'{к}')
    if 'рандомизирован' not in к and 'двойник' not in к:
        return ('V04_CONTROL', 'warn', f'контроль неслучайный ({к}) — только квазиэксперимент')
    return ('V04_CONTROL', 'ok', к)


def v05_единица_решения(h, ctx):
    e = (h.get('target_segment') or {}).get('единица')
    if e not in ('SKU', 'семья', 'кампания', 'запрос', 'аккаунт', 'решение'):
        return ('V05_DECISION_UNIT', 'fail', f'единица решения не объявлена ({e})')
    return ('V05_DECISION_UNIT', 'ok', f'единица решения: {e}')


def v06_свежесть(h, ctx):
    отставание = (_d(TODAY) - _d(ctx['зрелый_день'])).days
    if отставание > 2:
        return ('V06_FRESHNESS', 'fail',
                f'рекламная витрина доехала только до {ctx["зрелый_день"]} ({отставание} дн. назад)')
    return ('V06_FRESHNESS', 'ok', f'витрина зрела по {ctx["зрелый_день"]}')


def v07_остаток(h, ctx):
    sku = h.get('_sku') or []
    if not sku:
        return ('V07_STOCK', 'ok', 'сегмент не на уровне SKU — проверка остатка неприменима')
    без = [s for s in sku if s not in ctx['остаток']]
    if len(без) > 0.2 * len(sku):
        return ('V07_STOCK', 'fail',
                f'{len(без)} из {len(sku)} SKU без остатка — реклама будет крутиться в пустоту')
    if без:
        return ('V07_STOCK', 'warn', f'{len(без)} SKU без остатка исключаются из состава')
    return ('V07_STOCK', 'ok', f'все {len(sku)} SKU в наличии')


def v08_экономика(h, ctx):
    e = (h.get('target_segment') or {}).get('единица')
    деньги = h['proposed_lever'] in ('ставка', 'цена', 'бюджет')
    if not деньги:
        return ('V08_ECONOMICS', 'ok', 'рычаг не трогает деньги напрямую')
    if e == 'аккаунт':
        return ('V08_ECONOMICS', 'ok', 'канон BI читается на уровне аккаунта')
    h['_флаг_экономики'] = 'BLOCKED_BY_ECONOMICS_GRANULARITY'
    return ('V08_ECONOMICS', 'warn',
            f'канонический финансовый результат BI не разложен на единицу «{e}»; доступна только '
            f'вспомогательная витрина. Статус BLOCKED_BY_ECONOMICS_GRANULARITY: наблюдать можно, '
            f'автономно менять деньги — нет')


def v09_допустимость_ставки(h, ctx):
    if h['proposed_lever'] != 'ставка':
        return ('V09_BID_ADMISSIBLE', 'ok', 'ставка не меняется')
    sku = h.get('_sku') or []
    ст = [ctx['ставки'][s]['b'] for s in sku if s in ctx['ставки']]
    if not ст:
        return ('V09_BID_ADMISSIBLE', 'fail', 'ни один SKU сегмента не найден в снимке ставок — '
                                              'менять нечего')
    на_полу = sum(1 for b in ст if b <= ПОЛ_СТАВКИ)
    if h.get('_направление') == 'вниз' and на_полу == len(ст):
        return ('V09_BID_ADMISSIBLE', 'fail',
                f'все {len(ст)} SKU уже на полу {ПОЛ_СТАВКИ} — вниз хода нет')
    return ('V09_BID_ADMISSIBLE', 'ok',
            f'{len(ст)} SKU в снимке ставок, на полу {на_полу}; шаг ограничен '
            f'{ПОТОЛОК_ШАГА:.0f} %')


def v10_общий_бюджет(h, ctx):
    if h['proposed_lever'] not in ('ставка', 'бюджет', 'состав рекламы'):
        return ('V10_SHARED_BUDGET', 'ok', 'бюджет кампании не затрагивается')
    return ('V10_SHARED_BUDGET', 'warn',
            'кампании делят общий бюджет: подъём внутри одной кампании гасит показы её же '
            'хвоста (проверено при разгоне — показы упали у ~6700 SKU). Контроль обязан лежать '
            'в той же кампании либо кампания должна быть отдельной')


def v11_baseline(h, ctx):
    sku = h.get('_sku') or []
    if not sku:
        return ('V11_BASELINE', 'ok', 'сегмент не на уровне SKU')
    m = h.get('_мощность')
    if not m:
        return ('V11_BASELINE', 'warn', 'baseline не посчитан')
    if m['режим'] == 'счётная':
        if not m['заказов_за_базу']:
            return ('V11_BASELINE', 'fail', 'за базовые две недели сегмент не дал ни одного '
                                            'заказа — сравнивать не с чем')
        if m['заказов_за_базу'] < 30:
            return ('V11_BASELINE', 'warn',
                    f'база всего {m["заказов_за_базу"]} заказов на весь сегмент')
        return ('V11_BASELINE', 'ok',
                f'база {m["заказов_за_базу"]} заказов за 14 дней, оборот '
                f'{m["оборот_₽_в_день"]:,.0f} ₽/день')
    if m['нулевых_%'] > 90:
        return ('V11_BASELINE', 'fail',
                f'{m["нулевых_%"]} % сегмента не продавались ни разу за базу — дисперсионная '
                f'оценка на такой единице бессмысленна; метрика должна считаться на когорте')
    return ('V11_BASELINE', 'ok',
            f'baseline: {m["среднее_₽_в_день"]} ₽/SKU в день, нулевых {m["нулевых_%"]} %')


def v12_мощность(h, ctx):
    m = h.get('_мощность')
    if not m:
        return ('V12_POWER', 'warn', 'мощность не считалась: сегмент не на уровне SKU')
    д = m.get('MDE_отн')
    if д is None:
        return ('V12_POWER', 'fail', f'выборки {m["n"]} не хватает даже для оценки MDE')
    порог_ok, порог_warn = (0.35, 0.70) if m['режим'] == 'счётная' else (0.30, 0.50)
    хвост = (f'MDE {д} ({m["единица_MDE"]}), режим {m["режим"]}, n={m["n"]}'
             + (f', λ={m["λ_на_плечо"]} заказов на плечо' if m['режим'] == 'счётная' else ''))
    if д <= порог_ok:
        return ('V12_POWER', 'ok', хвост)
    if д <= порог_warn:
        return ('V12_POWER', 'warn', хвост + ' — увидим только крупный эффект')
    return ('V12_POWER', 'fail', хвост + ' — эксперимент прочитать не удастся')


def v13_откат(h, ctx):
    if h['proposed_lever'] in ('наблюдение',):
        return ('V13_ROLLBACK', 'ok', 'вмешательства нет — откат не нужен')
    if h['proposed_lever'] in ('ставка', 'состав рекламы', 'цена', 'бюджет'):
        return ('V13_ROLLBACK', 'ok',
                'откат = возврат снятого снимка состояния; проверено на E7 (возврат конвертеров)')
    return ('V13_ROLLBACK', 'fail', f'для рычага «{h["proposed_lever"]}» откат не описан')


def v14_снимок(h, ctx):
    if h['proposed_lever'] == 'наблюдение':
        return ('V14_SNAPSHOT', 'ok', 'состояние не меняется')
    return ('V14_SNAPSHOT', 'ok',
            'контракт обязывает записать снимок ozon_bids/состава кампаний в '
            'docs/experiments/cohorts до отправки; без файла снимка ACT не разрешается')


def v15_факт_воздействия(h, ctx):
    if h['proposed_lever'] == 'наблюдение':
        return ('V15_APPLIED_FACT', 'ok', 'воздействия нет')
    return ('V15_APPLIED_FACT', 'ok',
            'фиксируется фактически применённое, а не запланированное: applied/applied_at/'
            'api_response в mkt_ozon_bid_journal; строки с applied=false в анализ не входят')


def v16_история(h, ctx):
    return ('V16_HISTORY', 'ok',
            f'переходы статусов пишутся в {os.path.basename(LOG)} только добавлением')


def v17_не_повтор(h, ctx):
    """Отдельная проверка сверх шестнадцати: не предлагаем то, что уже отвергнуто.

    E1 провалился не размером, а конструкцией: массовый подъём ставок вглубь БЕЗ контроля
    и без способа прочитать результат. Тот же размер с рандомизированной половиной —
    другой дизайн, и запрещать его значит запрещать единственный способ узнать правду."""
    n = (h.get('target_segment') or {}).get('всего') or 0
    рандом = 'рандомизирован' in (h.get('_контроль') or '')
    if h['proposed_lever'] == 'ставка' and h.get('_направление') != 'вниз' and n > E1_ПОРОГ:
        if not рандом:
            return ('V17_NOT_REPEAT', 'fail',
                    f'подъём ставок на {n} SKU без рандомизированного контроля буквально '
                    f'повторяет E1: результат был отрицательным, показы хвоста упали')
        return ('V17_NOT_REPEAT', 'warn',
                f'{n} SKU — масштаб E1, но с рандомизированной половиной в контроле. '
                f'Обязателен guardrail на показы хвоста кампании: именно они и упали в E1')
    if h['proposed_lever'] == 'ставка' and h.get('_источник') == 'E3':
        return ('V17_NOT_REPEAT', 'fail', 'E3 имеет статус VOID / NEVER APPLY')
    return ('V17_NOT_REPEAT', 'ok', 'не повторяет отвергнутых решений')


def v18_происхождение(h, ctx):
    """Идея без данных не становится планом.

    Формулировку механизма может дать модель — факт, размер и метрику даёт витрина.
    Гипотеза с происхождением LLM_SUGGESTED_WITHOUT_DATA остаётся черновиком до тех пор,
    пока наблюдатель не найдёт отклонение в живых данных."""
    p = h.get('provenance')
    if p not in ПРОИСХОЖДЕНИЕ:
        return 'V18_PROVENANCE', 'fail', f'происхождение не указано или неизвестно: {p}'
    if p == 'LLM_SUGGESTED_WITHOUT_DATA':
        return ('V18_PROVENANCE', 'fail',
                'придумано без данных: до READY не допускается, нужен наблюдатель с фактом')
    if p == 'MANUALLY_DEFINED':
        return ('V18_PROVENANCE', 'warn',
                'поставлено вручную: факт не подтверждён наблюдателем, проверять глазами')
    return 'V18_PROVENANCE', 'ok', f'происхождение {p}'


ВАЛИДАТОРЫ = (v01_пересечение_sku, v02_даты_и_кампании, v03_один_рычаг, v04_контроль,
              v05_единица_решения, v06_свежесть, v07_остаток, v08_экономика,
              v09_допустимость_ставки, v10_общий_бюджет, v11_baseline, v12_мощность,
              v13_откат, v14_снимок, v15_факт_воздействия, v16_история, v17_не_повтор,
              v18_происхождение)


def валидировать(h, ctx, считать_мощность=True):
    if считать_мощность and h.get('_sku') and '_мощность' not in h:
        h['_мощность'] = мощность(h['_sku'], h.get('primary_metric') or '',
                                  дней_теста=h.get('minimum_duration') or 14)
    отчёт = [dict(zip(('код', 'вердикт', 'объяснение'), f(h, ctx))) for f in ВАЛИДАТОРЫ]
    провал = [x for x in отчёт if x['вердикт'] == 'fail']
    h['_валидация'] = отчёт
    h['_блокировано'] = [x['код'] for x in провал]
    h['risk_level'] = ('высокий' if провал else
                       'средний' if any(x['вердикт'] == 'warn' for x in отчёт) else 'низкий')
    return отчёт


# ============================== приоритет =============================================
# Формула предварительная. Требование к ней одно: каждая составляющая объяснима словами,
# иначе ранжирование превращается в мнение с числом.

ВЕСА = {
    'деньги': 0.20, 'доказательства': 0.15, 'сегмент': 0.10, 'скорость': 0.10,
    'дешевизна_теста': 0.05, 'малый_риск': 0.10, 'обратимость': 0.10,
    'измеримость': 0.10, 'нет_пересечений': 0.05, 'переводимость_в_политику': 0.05,
}


def _баллы(h, ctx):
    сег = (h.get('target_segment') or {}).get('всего') or 0
    m = h.get('_мощность') or {}
    вал = {x['код']: x['вердикт'] for x in (h.get('_валидация') or [])}
    b = {}

    деньги = (m.get('среднее_₽_в_день') or 0) * (m.get('n') or 0)
    b['деньги'] = (min(1.0, деньги / 50000.0),
                   f'сегмент оборачивает ≈{деньги:,.0f} ₽/день; 50 000 ₽/день = 1,0'
                   if деньги else 'денежный оборот сегмента не измерен или нулевой')
    сила = {'ok': 1.0, 'warn': 0.6, 'fail': 0.2}
    b['доказательства'] = (сила.get(вал.get('V11_BASELINE'), 0.5),
                           f'база наблюдения: {h["current_evidence"][:90]}')
    b['сегмент'] = (min(1.0, сег / 1000.0), f'{сег} единиц в сегменте; 1000 = 1,0')
    b['скорость'] = (max(0.0, 1.0 - (h['minimum_duration'] or 28) / 28.0),
                     f'{h["minimum_duration"]} дней до результата; 28 дней = 0,0')
    b['дешевизна_теста'] = (1.0 if h['proposed_lever'] == 'наблюдение' else 0.6,
                            'наблюдение бесплатно; вмешательство стоит части бюджета'
                            if h['proposed_lever'] == 'наблюдение'
                            else 'тест расходует рекламный бюджет на половине сегмента')
    b['малый_риск'] = ({'низкий': 1.0, 'средний': 0.6, 'высокий': 0.2}[h['risk_level']],
                       f'уровень риска {h["risk_level"]} по итогам валидации')
    b['обратимость'] = (1.0 if вал.get('V13_ROLLBACK') == 'ok' else 0.0,
                        'откат описан' if вал.get('V13_ROLLBACK') == 'ok' else 'отката нет')
    изм = 1.0 if вал.get('V12_POWER') == 'ok' else 0.5 if вал.get('V12_POWER') == 'warn' else 0.0
    b['измеримость'] = (изм, {1.0: 'MDE в пределах половины среднего',
                              0.5: 'мощность не считается на этой единице',
                              0.0: 'выборки не хватит, чтобы увидеть эффект'}[изм])
    пер = 1.0 if вал.get('V01_SKU_INTERSECT') == 'ok' else 0.0
    b['нет_пересечений'] = (пер, 'состав свободен' if пер else 'состав пересекается с E5–E8/H-001')
    b['переводимость_в_политику'] = (
        1.0 if h['proposed_lever'] in ('состав рекламы', 'ставка') else 0.5,
        'результат превращается в правило еженедельного плана'
        if h['proposed_lever'] in ('состав рекламы', 'ставка')
        else 'результат останется разовым решением')
    return b


def приоритет(h, ctx):
    b = _баллы(h, ctx)
    score = sum(ВЕСА[k] * v[0] for k, v in b.items())
    h['priority_score'] = round(score, 3)
    h['_объяснение_баллов'] = {k: {'балл': round(v[0], 2), 'вес': ВЕСА[k], 'почему': v[1]}
                               for k, v in b.items()}
    h['confidence'] = round(b['доказательства'][0] * b['измеримость'][0], 2)
    m0 = h.get('_мощность') or {}
    if m0.get('режим') == 'счётная':
        h['required_sample'] = (
            f'≥{m0.get("нужно_заказов_на_плечо")} заказов на плечо за окно теста; сейчас '
            f'{m0.get("λ_на_плечо")} — не хватает в {m0.get("во_сколько_не_хватает")} раза')
    elif m0.get('режим') == 'дисперсионная':
        h['required_sample'] = (
            f'≥{m0.get("нужно_sku")} SKU при текущем разбросе; сейчас {m0.get("n")} — '
            f'не хватает в {m0.get("во_сколько_не_хватает")} раза')
    else:
        h['required_sample'] = 'единица анализа не SKU: выборка определяется контрактом'
    m = h.get('_мощность') or {}
    # Оборот сегмента в день. В счётном режиме он лежит готовым, в дисперсионном —
    # собирается из среднего по SKU. Ноль здесь не «эффекта нет», а «единица анализа
    # не SKU», и врать нулём нельзя: гипотеза без денежной шкалы должна так и говорить.
    оборот = m.get('оборот_₽_в_день')
    if оборот is None:
        оборот = (m.get('среднее_₽_в_день') or 0) * (m.get('n') or 0)
    if not h['estimated_upside_rub'] and оборот:
        h['estimated_upside_rub'] = round(оборот * 0.05 * (h['minimum_duration'] or 14))
        h['estimated_downside_rub'] = -round(оборот * 0.02 * (h['minimum_duration'] or 14))
        h['expected_business_effect'] = (
            f'±5 % оборота сегмента ({оборот:,.0f} ₽/день) за {h["minimum_duration"]} дней; '
            f'оценка предварительная, на вспомогательной экономике')
    elif not h['estimated_upside_rub']:
        h['estimated_upside_rub'] = h['estimated_downside_rub'] = None
        h['expected_business_effect'] = (
            'деньги напрямую не оцениваются: единица анализа не SKU. Выигрыш — качество '
            'решений и снятая слепота, а не оборот сегмента')
    эк = ('канон' if h['proposed_lever'] == 'наблюдение' or not h.get('_флаг_экономики')
          else 'грубее')
    ур, почему = уровень_действия(
        h['proposed_lever'], (h.get('target_segment') or {}).get('всего') or 0,
        обратимо=True, есть_контроль=bool(h.get('_контроль')), экономика=эк, откат_ок=True)
    if h['proposed_lever'] == 'наблюдение':
        ур, почему = 'AUTONOMOUS_READ', 'только чтение и расчёт'
    h['approval_level'] = ур
    h['_почему_уровень'] = почему
    return h


def сузить(h, ctx, предел=МАССОВЫЙ_ПОРОГ):
    """Система не отменяет гипотезу из-за размера — она перестраивает дизайн и говорит, как.

    Перестройка идёт по трём правилам подряд: убрать занятых чужими экспериментами,
    убрать нулевой остаток (реклама в пустоту), оставить первых по силе наблюдения."""
    sku = h.get('_sku') or []
    if not sku:
        return h, None
    шаги, было = [], len(sku)
    занятых = [x for x in sku if x in ctx['занято']]
    if занятых:
        sku = [x for x in sku if x not in ctx['занято']]
        шаги.append(f'{len(занятых)} занятых E5–E8/H-001')
    без_остатка = [x for x in sku if x not in ctx['остаток']]
    if без_остатка and len(без_остатка) < len(sku):
        sku = [x for x in sku if x in ctx['остаток']]
        шаги.append(f'{len(без_остатка)} без остатка')
    рандом = 'рандомизирован' in (h.get('_контроль') or '')
    if len(sku) > предел and not рандом:
        набл = h.get('_наблюдение') or {}
        порядок = [x['sku'] for x in (набл.get('подробности') or {}).get('топ', [])]
        отсорт = [x for x in порядок if x in sku] + [x for x in sku if x not in порядок]
        шаги.append(f'{len(sku) - предел} сверх порога {предел} (шире — повтор E1)')
        sku = отсорт[:предел]
    if рандом and len(sku) > E1_ПОРОГ and h['proposed_lever'] == 'ставка':
        h['counter_evidence'] = (h.get('counter_evidence') or []) + [
            'E1: массовый подъём ставок вглубь дал отрицательный результат — показы хвоста '
            'упали примерно у 6700 SKU из-за общего бюджета кампании']
        h['guardrails'] = h['guardrails'] + [
            'показы хвоста кампании не падают более чем на 10 % — это и был провал E1']
    if not шаги:
        return h, None
    h['_sku'] = sku
    h['_пересечение'] = занятых
    if занятых:
        мешают = sorted({e for e, x in ctx['по_экспериментам'].items() if set(занятых) & x})
        h['dependencies'] = [
            f'{e}: состав освободится после сверки {ctx["даты"].get(e, {}).get("сверка") or "—"}'
            for e in мешают]
    h['target_segment'] = dict(h['target_segment'], всего=len(sku), исходный_размер=было,
                               отбор='; '.join(шаги))
    h.pop('_мощность', None)
    return h, f'сегмент перестроен {было} → {len(sku)}: убраны ' + '; '.join(шаги)


def контракт(h, ctx):
    """Предварительный экспериментальный контракт. Ничего не применяет."""
    старт = (_d(TODAY) + dt.timedelta(days=1)).isoformat()
    конец = (_d(старт) + dt.timedelta(days=h['minimum_duration'] or 14)).isoformat()
    sku = h.get('_sku') or []
    половина = len(sku) // 2
    бессильна = недостаточная_мощность(h)
    return {
        'hypothesis_id': h['hypothesis_id'],
        'статус': (f'ПРОЕКТ ДИЗАЙНА — {BLOCK_POWER}, ИСПОЛНЕНИЮ НЕ ПОДЛЕЖИТ' if бессильна
                   else 'ЧЕРНОВИК — НЕ ПРИМЕНЁН'),
        'исполнимый': not бессильна,
        'blocking_reason': BLOCK_POWER if бессильна else h.get('blocking_reason'),
        'пути_разблокировки': list(ПУТИ_РАЗБЛОКИРОВКИ) if бессильна else [],
        'аккаунт': ACC, 'рычаг': h['proposed_lever'],
        'единица_рандомизации': (h['target_segment'] or {}).get('единица'),
        'состав': {'всего': len(sku) or (h['target_segment'] or {}).get('всего'),
                   'treatment': половина, 'control': len(sku) - половина,
                   'правило': 'случайное деление пополам с фиксацией файла состава до отправки'},
        'контроль': h.get('_контроль'),
        'окно': {'старт': старт, 'конец': конец, 'сверка': конец},
        'primary_metric': h['primary_metric'], 'secondary': h['secondary_metrics'],
        'guardrails': h['guardrails'],
        'мощность': h.get('_мощность'),
        'откат': 'возврат снимка состояния, снятого до отправки',
        'снимок_до': f'{COHORTS}/{h["hypothesis_id"]}_snapshot_{TODAY}.csv (не создан: shadow)',
        'уровень_допуска': (f'НЕ ПРИМЕНИМО: {BLOCK_POWER} — разрешение не запрашивается'
                            if бессильна else h['approval_level']),
        'экономика': h.get('_флаг_экономики') or 'канон BI применим',
        'предупреждения': [x for x in h['_валидация'] if x['вердикт'] == 'warn'],
        'блокировки': [x for x in h['_валидация'] if x['вердикт'] == 'fail'],
        'что_не_делается': ['внешние вызовы отсутствуют', 'состав E5–E8 и H-001 не трогается',
                            'цены не меняются'],
    }


# ============================== волновой режим ========================================
# 1 458 SKU не помещаются в один эксперимент — но это не повод делать одно массовое
# вмешательство: ровно так был построен E1, и ровно поэтому он кончился откатом.
# Волна — это контейнер риска: небольшой состав, свой контроль, денежный потолок и одно
# из трёх решений на выходе. Следующая волна открывается только результатом предыдущей.
# Гипотеза при этом остаётся одна и охватывает весь сегмент.

ВОЛНА_МАКС = 200        # SKU в волне: выше начинается FOUNDER_APPROVAL и повтор E1
ВОЛНА_МИН = 100         # ниже волна не окупает накладных расходов на измерение
СТРАТ = 4               # страты по силе спроса
РИСК_ВОЛНЫ_РУБ = 15000  # потолок расхода на волну; достигнут раньше окна — STOP
WAVES = BASE + '/docs/experiments/waves'


def _жребий(sku, соль):
    """Детерминированная жеребьёвка: воспроизводится от состава, а не от времени.

    Случайность через now() или random() невоспроизводима, а значит недоказуема:
    через месяц никто не сможет подтвердить, что состав не подбирали под результат."""
    return int(hashlib.sha1(f'{соль}:{sku}'.encode()).hexdigest()[:8], 16)


def стратифицировать(sku, веса, страт=СТРАТ):
    """Страты по силе спроса. Без них жребий на маленькой волне легко уводит весь
    спрос сегмента в одно плечо, и разница плеч оказывается разницей составов."""
    порядок = sorted(sku, key=lambda x: (-(веса.get(x) or 0.0), x))
    if not порядок:
        return []
    k = max(1, len(порядок) // страт)
    слои = [порядок[i:i + k] for i in range(0, k * страт, k)] or [порядок]
    for i, x in enumerate(порядок[k * страт:]):
        слои[i % len(слои)].append(x)
    return [x for x in слои if x]


def волны(h, ctx, размер=ВОЛНА_МАКС, риск=РИСК_ВОЛНЫ_РУБ, горизонт=False):
    """Одна гипотеза — несколько связанных волн-экспериментов.

    Волна ограничивает деньги, а не измерение: на 200 SKU хвоста статистики не хватит
    никогда. Поэтому решение KEEP принимается на накопленной по волнам когорте, а сама
    волна отвечает только за два быстрых исхода — STOP по деньгам и ROLLBACK по вреду."""
    бессильна = недостаточная_мощность(h)
    свободные = list(h.get('_sku') or [])
    ждут = list(h.get('_пересечение') or [])
    веса = ((h.get('_наблюдение') or {}).get('веса')) or {}
    слои = стратифицировать(свободные, веса)
    дней = h.get('minimum_duration') or 14
    m = h.get('_мощность') or {}
    n = m.get('n') or len(свободные) or 1
    на_sku = float(m.get('λ_на_плечо') or 0) / (n / 2.0) if n else 0.0
    нужно_заказов = m.get('нужно_заказов_на_плечо') or 128
    λ_волны = на_sku * (размер / 2.0)
    волн_нужно = math.ceil(нужно_заказов / λ_волны) if λ_волны > 0 else None
    волн_доступно = math.ceil(len(свободные) / размер) if свободные else 0

    def нарезать(слои_, сколько, сдвиг, занятая):
        """Нарезать пул на волны. `занятая` — состав ещё держат идущие эксперименты:
        такая волна планируется, но открыться может только после их сверки."""
        очередь = [list(x) for x in слои_]
        куски = []
        for k in range(сколько):
            i = сдвиг + k
            wid = f'{h["hypothesis_id"]}/W{i + 1}'
            состав, на_слой = [], max(1, размер // max(1, len(очередь)))
            for j, слой in enumerate(очередь):
                берём = слой[:на_слой]
                del слой[:на_слой]
                состав += [(x, j) for x in берём]
            if not состав:
                break
            воздействие = [(x, j) for x, j in состав if _жребий(x, wid) % 2 == 0]
            контроль = [(x, j) for x, j in состав if _жребий(x, wid) % 2 == 1]
            старт = (_d(TODAY) + dt.timedelta(days=1 + i * (дней + 1))).isoformat()
            сверка = (_d(старт) + dt.timedelta(days=дней)).isoformat()
            по_стратам = {f'страта {j + 1}': {
                'воздействие': sum(1 for _, s2 in воздействие if s2 == j),
                'контроль': sum(1 for _, s2 in контроль if s2 == j)}
                for j in range(len(очередь))}
            держат = '; '.join(h.get('dependencies') or []) or 'идущие эксперименты'
            куски.append({
                'wave_id': wid, 'hypothesis_id': h['hypothesis_id'],
                'статус': (f'ПРОЕКТ ДИЗАЙНА — {BLOCK_POWER}' if бессильна else
                           'ЖДЁТ СОГЛАСОВАНИЯ' if i == 0 else
                           'ЗАКРЫТА ЗАНЯТЫМ СОСТАВОМ' if занятая else
                           'ЗАКРЫТА ПРЕДЫДУЩЕЙ ВОЛНОЙ'),
                'исполнимый': not бессильна,
                'предусловие': (
                    f'{BLOCK_POWER}: {почему_бессильна(h)}; волна не может быть согласована '
                    f'или запущена, пока мощность не появится' if бессильна else
                    'нет: первая волна' if i == 0 else
                    f'{h["hypothesis_id"]}/W{i} в EVALUATED и решение не ROLLBACK, '
                    f'плюс состав освобождён: {держат}' if занятая else
                    f'{h["hypothesis_id"]}/W{i} в EVALUATED и решение не ROLLBACK'),
                'состав': {'всего': len(состав), 'воздействие': len(воздействие),
                           'контроль': len(контроль), 'страты': по_стратам,
                           'состав_занят_сейчас': занятая},
                'рандомизация': 'стратифицированная; жребий sha1(wave_id:sku), воспроизводим',
                'окно': {'старт': старт, 'сверка': сверка, 'дней': дней},
                'риск': {'потолок_расхода_₽': риск, 'дневной_лимит_₽': round(риск / дней),
                         'при_достижении': 'STOP: показ волны выключается до конца окна'},
                'решение': {
                    'KEEP': 'только на накопленной когорте всех прошедших волн: лифт заказов '
                            f'значим при α=0,05 и накоплено ≥{нужно_заказов} заказов на плечо',
                    'STOP': f'расход волны достиг {риск:,.0f} ₽ или ДРР волны выше планового '
                            'по кампании — волна гасится, состав остаётся в когорте',
                    'ROLLBACK': 'показы хвоста кампании упали более чем на 10 % (провал E1) '
                                'или маржа сегмента после рекламы отрицательна'},
                'состав_файл': f'{WAVES}/{h["hypothesis_id"].replace("/", "_")}_W{i + 1}.csv',
                'уровень_допуска': (f'НЕ ПРИМЕНИМО: {BLOCK_POWER}' if бессильна
                                    else h.get('approval_level')),
                'применено': False, 'внешних_вызовов': 0,
                '_состав': {'воздействие': воздействие, 'контроль': контроль},
            })
        return куски

    план = нарезать(слои, волн_доступно, 0, False)
    if горизонт and ждут:
        план += нарезать(стратифицировать(ждут, веса),
                         math.ceil(len(ждут) / размер), len(план), True)
    сводка = {
        'гипотеза': h['hypothesis_id'], 'сегмент_всего': len(свободные) + len(ждут),
        'свободно_сейчас': len(свободные), 'ждут_освобождения': len(ждут),
        'кто_держит': h.get('dependencies') or [],
        'размер_волны': размер, 'волн_доступно': волн_доступно,
        'волн_в_плане': len(план),
        'горизонт': ('весь сегмент, включая занятый состав' if горизонт
                     else 'только свободный состав'),
        'заказов_на_плечо_в_волне': round(λ_волны, 1),
        'нужно_заказов_на_плечо': нужно_заказов, 'волн_до_мощности': волн_нужно,
        'статус_плана': (f'ПРОЕКТ ДИЗАЙНА — {BLOCK_POWER}, ни одна волна не исполнима'
                         if бессильна else 'волны исполнимы после разрешения'),
        'вердикт': ('накопленной когорты хватит: '
                    f'{волн_нужно} волн из {волн_доступно} доступных'
                    if not бессильна and волн_нужно and волн_доступно
                    and волн_нужно <= волн_доступно else
                    f'{BLOCK_POWER}: {почему_бессильна(h)}; даже все волны дают '
                    f'{round(λ_волны * len(план), 1) if план else 0} заказов на плечо при '
                    f'необходимых {нужно_заказов}. Дробление на волны мощности не создаёт'),
        'пути_разблокировки': list(ПУТИ_РАЗБЛОКИРОВКИ) if бессильна else [],
        'разрешение_основателя': ('не требуется и не запрашивается: волны не исполнимы'
                                  if бессильна else 'требуется на первую волну'),
        'общий_риск_₽': риск * len(план),
        'риск_на_волну_₽': риск,
    }
    return {'сводка': сводка, 'волны': план}


def записать_волны(res):
    """Состав волн фиксируется файлом до любого согласования — иначе его можно подобрать
    под результат задним числом."""
    os.makedirs(WAVES, exist_ok=True)
    файлы = []
    for w in res['волны']:
        строки = ['sku,arm,stratum']
        for плечо, ключ in (('treatment', 'воздействие'), ('control', 'контроль')):
            for sku, j in w['_состав'][ключ]:
                строки.append(f'{sku},{плечо},{j + 1}')
        open(w['состав_файл'], 'w', encoding='utf-8').write('\n'.join(строки) + '\n')
        файлы.append(w['состав_файл'])
    return файлы


# ============================== посев: прошлые гипотезы как тест системы ===============
# Система обязана уметь принять уже прожитое: E1 (отвергнут), E3 (VOID), E5–E8 (идут),
# H-001 (наблюдательная). Если реестр не выдерживает их, он не выдержит и новых.

ПОСЕВ = [
    dict(hid='E1', путь=('DRAFT', 'VALIDATED', 'READY', 'APPROVAL_REQUIRED', 'RUNNING',
                         'MEASURING', 'EVALUATED', 'ROLLBACK'),
         problem='Ставки хвоста считались заниженными.',
         mechanism='Разгон ставок вглубь каталога купит показы и заказы.',
         lever='ставка', unit='SKU', metric='выручка на SKU-день',
         evidence='до разгона хвост показывался мало',
         result='отрицательный: показы хвоста упали, разгон вглубь убыточен',
         decision='ROLLBACK', learning='Общий бюджет кампании делает разгон вглубь игрой '
                  'с отрицательной суммой: подъём ставок отключил показы примерно у 6700 SKU '
                  'хвоста. Платим за ширину показа, а не за глубину ставки.',
         next_h=['H-004 (разделение бюджета)', 'H-002 (ширина показа вместо глубины)']),
    dict(hid='E3', путь=('DRAFT', 'VALIDATED', 'READY', 'REJECTED'),
         problem='Недельный план ставок ±10 % от 17.08.',
         mechanism='Регулярный шаг ставки по марже приблизит ставки к оптимуму.',
         lever='ставка', unit='SKU', metric='маржа после рекламы',
         evidence='план построен на вспомогательной экономике',
         result='VOID / NEVER APPLY',
         decision='REJECTED', learning='План опирался на экономику и остатки, которым нельзя '
                  'доверять на единице SKU. Перестраивать только после починки экономики '
                  'и остатков; применять в текущем виде нельзя никогда.',
         next_h=['H-009 (сверка применённых решений с фактом)']),
    dict(hid='E5', путь=('DRAFT', 'VALIDATED', 'READY', 'APPROVAL_REQUIRED', 'RUNNING',
                         'MEASURING'),
         problem='Ядро недополучает показы при доступном потолке ставки.',
         mechanism='Разгон ядра по потолку.', lever='ставка', unit='SKU',
         metric='выручка на SKU-день', evidence='ядро конвертирует лучше хвоста',
         result=None, decision=None, learning=None, next_h=[]),
    dict(hid='E6', путь=('DRAFT', 'VALIDATED', 'READY', 'APPROVAL_REQUIRED', 'RUNNING',
                         'MEASURING'),
         problem='На acc2 часть рекламы неэффективна.',
         mechanism='Снятие неэффективных и понижение ставок.', lever='ставка', unit='SKU',
         metric='ДРР', evidence='расход без заказов', result=None, decision=None,
         learning=None, next_h=[]),
    dict(hid='E7', путь=('DRAFT', 'VALIDATED', 'READY', 'APPROVAL_REQUIRED', 'RUNNING',
                         'MEASURING'),
         problem='Часть конвертеров откачена ошибочно.',
         mechanism='Возврат ошибочно откаченных конвертеров.', lever='состав рекламы',
         unit='SKU', metric='заказы на SKU-день', evidence='откат затронул конвертеры',
         result=None, decision=None, learning=None, next_h=[]),
    dict(hid='E8', путь=('DRAFT', 'VALIDATED', 'READY', 'APPROVAL_REQUIRED', 'RUNNING',
                         'MEASURING'),
         problem='Снятая волна 1 могла нести не только собственные продажи.',
         mechanism='Возврат случайной половины снятой когорты волны 1 (A/B).',
         lever='состав рекламы', unit='SKU', metric='заказы на SKU-день (ITT)',
         evidence='рандомизация половины даёт единственный причинный контраст',
         result=None, decision=None, learning=None, next_h=['H-001']),
    dict(hid='H-001', путь=('DRAFT', 'VALIDATED', 'READY', 'APPROVAL_REQUIRED', 'RUNNING',
                            'MEASURING'),
         problem='Снятие дешёвого хвоста могло уронить продажи товаров, которые рекламу '
                 'не теряли.',
         mechanism='Ореол: показ хвоста приводил покупателя в карточки соседей по кластеру '
                   'и в аккаунт целиком.',
         lever='наблюдение', unit='SKU', metric='заказы на 100 доступных SKU-дней',
         evidence='наблюдательное исследование P1-HALO, режим C с 20.08',
         result=None, decision=None, learning=None,
         next_h=['CLUSTER_SPILLOVER (дизайн готов, вердикт запрещён до накопления)']),
]


def посеять():
    r = читать_реестр()
    есть = {h['hypothesis_id'] for h in r['гипотезы']}
    новые = 0
    for p in ПОСЕВ:
        if p['hid'] in есть:
            continue
        h = пустая(p['hid'], business_problem=p['problem'], mechanism=p['mechanism'],
                   proposed_lever=p['lever'], primary_metric=p['metric'],
                   current_evidence=p['evidence'],
                   target_segment={'единица': p['unit']},
                   source_observation='исторический реестр экспериментов Ozon',
                   provenance=('DERIVED_FROM_FAILED_EXPERIMENT'
                               if p['decision'] in ('ROLLBACK', 'REJECTED')
                               else 'SEEDED_FROM_HISTORY'),
                   experiment_id=p['hid'] if p['hid'].startswith('E') else 'P1-HALO',
                   result=p['result'], decision=p['decision'], learning=p['learning'],
                   next_hypotheses=p['next_h'])
        for стало in p['путь']:
            перевести(h, стало, 'посев исторического состояния', кто='посев')
        r['гипотезы'].append(h)
        новые += 1
    писать_реестр(r)
    return новые


ЗАПРЕТЫ = {
    'E1': 'не предлагать массовый разгон ставок вглубь: результат отрицательный',
    'E3': 'VOID / NEVER APPLY — не применять ни в каком виде',
    'H-001': 'ореол не доказан причинно; до 7 полных дней режима C — только накопление',
}


# ============================== прогон ================================================
def прогон(предел=МАССОВЫЙ_ПОРОГ):
    ctx = контекст()
    набл = собрать_наблюдения()
    сырые = [x for x in (предложить(o, ctx) for o in набл['наблюдения']) if x]
    гип, дубли = дедуп(сырые)
    перестройки = []
    for h in гип:
        _, note = сузить(h, ctx, предел)
        if note:
            перестройки.append({'id': h['hypothesis_id'], 'что': note})
        валидировать(h, ctx)
        приоритет(h, ctx)
    гип.sort(key=lambda h: -h['priority_score'])
    р = читать_реестр()
    было = {x['hypothesis_id']: x['status'] for x in р['гипотезы']}
    for h in гип:
        продвинуть(h, было.get(h['hypothesis_id'], 'OBSERVED'))
    return {'ctx': ctx, 'наблюдения': набл, 'гипотезы': гип, 'дубли': дубли,
            'перестройки': перестройки}


ЛИНИЯ = ('OBSERVED', 'DRAFT', 'VALIDATED', 'READY', 'APPROVAL_REQUIRED')


def продвинуть(h, текущий='OBSERVED'):
    """Статус — следствие проверок, а не подпись.

    Прогон идемпотентен: гипотеза доводится ровно до того узла, который заслужила
    сегодняшними проверками. Если вчера она была готова, а сегодня проверка перестала
    проходить, статус едет назад — по разрешённым автоматом переходам, а не правкой поля."""
    h['status'] = текущий
    if недостаточная_мощность(h):
        h['blocking_reason'] = BLOCK_POWER
        h['result'] = (f'НЕ ЗАПУЩЕНА: {BLOCK_POWER} — {почему_бессильна(h)}; дробление '
                       f'на волны мощности не создаёт. Пути разблокировки: '
                       + '; '.join(ПУТИ_РАЗБЛОКИРОВКИ))
        цель = 'DRAFT' if h['_блокировано'] else 'VALIDATED'
    elif h['_блокировано']:
        h['blocking_reason'] = h['_блокировано'][0]
        h['result'] = 'НЕ ЗАПУЩЕНА: ' + ', '.join(h['_блокировано'])
        цель = 'DRAFT'
    elif h.get('provenance') == 'LLM_SUGGESTED_WITHOUT_DATA':
        h['blocking_reason'] = 'BLOCKED_BY_PROVENANCE'
        h['result'] = 'НЕ ЗАПУЩЕНА: происхождение LLM_SUGGESTED_WITHOUT_DATA'
        цель = 'VALIDATED'          # потолок жёсткий: READY недостижим без факта из данных
    else:
        готова = all([h['primary_metric'], h.get('_контроль'), h['required_sample'],
                      h['minimum_duration']])
        цель = ('APPROVAL_REQUIRED' if готова and h['approval_level'] != 'AUTONOMOUS_READ'
                else 'READY' if готова else 'VALIDATED')
    if текущий not in ЛИНИЯ:          # RUNNING/MEASURING/терминальные ведёт человек
        return h
    i, j = ЛИНИЯ.index(текущий), ЛИНИЯ.index(цель)
    if i == j:
        return h
    шаг = 1 if j > i else -1
    вперёд = {'DRAFT': 'кандидат сформирован наблюдателем',
              'VALIDATED': 'все 17 проверок пройдены',
              'READY': 'метрика, выборка, контроль и длительность определены',
              'APPROVAL_REQUIRED': f'нужен допуск уровня {h["approval_level"]}'}
    назад = 'проверки перестали проходить: ' + ', '.join(h['_блокировано'] or ['—'])
    for k in range(i + шаг, j + шаг, шаг):
        перевести(h, ЛИНИЯ[k], вперёд[ЛИНИЯ[k]] if шаг > 0 else назад)
    return h


def сохранить_кандидатов(гип):
    r = читать_реестр()
    есть = {h['hypothesis_id']: i for i, h in enumerate(r['гипотезы'])}
    for h in гип:
        чистая = {k: v for k, v in h.items() if not k.startswith('_')}
        чистая['_служебное'] = {
            'валидация': h.get('_валидация'), 'мощность': h.get('_мощность'),
            'объяснение_баллов': h.get('_объяснение_баллов'),
            'блокировано': h.get('_блокировано'),
            'флаг_экономики': h.get('_флаг_экономики'),
            'почему_уровень': h.get('_почему_уровень'), 'контроль': h.get('_контроль')}
        if h['hypothesis_id'] in есть:
            # Живой прогон пересчитывает гипотезу заново, но не имеет права стирать то,
            # чего в нём нет: волновой план и решённый исход живут только в реестре.
            старая = r['гипотезы'][есть[h['hypothesis_id']]]
            for k in ('waves', '_волновая_сводка', 'experiment_id', 'decision', 'learning'):
                if старая.get(k) and not чистая.get(k):
                    чистая[k] = старая[k]
            r['гипотезы'][есть[h['hypothesis_id']]] = чистая
        else:
            r['гипотезы'].append(чистая)
            запись_в_журнал(h['hypothesis_id'], 'OBSERVED', 'OBSERVED',
                            'кандидат создан наблюдателем ' + (h['source_observation'] or ''))
    писать_реестр(r)


# ============================== команды ===============================================
def cmd_observe(a):
    r = собрать_наблюдения()
    print(f'окно {r["окно_текущее"][0]}…{r["окно_текущее"][1]} против '
          f'{r["окно_прошлое"][0]}…{r["окно_прошлое"][1]}\n')
    for o in r['наблюдения']:
        print(f'  {o["код"]:<26} {o["заголовок"]}')
        print(f'  {"":<26} {o["факт"]}  [{o["размер_сегмента"]} {o["единица"]}]')
    for s in r['сбои']:
        print(f'  СБОЙ {s["наблюдатель"]}: {s["ошибка"]}')
    return r


def cmd_propose(a):
    p = прогон()
    for h in p['гипотезы']:
        print(f'  {h["hypothesis_id"]}  {h["business_problem"][:80]}')
    print(f'\nдублей отброшено: {len(p["дубли"])}; перестроено дизайнов: {len(p["перестройки"])}')
    if a.save:
        сохранить_кандидатов(p['гипотезы'])
    return p


def cmd_validate(a):
    p = прогон()
    for h in p['гипотезы']:
        плохо = [x for x in h['_валидация'] if x['вердикт'] != 'ok']
        print(f'\n{h["hypothesis_id"]}  риск {h["risk_level"]}  допуск {h["approval_level"]}')
        for x in плохо:
            print(f'   {x["вердикт"].upper():<5} {x["код"]}: {x["объяснение"]}')
    return p


def cmd_prioritize(a):
    p = прогон()
    for i, h in enumerate(p['гипотезы'], 1):
        print(f'{i:>2}. {h["hypothesis_id"]}  балл {h["priority_score"]:.3f}  '
              f'{h["approval_level"]:<18} {h["business_problem"][:60]}')
    return p


def cmd_plan(a):
    p = прогон()
    годные = [h for h in p['гипотезы'] if not h['_блокировано']]
    for h in годные[:a.top]:
        print(json.dumps(контракт(h, p['ctx']), ensure_ascii=False, indent=2)[:1200])
    return p


def cmd_status(a):
    r = читать_реестр()
    print(f'режим контура: {РЕЖИМ}; ACT только после разрешения человека, '
          f'EVALUATE→KEEP/ROLLBACK→MEMORY вручную')
    по_статусу = {}
    for h in r['гипотезы']:
        по_статусу.setdefault(h['status'], []).append(h['hypothesis_id'])
    for s in СТАТУСЫ:
        if s in по_статусу:
            print(f'  {s:<18} {", ".join(sorted(по_статусу[s]))}')
    зб = [(h['hypothesis_id'], h['blocking_reason']) for h in r['гипотезы']
          if h.get('blocking_reason')]
    if зб:
        print('\nзаблокированы программно:')
        for hid, b in sorted(зб):
            print(f'  {hid:<6} {b}')
    реш = [(h['hypothesis_id'], h['decision']) for h in r['гипотезы'] if h.get('decision')]
    if реш:
        print('\nрешения человека:')
        for hid, d in sorted(реш):
            print(f'  {hid:<6} {d}')
    print(f'\nвсего гипотез: {len(r["гипотезы"])}')
    return r


def cmd_evaluate(a):
    r = читать_реестр()
    if a.decide:
        for h in r['гипотезы']:
            if h['hypothesis_id'] != a.decide:
                continue
            if a.as_ == 'REJECTED':
                перевести(h, 'REJECTED', a.reason, кто='человек')
            else:
                запись_в_журнал(h['hypothesis_id'], h['status'], h['status'],
                                f'{a.as_}: {a.reason}', кто='человек')
            h['decision'] = a.as_
            писать_реестр(r)
            print(f'{a.decide}: {a.as_} — {a.reason}')
            return r
        print(f'не найдено: {a.decide}')
        return r
    with open(EXPERIMENTS, encoding='utf-8') as f:
        exp = json.load(f)
    print('что и когда подлежит измерению:')
    for e in exp['эксперименты']:
        if e.get('дата_сверки'):
            print(f'  {e["id"]:<6} сверка {e["дата_сверки"]}  {e["название"][:50]}')
    print(f'  H-001  первый разбор 2026-08-26 (7 полных дней режима C с 20.08)')
    return r


ИТОГОВЫЕ_ВЕРДИКТЫ = ('EFFECT_CONFIRMED_POSITIVE', 'EFFECT_CONFIRMED_NEGATIVE',
                     'INCONCLUSIVE', 'NO_CAUSAL_CLAIM', 'CONTAMINATED')


def cmd_evaluate_ready(a):
    """H-009: сверить всё, чему пришёл срок, и записать вывод. Ничего не применять.

    Автоматически разрешено ровно пять вещей: прочитать данные, проверить зрелость,
    посчитать, дописать оценку в append-only журнал, отметить в памяти, что измерение
    состоялось. KEEP и ROLLBACK остаются рекомендацией и требуют отдельного «да».
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import ozon_eval_core as E

    сегодня = a.today or E.TODAY
    данные = E.ЖивыеДанные(ACC)
    итог = E.прогон(данные, today=сегодня, только=(a.only.split(',') if a.only else None),
                    писать=not a.dry_run)

    print(f'evaluate-ready · {сегодня} · режим {E.РЕЖИМ} · оценщик {E.EVALUATOR_VERSION}'
          + (' · ПРОГОН БЕЗ ЗАПИСИ' if a.dry_run else ''))
    print(f'{"эксп":<16}{"вердикт":<28}{"рекоменд.":<20}{"причин":<7}{"данные":<12}запись')
    for x in итог:
        r = x['запись']
        print(f'{r["experiment_id"]:<16}{r["verdict"]:<28}{r["recommendation"]:<20}'
              f'{len(r["reason_codes"]):<7}{str(r["data_as_of"] or "—"):<12}{x["статус_записи"]}')

    # память теневого режима: фиксируем ТОЛЬКО факт состоявшегося измерения
    if not a.dry_run:
        r = читать_реестр()
        сдвинуто = []
        for x in итог:
            зап = x['запись']
            if x['статус_записи'] != 'добавлено' or зап['verdict'] not in ИТОГОВЫЕ_ВЕРДИКТЫ:
                continue
            for h in r['гипотезы']:
                if h['hypothesis_id'] != зап['hypothesis_id'] or h['status'] != 'MEASURING':
                    continue
                перевести(h, 'EVALUATED',
                          f'оценка {зап["evaluation_id"]}: {зап["verdict"]} '
                          f'(рекомендация {зап["recommendation"]}, не исполнена)')
                сдвинуто.append(h['hypothesis_id'])
        if сдвинуто:
            писать_реестр(r)
        print(f'\nпамять: MEASURING→EVALUATED — {", ".join(сдвинуто) if сдвинуто else "нет"}')
    print(f'журнал оценок: {E.ОЦЕНКИ}')
    print('KEEP/ROLLBACK здесь — рекомендация. Решение и исполнение не записаны.')
    return итог


# ============================== карта цикла ===========================================
# Честная оценка того, что уже было. Столбец «разрыв» — не пожелание, а то, что
# приходилось делать руками в каждой сессии заново.

ЦИКЛ = [
    ('OBSERVE', 'частично',
     'ozon_search_summary, ozon_sku_scan, ozon_cannibal, ежедневные витрины',
     'наблюдения нигде не сохранялись: каждая сессия начинала осмотр с нуля',
     '11 наблюдателей в одной точке входа, факт + размер сегмента + рычаг'),
    ('DIAGNOSE', 'частично', 'разборы внутри сессий, отчёты P0',
     'диагноз жил в переписке и умирал вместе с сессией',
     'наблюдение несёт готовый диагноз машиночитаемо'),
    ('HYPOTHESIZE', 'отсутствовал', 'гипотезы существовали только текстом в брифе',
     'гипотезу нельзя было ни найти, ни сверить, ни запретить',
     'реестр из 30 полей; гипотеза живёт до эксперимента и после него'),
    ('PRIORITIZE', 'отсутствовал', '—',
     'очередь задавалась разговором; дорогое и дешёвое не разделялись',
     'балл из 10 составляющих, каждая с объяснением словами'),
    ('DESIGN', 'частично', 'ozon_experiments.json (E5–E8 заполнялись руками)',
     'контракт писался руками, поля забывались',
     'контракт генерируется из гипотезы и валидации'),
    ('VALIDATE', 'отсутствовал', '—',
     'ошибки ловились постфактум: E1 запущен без контроля, E3 — на негодной экономике',
     '17 проверок до запуска; провал = конкретная причина и перестройка дизайна'),
    ('APPROVE', 'частично', 'устное согласование',
     'решение не хранилось: через месяц не восстановить, что и почему разрешили',
     'уровень допуска считается автоматически; решение пишется в append-only журнал'),
    ('ACT', 'есть', 'ozon_e7b_restore, ozon_weekly_bids, ozon_wave1_restore/delete',
     'каждый инструмент со своим составом, вне общего контура',
     'сегодня НЕ закрывается: внешних записей в этой сессии нет'),
    ('MEASURE', 'есть', 'ozon_exp_eval, ozon_halo_eval (ITT, зрелость, RVI)',
     'проверка зрелости данных жила только в halo-инструменте',
     'зрелость вынесена в валидатор V06 и применяется ко всем гипотезам'),
    ('EVALUATE', 'частично', 'ozon_exp_eval по датам сверки',
     'сверка запускалась по памяти, календарь вёлся вручную',
     'evaluate печатает, что и когда подлежит измерению'),
    ('KEEP/ROLLBACK', 'частично', 'mkt_ozon_bid_journal (outcome, m_after)',
     '7156 применённых решений не сверены с фактом — прогноз ничем не ограничен',
     'решение человека фиксируется командой evaluate --decide'),
    ('MEMORY', 'частично', 'бриф, docs/experiments, memory-файлы',
     'знание E1 хранилось прозой: система могла предложить E1 второй раз',
     'learning + ЗАПРЕТЫ + валидатор V17 делают запрет исполняемым'),
]


def cmd_report(a):
    p = прогон()
    гип, ctx = p['гипотезы'], p['ctx']
    годные = [h for h in гип if not h['_блокировано']]
    L = []
    ад = L.append
    ад(f'# Контур гипотез Ozon — рабочий прототип, shadow mode\n')
    ад(f'Дата прогона {TODAY}. Аккаунт `{ACC}`. Все данные — только SELECT; внешних вызовов '
       f'нет ни одного; ничего не применено.\n')
    ад(f'\n## 0. Рамка контура — `{РЕЖИМ}`\n')
    for i, ф in enumerate(ФИКСАЦИИ, 1):
        ад(f'{i}. {ф}')
    ад('')
    ад(f'Витрина рекламы зрела по {ctx["зрелый_день"]}, окно наблюдения '
       f'{p["наблюдения"]["окно_текущее"][0]}…{p["наблюдения"]["окно_текущее"][1]} против '
       f'{p["наблюдения"]["окно_прошлое"][0]}…{p["наблюдения"]["окно_прошлое"][1]}.\n')

    ад('\n## 1. Карта цикла и разрывы\n')
    ад('| Стадия | Было | Чем закрывалась | Разрыв: что делалось руками | Что закрывает сейчас |')
    ад('|---|---|---|---|---|')
    for st, было, чем, разрыв, чем2 in ЦИКЛ:
        ад(f'| {st} | {было} | {чем} | {разрыв} | {чем2} |')

    ад('\n## 2. Схема реестра гипотез\n')
    ад(f'Файл `{os.path.relpath(REGISTRY, BASE)}`, история переходов — '
       f'`{os.path.relpath(LOG, BASE)}` (только добавление).\n')
    ад('Поля: ' + ', '.join(f'`{f}`' for f in ПОЛЯ) + '.\n')
    ад('Ключевое отличие от `ozon_experiments.json`: там хранился дизайн вмешательства, '
       'здесь — гипотеза. Она появляется из наблюдения до эксперимента и продолжает жить '
       'после него, неся `result`, `decision`, `learning` и `next_hypotheses`. Именно это '
       'делает запрет на повтор E1 исполняемым, а не декларативным.\n')

    ад('\n## 3. Автомат статусов\n')
    ад('| Из | Куда разрешено |')
    ад('|---|---|')
    for a1, b1 in ПЕРЕХОДЫ.items():
        ад(f'| `{a1}` | {", ".join(f"`{x}`" for x in b1) or "— (терминальный)"} |')
    ад('\nПереход проверяется программно (`проверить_переход`), недопустимый бросает '
       '`ПереходЗапрещён`. Смысл трёх узлов:\n')
    for k, v in ПОЧЕМУ_ТАК.items():
        ад(f'- `{k}` — {v};')

    ад('\n## 4. Матрица автономности\n')
    ад('| Действие | Уровень | Почему |')
    ад('|---|---|---|')
    for д, (у, поч) in АВТОНОМНОСТЬ.items():
        ад(f'| {д} | `{у}` | {поч} |')
    ад(f'\nПотолок сессии: все внешние записи — не ниже `{ПОТОЛОК_СЕССИИ}`. '
       f'В этой сессии внешних записей нет вообще.\n')

    ад('\n## 5. Dry-run: что увидели наблюдатели\n')
    ад('| Код | Наблюдение | Размер |')
    ад('|---|---|---|')
    for o in p['наблюдения']['наблюдения']:
        ад(f'| `{o["код"]}` | {o["заголовок"]} | {o["размер_сегмента"]} {o["единица"]} |')
    if p['наблюдения']['сбои']:
        ад('\nСбои наблюдателей: ' + '; '.join(
            f'{x["наблюдатель"]} — {x["ошибка"]}' for x in p['наблюдения']['сбои']))
    ад(f'\nИз наблюдений построено {len(гип)} гипотез, дублей отброшено '
       f'{len(p["дубли"])}, дизайнов перестроено {len(p["перестройки"])}:\n')
    for x in p['перестройки']:
        ад(f'- {x["id"]}: {x["что"]};')

    ад('\n## 6. Ранжированный список гипотез\n')
    ад('| # | id | Балл | Статус | Допуск | Проблема | Рычаг | Сегмент | Метрика |')
    ад('|---|---|---|---|---|---|---|---|---|')
    for i, h in enumerate(гип, 1):
        сег = h['target_segment'] or {}
        ад(f'| {i} | {h["hypothesis_id"]} | {h["priority_score"]:.3f} | `{h["status"]}` | '
           f'`{h["approval_level"]}` | {h["business_problem"][:70]} | {h["proposed_lever"]} | '
           f'{сег.get("всего")} {сег.get("единица")} | {h["primary_metric"]} |')
    ад('\nВеса балла: ' + ', '.join(f'{k} {v}' for k, v in ВЕСА.items()) + '.\n')
    ад('Разбор баллов первых трёх:\n')
    for h in гип[:3]:
        ад(f'\n**{h["hypothesis_id"]}** (балл {h["priority_score"]:.3f}, '
           f'уверенность {h["confidence"]}):\n')
        for k, v in h['_объяснение_баллов'].items():
            ад(f'- {k}: {v["балл"]} × {v["вес"]} — {v["почему"]};')

    ад('\n## 7. Экспериментальные контракты (черновики, ничего не применено)\n')
    for h in годные[:3]:
        c = контракт(h, ctx)
        ад(f'\n### {h["hypothesis_id"]} — {h["business_problem"]}\n')
        ад(f'Механизм: {h["mechanism"]}\n')
        ад('```json')
        ад(json.dumps(c, ensure_ascii=False, indent=2))
        ад('```')

    ад('\n## 8. Почему остальные не запущены\n')
    ад('| id | Блокировки | Что именно сказано |')
    ад('|---|---|---|')
    for h in гип:
        if not h['_блокировано']:
            continue
        причины = '; '.join(x['объяснение'] for x in h['_валидация'] if x['вердикт'] == 'fail')
        ад(f'| {h["hypothesis_id"]} | {", ".join(h["_блокировано"])} | {причины} |')
    ад('\nОтдельно про требуемую выборку:\n')
    for h in гип:
        if h.get('required_sample'):
            ад(f'- {h["hypothesis_id"]}: {h["required_sample"]};')

    ад('\n## 9. Проверка системы на уже прожитых гипотезах\n')
    r = читать_реестр()
    ад('| id | Статус | Решение | Знание |')
    ад('|---|---|---|---|')
    for x in r['гипотезы']:
        if x['hypothesis_id'] in ('E1', 'E3', 'E5', 'E6', 'E7', 'E8', 'H-001'):
            ад(f'| {x["hypothesis_id"]} | `{x["status"]}` | {x["decision"] or "—"} | '
               f'{(x["learning"] or "—")[:150]} |')
    ад('')
    for k, v in ЗАПРЕТЫ.items():
        ад(f'- {k}: {v};')

    ад('\n## 10. Какой ручной труд эта версия уже снимает\n')
    for x in ('осмотр одиннадцати витрин руками в начале каждой сессии;',
              'сверка состава новой когорты с E5–E8 и H-001 по CSV-файлам вручную;',
              'расчёт baseline и MDE перед каждым дизайном;',
              'память о том, что E1 отвергнут, а E3 — VOID: теперь это валидаторы, а не проза;',
              'ручное написание контракта эксперимента со всеми полями;',
              'ведение истории решений: переходы пишутся сами и не переписываются;',
              'ручной ответ на вопрос «что и когда мы должны измерить».'):
        ад(f'- {x}')

    ад('\n## 11. Что нужно доказать, чтобы следующий класс действий стал автономным\n')
    for x in (f'Канон BI на единице анализа. Сейчас финансовый результат читается на аккаунте, '
              f'а решения принимаются на SKU и кампании — отсюда '
              f'`BLOCKED_BY_ECONOMICS_GRANULARITY` и потолок `FOUNDER_APPROVAL` для всех '
              f'денежных рычагов.',
              'Снимок и откат, проверенные на живом цикле: снять состояние → применить → '
              'вернуть → убедиться, что вернулось ровно исходное.',
              f'Фиксация факта, а не плана. Сегодня в журнале ставок '
              f'{"7156"} применённых решений без сверки с результатом; пока доля сверенных '
              f'не близка к единице, обещанный эффект ничем не ограничен.',
              'Измеримость на выбранной единице: счётная метрика требует порядка 128 заказов '
              'на плечо за окно теста. Ни один из сегодняшних хвостовых сегментов этого '
              'не даёт — значит, автономно менять там нечего, пока не выбран более крупный '
              'сегмент или более длинное окно.',
              'Тридцать дней журнала, в которых прогноз предыдущего решения совпал с фактом '
              'в пределах объявленной ошибки.'):
        ад(f'- {x}')

    ад(f'\n---\nПостроено `tools/ozon_hypo.py` ({TODAY}). Ни одного POST/PUT/DELETE, '
       f'ни одной записи во внешние системы, ни одного изменения ставок, цен, кампаний '
       f'и остатков.')
    путь = f'{REPORTS}/ozon_hypotheses_{TODAY}.md'
    with open(путь, 'w', encoding='utf-8') as f:
        f.write('\n'.join(L) + '\n')
    сохранить_кандидатов(гип)
    print(f'{путь}: {len(L)} строк; гипотез {len(гип)}, годных {len(годные)}')
    return p


def дополнить_происхождение():
    """Записи, созданные до появления поля, получают происхождение по своей истории.

    Молча оставить поле пустым нельзя: пустое происхождение валится V18, и посеянные
    E5–E8 выглядели бы браком системы, которым они не являются."""
    r = читать_реестр()
    n = 0
    for h in r['гипотезы']:
        if h.get('provenance'):
            continue
        h['provenance'] = ('DERIVED_FROM_FAILED_EXPERIMENT'
                           if h.get('decision') in ('ROLLBACK', 'REJECTED')
                           else 'SEEDED_FROM_HISTORY' if h.get('experiment_id')
                           else 'DETECTED_FROM_LIVE_DATA')
        n += 1
    if n:
        писать_реестр(r)
    return n


def _кр(t, n=64):
    t = ' '.join(str(t or '—').split())
    return t if len(t) <= n else t[:n - 1] + '…'


def cmd_table(a):
    """Все гипотезы реестра одной деловой таблицей: что, откуда, почём и что мешает."""
    р = прогон()
    сохранить_кандидатов(р['гипотезы'])
    дополнить_происхождение()
    r = читать_реестр()
    жив = {h['hypothesis_id']: h for h in р['гипотезы']}
    порядок = sorted(r['гипотезы'], key=lambda h: (
        {'MEASURING': 0, 'APPROVAL_REQUIRED': 1, 'READY': 2, 'DRAFT': 3}.get(h['status'], 4),
        -(h.get('priority_score') or 0), h['hypothesis_id']))
    A = ['| id | суть простыми словами | наблюдаемый факт | рычаг | происхождение | статус |',
         '|---|---|---|---|---|---|']
    B = ['| id | ожидаемые деньги, ₽ | длительность | необходимая выборка | риск | '
         'причина блокировки | пересечения с идущими |', '|---|---|---|---|---|---|---|']
    for h in порядок:
        сл = h.get('_служебное') or {}
        сег = h.get('target_segment') or {}
        блок = сл.get('блокировано') or []
        up, dn = h.get('estimated_upside_rub'), h.get('estimated_downside_rub')
        деньги = (f'+{up:,.0f} / {dn:,.0f}' if up is not None else
                  'закрытый эксперимент' if h['status'] in ('MEASURING', 'ROLLBACK', 'REJECTED')
                  else 'не оценивается (единица анализа не SKU)')
        причина = ('; '.join(блок) if блок else
                   'не блокирована' if h['status'] in ('READY', 'APPROVAL_REQUIRED') else
                   _кр(h.get('result'), 46) if h.get('result') else '—')
        пересеч = (f'{сег.get("пересечение_с_экспериментами")} SKU заняты; '
                   + '; '.join(_кр(d, 40) for d in (h.get('dependencies') or []) or ['—'])
                   if сег.get('пересечение_с_экспериментами') else
                   'идёт сам' if h['status'] == 'MEASURING' else 'нет')
        A.append(f'| `{h["hypothesis_id"]}` | {_кр(h.get("business_problem"), 90)} | '
                 f'{_кр(h.get("current_evidence"), 70)} | {h.get("proposed_lever") or "—"} | '
                 f'`{h.get("provenance") or "—"}` | `{h["status"]}` |')
        B.append(f'| `{h["hypothesis_id"]}` | {деньги} | '
                 f'{(str(h.get("minimum_duration")) + " дней") if h.get("minimum_duration") else "—"} | '
                 f'{_кр(h.get("required_sample"), 70)} | {h.get("risk_level") or "—"} | '
                 f'{причина} | {_кр(пересеч, 60)} |')
    путь = f'{REPORTS}/ozon_hypotheses_table_{TODAY}.md'
    open(путь, 'w', encoding='utf-8').write(
        f'# Гипотезы Ozon — деловая таблица, {TODAY}\n\n'
        f'Всего в реестре: {len(порядок)}. Живой прогон дал {len(жив)} кандидатов.\n\n'
        '## Что и откуда\n\n' + '\n'.join(A) +
        '\n\n## Деньги, выборка, риск\n\n' + '\n'.join(B) + '\n')
    print(f'{путь}: {len(порядок)} гипотез')
    for h in порядок:
        print(f'  {h["hypothesis_id"]:<6} {h["status"]:<18} {(h.get("provenance") or "—")[:31]:<31} '
              f'{_кр(h.get("business_problem"), 46)}')
    return порядок


def cmd_trace(a):
    """Один сквозной след: строки данных → отклонение → гипотеза → приоритет → дизайн →
    проверки → статус → dry-run. Без него вертикаль недоказуема."""
    р = прогон()
    гип = р['гипотезы']
    h = next((x for x in гип if x['hypothesis_id'] == getattr(a, 'для', None)), гип[0])
    набл = h.get('_наблюдение') or {}
    b = h.get('_объяснение_баллов') or {}
    к = контракт(h, р['ctx'])
    L = [f'# Сквозной след гипотезы {h["hypothesis_id"]} — {TODAY}', '',
         f'Окно наблюдения: {р["наблюдения"]["окно_текущее"][0]}…'
         f'{р["наблюдения"]["окно_текущее"][1]} против '
         f'{р["наблюдения"]["окно_прошлое"][0]}…{р["наблюдения"]["окно_прошлое"][1]}.', '',
         '## 1. LIVE DATA — что прочитано', '',
         f'- витрина наблюдателя: `{набл.get("код")}`',
         f'- агрегаты: {json.dumps(набл.get("подробности") or {}, ensure_ascii=False)[:600]}',
         f'- размер среза: {набл.get("размер_сегмента")} {набл.get("единица")}', '',
         '## 2. OBSERVATION — обнаруженное отклонение', '',
         f'- {набл.get("заголовок")}', f'- факт: {набл.get("факт")}', '',
         '## 3. MECHANISM — чем это объясняется', '', f'{h.get("mechanism")}', '',
         '## 4. HYPOTHESIS — что проверяем', '',
         f'- проблема: {h.get("business_problem")}',
         f'- рычаг: {h.get("proposed_lever")}; метрика: {h.get("primary_metric")}',
         f'- происхождение: `{h.get("provenance")}`', '',
         '## 5. PRIORITY — расчёт', '', '| составляющая | балл | вес | почему |', '|---|---|---|---|']
    for k2, v in b.items():
        L.append(f'| {k2} | {v["балл"]} | {v["вес"]} | {v["почему"]} |')
    L += ['', f'**Итог: {h.get("priority_score")}**, допуск `{h.get("approval_level")}` '
              f'({h.get("_почему_уровень")}).', '',
          '## 6. DESIGN — дизайн теста', '',
          f'- состав: {к["состав"]}', f'- контроль: {к["контроль"]}',
          f'- окно: {к["окно"]}', f'- мощность: {json.dumps(h.get("_мощность") or {}, ensure_ascii=False)}',
          f'- выборка: {h.get("required_sample")}', '',
          '## 7. VALIDATION — 18 проверок', '', '| код | вердикт | объяснение |', '|---|---|---|']
    for v in h.get('_валидация') or []:
        L.append(f'| `{v["код"]}` | {v["вердикт"]} | {v["объяснение"]} |')
    L += ['', f'## 8. STATUS — `{h["status"]}`', '',
          f'- блокировки: {", ".join(h.get("_блокировано") or []) or "нет"}',
          f'- риск: {h.get("risk_level")}', '',
          '## 9. DRY-RUN — что было бы отправлено', '',
          f'- статус контракта: {к["статус"]}',
          f'- внешних вызовов: 0; изменённых ставок: 0; изменённых кампаний: 0',
          f'- откат: {к["откат"]}', f'- снимок до отправки: {к["снимок_до"]}',
          f'- не делается: {"; ".join(к["что_не_делается"])}', '']
    путь = f'{REPORTS}/ozon_trace_{h["hypothesis_id"].replace("/", "_")}_{TODAY}.md'
    open(путь, 'w', encoding='utf-8').write('\n'.join(L) + '\n')
    print(f'{путь}: {len(L)} строк, гипотеза {h["hypothesis_id"]} '
          f'({h["status"]}, балл {h.get("priority_score")})')
    return h


def cmd_waves(a):
    """Волновой план внутри одной гипотезы. Ничего не применяет."""
    р = прогон()
    h = next((x for x in р['гипотезы'] if x['hypothesis_id'] == a.для), None)
    if not h:
        print(f'нет такой гипотезы в живом прогоне: {a.для}')
        return None
    res = волны(h, р['ctx'], размер=a.размер, горизонт=getattr(a, 'горизонт', False))
    файлы = записать_волны(res)
    доступно = [w for w in res['волны'] if w['статус'] == 'ЖДЁТ СОГЛАСОВАНИЯ']
    чистые = [{k: v for k, v in w.items() if not k.startswith('_')} for w in res['волны']]
    r = читать_реестр()
    for rec in r['гипотезы']:
        if rec['hypothesis_id'] == h['hypothesis_id']:
            rec['waves'] = чистые
            rec['_волновая_сводка'] = res['сводка']
    писать_реестр(r)
    запись_в_журнал(h['hypothesis_id'], h['status'], h['status'],
                    f'волновой план: {len(чистые)} волн по {a.размер} SKU, ничего не применено')
    L = [f'# Волновой план {h["hypothesis_id"]} — {TODAY}', '',
         f'Гипотеза охватывает {res["сводка"]["сегмент_всего"]} SKU; воздействие применяется '
         f'волнами по {a.размер}. Ни одна волна не применена. '
         + (f'Разрешение не запрашивается: {res["сводка"]["статус_плана"]}.'
            if not any(w['исполнимый'] for w in res['волны'])
            else f'Открыта к согласованию {len(доступно)} из {len(res["волны"])}.'), '',
         '## Сводка', ''] + [f'- {k}: {v}' for k, v in res['сводка'].items()] + [
         '', '## Волны', '',
         '| волна | статус | всего | воздействие | контроль | окно | потолок ₽ | предусловие |',
         '|---|---|---|---|---|---|---|---|']
    for w in res['волны']:
        L.append(f'| `{w["wave_id"]}` | {w["статус"]} | {w["состав"]["всего"]} | '
                 f'{w["состав"]["воздействие"]} | {w["состав"]["контроль"]} | '
                 f'{w["окно"]["старт"]}…{w["окно"]["сверка"]} | '
                 f'{w["риск"]["потолок_расхода_₽"]:,} | {w["предусловие"]} |')
    L += ['', '## Правила выхода волны', '']
    for k2, v in (res['волны'][0]['решение'] if res['волны'] else {}).items():
        L.append(f'- **{k2}** — {v}')
    L += ['', '## Страты первой волны', '']
    if res['волны']:
        for k2, v in res['волны'][0]['состав']['страты'].items():
            L.append(f'- {k2}: воздействие {v["воздействие"]}, контроль {v["контроль"]}')
    L += ['', '## Файлы состава (заморожены до согласования)', ''] + [f'- `{f}`' for f in файлы]
    путь = f'{REPORTS}/ozon_waves_{h["hypothesis_id"].replace("/", "_")}_{TODAY}.md'
    open(путь, 'w', encoding='utf-8').write('\n'.join(L) + '\n')
    print(f'{путь}: волн {len(res["волны"])}, файлов состава {len(файлы)}')
    for k2, v in res['сводка'].items():
        print(f'  {k2}: {v}')
    return res


def main():
    ap = argparse.ArgumentParser(description='Контур гипотез Ozon, shadow mode (только чтение)')
    sp = ap.add_subparsers(dest='cmd', required=True)
    sp.add_parser('observe')
    pr = sp.add_parser('propose'); pr.add_argument('--save', action='store_true')
    sp.add_parser('validate')
    sp.add_parser('prioritize')
    pl = sp.add_parser('plan'); pl.add_argument('--top', type=int, default=3)
    sp.add_parser('status')
    sp.add_parser('seed')
    sp.add_parser('report')
    sp.add_parser('table')
    tr = sp.add_parser('trace'); tr.add_argument('--for', dest='для')
    wv = sp.add_parser('waves')
    wv.add_argument('--for', dest='для', required=True)
    wv.add_argument('--size', dest='размер', type=int, default=ВОЛНА_МАКС)
    wv.add_argument('--horizon', dest='горизонт', action='store_true',
                    help='планировать весь сегмент, включая состав, занятый E5–E8')
    ev = sp.add_parser('evaluate')
    ev.add_argument('--decide'); ev.add_argument('--as', dest='as_',
                                                 choices=('APPROVED', 'REJECTED', 'DEFERRED'))
    ev.add_argument('--reason', default='')
    er = sp.add_parser('evaluate-ready')
    er.add_argument('--only', help='список id экспериментов через запятую')
    er.add_argument('--today', help='дата сверки (по умолчанию TODAY модуля)')
    er.add_argument('--dry-run', dest='dry_run', action='store_true',
                    help='посчитать и показать, ничего не записывая')
    a = ap.parse_args()
    if a.cmd == 'seed':
        print(f'посеяно новых: {посеять()}')
        return
    {'observe': cmd_observe, 'propose': cmd_propose, 'validate': cmd_validate,
     'prioritize': cmd_prioritize, 'plan': cmd_plan, 'status': cmd_status,
     'evaluate': cmd_evaluate, 'report': cmd_report, 'table': cmd_table,
     'trace': cmd_trace, 'waves': cmd_waves,
     'evaluate-ready': cmd_evaluate_ready}[a.cmd](a)


if __name__ == '__main__':
    main()
