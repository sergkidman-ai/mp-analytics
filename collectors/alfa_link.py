# поток: inv
"""collectors/alfa_link.py — привязка исходящих платежей выписки к ПРИЁМКАМ МойСклада.

Замыкает контур закупки: счёт → Заказ поставщику → УПД → Приёмка → черновик платёжки →
оплата в банке → выписка → Исходящий платёж → **связь платежа с Приёмкой** (этот модуль).
Итог: по каждой поставке видно Заказ (сумма X) = Приёмка (X) = Оплата (X).

Как в МС: связь живёт в `paymentout.operations` — массив
`{meta: .../entity/supply/{id}, linkedSum: <копейки>}`, пишется `PUT /entity/paymentout/{id}`.
После привязки `supply.payedSum` растёт до `supply.sum` (приёмка закрыта). Ровно так заведены
ручные платежи владельца (61 из 100 июльских исходящих; 215 ссылок на supply).

Мост «платёж → документы» — номера из НАЗНАЧЕНИЯ платежа (`paymentPurpose` выписки):
  • предоплата по счёту (Феррет): «...по СЧЕТ-ДОГОВОР 6307 от 27.07.2026» → приёмка №6307;
  • пачка по отсрочке (КВК ТРЕЙД): «Оплата по KV00009220, 9200, 9267…» → номера ЗАКАЗОВ,
    берём приёмки этих заказов. Первый номер полный, остальные сокращены — разворачиваем
    по образцу первого (KV0000|9220 → 9200 ⇒ KV00009200).
  • АВАНС (номеров в назначении нет вообще: «Авансовый платеж за картриджи») → приёмки этого
    поставщика по возрастанию даты, пока хватает денег (см. `plan_advance`).
Поставщик не хардкодится: сначала ищем приёмку с таким номером, затем заказ с таким номером,
и всё это ТОЛЬКО у контрагента самого платежа — чужой документ с похожим номером не подхватится.

ПРИВЯЗКА — НЕ ОДНОРАЗОВОЕ ДЕЙСТВИЕ. Предоплата уходит РАНЬШЕ поставки: платёж Феррету по счёту
6325 прошёл 28.07, а приёмка 6325 появилась 29.07 — в момент записи платежа привязывать было не
к чему («привязано к приёмкам 0» в крон-логе 29.07). Поэтому `run_inv.py` после разбора дня
делает ДОБОР по непривязанным платежам за последние `LINK_LOOKBACK_DAYS` дней.

ГЛАВНЫЙ ПРЕДОХРАНИТЕЛЬ: привязываем, только если сумма платежа раскладывается по найденным
приёмкам ПОЛНОСТЬЮ (остаток 0). Частичная привязка молча исказит учёт — такой платёж честно
остаётся непривязанным и попадает в лог на ручной разбор.

КОГО ПРИВЯЗЫВАЕМ: только контрагентов из `supplier_payment_terms` (см. `supplier_agents`).
Платёж кому угодно ещё — не наш случай, разбор даже не начинается.

К ЧЕМУ ПРИВЯЗЫВАЕМ: только к приёмкам ПРОВЕДЁННЫМ и в статусе «Принят/ на оплату» (`linkable`).
Черновик ещё правят — деньги на нём исказят учёт; непринятая приёмка деньги пока не ждёт.
Внутри одного юрлица: организация платежа = организация приёмки (`_org_href`).

АВАНСЫ — ОЧЕРЕДЬ FIFO (решение Сергея 19.08.2026). Пока у поставщика есть БОЛЕЕ РАННИЙ аванс с
неизрасходованным остатком, свежий платёж приёмки не забирает (`older_open_advance` → статус
`advance-wait`): сначала полностью тратится предыдущий, потом начинается следующий. Раньше
порядок задавала выдача МС — она не отсортирована, и свежий платёж расхватывал приёмки вперёд
старого (аванс Тонероптторга от 13.08 так и висел с остатком 38 771.78 ₽). Гейт снимается сам:
как только старший аванс израсходован, младший разбирается на том же прогоне.

ТОЛЬКО ПЛАТЕЖИ ИЗ ВЫПИСКИ. Разбираем лишь то, что записал наш конвейер (`from_bank`, признак —
`syncId`). Авансы, заведённые в МС руками до подключения банковского API, не привязываем никуда
и в баланс поставщика не берём (статус `manual-skip`) — их остатки владелец разносит сам.

БАЛАНС ПОСТАВЩИКА (`advance_balance`) = Σ неизрасходованных остатков его авансов. Это же число —
гейт черновика нового аванса в `invoice_bot/po_payment_watch.py`; заказы в него не входят.

Запуск отдельно (добор ранее созданных платежей):
    ./venv/bin/python collectors/alfa_link.py 2026-07-01            # dry-run с даты
    ./venv/bin/python collectors/alfa_link.py 2026-07-01 --apply    # записать
    ./venv/bin/python collectors/alfa_link.py 2026-07-01 --all      # + отсеянные строки
"""
import os
import re
import sys
import time
import pathlib
import datetime as dt
import urllib.error
import urllib.parse

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))                     # core.db — гейт по таблице поставщиков
sys.path.insert(0, "/opt/mp-analytics/invoice_bot")
from ms import get as _ms_get, put, MS                   # noqa: E402
import core.db as db                                     # noqa: E402

# токен-кандидат: необязательный буквенный префикс + разделитель + 3..12 цифр (KV00009220, 6307)
_TOKEN = re.compile(r"\b([A-Za-zА-Яа-я]{0,6})([- ]?)(\d{3,12})\b")
_DATE = re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{2,4}\b")     # 27.07.2026 — не номер документа
_MONEY = re.compile(r"\b\d[\d\s]*[.,]\d{2}\b")           # 3998.31 — сумма, не номер
_CYR = re.compile(r"[А-Яа-я]")
MAX_TOKENS = 20                                          # предохранитель от мусорных назначений

