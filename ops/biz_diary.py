# поток: ev — дневник управленческих событий: что мы меняли, когда и с каким ожиданием
"""Дневник важных событий бизнеса — слой, который кладётся ПОВЕРХ цифр.

Зачем: отчёты маркетплейсов показывают, ЧТО произошло, но не помнят, ПОЧЕМУ. Выручка Дисквэра
просела 20 августа — это рынок, сезон или мы сами отключили «Звёздные товары»? Без записи
ответа нет, и через месяц его уже никто не восстановит. Дневник хранит решения рядом с
цифрами, чтобы недельная динамика читалась вместе с ними, а не отдельно.

Три источника событий живут в ОДНОЙ таблице `biz_events` и различаются полем `kind`:
  own    — наши решения (отключили программу, запустили бандлы, сменили график выплат);
  mp     — изменения на стороне площадки (тарифы, комиссии, автоподключённые услуги);
  supply — поставка/остатки (пишет `ops/stock_watch.py` сам, руками не заводим).

Почему одна таблица, а не три: на главной они нужны ОДНИМ хронологическим списком — читателю
важна дата и влияние на цифры, а не чей это источник. Разделять начнём тогда, когда появится
разная логика хранения, а не разная подпись.

Запуск:
  ./venv/bin/python -m ops.biz_diary --list                       последние записи
  ./venv/bin/python -m ops.biz_diary --add "20.08 отключили ..."   разбор свободного текста
  ./venv/bin/python -m ops.biz_diary --seed                        события из записки Натальи
"""
import argparse
import re
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, "/opt/mp-analytics")
from dotenv import load_dotenv

load_dotenv("/opt/mp-analytics/.env")

from core import db  # noqa: E402

KINDS = ("own", "mp", "supply")
PLATFORMS = ("wb", "ozon", "yandex")

# Разбор свободного текста для бота. Пишет человек с телефона одной строкой, поэтому
# распознаём то, что он назовёт наверняка: дату, площадку и аккаунт. Всё остальное —
# заголовок события как есть. Чего не поняли — оставляем пустым и спрашиваем кнопкой,
# а не угадываем: неверно проставленная площадка хуже пустой (событие сядет не на тот график).
PLATFORM_WORDS = {
    "ozon": ("озон", "ozon"),
    "wb": ("вб", "вайлдберриз", "wb", "wildberries"),
    "yandex": ("маркет", "яндекс", "yandex", "market"),
}
# Аккаунты Сергей и Наталья называют по фирме, а не по коду в базе.
ACCOUNT_WORDS = {
    "oz_acc2": ("дисквэр", "дисквер", "disquare", "dsquare"),
    "oz_acc1": ("цифровой", "цифровому", "премиум-про", "премиум про"),
}
MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}


def parse_date(text, today=None):
    """Дата события из текста. -> (date | None, остаток текста).

    Человек пишет «20.08», «20 августа», «вчера», «во вторник». Год почти никогда не пишет —
    подставляем текущий, но с оговоркой: если получилась дата в будущем больше чем на неделю,
    значит речь о прошлом декабре — отматываем год назад. Иначе запись «29.12», сделанная
    в январе, уедет на 11 месяцев вперёд и не попадёт ни в один отчёт.
    """
    today = today or date.today()
    low = text.lower()

    m = re.search(r"\b(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?\b", low)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        year = today.year if not y else (2000 + int(y) if len(y) == 2 else int(y))
        try:
            got = date(year, mo, d)
        except ValueError:
            return None, text
        if not y and got > today + timedelta(days=7):
            got = date(year - 1, mo, d)
        return got, (text[:m.start()] + text[m.end():]).strip()

    m = re.search(r"\b(\d{1,2})\s+([а-я]{3})[а-я]*\b", low)
    if m and m.group(2) in MONTHS:
        try:
            got = date(today.year, MONTHS[m.group(2)], int(m.group(1)))
        except ValueError:
            return None, text
        if got > today + timedelta(days=7):
            got = date(today.year - 1, got.month, got.day)
        return got, (text[:m.start()] + text[m.end():]).strip()

    for word, delta in (("сегодня", 0), ("вчера", 1), ("позавчера", 2)):
        if word in low:
            i = low.index(word)
            return today - timedelta(days=delta), (text[:i] + text[i + len(word):]).strip()

    return None, text


def parse_free(text, today=None):
    """Свободная строка -> черновик события. Не угаданное оставляем пустым."""
    when, rest = parse_date(text, today)
    low = text.lower()
    platform = next((p for p, words in PLATFORM_WORDS.items()
                     if any(w in low for w in words)), None)
    account = next((a for a, words in ACCOUNT_WORDS.items()
                    if any(w in low for w in words)), None)
    # Аккаунт назвали, площадку нет: «Дисквэр» и «Цифровой» — озоновские коды, площадка ясна.
    if account and not platform:
        platform = "ozon"
    title = re.sub(r"\s+", " ", (rest or text)).strip(" ,.;—-")
    return {"event_date": when, "kind": "own", "platform": platform,
            "account": account, "title": title[:200] or text[:200],
            "details": text if len(text) > 200 else None}