_ADVANCE = re.compile(r"(?i)аванс")                      # «Авансовый платеж за картриджи»
ADVANCE_LOOKBACK = 45                                    # приёмки ДО аванса: хвосты прошлых поставок
# Разнос авансов пишет в документы, которые владелец ведёт руками, поэтому по умолчанию ВЫКЛЮЧЕН.
# Включение — `ALFA_LINK_ADVANCE=1` в .env, после того как dry-run показан и согласован.
ADVANCE_ON = os.getenv("ALFA_LINK_ADVANCE") == "1"
ADVANCE_QUEUE_LOOKBACK = 180                             # окно поиска более ранних открытых авансов


def from_bank(payment):
    """Платёж записан НАШИМ конвейером из банковской выписки (`bank_ms.sync`), а не руками в МС.

    Признак — поле `syncId`: туда кладётся uuid банковской операции (идемпотентность записи).
    У платежей, заведённых человеком до подключения банковского API, его нет: у Цифрового
    Квадрата граница 27.07.2026, у Дисквэра — 03.08.2026 (проверено по МС).

    Решение Сергея 19.08.2026: старые ручные авансы с остатком не привязываем никуда и в баланс
    поставщика не берём — их разносил человек, наша очередь про них ничего не знает."""
    return bool(payment.get("syncId"))


def get(path, tries=5):
    """GET с бэкоффом: МС лимитирует частоту (429), а мы бьём по номеру в цикле."""
    for i in range(tries):
        try:
            return _ms_get(path)
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < tries - 1:
                time.sleep(2 * (i + 1))
                continue
            raise


def purpose_tokens(purpose):
    """Номера документов из назначения платежа. Хвост «в том числе НДС…» отбрасываем —
    там проценты и суммы, которые иначе читаются как номера."""
    s = (purpose or "")
    cut = re.split(r"(?i)\bв\s+том\s+числе\b", s)[0]
    cut = _MONEY.sub(" ", _DATE.sub(" ", cut))
    out, pattern = [], None
    for pref, sep, digits in _TOKEN.findall(cut):
        pref = pref.strip()
        if not pref and len(digits) == 4 and digits.startswith(("19", "20")):
            continue                                     # похоже на год
        if pref and not sep and _CYR.search(pref) and pref != pref.upper():
            continue                                     # «карте220015», «счет1234» — слово+число,
            #                                              номера документов так не пишут.
            #                                              ЗАГЛАВНЫЕ без разделителя — наоборот,
            #                                              обычный номер («ОД00004103», «КТ00097»)
        sep = sep if pref else ""                        # разделитель значим только при префиксе
        if pref and pattern is None:
            pattern = (pref, sep, len(digits))           # образец полного номера (KV, '', 8)
        out.append((pref, sep, digits))
    tokens = []
    for pref, sep, digits in out:
        tokens.append(f"{pref}{sep}{digits}")            # «СП-1234» — дефис часть номера
        if not pref and pattern and len(digits) < pattern[2]:
            # сокращённый номер в перечислении → разворачиваем по образцу первого
            tokens.append(f"{pattern[0]}{pattern[1]}{digits.zfill(pattern[2])}")
    seen, uniq = set(), []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq[:MAX_TOKENS]


def _agent_href(payment):
    return ((payment.get("agent") or {}).get("meta") or {}).get("href")


def _org_href(payment):
    """Юрлицо платежа. Привязка ВСЕГДА внутри одного юрлица: у нас их два (Цифровой квадрат и
    Дисквэр), общий каталог и пересекающиеся номера документов, а деньги и товар у каждого свои.
    Контроль на живых данных 2026-07-30: 945 ручных привязок владельца — все внутри своего
    юрлица, межъюрлицных ноль."""
    return ((payment.get("organization") or {}).get("meta") or {}).get("href")


def _agent_id(payment):
    return (_agent_href(payment) or "").rsplit("/", 1)[-1].split("?")[0]


_ORG_INN = {}


def org_inn(payment):
    """ИНН НАШЕГО юрлица-плательщика (не поставщика!) — ключ разреза во всех таблицах контура
    после миграции 207: условия оплаты, очередь черновиков и статусы заказов теперь заведены
    на каждое юрлицо отдельно. Без этого ключа по одному ИНН поставщика возвращаются ДВЕ
    строки, и платёж Дисквэра может разобраться черновиком Цифрового Квадрата — поставщики
    у фирм одни и те же, суммы совпадают легко."""
    oid = (_org_href(payment) or "").rsplit("/", 1)[-1].split("?")[0]
    if not oid:
        return None
    if oid not in _ORG_INN:
        for o in get("/entity/organization").get("rows", []):
            _ORG_INN[o["id"]] = o.get("inn")
    return _ORG_INN.get(oid)


_SUPPLIER_AGENTS = {}


def supplier_agents(our_inn):
    """id контрагентов МС из `supplier_payment_terms` — ЕДИНСТВЕННЫЕ, чьи платежи привязываем
    (правило Сергея 2026-07-29). Таблица условий оплаты и есть список наших поставщиков: всё
    остальное (налоги, банк, аренда, зарплата, разовые контрагенты) к приёмкам не относится, и
    угадывать там нечего. Гейт стоит ПЕРЕД разбором назначения, поэтому лишних запросов в МС по
    чужим платежам не будет.

    Берём строки независимо от `active`: флаг выключает автосоздание черновиков платёжек, а не
    отменяет факт поставки — по старому поставщику приёмки и платежи привязывать по-прежнему надо.

    Кэш на процесс: прогон крона живёт минуты, а таблицу правит человек в дашборде.
    ПУСТАЯ таблица — не повод привязывать всё подряд: это сломанная конфигурация, и молчаливый
    «привязываем всех» опаснее шумной ошибки, поэтому падаем.

    Разрез по нашему юрлицу (`our_inn`, миграция 207): у каждой фирмы свой список поставщиков,
    и платёж Дисквэра проверяем по условиям Дисквэра, а не по общей куче."""
    if our_inn not in _SUPPLIER_AGENTS:
        rows = db.query("SELECT ms_agent_id FROM supplier_payment_terms "
                        "WHERE org_inn = %s AND ms_agent_id IS NOT NULL AND ms_agent_id <> ''",
                        (our_inn,))
        if not rows:
            raise RuntimeError(f"supplier_payment_terms пуста для юрлица {our_inn} "
                               "(или без ms_agent_id) — привязка платежей отключена "
                               "до заполнения таблицы")
        _SUPPLIER_AGENTS[our_inn] = {r["ms_agent_id"] for r in rows}
    return _SUPPLIER_AGENTS[our_inn]


def _find(entity, name, agent_href, org_href):
    """Документ с таким номером у ЭТОГО контрагента И ЭТОГО юрлица (иначе можно схватить чужой).
    Фильтр по организации обязателен: номера документов у Цифрового и Дисквэра пересекаются."""
    flt = urllib.parse.quote(f"name={name};agent={agent_href};organization={org_href}")
    rows = get(f"/entity/{entity}?filter={flt}&limit=5").get("rows", [])
    return rows


# Единственный статус приёмки, к которой можно привязывать деньги (решение Сергея 2026-07-30).
# В МС это ОДНО значение с косой чертой, а не два: «Принят/ на оплату».
LINKABLE_STATE = "Принят/ на оплату"
_STATE_NAMES = None


def _state_names():
    """id статуса приёмки → имя (из метаданных сущности). Если статуса `LINKABLE_STATE` в МС нет —
    падаем: значит его переименовали, и молчаливая деградация превратила бы гейт в «не привязывать
    вообще никогда», что выглядит как исправная работа."""
    global _STATE_NAMES
    if _STATE_NAMES is None:
        names = {s["id"]: s["name"] for s in get("/entity/supply/metadata").get("states", [])}
        if LINKABLE_STATE not in names.values():
            raise RuntimeError(f"в МС нет статуса приёмки «{LINKABLE_STATE}» — переименовали? "
                               "привязка остановлена, пока статус не сверен")
        _STATE_NAMES = names
    return _STATE_NAMES


def linkable(supply):
    """Можно ли привязывать деньги к этой приёмке (решение Сергея 2026-07-30):
    **проведена** (`applicable`) И в статусе «Принят/ на оплату».

    Черновик — не обязательство: пока флажок «Проведён» не стоит, документ правят, и привязанный
    к нему платёж исказит учёт. Статус отсекает проведённые, но ещё не принятые («Создан»,
    «Идет приемка») и те, что платить не нужно («Не оплачивать»). «Оплачен» сюда не входит
    осознанно: у него неоплаченного остатка нет, деньги ему не нужны.

    Срез на 30.07 (приёмки с 01.05): из 107 с неоплаченным остатком проходят 94, отсекаются 13 —
    10 черновиков и 3 проведённых в статусе «Создан»."""
    if not supply.get("applicable"):
        return False
    sid = (((supply.get("state") or {}).get("meta") or {}).get("href") or "") \
        .rsplit("/", 1)[-1].split("?")[0]
    return _state_names().get(sid) == LINKABLE_STATE


def _full_supply(s):
    """У заказа приёмки приходят кратко — догружаем документ ради sum/payedSum и `applicable`
    (без него `linkable` приняла бы черновик за непроведённый только по отсутствию поля)."""
    if "sum" in s and "payedSum" in s and "applicable" in s:
        return s
    href = (s.get("meta") or {}).get("href")
    return get(href.replace(MS, "")) if href else None


def unpaid_left(supply):
    """Неоплаченный остаток приёмки, копейки. `payedSum` — это и есть «сколько исходящих платежей
    к ней привязано», отдельного поля связи в МС нет."""
    return (supply.get("sum") or 0) - (supply.get("payedSum") or 0)


_DIGIT_RUN = re.compile(r"\d+")


def _numkey(name):
    """Ключ номера документа: самая длинная группа цифр без ведущих нулей. Буквы и разделители
    отбрасываем — в назначении платежа поставщик пишет номер как хочет, а цифры не врут:
    «КТ-00097» и наша приёмка «КТ-000097» → 97; «326006» и «326006/И» → 326006;
    «ОД00004103» → 4103. Ключ работает только внутри пары контрагент+юрлицо и только по
    приёмкам с неоплаченным остатком — иначе такая вольность ловила бы чужие документы."""
    runs = _DIGIT_RUN.findall(name or "")
    return max(runs, key=len).lstrip("0") or "0" if runs else None


_POOL = {}


def unpaid_pool(agent_href, org_href, since):
    """Приёмки поставщика, к которым исходящий платёж ещё НЕ привязан (остаток > 0), — единственное
    множество, среди которого имеет смысл искать (правило Сергея 2026-08-01). Оплаченную приёмку
    деньги второй раз не ищут, поэтому фильтр заодно снимает неоднозначность одинаковых номеров:
    у Колортека «3814» и «3814-Колортек» оба подходят по цифрам, но неоплаченная из них одна."""
    key = (agent_href, org_href, since)
    if key not in _POOL:
        _POOL[key] = [s for s in agent_supplies(agent_href, since, org_href) if unpaid_left(s) > 0]
    return _POOL[key]