def add(event_date, title, kind="own", platform=None, account=None, details=None,
        expect=None, date_to=None, review_at=None, author=None, source="web",
        scope_kind="all", scope_ref=None, dedup_key=None, mark=None):
    """Запись в дневник. -> id или None, если событие с таким dedup_key уже есть."""
    if kind not in KINDS:
        raise ValueError(f"kind должен быть одним из {KINDS}, а не {kind!r}")
    if platform and platform not in PLATFORMS:
        raise ValueError(f"platform должен быть одним из {PLATFORMS}, а не {platform!r}")
    if not title or not str(title).strip():
        raise ValueError("у события должен быть заголовок")
    # Срок разбора эффекта: если человек не назвал свой, ставим +21 день. Три недели — это
    # три сопоставимых недельных точки (правило сравнимости дней недели), меньше — шум.
    if review_at is None and kind == "own":
        review_at = (event_date or date.today()) + timedelta(days=21)
    rows = db.query("""
        INSERT INTO biz_events (event_date, date_to, kind, platform, account,
                                scope_kind, scope_ref, title, details, expect,
                                review_at, author, source, dedup_key, mark)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (dedup_key) DO NOTHING
        RETURNING id
    """, (event_date or date.today(), date_to, kind, platform, account, scope_kind,
          scope_ref, str(title).strip()[:200], details, expect, review_at, author,
          source, dedup_key, mark))
    return rows[0]["id"] if rows else None


def recent(limit=20, kind=None, platform=None, days=None):
    """Последние события — для главной страницы и бота."""
    where, params = ["1=1"], []
    if kind:
        where.append("kind = %s")
        params.append(kind)
    if platform:
        where.append("(platform = %s OR platform IS NULL)")
        params.append(platform)
    if days:
        where.append("event_date >= current_date - %s::int")
        params.append(days)
    params.append(limit)
    return db.query(f"""
        SELECT id, event_date::text, date_to::text, kind, platform, account,
               title, details, expect, review_at::text, author, source, mark
        FROM biz_events WHERE {' AND '.join(where)}
        ORDER BY event_date DESC, id DESC LIMIT %s
    """, tuple(params))


def by_week(weeks=8):
    """События, разложенные по неделям (пн) — для маркеров 📌 в недельной динамике."""
    rows = db.query("""
        SELECT date_trunc('week', event_date)::date::text AS week,
               id, event_date::text, kind, platform, account, title
        FROM biz_events
        WHERE event_date >= date_trunc('week', current_date) - (%s::int * interval '1 week')
        ORDER BY event_date
    """, (weeks,))
    out = {}
    for r in rows:
        out.setdefault(r["week"], []).append(r)
    return out


EDITABLE = ("event_date", "date_to", "platform", "account", "title",
            "details", "expect", "review_at")


def update(event_id, **fields):
    """Правка события. -> обновлённая запись или None, если такой записи нет.

    Править разрешаем наши решения (`kind='own'`) и записки из бота (`source='dropbox'`).
    Запрет касается только того, что переписывает автоматика: события площадок собраны из их
    новостей и пересчитываются `--reclassify`, записи об остатках пишет сторож — ручная правка
    там потерялась бы молча. Записку из бота не пересчитывает никто (её dedup_key — имя файла),
    а править её надо чаще всего: человек пишет на бегу и потом уточняет (случай 23.08 —
    «Чапаевск» оказался «Самара РФЦ»).
    """
    bad = set(fields) - set(EDITABLE)
    if bad:
        raise ValueError(f"эти поля не правим: {', '.join(sorted(bad))}")
    if "platform" in fields and fields["platform"] and fields["platform"] not in PLATFORMS:
        raise ValueError(f"platform должен быть одним из {PLATFORMS}")
    if "title" in fields:
        if not fields["title"] or not str(fields["title"]).strip():
            raise ValueError("у события должен быть заголовок")
        fields["title"] = str(fields["title"]).strip()[:200]
    cur = db.query("SELECT kind, source FROM biz_events WHERE id = %s", (event_id,))
    if not cur:
        return None
    if cur[0]["kind"] != "own" and cur[0]["source"] != "dropbox":
        raise ValueError("править можно наши решения и записки из бота — "
                         "событие площадки и запись сторожа правке не подлежат")
    if not fields:
        return db.query("SELECT * FROM biz_events WHERE id = %s", (event_id,))[0]
    sets = ", ".join(f"{k} = %s" for k in fields)
    rows = db.query(f"UPDATE biz_events SET {sets} WHERE id = %s RETURNING *",
                    tuple(fields.values()) + (event_id,))
    return rows[0] if rows else None