POOL_LOOKBACK_DAYS = 180        # отсрочка платежа у поставщиков до ~90 дней, берём с запасом


def resolve_supplies(payment):
    """→ (список приёмок, список нерезолвленных номеров). Номер = приёмка, либо заказ→его приёмки.

    Это ЗАПАСНОЙ путь: состав платежа восстанавливается по тексту назначения и применяется только
    к платежам, которых нет в очереди черновиков (`plan_draft`). Кандидатами берём ТОЛЬКО приёмки
    с неоплаченным остатком."""
    agent_href, org_href = _agent_href(payment), _org_href(payment)
    if not agent_href or not org_href:
        return [], []
    since = (dt.date.fromisoformat((payment.get("moment") or "")[:10])
             - dt.timedelta(days=POOL_LOOKBACK_DAYS)).isoformat()
    found, missed, seen = [], [], set()
    for tok in purpose_tokens(payment.get("paymentPurpose")):
        rows = _find("supply", tok, agent_href, org_href)
        if not rows:
            for po in _find("purchaseorder", tok, agent_href, org_href):
                po_full = get(f"/entity/purchaseorder/{po['id']}?expand=supplies")
                rows.extend(po_full.get("supplies") or [])
        cands = []
        for r in rows:
            full = _full_supply(r)
            if full and full.get("id") and linkable(full) and unpaid_left(full) > 0:
                cands.append(full)
        if not cands:
            # точного имени нет: поставщик написал номер иначе, чем он заведён в МС. Ищем по цифрам
            # среди неоплаченных приёмок этого контрагента и юрлица.
            key = _numkey(tok)
            near = [s for s in unpaid_pool(agent_href, org_href, since)
                    if key and _numkey(s.get("name")) == key] if key else []
            if len(near) == 1:
                cands = near
            elif len(near) > 1:
                missed.append(f"{tok} (подходят {len(near)} приёмки — ручной разбор)")
                continue        # угадывать между несколькими нельзя
        # приёмка-черновик, «Оплачен» или уже закрытая деньгами = как будто её нет: платёж
        # останется непривязанным и добор подхватит его на следующем прогоне
        if not cands:
            missed.append(tok)
            continue
        for full in cands:      # номер разобран, даже если приёмку дал сосед-токен
            if full["id"] not in seen:
                seen.add(full["id"])
                found.append(full)
    return found, missed


# ───────────────────── путь 1: состав платежа из ЧЕРНОВИКА платёжки ─────────────────────
# Черновик знает, ЗА ЧТО платили: `covers_po_ids` — список заказов поставщику, под которые он
# собран. Это ПЕРВИЧНЫЙ источник состава (решение Сергея 2026-08-01), а разбор назначения —
# запасной: назначение лишь текстовое отражение того же списка, и на нём мы теряли номера
# (кириллический префикс без разделителя «ОД00004103», суффикс «326006/И», разрядность
# «КТ-00097» ≠ «КТ-000097» в МС). Замер 01.08.2026: черновик #13 (Одиссей, 474 950,87 ₽) даёт
# 23 приёмки из 23, разбор назначения — 1, из-за чего 468 060,75 ₽ ушли в ручной разбор.
# Снят: черновики, отменённые владельцем (`cancelled`) — по ним денег не ждём.
DRAFT_STATUSES = ("planned", "sent_sandbox", "sent_prod", "error", "paid")


def _inn_of(payment, our_inn=None):
    """ИНН ПОСТАВЩИКА по контрагенту МС — ключ, которым черновики связаны с платежами.
    `our_inn` — наше юрлицо-плательщик: после миграции 207 одна карточка контрагента даёт
    строку условий в каждой фирме, и без разреза берётся случайная из двух."""
    aid = _agent_id(payment)
    if not aid:
        return None
    if our_inn:
        rows = db.query("SELECT inn FROM supplier_payment_terms "
                        "WHERE ms_agent_id=%s AND org_inn=%s", (aid, our_inn))
    else:
        rows = db.query("SELECT inn FROM supplier_payment_terms WHERE ms_agent_id=%s", (aid,))
    return rows[0]["inn"] if rows else None


def find_draft(payment):
    """Черновик платёжки, из которого вырос этот платёж, либо None.

    Ключ — ИНН + сумма ДО КОПЕЙКИ + «черновик не позже платежа». Сумму сверяем точно: черновик
    считаем мы сами, и банк платит ровно его сумму; приблизительный матч тут опаснее промаха —
    он привяжет деньги к чужому списку заказов.

    Черновик, уже закреплённый за ДРУГИМ платежом (`ms_payment_id`, миграция 206), не берём:
    иначе два одинаковых платежа одному поставщику разобрались бы одним и тем же черновиком.
    Авансовые черновики сюда не попадают — у них `covers_po_ids` пуст, состав определяется
    не списком заказов, а FIFO по приёмкам (см. `plan_advance`).

    Разрез по нашему юрлицу обязателен (миграция 207): поставщики у Цифрового Квадрата и
    Дисквэра одни и те же, суммы платежей совпадают запросто — без `org_inn` платёж одной
    фирмы разобрался бы черновиком другой и лёг бы на ЧУЖИЕ приёмки."""
    our = org_inn(payment)
    inn = _inn_of(payment, our)
    if not inn or not our:
        return None
    rows = db.query(
        """SELECT id, covers_po_ids FROM payment_draft_queue
            WHERE inn = %s AND org_inn = %s AND round(amount * 100) = %s
              AND status = ANY(%s)
              AND coalesce(array_length(covers_po_ids, 1), 0) > 0
              AND (ms_payment_id IS NULL OR ms_payment_id = %s)
              AND created_at::date <= %s::date
            ORDER BY (ms_payment_id = %s) DESC NULLS LAST, created_at DESC
            LIMIT 1""",
        (inn, our, payment["sum"], list(DRAFT_STATUSES), payment["id"],
         (payment.get("moment") or "")[:10], payment["id"]))
    return rows[0] if rows else None


def draft_supplies(draft):
    """Приёмки под заказами черновика. → (приёмки по возрастанию даты, заказы без годной приёмки).

    Заказ без приёмки — не ошибка: товар оплачен, но ещё не приехал (или документ не проведён).
    Такие заказы возвращаем отдельным списком, чтобы честно показать в логе, чего ждём."""
    found, waiting, seen = [], [], set()
    for po_id in draft["covers_po_ids"]:
        try:
            po = get(f"/entity/purchaseorder/{po_id}?expand=supplies")
        except urllib.error.HTTPError:
            waiting.append(str(po_id)[:8])
            continue
        got = False
        for s in po.get("supplies") or []:
            full = _full_supply(s)
            if not (full and full.get("id") and linkable(full)):
                continue
            got = True
            if full["id"] not in seen:
                seen.add(full["id"])
                found.append(full)
        if not got:
            waiting.append(po.get("name") or str(po_id)[:8])
    return sorted(found, key=lambda s: s.get("moment") or ""), waiting


def plan_draft(payment):
    """Раскладка ПО ЧЕРНОВИКУ. → (operations | None, остаток_копеек, пояснение, id черновика).
    `operations is None` ⇒ черновика нет, разбираем платёж прежними путями.

    Остаток здесь НЕ запрет на запись — в отличие от разбора назначения (`plan`). Там остаток
    означает «мы не поняли состав платежа», и привязывать вслепую нельзя. Здесь состав известен
    точно, а остаток говорит лишь «приёмку по оплаченному заказу ещё не провели»: привязываем
    то, что уже есть, и ДОБИРАЕМ на следующих прогонах, как у аванса. Иначе один непроведённый
    документ снова заблокировал бы платёж целиком.

    Как и у аванса, возвращаем ПОЛНЫЙ массив operations (старое + новое): МС принимает
    `operations` целиком, а не дельтой."""
    draft = find_draft(payment)
    if not draft:
        return None, payment["sum"], "черновика нет", None
    own = _ops_map(payment.get("operations"))
    left = payment["sum"] - sum(own.values())
    if left <= 0:                                # дёшево: без похода в МС за приёмками
        return [], 0, f"черновик #{draft['id']}: платёж уже разнесён", draft["id"]
    supplies, waiting = draft_supplies(draft)
    merged, added, taken = dict(own), 0, 0
    for s in supplies:
        # payedSum уже включает наш собственный вклад — берём реально неоплаченный остаток
        unpaid = (s.get("sum") or 0) - (s.get("payedSum") or 0)
        take = min(unpaid, left)
        if take <= 0:
            continue
        merged[s["id"]] = merged.get(s["id"], 0) + take
        left -= take
        added += 1
        taken += take
        if left <= 0:
            break
    note = f"черновик #{draft['id']}: приёмок +{added} на {taken/100:,.2f} ₽"
    if waiting:
        note += f", ждут приёмки заказов: {len(waiting)}"
    if left > 0:
        note += f", не разнесено {left/100:,.2f} ₽"
    if not added:
        return [], left, note, draft["id"]
    ops = [{"meta": {"href": f"{MS}/entity/supply/{sid}", "type": "supply",
                     "mediaType": "application/json"}, "linkedSum": v}
           for sid, v in merged.items()]
    return ops, left, note, draft["id"]


def distribute(total, supplies, paid_override=None):
    """Разложить сумму платежа по приёмкам: каждой — её неоплаченный остаток, пока деньги есть.
    → (operations, нераспределённый остаток). `paid_override` {supply_id: payedSum} нужен тестам,
    чтобы посчитать распределение «как если бы платёж ещё не был привязан»."""
    ops, left = [], total
    for s in supplies:
        paid = (paid_override or {}).get(s["id"], s.get("payedSum") or 0)
        unpaid = (s.get("sum") or 0) - paid
        take = min(unpaid, left)
        if take <= 0:
            continue
        ops.append({"meta": {"href": f"{MS}/entity/supply/{s['id']}", "type": "supply",
                             "mediaType": "application/json"},
                    "linkedSum": take})
        left -= take
    return ops, left


def is_advance(payment):
    """Аванс = деньги вперёд, без ссылки на конкретный документ («Авансовый платеж за картриджи»).
    Маркер берём ТОЛЬКО из слова в назначении: «нет номеров» само по себе маркером быть не может —
    так выглядят и зарплата, и налоги, и эквайринг, а их к приёмкам привязывать нельзя."""
    return bool(_ADVANCE.search(payment.get("paymentPurpose") or ""))


def agent_supplies(agent_href, since, org_href):
    """Приёмки контрагента с даты `since` (включая появившиеся ПОЗЖЕ платежа — аванс закрывает
    будущие поставки), по возрастанию даты. Фильтр по юрлицу обязателен: деньги Цифрового не гасят
    поставки Дисквэра (см. `_org_href`). Черновики и не-«Принят/ на оплату» отсеиваются
    (`linkable`) — аванс подождёт, пока документ проведут."""
    flt = urllib.parse.quote(
        f"agent={agent_href};organization={org_href};moment>={since} 00:00:00", safe="=;:")
    out, off = [], 0
    while True:
        page = get(f"/entity/supply?filter={flt}&limit=100&offset={off}")
        rows = page.get("rows", [])
        out.extend(rows)
        if len(rows) < 100:
            break
        off += 100
    return sorted((s for s in out if linkable(s)), key=lambda s: s.get("moment") or "")