def resolve(event_id, result, mark="✅"):
    """Задача из записки выполнена — в дневнике вместо задачи стоит РЕЗУЛЬТАТ.

    Правило Сергея 23.08.2026. Записка человека часто приходит как «событие + поручение»
    («по Оренбургу удар БПЛА — посмотри, что у нас там»). Если оставить в ленте текст
    поручения, на главной висит задача, по которой не видно, сделана она или нет, — и через
    неделю никто уже не помнит. Поэтому: поручение выполняем сразу, а текст события заменяем
    на ответ («нашего товара на складе нет»). Значок 📥 («из бота, не разобрано») меняется
    на ✅ — видно, что вопрос закрыт, и лента остаётся лентой событий, а не списком дел.

    Заменяем, а не дописываем: две версии текста (задача + ответ) читаются дольше, чем ответ,
    и заставляют человека решать, актуальна ли ещё первая. Исходная записка не теряется —
    она лежит файлом в dropbox и ключом `dedup_key`.
    """
    if not result or not str(result).strip():
        raise ValueError("результат не может быть пустым — иначе задача просто исчезнет")
    rows = db.query("UPDATE biz_events SET details = %s, mark = %s WHERE id = %s RETURNING *",
                    (str(result).strip(), mark, event_id))
    return rows[0] if rows else None


def delete(event_id):
    return db.query("DELETE FROM biz_events WHERE id = %s RETURNING id", (event_id,))


# Записка Натальи от 20.08.2026 (dropbox/20260821_051933). Даты «во вторник на этой неделе»
# и «в начале августа» разложены в конкретные: вторник этой недели = 18.08, начало августа =
# 01.08 (точную Наталья поправит на дашборде — редактирование там есть).
SEED = [
    dict(event_date=date(2026, 8, 20), kind="own", platform="ozon", account="oz_acc2",
         title="Отключили «Звёздные товары»",
         details="Программа Ozon «Звёздные товары» отключена на аккаунте Дисквэр.",
         expect="Расход на программу уходит из P&L; риск — падение показов и заказов.",
         author="Наталья"),
    dict(event_date=date(2026, 8, 20), kind="own", platform="ozon", account=None,
         title="Перешли на выплаты раз в 2 недели (оба аккаунта)",
         details="Ozon: график выплат сменён на двухнедельный по Цифровому и по Дисквэру.",
         expect="Меняется ритм поступлений — сверять кассовый разрыв, не выручку.",
         author="Наталья"),
    dict(event_date=date(2026, 8, 18), kind="own", platform="yandex", account="oz_acc1",
         title="Запустили бандлы на Маркете (Цифровой)",
         expect="Рост среднего чека и конверсии; смотреть долю заказов с бандлом.",
         author="Наталья"),
    dict(event_date=date(2026, 8, 1), kind="own", platform="ozon", account="oz_acc1",
         title="Запустили бандлы на Ozon (Цифровой)",
         expect="Рост среднего чека; эффект мерить неделями пн–вс, не днями.",
         author="Наталья"),
]


def seed():
    added = 0
    for ev in SEED:
        key = f"seed:{ev['event_date']}:{ev['title'][:40]}"
        if add(source="seed", dedup_key=key, **ev):
            added += 1
    print(f"события из записки: добавлено {added}, всего в дневнике "
          f"{db.query('SELECT count(*) n FROM biz_events')[0]['n']}")


def show(limit=20):
    rows = recent(limit)
    if not rows:
        print("дневник пуст")
        return
    mark = {"own": "🔧", "mp": "🏪", "supply": "📦"}
    print(f"{'дата':11} {'':2} {'площадка':9} {'аккаунт':8} событие")
    for r in rows:
        print(f"{r['event_date']:11} {mark.get(r['kind'], '·'):2} "
              f"{r['platform'] or '—':9} {r['account'] or '—':8} {r['title'][:60]}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Дневник управленческих событий")
    ap.add_argument("--add", metavar="ТЕКСТ", help="добавить событие из свободного текста")
    ap.add_argument("--list", action="store_true", help="показать последние записи")
    ap.add_argument("--seed", action="store_true", help="завести события из записки Натальи")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--resolve", type=int, metavar="ID", help="закрыть задачу события результатом")
    ap.add_argument("--result", metavar="ТЕКСТ", help="текст результата для --resolve")
    args = ap.parse_args(argv)

    if args.resolve:
        if not args.result:
            print("нужен --result: чем закончилась задача")
            return 1
        row = resolve(args.resolve, args.result)
        print(f"#{args.resolve}: результат записан" if row else f"#{args.resolve} не найдено")
        return 0

    if args.seed:
        seed()
    if args.add:
        draft = parse_free(args.add)
        if not draft["event_date"]:
            print("дату не разобрал — укажи «20.08» или «вчера» в тексте")
            return 1
        new_id = add(source="cli", **draft)
        print(f"записано #{new_id}: {draft['event_date']} · {draft['title']}")
    if args.list or not (args.add or args.seed):
        show(args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