def open_advances_of(agent_href, org_href, since, before=None):
    """Авансы ЭТОГО поставщика у ЭТОГО юрлица с неизрасходованным остатком, по возрастанию даты.

    Два применения:
      • ОЧЕРЕДЬ (FIFO): `before=moment платежа` — есть ли аванс СТАРШЕ разбираемого, который ещё
        не израсходован. Пока такой есть, свежий платёж приёмки не забирает (решение Сергея
        19.08.2026: сначала закрываем предыдущий, потом начинаем тратить следующий);
      • БАЛАНС поставщика (`advance_balance`) — сколько наших денег у него ещё лежит.

    Берём только платежи из банковской выписки (`from_bank`) и только со словом «аванс» в
    назначении — фильтром на СТОРОНЕ МС, как в `payments_since` (страница с `expand=operations`
    дорогая, тянуть все исходящие ради двух авансов незачем)."""
    terms = [f"agent={agent_href}", f"organization={org_href}",
             f"moment>={since} 00:00:00", "paymentPurpose~аванс"]
    if before:
        terms.append(f"moment<{before}")
    flt = urllib.parse.quote(";".join(terms), safe="=;:")
    out, off = [], 0
    while True:
        page = get(f"/entity/paymentout?filter={flt}&expand=operations&limit=100&offset={off}")
        rows = page.get("rows", [])
        out.extend(rows)
        if len(rows) < 100:
            break
        off += 100
    return sorted((p for p in out if from_bank(p) and unlinked_sum(p) > 0),
                  key=lambda p: ((p.get("moment") or ""), p.get("id") or ""))


def advance_balance(agent_href, org_href, since=None):
    """Баланс поставщика = Σ НЕИЗРАСХОДОВАННЫХ остатков его авансов (копейки) + сами платежи.

    Это то же число, что видно в МС: сумма платежа минус всё, что уже привязано к приёмкам.
    Заказы поставщику сюда НЕ входят — заказ ещё не расход аванса, деньги у поставщика лежат,
    пока не пришла приёмка (решение Сергея 19.08.2026; прежний расчёт по заказам завышал расход
    и плодил лишние авансы)."""
    since = since or (dt.date.today() - dt.timedelta(days=ADVANCE_QUEUE_LOOKBACK)).isoformat()
    rows = open_advances_of(agent_href, org_href, since)
    return sum(unlinked_sum(p) for p in rows), rows


def plan_advance(payment):
    """Раскладка АВАНСА: неоплаченные приёмки этого поставщика по возрастанию даты, пока хватает
    денег; хвостовая закрывается частично. Правило снято с ручной практики владельца и сверено
    на его 11 авансах (июнь–июль 2026): 8 совпали до копейки, 3 «разошлись» только тем, что наш
    расчёт добирал свежие приёмки, которые он ещё не успел разнести.

    Отличия от привязки по номеру:
      • ОСТАТОК АВАНСА — НОРМА, а не ошибка: деньги ждут будущих поставок (у аванса №498 на
        22.07 так и висело 73 817 ₽). Поэтому «остаток ≠ 0 ⇒ не привязывать» здесь не действует.
      • Привязка ДОБИРАЕТСЯ: на каждом прогоне к уже привязанным приёмкам добавляются новые,
        пока аванс не израсходован. Поэтому возвращаем ПОЛНЫЙ массив operations (старое+новое),
        слитый по приёмке: МС принимает `operations` целиком, а не дельтой.

    → (operations, неизрасходованный_остаток_копеек, пояснение). operations == [] ⇒ добавить нечего.

    ГРАБЛЯ ЧТЕНИЯ DRY-RUN: остатки приёмок читаются из МС на момент вызова, поэтому в dry-run два
    аванса ОДНОГО поставщика могут «забрать» одну и ту же неоплаченную приёмку — на бумаге выйдет
    двойной счёт. В `--apply` этого не происходит: платежи обрабатываются последовательно, и второй
    уже видит `payedSum`, обновлённый записью первого.
    """
    agent_href, org_href = _agent_href(payment), _org_href(payment)
    if not agent_href or not org_href:
        return [], payment["sum"], "у платежа нет контрагента или юрлица"
    pdate = (payment.get("moment") or "")[:10]
    since = (dt.date.fromisoformat(pdate) - dt.timedelta(days=ADVANCE_LOOKBACK)).isoformat()
    # уже привязанное этим же платежом: и остаток аванса, и база для слияния
    own = {}
    for o in payment.get("operations") or []:
        sid = ((o.get("meta") or {}).get("href") or "").rsplit("/", 1)[-1].split("?")[0]
        own[sid] = own.get(sid, 0) + (o.get("linkedSum") or 0)
    left = payment["sum"] - sum(own.values())
    if left <= 0:
        return [], 0, "аванс уже разнесён полностью"
    merged, added, taken = dict(own), 0, 0
    for s in agent_supplies(agent_href, since, org_href):
        # payedSum уже включает наш собственный вклад — берём только реально неоплаченный остаток
        unpaid = (s.get("sum") or 0) - (s.get("payedSum") or 0)
        take = min(unpaid, left)
        if take <= 0:
            continue
        merged[s["id"]] = merged.get(s["id"], 0) + take
        left -= take
        added += 1
        taken += take
        if left <= 0:
            break
    if not added:
        return [], left, f"новых неоплаченных приёмок нет; не разнесено {left/100:,.2f} ₽"
    ops = [{"meta": {"href": f"{MS}/entity/supply/{sid}", "type": "supply",
                     "mediaType": "application/json"}, "linkedSum": v}
           for sid, v in merged.items()]
    note = (f"аванс: +{added} приёмк(и) на {taken/100:,.2f} ₽" +
            (f", остаётся нераспределённым {left/100:,.2f} ₽" if left > 0 else ", аванс израсходован"))
    return ops, left, note


def plan(payment):
    """→ (operations, остаток_копеек, пояснение). Остаток ≠ 0 ⇒ привязывать НЕЛЬЗЯ."""
    supplies, missed = resolve_supplies(payment)
    if not supplies:
        return [], payment["sum"], f"документы не найдены (номера: {missed or '—'})"
    ops, left = distribute(payment["sum"], supplies)
    note = f"приёмок {len(ops)}"
    if missed:
        note += f", не найдено по номерам: {', '.join(missed)}"
    return ops, left, note


def _ops_map(operations):
    """operations → {id приёмки: сумма привязки}. Для сравнения «а не то же ли самое уже стоит»."""
    out = {}
    for o in operations or []:
        sid = ((o.get("meta") or {}).get("href") or "").rsplit("/", 1)[-1].split("?")[0]
        out[sid] = out.get(sid, 0) + (o.get("linkedSum") or 0)
    return out


def unlinked_sum(payment):
    """Сколько денег платежа ещё НЕ привязано ни к одной приёмке (копейки).
    У аванса это нормальное состояние: деньги ушли вперёд и ждут будущих поставок."""
    return (payment.get("sum") or 0) - sum(_ops_map(payment.get("operations")).values())


def payments_since(since, limit=100, until=None, purpose=None):
    """Исходящие платежи с даты `since`, ПОСТРАНИЧНО. Постранично не для красоты: исходящих
    ~100 в месяц, и на широком окне добора авансов одна страница молча обрезала бы хвост.

    `until` и `purpose` — отбор на СТОРОНЕ МС, а не у нас (замер 12.08.2026): страница
    `paymentout` с `expand=agent,operations` стоит ~5 с, и добор за 90 дней тянул 431 платёж
    (5 страниц, 33 с) ради 72 нужных. Широкое окно существует только ради авансов с остатком,
    а «аванс» — слово в назначении (`_ADVANCE`), значит его умеет отфильтровать сам МС:
    `paymentPurpose~аванс` (сравнение регистронезависимое, сверено — та же выборка авансов).
    """
    out, off = [], 0
    terms = [f"moment>={since} 00:00:00"]
    if until:
        terms.append(f"moment<{until} 00:00:00")
    if purpose:
        terms.append(f"paymentPurpose~{purpose}")
    flt = urllib.parse.quote(";".join(terms), safe="=;:")
    while True:
        page = get(f"/entity/paymentout?filter={flt}"
                   f"&expand=agent,operations&limit={limit}&offset={off}")
        rows = page.get("rows", [])
        out.extend(rows)
        if len(rows) < limit:
            break
        off += limit
    return out


def open_advances(payments):
    """Авансы с неизрасходованным остатком — кандидаты на ДОБОР на каждом прогоне (правило
    Сергея 2026-07-30): приёмка может прийти намного позже денег, поэтому такой платёж нельзя
    считать «разобранным» и забыть, пока остаток не исчерпан.

    Только авансы: у остальных платежей остаток означает частичную привязку РУКАМИ владельца
    (сами мы частичную не пишем — см. предохранитель в шапке модуля), туда лезть нельзя."""
    return [p for p in payments if unlinked_sum(p) > 0 and is_advance(p) and from_bank(p)]


def older_open_advance(payment):
    """Самый ранний НЕизрасходованный аванс того же поставщика/юрлица СТАРШЕ этого платежа.

    Пока он есть, разбираемый платёж приёмки не забирает: очередь строго по дате денег, иначе
    свежий аванс съедает поставки, а предыдущий висит неизрасходованным (случай Тонероптторга —
    платёж от 13.08 с остатком 38 771.78 ₽ при уже ушедшем авансе от 19.08).

    Не тупик: как только старший аванс израсходуется (в этом же проходе — он идёт по очереди
    раньше), гейт снимется сам. Если приёмок нет вообще, забирать всё равно нечего."""
    agent_href, org_href = _agent_href(payment), _org_href(payment)
    if not agent_href or not org_href or not payment.get("moment"):
        return None
    since = (dt.date.fromisoformat(payment["moment"][:10])
             - dt.timedelta(days=ADVANCE_QUEUE_LOOKBACK)).isoformat()
    rows = open_advances_of(agent_href, org_href, since, before=payment["moment"])
    return rows[0] if rows else None


def _write(payment, ops, note):
    st, resp = put(f"/entity/paymentout/{payment['id']}", {"operations": ops})
    return ("linked", note) if st in (200, 201) else ("error", f"HTTP {st}: {str(resp)[:200]}")


def link_payment(payment, apply=False):
    """→ (статус, пояснение). Статусы: linked / would-link / already / no-match / partial / error.

    Порядок разбора — сначала точный, потом приблизительный:
      0. **по черновику платёжки** (`payment_draft_queue.covers_po_ids`): состав платежа известен
         точно — мы сами его собрали; добирается на последующих прогонах;
      1. **по номерам документов** в назначении: раскладка обязана сойтись БЕЗ остатка;
      2. **аванс** (слово «аванс» в назначении): раскладка частичная по определению и
         ДОБИРАЕТСЯ на последующих прогонах.

    Черновик идёт первым (решение Сергея 2026-08-01): назначение платежа — лишь текстовое
    отражение того же списка заказов, а разбор текста хрупок (см. комментарий у `find_draft`).
    Разбор назначения остаётся для платежей, сделанных мимо наших черновиков (владелец платит
    из банка руками), и для старых платежей до появления очереди.

    Номер идёт первым нарочно: назначение может содержать И слово «аванс», И номер основания
    («Авансовый платеж по счету № …» — так пишут некоторые поставщики). Когда номер есть и
    раскладка по нему сошлась, привязка по документу точнее любого FIFO. Наши собственные платёжки
    сюда не попадают: предоплата по конкретному счёту слово «Аванс» не пишет (решение Сергея
    2026-07-29) — «Аванс» только у метода аванс/баланс, где основания нет.

    Перед обоими путями — гейт `supplier_agents()`: платежи не-поставщиков не разбираем вообще.
    """
    our = org_inn(payment)
    if not our:
        return "not-supplier", "у платежа не указано наше юрлицо"
    if _agent_id(payment) not in supplier_agents(our):
        return "not-supplier", f"контрагента нет в условиях оплаты юрлица {our}"

    advance = is_advance(payment)

    # 1) черновик платёжки — состав платежа известен точно, потому что мы его и составляли
    d_ops, d_left, d_note, d_id = plan_draft(payment)
    if d_ops is not None:
        if not d_ops:
            return ("already" if d_left <= 0 else "no-match"), d_note
        if _ops_map(payment.get("operations")) == _ops_map(d_ops):
            return "already", d_note
        if not apply:
            return "would-link", d_note
        st, msg = _write(payment, d_ops, d_note)
        if st == "linked":
            # закрепляем черновик за платежом: второй платёж той же суммы его уже не возьмёт
            db.execute("UPDATE payment_draft_queue SET ms_payment_id=%s WHERE id=%s",
                       (payment["id"], d_id))
        return st, msg

    if payment.get("operations") and not advance:
        return "already", "уже привязан"        # дёшево: без похода в МС за приёмками

    ops, left, note = plan(payment)              # 2) по номерам документов из назначения
    if ops and left == 0:
        if _ops_map(payment.get("operations")) == _ops_map(ops):
            return "already", "уже привязан по номеру документа"
        return ("would-link", note) if not apply else _write(payment, ops, note)

    if advance:                                  # 2) аванс — FIFO по неоплаченным приёмкам
        if not ADVANCE_ON:
            return "advance-off", "аванс: разнос выключен (ALFA_LINK_ADVANCE≠1)"
        if not from_bank(payment):
            # ручной аванс до подключения банка: его разносил человек, мы в него не лезем
            return "manual-skip", "аванс заведён в МС руками (нет syncId) — не привязываем"
        older = older_open_advance(payment)
        if older:
            return "advance-wait", (
                f"очередь: сначала расходуется аванс №{older.get('name')} от "
                f"{(older.get('moment') or '')[:10]}, на нём ещё "
                f"{unlinked_sum(older)/100:,.2f} ₽")
        a_ops, a_left, a_note = plan_advance(payment)
        if not a_ops:
            # остаток исчерпан — аванс закрыт; остаток есть, но приёмок нет — ждём поставки
            return ("already" if a_left <= 0 else "no-match"), a_note
        return ("would-link", a_note) if not apply else _write(payment, a_ops, a_note)

    if not ops:
        return "no-match", note
    # платёж не раскладывается по приёмкам без остатка — учёт важнее «хоть как-то привязать»
    return "partial", f"{note}; не распределено {left/100:.2f} ₽ — нужен ручной разбор"


def link_new(payments, apply=False):
    """Привязка пачки платежей (вызывается из alfa_ms.sync). → (stats, строки лога)."""
    stats, lines = {"linked": 0, "would_link": 0, "already": 0, "no_match": 0,
                    "partial": 0, "errors": 0, "advance_off": 0, "not_supplier": 0,
                    "advance_wait": 0, "manual_skip": 0}, []
    for p in payments:
        status, note = link_payment(p, apply=apply)
        key = {"linked": "linked", "would-link": "would_link", "already": "already",
               "no-match": "no_match", "partial": "partial", "error": "errors",
               "advance-off": "advance_off", "not-supplier": "not_supplier",
               "advance-wait": "advance_wait", "manual-skip": "manual_skip"}[status]
        stats[key] += 1
        if status in ("partial", "error"):
            lines.append(f"платёж №{p.get('name')} {p['sum']/100:.2f} ₽: {note}")
    return stats, lines


def main(argv):
    since = next((a for a in argv if not a.startswith("--")), None)
    apply = "--apply" in argv
    if not since:
        raise SystemExit("укажи дату начала: alfa_link.py 2026-07-01 [--apply]")
    flt = urllib.parse.quote(f"moment>={since} 00:00:00")
    rows = get(f"/entity/paymentout?filter={flt}&expand=agent,operations&limit=100").get("rows", [])
    print(f"исходящих платежей с {since}: {len(rows)} — {'ЗАПИСЬ' if apply else 'DRY-RUN'}")
    for p in sorted(rows, key=lambda x: ((x.get("moment") or ""), x.get("id") or "")):
        status, note = link_payment(p, apply=apply)
        if status in ("already", "not-supplier") and "--all" not in argv:
            continue
        mark = {"linked": "✓", "would-link": "•", "partial": "⚠", "error": "✗",
                "no-match": "·", "advance-off": "○", "not-supplier": "–",
                "advance-wait": "⏳", "manual-skip": "✋",
                "already": "="}.get(status, "?")   # `--all` показывает и уже разнесённые
        print(f"  {mark} №{p.get('name'):>8} {p['sum']/100:>11.2f} ₽ "
              f"{((p.get('agent') or {}).get('name') or '')[:26]:26} {status}: {note}")


if __name__ == "__main__":
    main(sys.argv[1:])
